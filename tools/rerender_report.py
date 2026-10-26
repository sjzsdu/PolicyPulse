"""按库内数据重渲染某次 run 的日报，复核「信号级可投资性闸门」与「报告分区」。

    python tools/rerender_report.py --run 20261021-162701-ea53b8
    python tools/rerender_report.py --run 20261020-221515-312028 --report reports/2026-10-20.md

只重算两件事：逐条信号的可投资性判定（当前词表）+ 报告分区（md / html / json 同源对账）。
结论行直接取库内 `signal_ticker` 展开，不重新抓取、不重新打分；抓取期告警
（窗口外剔除、跨 run 去重）不回填，因此产物只用于复核闸门与分区，
不替代当时的日报文件。

退出码：0 = 三端分区一致且报告引用的信号判定自洽；1 = 不一致。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from policypulse.clustering import cluster_events  # noqa: E402
from policypulse.config import default_paths, load_settings  # noqa: E402
from policypulse.investability import assess_text  # noqa: E402
from policypulse.pipeline import RunResult  # noqa: E402
from policypulse.report import (  # noqa: E402
    LEGACY_SECTION_CONCEPT_PENDING,
    MAIN_SECTIONS,
    SECTION_CONCEPT_PENDING,
    _qualifying,
    render_html,
    render_json,
    render_markdown,
)
from policypulse.store import Store  # noqa: E402

GENERIC_SECTION = "非投资信号"


def _loads(raw, default):
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def build_conclusions_from_store(store: Store, run_id: str, settings) -> tuple[list, list[dict]]:
    """库内信号 → 结论行（闸门按当前词表实时判定，历史行不回填也照样复核）。"""
    signals, conclusions = [], []
    for row in store.signals_for_run(run_id):
        doc = store.document(row["doc_id"])
        ok, reason = assess_text(f"{row['summary']}{row['quote']}", settings,
                                 theme_tags=_loads(row["theme_tags"], []))
        signals.append({"signal_id": row["signal_id"], "investable": ok, "reason": reason})

        rows = store.tickers_for_signal(row["signal_id"]) if ok else []
        tickers = [{
            "ticker_code": r["code"], "ticker_name": r["name"] or "",
            "exchange": r["exchange"] or "", "board": r["board"] or "",
            "market_cap": r["market_cap_yi"] or 0.0, "sector_name": r["sector_name"],
            "sector_source": r["sector_source"], "why": r["why"], "score": r["score"],
            "rank": r["rank"], "mapping_path": r["mapping_path"],
            "mapping_confidence": r["mapping_confidence"],
            "relevance": r["relevance"], "gate_basis": r["gate_basis"] or "",
            "gate_evidence": r["gate_evidence"] or "",
        } for r in rows]
        conclusions.append({
            "signal_id": row["signal_id"], "doc_id": row["doc_id"],
            "doc_title": (doc["title"] if doc else "") or row["summary"],
            "summary": row["summary"], "direction": row["direction"],
            "strength": row["strength"], "horizon": row["horizon"], "novelty": row["novelty"],
            "instruments": _loads(row["instruments"], []),
            "theme_tags": _loads(row["theme_tags"], []),
            "confidence": row["confidence"], "quote": row["quote"],
            "quote_verified": bool(row["quote_verified"]),
            "source_url": (doc["url"] if doc else ""), "publisher": (doc["org"] if doc else ""),
            "org": (doc["org"] if doc else ""), "meeting_type": (doc["meeting_type"] if doc else ""),
            "published_at": (doc["published_at"] if doc else ""),
            "fetched_at": (doc["fetched_at"] if doc else ""),
            "content_hash": (doc["content_hash"] if doc else ""),
            "raw_hash": (doc["raw_hash"] if doc else ""),
            "raw_snapshot_path": (doc["raw_snapshot_path"] if doc else ""),
            "model": row["model"], "prompt_version": row["prompt_version"],
            "investable": ok, "investable_reason": reason,
            "matched_sectors": sorted({t["sector_name"] for t in tickers}),
            "tickers": tickers,
        })
    return signals, conclusions


def build_result(store: Store, run_id: str, settings):
    run = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise SystemExit(f"未找到 run {run_id}")
    docs = store.documents_for_run(run_id)
    signals, conclusions = build_conclusions_from_store(store, run_id, settings)
    # 事件级选股上限与线上链路同一口径：重渲染不得把 5 只上限放大回去
    events = cluster_events(conclusions, settings.max_picks)
    src_total = store.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    src_ok = src_total
    non_inv = [c for c in conclusions if not c["investable"]]
    result = RunResult(run_id=run_id, window_start=run["window_start"] or "",
                       window_end=run["window_end"] or "", conclusions=conclusions,
                       events=events)
    result.stats = {
        "docs": len(docs), "signals": len(signals),
        "with_tickers": sum(1 for c in conclusions if c["tickers"]),
        "ticker_rows": sum(len(e.tickers) for e in events), "events": len(events),
        "sources_total": src_total or 0, "sources_ok": src_ok or 0,
        "sources_failed": 0, "fetch_failures": 0, "tongstock_calls": 0,
        "llm_enabled": False, "duplicates_skipped": 0, "out_of_window_docs": 0,
        "sources_no_update": [], "gate": {}, "deepdive_count": 0,
        "min_strength_for_report": settings.min_strength_for_report,
        "max_picks": settings.max_picks,
        "investable_gate": settings.investable_gate,
        "non_investable_signals": len(non_inv),
        "non_investable_signal_ids": [c["signal_id"] for c in non_inv],
        "pbc_related_docs": 0,
    }
    return result, signals, events


def _md_sections(md: str) -> dict[str, str]:
    out, current = {}, ""
    for line in md.splitlines():
        if line.startswith("- 事件ID：`"):
            current = line.split("`")[1]
        elif line.startswith("- 分区：") and current:
            out[current] = line[len("- 分区："):].strip()
    return out


def report_signal_ids(path: Path) -> list[str]:
    """取已入库日报里引用过的信号ID（含「受益」表与「其余信号」表）。"""
    text = path.read_text(encoding="utf-8")
    return list(dict.fromkeys(re.findall(r"`((?:doc|evt)-[\w-]+#\d+)`", text)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按库内数据重渲染 run，复核闸门与分区")
    parser.add_argument("--run", required=True)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None, help="把 md/json/html 写到该目录")
    parser.add_argument("--report", type=Path, default=None,
                        help="对照一份已入库日报：其中引用的信号必须仍判可投资")
    args = parser.parse_args(argv)

    settings = load_settings()
    paths = default_paths()
    store = Store(args.db or paths.db)
    result, signals, events = build_result(store, args.run, settings)
    store.close()

    blocked = [s for s in signals if not s["investable"]]
    print(f"[policypulse] run {args.run} · docs={result.stats['docs']} "
          f"signals={len(signals)} ticker_rows={result.stats['ticker_rows']} "
          f"事件={len(events)}")
    print(f"[gate] 闸门={'开' if settings.investable_gate else '关'} · "
          f"非投资信号 {len(blocked)} / {len(signals)} 条")
    for s in blocked:
        print(f"  - {s['signal_id']} 否 | {s['reason']}")

    md = render_markdown(result)
    js = render_json(result)
    payload = json.loads(js)
    html = render_html(result)

    print("[sections] 受益个股: " + (", ".join(payload["sections"]["受益个股"]) or "（空）"))
    print("[sections] 仅概念关联: " + (", ".join(payload["sections"]["仅概念关联"]) or "（空）"))
    # R1：pending 单列键；老键做兜底，老报告/老解析方读旧键也拿得到
    pending_ids_json = (payload["sections"].get(SECTION_CONCEPT_PENDING)
                        or payload["sections"].get(LEGACY_SECTION_CONCEPT_PENDING) or [])
    print(f"[sections] {SECTION_CONCEPT_PENDING}: " + (", ".join(pending_ids_json) or "（空）"))
    print("[sections] 非投资信号: " + (", ".join(payload["sections"]["非投资信号"]) or "（空）"))

    json_sections = {e["event_id"]: e["section"] for e in payload["events"]}
    # md / html 只渲染「有达门槛个股行」的事件；json 的 events 是全量清单，
    # 所以对账口径 = md/html 应渲染的事件集，json 对这些事件的 section 必须同值
    min_strength = settings.min_strength_for_report
    expected = {ev.event_id: json_sections[ev.event_id] for ev in events
                if json_sections[ev.event_id] != GENERIC_SECTION
                and _qualifying(ev, min_strength)}
    md_sections = _md_sections(md)
    html_sections = {k: v.strip() for k, v in
                     re.findall(r"事件ID <code>(evt-[\w]+)</code> · 分区：([^·<]+)", html)}
    json_ok = all(json_sections.get(k) == v for k, v in expected.items())
    consistent = md_sections == expected and html_sections == expected and json_ok
    extra = len(json_sections) - len(expected)
    print(f"[three-end] md / json / html 分区一致: {'OK' if consistent else 'FAIL'}"
          f"（渲染 {len(expected)} 个事件；json 另列 {extra} 个无达门槛个股行的事件）")
    if not consistent:
        print(f"  md={md_sections}\n  html={html_sections}\n  期望={expected}\n  json={json_sections}")

    has_main = any(v in MAIN_SECTIONS for v in expected.values())
    stated = ("今日无实质投资信号" in md) and ("今日无实质投资信号" in html)
    if has_main:
        empty_ok = not ("今日无实质投资信号" in md or "今日无实质投资信号" in html)
        label = "受益区有事件时不误报空日"
    else:
        empty_ok = stated
        label = "受益区为空时三端写明「今日无实质投资信号」"
    print(f"[empty-day] {label}: {'OK' if empty_ok else 'FAIL'}")

    # R1：无票（pending）事件必须单列「待判定」标题，且不出现在受益区标题下
    pending_expected = [k for k, v in expected.items() if v == SECTION_CONCEPT_PENDING]
    md_main = (re.split(r"\n## ", md.split("## 受益个股", 1)[1], maxsplit=1)[0]
               if "## 受益个股" in md else "")
    html_main = (html.split("<h2>受益个股</h2>", 1)[1].split("<h2>", 1)[0]
                 if "<h2>受益个股</h2>" in html else "")
    heading = "待判定：无实质传导证据"
    pending_ok = ((heading in md) == (heading in html) == bool(pending_expected)
                  and all(i not in md_main and i not in html_main for i in pending_expected))
    print(f"[pending] 无票事件单列「待判定」、不挂受益区: {'OK' if pending_ok else 'FAIL'}")
    if not pending_ok:
        print(f"  待判定={pending_expected}\n  md_main 含待判定事件="
              f"{[i for i in pending_expected if i in md_main]}")

    ni_ids = payload["sections"]["非投资信号"]
    ni_ok = all(i in md and i in html for i in ni_ids)
    print(f"[non-investable] 非投资信号 {len(ni_ids)} 条在 md/html 逐条留痕: "
          f"{'OK' if ni_ok else 'FAIL'}")

    exit_code = 0 if (consistent and empty_ok and ni_ok and pending_ok) else 1

    if args.report:
        ids = report_signal_ids(args.report)
        by_id = {s["signal_id"]: s for s in signals}
        missing = [i for i in ids if i not in by_id]
        killed = [i for i in ids if by_id.get(i) and not by_id[i]["investable"]]
        print(f"[report-check] {args.report} 引用信号 {len(ids)} 个 · "
              f"不在本次 run {len(missing)} 个 · 被判非投资 {len(killed)} 个")
        for i in ids:
            item = by_id.get(i)
            print(f"  - {i} {'是' if item and item['investable'] else '否'}"
                  f"{'（不在本次 run）' if item is None else ''}")
        if killed or missing:
            print(f"[report-check] FAIL killed={killed} missing={missing}")
            exit_code = 1
        else:
            print("[report-check] OK：日报引用的信号无一被闸门误杀")

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        stem = result.window_end or args.run
        for name, text in (("md", md), ("json", js), ("html", html)):
            (args.out / f"{stem}.{name}").write_text(text, encoding="utf-8")
        print(f"[out] {args.out}/{stem}.{{md,json,html}}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
