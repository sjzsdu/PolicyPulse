"""个股深度研读（可插拔模块）：没有溯源就不产出。"""

from __future__ import annotations

from policypulse.clustering import PolicyEvent
from policypulse.config import Settings
from policypulse.deepdive import (
    F10_REF_PREFIX,
    SKIP_INVALID,
    SKIP_NO_PROFILE,
    build_deepdives,
    validate,
)

POLICY_URL = "https://www.gov.cn/a.htm"
SNAPSHOT = "data/snapshots/r1/doc-1.html"


class _ClientWithF10:
    def __init__(self, business="通用飞机制造、汽车零部件", segments=None):
        self.business = business
        self.segments = segments or []

    def f10_block(self, code, block):
        if block == "公司概况":
            return (f"│公司名称│某公司│证券代码│{code}│\n"
                    f"│通达信研究行业│航空装备│证监会行业│铁路船舶航空航天│\n"
                    f"│主营业务│{self.business}│\n"
                    "│经营范围│货物进出口；技术培训│\n")
        return "".join(f"{s}(行业) 100 10%\n" for s in self.segments)


class _ClientNoF10:
    pass


def _event(tickers) -> PolicyEvent:
    return PolicyEvent(
        event_id="evt-1", title="国务院常务会议：部署低空经济", org="国务院",
        meeting_type="国务院常务会议", published_at="2026-10-19",
        source_url=POLICY_URL, snapshot_path=SNAPSHOT, doc_ids=["doc-1"],
        conclusions=[{"signal_id": "doc-1#1", "summary": "x"}],
        tickers=tickers,
    )


def _row(code="600001", score=88.0):
    return {"ticker_code": code, "ticker_name": "名称" + code, "score": score,
            "why": "主题映射", "sector_name": "低空经济", "market_cap": 500.0,
            "mapping_path": "低空经济 → 低空经济(通达信概念) → " + code,
            "mapping_confidence": 1.0}


def test_validate_accepts_only_known_sources():
    ok = validate({
        "transmission": "直接", "competition": "整机龙头", "impact": "订单弹性放大",
        "risks": ["订单不及预期"],
        "sources": [{"label": "政策原文", "ref": POLICY_URL},
                    {"label": "公司主业", "ref": F10_REF_PREFIX + "600001"}],
        "policy_url": POLICY_URL, "raw_snapshot_path": SNAPSHOT,
    }, "600001", Settings())
    assert ok and len(ok["sources"]) == 2


def test_validate_rejects_unsourced_conclusion():
    base = {"transmission": "直接", "competition": "x", "impact": "y"}
    # 来路不明的 ref（模型自己编的链接）→ 整段作废
    assert validate({**base, "sources": [{"label": "研报", "ref": "https://example.com/x"}],
                     "policy_url": POLICY_URL}, "600001", Settings()) is None
    # 没有 sources → 作废
    assert validate({**base, "sources": [], "policy_url": POLICY_URL},
                    "600001", Settings()) is None
    # 必填字段缺失 → 作废
    assert validate({"transmission": "", "competition": "x", "impact": "y",
                     "sources": [{"label": "政策原文", "ref": POLICY_URL}],
                     "policy_url": POLICY_URL}, "600001", Settings()) is None
    assert validate(None, "600001", Settings()) is None


def test_no_llm_key_skips_with_reason():
    dives, notes = build_deepdives([_event([_row()])], _ClientWithF10(), Settings(),
                                   llm_fn=lambda *_: None)
    assert dives == []
    assert notes and "跳过" in notes[0]


def test_missing_profile_skips_with_reason():
    dives, notes = build_deepdives([_event([_row()])], _ClientNoF10(), Settings(),
                                   llm_fn=lambda *_: {"transmission": "直接"})
    assert dives == []
    assert any(SKIP_NO_PROFILE in n for n in notes)


def test_llm_error_does_not_blow_up_the_report():
    def boom(*_):
        raise RuntimeError("timeout")

    dives, notes = build_deepdives([_event([_row()])], _ClientWithF10(), Settings(), llm_fn=boom)
    assert dives == []
    assert notes and "失败" in notes[0]


def test_valid_payload_becomes_a_deep_dive():
    payload = {"transmission": "直接", "competition": "通航整机主要供应商", "impact": "订单有望增厚",
               "risks": ["适航取证进度"],
               "sources": [{"label": "政策原文", "ref": POLICY_URL},
                           {"label": "公司公开资料", "ref": F10_REF_PREFIX + "600001"}]}
    dives, notes = build_deepdives([_event([_row()])], _ClientWithF10(), Settings(),
                                   llm_fn=lambda *_: payload)
    assert not notes
    assert len(dives) == 1
    d = dives[0]
    assert d.ticker_code == "600001"
    assert d.transmission == "直接"
    assert all(s["ref"] in (POLICY_URL, F10_REF_PREFIX + "600001") for s in d.sources)
    assert SKIP_INVALID not in "".join(notes)


def test_invalid_payload_is_dropped_not_reported():
    dives, notes = build_deepdives(
        [_event([_row()])], _ClientWithF10(), Settings(),
        llm_fn=lambda *_: {"transmission": "直接", "competition": "x", "impact": "y",
                           "sources": [{"label": "编的", "ref": "https://evil.example/x"}]})
    assert dives == []
    assert any(SKIP_INVALID in n for n in notes)
