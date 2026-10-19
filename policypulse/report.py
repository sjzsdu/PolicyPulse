"""日报渲染：Markdown（主交付）+ JSON + 单页 HTML。"""

from __future__ import annotations

import html
import json
from pathlib import Path

from policypulse.config import DISCLAIMER, MAPPING_CAVEAT, REPO_ROOT
from policypulse.pipeline import RunResult


def _esc(text) -> str:
    return html.escape(str(text or ""))


def relative_snapshot(path: str) -> str:
    """Markdown 里写相对仓库根的路径；JSON/DB 仍保留绝对路径。"""
    try:
        return Path(path).resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        return path


def _compliance_lines(result: RunResult) -> list[str]:
    """逐源合规台账：robots 判定 + 本轮文档数。停用/本地源明确写「不发起请求」。"""
    rows = result.stats.get("sources_detail") or []
    if not rows:
        return []
    lines = ["## 信息源与合规", "",
             "| 源ID | 机构 | 状态 | robots 判定 | 判定依据 | 本轮文档 |",
             "|---|---|---|---|---|---|"]
    for row in rows:
        flag = "启用" if row["enabled"] else "停用"
        reason = row["robots_reason"]
        if len(reason) > 60:
            reason = reason[:59] + "…"
        lines.append(f"| `{row['id']}` | {row['org']} | {flag} | {row['robots']} | "
                     f"{reason} | {row['docs']} |")
    lines.append("")
    return lines


def render_markdown(result: RunResult) -> str:
    s = result.stats
    min_strength = s.get("min_strength_for_report", 3)
    lines: list[str] = []
    lines.append(f"# PolicyPulse 日报 {result.window_end}")
    lines.append("")
    lines.append(f"> {DISCLAIMER}")
    lines.append("")
    lines.append("## 今日概览")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 统计窗口 | {result.window_start} ~ {result.window_end} |")
    lines.append(f"| run_id | `{result.run_id}` |")
    lines.append(f"| 抓取文档 | {s['docs']} |")
    lines.append(f"| 抽取信号 | {s['signals']} |")
    lines.append(f"| 有映射个股的信号 | {s['with_tickers']} |")
    lines.append(f"| 个股结论行数 | {s['ticker_rows']} |")
    lines.append(f"| 信息源（成功/总数） | {s.get('sources_ok', s['sources_total'] - s['sources_failed'])}"
                 f" / {s['sources_total']} |")
    lines.append(f"| 抓取失败条目 | {s.get('fetch_failures', 0)} |")
    lines.append(f"| 跨 run 去重跳过 | {s.get('duplicates_skipped', 0)} |")
    lines.append(f"| LLM 抽取 | {'开启' if s['llm_enabled'] else '关闭（规则抽取）'} |")
    lines.append(f"| tongstock 调用（不含缓存命中） | {s['tongstock_calls']} |")
    pbc_docs = s.get("pbc_related_docs", 0)
    pbc_src = s.get("pbc_related_sources") or []
    lines.append(f"| 央行（中国人民银行）相关文档 | {pbc_docs}"
                 f"{'（来源：' + '、'.join(pbc_src) + '）' if pbc_src else ''} |")
    lines.append("")
    lines.append(f"> 口径说明：{MAPPING_CAVEAT}")
    lines.append("")

    warned: list[str] = list(result.failed_sources) + list(result.warnings)
    if warned:
        lines.append("### 抓取告警")
        lines.append("")
        for item in warned[:15]:
            lines.append(f"- {item}")
        lines.append("")

    if result.duplicates_skipped:
        lines.append(f"- 另有 {result.duplicates_skipped} 篇文档正文指纹已在库（跨 run 去重），本次未重复抽取。")
        lines.append("")
    if s.get("out_of_window_docs"):
        lines.append(f"- 有 {s['out_of_window_docs']} 篇来自「栏目窗口内无更新」的兜底抓取，日期可能早于统计窗口，"
                     "对应条目在下方标注 `窗口外兜底`。")
        lines.append("")

    bullish_all = [c for c in result.conclusions if c["direction"] == "利好" and c["tickers"]]
    bullish = [c for c in bullish_all if c["strength"] >= min_strength]
    filtered_out = len(bullish_all) - len(bullish)
    bullish.sort(key=lambda c: (-c["strength"], -c["confidence"]))

    lines.append("## 利好信号与候选个股")
    lines.append("")
    if filtered_out:
        lines.append(f"另有 {filtered_out} 条利好结论力度 < {min_strength}，未列入本表，详见同名 JSON 与数据库。")
        lines.append("")
    if not bullish:
        lines.append(f"本轮没有命中「利好、有板块映射且力度 ≥ {min_strength}」的信号。")
        lines.append("")

    for idx, c in enumerate(bullish, start=1):
        lines.append(f"### {idx}. {c['summary']}")
        lines.append("")
        lines.append(f"- 信号ID：`{c['signal_id']}`")
        lines.append(f"- 力度/周期/性质：strength **{c['strength']}** / {c['horizon']} / {c['novelty']}")
        lines.append(f"- 主题：{', '.join(c['theme_tags']) or '—'}")
        lines.append(f"- 工具：{', '.join(c['instruments']) or '—'}")
        lines.append(f"- 置信度：{c['confidence']}（引用校验：{'通过' if c['quote_verified'] else '未通过'}）")
        fallback = " · `窗口外兜底`" if c.get("out_of_window_fallback") else ""
        lines.append(f"- 来源：{c['org']} · {c['meeting_type']} · {c['published_at'] or '日期未知'}{fallback}")
        lines.append(f"- 原文：[{c['source_url']}]({c['source_url']})")
        lines.append(f"- 快照：`{relative_snapshot(c['raw_snapshot_path'])}`")
        lines.append("")
        lines.append(f"> {c['quote']}")
        lines.append("")
        if c["tickers"]:
            lines.append("| # | 代码 | 名称 | 板块 | 市值(亿) | score | 受益理由 | 映射路径 |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for t in c["tickers"]:
                lines.append(
                    f"| {t['rank']} | {t['ticker_code']} | {t['ticker_name']} | "
                    f"{t['sector_name']}({t['sector_source']}) | {t['market_cap']:.1f} | {t['score']} | "
                    f"{t['why']} | {t['mapping_path']} |"
                )
        else:
            lines.append("_未映射到板块（宁缺勿错），该信号仍保留溯源。_")
        lines.append("")

    bullish_ids = {c["signal_id"] for c in bullish}
    others = [c for c in result.conclusions if c["signal_id"] not in bullish_ids]
    if others:
        lines.append("## 其余信号（中性/利空/力度不足/无映射）")
        lines.append("")
        lines.append("| 信号ID | 方向 | 力度 | 摘要 | 来源 |")
        lines.append("|---|---|---|---|---|")
        for c in others:
            lines.append(f"| `{c['signal_id']}` | {c['direction']} | {c['strength']} | "
                         f"{c['summary'][:40]} | [{c['org']}]({c['source_url']}) |")
        lines.append("")

    lines.extend(_compliance_lines(result))

    lines.append("## 运行元信息")
    lines.append("")
    snap_dir = Path(result.docs[0].snapshot_path).parent if result.docs else None
    lines.append(f"- 快照目录：`{relative_snapshot(str(snap_dir)) if snap_dir else '—'}`")
    lines.append(f"- 数据库：`data/policypulse.db`（run_id `{result.run_id}`）")
    lines.append(f"- 复核命令：`policypulse verify --run {result.run_id}`")
    lines.append(f"- 单信号溯源：`policypulse show {result.conclusions[0]['signal_id'] if result.conclusions else '<signal_id>'}`")
    lines.append("")
    lines.append(f"> {DISCLAIMER}")
    return "\n".join(lines)


def render_json(result: RunResult) -> str:
    payload = {
        "run_id": result.run_id,
        "window": {"start": result.window_start, "end": result.window_end},
        "stats": result.stats,
        "failed_sources": result.failed_sources,
        "warnings": result.warnings,
        "conclusions": result.conclusions,
        "disclaimer": DISCLAIMER,
        "mapping_caveat": MAPPING_CAVEAT,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_html(result: RunResult) -> str:
    s = result.stats
    min_strength = s.get("min_strength_for_report", 3)
    rows: list[str] = []
    for c in result.conclusions:
        if c["direction"] != "利好" or not c["tickers"] or c["strength"] < min_strength:
            continue
        fallback = " · 窗口外兜底" if c.get("out_of_window_fallback") else ""
        trows = "".join(
            f"<tr><td>{t['rank']}</td><td>{_esc(t['ticker_code'])}</td><td>{_esc(t['ticker_name'])}</td>"
            f"<td>{_esc(t['sector_name'])}</td><td>{t['market_cap']:.1f}</td><td>{t['score']}</td>"
            f"<td>{_esc(t['why'])}</td>"
            f"<td><a href='{_esc(c['source_url'])}' target='_blank'>原文</a></td></tr>"
            for t in c["tickers"]
        )
        rows.append(
            f"<section><h3>{_esc(c['summary'])}</h3>"
            f"<p class='meta'>{_esc(c['org'])} · {_esc(c['meeting_type'])} · {_esc(c['published_at'])} · "
            f"strength {c['strength']} · 置信度 {c['confidence']}{fallback}</p>"
            f"<blockquote>{_esc(c['quote'])}</blockquote>"
            f"<table><thead><tr><th>#</th><th>代码</th><th>名称</th><th>板块</th><th>市值(亿)</th>"
            f"<th>score</th><th>理由</th><th>溯源</th></tr></thead><tbody>{trows}</tbody></table></section>"
        )

    compliance = ""
    rows_detail = s.get("sources_detail") or []
    if rows_detail:
        cells = "".join(
            f"<tr><td>{_esc(r['id'])}</td><td>{_esc(r['org'])}</td>"
            f"<td>{'启用' if r['enabled'] else '停用'}</td><td>{_esc(r['robots'])}</td>"
            f"<td>{_esc(r['robots_reason'])}</td><td>{r['docs']}</td></tr>"
            for r in rows_detail)
        compliance = (
            "<h2>信息源与合规</h2><table><thead><tr><th>源ID</th><th>机构</th><th>状态</th>"
            "<th>robots 判定</th><th>判定依据</th><th>本轮文档</th></tr></thead>"
            f"<tbody>{cells}</tbody></table>")

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>PolicyPulse 日报 {_esc(result.window_end)}</title>
<style>
body{{font-family:-apple-system,"PingFang SC",Helvetica,Arial,sans-serif;margin:32px auto;max-width:1080px;color:#1b1b1b;line-height:1.6}}
h1{{font-size:24px}} h3{{font-size:18px;margin-bottom:4px}}
.meta{{color:#666;font-size:13px;margin-top:0}}
blockquote{{border-left:3px solid #c9a227;margin:8px 0;padding:8px 12px;background:#fbf8ef;color:#333}}
table{{border-collapse:collapse;width:100%;margin:8px 0 20px;font-size:14px}}
th,td{{border:1px solid #e2e2e2;padding:6px 8px;text-align:left}}
th{{background:#f5f5f5}}
.disc{{background:#fff6f6;border:1px solid #f0c9c9;padding:10px 14px;color:#8a2b2b;font-size:13px}}
.caveat{{background:#fbf8ef;border:1px solid #e6d69a;padding:10px 14px;color:#6b5716;font-size:13px}}
</style></head><body>
<h1>PolicyPulse 日报 {_esc(result.window_end)}</h1>
<p class="disc">{_esc(DISCLAIMER)}</p>
<p class="caveat">口径说明：{_esc(MAPPING_CAVEAT)}</p>
<p>窗口 {_esc(result.window_start)} ~ {_esc(result.window_end)} · 文档 {s['docs']} · 信号 {s['signals']} · 个股结论 {s['ticker_rows']} · 央行相关 {s.get('pbc_related_docs', 0)}</p>
{''.join(rows) or '<p>本轮没有命中「利好且有板块映射」的信号。</p>'}
{compliance}
<p class="disc">{_esc(DISCLAIMER)}</p>
</body></html>"""


def write_reports(result: RunResult, reports_dir: Path) -> dict[str, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = result.window_end
    md = reports_dir / f"{stem}.md"
    js = reports_dir / f"{stem}.json"
    ht = reports_dir / f"{stem}.html"
    md.write_text(render_markdown(result), encoding="utf-8")
    js.write_text(render_json(result), encoding="utf-8")
    ht.write_text(render_html(result), encoding="utf-8")
    return {"markdown": md, "json": js, "html": ht}
