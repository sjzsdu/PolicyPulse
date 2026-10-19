"""日报渲染：Markdown（主交付）+ JSON + 单页 HTML。"""

from __future__ import annotations

import html
import json
from pathlib import Path

from policypulse.config import DISCLAIMER
from policypulse.pipeline import RunResult


def _esc(text) -> str:
    return html.escape(str(text or ""))


def render_markdown(result: RunResult) -> str:
    s = result.stats
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
    lines.append(f"| 信息源（成功/总数） | {s['sources_total'] - s['sources_failed']} / {s['sources_total']} |")
    lines.append(f"| LLM 抽取 | {'开启' if s['llm_enabled'] else '关闭（规则抽取）'} |")
    lines.append(f"| tongstock 调用（不含缓存命中） | {s['tongstock_calls']} |")
    lines.append("")

    if s["sources_failed"]:
        lines.append("### 抓取告警")
        lines.append("")
        for item in result.failed_sources[:10]:
            lines.append(f"- {item}")
        lines.append("")

    bullish = [c for c in result.conclusions if c["direction"] == "利好" and c["tickers"]]
    bullish.sort(key=lambda c: (-c["strength"], -c["confidence"]))

    lines.append("## 利好信号与候选个股")
    lines.append("")
    if not bullish:
        lines.append("本轮没有命中「利好且有板块映射」的信号。")
        lines.append("")

    for idx, c in enumerate(bullish, start=1):
        lines.append(f"### {idx}. {c['summary']}")
        lines.append("")
        lines.append(f"- 信号ID：`{c['signal_id']}`")
        lines.append(f"- 力度/周期/性质：strength **{c['strength']}** / {c['horizon']} / {c['novelty']}")
        lines.append(f"- 主题：{', '.join(c['theme_tags']) or '—'}")
        lines.append(f"- 工具：{', '.join(c['instruments']) or '—'}")
        lines.append(f"- 置信度：{c['confidence']}（引用校验：{'通过' if c['quote_verified'] else '未通过'}）")
        lines.append(f"- 来源：{c['org']} · {c['meeting_type']} · {c['published_at'] or '日期未知'}")
        lines.append(f"- 原文：[{c['source_url']}]({c['source_url']})")
        lines.append(f"- 快照：`{c['raw_snapshot_path']}`")
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

    others = [c for c in result.conclusions if c not in bullish]
    if others:
        lines.append("## 其余信号（中性/利空/无映射）")
        lines.append("")
        lines.append("| 信号ID | 方向 | 力度 | 摘要 | 来源 |")
        lines.append("|---|---|---|---|---|")
        for c in others:
            lines.append(f"| `{c['signal_id']}` | {c['direction']} | {c['strength']} | "
                         f"{c['summary'][:40]} | [{c['org']}]({c['source_url']}) |")
        lines.append("")

    lines.append("## 运行元信息")
    lines.append("")
    lines.append(f"- 快照目录：`{Path(result.docs[0].snapshot_path).parent if result.docs else '—'}`")
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
        "conclusions": result.conclusions,
        "disclaimer": DISCLAIMER,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_html(result: RunResult) -> str:
    s = result.stats
    rows: list[str] = []
    for c in result.conclusions:
        if c["direction"] != "利好" or not c["tickers"]:
            continue
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
            f"strength {c['strength']} · 置信度 {c['confidence']}</p>"
            f"<blockquote>{_esc(c['quote'])}</blockquote>"
            f"<table><thead><tr><th>#</th><th>代码</th><th>名称</th><th>板块</th><th>市值(亿)</th>"
            f"<th>score</th><th>理由</th><th>溯源</th></tr></thead><tbody>{trows}</tbody></table></section>"
        )

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
</style></head><body>
<h1>PolicyPulse 日报 {_esc(result.window_end)}</h1>
<p class="disc">{_esc(DISCLAIMER)}</p>
<p>窗口 {_esc(result.window_start)} ~ {_esc(result.window_end)} · 文档 {s['docs']} · 信号 {s['signals']} · 个股结论 {s['ticker_rows']}</p>
{''.join(rows) or '<p>本轮没有命中「利好且有板块映射」的信号。</p>'}
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
