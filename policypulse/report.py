"""日报渲染：Markdown（主交付）+ JSON（结构化）+ 单页 HTML（浏览器阅读）。

正文按「政策事件」组织（同文档/同发布会的信号聚成一条事件，个股按代码去重一张表），
不再逐条信号重复贴一模一样的个股表。

**md / html 是给读者看的**：运行期指标（run_id、抓取告警、快照路径、事件/信号内部 ID、
robots 合规台账、闸门统计、复核命令）一律不进这两个出口——它们留在 JSON 与数据库里
供复核。正文只讲三件事：政策说了什么、会怎么传导、落在哪些公司上。
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path

from policypulse.clustering import _ensure_events, merge_tickers
from policypulse.config import (
    DISCLAIMER,
    EMPTY_DAY_NOTICE,
    GATE_CAVEAT,
    INVESTABLE_CAVEAT,
    MAPPING_CAVEAT,
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

# 展示文案：JSON 里的分区键保持不变（机器可读、老解析方不破），
# md / html 的标题换成读者一眼能懂的说话方式——不再出现「闸门」「分区」「深读判定」
# 这类实现层词汇。
SECTION_LABELS = {
    SECTION_BULLISH: "受益个股",
    SECTION_CONCEPT_ONLY: "主题关联",
    SECTION_CONCEPT_PENDING: "待确认：暂无实质传导证据",
    SECTION_NON_INVESTABLE: "泛政策表述",
}

SECTION_NOTES = {
    SECTION_BULLISH: "政策明确指向具体行业，下列个股按政策受益度从高到低排列。",
    SECTION_CONCEPT_ONLY: "政策与下列行业有关，但本期没有找到足以判断「会实质影响公司业绩」的证据，"
                          "仅作主题参考，不算受益结论。",
    SECTION_CONCEPT_PENDING: "这些政策事件本期还没有做出实质影响判断，先列出可关联的主题与个股，"
                             "不作为受益结论。",
    SECTION_NON_INVESTABLE: "以下表述偏整体性、方向性，没有指向具体行业或公司，本期不展开个股分析。",
}

STRENGTH_WORDS = {1: "弱", 2: "偏弱", 3: "中等", 4: "较强", 5: "强"}
HORIZON_WORDS = (("短期", "短期（1 个月内）"), ("中期", "中期（1–6 个月）"),
                 ("长期", "长期（6 个月以上）"))
SUBSTANTIVE_LABELS = {"是": "有实质影响", "否": "无实质影响", "不确定": "待确认"}


def _esc(text) -> str:
    return html.escape(str(text or ""))


# ----------------------------------------------------------------- 读者语言

def strength_label(value) -> str:
    """1–5 的力度分数 → 「4/5（较强）」：保留原始分数，同时给读者一个刻度感。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value or "—")
    word = STRENGTH_WORDS.get(n)
    return f"{n}/5（{word}）" if word else f"{n}/5"


def horizon_label(value) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    for key, label in HORIZON_WORDS:
        if text.startswith(key):
            return label
    return text


def confidence_label(value) -> str:
    """模型研判可信度：0–1 的原始值 + 高/中/偏低的口径词。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    word = "高" if num >= 0.8 else ("中" if num >= 0.6 else "偏低")
    return f"{word}（{num:.2f}）"


def substantive_label(value) -> str:
    return SUBSTANTIVE_LABELS.get(str(value or "").strip(), "待确认")


def _brief(text, limit: int = 40) -> str:
    """表格里的长句截断到一行宽，截断处留省略号（不静默丢字）。"""
    s = str(text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _theme_text(c: dict) -> str:
    return "、".join(c.get("theme_tags") or []) or "—"


def _instrument_text(c: dict) -> str:
    return "、".join(c.get("instruments") or []) or "—"


def _source_meta(ev) -> list[str]:
    """事件头一行的来源信息：机构 · 会议/文件类型 · 发布日期。"""
    meta = [ev.org or "来源未标注", ev.meeting_type or "", ev.published_at or "发布日期未标注"]
    return [m for m in meta if m]


def _pick_caption(caps: "PickCaps") -> str:
    """选股口径（md / html 共用一句），说清「最多几只、怎么排」。"""
    if caps.global_cap > 0:
        return (f"整份日报最多列出 {caps.global_cap} 只个股：跨政策事件按代码去重，"
                f"按综合评分从高到低取前 {caps.global_cap} 只；单个事件最多 {caps.per_event} 只，"
                f"候选不足时按实际数量列出，不补位。")
    return (f"每个政策事件按综合评分从高到低最多列 {caps.per_event} 只，"
            f"候选不足时按实际数量列出，不补位。")


def _metric_caption() -> str:
    return ("综合评分：政策力度、公司与主题的业务相关度、市值流动性与政策性质加权所得（0–100），"
            "越高表示政策受益度越高，也越靠前；主题相关度：公司与该政策主题的业务匹配程度（0–1）。")


def _empty_event_notice(caps: "PickCaps") -> str:
    if caps.global_cap > 0:
        return f"本事件的相关个股未进入本期名单（整份日报最多 {caps.global_cap} 只），此处不列个股表。"
    return "本事件的相关个股暂无足够数据，此处不列个股表。"


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


@dataclass(frozen=True)
class PickCaps:
    """选股上限口径（WAN-21）：事件内上限 + 全局上限（0=关闭）+ 全局允许集（None=不过滤）。"""

    per_event: int
    global_cap: int
    allowed: set[str] | None


def pick_caps(stats: dict, events: list) -> PickCaps:
    """从 stats 读出口径；老对象/老报告缺键时回落 Settings 默认值并从 events 反推允许集。

    `allowed=None` 表示不做全局过滤（`global_max_picks=0` 关闭时，或拿不到事件时）。
    允许集为空集合则表示「本份日报一只都没入选」，此时所有事件表都为空。
    """
    global_cap = int(stats.get("global_max_picks", Settings.global_max_picks))
    per_event = int(stats.get("max_picks", Settings.max_picks))
    if global_cap <= 0:
        return PickCaps(per_event=per_event, global_cap=0, allowed=None)
    raw = stats.get("global_top_codes")
    if raw is None:
        # 老对象（stats 里没有全局口径）：events 已被 _ensure_events 按同一口径截断，
        # 用它们的并集当允许集，md/html/json 就不会和 json 的 events 脱钩
        raw = sorted({t.get("ticker_code", "") for e in events for t in e.tickers} - {""})
    return PickCaps(per_event=per_event, global_cap=global_cap, allowed=set(raw))


def _restrict_tickers(conclusions: list[dict], allowed: set[str] | None) -> list[dict]:
    """按全局允许集过滤个股行——在去重/截断之前过滤，低分行绝不会因先截断被漏掉。"""
    if allowed is None:
        return conclusions
    return [
        {**c, "tickers": [t for t in c.get("tickers", [])
                          if t.get("ticker_code") in allowed]}
        for c in conclusions
    ]


def _event_allowed(caps: PickCaps, ev) -> set[str] | None:
    """每事件的「所有权集」：全局允许集 ∩ 该事件回写后的个股码。

    同码被一天内多个事件同时命中时，全局口径只保留 score 最高那行（归它所属的事件），
    这条所有权只存在于回写后的 `ev.tickers` 里——渲染层从 conclusions 重算时必须再取一次
    交集，否则另一个事件会用自己那份低分行把同一个码重新列出来（日报总行数超上限）。

    `caps.allowed is None`（`global_max_picks: 0` 关闭）时原样透传 None，
    不能拿 `ev.tickers` 反过来限制关闭口径。
    """
    if caps.allowed is None:
        return None
    return caps.allowed & {t.get("ticker_code", "") for t in ev.tickers}


def _event_ticker_rows(ev, caps: PickCaps) -> list[dict]:
    """该事件在 json 出口的个股行：按同一所有权口径过滤（events 已回写时幂等）。"""
    allowed = _event_allowed(caps, ev)
    if allowed is None:
        return list(ev.tickers)
    return [t for t in ev.tickers if t.get("ticker_code") in allowed]


def _qualifying(ev, min_strength: int) -> list[dict]:
    return [c for c in ev.conclusions if _is_bullish(c, min_strength)]


def _event_picks(ev, qualifying: list[dict], caps: PickCaps) -> list[dict]:
    """渲染层个股行：先按全局允许集过滤，再按 score 降序去重、截断到事件上限。

    与逻辑层同一口径——渲染层不会因为「筛过一遍结论」把上限放大回去，
    也不会脱钩全局 Top-N。
    """
    return merge_tickers(_restrict_tickers(qualifying, _event_allowed(caps, ev)),
                         caps.per_event)


# ------------------------------------------------------------------ 概览/来源

def _source_count(stats: dict) -> int:
    """来源渠道数：优先用统计里的成功数，回落到合规台账里启用的源数。"""
    ok = int(stats.get("sources_ok") or 0)
    if ok:
        return ok
    return len([r for r in (stats.get("sources_detail") or []) if r.get("enabled", True)])


def _overview_lines(result: RunResult, events: list, caps: PickCaps,
                    min_strength: int) -> list[str]:
    """读者视角的概览：只看「本期有哪些政策、落到哪些公司」，不看管道指标。"""
    s = result.stats
    orgs = []
    for ev in events:
        if ev.org and ev.org not in orgs:
            orgs.append(ev.org)
    org_text = "、".join(orgs[:3]) + (f" 等 {len(orgs)} 个渠道" if len(orgs) > 3 else "")
    displayed = sum(len(_event_picks(ev, _qualifying(ev, min_strength), caps)) for ev in events)
    with_tickers = s.get("with_tickers", 0)
    lines = ["## 本期概览", ""]
    lines.append(f"- **统计区间**：{result.window_start} ~ {result.window_end}"
                 + (f"（{s['window_days']} 天）" if s.get("window_days") else ""))
    lines.append(f"- **政策事件**：{s.get('events', len(events))} 起"
                 + (f"，来自 {org_text}" if org_text else ""))
    lines.append(f"- **政策信号**：{s.get('signals', len(result.conclusions))} 条"
                 + (f"，其中 {with_tickers} 条落到具体行业与个股" if with_tickers else ""))
    lines.append(f"- **列出个股**：{displayed} 只")
    if _source_count(s):
        lines.append(f"- **信息来源**：{_source_count(s)} 个官方渠道")
    lines.append("")
    return lines


def _source_rows(stats: dict) -> list[tuple[str, int]]:
    """启用的信息源按机构合并（同一机构常有多条栏目源，读者只关心这家机构收了多少篇）。"""
    merged: dict[str, int] = {}
    for r in (stats.get("sources_detail") or []):
        if not r.get("enabled", True):
            continue
        org = str(r.get("org") or r.get("id") or "")
        if org:
            merged[org] = merged.get(org, 0) + int(r.get("docs") or 0)
    return list(merged.items())


def _source_lines(stats: dict) -> list[str]:
    """信息来源：机构 + 本期收录篇数。robots 判定等合规细节留在 JSON，不进正文。"""
    rows = _source_rows(stats)
    if not rows:
        return []
    lines = ["## 信息来源", "", "| 机构 | 本期收录 |", "|---|---|"]
    for org, docs in rows:
        lines.append(f"| {org} | {f'{docs} 篇' if docs else '无更新'} |")
    lines.append("")
    failed = int(stats.get("sources_failed") or 0)
    if failed:
        lines.append(f"> 本期有 {failed} 个渠道访问失败，可能影响覆盖完整性。")
        lines.append("")
    return lines


# --------------------------------------------------------------------- Markdown

def _render_event(lines: list[str], ev, qualifying: list[dict], index: int,
                  caps: PickCaps) -> None:
    rows = _event_picks(ev, qualifying, caps)
    lines.append(f"### {index}. {ev.title}")
    lines.append("")
    lines.append(f"{' · '.join(_source_meta(ev))} · [查看政策原文]({ev.source_url})")
    lines.append("")
    for c in qualifying:
        lines.append(f"**{c['summary']}**")
        lines.append("")
        lines.append(f"- 影响力度 {strength_label(c['strength'])}"
                     f" · 影响周期 {horizon_label(c['horizon'])}"
                     f" · 政策性质 {c['novelty'] or '—'}")
        lines.append(f"- 涉及主题：{_theme_text(c)}")
        lines.append(f"- 政策工具：{_instrument_text(c)}")
        lines.append(f"- 研判可信度：{confidence_label(c['confidence'])}")
        lines.append("")
        lines.append(f"> {c['quote']}")
        lines.append("")
    if not rows:
        lines.append(f"- {_empty_event_notice(caps)}")
        lines.append("")
        return
    lines.append("| # | 代码 | 名称 | 所属板块 | 市值（亿元） | 综合评分 | 主题相关度 | 受益理由 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for i, t in enumerate(rows, start=1):
        rel = t.get("relevance", t.get("mapping_confidence", 0))
        lines.append(
            f"| {i} | {t['ticker_code']} | {t['ticker_name']} | {t['sector_name']} | "
            f"{t['market_cap']:.1f} | {t['score']} | {rel} | {t['why']} |"
        )
    lines.append("")


def _non_investable_lines(conclusions: list[dict]) -> list[str]:
    """泛政策表述区：一行一条，只说「这条没指向具体行业」，不摆判定规则。"""
    rows = [c for c in conclusions if not c.get("investable", True)]
    if not rows:
        return []
    lines = [f"## {SECTION_LABELS[SECTION_NON_INVESTABLE]}", "",
             f"> {SECTION_NOTES[SECTION_NON_INVESTABLE]}", "",
             "| 政策表述 | 原文 |", "|---|---|"]
    for c in rows:
        lines.append(f"| {_brief(c['summary'])} | [{c['org']}]({c['source_url']}) |")
    lines.append("")
    return lines


def _other_signal_lines(conclusions: list[dict], rendered_ids: set[str]) -> list[str]:
    """其他政策信号：中性/偏空/影响有限/暂未落到板块的信号，供读者知道「还有这些」。"""
    rows = [c for c in conclusions
            if c["signal_id"] not in rendered_ids and c.get("investable", True)]
    if not rows:
        return []
    lines = ["## 其他政策信号（中性、偏空或影响有限）", "",
             "> 这些信号本期没有形成明确的受益个股，仅作记录。", "",
             "| 方向 | 影响力度 | 政策信号 | 原文 |", "|---|---|---|---|"]
    for c in rows:
        lines.append(f"| {c['direction']} | {strength_label(c['strength'])} | "
                     f"{_brief(c['summary'])} | [{c['org']}]({c['source_url']}) |")
    lines.append("")
    return lines


def _deepdive_lines(result: RunResult) -> list[str]:
    """个股深度研判：每只入选个股的政策传导、竞争地位、影响判断与风险。"""
    dives = list(getattr(result, "deepdives", None) or [])
    if not dives:
        return []
    lines = ["## 个股深度研判", "",
             "> 针对本期入选个股，逐只说明政策怎么传导到公司、公司处在什么位置、"
             "以及需要留意的风险。", ""]
    for d in dives:
        lines.append(f"### {d.ticker_name}（{d.ticker_code}）· {d.policy_title}")
        lines.append("")
        if d.policy_summary:
            lines.append(f"- 主题关联：{d.policy_summary}")
        if d.transmission:
            lines.append(f"- 传导路径：{d.transmission}")
        if d.competition:
            lines.append(f"- 竞争地位：{d.competition}")
        if d.impact:
            lines.append(f"- 影响判断：{d.impact}")
        if getattr(d, "substantive", ""):
            lines.append(f"- 实质影响：{substantive_label(d.substantive)}")
        if d.risks:
            lines.append(f"- 风险提示：{'；'.join(d.risks)}")
        # 拼好再嵌入：嵌套同引号 f-string 是 PEP 701（仅 3.12+），3.10/3.11 直接 SyntaxError
        src_lines = [f"{x['label']}（`{x['ref']}`）" for x in d.sources]
        if src_lines:
            lines.append(f"- 溯源：{'; '.join(src_lines)}")
        lines.append("")
    return lines


def _partition(result, events, min_strength: int):
    """分区共用：md / json / html 三端都从这里拿同一批结果。"""
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
    return verdicts, totals, sections, main_events, concept_events, pending_events


def render_markdown(result: RunResult) -> str:
    s = result.stats
    min_strength = s.get("min_strength_for_report", 3)
    events = _ensure_events(result)
    caps = pick_caps(s, events)
    lines: list[str] = [f"# PolicyPulse 日报 · {result.window_end}", ""]
    lines.extend(_overview_lines(result, events, caps, min_strength))

    if not result.conclusions:
        lines.append(f"## {EMPTY_DAY_NOTICE}")
        lines.append("")
        lines.append(f"统计区间 {result.window_start} ~ {result.window_end} 内没有新的"
                     "政策发布或会议通稿，本期无新增内容。")
        lines.append("")
        lines.extend(_source_lines(s))
        lines.append(f"> {DISCLAIMER}")
        return "\n".join(lines)

    (_, _, _, main_events,
     concept_events, pending_events) = _partition(result, events, min_strength)

    lines.append(f"## {SECTION_LABELS[SECTION_BULLISH]}")
    lines.append("")
    lines.append(f"> {_pick_caption(caps)}")
    lines.append("")
    lines.append(f"> {_metric_caption()}")
    lines.append("")
    if not main_events:
        lines.append("本期暂无明确受益个股：没有找到「政策明确指向、且能落到具体公司」的事件。"
                     "下面按主题关联与政策留痕列出，供参考。")
        lines.append("")
    for i, ev in enumerate(main_events, start=1):
        _render_event(lines, ev, _qualifying(ev, min_strength), i, caps)

    if concept_events:
        lines.append(f"## {SECTION_LABELS[SECTION_CONCEPT_ONLY]}")
        lines.append("")
        lines.append(f"> {SECTION_NOTES[SECTION_CONCEPT_ONLY]}")
        lines.append("")
        for i, ev in enumerate(concept_events, start=1):
            _render_event(lines, ev, _qualifying(ev, min_strength), i, caps)

    if pending_events:
        lines.append(f"## {SECTION_LABELS[SECTION_CONCEPT_PENDING]}")
        lines.append("")
        lines.append(f"> {SECTION_NOTES[SECTION_CONCEPT_PENDING]}")
        lines.append("")
        for i, ev in enumerate(pending_events, start=1):
            _render_event(lines, ev, _qualifying(ev, min_strength), i, caps)

    # 「其他政策信号」只收正文事件块里没出现过的信号，避免同一条写两遍
    rendered_ids = {c["signal_id"]
                    for ev in main_events + concept_events + pending_events
                    for c in _qualifying(ev, min_strength)}
    lines.extend(_other_signal_lines(result.conclusions, rendered_ids))
    lines.extend(_non_investable_lines(result.conclusions))
    lines.extend(_deepdive_lines(result))
    lines.extend(_source_lines(s))
    lines.append(f"> {DISCLAIMER}")
    return "\n".join(lines)


def render_json(result: RunResult) -> str:
    deepdives = list(getattr(result, "deepdives", None) or [])
    verdicts = _verdicts_by_event(deepdives)
    totals = _totals_by_event(deepdives)
    events = _ensure_events(result)
    caps = pick_caps(result.stats, events)
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
                # 全局允许集过滤（幂等）：events 已由逻辑层回写，这里再兜一层保证三端同口径
                "tickers": _event_ticker_rows(e, caps),
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
        # 三条口径说明仅供机器/复核方读取：md / html 正文不再印这些规则话术
        "mapping_caveat": MAPPING_CAVEAT,
        "investable_caveat": INVESTABLE_CAVEAT,
        "gate_caveat": GATE_CAVEAT,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------------- HTML

_PAGE_CSS = """
:root{
--ink:#131a26;--ink-soft:#3b4757;--muted:#6b7a90;--line:#e6eaf2;--bg:#f4f6fb;--card:#fff;
--navy:#152a45;--navy-2:#203f63;--gold:#b8860b;--gold-soft:#fdf7e7;
--green:#0f7a55;--green-soft:#eaf7f1;--blue:#1d4ed8;--blue-soft:#eff4ff;
--amber:#a8620a;--amber-soft:#fdf5e9;--gray:#5b6b82;--gray-soft:#f2f4f8;--red:#b42318;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-size:15px;line-height:1.72;
font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",Helvetica,Arial,sans-serif;
-webkit-font-smoothing:antialiased}
.paper{max-width:1040px;margin:0 auto;padding:28px 20px 72px}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.92em}

/* ---------- 头部 ---------- */
.hero{position:relative;overflow:hidden;background:linear-gradient(135deg,#0f1e33 0%,#1b3454 58%,#274d78 100%);
color:#f4f8ff;border-radius:20px;padding:34px 36px 28px;box-shadow:0 18px 40px rgba(15,30,51,.26)}
.hero .eyebrow{font-size:12.5px;letter-spacing:.22em;color:#9db8d8;text-transform:uppercase}
.hero h1{margin:8px 0 6px;font-size:34px;line-height:1.2;letter-spacing:.5px}
.hero h1 .date{color:#ffd97a}
.hero .lede{margin:6px 0 0;color:#cfdcee;font-size:15px;max-width:56em}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:22px 0 6px}
.kpi{background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.16);border-radius:14px;padding:13px 15px}
.kpi b{display:block;font-size:27px;font-weight:700;line-height:1.15}
.kpi span{font-size:12.5px;color:#c3d5ea}
.hero .meta{margin:14px 0 0;color:#9db8d8;font-size:12.5px}

/* ---------- 导航 ---------- */
.toc{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 0}
.toc a{background:var(--card);border:1px solid var(--line);color:var(--ink-soft);padding:6px 14px;
border-radius:999px;font-size:13px;box-shadow:0 1px 2px rgba(16,24,40,.04);transition:.15s}
.toc a:hover{border-color:#9fb2cc;color:var(--navy);text-decoration:none;transform:translateY(-1px)}

/* ---------- 内容区块 ---------- */
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px 28px 20px;
margin:18px 0;box-shadow:0 2px 12px rgba(16,24,40,.045)}
.card>h2{margin:0 0 4px;font-size:21px;letter-spacing:.2px;display:flex;align-items:center;gap:10px}
.card>h2::before{content:"";width:6px;height:22px;border-radius:3px;background:var(--navy)}
#bullish>h2::before{background:var(--green)}
#concept>h2::before{background:var(--blue)}
#pending>h2::before{background:var(--amber)}
#noninv>h2::before,#others>h2::before,#deepdive>h2::before,#sources>h2::before{background:var(--gray)}
.caption{color:var(--muted);font-size:13.5px;margin:6px 0 4px}
.notice{background:var(--gold-soft);border:1px solid #ecd9a5;border-radius:12px;padding:12px 16px;
color:#6d5716;font-size:14px;margin:12px 0}

/* ---------- 事件 ---------- */
.event{border:1px solid var(--line);border-radius:16px;padding:18px 20px 12px;margin:16px 0;
background:linear-gradient(180deg,#fff 0%,#fbfcff 100%)}
.event>header{display:flex;flex-direction:column;gap:4px;margin-bottom:10px}
.event h3{margin:0;font-size:18px;line-height:1.45}
.event .meta{margin:0;color:var(--muted);font-size:13px}
.event .meta .org{color:var(--ink-soft);font-weight:600}
.event .meta a{white-space:nowrap}

.signal{padding:12px 14px;border-radius:12px;background:#f8fafd;border:1px solid #eef2f8;margin:12px 0}
.signal h4{margin:0 0 8px;font-size:15.5px;color:var(--navy-2);line-height:1.55}
.facts{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:13px;color:var(--ink-soft);margin:0 0 8px}
.fct em{font-style:normal;color:var(--muted);margin-right:5px}
.fct b{font-weight:600}
blockquote{margin:8px 0 0;border-left:3px solid var(--gold);background:#fffdf6;color:#4b3f19;
padding:10px 14px;border-radius:0 10px 10px 0;font-size:14px}

/* ---------- 表格 ---------- */
.tablewrap{overflow-x:auto;margin:12px 0 6px}
table{border-collapse:collapse;width:100%;font-size:13.8px;background:#fff;border-radius:12px;overflow:hidden}
thead th{background:#eef2f9;color:#33415c;font-weight:600;font-size:13px;white-space:nowrap;
padding:9px 12px;text-align:left;border-bottom:1px solid var(--line)}
tbody td{padding:9px 12px;border-bottom:1px solid #eef1f7;vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tbody tr:nth-child(even){background:#fafbfe}
tbody tr:hover{background:#f2f7ff}
td.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink-soft)}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
th.num{text-align:right}
.score{display:inline-flex;align-items:center;gap:8px;justify-content:flex-end;min-width:92px}
.score i{position:relative;display:block;width:46px;height:6px;border-radius:99px;background:#e8edf5;overflow:hidden}
.score i::after{content:"";display:block;height:100%;width:var(--w,0%);border-radius:99px;
background:linear-gradient(90deg,#79b7a3,#0f7a55)}
.score b{font-weight:600;min-width:36px;text-align:right}
.why{color:var(--ink-soft)}

/* ---------- 深度研判 ---------- */
.dive{border:1px solid var(--line);border-radius:16px;padding:16px 20px;margin:14px 0;background:#fcfdff}
.dive h3{margin:0 0 10px;font-size:17px}
.dive dl{margin:0;display:grid;grid-template-columns:96px 1fr;gap:6px 14px;font-size:14px}
.dive dt{color:var(--muted)}
.dive dd{margin:0;color:var(--ink-soft)}
.tag{display:inline-block;padding:1px 10px;border-radius:999px;font-size:12.5px;border:1px solid transparent;margin-left:6px}
.tag.yes{background:var(--green-soft);color:var(--green);border-color:#c7e9da}
.tag.no{background:var(--gray-soft);color:var(--gray);border-color:#e1e6ee}
.tag.unknown{background:var(--amber-soft);color:var(--amber);border-color:#eedcc0}

/* ---------- 收尾 ---------- */
.disclaimer{background:#fff8f8;border:1px solid #f3d2d2;color:#8f2727;font-size:13.5px}
.disclaimer h2{font-size:16px;margin:0 0 6px}
footer{color:var(--muted);font-size:12.5px;text-align:center;margin-top:22px;line-height:1.9}
@media (max-width:640px){
.paper{padding:16px 12px 48px}.hero{padding:24px 20px;border-radius:16px}.hero h1{font-size:26px}
.card{padding:18px 16px}.event{padding:14px 14px 8px}
.dive dl{grid-template-columns:78px 1fr}
}
@media print{
body{background:#fff}.paper{max-width:none;padding:0}.hero{box-shadow:none}
.card,.event,.dive{box-shadow:none;break-inside:avoid}.toc{display:none}
}
"""


def _signal_html(c: dict) -> str:
    facts = "".join([
        f"<span class='fct'><em>影响力度</em><b>{_esc(strength_label(c['strength']))}</b></span>",
        f"<span class='fct'><em>影响周期</em><b>{_esc(horizon_label(c['horizon']))}</b></span>",
        f"<span class='fct'><em>政策性质</em><b>{_esc(c['novelty'] or '—')}</b></span>",
        f"<span class='fct'><em>涉及主题</em><b>{_esc(_theme_text(c))}</b></span>",
        f"<span class='fct'><em>政策工具</em><b>{_esc(_instrument_text(c))}</b></span>",
        f"<span class='fct'><em>研判可信度</em><b>{_esc(confidence_label(c['confidence']))}</b></span>",
    ])
    return (f"<div class='signal'><h4>{_esc(c['summary'])}</h4>"
            f"<p class='facts'>{facts}</p>"
            f"<blockquote>{_esc(c['quote'])}</blockquote></div>")


def _score_cell(score) -> str:
    try:
        pct = max(0.0, min(100.0, float(score)))
    except (TypeError, ValueError):
        pct = 0.0
    return (f"<span class='score'><i style='--w:{pct:.0f}%'></i>"
            f"<b>{_esc(score)}</b></span>")


def _picks_table_html(rows: list[dict]) -> str:
    body = "".join(
        f"<tr><td>{i}</td><td class='code'>{_esc(t['ticker_code'])}</td>"
        f"<td><b>{_esc(t['ticker_name'])}</b></td><td>{_esc(t['sector_name'])}</td>"
        f"<td class='num'>{t['market_cap']:.1f}</td>"
        f"<td class='num'>{_score_cell(t['score'])}</td>"
        f"<td class='num'>{_esc(t.get('relevance', t.get('mapping_confidence', 0)))}</td>"
        f"<td class='why'>{_esc(t['why'])}</td></tr>"
        for i, t in enumerate(rows, start=1)
    )
    return ("<div class='tablewrap'><table><thead><tr><th>#</th><th>代码</th><th>名称</th>"
            "<th>所属板块</th><th class='num'>市值（亿元）</th><th class='num'>综合评分</th>"
            "<th class='num'>主题相关度</th><th>受益理由</th></tr></thead>"
            f"<tbody>{body}</tbody></table></div>")


def _event_html(ev, min_strength: int, caps: PickCaps) -> str:
    """单个事件的 HTML 段：与 md 的 _render_event 同一套分区与筛选口径。"""
    qualifying = _qualifying(ev, min_strength)
    rows = _event_picks(ev, qualifying, caps)
    meta = " · ".join(_esc(m) if i else f"<span class='org'>{_esc(m)}</span>"
                      for i, m in enumerate(_source_meta(ev)))
    sigs = "".join(_signal_html(c) for c in qualifying)
    table = (_picks_table_html(rows) if rows
             else f"<p class='caption'>{_esc(_empty_event_notice(caps))}</p>")
    return (f"<article class='event'><header>"
            f"<h3>{_esc(ev.title)}</h3>"
            f"<p class='meta'>{meta} · "
            f"<a href='{_esc(ev.source_url)}' target='_blank' rel='noopener'>查看政策原文 ↗</a></p>"
            f"</header>{sigs}{table}</article>")


def _non_investable_html(conclusions: list[dict]) -> str:
    """泛政策表述区（HTML）：与 md 同名分区同一批信号。"""
    rows = [c for c in conclusions if not c.get("investable", True)]
    if not rows:
        return ""
    cells = "".join(
        f"<tr><td>{_esc(_brief(c['summary']))}</td>"
        f"<td><a href='{_esc(c['source_url'])}' target='_blank' rel='noopener'>"
        f"{_esc(c['org'])}</a></td></tr>"
        for c in rows
    )
    return (f"<section class='card' id='noninv'><h2>{SECTION_LABELS[SECTION_NON_INVESTABLE]}</h2>"
            f"<p class='caption'>{SECTION_NOTES[SECTION_NON_INVESTABLE]}</p>"
            "<div class='tablewrap'><table><thead><tr><th>政策表述</th><th>原文</th></tr></thead>"
            f"<tbody>{cells}</tbody></table></div></section>")


def _other_signals_html(conclusions: list[dict], rendered_ids: set[str]) -> str:
    rows = [c for c in conclusions
            if c["signal_id"] not in rendered_ids and c.get("investable", True)]
    if not rows:
        return ""
    cells = "".join(
        f"<tr><td>{_esc(c['direction'])}</td><td class='num'>{_esc(strength_label(c['strength']))}</td>"
        f"<td>{_esc(_brief(c['summary']))}</td>"
        f"<td><a href='{_esc(c['source_url'])}' target='_blank' rel='noopener'>"
        f"{_esc(c['org'])}</a></td></tr>"
        for c in rows
    )
    return ("<section class='card' id='others'><h2>其他政策信号（中性、偏空或影响有限）</h2>"
            "<p class='caption'>这些信号本期没有形成明确的受益个股，仅作记录。</p>"
            "<div class='tablewrap'><table><thead><tr><th>方向</th><th class='num'>影响力度</th>"
            "<th>政策信号</th><th>原文</th></tr></thead>"
            f"<tbody>{cells}</tbody></table></div></section>")


def _deepdive_html(result: RunResult) -> str:
    dives = list(getattr(result, "deepdives", None) or [])
    if not dives:
        return ""
    tag_class = {"是": "yes", "否": "no"}
    parts = ["<section class='card' id='deepdive'><h2>个股深度研判</h2>"
             "<p class='caption'>针对本期入选个股，逐只说明政策怎么传导到公司、"
             "公司处在什么位置，以及需要留意的风险。</p>"]
    for d in dives:
        rows = []
        if d.policy_summary:
            rows.append(("主题关联", _esc(d.policy_summary)))
        if d.transmission:
            rows.append(("传导路径", _esc(d.transmission)))
        if d.competition:
            rows.append(("竞争地位", _esc(d.competition)))
        if d.impact:
            rows.append(("影响判断", _esc(d.impact)))
        if getattr(d, "substantive", ""):
            label = substantive_label(d.substantive)
            cls = tag_class.get(str(d.substantive).strip(), "unknown")
            rows.append(("实质影响", f"<span class='tag {cls}'>{_esc(label)}</span>"))
        if d.risks:
            rows.append(("风险提示", _esc("；".join(d.risks))))
        if d.sources:
            links = "；".join(f"{_esc(x['label'])}（<code>{_esc(x['ref'])}</code>）"
                             for x in d.sources)
            rows.append(("溯源", links))
        body = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
        parts.append(f"<div class='dive'><h3>{_esc(d.ticker_name)}"
                     f"（{_esc(d.ticker_code)}）· {_esc(d.policy_title)}</h3>"
                     f"<dl>{body}</dl></div>")
    parts.append("</section>")
    return "".join(parts)


def _sources_html(stats: dict) -> str:
    rows = _source_rows(stats)
    if not rows:
        return ""
    cells = "".join(
        f"<tr><td>{_esc(org)}</td><td class='num'>"
        f"{(f'{docs} 篇' if docs else '无更新')}</td></tr>"
        for org, docs in rows)
    failed = int(stats.get("sources_failed") or 0)
    note = (f"<p class='caption'>本期有 {failed} 个渠道访问失败，可能影响覆盖完整性。</p>"
            if failed else "")
    return ("<section class='card' id='sources'><h2>信息来源</h2>"
            "<p class='caption'>本期检查的官方渠道，以及各自收录到的政策/会议稿件数量。</p>"
            "<div class='tablewrap'><table><thead><tr><th>机构</th>"
            f"<th class='num'>本期收录</th></tr></thead><tbody>{cells}</tbody></table></div>"
            f"{note}</section>")


def _sections_html(result, events, min_strength: int, caps: PickCaps,
                   parts_nav: dict | None = None) -> str:
    """HTML 的分区，与 md / json 使用同一套 _event_section 判定。"""
    (_, _, _, main_events,
     concept_events, pending_events) = _partition(result, events, min_strength)
    if parts_nav is not None:
        parts_nav.update({
            "bullish": bool(main_events), "concept": bool(concept_events),
            "pending": bool(pending_events),
            "non_investable": any(not c.get("investable", True)
                                  for c in result.conclusions),
        })

    parts = ["<section class='card' id='bullish'>",
             f"<h2>{SECTION_LABELS[SECTION_BULLISH]}</h2>",
             f"<p class='caption'>{_esc(_pick_caption(caps))}</p>",
             f"<p class='caption'>{_esc(_metric_caption())}</p>"]
    if main_events:
        parts.extend(_event_html(ev, min_strength, caps) for ev in main_events)
    else:
        parts.append("<p class='notice'><strong>本期暂无明确受益个股</strong>：没有找到"
                     "「政策明确指向、且能落到具体公司」的事件。下面按主题关联与政策留痕"
                     "列出，供参考。</p>")
    parts.append("</section>")
    if concept_events:
        parts.append("<section class='card' id='concept'>")
        parts.append(f"<h2>{SECTION_LABELS[SECTION_CONCEPT_ONLY]}</h2>")
        parts.append(f"<p class='caption'>{SECTION_NOTES[SECTION_CONCEPT_ONLY]}</p>")
        parts.extend(_event_html(ev, min_strength, caps) for ev in concept_events)
        parts.append("</section>")
    if pending_events:
        parts.append("<section class='card' id='pending'>")
        parts.append(f"<h2>{SECTION_LABELS[SECTION_CONCEPT_PENDING]}</h2>")
        parts.append(f"<p class='caption'>{SECTION_NOTES[SECTION_CONCEPT_PENDING]}</p>")
        parts.extend(_event_html(ev, min_strength, caps) for ev in pending_events)
        parts.append("</section>")

    rendered_ids = {c["signal_id"]
                    for ev in main_events + concept_events + pending_events
                    for c in _qualifying(ev, min_strength)}
    parts.append(_other_signals_html(result.conclusions, rendered_ids))
    parts.append(_non_investable_html(result.conclusions))
    return "".join(parts)


def render_html(result: RunResult) -> str:
    s = result.stats
    min_strength = s.get("min_strength_for_report", 3)
    events = _ensure_events(result)
    caps = pick_caps(s, events)
    displayed = sum(len(_event_picks(ev, _qualifying(ev, min_strength), caps)) for ev in events)
    (_, _, _, main_events,
     concept_events, pending_events) = _partition(result, events, min_strength)
    nav_flags = {"bullish": bool(main_events), "concept": bool(concept_events),
                 "pending": bool(pending_events),
                 "non_investable": any(not c.get("investable", True)
                                       for c in result.conclusions)}

    if not result.conclusions:
        body = (f"<section class='card' id='bullish'><h2>{EMPTY_DAY_NOTICE}</h2>"
                f"<p class='notice'>统计区间 {_esc(result.window_start)} ~ "
                f"{_esc(result.window_end)} 内没有新的政策发布或会议通稿，本期无新增内容。</p>"
                "</section>")
    else:
        body = _sections_html(result, events, min_strength, caps, parts_nav=nav_flags)

    rendered_ids = {c["signal_id"]
                    for ev in main_events + concept_events + pending_events
                    for c in _qualifying(ev, min_strength)}
    has_others = any(c["signal_id"] not in rendered_ids and c.get("investable", True)
                     for c in result.conclusions)

    orgs: list[str] = []
    for ev in events:
        if ev.org and ev.org not in orgs:
            orgs.append(ev.org)
    with_tickers = s.get("with_tickers", 0)
    lede_bits = [f"跟踪到 <b>{s.get('events', len(events))}</b> 起政策事件",
                 f"<b>{s.get('signals', len(result.conclusions))}</b> 条政策信号"]
    if with_tickers:
        lede_bits.append(f"其中 <b>{with_tickers}</b> 条落到具体行业与个股")
    lede = "，".join(lede_bits) + "。"

    kpis = [
        (s.get("events", len(events)), "政策事件"),
        (s.get("signals", len(result.conclusions)), "政策信号"),
        (displayed, "列出个股"),
    ]
    if _source_count(s):
        kpis.append((_source_count(s), "信息来源"))
    kpi_html = "".join(f"<div class='kpi'><b>{v}</b><span>{_esc(k)}</span></div>" for v, k in kpis)

    nav_items = []
    if nav_flags["bullish"]:
        nav_items.append(("bullish", SECTION_LABELS[SECTION_BULLISH]))
    if nav_flags["concept"]:
        nav_items.append(("concept", SECTION_LABELS[SECTION_CONCEPT_ONLY]))
    if nav_flags["pending"]:
        nav_items.append(("pending", SECTION_LABELS[SECTION_CONCEPT_PENDING]))
    if nav_flags["non_investable"]:
        nav_items.append(("noninv", SECTION_LABELS[SECTION_NON_INVESTABLE]))
    if has_others:
        nav_items.append(("others", "其他政策信号"))
    if getattr(result, "deepdives", None):
        nav_items.append(("deepdive", "个股深度研判"))
    if [r for r in (s.get("sources_detail") or []) if r.get("enabled", True)]:
        nav_items.append(("sources", "信息来源"))
    toc = "".join(f"<a href='#{fid}'>{_esc(label)}</a>" for fid, label in nav_items)
    toc = f"<nav class='toc'>{toc}</nav>" if toc else ""

    window_note = f"统计区间 {result.window_start} ~ {result.window_end}"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PolicyPulse 日报 · {_esc(result.window_end)}</title>
<style>{_PAGE_CSS}</style></head><body><div class="paper">
<header class="hero">
<div class="eyebrow">PolicyPulse · 政策信号日报</div>
<h1><span class="date">{_esc(result.window_end)}</span></h1>
<p class="lede">{lede}</p>
<div class="kpis">{kpi_html}</div>
<p class="meta">{_esc(window_note)} · 数据仅供研究参考，不构成投资建议</p>
</header>
{toc}
<main>
{body}
{_deepdive_html(result)}
{_sources_html(s)}
<section class="card disclaimer" id="disclaimer">
<h2>免责声明</h2>
<p>{_esc(DISCLAIMER)}</p>
</section>
</main>
<footer>PolicyPulse · 公开政策信息自动汇总与主题映射<br>
个股为政策主题映射结果，不代表公司基本面判断，也不构成任何收益承诺</footer>
</div></body></html>"""


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
