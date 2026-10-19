import pytest

from pathlib import Path

from policypulse.http import Fetcher
from policypulse.verify import (
    OK_STATUSES,
    STATUS_CHANGED,
    STATUS_GONE,
    STATUS_LOCAL_MISSING,
    STATUS_LOCAL_OK,
    STATUS_OK,
    render_verify,
    verify_documents,
)


class _StubFetcher:
    def __init__(self, mapping):
        self.mapping = mapping

    def get_text(self, url):
        if url in self.mapping:
            return self.mapping[url]
        from policypulse.http import FetchError

        raise FetchError(f"HTTP 404 for {url}")

    def close(self):
        pass


class _StubStore:
    def __init__(self, docs):
        self.docs = docs
        self.logged = []

    def documents_for_run(self, run_id):
        return self.docs

    def document(self, doc_id):
        return next((d for d in self.docs if d["doc_id"] == doc_id), None)

    def log_verify(self, doc_id, url, status, old, new):
        self.logged.append((doc_id, status))


def _doc(doc_id, url, hash_):
    return {"doc_id": doc_id, "url": url, "content_hash": hash_, "raw_hash": hash_,
            "source_id": None, "raw_snapshot_path": None}


def _local_doc(doc_id, url, hash_, snapshot: Path):
    return {"doc_id": doc_id, "url": url, "content_hash": hash_, "raw_hash": hash_,
            "source_id": "pbc_inbound", "raw_snapshot_path": str(snapshot)}


def test_verify_detects_identical_and_changed_content():
    from policypulse.http import content_hash

    body_same = "同一份原文"
    docs = [_doc("d1", "https://a", content_hash(body_same)),
            _doc("d2", "https://b", content_hash("旧版本")),
            _doc("d3", "https://gone", "x")]
    fetcher = _StubFetcher({"https://a": body_same, "https://b": "新版本"})
    store = _StubStore(docs)
    results = verify_documents(store, doc_ids=["d1", "d2", "d3"], fetcher=fetcher)
    status = {r.doc_id: r.status for r in results}
    assert status["d1"] == STATUS_OK
    assert status["d2"] == STATUS_CHANGED
    assert status["d3"] == STATUS_GONE
    assert len(store.logged) == 3


def test_local_inbound_documents_are_verified_against_the_local_original(tmp_path):
    """源站 robots 禁止抓取时，不能去重抓；与本地原件复算指纹，且不算「链接失效」。"""
    original = tmp_path / "doc.html"
    body = "<html><body><p>中国人民银行开展了公开市场业务逆回购操作。</p></body></html>"
    original.write_text(body, encoding="utf-8")
    from policypulse.http import content_hash
    from policypulse.text import html_to_text

    doc = _local_doc("d-inbound", "http://www.pbc.gov.cn/x/index.html",
                     content_hash(html_to_text(body)), original)
    store = _StubStore([doc])
    # fetcher 若被调用就是 bug：本地源不该联网
    results = verify_documents(store, doc_ids=["d-inbound"], fetcher=_StubFetcher({}),
                               local_source_ids={"pbc_inbound"})
    assert results[0].status == STATUS_LOCAL_OK
    assert results[0].status in OK_STATUSES


def test_local_inbound_missing_original_is_reported_not_guessed():
    from policypulse.http import content_hash

    doc = _local_doc("d-gone", "http://www.pbc.gov.cn/x/index.html",
                     content_hash("原文"), __file__ + "/nope.html")
    results = verify_documents(_StubStore([doc]), doc_ids=["d-gone"], fetcher=_StubFetcher({}),
                               local_source_ids={"pbc_inbound"})
    assert results[0].status == STATUS_LOCAL_MISSING


def test_render_verify_table():
    from policypulse.verify import VerifyResult

    table = render_verify([VerifyResult("d1", "https://a", STATUS_OK, "h", "h")])
    assert STATUS_OK in table and "d1" in table


@pytest.mark.network
def test_verify_against_real_gov_page():
    docs = [_doc("d-real", "https://www.gov.cn/", "")]
    with Fetcher(min_interval=0) as fetcher:
        results = verify_documents(_StubStore(docs), doc_ids=["d-real"], fetcher=fetcher)
    assert results[0].status in (STATUS_OK, STATUS_CHANGED)
