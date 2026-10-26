"""日报渲染：Markdown（主交付）+ JSON + 单页 HTML。

正文按「政策事件」组织（同文档/同发布会的信号聚成一条事件，个股按代码去重一张表），
不再逐条信号重复贴一模一样的个股表。
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from policypulse.clustering import _ensure_events, merge_tickers
from policypulse.config import (
    DISCLAIMER,
    EMPTY_DAY_NOTICE,
    GATE_CAVEAT,
    INVESTABLE_CAVEAT,
    MAPPING_CAVEAT,
    REPO_ROOT,
    Settings,
)
from policypulse.deepdive import substantive_verdict
from policypulse.pipeline import RunResult

# 报告分区：受益个股 > 仅概念关联 > 待判定 > 非投资信号。
# 受益区只收深读明判「有实质影响」的事件；无可判定票的事件（未配 LLM /
# 模型只回「不确定」）单列「待判定：无实质传导证据」——保持可见（未配 LLM
# 的日子事件仍能被看到），但不再挂在受益区标题下（R1：标题下挂「未判定」
# 等于没降级；issue 红线是「受益 = 明说有实质影响，其余全降级」）。
SECTION_BULLISH = "受益个股"
SECTION_CONCEPT_PENDING = "待判定：无实质传导证据"
SECTION_CONCEPT_ONLY = "仅概念关联"
SECTION_NON_INVESTABLE = "非投资信号"

# 老报告 JSON 的 pending 键名：分区改名单列后保留别名键（同值），老解析方读旧键
# 仍拿得到数据；老报告本身（只有旧键）也照常可读
LEGACY_SECTION_CONCEPT_PENDING = "概念关联（未做实质传导判定）"

# 只有「受益个股」算主区；待判定单列成节，不再进受益区
MAIN_SECTIONS = (SECTION_BULLISH,)


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


def _is_bullish(c: dict, min_strength: int) -> bool:
    return c["direction"] == "利好" and c["strength"] >= min_strength and c["tickers"]


def _verdicts_by_event(deepdives) -> dict[str, list[bool]]:
    """event_id -> 该事件各条深读的可判定语义票（True=实质影响，False=无实质影响）。

    只认 DeepDive.substantive 的结构化枚举（LLM 侧语义判定），不再扫 transmission
    自由文本——WAN-15 根因就是词表命中漏掉了 impact 里的否定性判断。
    「不确定」/缺失不可判定，不进这张表，但计入 `_totals_by_event` 的分母。
    """
    out: dict[str, list[bool]] = {}
    for d in deepdives or []:
        verdict = substantive_verdict(getattr(d, "substantive", ""))
        if verdict is None or not getattr(d, "event_id", ""):
            continue
        out.setdefault(d.event_id, []).append(verdict)
    return out


def _totals_by_event(deepdives) -> dict[str, int]:
    """event_id -> 该事件深读总条数（含「不确定」/缺失，计入表决分母）。"""
    out: dict[str, int] = {}
    for d in deepdives or []:
        eid = getattr(d, "event_id", "")
        if eid:
            out[eid] = out.get(eid, 0) + 1
    return out


def _event_section(ev, verdicts_by_event: dict[str, list[bool]],
                   totals_by_event: dict[str, int] | None = None) -> str:
    """深读结论反向决定事件分区。

    - 没有可投资信号 → 非投资信号（信号闸门已判定）
    - 没有可判定票（未配 LLM / substantive 全为「不确定」或缺失）→ 待判定：
      单列可见，不进受益区（R1）
    - 是票必须 **> 全部深读数的一半**才升级受益（O2：不确定/缺失计入分母、
      视为不抵升级——「不确定」不是「明说」）；否、打平、票不够 → 仅概念关联
    - 任何情形都绝不拿 transmission 文本猜升级（fail-closed）
    """
    if not [c for c in ev.conclusions if c.get("investable", True)]:
        return SECTION_NON_INVESTABLE
    verdicts = verdicts_by_event.get(ev.event_id) or []
    if not verdicts:
        return SECTION_CONCEPT_PENDING
    total = (totals_by_event or {}).get(ev.event_id) or len(verdicts)
    if sum(1 for v in verdicts if v) * 2 > total:
        return SECTION_BULLISH
    return SECTION_CONCEPT_ONLY


def _qualifying(ev, min_strength: int) -> list[dict]:
    return [c for c in ev.conclusions if _is_bullish(c, min_strength)]


def _render_event(lines: list[str], ev, qualifying: list[dict], min_strength: int,
                  section: str, index: int, max_picks: int) -> None:
    # 事件内个股先按 score 降序去重、再截断到 max_picks：与逻辑层同一口径，
    # 渲染层不会因为「筛过一遍结论」而把上限放大回去
    rows = merge_tickers(qualifying, max_picks)
    doc_count = len(ev.doc_ids)
    lines.append(f"### {index}. {ev.title}")
    lines.append("")
    lines.append(f"- 事件ID：`{ev.event_id}`（合并 {doc_count} 篇文档）")
    lines.append(f"- 分区：{section}")
    lines.append(f"- 来源：{ev.org or '—'} · {ev.meeting_type or '—'} · "
                 f"{ev.published_at or '日期未知'}")
    lines.append(f"- 原文：[{ev.source_url}]({ev.source_url})")
    lines.append(f"- 快照：`{relative_snapshot(ev.snapshot_path)}`")
    lines.append(f"- 事件内信号 {len(ev.conclusions)} 条，其中力度 ≥ {min_strength} 且有映射 "
                 f"{len(qualifying)} 条")
    lines.append("")
    for c in qualifying:
        lines.append(f"#### `{c['signal_id']}` {c['summary']}")
        lines.append("")
        lines.append(f"- 信号ID：`{c['signal_id']}`")
        lines.append(f"- 力度/周期/性质：strength **{c['strength']}** / {c['horizon']} / {c['novelty']}")
        lines.append(f"- 主题：{', '.join(c['theme_tags']) or '—'}")
        lines.append(f"- 工具：{', '.join(c['instruments']) or '—'}")
        lines.append(f"- 置信度：{c['confidence']}（引用校验：{'通过' if c['quote_verified'] else '未通过'}）")
        lines.append(f"- 来源：{c['org']} · {c['meeting_type']} · {c['published_at'] or '日期未知'}")
        lines.append(f"- 原文：{_source_links(c)}")
        lines.append(f"- 快照：`{relative_snapshot(c['raw_snapshot_path'])}`")
        lines.append("")
        lines.append(f"> {c['quote']}")
        lines.append("")
    lines.append("| # | 代码 | 名称 | 板块 | 市值(亿) | score | 相关性 | 受益理由 | 映射路径 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for i, t in enumerate(rows, start=1):
        rel = t.get("relevance", t.get("mapping_confidence", 0))
        lines.append(
            f"| {i} | {t['ticker_code']} | {t['ticker_name']} | "
            f"{t['sector_name']}({t['sector_source']}) | {t['market_cap']:.1f} | {t['score']} | "
            f"{rel} | {t['why']} | {t['mapping_path']} |"
        )
    lines.append("")


def _non_investable_lines(conclusions: list[dict]) -> list[str]:
    """非投资信号区：一行一条，带判定依据与原文链接，方便逐条复核判定。"""
    rows = [c for c in conclusions if not c.get("investable", True)]
    if not rows:
        return []
    lines = ["## 非投资信号（泛经济样板表述，不映射板块、不展开个股）", "",
             "| 信号ID | 摘要 | 判定依据 | 原文 |",
             "|---|---|---|---|"]
    for c in rows:
        reason = c.get("investable_reason", "") or "—"
        lines.append(f"| `{c['signal_id']}` | {c['summary'][:40]} | {reason} | "
                     f"[{c['org']}]({c['source_url']}) |")
    lines.append("")
    return lines


def _gate_line(gate: dict) -> str:
    if not gate:
        return "—"
    labels = {"profile_hit": "业务命中放行", "weak_hit": "经营范围弱命中",
              "no_profile": "无画像放行", "unparsed_profile": "F10已取未解析放行",
              "profile_miss": "无实质交集",
              "disabled": "闸门关闭", "dropped": "直接剔除"}
    parts = [f"{labels.get(k, k)} {v}" for k, v in gate.items() if v]
    return "、".join(parts) or "—"


def _source_links(c: dict) -> str:
    return f"[{c['source_url']}]({c['source_url']})"


def render_markdown(result: RunResult) -> str:
    s = result.stats
    min_strength = s.get("min_strength_for_report", 3)
    max_picks = int(s.get("max_picks", Settings.max_picks))
    events = _ensure_events(result)
    lines: list[str] = []
    lines.append(f"# PolicyPulse 日报 {result.window_end}")
    lines.append("")
    lines.append(f"> {DISCLAIMER}")
    lines.append("")
    lines.append("## 今日概览")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 统计窗口 | {result.window_start} ~ {result.window_end}"
                 f"（{s.get('window_days', '?')} 天） |")
    lines.append(f"| run_id | `{result.run_id}` |")
    lines.append(f"| 抓取文档 | {s['docs']} |")
    lines.append(f"| 政策事件（聚类后） | {s.get('events', len(events))} |")
    lines.append(f"| 抽取信号 | {s['signals']} |")
    lines.append(f"| 有映射个股的信号 | {s['with_tickers']} |")
    non_inv = s.get("non_investable_signals", 0)
    gate_off = not s.get("investable_gate", True)
    lines.append(f"| 信号级可投资性闸门 | "
                 f"{'关闭（全部按可投资处理）' if gate_off else f'非投资信号 {non_inv} 条'} |")
    lines.append(f"| 个股结论行数（事件内去重后） | {s['ticker_rows']} |")
    lines.append(f"| 信息源（可用/总数） | {s.get('sources_ok', 0)} / {s['sources_total']} |")
    lines.append(f"| 抓取失败条目 | {s.get('fetch_failures', 0)} |")
    lines.append(f"| 跨 run 去重跳过 | {s.get('duplicates_skipped', 0)} |")
    lines.append(f"| 窗口外剔除（详情页日期） | {s.get('out_of_window_docs', 0)} |")
    lines.append(f"| 公司级相关性闸门 | {_gate_line(s.get('gate', {}))} |")
    lines.append(f"| 个股深度研读 | {s.get('deepdive_count', 0)} 条 |")
    lines.append(f"| LLM 抽取 | {'开启' if s['llm_enabled'] else '关闭（规则抽取）'} |")
    lines.append(f"| tongstock 调用（不含缓存命中） | {s['tongstock_calls']} |")
    pbc_docs = s.get("pbc_related_docs", 0)
    pbc_src = s.get("pbc_related_sources") or []
    lines.append(f"| 央行（中国人民银行）相关文档 | {pbc_docs}"
                 f"{'（来源：' + '、'.join(pbc_src) + '）' if pbc_src else ''} |")
    lines.append("")
    lines.append(f"> 口径说明：{MAPPING_CAVEAT}")
    lines.append("")
    lines.append(f"> {INVESTABLE_CAVEAT}")
    lines.append("")
    lines.append(f"> {GATE_CAVEAT}")
    lines.append("")

    warned: list[str] = list(result.failed_sources) + list(result.warnings)
    if warned:
        lines.append("### 抓取告警")
        lines.append("")
        # 30：窗口外剔除改为逐条告警后，15 条会把明细截掉，数字就不可回溯了
        for item in warned[:30]:
            lines.append(f"- {item}")
        lines.append("")

    if result.duplicates_skipped:
        lines.append(f"- 另有 {result.duplicates_skipped} 篇文档正文指纹已在库（跨 run 去重），本次未重复抽取。")
        lines.append("")
    out_window = s.get("out_of_window_docs", 0)
    if out_window:
        lines.append(f"- {out_window} 篇文档的详情页发布日期落在统计窗口外，已剔除"
                     "（列表页无日期的源靠详情页日期兜底判定，不进当日日报）。")
        lines.append("")
    no_update = s.get("sources_no_update") or []
    if no_update:
        lines.append(f"- 窗口内无更新的源：{'、'.join(no_update)}（如实记录，不再回退抓旧稿）。")
        lines.append("")

    if not result.conclusions:
        lines.append("## 今日无新增政策 / 事件")
        lines.append("")
        lines.append(f"统计窗口 {result.window_start} ~ {result.window_end} 内没有抓到新文档，"
                     "或文档未产生可映射的信号。需要更早的稿子请显式回补："
                     "`policypulse run --since N`。")
        lines.append("")
        lines.extend(_compliance_lines(result))
        lines.append("## 运行元信息")
        lines.append("")
        lines.append(f"- 数据库：`data/policypulse.db`（run_id `{result.run_id}`）")
        lines.append("")
        lines.append(f"> {DISCLAIMER}")
        return "\n".join(lines)

    bullish_all = [c for c in result.conclusions if c["direction"] == "利好" and c["tickers"]]
    bullish = [c for c in bullish_all if c["strength"] >= min_strength]
    filtered_out = len(bullish_all) - len(bullish)
    bullish.sort(key=lambda c: (-c["strength"], -c["confidence"]))

    deepdives = list(getattr(result, "deepdives", None) or [])
    verdicts = _verdicts_by_event(deepdives)
    totals = _totals_by_event(deepdives)
    sections = {ev.event_id: _event_section(ev, verdicts, totals) for ev in events}
    main_events = [ev for ev in events
                   if sections[ev.event_id] in MAIN_SECTIONS and _qualifying(ev, min_strength)]
    concept_events = [ev for ev in events
                      if sections[ev.event_id] == SECTION_CONCEPT_ONLY and _qualifying(ev, min_strength)]
    pending_events = [ev for ev in events
                      if sections[ev.event_id] == SECTION_CONCEPT_PENDING and _qualifying(ev, min_strength)]

    lines.append("## 受益个股")
    lines.append("")
    lines.append(f"> 选股上限：每个政策事件按 score（越高 = 相对上涨概率越大）降序，"
                 f"只列前 {max_picks} 只；候选不足 {max_picks} 只时按实际数量列出，不补位。")
    lines.append("")
    if filtered_out:
        lines.append(f"另有 {filtered_out} 条利好结论力度 < {min_strength}，未列入本表，详见同名 JSON 与数据库。")
        lines.append("")
    if not bullish:
        lines.append(f"本轮没有命中「利好、有板块映射且力度 ≥ {min_strength}」的信号。")
        lines.append("")
    if not main_events:
        lines.append("**今日无实质投资信号**：没有深读明判「有实质影响」的政策事件。"
                     "下面是仅概念关联、待判定与非投资信号留痕。")
        lines.append("")
    for i, ev in enumerate(main_events, start=1):
        _render_event(lines, ev, _qualifying(ev, min_strength), min_strength,
                      sections[ev.event_id], i, max_picks)

    if concept_events:
        lines.append("## 仅概念关联（深读判定无实质传导，不授予受益头衔）")
        lines.append("")
        for i, ev in enumerate(concept_events, start=1):
            _render_event(lines, ev, _qualifying(ev, min_strength), min_strength,
                          sections[ev.event_id], i, max_picks)

    if pending_events:
        lines.append("## 待判定：无实质传导证据（未做深读判定，留痕可见但不进受益区）")
        lines.append("")
        for i, ev in enumerate(pending_events, start=1):
            _render_event(lines, ev, _qualifying(ev, min_strength), min_strength,
                          sections[ev.event_id], i, max_picks)

    lines.extend(_non_investable_lines(result.conclusions))

    bullish_ids = {c["signal_id"] for c in bullish}
    others = [c for c in result.conclusions
              if c["signal_id"] not in bullish_ids and c.get("investable", True)]
    if others:
        lines.append("## 其余信号（中性/利空/力度不足/无映射）")
        lines.append("")
        lines.append("| 信号ID | 方向 | 力度 | 摘要 | 来源 |")
        lines.append("|---|---|---|---|---|")
        for c in others:
            lines.append(f"| `{c['signal_id']}` | {c['direction']} | {c['strength']} | "
                         f"{c['summary'][:40]} | [{c['org']}]({c['source_url']}) |")
        lines.append("")

    if deepdives:
        lines.append("## 个股深度研读（每条结论均带溯源）")
        lines.append("")
        for d in deepdives:
            lines.append(f"### {d.ticker_name}（{d.ticker_code}）· {d.policy_title}")
            lines.append("")
            lines.append(f"- 政策：{d.policy_summary}")
            lines.append(f"- 传导路径：{d.transmission}")
            lines.append(f"- 竞争地位：{d.competition}")
            lines.append(f"- 影响判断：{d.impact}")
            if getattr(d, "substantive", ""):
                lines.append(f"- 实质影响判定：{d.substantive}（语义判定，决定事件分区）")
            if d.risks:
                lines.append(f"- 风险提示：{'；'.join(d.risks)}")
            # 拼好再嵌入：嵌套同引号 f-string 是 PEP 701（仅 3.12+），3.10/3.11 直接 SyntaxError
            src_lines = [f"{x['label']}（`{x['ref']}`）" for x in d.sources]
            lines.append(f"- 溯源：{'; '.join(src_lines)}")
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
    deepdives = list(getattr(result, "deepdives", None) or [])
    verdicts = _verdicts_by_event(deepdives)
    totals = _totals_by_event(deepdives)
    events = _ensure_events(result)
    sections = {ev.event_id: _event_section(ev, verdicts, totals) for ev in events}
    pending_ids = [e.event_id for e in events
                   if sections[e.event_id] == SECTION_CONCEPT_PENDING]
    payload = {
        "run_id": result.run_id,
        "window": {"start": result.window_start, "end": result.window_end},
        "stats": result.stats,
        "failed_sources": result.failed_sources,
        "warnings": result.warnings,
        "conclusions": result.conclusions,
        "events": [
            {
                "event_id": e.event_id, "title": e.title, "org": e.org,
                "meeting_type": e.meeting_type, "published_at": e.published_at,
                "source_url": e.source_url, "doc_ids": e.doc_ids,
                "signal_ids": [c["signal_id"] for c in e.conclusions],
                "tickers": e.tickers,
                # 反向分区：json 与 md/html 三端一致
                "investable": bool([c for c in e.conclusions if c.get("investable", True)]),
                "section": sections[e.event_id],
                "transmission_verdicts": verdicts.get(e.event_id, []),
            }
            for e in events
        ],
        "sections": {
            "受益个股": [e.event_id for e in events
                       if sections[e.event_id] == SECTION_BULLISH],
            "仅概念关联": [e.event_id for e in events
                        if sections[e.event_id] == SECTION_CONCEPT_ONLY],
            # R1：pending 单列，键名与 md/html 标题同步
            SECTION_CONCEPT_PENDING: pending_ids,
            # 老键别名（同值）：老解析方仍读旧键、老报告仍读得到，兼容不破
            LEGACY_SECTION_CONCEPT_PENDING: pending_ids,
            "非投资信号": [c["signal_id"] for c in result.conclusions
                        if not c.get("investable", True)],
        },
        "deepdives": [
            {
                "ticker_code": d.ticker_code, "ticker_name": d.ticker_name,
                "policy_title": d.policy_title, "transmission": d.transmission,
                "competition": d.competition, "impact": d.impact,
                "substantive": getattr(d, "substantive", ""),
                "risks": d.risks, "sources": d.sources,
                "event_id": getattr(d, "event_id", ""),
            }
            for d in deepdives
        ],
        "disclaimer": DISCLAIMER,
        "mapping_caveat": MAPPING_CAVEAT,
        "investable_caveat": INVESTABLE_CAVEAT,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _event_html(ev, verdicts_by_event: dict[str, list[bool]],
                totals_by_event: dict[str, int] | None, min_strength: int,
                max_picks: int) -> str:
    """单个事件的 HTML 段：与 md 的 _render_event 同一套分区与筛选口径。"""
    section = _event_section(ev, verdicts_by_event, totals_by_event)
    qualifying = _qualifying(ev, min_strength)
    table_rows = merge_tickers(qualifying, max_picks)
    trows = "".join(
        f"<tr><td>{i}</td><td>{_esc(t['ticker_code'])}</td><td>{_esc(t['ticker_name'])}</td>"
        f"<td>{_esc(t['sector_name'])}</td><td>{t['market_cap']:.1f}</td><td>{t['score']}</td>"
        f"<td>{t.get('relevance', t.get('mapping_confidence', 0))}</td>"
        f"<td>{_esc(t['why'])}</td>"
        f"<td><a href='{_esc(ev.source_url)}' target='_blank'>原文</a></td></tr>"
        for i, t in enumerate(table_rows, start=1)
    )
    sigs = "".join(
        f"<h4><code>{_esc(c['signal_id'])}</code> {_esc(c['summary'])}</h4>"
        f"<p class='meta'>strength {c['strength']} · 置信度 {c['confidence']} · "
        f"引用校验 {'通过' if c['quote_verified'] else '未通过'} · {_esc(c['org'])} · "
        f"{_esc(c['published_at'])}</p>"
        f"<blockquote>{_esc(c['quote'])}</blockquote>"
        for c in qualifying
    )
    return (
        f"<section><h3>{_esc(ev.title)}</h3>"
        f"<p class='meta'>事件ID <code>{_esc(ev.event_id)}</code> · 分区：{_esc(section)} · "
        f"{_esc(ev.org)} · {_esc(ev.meeting_type)} · "
        f"{_esc(ev.published_at)} · 合并 {len(ev.doc_ids)} 篇文档 · 信号 {len(ev.conclusions)} 条</p>"
        f"{sigs}"
        f"<table><thead><tr><th>#</th><th>代码</th><th>名称</th><th>板块</th><th>市值(亿)</th>"
        f"<th>score</th><th>相关性</th><th>理由</th><th>溯源</th></tr></thead>"
        f"<tbody>{trows}</tbody></table></section>"
    )


def _non_investable_html(conclusions: list[dict]) -> str:
    """非投资信号区（HTML）：与 md 同名分区同一批信号、同一判定依据。"""
    rows = [c for c in conclusions if not c.get("investable", True)]
    if not rows:
        return ""
    cells = "".join(
        f"<tr><td><code>{_esc(c['signal_id'])}</code></td><td>{_esc(c['summary'][:40])}</td>"
        f"<td>{_esc(c.get('investable_reason', '') or '—')}</td>"
        f"<td><a href='{_esc(c['source_url'])}' target='_blank'>{_esc(c['org'])}</a></td></tr>"
        for c in rows
    )
    return (
        "<h2>非投资信号（泛经济样板表述，不映射板块、不展开个股）</h2>"
        "<table><thead><tr><th>信号ID</th><th>摘要</th><th>判定依据</th><th>原文</th></tr></thead>"
        f"<tbody>{cells}</tbody></table>"
    )


def _sections_html(result, events, min_strength: int, max_picks: int) -> str:
    """HTML 的分区，与 md / json 使用同一套 _event_section 判定。"""
    deepdives = list(getattr(result, "deepdives", None) or [])
    verdicts = _verdicts_by_event(deepdives)
    totals = _totals_by_event(deepdives)
    sections = {ev.event_id: _event_section(ev, verdicts, totals) for ev in events}
    main_events = [ev for ev in events
                   if sections[ev.event_id] in MAIN_SECTIONS and _qualifying(ev, min_strength)]
    concept_events = [ev for ev in events
                      if sections[ev.event_id] == SECTION_CONCEPT_ONLY and _qualifying(ev, min_strength)]
    pending_events = [ev for ev in events
                      if sections[ev.event_id] == SECTION_CONCEPT_PENDING and _qualifying(ev, min_strength)]

    parts = ["<h2>受益个股</h2>",
             f"<p class='meta'>选股上限：每个政策事件按 score（越高 = 相对上涨概率越大）"
             f"降序只列前 {max_picks} 只；不足按实际数量列出，不补位。</p>"]
    if main_events:
        parts.extend(_event_html(ev, verdicts, totals, min_strength, max_picks)
                     for ev in main_events)
    else:
        parts.append("<p><strong>今日无实质投资信号</strong>：没有深读明判「有实质影响」"
                     "的政策事件。下面是仅概念关联、待判定与非投资信号留痕。</p>")
    if concept_events:
        parts.append("<h2>仅概念关联（深读判定无实质传导，不授予受益头衔）</h2>")
        parts.extend(_event_html(ev, verdicts, totals, min_strength, max_picks)
                     for ev in concept_events)
    if pending_events:
        parts.append("<h2>待判定：无实质传导证据（未做深读判定，留痕可见但不进受益区）</h2>")
        parts.extend(_event_html(ev, verdicts, totals, min_strength, max_picks)
                     for ev in pending_events)
    parts.append(_non_investable_html(result.conclusions))
    return "".join(parts)


def render_html(result: RunResult) -> str:
    s = result.stats
    min_strength = s.get("min_strength_for_report", 3)
    max_picks = int(s.get("max_picks", Settings.max_picks))
    events = _ensure_events(result)
    non_investable = [c for c in result.conclusions if not c.get("investable", True)]

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

    if not result.conclusions:
        body = f"<p>{_esc(EMPTY_DAY_NOTICE)}</p>"
        if s["docs"] == 0:
            body = (f"<p><strong>{_esc(EMPTY_DAY_NOTICE)}</strong>（窗口 "
                    f"{_esc(result.window_start)} ~ {_esc(result.window_end)}）</p>")
    else:
        body = _sections_html(result, events, min_strength, max_picks)

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>PolicyPulse 日报 {_esc(result.window_end)}</title>
<style>
body{{font-family:-apple-system,"PingFang SC",Helvetica,Arial,sans-serif;margin:32px auto;max-width:1080px;color:#1b1b1b;line-height:1.6}}
h1{{font-size:24px}} h2{{font-size:20px;margin-top:26px}} h3{{font-size:18px;margin-bottom:4px}} h4{{font-size:15px;margin-bottom:2px}}
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
<p class="caveat">{_esc(INVESTABLE_CAVEAT)}</p>
<p class="caveat">{_esc(GATE_CAVEAT)}</p>
<p>窗口 {_esc(result.window_start)} ~ {_esc(result.window_end)} · 文档 {s['docs']} · 事件 {s.get('events', len(events))} · 信号 {s['signals']} · 个股结论 {s['ticker_rows']} · 非投资信号 {len(non_investable)} · 央行相关 {s.get('pbc_related_docs', 0)}</p>
{body}
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
