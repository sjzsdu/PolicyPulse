"""日报渲染：Markdown（主交付）+ JSON + 单页 HTML。

正文按「政策事件」组织（同文档/同发布会的信号聚成一条事件，个股按代码去重一张表），
不再逐条信号重复贴一模一样的个股表。
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
             "| 源ID | 机构 | 状态 | robots 判定 | 判定依据 | 本轮文档 | 本轮去向 |",
             "|---|---|---|---|---|---|---|"]
    for row in rows:
        flag = "启用" if row["enabled"] else "停用"
        reason = row["robots_reason"]
        if len(reason) > 60:
            reason = reason[:59] + "…"
        lines.append(f"| `{row['id']}` | {row['org']} | {flag} | {row['robots']} | "
                     f"{reason} | {row['docs']} | {row.get('outcome', '—')} |")
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


def _pick_caption(caps: PickCaps) -> str:
    """口径说明（md / html 共用一句，避免两端各写一套）。"""
    if caps.global_cap > 0:
        return (f"选股上限：整份日报全局最多 {caps.global_cap} 只——跨事件按代码去重、"
                f"按 score（越高 = 相对上涨概率越大）降序取前 {caps.global_cap}；"
                f"事件内不超过 {caps.per_event} 只，候选不足按实际数量列出，不补位。")
    return (f"选股上限：全局截断已关闭（`global_max_picks: 0`），每个政策事件按 score "
            f"降序最多列 {caps.per_event} 只；候选不足按实际数量列出，不补位。")


def _qualifying(ev, min_strength: int) -> list[dict]:
    return [c for c in ev.conclusions if _is_bullish(c, min_strength)]


def _render_event(lines: list[str], ev, qualifying: list[dict], min_strength: int,
                  section: str, index: int, caps: PickCaps) -> None:
    # 先按全局允许集过滤，再按 score 降序去重、截断到事件上限——与逻辑层同一口径，
    # 渲染层不会因为「筛过一遍结论」把上限放大回去，也不会脱钩全局 Top-N
    rows = merge_tickers(_restrict_tickers(qualifying, _event_allowed(caps, ev)),
                         caps.per_event)
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
    if not rows:
        lines.append(f"- 个股：本事件个股未进入全局选股 Top-{caps.global_cap}，不列个股表。")
        lines.append("")
        return
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
    events = _ensure_events(result)
    caps = pick_caps(s, events)
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
    lines.append(f"| 窗口内文档（入库+复用） | {s['docs']} |")
    reused = s.get("reused_docs", 0)
    if reused:
        lines.append(f"| 其中复用库内（信号不重抽） | {reused} |")
    lines.append(f"| 政策事件（聚类后） | {s.get('events', len(events))} |")
    lines.append(f"| 报告信号总数（含复用） | {s['signals']} |")
    extracted = s.get("signals_extracted")
    if extracted is not None and extracted != s["signals"]:
        lines.append(f"| 本 run 新抽取信号 | {extracted} |")
    lines.append(f"| 有映射个股的信号 | {s['with_tickers']} |")
    non_inv = s.get("non_investable_signals", 0)
    gate_off = not s.get("investable_gate", True)
    lines.append(f"| 信号级可投资性闸门 | "
                 f"{'关闭（全部按可投资处理）' if gate_off else f'非投资信号 {non_inv} 条'} |")
    lines.append(f"| 个股结论行（按信号累计） | {s['ticker_rows']} |")
    ticker_event = s.get("ticker_rows_event")
    if ticker_event is not None:
        lines.append(f"| 个股（事件内按代码去重后） | {ticker_event} |")
    if caps.global_cap > 0:
        lines.append(f"| 全局入选个股（上限 {caps.global_cap}） | "
                     f"{len(caps.allowed or set())} |")
    lines.append(f"| 信息源（discover 成功/网络源） | "
                 f"{s.get('sources_ok', 0)} / {s['sources_total']} |")
    lines.append(f"| 抓取失败条目 | {s.get('fetch_failures', 0)} |")
    lines.append(f"| 复用库内文档（跨 run 去重） | {s.get('duplicates_skipped', 0)} |")
    lines.append(f"| 窗口外剔除（详情页日期） | {s.get('out_of_window_docs', 0)} |")
    cached = s.get("out_of_window_cached", 0)
    if cached:
        lines.append(f"| 窗口外旧 URL 缓存跳过（未重复请求） | {cached} |")
    short_body = s.get("short_body_skipped", 0)
    if short_body:
        lines.append(f"| 正文过短剔除 | {short_body} |")
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
        lines.append(f"- 另有 {result.duplicates_skipped} 篇文档正文指纹已在库："
                     "直接复用库内信号与个股展开（不重复抽取，仍完整进入本报告）。")
        lines.append("")
    out_window = s.get("out_of_window_docs", 0)
    if out_window:
        lines.append(f"- {out_window} 篇文档的详情页发布日期落在统计窗口外，已剔除"
                     "（列表页无日期的源靠详情页日期兜底判定，不进当日日报）。")
        lines.append("")
    cached = s.get("out_of_window_cached", 0)
    if cached:
        lines.append(f"- {cached} 篇旧稿此前已判定窗口外，按库内缓存直接跳过，本轮未重复请求详情页。")
        lines.append("")
    short_body = s.get("short_body_skipped", 0)
    if short_body:
        lines.append(f"- {short_body} 篇候选正文长度不足阈值（min_body_length），未入库。")
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
    lines.append(f"> {_pick_caption(caps)}")
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
                      sections[ev.event_id], i, caps)

    if concept_events:
        lines.append("## 仅概念关联（深读判定无实质传导，不授予受益头衔）")
        lines.append("")
        for i, ev in enumerate(concept_events, start=1):
            _render_event(lines, ev, _qualifying(ev, min_strength), min_strength,
                          sections[ev.event_id], i, caps)

    if pending_events:
        lines.append("## 待判定：无实质传导证据（未做深读判定，留痕可见但不进受益区）")
        lines.append("")
        for i, ev in enumerate(pending_events, start=1):
            _render_event(lines, ev, _qualifying(ev, min_strength), min_strength,
                          sections[ev.event_id], i, caps)

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
    snap_dirs = sorted({str(Path(d.snapshot_path).parent) for d in result.docs})
    snap_text = "、".join(f"`{relative_snapshot(p)}`" for p in snap_dirs[:3]) or "—"
    if len(snap_dirs) > 3:
        snap_text += f"（等 {len(snap_dirs)} 个目录，含复用文档的历史快照目录）"
    lines.append(f"- 快照目录：{snap_text}")
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
        "mapping_caveat": MAPPING_CAVEAT,
        "investable_caveat": INVESTABLE_CAVEAT,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _event_html(ev, verdicts_by_event: dict[str, list[bool]],
                totals_by_event: dict[str, int] | None, min_strength: int,
                caps: PickCaps) -> str:
    """单个事件的 HTML 段：与 md 的 _render_event 同一套分区与筛选口径。"""
    section = _event_section(ev, verdicts_by_event, totals_by_event)
    qualifying = _qualifying(ev, min_strength)
    table_rows = merge_tickers(_restrict_tickers(qualifying, _event_allowed(caps, ev)),
                              caps.per_event)
    trows = "".join(
        f"<tr><td>{i}</td><td>{_esc(t['ticker_code'])}</td><td>{_esc(t['ticker_name'])}</td>"
        f"<td>{_esc(t['sector_name'])}</td><td>{t['market_cap']:.1f}</td><td>{t['score']}</td>"
        f"<td>{t.get('relevance', t.get('mapping_confidence', 0))}</td>"
        f"<td>{_esc(t['why'])}</td>"
        f"<td><a href='{_esc(ev.source_url)}' target='_blank'>原文</a></td></tr>"
        for i, t in enumerate(table_rows, start=1)
    )
    table_html = (
        "<div class='tablewrap'><table><thead><tr><th>#</th><th>代码</th><th>名称</th><th>板块</th>"
        "<th>市值(亿)</th>"
        "<th>score</th><th>相关性</th><th>理由</th><th>溯源</th></tr></thead>"
        f"<tbody>{trows}</tbody></table></div>"
        if table_rows else
        f"<p class='meta'>本事件个股未进入全局选股 Top-{caps.global_cap}，不列个股表。</p>"
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
        f"{table_html}</section>"
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
        "<div class='tablewrap'><table><thead><tr><th>信号ID</th><th>摘要</th>"
        "<th>判定依据</th><th>原文</th></tr></thead>"
        f"<tbody>{cells}</tbody></table></div>"
    )


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

def _sections_html(result, events, min_strength: int, caps: PickCaps,
                   parts_nav: dict | None = None) -> str:
    """HTML 的分区，与 md / json 使用同一套 _event_section 判定。"""
    (verdicts, totals, sections, main_events,
     concept_events, pending_events) = _partition(result, events, min_strength)
    if parts_nav is not None:
        parts_nav.update({
            "bullish": bool(main_events), "concept": bool(concept_events),
            "pending": bool(pending_events),
            "non_investable": any(not c.get("investable", True)
                                  for c in result.conclusions),
        })

    parts = ["<a id='sec-bullish'></a><h2>受益个股</h2>",
             f"<p class='meta'>{_esc(_pick_caption(caps))}</p>"]
    if main_events:
        parts.extend(_event_html(ev, verdicts, totals, min_strength, caps)
                     for ev in main_events)
    else:
        parts.append("<p><strong>今日无实质投资信号</strong>：没有深读明判「有实质影响」"
                     "的政策事件。下面是仅概念关联、待判定与非投资信号留痕。</p>")
    if concept_events:
        parts.append("<a id='sec-concept'></a><h2>仅概念关联（深读判定无实质传导，不授予受益头衔）</h2>")
        parts.extend(_event_html(ev, verdicts, totals, min_strength, caps)
                     for ev in concept_events)
    if pending_events:
        parts.append("<a id='sec-pending'></a><h2>待判定：无实质传导证据（未做深读判定，留痕可见但不进受益区）</h2>")
        parts.extend(_event_html(ev, verdicts, totals, min_strength, caps)
                     for ev in pending_events)
    non_inv_html = _non_investable_html(result.conclusions)
    if non_inv_html:
        non_inv_html = "<a id='sec-noninv'></a>" + non_inv_html
    parts.append(non_inv_html)
    return "".join(parts)


_PAGE_CSS = """
:root{--bg:#f3f5f9;--paper:#fff;--ink:#182230;--muted:#667085;--line:#e4e8ef;
--navy:#132439;--gold:#b98a13;--green:#0e7a52;--blue:#1d4ed8;--amber:#b45309;
--gray:#667085;--red:#b42318}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-size:15px;line-height:1.7;
font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Helvetica,Arial,"Microsoft YaHei",sans-serif}
.paper{max-width:1120px;margin:0 auto;padding:26px 20px 64px}
.hero{background:linear-gradient(135deg,#101c30 0%,#1c3352 55%,#274b74 100%);color:#f5f8ff;
border-radius:18px;padding:30px 34px;box-shadow:0 14px 34px rgba(16,28,48,.28)}
.hero h1{margin:0 0 6px;font-size:27px;letter-spacing:.5px}
.hero .sub{color:#bfd2ea;font-size:14px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0 4px}
.kpi{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.16);border-radius:12px;padding:12px 14px}
.kpi .v{font-size:26px;font-weight:700;line-height:1.2}
.kpi .k{font-size:12.5px;color:#c8d8ef;margin-top:2px}
.toc{margin:16px 0 0;display:flex;flex-wrap:wrap;gap:8px}
.toc a{background:#fff;border:1px solid var(--line);color:#344054;text-decoration:none;
padding:5px 13px;border-radius:999px;font-size:13px;box-shadow:0 1px 2px rgba(16,24,40,.05)}
.toc a:hover{border-color:#98a6bd;color:var(--navy)}
.card{background:var(--paper);border:1px solid var(--line);border-radius:16px;padding:22px 26px;
margin:18px 0;box-shadow:0 2px 10px rgba(16,24,40,.05)}
h1{font-size:26px} h2{font-size:20px;margin:6px 0 12px;padding-left:12px;border-left:5px solid var(--navy)}
a#sec-bullish + h2{border-color:var(--green)}
a#sec-concept + h2{border-color:var(--blue)}
a#sec-pending + h2{border-color:var(--amber)}
a#sec-noninv + h2,a#sec-compliance + h2{border-color:var(--gray)}
h3{font-size:17.5px;margin:4px 0 4px} h4{font-size:15px;margin:14px 0 2px;color:#243b53}
.meta{color:var(--muted);font-size:13px;margin-top:0}
section{border:1px solid var(--line);border-radius:14px;padding:16px 18px 14px;margin:14px 0;
background:linear-gradient(180deg,#fff 0%,#fcfdff 100%)}
section h3{margin-top:0}
blockquote{border-left:4px solid var(--gold);margin:8px 0;padding:9px 14px;background:#fbf7ec;color:#4a3d17;border-radius:0 10px 10px 0;font-size:14.5px}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;margin:10px 0 8px;font-size:14px;background:#fff}
th,td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}
th{background:#f0f3f8;color:#344054;font-weight:600;white-space:nowrap;position:sticky;top:0}
tbody tr:nth-child(even){background:#f8fafc}
tbody tr:hover{background:#eef4fd}
td code{font-size:12.5px}
.disc{background:#fff5f5;border:1px solid #f2c9c9;border-radius:12px;padding:12px 16px;color:#8f2727;font-size:13.5px}
.caveat{background:#fdf9ec;border:1px solid #ead9a6;border-radius:12px;padding:11px 15px;color:#6d5716;font-size:13.5px;margin:8px 0}
.notice{background:#eef6ff;border:1px solid #cfe0f7;border-radius:12px;padding:14px 18px;color:#1f4d8f}
.small{font-size:12.5px;color:var(--muted)}
.pill{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;border:1px solid transparent}
.pill.on{background:#e7f6ef;color:#0e7a52;border-color:#bfe8d7}
.pill.off{background:#f2f4f7;color:#667085;border-color:#e4e7ec}
.statline{display:flex;flex-wrap:wrap;gap:8px 18px;background:#fff;border:1px solid var(--line);
border-radius:12px;padding:11px 16px;font-size:13.5px;color:#475467;margin:14px 0}
.statline b{color:var(--navy)}
footer{margin-top:26px}
@media print{body{background:#fff}.card,section{box-shadow:none}.toc{display:none}}
"""
def render_html(result: RunResult) -> str:
    s = result.stats
    min_strength = s.get("min_strength_for_report", 3)
    events = _ensure_events(result)
    caps = pick_caps(s, events)
    non_investable = [c for c in result.conclusions if not c.get("investable", True)]
    (verdicts, totals, sections, main_events,
     concept_events, pending_events) = _partition(result, events, min_strength)
    nav_flags = {"bullish": bool(main_events), "concept": bool(concept_events),
                 "pending": bool(pending_events), "non_investable": bool(non_investable)}

    compliance = ""
    rows_detail = s.get("sources_detail") or []
    if rows_detail:
        cells = "".join(
            f"<tr><td><code>{_esc(r['id'])}</code></td><td>{_esc(r['org'])}</td>"
            f"<td><span class='pill {'on' if r['enabled'] else 'off'}'>"
            f"{'启用' if r['enabled'] else '停用'}</span></td>"
            f"<td>{_esc(r['robots'])}</td>"
            f"<td class='small'>{_esc(r['robots_reason'])}</td><td>{r['docs']}</td>"
            f"<td>{_esc(r.get('outcome', '—'))}</td></tr>"
            for r in rows_detail)
        compliance = (
            "<a id='sec-compliance'></a><h2>信息源与合规</h2><div class='tablewrap'>"
            "<table class='ledger'><thead><tr><th>源ID</th><th>机构</th><th>状态</th>"
            "<th>robots 判定</th><th>判定依据</th><th>本轮文档</th><th>本轮去向</th>"
            f"</tr></thead><tbody>{cells}</tbody></table></div>")

    if not result.conclusions:
        body = f"<p class='notice'>{_esc(EMPTY_DAY_NOTICE)}</p>"
        if s.get("docs", 0) == 0:
            body = (f"<p class='notice'><strong>{_esc(EMPTY_DAY_NOTICE)}</strong>（窗口 "
                    f"{_esc(result.window_start)} ~ {_esc(result.window_end)}）</p>")
    else:
        body = _sections_html(result, events, min_strength, caps, parts_nav=nav_flags)

    nav_items = []
    if nav_flags["bullish"]:
        nav_items.append(("sec-bullish", "受益个股"))
    if nav_flags["concept"]:
        nav_items.append(("sec-concept", "仅概念关联"))
    if nav_flags["pending"]:
        nav_items.append(("sec-pending", "待判定"))
    if nav_flags["non_investable"]:
        nav_items.append(("sec-noninv", "非投资信号"))
    if compliance:
        nav_items.append(("sec-compliance", "信息源与合规"))
    toc = "".join(f"<a href='#{fid}'>{label}</a>" for fid, label in nav_items)
    toc = f"<nav class='toc'>{toc}</nav>" if toc else ""

    kpis = [
        (s.get("docs", len(result.docs)), "窗口内文档（入库+复用）"),
        (s.get("events", len(events)), "政策事件（聚类后）"),
        (s.get("signals", len(result.conclusions)), "报告信号总数"),
        (s.get("ticker_rows_event", s.get("ticker_rows", 0)), "个股（事件内去重）"),
        (len(non_investable), "非投资信号"),
        (s.get("pbc_related_docs", 0), "央行相关文档"),
    ]
    kpi_html = "".join(f"<div class='kpi'><div class='v'>{v}</div>"
                       f"<div class='k'>{_esc(k)}</div></div>" for v, k in kpis)
    stat_bits = [
        f"窗口 <b>{_esc(result.window_start)} ~ {_esc(result.window_end)}</b>",
        f"run_id <b>{_esc(result.run_id)}</b>",
        f"有映射个股的信号 <b>{s.get('with_tickers', 0)}</b>",
        f"全局入选 <b>{len(caps.allowed or set()) if caps.global_cap > 0 else s['ticker_rows']}</b>",
        f"跨 run 复用 <b>{s.get('duplicates_skipped', 0)}</b> 篇",
        f"窗口外剔除 <b>{s.get('out_of_window_docs', 0)}</b>",
        f"抓取失败 <b>{s.get('fetch_failures', 0)}</b>",
        f"LLM 抽取 <b>{'开启' if s.get('llm_enabled') else '关闭（规则）'}</b>",
        f"深度研读 <b>{s.get('deepdive_count', 0)}</b> 条",
    ]
    statline = "<div class='statline'>" + "".join(f"<span>{b}</span>" for b in stat_bits) + "</div>"

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PolicyPulse 日报 {_esc(result.window_end)}</title>
<style>{_PAGE_CSS}</style></head><body><div class="paper">
<header class="hero">
<h1>PolicyPulse 日报 <span style="color:#ffd97a">{_esc(result.window_end)}</span></h1>
<p class="sub">窗口 {_esc(result.window_start)} ~ {_esc(result.window_end)} · 数据仅供研究参考</p>
<div class="kpis">{kpi_html}</div>
</header>
{toc}
<main class="card">
<p class="disc">{_esc(DISCLAIMER)}</p>
<p class="caveat">口径说明：{_esc(MAPPING_CAVEAT)}</p>
<p class="caveat">{_esc(INVESTABLE_CAVEAT)}</p>
<p class="caveat">{_esc(GATE_CAVEAT)}</p>
{statline}
{body}
{compliance}
</main>
<footer><p class="disc">{_esc(DISCLAIMER)}</p></footer>
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
