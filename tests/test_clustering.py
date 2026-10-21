"""政策事件聚类：同文档/同发布会的信号合并，个股按代码去重。"""

from __future__ import annotations

from policypulse.clustering import cluster_events, merge_tickers

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
