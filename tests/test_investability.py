"""信号级可投资性闸门（改动 1）与深读结论反向分区（改动 2 / 3）的离线单测。

对应 `docs/iteration-prompt-001.md` 验收第 1、2 条 + WAN-15 语义判定改造：
- 样板话信号 → `investable=False`、不映射板块、不展开个股、不进「受益」表；
- 具名产业信号 → 保留；
- 受益主体明确性（LLM 语义判定）「无受益链条」→ 非投资；「有产业方向但无具体主体」
  /「有明确受益主体」→ 交回下游；
- 深读分区只认结构化 `substantive` 枚举（是/否/不确定），「否」/打平 →「仅概念关联」，
  明确「是」→「受益个股」（且是票必须 > 全部深读数的一半，O2），无可判定票 →
  「待判定：无实质传导证据」（单列，不进受益区，R1）；
- md / html / json 三端分区一致。

全程离线：不碰网络、不碰 tongstock、不调 LLM。
"""

from __future__ import annotations

import json
import re

from policypulse.clustering import cluster_events
from policypulse.config import Settings, load_theme_alias
from policypulse.deepdive import (
    SUBSTANTIVE_NO,
    SUBSTANTIVE_UNCERTAIN,
    SUBSTANTIVE_YES,
    DeepDive,
    substantive_verdict,
)
from policypulse.extract import Signal
from policypulse.investability import (
    GENERIC_POLICY_MARKERS,
    GENERIC_THEME_ALIASES,
    SPECIFICITY_DIRECTIONAL,
    SPECIFICITY_NAMED,
    SPECIFICITY_NONE,
    assess_investable,
    assess_signal,
    assess_text,
    specificity_enum_violation,
    theme_evidence,
)
from policypulse.pipeline import DocRecord, RunResult, build_conclusions
from policypulse.report import (
    SECTION_BULLISH,
    SECTION_CONCEPT_ONLY,
    SECTION_CONCEPT_PENDING,
    SECTION_LABELS,
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
    non_inv = _md_block(md, "## " + SECTION_LABELS[SECTION_NON_INVESTABLE])
    assert conclusions[0]["summary"][:40] in non_inv, "非投资信号必须留痕"
    assert signal_id not in md, "内部信号 ID 不进产物（JSON 里有）"
    assert "## 受益个股" in md and "本期暂无明确受益个股" in md
    assert "601888" not in md, "非投资信号不得展开任何个股"
    assert not re.search(r"^\| 1 \| 601888", md, re.M)

    payload = json.loads(render_json(result))
    assert payload["sections"]["非投资信号"] == [signal_id]
    assert payload["sections"]["受益个股"] == []
    assert payload["conclusions"][0]["investable"] is False

    html = render_html(result)
    html_non_inv = _html_blocks(html)["noninv"]
    assert conclusions[0]["summary"][:40] in html_non_inv
    assert signal_id not in html
    assert "601888" not in html


# ---- 改动 2：深读结论反向分区 ----


def test_substantive_verdict_consumes_structured_enum_only():
    """WAN-15：分区判定只认 LLM 的结构化 substantive 枚举，不做自由文本匹配。

    旧实现扫 transmission 文本（含「间接」→ 实质），中—东联委会那批
    「间接 + 影响有限」的深读被放行成受益——现在语义判定在 LLM 侧完成，
    代码只消费「是 / 否 / 不确定」三个枚举值。
    """
    assert substantive_verdict(SUBSTANTIVE_YES) is True
    assert substantive_verdict(SUBSTANTIVE_NO) is False
    # 未给出 / 不可识别 → None（fail-closed：不升级为受益）
    for value in ("", "   ", SUBSTANTIVE_UNCERTAIN, "直接影响", "间接传导", "yes", "否（影响有限）"):
        assert substantive_verdict(value) is None, f"不可识别的值不得被猜成实质：{value!r}"


def _event_section_of(conclusions, deepdive_specs):
    """deepdive_specs: [(transmission, impact, substantive)] → 事件分区。

    分母按全部深读条数计（O2：「不确定」/缺失计入分母、视为不抵升级）。
    """
    events = cluster_events(conclusions)
    deepdives = [DeepDive(
        ticker_code="601888", ticker_name="中国中免", policy_title="政策",
        policy_summary="摘要", policy_quote="引用", policy_url="https://www.gov.cn/a.htm",
        profile_business="免税零售", transmission=t, competition="竞争", impact=impact,
        substantive=substantive,
        risks=["风险"], sources=[{"label": "政策原文", "ref": "https://www.gov.cn/a.htm"}],
        event_id=events[0].event_id,
    ) for t, impact, substantive in deepdive_specs]
    verdicts = {}
    for d in deepdives:
        verdict = substantive_verdict(d.substantive)
        if verdict is not None:
            verdicts.setdefault(d.event_id, []).append(verdict)
    totals = {events[0].event_id: len(deepdives)}
    return _event_section(events[0], verdicts, totals)


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
    """验收第 2 条的分区单测：实质判定「否」→ 仅概念关联；明确「是」→ 受益个股。"""
    assert _event_section_of([_bullish_conclusion()],
                             [("中性", "无具体落点", SUBSTANTIVE_NO)]) == SECTION_CONCEPT_ONLY
    assert _event_section_of([_bullish_conclusion()],
                             [("直接", "无实质传导", SUBSTANTIVE_NO)]) == SECTION_CONCEPT_ONLY
    assert _event_section_of([_bullish_conclusion()],
                             [("直接", "订单落地", SUBSTANTIVE_YES)]) == SECTION_BULLISH
    assert _event_section_of([_bullish_conclusion()],
                             [("间接", "构成间接情绪面利好，但无具体项目", SUBSTANTIVE_YES)]) == SECTION_BULLISH
    # 没配 LLM（无深读）：保持现有行为，但不授予「受益」头衔
    assert _event_section_of([_bullish_conclusion()], []) == SECTION_CONCEPT_PENDING
    # 多数决：2 条「否」+ 1 条「是」→ 降级
    assert _event_section_of([_bullish_conclusion()],
                             [("间接", "影响有限", SUBSTANTIVE_NO),
                              ("中性", "无具体措施", SUBSTANTIVE_NO),
                              ("直接", "明确订单", SUBSTANTIVE_YES)]) == SECTION_CONCEPT_ONLY
    # 平局（1「是」 vs 1「否」）也降级：宁可空着也不编
    assert _event_section_of([_bullish_conclusion()],
                             [("直接", "明确订单", SUBSTANTIVE_YES),
                              ("直接", "无具体项目指向，影响有限", SUBSTANTIVE_NO)]) == SECTION_CONCEPT_ONLY
    # 没有可判定票（全部「不确定」/缺失）→ 待判定单列（R1），绝不被 transmission 文本猜成升级依据
    assert _event_section_of([_bullish_conclusion()],
                             [("直接", "尚在评估", SUBSTANTIVE_UNCERTAIN)]) == SECTION_CONCEPT_PENDING
    assert _event_section_of([_bullish_conclusion()],
                             [("直接", "尚在评估", "")]) == SECTION_CONCEPT_PENDING
    # O2：「不确定」计入分母——1 是 + 1 不确定（1 > 1 不成立）同样不升级
    assert _event_section_of([_bullish_conclusion()],
                             [("直接", "明确订单", SUBSTANTIVE_YES),
                              ("间接", "待观察", SUBSTANTIVE_UNCERTAIN)]) == SECTION_CONCEPT_ONLY


def test_yes_votes_must_exceed_half_of_all_deepdives():
    """O2 表决口径：是票必须 > 全部深读数的一半，「不确定」/缺失计入分母、不给升级让位。"""
    # 1 是 + 2 不确定 → 1 > 1.5 不成立 → 仅概念关联（核心用例）
    assert _event_section_of([_bullish_conclusion()],
                             [("直接", "明确订单", SUBSTANTIVE_YES),
                              ("间接", "尚在评估", SUBSTANTIVE_UNCERTAIN),
                              ("间接", "尚在评估", SUBSTANTIVE_UNCERTAIN)]) == SECTION_CONCEPT_ONLY
    # 1 是 + 1 否 + 1 不确定 → 1 > 1.5 不成立 → 仅概念关联
    assert _event_section_of([_bullish_conclusion()],
                             [("直接", "明确订单", SUBSTANTIVE_YES),
                              ("间接", "无具体项目", SUBSTANTIVE_NO),
                              ("间接", "尚在评估", SUBSTANTIVE_UNCERTAIN)]) == SECTION_CONCEPT_ONLY
    # 2 是 + 1 不确定 → 2 > 1.5 成立 → 照常升级（防焊死）
    assert _event_section_of([_bullish_conclusion()],
                             [("直接", "明确订单", SUBSTANTIVE_YES),
                              ("直接", "订单落地", SUBSTANTIVE_YES),
                              ("间接", "尚在评估", SUBSTANTIVE_UNCERTAIN)]) == SECTION_BULLISH
    # 3 张全「是」→ 全票升级
    assert _event_section_of([_bullish_conclusion()],
                             [(t, "明确订单", SUBSTANTIVE_YES) for t in ("直接", "直接", "直接")]
                             ) == SECTION_BULLISH


def test_wan15_transmission_alone_never_upgrades():
    """WAN-15 根因回归：transmission 写「直接/间接」但 substantive 判「否」→ 必须降级。

    改前实现只扫 transmission 文本（含「间接」→ 实质），中—东联委会那批
    「间接 + 影响有限 / 无具体项目 / 无直接可验证」的深读被放行成「受益个股」。
    """
    # 中色股份式：间接 + 未提及具体项目 → 否 → 仅概念关联
    assert _event_section_of([_bullish_conclusion()],
                             [("间接", "未提及具体项目，传导有限", SUBSTANTIVE_NO)]) == SECTION_CONCEPT_ONLY
    # 怡亚通式：间接 + 无直接可验证影响 → 否
    assert _event_section_of([_bullish_conclusion()],
                             [("间接", "无直接可验证影响", SUBSTANTIVE_NO)]) == SECTION_CONCEPT_ONLY
    # 中工国际式：间接 + 无具体项目指向 → 否
    assert _event_section_of([_bullish_conclusion()],
                             [("间接", "无具体项目指向，影响有限", SUBSTANTIVE_NO)]) == SECTION_CONCEPT_ONLY
    # 即便 transmission 写「直接」，impact 否定性判断同样降级
    assert _event_section_of([_bullish_conclusion()],
                             [("直接", "难以验证的实际拉动，暂无披露", SUBSTANTIVE_NO)]) == SECTION_CONCEPT_ONLY


def test_deepdive_positive_verdicts_still_upgrade():
    """正向对照：真·明确订单 / 额度 / 试点的深读不能被焊死成永远不升级。"""
    positives = [
        ("直接", "已签订 50 亿元成套设备出口合同，订单本年度确认收入", SUBSTANTIVE_YES),
        ("直接", "获下达可再生能源补贴额度 12 亿元，明确由公司结算", SUBSTANTIVE_YES),
        ("间接", "纳入首批试点名单，预计将显著拉动出货量", SUBSTANTIVE_YES),
        ("直接", "政策直接受益，主营业务订单弹性放大", SUBSTANTIVE_YES),
    ]
    for spec in positives:
        assert _event_section_of([_bullish_conclusion()], [spec]) == SECTION_BULLISH, \
            f"正向深读不得被降级：{spec}"


def test_all_non_investable_event_is_not_investable_section():
    c = _bullish_conclusion()
    c["investable"] = False
    c["tickers"] = []
    assert _event_section_of([c], [("直接", "订单落地", SUBSTANTIVE_YES)]) == SECTION_NON_INVESTABLE


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
                 impact="订单弹性放大", substantive=SUBSTANTIVE_YES, risks=["风险"],
                 sources=[{"label": "政策原文", "ref": "https://www.gov.cn/a.htm"}],
                 event_id=by_doc["doc-bull"].event_id),
        DeepDive(ticker_code="600415", ticker_name="小商品城", policy_title="政策",
                 policy_summary="摘要", policy_quote="引用", policy_url="https://www.mofcom.gov.cn/b.htm",
                 profile_business="小商品批发", transmission="无实质传导", competition="竞争",
                 impact="无具体项目指向，影响有限", substantive=SUBSTANTIVE_NO, risks=["风险"],
                 sources=[{"label": "政策原文", "ref": "https://www.mofcom.gov.cn/b.htm"}],
                 event_id=by_doc["doc-concept"].event_id),
    ]
    r.stats = _stats(docs=3, signals=3, ticker_rows=2, events=3,
                     non_investable_signals=1, deepdive_count=2)
    return r


def _md_block(md: str, heading: str) -> str:
    """某个二级标题到下一个二级标题之间的正文。"""
    if heading not in md:
        return ""
    return re.split(r"\n## ", md.split(heading, 1)[1], maxsplit=1)[0]


def _md_blocks(md: str) -> dict[str, str]:
    """md 的二级标题 → 该块正文。"""
    blocks: dict[str, str] = {}
    for part in re.split(r"\n## ", md)[1:]:
        head, _, body = part.partition("\n")
        blocks[head.strip()] = body
    return blocks


def _html_blocks(html: str) -> dict[str, str]:
    """html 的内容区块：<section class='card' id='xxx'> … 到下一个区块。"""
    blocks: dict[str, str] = {}
    marks = list(re.finditer(r"<section class='card' id='([\w-]+)'>", html))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(html)
        blocks[m.group(1)] = html[m.start():end]
    return blocks


# 分区 → (md 二级标题, html 区块 id)：产物的分区展示与内部键名的唯一映射表
SECTION_BLOCKS = {
    SECTION_BULLISH: (SECTION_LABELS[SECTION_BULLISH], "bullish"),
    SECTION_CONCEPT_ONLY: (SECTION_LABELS[SECTION_CONCEPT_ONLY], "concept"),
    SECTION_CONCEPT_PENDING: (SECTION_LABELS[SECTION_CONCEPT_PENDING], "pending"),
}


def test_three_end_sections_are_consistent():
    r = _mixed_result()
    md, payload, html = render_markdown(r), json.loads(render_json(r)), render_html(r)
    md_blocks, html_blocks = _md_blocks(md), _html_blocks(html)

    json_sections = {e["event_id"]: e["section"] for e in payload["events"]}
    title_of = {e.event_id: e.title for e in r.events}

    # md / html 按「事件」渲染分区：每个事件只出现在它被判定的那个区里，
    # 三端（JSON section / md 标题块 / html 区块）必须对得上
    for eid, section in json_sections.items():
        if section == SECTION_NON_INVESTABLE:
            continue
        md_head, html_id = SECTION_BLOCKS[section]
        title = title_of[eid]
        assert title in md_blocks.get(md_head, ""), f"{title} 应出现在 md「{md_head}」区"
        assert title in html_blocks.get(html_id, ""), f"{title} 应出现在 html #{html_id} 区"
        for other_head, other_id in SECTION_BLOCKS.values():
            if other_head == md_head:
                continue
            assert title not in md_blocks.get(other_head, ""), \
                f"{title} 不该出现在 md「{other_head}」区"
            assert title not in html_blocks.get(other_id, ""), \
                f"{title} 不该出现在 html #{other_id} 区"

    event_ids = [e.event_id for e in r.events]
    sections_by_event = {e["event_id"]: e["section"] for e in payload["events"]}
    for key, expected in (
            ("受益个股", [i for i in event_ids if sections_by_event[i] == SECTION_BULLISH]),
            ("仅概念关联", [i for i in event_ids if sections_by_event[i] == SECTION_CONCEPT_ONLY]),
            (SECTION_CONCEPT_PENDING,
             [i for i in event_ids if sections_by_event[i] == SECTION_CONCEPT_PENDING])):
        assert payload["sections"][key] == expected
    # R1 老键兼容：改名单列后旧键仍发（同值），老解析方读旧键不破
    assert payload["sections"]["概念关联（未做实质传导判定）"] == \
        payload["sections"][SECTION_CONCEPT_PENDING]

    concept_event = next(e for e in r.events if e.doc_ids[0] == "doc-concept")
    bullish_event = next(e for e in r.events if e.doc_ids[0] == "doc-bull")

    # md：降级事件只出现在「主题关联」区，受益区里没有它
    md_main = _md_block(md, "## " + SECTION_LABELS[SECTION_BULLISH])
    md_concept = _md_block(md, "## " + SECTION_LABELS[SECTION_CONCEPT_ONLY])
    assert bullish_event.title in md_main and concept_event.title not in md_main
    assert concept_event.title in md_concept

    # html：同样两块，分区标注与 md 一致
    html_main = html_blocks["bullish"]
    html_concept = html_blocks["concept"]
    assert bullish_event.title in html_main and concept_event.title not in html_main
    assert concept_event.title in html_concept

    # 非投资信号：三端同一条，且不在受益区；判定依据（规则话术）只留 JSON
    generic_id = "doc-generic#1"
    reason = "命中泛样板表述「扩大国内需求」且无具名产业词"
    generic_summary = _non_investable_conclusion()["summary"][:40]
    assert payload["sections"]["非投资信号"] == [generic_id]
    # json 的事件级 section 与信号级清单必须自洽
    non_inv_events = [e for e in payload["events"] if e["section"] == SECTION_NON_INVESTABLE]
    assert [sid for e in non_inv_events for sid in e["signal_ids"]] == [generic_id]
    non_inv_md = _md_block(md, "## " + SECTION_LABELS[SECTION_NON_INVESTABLE])
    non_inv_html = html_blocks["noninv"]
    assert generic_summary in non_inv_md and generic_summary in non_inv_html
    assert reason not in md and reason not in html, "规则式判定依据是审计信息，只留 JSON"
    assert payload["conclusions"][2]["investable_reason"] == reason
    assert generic_id not in md_main and generic_id not in html_main


def test_empty_bullish_day_is_stated_on_all_three_ends():
    r = _mixed_result()
    # 把两个真信号事件也降级 / 判非投资 → 受益区必须明确「本期暂无明确受益个股」
    for d in r.deepdives:
        d.substantive = SUBSTANTIVE_NO
    md, payload, html = render_markdown(r), json.loads(render_json(r)), render_html(r)
    assert "本期暂无明确受益个股" in md
    assert "本期暂无明确受益个股" in html
    assert payload["sections"]["受益个股"] == []
    assert payload["sections"]["仅概念关联"], "降级事件仍要留痕"
    assert all(e["section"] != SECTION_BULLISH for e in payload["events"])


def test_no_vote_event_renders_under_pending_heading_not_bullish():
    """R1 文本级：无可判定票的事件必须单列「待确认」，不得再挂在「受益个股」标题下。

    md 与 html 都要按标题块断言（只查 JSON sections 不够——R1 的缺陷正是
    JSON 干净、md/html 却把「未判定」渲染在受益区标题下）。
    """
    r = _mixed_result()
    # 受益事件的深读改成「不确定」→ 无可判定票 → 落「待确认」；
    # 另一事件保持「否」→ 落「主题关联」；非投资信号不变——三区对照
    for d in r.deepdives:
        if d.ticker_code == "601888":
            d.substantive = SUBSTANTIVE_UNCERTAIN
    md, payload, html = render_markdown(r), json.loads(render_json(r)), render_html(r)
    bull_event = next(e for e in r.events if e.doc_ids[0] == "doc-bull")
    concept_event = next(e for e in r.events if e.doc_ids[0] == "doc-concept")

    # JSON：受益=0；无票事件进「待确认」（新旧键同值）；「否」事件进仅概念关联
    assert payload["sections"]["受益个股"] == []
    assert payload["sections"][SECTION_CONCEPT_PENDING] == [bull_event.event_id]
    assert payload["sections"]["概念关联（未做实质传导判定）"] == [bull_event.event_id]
    assert payload["sections"]["仅概念关联"] == [concept_event.event_id]

    md_main = _md_block(md, "## " + SECTION_LABELS[SECTION_BULLISH])
    md_pending = _md_block(md, "## " + SECTION_LABELS[SECTION_CONCEPT_PENDING])
    assert bull_event.title not in md_main and concept_event.title not in md_main
    assert "本期暂无明确受益个股" in md_main, "受益区为空要写明空日"
    assert bull_event.title in md_pending and concept_event.title not in md_pending

    # html 同样两块，口径与 md 一致
    html_blocks = _html_blocks(html)
    html_main = html_blocks["bullish"]
    html_pending = html_blocks["pending"]
    assert bull_event.title not in html_main and concept_event.title not in html_main
    assert "本期暂无明确受益个股" in html_main
    assert bull_event.title in html_pending
    assert concept_event.title not in html_pending


# ---- CR-1：主题证据校验（同义改写漏判 + 具名项目误杀，双向验证） ----


def _signal(text: str, themes: list[str]) -> Signal:
    return Signal(summary=text[:40], quote=text, theme_tags=themes)


def test_synonym_rewrite_of_generic_boilerplate_is_blocked():
    """CR-1 漏判侧：评审实测的三种同义改写必须判非投资（不再只认 18 条样板短语）。"""
    rewrites = [
        "会议进一步强调要大力提振内需，扩大居民消费，培育消费新业态新模式，巩固经济恢复发展基础。",
        "会议指出要释放消费潜力，扩内需稳增长，巩固经济回升向好基础。",
        "促进消费扩容提质，更好发挥消费对经济发展的基础性作用。",
    ]
    for text in rewrites:
        ok, reason = assess_signal(_signal(text, ["新零售"]))
        assert ok is False, f"泛需求同义改写必须判非投资：{text} → {reason}"
        assert "泛需求措辞" in reason and "具名证据" in reason
        # 老的样板短语路径也不受影响
        assert assess_signal(_signal(APEC_TEXT, ["新零售"]))[0] is False


def test_theme_evidence_accepts_named_evidence():
    """路线 (b) 的放行侧：业务词 / 具名 alias / 主题名本身都算具名证据。"""
    assert theme_evidence("促进消费扩容提质，加快零售数字化改造", ["新零售"])[0] is True
    assert theme_evidence("大力发展离岛免税，做大免税消费回流", ["免税概念"])[0] is True
    assert theme_evidence("会议提到新零售业态创新。", ["新零售"])[0] is True
    assert theme_evidence("会议强调扩大内需。", ["新零售"])[0] is False
    assert theme_evidence("会议强调扩大内需。", [])[0] is True  # 无主题不做证据降级


def test_generic_alias_set_is_not_a_named_channel():
    """泛需求措辞集只收 macro 措辞：具名渠道 alias 不得被当成泛化。"""
    assert "内需" in GENERIC_THEME_ALIASES and "消费新业态" in GENERIC_THEME_ALIASES
    assert "免税" not in GENERIC_THEME_ALIASES and "离岛免税" not in GENERIC_THEME_ALIASES


def test_concrete_program_words_are_not_killed():
    """CR-1 误杀侧：具名政策项目不得被样板词连坐（含评审给的合并反例）。"""
    cases = [
        ("大力发展首发经济，支持国际品牌在华设立首店。", ["新零售"]),
        ("加大城镇老旧小区改造力度，完善配套基础设施。", ["新型城镇"]),
        ("健全县域商业体系，推动物流配送下乡。", ["新零售"]),
        ("推动大规模设备更新和消费品以旧换新，中央财政按比例给予补贴。", ["新零售"]),
        ("着力扩大国内需求，提振消费，大力发展首发经济、加大老旧小区改造力度、健全县域商业体系。",
         ["新零售"]),
    ]
    for text, themes in cases:
        ok, reason = assess_signal(_signal(text, themes))
        assert ok is True, f"具名项目被误杀：{text} → {reason}"


def test_empty_theme_signal_falls_back_to_marker_rule():
    """设计边界：无主题标签时不做证据降级，仍交给样板词规则兜底。"""
    ok, reason = assess_signal(_signal("促进消费扩容提质，夯实发展基础。", []))
    assert ok is True and "未命中泛经济样板表述" in reason
    ok2, reason2 = assess_signal(_signal("会议强调扩大内需，坚定不移完成目标。", []))
    assert ok2 is False and "泛样板" in reason2


# ---- 受益主体明确性（语义判定，LLM 侧）→ 信号级闸门第 1 层 ----


def _spec_signal(text: str, specificity: str, themes: list[str] | None = None) -> Signal:
    return Signal(summary=text[:40], quote=text, theme_tags=themes or ["一带一路"],
                  beneficiary_specificity=specificity)


def test_specificity_none_blocks_even_without_boilerplate():
    """「无受益链条」→ 非投资：机制性双边会议稿没有泛样板词也照样拦。

    词表抓不住「结构性」非投资信号（中—东联委会式），语义判定在 LLM 侧完成，
    代码只消费枚举。反例文本刻意不含任何 GENERIC_POLICY_MARKERS。
    """
    text = ("中国—东帝汶经贸合作联合委员会第二次会议在北京举行。双方就加强经贸务实合作"
            "深入交换意见，一致同意推动两国经贸关系健康稳定发展。")
    for marker in GENERIC_POLICY_MARKERS:
        assert marker not in text, f"反例文本不得含样板词，否则测不出语义层：{marker}"
    ok, reason = assess_signal(_spec_signal(text, SPECIFICITY_NONE))
    assert ok is False, f"「无受益链条」必须判非投资：{reason}"
    assert "受益主体明确性" in reason and SPECIFICITY_NONE in reason
    # 同一段文本不带语义判定（规则抽取路径）→ 仍放行：证明是语义层在起作用
    ok_plain, _ = assess_signal(Signal(summary=text[:40], quote=text, theme_tags=["一带一路"]))
    assert ok_plain is True


def test_specificity_directional_and_named_do_not_block():
    """「有产业方向但无具体主体」「有明确受益主体」不被语义层拦下，交给下游。"""
    text = "会议提出推动跨境电商海外仓建设，支持企业建设共享式海外仓。"
    for spec in (SPECIFICITY_DIRECTIONAL, SPECIFICITY_NAMED):
        ok, reason = assess_signal(_spec_signal(text, spec))
        assert ok is True, f"语义层不得拦方向性/具名信号（{spec}）：{reason}"
    # 具名订单文本 + 明确受益主体：明确放行
    order = "中国交建与东帝汶政府签署 5 亿美元基础设施总承包合同，年内开工。"
    ok, _ = assess_signal(_spec_signal(order, SPECIFICITY_NAMED))
    assert ok is True


def test_specificity_gate_respects_switch_and_missing_value():
    """闸门关闭时语义层也关闭；缺省值（规则路径 / 老数据）不启用语义层。"""
    settings = Settings(investable_gate=False)
    text = "机制性会议，无具名受益主体。"
    assert assess_signal(_spec_signal(text, SPECIFICITY_NONE), settings)[0] is True
    # 缺省 beneficiary_specificity → 跳过语义层，走原有词表路径
    sig = Signal(summary=text[:40], quote=text, theme_tags=["一带一路"])
    assert assess_signal(sig)[0] is True


def test_non_enum_specificity_triggers_audit(tmp_path, monkeypatch):
    """O1：非空但非量表枚举的 specificity 必须出审计告警/计数（模型没守量表有痕迹）。"""
    # 单元：枚举值与缺省都不告警；非空非枚举必须返回告警文本
    for value in (SPECIFICITY_NAMED, SPECIFICITY_DIRECTIONAL, SPECIFICITY_NONE, "", None, "  "):
        assert specificity_enum_violation(value) is None, f"不应告警：{value!r}"
    note = specificity_enum_violation("有受益链条")
    assert note and "未守量表" in note and "有受益链条" in note

    # 管线级：LLM 回非枚举值 → build_conclusions 的 audit 收到计数、明细与拼好的告警
    from policypulse import llm as llm_mod

    bad_signal = [{**TIMOR_LLM_SIGNAL[0], "beneficiary_specificity": "有受益链条"}]
    monkeypatch.setattr(llm_mod, "llm_available", lambda: True)
    monkeypatch.setattr(llm_mod, "extract_with_llm",
                        lambda title, body, timeout=60.0: bad_signal)
    audit: dict = {}
    store = Store(tmp_path / "audit.db")
    build_conclusions([_timor_doc()], store, Settings(), _BeltRoadMarket(), "run-au",
                      load_theme_alias(), audit=audit)
    store.close()
    assert audit.get("enum_violations") == 1, f"非枚举值必须计数：{audit}"
    assert any("doc-timor000000001#llm1" in d for d in audit.get("details", []))
    warning = audit.get("warning", "")
    assert "模型未守量表" in warning and "有受益链条" in warning

    # 对照：全部守量表 → 零计数、无告警
    ok_signal = [TIMOR_LLM_SIGNAL[0]]
    monkeypatch.setattr(llm_mod, "extract_with_llm",
                        lambda title, body, timeout=60.0: ok_signal)
    audit_ok: dict = {}
    store_ok = Store(tmp_path / "audit-ok.db")
    build_conclusions([_timor_doc()], store_ok, Settings(), _BeltRoadMarket(), "run-au2",
                      load_theme_alias(), audit=audit_ok)
    store_ok.close()
    assert not audit_ok.get("enum_violations") and not audit_ok.get("warning")


# ---- WAN-15 端到端：中—东联委会稿必须落「仅概念关联」，受益个股=0 ----

TIMOR_BODY = (
    "中国—东帝汶经贸合作联合委员会第二次会议在北京举行。商务部部长助理与东帝汶经济"
    "协调部长共同主持会议。双方积极评价建交以来双边经贸关系发展，就加强贸易投资、"
    "基础设施、农业等领域务实合作深入交换意见。会议一致同意，双方将充分发挥联合委员"
    "会机制作用，推动两国经贸关系健康稳定发展，更好造福两国人民。"
)

TIMOR_LLM_SIGNAL = [{
    "summary": "中国—东帝汶经贸合作联合委员会第二次会议举行，推动双边经贸合作",
    "quote": "双方将充分发挥联合委员会机制作用，推动两国经贸关系健康稳定发展",
    "direction": "利好", "strength": 3, "horizon": "长期(>6月)", "novelty": "延续",
    "instruments": ["合作机制"], "theme_tags": ["一带一路"],
    "affected_regions": ["东帝汶"], "confidence": 0.7,
    # LLM 语义判定：机制性双边合作、无具名受益方和落点 → 有产业方向但无具体主体
    "beneficiary_specificity": SPECIFICITY_DIRECTIONAL,
}]


class _BeltRoadMarket:
    """一带一路概念最小数据集：证明闸门放行时个股本可展开（改动 1 的对照组）。"""

    def sectors(self, block_file: str = CONCEPT_FILE):
        return {"一带一路": 4} if block_file == CONCEPT_FILE else {"工程建筑": 4}

    def members(self, sector: str, block_file: str = CONCEPT_FILE):
        return ["601800", "002051", "000065"]

    def tickers(self):
        return {
            "601800": Ticker("601800", "中国交建", "sh", "沪市主板", 1500.0),
            "002051": Ticker("002051", "中工国际", "sz", "深市主板", 220.0),
            "000065": Ticker("000065", "北方国际", "sz", "深市主板", 180.0),
        }


class _TimorClient:
    """F10 画像桩：让深读模块有可引用的公司资料，从而真实产出深读结论。"""

    def f10_block(self, code, block):
        if block == "公司概况":
            return (f"│公司名称│某国际工程公司{code}│证券代码│{code}│\n"
                    "│通达信研究行业│工程建筑│证监会行业│土木工程建筑业│\n"
                    "│主营业务│国际工程承包、成套设备出口与境内工程承包│\n"
                    "│经营范围│货物进出口；技术培训；对外承包工程│\n")
        return "".join(f"{s}(行业) 40% 8%\n" for s in ("国际工程", "成套设备"))


def _timor_doc() -> DocRecord:
    return DocRecord(
        doc_id="doc-timor000000001", url="https://www.mofcom.gov.cn/timor.htm",
        title="中国—东帝汶经贸合作联合委员会第二次会议举行", org="商务部", tier="中央",
        source_id="mofcom_xwfb", meeting_type="部长会议", published_at="2026-10-22",
        fetched_at="2026-10-22T16:00:00+0800", content_hash="th", raw_hash="tr",
        snapshot_path="data/snapshots/run-t/doc-timor000000001.html",
        body=TIMOR_BODY,
    )


def test_wan15_timor_article_ends_in_concept_only_with_zero_bullish(tmp_path, monkeypatch):
    """WAN-15 回归主用例：中—东联委会稿 → 仅概念关联，受益个股=0。

    真实链路：LLM 抽取（beneficiary_specificity=有产业方向但无具体主体）→
    闸门放行、个股展开（一带一路概念）→ 3 条深读全部「间接 + 影响有限 / 无具体项目」
    + substantive=否 → report 反向分区降级「仅概念关联」。
    改前：分区只扫 transmission 文本（含「间接」→ 实质）→ 误标「受益个股」。
    """
    from policypulse import llm as llm_mod
    from policypulse.deepdive import build_deepdives

    monkeypatch.setattr(llm_mod, "llm_available", lambda: True)
    monkeypatch.setattr(llm_mod, "extract_with_llm",
                        lambda title, body, timeout=60.0: TIMOR_LLM_SIGNAL)

    store = Store(tmp_path / "wan15.db")
    signals, conclusions = build_conclusions(
        [_timor_doc()], store, Settings(), _BeltRoadMarket(), "run-w15", load_theme_alias())

    # 语义判定为「有产业方向但无具体主体」→ 闸门放行，个股本可展开（这一步改前也放行）
    assert signals and all(s.investable for s in signals), \
        f"方向性信号不得被语义层拦下：{[(s.summary, s.investable_reason) for s in signals]}"
    assert signals[0].beneficiary_specificity == SPECIFICITY_DIRECTIONAL
    with_tickers = [c for c in conclusions if c["tickers"]]
    assert with_tickers, "对照组：产业链条存在时概念板块本可展开个股"

    # 深读：3 条真实复现（间接 + 否定性 impact），substantive 全部「否」
    deepdive_payloads = {
        "601800": {"transmission": "间接", "competition": "在东帝汶无直接业务布局",
                   "impact": "未提及具体项目，传导有限", "substantive": "否"},
        "002051": {"transmission": "间接", "competition": "国际工程经验丰富但无当地订单",
                   "impact": "无直接可验证影响", "substantive": "否"},
        "000065": {"transmission": "间接", "competition": "区域性承包商，未涉及该合作",
                   "impact": "无具体项目指向，影响有限", "substantive": "否"},
    }

    def fake_llm_fn(policy_text, profile_text):
        code = next((c for c in deepdive_payloads if c in profile_text), "601800")
        payload = deepdive_payloads[code]
        return {**payload,
                "risks": ["合作项目落地不及预期"],
                "sources": [{"label": "政策原文", "ref": "https://www.mofcom.gov.cn/timor.htm"},
                            {"label": "公司公开资料", "ref": f"通达信F10:{code}"}]}

    events = cluster_events(conclusions)
    dives, _ = build_deepdives(events, _TimorClient(), Settings(), llm_fn=fake_llm_fn)
    assert len(dives) == 3 and all(d.substantive == "否" for d in dives)

    result = RunResult(run_id="run-w15", window_start="2026-10-21", window_end="2026-10-22",
                       docs=[_timor_doc()], signals=signals, conclusions=conclusions,
                       events=events, deepdives=dives)
    result.stats = _stats(docs=1, signals=len(signals), with_tickers=len(with_tickers),
                          ticker_rows=sum(len(c["tickers"]) for c in with_tickers),
                          events=len(events), deepdive_count=len(dives))

    payload = json.loads(render_json(result))
    assert payload["sections"]["受益个股"] == [], \
        f"中—东联委会稿不得进受益区：{payload['sections']}"
    assert events and payload["events"][0]["section"] == SECTION_CONCEPT_ONLY, \
        f"事件必须降级「仅概念关联」：{payload['events'][0]}"
    # JSON 深读块携带 substantive 判定，供外部审计
    assert all(d["substantive"] == "否" for d in payload["deepdives"])

    md = render_markdown(result)
    concept_head = "## " + SECTION_LABELS[SECTION_CONCEPT_ONLY]
    assert concept_head in md
    assert events[0].title in _md_block(md, concept_head)
    assert "## 受益个股" in md and "本期暂无明确受益个股" in md

    # R1 文本级：全「否」（0 张是票）的事件在 md/html 都不得出现在受益区标题下
    html = render_html(result)
    md_main = _md_block(md, "## " + SECTION_LABELS[SECTION_BULLISH])
    html_main = _html_blocks(html)["bullish"]
    assert events[0].title not in md_main, "无票事件不得挂在「受益个股」标题下"
    assert events[0].title not in html_main, "html 同口径"
    store.close()


def test_wan15_positive_control_order_signal_still_upgrades(tmp_path, monkeypatch):
    """正向对照（O3）：真·具名订单信号 + 深读判「是」→ 照常进「受益个股」。

    深读与回归样本同链——经 `build_deepdives` 合成（校验 / 清洗 / 挂 event_id 一致），
    不再直接构造 DeepDive 对象。
    """
    from policypulse import llm as llm_mod

    order_signal = [{
        "summary": "中国交建签署东帝汶基础设施总承包合同",
        "quote": "中国交建与东帝汶政府签署 5 亿美元基础设施总承包合同，年内开工",
        "direction": "利好", "strength": 5, "horizon": "中期(1–6月)", "novelty": "新增",
        "instruments": ["项目合同"], "theme_tags": ["一带一路"],
        "affected_regions": ["东帝汶"], "confidence": 0.9,
        "beneficiary_specificity": SPECIFICITY_NAMED,
    }]
    doc = _timor_doc()
    doc.body = TIMOR_BODY + "中国交建与东帝汶政府签署 5 亿美元基础设施总承包合同，年内开工。"
    monkeypatch.setattr(llm_mod, "llm_available", lambda: True)
    monkeypatch.setattr(llm_mod, "extract_with_llm",
                        lambda title, body, timeout=60.0: order_signal)

    store = Store(tmp_path / "wan15-pos.db")
    signals, conclusions = build_conclusions(
        [doc], store, Settings(), _BeltRoadMarket(), "run-pos", load_theme_alias())
    assert signals[0].investable is True and signals[0].beneficiary_specificity == SPECIFICITY_NAMED

    events = cluster_events(conclusions)
    # O3：正向样本与回归样本走同一条合成链——build_deepdives（校验/清洗/挂 event_id 同路）
    from policypulse.deepdive import build_deepdives

    def fake_llm_fn(policy_text, profile_text):
        return {"transmission": "直接", "competition": "主要基础设施承包商",
                "impact": "已签订 5 亿美元总承包合同，订单本年度陆续确认收入",
                "substantive": SUBSTANTIVE_YES, "risks": ["汇率波动"],
                "sources": [{"label": "政策原文", "ref": doc.url}]}

    deepdives, notes = build_deepdives(events, _TimorClient(), Settings(), llm_fn=fake_llm_fn)
    assert deepdives and all(d.substantive == SUBSTANTIVE_YES for d in deepdives), \
        f"正向深读必须经合成链产出且判定为「是」：{deepdives} / {notes}"

    result = RunResult(run_id="run-pos", window_start="2026-10-21", window_end="2026-10-22",
                       docs=[doc], signals=signals, conclusions=conclusions,
                       events=events, deepdives=deepdives)
    result.stats = _stats(docs=1, signals=1, events=1, deepdive_count=len(deepdives))

    payload = json.loads(render_json(result))
    assert payload["sections"]["受益个股"] == [events[0].event_id], \
        "具名订单 + 实质判定「是」必须进受益区"
    assert payload["events"][0]["section"] == SECTION_BULLISH

    # 文本级正向对照：事件确实渲染在「受益个股」标题块下
    md = render_markdown(result)
    assert events[0].title in _md_block(md, "## " + SECTION_LABELS[SECTION_BULLISH])
    html = render_html(result)
    assert events[0].title in _html_blocks(html)["bullish"]
    store.close()
