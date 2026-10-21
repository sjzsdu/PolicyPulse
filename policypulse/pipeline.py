"""端到端流水线：抓取 -> 去重 -> 抽取 -> 映射 -> 打分 -> 落库 -> 报告。"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from policypulse import llm
from policypulse.clustering import PolicyEvent, cluster_events
from policypulse.config import (
    DISCLAIMER,
    REPO_ROOT,
    Settings,
    load_sources,
    load_theme_alias,
    load_theme_business,
)
from policypulse.deepdive import DeepDive, build_deepdives
from policypulse.extract import classify_meeting_type, extract_signals, verify_quote
from policypulse.http import FetchError, Fetcher, content_hash
from policypulse.investability import assess_signal
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
    # 事件聚类后的政策事件（每篇文档/发布会一条，个股已按代码去重）
    events: list[PolicyEvent] = field(default_factory=list)
    # 个股深度研读；空列表 + warnings 里的跳过说明 = 没配 LLM 或凑不出溯源
    deepdives: list[DeepDive] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def doc_id_for(url: str) -> str:
    return "doc-" + content_hash(url)[:16]


# 网站在 <title> 后面挂的自家站名，入库前去掉，免得标题里混进来源站点
SITE_SUFFIX_RE = re.compile(
    r"\s*[-—|]\s*(中国政府网|新华网|人民网|中国经济网|中国金融新闻网|金融时报)\s*$")

# 转载源：从标题/机构名判断该文档是否与央行（中国人民银行）相关
PBC_MENTION_RE = re.compile(
    r"中国人民银行|央行|货币政策|公开市场业务|公开市场操作|存款准备金|降准|"
    r"贷款市场报价利率|LPR|再贷款|再贴现|逆回购|中期借贷便利|MLF")


def is_pbc_related(title: str, body: str) -> bool:
    """文档是否与央行（中国人民银行）相关。用于产物里指认央行条目。"""
    return bool(PBC_MENTION_RE.search(f"{title}\n{body[:1500]}"))


def _source_compliance(specs, docs, fetcher) -> list[dict]:
    """逐源的合规台账：启用状态 + robots 判定 + 本轮文档数。

    停用或本地入库的源**不发起任何网络请求**，因此这里只读 robots 缓存里已经
    发生过的判定，绝不为了填表去补一次请求（否则「没有直连 pbc.gov.cn」就不成立了）。
    """
    counts: dict[str, int] = {}
    for doc in docs:
        counts[doc.source_id] = counts.get(doc.source_id, 0) + 1
    observed = fetcher.robots.observed() if getattr(fetcher, "robots", None) else {}

    rows: list[dict] = []
    for spec in specs:
        host = urlparse(spec.url).netloc
        if not spec.enabled:
            status, reason = "未启用", "源已停用，本轮不发起任何请求"
        elif spec.adapter == "local_inbound":
            status, reason = "本地入库", "不发起网络请求：解析用户自己下载的本地文件"
        else:
            policy = observed.get(host)
            if policy is None:
                status, reason = "未判定", "本轮未对该 host 发起请求"
            else:
                status = policy.status
                reason = policy.reason
                if policy.crawl_delay:
                    reason += f"（站点声明 Crawl-delay: {policy.crawl_delay:g}s）"
        rows.append({
            "id": spec.id, "org": spec.org, "tier": spec.tier, "host": host,
            "adapter": spec.adapter, "column": spec.column,
            "enabled": bool(spec.enabled), "robots": status, "robots_reason": reason,
            "docs": counts.get(spec.id, 0),
        })
    return rows


def _in_window(published: str, start: str, end: str) -> bool:
    if not published:
        return True  # 无日期的按 fetched 处理，交给调用方
    return start <= published <= end


def fetch_documents(fetcher: Fetcher, specs, settings: Settings, window_start: str,
                    window_end: str, snapshot_dir: Path, run_id: str,
                    store: Store | None = None,
                    diagnostics: dict | None = None) -> tuple[list[DocRecord], list[str], int]:
    """抓取窗口内文档。

    本 issue 第 1 点：**不再有「窗口内无更新就退回抓入口页最近几条」的兜底**。
    栏目当天没更新就是没更新，日报如实写「今日无新增政策 / 事件」，
    绝不把 10 月 8 日的旧稿塞进 10 月 20 日的日报。

    `diagnostics`（可选）：调用方可传 dict 接收
    `discovered`（discover 成功的源）与 `no_update`（窗口内无更新的源），
    不改变返回值的 3 元组契约。
    """
    docs: list[DocRecord] = []
    failed: list[str] = []
    warnings: list[str] = []
    seen_hashes: dict[str, str] = {}
    duplicates_skipped = 0
    diag = diagnostics if diagnostics is not None else {}
    discovered: list[str] = diag.setdefault("discovered", [])
    no_update: list[str] = diag.setdefault("no_update", [])
    out_of_window_docs: int = diag.setdefault("out_of_window_docs", 0)

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
        discovered.append(spec.id)

        # 本地 inbound：没有溯源 URL 的文件不入库，但不能静默跳过
        missing = getattr(adapter, "missing_source_url", None)
        if callable(missing):
            for name in missing(spec):
                warnings.append(f"{spec.id}: {name} 没有溯源 URL（加同名 .url 文件或在开头写"
                                f"「原文: https://…」），已跳过")
        if not items:
            if spec.adapter == "local_inbound":
                # 相对仓库根：日报是要入库给人看的，不能冒出本机绝对路径
                warnings.append(f"{spec.id}: 本地 inbound 目录 {_rel_to_repo(adapter.directory(spec))} "
                                f"为空，人工下载的央行原稿放进该目录即可入库（见 README「人工入库」）")
            continue

        # 正文最短长度：公开市场操作公告一类短稿可以逐源放宽
        min_body = int(spec.extra.get("min_body_length", 200))

        picked = []
        for item in items:
            fetcher.configure_rate_limit(item.url, spec.rate_limit_seconds)
            if item.published_at and not _in_window(item.published_at, window_start, window_end):
                continue
            picked.append(item)
            if len(picked) >= settings.max_docs_per_source:
                break
        # 窗口内没有更新 = 真的没有更新：如实记录，不退回抓旧稿
        if items and not picked:
            no_update.append(spec.id)

        dropped_out_of_window = 0
        for item in picked:
            try:
                # 本地 inbound 源不经过网络：适配器自己提供正文
                loader = getattr(adapter, "load", None)
                html = loader(spec, fetcher, item) if loader else fetcher.get_text(item.url)
            except FetchError as exc:
                failed.append(f"{spec.id}/{item.url}: {exc}")
                continue
            if not html:
                failed.append(f"{spec.id}/{item.url}: 正文为空（本地文件可能已被移走）")
                continue

            title = extract_title(html) or item.title
            title = SITE_SUFFIX_RE.sub("", title).strip() or item.title
            body = extract_main_text(html)
            if len(body) < min_body:
                continue
            published = item.published_at or extract_publish_time(html, body)
            # 列表页没给日期的源（如商务部 xwfb）会绕过上面的窗口过滤，
            # 详情页日期在这里必须再判一次窗口，否则 10-14 的旧稿照进当日日报。
            if not _in_window(published, window_start, window_end):
                dropped_out_of_window += 1
                diag["out_of_window_docs"] += 1
                # 剔除发生在快照落盘之前，产物里不留文档明细；告警是唯一可回溯出口
                warnings.append(
                    f"{spec.id}: 窗口外剔除 {item.url}（详情页日期 {published} ∉ "
                    f"[{window_start}, {window_end}]）")
                continue

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
            snap = snapshot_dir / f"{doc_id}{item.snapshot_ext}"
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
            ))
        # 候选全被详情页日期判到窗口外 = 该源窗口内确实没有更新，如实记入 no_update
        if picked and dropped_out_of_window == len(picked):
            no_update.append(spec.id)
    _emit_warnings(fetcher, warnings)
    return docs, failed, duplicates_skipped


def _rel_to_repo(path) -> str:
    try:
        return Path(path).resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)


def _emit_warnings(fetcher, messages: list[str]) -> None:
    """把抓取期产生的告警挂到 fetcher.warnings，由 run() 统一进日报告警区。"""
    if not messages:
        return
    sink = getattr(fetcher, "warnings", None)
    if sink is None:
        return
    sink.extend(messages)


def build_conclusions(docs, store: Store, settings: Settings, client: TongstockClient,
                      run_id: str, alias_map,
                      gate_stats: dict | None = None,
                      business_map: dict | None = None) -> tuple[list, list[dict]]:
    """抽取信号并映射到个股；返回 (signals, conclusions)。

    `gate_stats`（可选）：接收公司级相关性闸门的逐档计数，
    用于在日报里说明「多少只被放行 / 压低 / 拿不到画像」。
    """
    concept_sectors = list(client.sectors(CONCEPT_FILE).keys())
    industry_sectors = list(client.sectors(INDUSTRY_FILE).keys())
    universe = client.tickers()
    stats = gate_stats if gate_stats is not None else {}

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
            # 信号级可投资性闸门：确定性词表判定，在板块匹配与个股展开之前。
            # 非投资信号仍入库（可审计），但不映射板块、不展开个股，
            # 并清掉历史 run 遗留的展开行——否则「受益」表里还留着旧个股
            sig.investable, sig.investable_reason = assess_signal(sig, settings, business_map)
            store.upsert_signal(sig, run_id)
            all_signals.append(sig)

            if not sig.investable:
                store.clear_signal_tickers(sig.signal_id)
                conclusions.append(_skeleton_conclusion(sig, doc))
                continue

            matches = match_sectors(sig.theme_tags, concept_sectors, industry_sectors, alias_map)
            candidates = build_candidates(matches, client, settings, alias_map,
                                          business_map, stats)
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
                                           cand.sector_source,
                                           relevance=cand.relevance,
                                           gate_basis=cand.gate_basis,
                                           gate_evidence=cand.gate_evidence)
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
                    "relevance": cand.relevance,
                    "gate_basis": cand.gate_basis,
                    "gate_evidence": cand.gate_evidence,
                })

            conclusions.append({
                "signal_id": sig.signal_id,
                "doc_id": doc.doc_id,
                "doc_title": doc.title,
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
                "investable": sig.investable,
                "investable_reason": sig.investable_reason,
                "matched_sectors": [m.sector_name for m in matches],
                "tickers": tickers,
            })
    return all_signals, conclusions


def _skeleton_conclusion(sig, doc) -> dict:
    """非投资信号的结论行：完整溯源 + 判定依据，但没有板块映射与个股展开。

    仍要产出 conclusion，否则日报的「非投资信号」区无从展示，库里也查不到判定了什么。
    """
    return {
        "signal_id": sig.signal_id,
        "doc_id": doc.doc_id,
        "doc_title": doc.title,
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
        "investable": sig.investable,
        "investable_reason": sig.investable_reason,
        "matched_sectors": [],
        "tickers": [],
    }


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


def window_for(target: date, since_days: int | None, settings: Settings) -> tuple[str, str, int]:
    """统计窗口 [target-N, target]；不传 since_days 用默认的 daily_window_days-1。

    默认值刻意只回看 1 天：日报是「今天」的日报，往前 14 天是旧文章进日报的根因。
    想要更长的历史必须显式 `--since N`（回补），并在产物里能看出这是回补。
    """
    days = since_days if since_days is not None else max(settings.daily_window_days - 1, 0)
    return (target - timedelta(days=days)).isoformat(), target.isoformat(), days


def run(settings: Settings, paths, target_date: date | None = None,
        sources_file: Path | None = None, refresh_market: bool = False,
        only_sources: list[str] | None = None, fetcher: Fetcher | None = None,
        market_client=None, since_days: int | None = None) -> RunResult:
    """跑一轮日报。

    窗口口径（本 issue 第 1 点）：默认只看 `daily_window_days` 天（= 目标日与前一天），
    想回补更早的日子必须显式传 `--since N`（窗口 [target-N, target]）。
    旧的 `lookback_days=14` 每天默认往前看 14 天，是「日报里出现旧文章」的直接原因。
    """
    target = target_date or date.today()
    window_start, window_end, days = window_for(target, since_days, settings)
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
    business_map = load_theme_business()
    warnings: list[str] = []
    diagnostics: dict = {}
    owns_fetcher = fetcher is None
    if owns_fetcher:
        # E：不再硬编码 2.0，默认间隔只作兜底，真实间隔由 sources.yaml 逐源下发
        fetcher = Fetcher(timeout=settings.request_timeout, retries=settings.request_retries,
                          user_agent=settings.user_agent, min_interval=2.0, verify_tls=True,
                          robots_unavailable_interval=settings.robots_unavailable_interval)
    try:
        docs, failed, duplicates = fetch_documents(
            fetcher, specs, settings, window_start, window_end, snapshot_dir, run_id, store,
            diagnostics)
        warnings.extend(getattr(fetcher, "warnings", []))
        source_rows = _source_compliance(all_specs, docs, fetcher)
    finally:
        if owns_fetcher:
            fetcher.close()

    client = market_client or TongstockClient(settings, cache_dir=paths.cache,
                                              refresh=refresh_market)
    gate_stats: dict = {}
    try:
        signals, conclusions = build_conclusions(docs, store, settings, client, run_id,
                                                 alias_map, gate_stats, business_map)
    except TongstockError as exc:
        store.finish_run(run_id, "failed", len(docs), 0, 0, notes=f"tongstock: {exc}")
        raise

    # 事件聚类：同文档 / 同发布会的信号聚成一个政策事件，个股按代码去重
    events = cluster_events(conclusions)
    # 个股深度研读：可插拔模块，无 LLM 时整段跳过并写明原因
    dives, deepdive_notes = build_deepdives(events, client, settings)
    warnings.extend(deepdive_notes)

    ticker_count = sum(len(e["tickers"]) for e in conclusions)
    non_investable = [c for c in conclusions if not c.get("investable", True)]
    store.finish_run(run_id, "ok", len(docs), len(signals), ticker_count,
                     notes="; ".join(failed)[:500])

    result = RunResult(run_id=run_id, window_start=window_start, window_end=window_end,
                       docs=docs, signals=signals, conclusions=conclusions,
                       failed_sources=failed, warnings=warnings,
                       duplicates_skipped=duplicates,
                       events=events, deepdives=dives)
    net_source_ids = {s.id for s in specs if s.adapter != "local_inbound"}
    # 源成功 = discover 成功（没抛异常），与「窗口内有没有更新」无关；
    # 后者单列 sources_no_update，不能再算成源挂了，也不能靠抓旧稿把数字凑出来
    discovered = set(diagnostics.get("discovered", []))
    ok_net = discovered & net_source_ids
    no_update = [s for s in diagnostics.get("no_update", []) if s in net_source_ids]
    result.stats = {
        "docs": len(docs),
        "signals": len(signals),
        "with_tickers": sum(1 for c in conclusions if c["tickers"]),
        "ticker_rows": ticker_count,
        # 信号级可投资性闸门：非投资信号 = 泛经济样板表述，只留审计行
        "investable_gate": settings.investable_gate,
        "non_investable_signals": len(non_investable),
        "non_investable_signal_ids": [c["signal_id"] for c in non_investable],
        "events": len(events),
        "event_conclusions": sum(len(e.conclusions) for e in events),
        "window_start": window_start,
        "window_end": window_end,
        "window_days": days + 1,
        "since_days": days,
        "sources_total": len(net_source_ids),
        "sources_ok": len(ok_net),
        "sources_failed": len(net_source_ids) - len(ok_net),
        "sources_no_update": no_update,
        "local_inbound_sources": len(specs) - len(net_source_ids),
        "fetch_failures": len(failed),
        "duplicates_skipped": duplicates,
        "out_of_window_docs": diagnostics.get("out_of_window_docs", 0),
        "gate": gate_stats,
        "deepdive_count": len(dives),
        # 央行相关内容条数与来源：WAN-10 的验收要能指认
        "pbc_related_docs": sum(1 for d in docs if is_pbc_related(d.title, d.body)),
        "pbc_related_sources": sorted({d.source_id for d in docs if is_pbc_related(d.title, d.body)}),
        "sources_detail": source_rows,
        "min_strength_for_report": settings.min_strength_for_report,
        "tongstock_calls": client.calls,
        "llm_enabled": llm.llm_available(),
        "disclaimer": DISCLAIMER,
    }
    store.close()
    return result
