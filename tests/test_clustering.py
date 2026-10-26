"""政策事件聚类：同文档/同发布会的信号合并，个股按代码去重。"""

from __future__ import annotations

from policypulse.clustering import apply_global_pick_cap, cluster_events, merge_tickers

BASE = {
    "doc_id": "doc-1",
    "doc_title": "国务院常务会议：部署加快培育低空经济",
    "summary": "加快培育低空经济新增长极",
    "org": "国务院",
    "meeting_type": "国务院常务会议",
    "published_at": "2026-10-19",
    "source_url": "https://www.gov.cn/a.htm",
    "raw_snapshot_path": "data/snapshots/r1/doc-1.html",
    "direction": "利好",
    "strength": 4,
    "confidence": 0.8,
    "signal_id": "doc-1#1",
    "tickers": [],
}


def _c(signal_id: str, **kw) -> dict:
    c = {**BASE, "signal_id": signal_id}
    c.update(kw)
    return c


def _t(code: str, score: float, **kw) -> dict:
    return {
        "ticker_code": code, "ticker_name": f"名称{code}", "exchange": "sh",
        "board": "主板", "market_cap": 500.0, "sector_name": "低空经济",
        "sector_source": "通达信概念", "why": "主题映射", "score": score, "rank": 1,
        "mapping_path": f"低空经济 → 低空经济(通达信概念) → {code}", "mapping_confidence": 1.0,
        "relevance": kw.get("relevance", 1.0),
    }


def test_one_doc_becomes_one_event_with_deduped_tickers():
    conclusions = [
        _c("doc-1#1", tickers=[_t("600001", 82.0), _t("600002", 75.0)]),
        _c("doc-1#2", tickers=[_t("600001", 80.0), _t("600003", 70.0)]),
    ]
    events = cluster_events(conclusions)
    assert len(events) == 1
    ev = events[0]
    assert len(ev.doc_ids) == 1 and len(ev.conclusions) == 2
    codes = [t["ticker_code"] for t in ev.tickers]
    # 同一只股只留一条，且保留得分最高的那条
    assert codes.count("600001") == 1
    assert set(codes) == {"600001", "600002", "600003"}
    assert next(t for t in ev.tickers if t["ticker_code"] == "600001")["score"] == 82.0
    # 去重后的个股要能追溯到它由哪几条信号推出
    assert ev.tickers[0]["signal_ids"] == ["doc-1#1", "doc-1#2"]


def test_same_org_same_day_similar_title_is_merged():
    a = _c("doc-1#1", doc_id="doc-1",
           doc_title="国务院常务会议：部署加快培育低空经济新增长极")
    b = _c("doc-2#1", doc_id="doc-2",
           doc_title="国务院常务会议：部署加快培育低空经济新增长极（全文）",
           source_url="https://www.gov.cn/b.htm")
    events = cluster_events([a, b])
    assert len(events) == 1, "同机构同日、标题高度相似 = 同一次发布"
    assert sorted(events[0].doc_ids) == ["doc-1", "doc-2"]


def test_different_day_or_org_stays_separate():
    a = _c("doc-1#1", doc_id="doc-1")
    b = _c("doc-2#1", doc_id="doc-2", published_at="2026-10-10")
    c = _c("doc-3#1", doc_id="doc-3", org="工业和信息化部")
    events = cluster_events([a, b, c])
    assert len(events) == 3


def test_unrelated_titles_are_not_merged():
    a = _c("doc-1#1", doc_id="doc-1", doc_title="国务院常务会议：部署加快培育低空经济")
    b = _c("doc-2#1", doc_id="doc-2",
           doc_title="国务院关于加强金融风险防控的通知")
    events = cluster_events([a, b])
    assert len(events) == 2


def test_merge_tickers_keeps_best_row_and_records_signals():
    rows = merge_tickers([
        _c("s1", tickers=[_t("600001", 60.0)]),
        _c("s2", tickers=[_t("600001", 90.0)]),
    ])
    assert len(rows) == 1
    assert rows[0]["score"] == 90.0
    assert rows[0]["signal_ids"] == ["s1", "s2"]


def test_events_sort_by_date_then_best_score():
    older = _c("doc-a#1", doc_id="doc-a", published_at="2026-10-10",
               tickers=[_t("600001", 50.0)])
    newer = _c("doc-b#1", doc_id="doc-b", published_at="2026-10-19",
               tickers=[_t("600002", 80.0)])
    events = cluster_events([newer, older])
    assert [e.published_at for e in events] == ["2026-10-19", "2026-10-10"]


def _many(code_prefix: str = "6000", n: int = 8):
    """n 只分数递增的个股，用于验证「先排序、再截断」。"""
    return [_t(f"{code_prefix}{i:02d}", float(i * 10)) for i in range(1, n + 1)]


def test_event_tickers_capped_after_score_sort():
    """事件级选股上限：去重后按 score 降序，只留前 max_picks 只（WAN-21）。"""
    events = cluster_events([_c("doc-1#1", tickers=_many())], max_picks=5)
    codes = [t["ticker_code"] for t in events[0].tickers]
    assert codes == ["600008", "600007", "600006", "600005", "600004"]
    assert [t["score"] for t in events[0].tickers] == [80.0, 70.0, 60.0, 50.0, 40.0]


def test_event_tickers_not_padded_when_fewer_than_cap():
    events = cluster_events(
        [_c("doc-1#1", tickers=[_t("600001", 70.0), _t("600002", 90.0)])], max_picks=5)
    assert [t["ticker_code"] for t in events[0].tickers] == ["600002", "600001"]


def test_event_tickers_without_limit_keep_all_rows():
    """不传上限时保持旧行为（供历史重算/单测使用）；线上链路一定传 settings.max_picks。"""
    events = cluster_events([_c("doc-1#1", tickers=_many())])
    assert len(events[0].tickers) == 8


def test_merge_tickers_limit_truncates_after_sort():
    rows = merge_tickers([
        _c("s1", tickers=[_t("600001", 10.0), _t("600002", 90.0), _t("600003", 50.0)]),
    ], 2)
    assert [r["ticker_code"] for r in rows] == ["600002", "600003"]


def _multi_events():
    """两个政策事件，分数交叉：doc-1 = 10/20/30/40，doc-2 = 15/25/35/45。"""
    return cluster_events([
        _c("doc-1#1", doc_id="doc-1", tickers=_many("6000", 4)),
        _c("doc-2#1", doc_id="doc-2", doc_title="另一条政策",
           published_at="2026-10-18",
           tickers=[_t(f"6001{i:02d}", 10.0 * i + 5) for i in range(1, 5)]),
    ], max_picks=5)


def test_global_cap_keeps_top_five_across_events():
    """整份日报跨事件只留 score 最高的 5 只，并回写到各事件。"""
    events = _multi_events()
    assert sum(len(e.tickers) for e in events) == 8, "前置：两个事件共 8 只"

    kept = apply_global_pick_cap(events, 5)

    assert kept == ["600104", "600004", "600103", "600003", "600102"]
    by_doc = {tuple(e.doc_ids): e for e in events}
    assert [t["ticker_code"] for t in by_doc[("doc-1",)].tickers] == ["600004", "600003"]
    assert [t["ticker_code"] for t in by_doc[("doc-2",)].tickers] == ["600104", "600103", "600102"]
    assert sum(len(e.tickers) for e in events) == 5, "回写后全日报合计不得超过上限"


def test_global_cap_zero_disables_global_truncation():
    """global_max_picks=0 = 关闭全局截断，退回「每事件各 max_picks 只」。"""
    events = _multi_events()
    assert apply_global_pick_cap(events, 0) == []
    assert sum(len(e.tickers) for e in events) == 8, "关闭时事件表不得被改动"


def test_global_cap_dedupes_same_code_keeping_highest_score():
    """跨事件同码只留一次，留 score 最高那次（不是最先出现那次）。"""
    low = _c("doc-1#1", doc_id="doc-1", tickers=[_t("600001", 10.0), _t("600002", 20.0)])
    high = _c("doc-2#1", doc_id="doc-2", doc_title="另一条政策", published_at="2026-10-18",
              tickers=[_t("600001", 90.0)])
    events = cluster_events([low, high], max_picks=5)

    kept = apply_global_pick_cap(events, 5)

    assert kept == ["600001", "600002"]
    rows = [t for e in events for t in e.tickers if t["ticker_code"] == "600001"]
    assert len(rows) == 1 and rows[0]["score"] == 90.0, "留下的必须是 score 最高那行"
    by_doc = {tuple(e.doc_ids): e for e in events}
    assert [t["ticker_code"] for t in by_doc[("doc-1",)].tickers] == ["600002"], \
        "被更高分事件抢走的 600001 不该留在 doc-1 表里"


def test_global_cap_tie_break_is_deterministic_by_code():
    """同分时按 ticker.code 升序决胜，结果不随输入顺序漂移。"""
    rows = [_t("600003", 50.0), _t("600001", 50.0), _t("600002", 50.0)]
    events = cluster_events([_c("doc-1#1", tickers=rows)], max_picks=5)
    # 事件内排序用同一个键：同分也按 code 升序（在全局截断之前先确认）
    assert [t["ticker_code"] for t in events[0].tickers] == ["600001", "600002", "600003"]
    assert apply_global_pick_cap(events, 2) == ["600001", "600002"]


def test_shared_row_sort_key_ignores_input_order():
    """同一批行换个输入顺序，全局选股结果必须一致。"""
    rows = [_t("600002", 50.0), _t("600001", 50.0), _t("600003", 50.0)]
    a = cluster_events([_c("doc-1#1", tickers=rows)], max_picks=5)
    b = cluster_events([_c("doc-1#1", tickers=list(reversed(rows)))], max_picks=5)
    assert apply_global_pick_cap(a, 2) == apply_global_pick_cap(b, 2) == ["600001", "600002"]
