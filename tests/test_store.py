from policypulse.store import Store


def test_run_and_document_roundtrip(tmp_path):
    store = Store(tmp_path / "t.db")
    store.start_run("r1", "2026-10-06", "2026-10-19", "score-v1")
    store.upsert_document({
        "doc_id": "d1", "run_id": "r1", "source_id": "gov_zhengce",
        "url": "https://www.gov.cn/a.htm", "canonical_url": "https://www.gov.cn/a.htm",
        "title": "标题", "publisher": "国务院", "org": "国务院", "source_tier": "中央",
        "meeting_type": "国务院常务会议", "published_at": "2026-10-09",
        "fetched_at": "2026-10-19T10:00:00+0800", "content_hash": "h1",
        "raw_snapshot_path": "/tmp/d1.html", "duplicate_of": None,
    })
    docs = store.documents_for_run("r1")
    assert len(docs) == 1
    assert docs[0]["url"] == "https://www.gov.cn/a.htm"
    assert store.document_exists_hash("h1") == "d1"
    assert store.document_exists_hash("nope") is None
    store.close()


def test_signal_ticker_relations(tmp_path):
    from policypulse.extract import Signal

    store = Store(tmp_path / "t2.db")
    store.upsert_signal(Signal(signal_id="d1#1", doc_id="d1", quote="原文片段",
                               instruments=["财政"], theme_tags=["低空经济"]), "r1")
    store.upsert_ticker("600001", "测试A", "sh", "沪市主板", 500.0)
    store.upsert_sector("低空经济", "通达信概念", 3)
    store.upsert_sector_member("低空经济", "通达信概念", "600001")
    store.upsert_signal_ticker("d1#1", "600001", "理由", 80.0, 1, "p", 1.0,
                               "低空经济", "通达信概念")

    rows = store.tickers_for_signal("d1#1")
    assert len(rows) == 1
    assert rows[0]["name"] == "测试A"
    assert rows[0]["market_cap_yi"] == 500.0
    assert store.signal("d1#1")["quote"] == "原文片段"
    store.close()


def test_verify_log(tmp_path):
    store = Store(tmp_path / "t3.db")
    store.log_verify("d1", "https://x", "一致", "h1", "h1")
    row = store.conn.execute("SELECT * FROM verify_log").fetchone()
    assert row["status"] == "一致"
    store.close()
