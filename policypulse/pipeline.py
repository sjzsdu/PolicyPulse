"""端到端流水线：抓取 -> 去重 -> 抽取 -> 映射 -> 打分 -> 落库 -> 报告。"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from policypulse import llm
from policypulse.config import DISCLAIMER, Settings, load_sources, load_theme_alias
from policypulse.extract import classify_meeting_type, extract_signals, verify_quote
from policypulse.http import FetchError, Fetcher, content_hash
from policypulse.mapping import build_candidates, match_sectors
from policypulse.scoring import SCORE_VERSION, rank, why_of
from policypulse.sources.registry import get_adapter
from policypulse.store import Store
from policypulse.text import (
    extract_main_text,
    extract_publish_time,
    extract_title,
    html_to_text,
)
from policypulse.tongstock import CONCEPT_FILE, INDUSTRY_FILE, TongstockClient, TongstockError


@dataclass
class DocRecord:
    doc_id: str
    url: str
    title: str
    org: str
    tier: str
    source_id: str
    meeting_type: str
    published_at: str
    fetched_at: str
    content_hash: str
    raw_hash: str
    snapshot_path: str
    body: str
    duplicate_of: str = ""
    # H：窗口内该栏目无更新时，退回去抓入口页最近几条，产物里要能看到
    out_of_window_fallback: bool = False


@dataclass
class RunResult:
    run_id: str
    window_start: str
    window_end: str
    docs: list[DocRecord] = field(default_factory=list)
    signals: list = field(default_factory=list)
    conclusions: list[dict] = field(default_factory=list)
    failed_sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duplicates_skipped: int = 0
    stats: dict = field(default_factory=dict)


def doc_id_for(url: str) -> str:
    return "doc-" + content_hash(url)[:16]


def _in_window(published: str, start: str, end: str) -> bool:
    if not published:
        return True  # 无日期的按 fetched 处理，交给调用方
    return start <= published <= end


def fetch_documents(fetcher: Fetcher, specs, settings: Settings, window_start: str,
                    window_end: str, snapshot_dir: Path, run_id: str,
                    store: Store | None = None) -> tuple[list[DocRecord], list[str], int]:
    docs: list[DocRecord] = []
    failed: list[str] = []
    seen_hashes: dict[str, str] = {}
    duplicates_skipped = 0

    for spec in specs:
        if not spec.enabled:
            continue
        # E：源的 rate_limit_seconds 真接上，不再硬编码
        fetcher.configure_rate_limit(spec.url, spec.rate_limit_seconds)
        try:
            adapter = get_adapter(spec)
            items = adapter.discover(spec, fetcher)
        except (FetchError, KeyError, ValueError) as exc:
            failed.append(f"{spec.id}: {type(exc).__name__}: {exc}")
            continue

        picked = []
        for item in items:
            fetcher.configure_rate_limit(item.url, spec.rate_limit_seconds)
            if item.published_at and not _in_window(item.published_at, window_start, window_end):
                continue
            picked.append(item)
            if len(picked) >= settings.max_docs_per_source:
                break
        fallback_used = False
        if not picked and items:
            # 该栏目在窗口内没有更新：抓入口页最近几条，保证链路有真实素材
            picked = items[: min(3, settings.max_docs_per_source)]
            fallback_used = True
            for item in picked:
                fetcher.configure_rate_limit(item.url, spec.rate_limit_seconds)

        for item in picked:
            try:
                html = fetcher.get_text(item.url)
            except FetchError as exc:
                failed.append(f"{spec.id}/{item.url}: {exc}")
                continue

            title = extract_title(html) or item.title
            title = re.sub(r"\s*[-—|]\s*中国政府网\s*$", "", title).strip() or item.title
            body = extract_main_text(html)
            if len(body) < 200:
                continue
            published = item.published_at or extract_publish_time(html, body)

            digest = content_hash(body)
            # 原文指纹取可见文本：忽略脚本注入的计数器/时间戳噪声
            raw_digest = content_hash(html_to_text(html))
            if digest in seen_hashes:
                continue
            seen_hashes[digest] = item.url

            # F：跨 run 去重——正文指纹已入库就不再重复抽
            if settings.cross_run_dedupe and store is not None:
                known = store.document_exists_hash(digest)
                if known:
                    duplicates_skipped += 1
                    continue

            doc_id = doc_id_for(item.url)
            snap = snapshot_dir / f"{doc_id}.html"
            snap.write_text(html, encoding="utf-8")

            docs.append(DocRecord(
                doc_id=doc_id,
                url=item.url,
                title=title,
                org=spec.org,
                tier=spec.tier,
                source_id=spec.id,
                meeting_type=spec.meeting_type_hint or classify_meeting_type(title),
                published_at=published,
                fetched_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                content_hash=digest,
                raw_hash=raw_digest,
                snapshot_path=str(snap),
                body=body,
                out_of_window_fallback=fallback_used,
            ))
    return docs, failed, duplicates_skipped


def build_conclusions(docs, store: Store, settings: Settings, client: TongstockClient,
                      run_id: str, alias_map) -> tuple[list, list[dict]]:
    """抽取信号并映射到个股；返回 (signals, conclusions)。"""
    concept_sectors = list(client.sectors(CONCEPT_FILE).keys())
    industry_sectors = list(client.sectors(INDUSTRY_FILE).keys())
    universe = client.tickers()

    all_signals = []
    conclusions: list[dict] = []
    for doc in docs:
        store.upsert_document({
            "doc_id": doc.doc_id, "run_id": run_id, "source_id": doc.source_id,
            "url": doc.url, "canonical_url": doc.url, "title": doc.title,
            "publisher": doc.org, "org": doc.org, "source_tier": doc.tier,
            "meeting_type": doc.meeting_type, "published_at": doc.published_at,
            "fetched_at": doc.fetched_at, "content_hash": doc.content_hash,
            "raw_hash": doc.raw_hash, "raw_snapshot_path": doc.snapshot_path, "duplicate_of": doc.duplicate_of or None,
        })

        if llm.llm_available():
            signals = _signals_from_llm(doc, store, settings)
            if not signals:
                signals = extract_signals(doc.doc_id, doc.title, doc.body, alias_map, settings)
        else:
            signals = extract_signals(doc.doc_id, doc.title, doc.body, alias_map, settings)

        for sig in signals:
            store.upsert_signal(sig, run_id)
            all_signals.append(sig)

            matches = match_sectors(sig.theme_tags, concept_sectors, industry_sectors, alias_map)
            candidates = build_candidates(matches, client, settings)
            ranked = rank(sig, candidates, settings)

            tickers = []
            for pos, cand in enumerate(ranked, start=1):
                store.upsert_ticker(cand.ticker.code, cand.ticker.name, cand.ticker.exchange,
                                    cand.ticker.board, cand.ticker.market_cap_yi)
                store.upsert_sector(cand.sector_name, cand.sector_source,
                                    len(client.members(cand.sector_name, _file_of(cand.sector_source))))
                store.upsert_sector_member(cand.sector_name, cand.sector_source, cand.ticker.code)
                store.upsert_signal_ticker(sig.signal_id, cand.ticker.code, why_of(sig, cand),
                                           cand.score, pos, cand.mapping_path,
                                           cand.mapping_confidence, cand.sector_name,
                                           cand.sector_source)
                tickers.append({
                    "ticker_code": cand.ticker.code,
                    "ticker_name": cand.ticker.name,
                    "exchange": cand.ticker.exchange,
                    "board": cand.ticker.board,
                    "market_cap": cand.ticker.market_cap_yi,
                    "sector_name": cand.sector_name,
                    "sector_source": cand.sector_source,
                    "why": why_of(sig, cand),
                    "score": cand.score,
                    "rank": pos,
                    "mapping_path": cand.mapping_path,
                    "mapping_confidence": cand.mapping_confidence,
                })

            conclusions.append({
                "signal_id": sig.signal_id,
                "doc_id": doc.doc_id,
                "summary": sig.summary,
                "direction": sig.direction,
                "strength": sig.strength,
                "horizon": sig.horizon,
                "novelty": sig.novelty,
                "instruments": sig.instruments,
                "theme_tags": sig.theme_tags,
                "confidence": sig.confidence,
                "quote": sig.quote,
                "quote_verified": sig.quote_verified,
                "source_url": doc.url,
                "publisher": doc.org,
                "org": doc.org,
                "meeting_type": doc.meeting_type,
                "published_at": doc.published_at,
                "fetched_at": doc.fetched_at,
                "content_hash": doc.content_hash,
                "raw_hash": doc.raw_hash,
                "raw_snapshot_path": doc.snapshot_path,
                "model": sig.model,
                "prompt_version": sig.prompt_version,
                "matched_sectors": [m.sector_name for m in matches],
                "out_of_window_fallback": doc.out_of_window_fallback,
                "tickers": tickers,
            })
    return all_signals, conclusions


def _file_of(sector_source: str) -> str:
    return CONCEPT_FILE if sector_source == "通达信概念" else INDUSTRY_FILE


def _signals_from_llm(doc, store: Store, settings: Settings):
    from policypulse.extract import Signal

    try:
        raw = llm.extract_with_llm(doc.title, doc.body)
    except Exception:  # noqa: BLE001 - LLM 不可用时静默回退规则抽取
        return []
    out = []
    for i, item in enumerate(raw, start=1):
        quote = str(item.get("quote", "")).strip()
        sig = Signal(
            signal_id=f"{doc.doc_id}#llm{i}",
            doc_id=doc.doc_id,
            summary=str(item.get("summary", ""))[:80],
            quote=quote[:200],
            direction=str(item.get("direction", "中性")),
            strength=int(item.get("strength", 3)),
            horizon=str(item.get("horizon", "中期(1–6月)")),
            novelty=str(item.get("novelty", "延续")),
            instruments=list(item.get("instruments", [])),
            theme_tags=list(item.get("theme_tags", []))[:5],
            affected_regions=list(item.get("affected_regions", [])),
            confidence=float(item.get("confidence", 0.5)),
            model=llm.PROMPT_VERSION,
            prompt_version=llm.PROMPT_VERSION,
        )
        sig.quote_verified = verify_quote(sig.quote, doc.body)
        if not sig.quote_verified:
            sig.confidence = min(sig.confidence, 0.4)
        store.log_llm_call(sig.signal_id, llm.PROMPT_VERSION, llm.PROMPT_VERSION,
                           doc.title, json.dumps(item, ensure_ascii=False))
        out.append(sig)
    return out


def run(settings: Settings, paths, target_date: date | None = None,
        sources_file: Path | None = None, refresh_market: bool = False,
        only_sources: list[str] | None = None, fetcher: Fetcher | None = None,
        market_client=None) -> RunResult:
    target = target_date or date.today()
    window_start = (target - timedelta(days=settings.lookback_days - 1)).isoformat()
    window_end = target.isoformat()
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    all_specs = load_sources(sources_file) if sources_file else load_sources()
    if only_sources:
        all_specs = [s for s in all_specs if s.id in only_sources]
    specs = [s for s in all_specs if s.enabled]  # 关闭的源不计入成功率分母

    store = Store(paths.db)
    store.start_run(run_id, window_start, window_end, SCORE_VERSION)
    for spec in specs:
        store.sources_upsert(spec)

    snapshot_dir = paths.snapshots / run_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    alias_map = load_theme_alias()
    warnings: list[str] = []
    owns_fetcher = fetcher is None
    if owns_fetcher:
        # E：不再硬编码 2.0，默认间隔只作兜底，真实间隔由 sources.yaml 逐源下发
        fetcher = Fetcher(timeout=settings.request_timeout, retries=settings.request_retries,
                          user_agent=settings.user_agent, min_interval=2.0, verify_tls=True,
                          robots_unavailable_interval=settings.robots_unavailable_interval)
    try:
        docs, failed, duplicates = fetch_documents(
            fetcher, specs, settings, window_start, window_end, snapshot_dir, run_id, store)
        warnings.extend(getattr(fetcher, "warnings", []))
    finally:
        if owns_fetcher:
            fetcher.close()

    client = market_client or TongstockClient(settings, cache_dir=paths.cache,
                                              refresh=refresh_market)
    try:
        signals, conclusions = build_conclusions(docs, store, settings, client, run_id, alias_map)
    except TongstockError as exc:
        store.finish_run(run_id, "failed", len(docs), 0, 0, notes=f"tongstock: {exc}")
        raise

    ticker_count = sum(len(c["tickers"]) for c in conclusions)
    store.finish_run(run_id, "ok", len(docs), len(signals), ticker_count,
                     notes="; ".join(failed)[:500])

    result = RunResult(run_id=run_id, window_start=window_start, window_end=window_end,
                       docs=docs, signals=signals, conclusions=conclusions,
                       failed_sources=failed, warnings=warnings,
                       duplicates_skipped=duplicates)
    ok_sources = {d.source_id for d in docs}
    result.stats = {
        "docs": len(docs),
        "signals": len(signals),
        "with_tickers": sum(1 for c in conclusions if c["tickers"]),
        "ticker_rows": ticker_count,
        "sources_total": len(specs),
        # 一个源只要抓到 1 篇就算成功；单篇失败只计条目，不算源挂掉
        "sources_ok": len(ok_sources),
        "sources_failed": len(specs) - len(ok_sources),
        "fetch_failures": len(failed),
        "duplicates_skipped": duplicates,
        "out_of_window_docs": sum(1 for d in docs if d.out_of_window_fallback),
        "min_strength_for_report": settings.min_strength_for_report,
        "tongstock_calls": client.calls,
        "llm_enabled": llm.llm_available(),
        "disclaimer": DISCLAIMER,
    }
    store.close()
    return result
