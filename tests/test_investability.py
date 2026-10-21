"""信号级可投资性闸门（改动 1）与深读结论反向分区（改动 2 / 3）的离线单测。

对应 `docs/iteration-prompt-001.md` 验收第 1、2 条：
- 样板话信号 → `investable=False`、不映射板块、不展开个股、不进「受益」表；
- 具名产业信号 → 保留；
- `transmission` 判「中性 / 无实质传导」→ 事件降级「仅概念关联」，判「直接 / 间接」→「受益个股」；
- md / html / json 三端分区一致。

全程离线：不碰网络、不碰 tongstock、不调 LLM。
"""

from __future__ import annotations

import json
import re

from policypulse.clustering import cluster_events
from policypulse.config import Settings, load_theme_alias
from policypulse.deepdive import DeepDive, substantive_transmission
from policypulse.extract import Signal
from policypulse.investability import (
    GENERIC_POLICY_MARKERS,
    assess_investable,
    assess_signal,
    assess_text,
)
from policypulse.pipeline import DocRecord, RunResult, build_conclusions
from policypulse.report import (
    SECTION_BULLISH,
    SECTION_CONCEPT_ONLY,
    SECTION_CONCEPT_PENDING,
    SECTION_NON_INVESTABLE,
    _event_section,
    render_html,
    render_json,
    render_markdown,
)
from policypulse.store import Store
from policypulse.tongstock import CONCEPT_FILE, Ticker

# reports/2026-10-21.md 里 APEC 财长会通稿的原句特征：泛经济样板表述，无具名产业词
APEC_TEXT = ("中国将坚定不移推动高质量发展，加快培育增长新动能，着力扩大国内需求，"
             "持续扩大高水平对外开放，以中国式现代化全面推进强国建设。")

GENERIC_DOC_BODY = (
    "2026年亚太经合组织财长会在香港举行。财政部部长表示，中国将坚定不移推动高质量发展，"
    "加快培育增长新动能，着力扩大国内需求，持续扩大高水平对外开放，以中国式现代化"
    "全面推进强国建设。"
)


def _generic_doc() -> DocRecord:
    return DocRecord(
        doc_id="doc-aaaa000000000001", url="https://www.mof.gov.cn/apec.htm",
        title="2026年亚太经合组织财长会在香港举行", org="财政部", tier="中央",
        source_id="mof_czxw", meeting_type="部长会议", published_at="2026-10-21",
        fetched_at="2026-10-21T16:27:01+0800", content_hash="h1", raw_hash="r1",
        snapshot_path="data/snapshots/run-1/doc-aaaa000000000001.html",
        body=GENERIC_DOC_BODY,
    )


class _RetailMarket:
    """最小数据集：给「关掉闸门」的对照组证明这篇稿子本来映射得出个股。"""

    def sectors(self, block_file: str = CONCEPT_FILE):
        return {"新零售": 4} if block_file == CONCEPT_FILE else {"商业贸易": 4}

    def members(self, sector: str, block_file: str = CONCEPT_FILE):
        return ["601888", "600415"]

    def tickers(self):
        return {
            "601888": Ticker("601888", "中国中免", "sh", "沪市主板", 993.4),
            "600415": Ticker("600415", "小商品城", "sh", "沪市主板", 605.9),
        }


# ---- 改动 1：信号级可投资性闸门 ----


def test_generic_boilerplate_is_not_investable():
    ok, reason = assess_investable(APEC_TEXT)
    assert ok is False
    assert "泛样板" in reason and "扩大国内需求" in reason


def test_named_industry_signal_stays_investable():
    """验收第 1 条：具体产业信号不能被样板词表误杀。"""
    cases = [
        "以全国碳市场为主体完善碳定价机制，扩大全国碳排放权交易市场覆盖范围。",
        "在扩大内需的同时，加快工业母机与数控机床核心技术攻关。",
        "全面实施人工智能+行动，扩大国内需求背景下培育智能网联新能源汽车产业链。",
        "在扩大内需的同时健全终身职业技能培训制度，扩大职业教育产教融合供给。",
        "在扩大内需的同时健全数据产权归属认定，实施数据要素相关试点示范工程。",
    ]
    for text in cases:
        ok, reason = assess_investable(text)
        assert ok is True, f"具名产业信号被误杀：{text} → {reason}"


def test_marker_only_text_without_industry_word_is_blocked():
    for marker in ("扩大内需", "高质量发展", "双循环", "稳中求进"):
        assert marker in " ".join(GENERIC_POLICY_MARKERS)
        ok, reason = assess_investable(f"会议强调{marker}，坚定不移完成全年目标。")
        assert ok is False, f"纯样板句应判非投资：{marker} → {reason}"


def test_signal_field_defaults_conservatively_to_false():
    """不确定时按非投资处理：字段默认 False，只有判定通过才写 True。"""
    assert Signal().investable is False


def test_gate_switch_off_passes_everything():
    settings = Settings(investable_gate=False)
    ok, reason = assess_signal(Signal(summary=APEC_TEXT[:40], quote=APEC_TEXT), settings)
    assert ok is True and "已关闭" in reason
    assert assess_text(APEC_TEXT, settings)[0] is True
    # 默认开：同样一段话必须被拦下
    assert assess_text(APEC_TEXT, Settings())[0] is False


def test_generic_signal_gets_no_sector_match_or_ticker_expansion(tmp_path):
    """管线级：非投资信号仍入库可审计，但跳过板块匹配与个股展开（含历史展开行清理）。"""
    doc = _generic_doc()
    alias = load_theme_alias()

    store = Store(tmp_path / "on.db")
    signals, conclusions = build_conclusions([doc], store, Settings(), _RetailMarket(),
                                             "run-on", alias)
    assert signals, "样板话稿子照样要抽得出信号（仍入库可审计）"
    assert all(s.investable is False for s in signals), \
        f"APEC 式泛话必须判非投资：{[(s.summary, s.investable_reason) for s in signals]}"
    assert conclusions and all(not c["tickers"] and not c["matched_sectors"] for c in conclusions)
    assert all(c["investable"] is False for c in conclusions)
    assert all(c["investable_reason"] for c in conclusions), "判定依据要落到结论里"
    assert store.conn.execute("SELECT COUNT(*) FROM signal_ticker").fetchone()[0] == 0
    row = store.signal(signals[0].signal_id)
    assert row["investable"] == 0 and row["investable_reason"]
    store.close()

    # 对照组：同一个闸门开关关掉，同一篇稿子按旧行为展开个股 —— 证明是闸门在起作用
    store_off = Store(tmp_path / "off.db")
    signals_off, conclusions_off = build_conclusions(
        [doc], store_off, Settings(investable_gate=False), _RetailMarket(), "run-off", alias)
    assert all(s.investable for s in signals_off)
    assert any(c["tickers"] for c in conclusions_off), "关掉闸门后应恢复旧行为"
    store_off.close()


def test_non_investable_signal_never_enters_bullish_table(tmp_path):
    """报告级：非投资信号只出现在「非投资信号」区，受益表里没有它。"""
    _, conclusions = build_conclusions(
        [_generic_doc()], Store(tmp_path / "r.db"), Settings(), _RetailMarket(),
        "run-r", load_theme_alias())
    result = RunResult(run_id="run-r", window_start="2026-10-20", window_end="2026-10-21",
                       conclusions=conclusions)
    result.stats = _stats(docs=1, signals=len(conclusions))
    signal_id = conclusions[0]["signal_id"]

    md = render_markdown(result)
    assert signal_id in md, "非投资信号必须留痕"
    assert "## 非投资信号" in md
    assert "## 受益个股" in md and "今日无实质投资信号" in md
    assert "601888" not in md, "非投资信号不得展开任何个股"
    assert not re.search(r"^\| 1 \| 601888", md, re.M)

    payload = json.loads(render_json(result))
    assert payload["sections"]["非投资信号"] == [signal_id]
    assert payload["sections"]["受益个股"] == []
    assert payload["conclusions"][0]["investable"] is False

    html = render_html(result)
    assert "非投资信号" in html and signal_id in html
    assert "601888" not in html


# ---- 改动 2：深读结论反向分区 ----


def test_substantive_transmission_mapping_is_deterministic():
    """否定表述先判：「无直接传导 / 间接情绪面 / 实质业务传导有限」里都含「直接」「间接」。"""
    no_substance = ["中性", "无实质传导", "无直接传导", "间接情绪面", "间接、无实质",
                    "实质业务传导有限", "关联度低", "影响有限", "缺乏具体措施", "仅情绪面"]
    for text in no_substance:
        assert substantive_transmission(text) is False, f"应判无实质传导：{text}"
    for text in ["直接传导", "政策直接利好", "间接传导", "以间接方式受益"]:
        assert substantive_transmission(text) is True, f"应判实质传导：{text}"
    assert substantive_transmission("") is None
    assert substantive_transmission("   ") is None


def _event_section_of(conclusions, transmissions):
    events = cluster_events(conclusions)
    deepdives = [DeepDive(
        ticker_code="601888", ticker_name="中国中免", policy_title="政策",
        policy_summary="摘要", policy_quote="引用", policy_url="https://www.gov.cn/a.htm",
        profile_business="免税零售", transmission=t, competition="竞争", impact="影响",
        risks=["风险"], sources=[{"label": "政策原文", "ref": "https://www.gov.cn/a.htm"}],
        event_id=events[0].event_id,
    ) for t in transmissions]
    verdicts = {}
    for d in deepdives:
        verdict = substantive_transmission(d.transmission)
        if verdict is not None:
            verdicts.setdefault(d.event_id, []).append(verdict)
    return _event_section(events[0], verdicts)


def _bullish_conclusion(signal_id: str = "doc-bull#1", doc_id: str = "doc-bull") -> dict:
    return {
        "signal_id": signal_id, "doc_id": doc_id, "doc_title": "加快发展碳市场",
        "summary": "加快全国碳市场建设", "direction": "利好", "strength": 4,
        "horizon": "中期(1–6月)", "novelty": "新增", "instruments": ["产业扶持"],
        "theme_tags": ["碳中和"], "confidence": 0.9, "quote": "加快全国碳市场建设",
        "quote_verified": True, "source_url": "https://www.gov.cn/a.htm",
        "publisher": "国务院", "org": "国务院", "meeting_type": "国务院常务会议",
        "published_at": "2026-10-21", "fetched_at": "2026-10-21T10:00:00+0800",
        "content_hash": "abc", "raw_hash": "def",
        "raw_snapshot_path": "data/snapshots/r/doc-bull.html",
        "model": "rules-v1", "prompt_version": "rules-v1",
        "investable": True, "investable_reason": "未命中泛经济样板表述",
        "matched_sectors": ["碳中和"],
        "tickers": [{
            "ticker_code": "601888", "ticker_name": "中国中免", "exchange": "sh",
            "board": "沪市主板", "market_cap": 993.4, "sector_name": "碳中和",
            "sector_source": "通达信概念", "why": "主题直接命中", "score": 87.0, "rank": 1,
            "mapping_path": "碳中和 → 碳中和(通达信概念) → 601888", "mapping_confidence": 1.0,
        }],
    }


def test_deepdive_verdict_decides_the_event_section():
    """验收第 2 条的分区单测：无实质 → 仅概念关联；直接/间接 → 受益个股。"""
    assert _event_section_of([_bullish_conclusion()], ["中性"]) == SECTION_CONCEPT_ONLY
    assert _event_section_of([_bullish_conclusion()], ["无实质传导"]) == SECTION_CONCEPT_ONLY
    assert _event_section_of([_bullish_conclusion()], ["实质业务传导有限"]) == SECTION_CONCEPT_ONLY
    assert _event_section_of([_bullish_conclusion()], ["直接传导"]) == SECTION_BULLISH
    assert _event_section_of([_bullish_conclusion()], ["间接传导"]) == SECTION_BULLISH
    # 没配 LLM（无深读）：保持现有行为，但不授予「受益」头衔
    assert _event_section_of([_bullish_conclusion()], []) == SECTION_CONCEPT_PENDING
    # 多数决：2 条无实质 + 1 条实质 → 降级
    assert _event_section_of([_bullish_conclusion()],
                             ["无实质传导", "中性", "直接传导"]) == SECTION_CONCEPT_ONLY


def test_all_non_investable_event_is_not_investable_section():
    c = _bullish_conclusion()
    c["investable"] = False
    c["tickers"] = []
    assert _event_section_of([c], ["直接传导"]) == SECTION_NON_INVESTABLE


# ---- 改动 3：md / html / json 三端分区一致 ----


def _stats(**kw) -> dict:
    base = {
        "docs": 1, "signals": 1, "with_tickers": 1, "ticker_rows": 1, "events": 2,
        "sources_total": 6, "sources_ok": 6, "sources_failed": 0, "fetch_failures": 0,
        "tongstock_calls": 0, "llm_enabled": False, "duplicates_skipped": 0,
        "out_of_window_docs": 0, "sources_no_update": [], "gate": {},
        "deepdive_count": 0, "min_strength_for_report": 3,
        "investable_gate": True, "non_investable_signals": 0, "pbc_related_docs": 0,
    }
    base.update(kw)
    return base


def _non_investable_conclusion() -> dict:
    return {
        "signal_id": "doc-generic#1", "doc_id": "doc-generic",
        "doc_title": "2026年亚太经合组织财长会在香港举行", "summary": APEC_TEXT[:40],
        "direction": "利好", "strength": 3, "horizon": "中期(1–6月)", "novelty": "加码",
        "instruments": ["产业扶持", "准入"], "theme_tags": ["新零售"], "confidence": 0.7,
        "quote": APEC_TEXT, "quote_verified": True,
        "source_url": "https://www.mof.gov.cn/a.htm", "publisher": "财政部", "org": "财政部",
        "meeting_type": "部长会议", "published_at": "2026-10-21",
        "fetched_at": "2026-10-21T16:27:01+0800", "content_hash": "gh", "raw_hash": "ij",
        "raw_snapshot_path": "data/snapshots/r/doc-generic.html",
        "model": "rules-v1", "prompt_version": "rules-v1",
        "investable": False, "investable_reason": "命中泛样板表述「扩大国内需求」且无具名产业词",
        "matched_sectors": [], "tickers": [],
    }


def _mixed_result() -> RunResult:
    """两个真信号事件 + 一个非投资信号：三端都渲染，逐项对账。"""
    bullish = _bullish_conclusion()
    concept = _bullish_conclusion(signal_id="doc-concept#1", doc_id="doc-concept")
    concept["doc_title"] = "推进跨境电商海外仓建设"
    concept["summary"] = "推进跨境电商海外仓建设"
    generic = _non_investable_conclusion()
    r = RunResult(run_id="r-mix", window_start="2026-10-20", window_end="2026-10-21",
                  conclusions=[bullish, concept, generic])
    r.events = cluster_events(r.conclusions)
    by_doc = {e.doc_ids[0]: e for e in r.events}
    r.deepdives = [
        DeepDive(ticker_code="601888", ticker_name="中国中免", policy_title="政策",
                 policy_summary="摘要", policy_quote="引用", policy_url="https://www.gov.cn/a.htm",
                 profile_business="免税零售", transmission="政策直接利好", competition="竞争",
                 impact="影响", risks=["风险"],
                 sources=[{"label": "政策原文", "ref": "https://www.gov.cn/a.htm"}],
                 event_id=by_doc["doc-bull"].event_id),
        DeepDive(ticker_code="600415", ticker_name="小商品城", policy_title="政策",
                 policy_summary="摘要", policy_quote="引用", policy_url="https://www.mofcom.gov.cn/b.htm",
                 profile_business="小商品批发", transmission="无实质传导", competition="竞争",
                 impact="影响", risks=["风险"],
                 sources=[{"label": "政策原文", "ref": "https://www.mofcom.gov.cn/b.htm"}],
                 event_id=by_doc["doc-concept"].event_id),
    ]
    r.stats = _stats(docs=3, signals=3, ticker_rows=2, events=3,
                     non_investable_signals=1, deepdive_count=2)
    return r


def _md_sections(md: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = ""
    for line in md.splitlines():
        if line.startswith("- 事件ID：`"):
            current = line.split("`")[1]
        elif line.startswith("- 分区：") and current:
            sections[current] = line[len("- 分区："):].strip()
    return sections


def _md_block(md: str, heading: str) -> str:
    """某个二级标题到下一个二级标题之间的正文。"""
    if heading not in md:
        return ""
    return re.split(r"\n## ", md.split(heading, 1)[1], maxsplit=1)[0]


def _html_sections(html: str) -> dict[str, str]:
    return {k: v.strip() for k, v in
            re.findall(r"事件ID <code>(evt-[\w]+)</code> · 分区：([^·<]+)", html)}


def test_three_end_sections_are_consistent():
    r = _mixed_result()
    md, payload, html = render_markdown(r), json.loads(render_json(r)), render_html(r)

    md_sections = _md_sections(md)
    json_sections = {e["event_id"]: e["section"] for e in payload["events"]}
    html_sections = _html_sections(html)

    # md / html 按「事件」渲染分区；非投资信号在两端是信号级一行一条（见下），
    # 所以事件块只对 受益个股 / 仅概念关联 / 概念关联（未做实质传导判定） 对账
    block_sections = {k: v for k, v in json_sections.items() if v != SECTION_NON_INVESTABLE}
    assert md_sections and md_sections == block_sections == html_sections, \
        f"三端分区不一致 md={md_sections} json={json_sections} html={html_sections}"

    event_ids = [e.event_id for e in r.events]
    sections_by_event = {e["event_id"]: e["section"] for e in payload["events"]}
    for key, expected in (
            ("受益个股", [i for i in event_ids if sections_by_event[i] == SECTION_BULLISH]),
            ("仅概念关联", [i for i in event_ids if sections_by_event[i] == SECTION_CONCEPT_ONLY]),
            ("概念关联（未做实质传导判定）",
             [i for i in event_ids if sections_by_event[i] == SECTION_CONCEPT_PENDING])):
        assert payload["sections"][key] == expected

    concept_event = next(e for e in r.events if e.doc_ids[0] == "doc-concept")
    bullish_event = next(e for e in r.events if e.doc_ids[0] == "doc-bull")

    # md：降级事件只出现在「仅概念关联」区，受益区里没有它
    md_main, md_concept = _md_block(md, "## 受益个股"), _md_block(md, "## 仅概念关联")
    assert bullish_event.event_id in md_main and concept_event.event_id not in md_main
    assert concept_event.event_id in md_concept and concept_event.title in md_concept

    # html：同样两块，分区标注与 md 一致
    html_main = html.split("<h2>受益个股</h2>", 1)[1].split("<h2>", 1)[0]
    html_concept = html.split("<h2>仅概念关联", 1)[1].split("</h2>", 1)[1].split("<h2>", 1)[0]
    assert bullish_event.event_id in html_main and concept_event.event_id not in html_main
    assert concept_event.event_id in html_concept

    # 非投资信号：三端同一条、同一判定依据，且不在受益区
    generic_id = "doc-generic#1"
    reason = "命中泛样板表述「扩大国内需求」且无具名产业词"
    assert payload["sections"]["非投资信号"] == [generic_id]
    # json 的事件级 section 与信号级清单必须自洽
    non_inv_events = [e for e in payload["events"] if e["section"] == SECTION_NON_INVESTABLE]
    assert [sid for e in non_inv_events for sid in e["signal_ids"]] == [generic_id]
    assert generic_id in _md_block(md, "## 非投资信号") and reason in _md_block(md, "## 非投资信号")
    assert generic_id not in md_main
    html_non_inv = html.split("非投资信号（泛经济样板表述", 1)[1]
    assert generic_id in html_non_inv and reason in html_non_inv
    assert generic_id not in html_main
    assert reason in md and reason in html


def test_empty_bullish_day_is_stated_on_all_three_ends():
    r = _mixed_result()
    # 把两个真信号事件也降级 / 判非投资 → 受益区必须明确「今日无实质投资信号」
    for d in r.deepdives:
        d.transmission = "无实质传导"
    md, payload, html = render_markdown(r), json.loads(render_json(r)), render_html(r)
    assert "今日无实质投资信号" in md
    assert "今日无实质投资信号" in html
    assert payload["sections"]["受益个股"] == []
    assert payload["sections"]["仅概念关联"], "降级事件仍要留痕"
    assert all(e["section"] != SECTION_BULLISH for e in payload["events"])
