"""pipeline 端到端离线用例。

HTTP 层用冻结的**真实** gov.cn 页面，tongstock 层用一个最小数据客户端，
抓取/去重/抽取/映射/打分/落库/报告整条链路都是真实代码。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from policypulse.config import Settings, default_paths, load_theme_alias
from policypulse.http import Fetcher
from policypulse.pipeline import build_conclusions, doc_id_for, fetch_documents
from policypulse.report import render_markdown
from policypulse.store import Store
from policypulse.tongstock import CONCEPT_FILE, INDUSTRY_FILE, Ticker

ARTICLE = (Path(__file__).parent / "fixtures" / "gov_article.html").read_text(encoding="utf-8")
ARTICLE_URL = "https://www.gov.cn/zhengce/202610/content_7082820.htm"


class _FrozenFetcher:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.warnings: list[str] = []
        self.requests: list[str] = []

    def get_text(self, url: str) -> str:
        self.requests.append(url)
        return self.pages[url]

    def get_json(self, url: str):
        return json.loads(self.pages[url])

    def configure_rate_limit(self, url: str, seconds: float) -> None:
        pass


class _MiniMarket:
    """最小可用数据集：只替换 TDX 读取，映射/过滤/打分是真实代码。"""

    calls = 0

    def sectors(self, block_file: str = CONCEPT_FILE):
        return {"碳中和": 4} if block_file == CONCEPT_FILE else {"环境保护": 4}

    def members(self, sector: str, block_file: str = CONCEPT_FILE):
        return ["600989", "300124", "000998", "002001"]

    def tickers(self):
        return {
            "600989": Ticker("600989", "宝丰能源", "sh", "沪市主板", 1705.0),
            "300124": Ticker("300124", "汇川技术", "sz", "创业板", 1220.0),
            "000998": Ticker("000998", "ST农科", "sz", "深市主板", 80.0),
            "002001": Ticker("002001", "小市值", "sz", "深市主板", 12.0),
        }


def _specs():
    from policypulse.config import SourceSpec

    return [SourceSpec(id="gov_zhengce", name="最新政策", org="国务院", tier="中央",
                       adapter="gov_json_feed", url="https://www.gov.cn/zhengce/zuixin/",
                       meeting_type_hint="政策文件", rate_limit_seconds=2.0,
                       extra={"feed_url": "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json"})]


def _feed():
    return json.dumps([{"TITLE": "中共中央 国务院关于发展新质生产力的意见",
                        "URL": ARTICLE_URL, "DOCRELPUBTIME": "2026-10-09"}],
                      ensure_ascii=False)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICYPULSE_HOME", str(tmp_path))
    paths = default_paths()
    (tmp_path / "snap").mkdir(exist_ok=True)
    return paths, tmp_path / "snap"


def _run_once(paths, snapshot_dir, store, dedupe=True):
    settings = Settings(cross_run_dedupe=dedupe)
    fetcher = _FrozenFetcher({
        "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json": _feed(),
        ARTICLE_URL: ARTICLE,
    })
    docs, failed, duplicates = fetch_documents(fetcher, _specs(), settings, "2026-10-06",
                                               "2026-10-19", snapshot_dir, "run-test", store)
    return docs, failed, duplicates


def test_end_to_end_offline(workspace):
    paths, snapshot_dir = workspace
    store = Store(paths.db)
    docs, failed, duplicates = _run_once(paths, snapshot_dir, store)

    assert not failed
    assert duplicates == 0
    assert len(docs) == 1
    doc = docs[0]
    assert doc.url == ARTICLE_URL
    assert Path(doc.snapshot_path).exists()
    assert doc.doc_id == doc_id_for(ARTICLE_URL)

    signals, conclusions = build_conclusions(docs, store, Settings(), _MiniMarket(),
                                             "run-test", load_theme_alias())
    assert signals, "真实政策全文应能抽出信号"
    assert all(s.quote_verified for s in signals), "quote 必须是原文子串"
    assert conclusions

    with_tickers = [c for c in conclusions if c["tickers"]]
    assert with_tickers, "新质生产力全文应能映射到碳中和板块"
    codes = [t["ticker_code"] for c in with_tickers for t in c["tickers"]]
    assert "600989" in codes
    assert "000998" not in codes, "ST 股必须被过滤"
    assert "002001" not in codes, "市值低于阈值的必须被过滤"

    md = render_markdown(type("R", (), {
        "run_id": "run-test", "window_start": "2026-10-06", "window_end": "2026-10-19",
        "docs": docs, "conclusions": conclusions, "failed_sources": [], "warnings": [],
        "duplicates_skipped": 0,
        "stats": {"docs": 1, "signals": len(signals), "with_tickers": len(with_tickers),
                  "ticker_rows": len(codes), "sources_total": 1, "sources_failed": 0,
                  "tongstock_calls": 0, "llm_enabled": False, "duplicates_skipped": 0,
                  "sources_no_update": [], "gate": {}, "min_strength_for_report": 3},
    })())
    assert ARTICLE_URL in md
    assert "600989" in md
    store.close()


def test_cross_run_dedupe_skips_already_seen_documents(workspace):
    """F：第二次 run 应命中跨 run 去重，而不是重复抽取。"""
    paths, snapshot_dir = workspace
    store = Store(paths.db)
    first_docs, _, first_dupes = _run_once(paths, snapshot_dir, store)
    assert len(first_docs) == 1 and first_dupes == 0
    build_conclusions(first_docs, store, Settings(), _MiniMarket(), "run-1", load_theme_alias())

    second_docs, _, second_dupes = _run_once(paths, snapshot_dir, store)
    assert second_docs == []
    assert second_dupes == 1
    store.close()


def test_dedupe_can_be_disabled(workspace):
    paths, snapshot_dir = workspace
    store = Store(paths.db)
    docs, _, _ = _run_once(paths, snapshot_dir, store)
    build_conclusions(docs, store, Settings(), _MiniMarket(), "run-1", load_theme_alias())

    docs2, _, dupes = _run_once(paths, snapshot_dir, store, dedupe=False)
    assert len(docs2) == 1 and dupes == 0
    store.close()


def test_out_of_window_documents_are_not_fetched(workspace):
    """第 1 点：窗口内没有更新 = 不抓。

    旧实现会「退回抓入口页最近 3 条」，把 10 月的稿子塞进 1 月的日报；
    现在必须是空结果 + 明确记录「该源窗口内无更新」。
    """
    paths, snapshot_dir = workspace
    store = Store(paths.db)
    settings = Settings()
    fetcher = _FrozenFetcher({
        "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json": _feed(),
        ARTICLE_URL: ARTICLE,
    })
    diag: dict = {}
    docs, failed, dupes = fetch_documents(fetcher, _specs(), settings, "2026-01-01",
                                          "2026-01-02", snapshot_dir, "run-test", store, diag)
    assert docs == [], "窗口外的稿子一条都不能进产物"
    assert failed == [] and dupes == 0, "源本身是可用的，只是窗口内没有更新"
    assert diag["discovered"] == ["gov_zhengce"]
    assert diag["no_update"] == ["gov_zhengce"]
    store.close()
