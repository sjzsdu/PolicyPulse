"""policypulse 命令行入口。

    policypulse run                      # 一条命令跑出真实日报
    policypulse show <signal_id>         # 打印完整溯源链
    policypulse verify --run <run_id>    # 重抓原文核对
    policypulse sources                  # 列出信息源
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from policypulse import llm
from policypulse.config import default_paths, load_settings, load_sources
from policypulse.pipeline import run as run_pipeline
from policypulse.report import write_reports
from policypulse.store import Store
from policypulse.verify import OK_STATUSES, render_verify, verify_documents


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="policypulse", description="政府会议 → 政策信号 → 行业 → 个股")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="抓取→抽取→映射→打分→出日报")
    p_run.add_argument("--date", help="目标日期 YYYY-MM-DD，默认今天")
    p_run.add_argument("--since", nargs="?", const=0, default=None, type=int, metavar="N",
                       help="显式补跑：收最近 N 天（--since 1 即只收当日；省略 N 则用配置 "
                            "lookback_days）；不带此参数时日报固定只收 [昨日, 今日]")
    p_run.add_argument("--sources", nargs="*", default=None, help="只跑指定 source id")
    p_run.add_argument("--sources-file", type=Path, default=None)
    p_run.add_argument("--refresh-market", action="store_true", help="强制刷新 tongstock 板块/个股缓存")
    p_run.add_argument("--no-dedupe", action="store_true",
                       help="关闭跨 run 去重，重跑当天也重新抽取")
    p_run.add_argument("--no-report", action="store_true", help="只落库不出报告")

    p_show = sub.add_parser("show", help="打印一个信号的完整溯源链")
    p_show.add_argument("signal_id")

    p_verify = sub.add_parser("verify", help="重抓原文核对结论")
    p_verify.add_argument("--run", dest="run_id", help="复核该 run 的全部文档")
    p_verify.add_argument("--doc", action="append", default=[], help="复核指定 doc_id（可重复）")

    sub.add_parser("sources", help="列出信息源清单")
    sub.add_parser("selftest", help="检查 tongstock / 网络 / LLM 可用性")
    return parser


def cmd_run(args) -> int:
    settings = load_settings()
    if args.no_dedupe:
        settings.cross_run_dedupe = False
    paths = default_paths()

    target = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    # lookback_days 只在显式 --since 补跑时生效；默认日报恒为 [target-1, target]
    since_days = args.since
    if since_days == 0:
        since_days = settings.lookback_days
    if since_days:
        print(f"[policypulse] 补跑模式 --since {since_days}d，窗口拉长到最近 {since_days} 天", flush=True)
    else:
        print(f"[policypulse] 默认窗口 = [{target - timedelta(days=1)}, {target}]"
              f"（只收窗口内发布，不再回退抓旧文）", flush=True)

    result = run_pipeline(settings, paths, target_date=target, sources_file=args.sources_file,
                          refresh_market=args.refresh_market, only_sources=args.sources,
                          since_days=since_days)

    print(f"[policypulse] docs={result.stats['docs']} signals={result.stats['signals']} "
          f"ticker_rows={result.stats['ticker_rows']} "
          f"sources_ok={result.stats['sources_total'] - result.stats['sources_failed']}/"
          f"{result.stats['sources_total']} llm={'on' if result.stats['llm_enabled'] else 'off'}"
          f" dedupe_skipped={result.duplicates_skipped}", flush=True)
    for failure in result.failed_sources[:10]:
        print(f"[policypulse][warn] {failure}", flush=True)
    for warning in result.warnings[:10]:
        print(f"[policypulse][robots] {warning}", flush=True)

    if not args.no_report:
        written = write_reports(result, paths.reports)
        print(f"[policypulse] report -> {written['markdown']}", flush=True)
        print(f"[policypulse] json   -> {written['json']}", flush=True)
        print(f"[policypulse] html   -> {written['html']}", flush=True)
    if not result.conclusions:
        if not result.docs and not result.failed_sources:
            print("[policypulse] 今日无新增政策/事件", flush=True)
            return 0
        print("[policypulse] 本轮没有产出结论（可能是源不可达）", flush=True)
        return 1
    return 0


def cmd_show(args) -> int:
    paths = default_paths()
    store = Store(paths.db)
    sig = store.signal(args.signal_id)
    if sig is None:
        print(f"未找到信号 {args.signal_id}")
        return 1
    doc = store.document(sig["doc_id"])
    tickers = store.tickers_for_signal(args.signal_id)

    print(f"信号 {sig['signal_id']}  ({sig['direction']} / strength {sig['strength']} / "
          f"confidence {sig['confidence']})")
    print(f"摘要: {sig['summary']}")
    print(f"引用: {sig['quote']}  [子串校验: {'通过' if sig['quote_verified'] else '未通过'}]")
    print(f"主题: {sig['theme_tags']}  工具: {sig['instruments']}  周期: {sig['horizon']}  性质: {sig['novelty']}")
    if doc is not None:
        print(f"来源: {doc['org']} · {doc['meeting_type']} · {doc['published_at']}")
        print(f"URL : {doc['url']}")
        print(f"快照: {doc['raw_snapshot_path']}")
        print(f"hash: {doc['content_hash']}  fetched_at={doc['fetched_at']}")
    print(f"模型: {sig['model']} / prompt {sig['prompt_version']}")
    if tickers:
        print("个股:")
        for t in tickers:
            print(f"  {t['rank']}. {t['code']} {t['name']} · {t['sector_name']}({t['sector_source']}) · "
                  f"市值 {t['market_cap_yi']:.1f}亿 · score {t['score']}")
            print(f"     映射: {t['mapping_path']} (conf {t['mapping_confidence']})")
    store.close()
    return 0


def cmd_verify(args) -> int:
    paths = default_paths()
    store = Store(paths.db)
    results = verify_documents(store, doc_ids=args.doc or None, run_id=args.run_id)
    print(render_verify(results))
    changed = sum(1 for r in results if r.status not in OK_STATUSES)
    store.close()
    return 0 if changed == 0 else 2


def cmd_sources(args) -> int:
    for spec in load_sources():
        flag = "on " if spec.enabled else "off"
        print(f"{flag} {spec.id:<14} {spec.tier:<3} {spec.org:<12} {spec.name:<14} {spec.url}")
    return 0


def cmd_selftest(args) -> int:
    from policypulse.http import Fetcher
    from policypulse.tongstock import TongstockClient, TongstockError
    from policypulse.config import Settings

    settings = load_settings()
    paths = default_paths()
    ok = True

    print(f"python          : {sys.version.split()[0]}")
    print(f"llm             : {'available' if llm.llm_available() else 'not configured (rule extraction)'}")

    client = TongstockClient(settings, cache_dir=paths.cache)
    try:
        out = client.run(["block", "files"])
        print("tongstock block : ok")
    except TongstockError as exc:
        ok = False
        print(f"tongstock block : FAIL {exc}")
    try:
        tickers = client.tickers()
        concept = client.sectors("block_gn.dat")
        print(f"tongstock data  : ok ({len(tickers)} tickers, {len(concept)} concept sectors)")
    except TongstockError as exc:
        ok = False
        print(f"tongstock data  : FAIL {exc}")

    try:
        with Fetcher(min_interval=0) as f:
            text = f.get_text("https://www.gov.cn/")
            print(f"network gov.cn  : ok ({len(text)} bytes)")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"network gov.cn  : FAIL {exc}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "run": cmd_run,
        "show": cmd_show,
        "verify": cmd_verify,
        "sources": cmd_sources,
        "selftest": cmd_selftest,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
