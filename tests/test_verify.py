import pytest

from policypulse.http import Fetcher
from policypulse.verify import STATUS_CHANGED, STATUS_GONE, STATUS_OK, render_verify, verify_documents


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
    return {"doc_id": doc_id, "url": url, "content_hash": hash_, "raw_hash": hash_}


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
