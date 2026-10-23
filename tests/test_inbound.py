"""人工 inbound 源的离线测试。

这条路径存在的前提：pbc.gov.cn robots 全站禁止抓取，程序不能主动抓；但用户本人
下载的原稿可以入库。因此这里要钉住三件事——**不发网络请求**、**没有溯源 URL 就拒收**、
**published_at 从文件名/正文来而不是编一个**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from policypulse.config import Settings, SourceSpec, default_paths
from policypulse.pipeline import fetch_documents, is_pbc_related
from policypulse.sources.local_inbound import LocalInboundAdapter
from policypulse.store import Store

HTML = """<html><head><title>中国人民银行将推动实现退出存贷款基准利率-新华网</title>
<meta name="pubtime" content="2026-10-19">
<link rel="canonical" href="http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html">
</head><body><div class="article">
<p>2026年10月19日 中国人民银行行长潘功胜在2026金融街论坛年会开幕式上表示，
随着利率市场化改革推进，存贷款基准利率逐步被市场化的定价基准替代。下一步，中国人民银行
将推动这些领域的计息依据用市场化的利率指标来替代，实现退出金融机构存贷款基准利率。</p>
</div></body></html>"""

MARKDOWN = """原文: http://www.pbc.gov.cn/zhengcehuobisi/125207/125213/index.html

中国人民银行于今日以固定利率、数量招标方式开展了公开市场业务逆回购操作，
操作量为 3000 亿元，期限为 7 天，中标利率为 1.40%。
"""


class _NoNetworkFetcher:
    """任何网络调用都应该失败：inbound 源不该碰网络。"""

    warnings: list[str] = []

    def get_text(self, url: str) -> str:
        raise AssertionError(f"local_inbound 不该发起网络请求，却请求了 {url}")

    def get_json(self, url: str):
        raise AssertionError(f"local_inbound 不该发起网络请求，却请求了 {url}")

    def configure_rate_limit(self, url: str, seconds: float) -> None:
        pass


def _spec(directory: Path) -> SourceSpec:
    return SourceSpec(id="pbc_inbound", name="人工下载原稿入库", org="中国人民银行",
                      tier="部委", adapter="local_inbound",
                      url="http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html",
                      meeting_type_hint="会议",
                      extra={"inbound_dir": str(directory), "min_body_length": 80})


@pytest.fixture()
def inbound(tmp_path):
    directory = tmp_path / "inbound"
    directory.mkdir()
    return directory


def test_html_file_with_sidecar_url_and_filename_date(inbound):
    (inbound / "2026-10-19-退出存贷款基准利率.html").write_text(HTML, encoding="utf-8")
    (inbound / "2026-10-19-退出存贷款基准利率.url").write_text(
        "http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html\n", encoding="utf-8")

    adapter = LocalInboundAdapter()
    items = adapter.discover(_spec(inbound), object())

    assert len(items) == 1
    assert items[0].url == "http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html"
    assert items[0].published_at == "2026-10-19"
    assert items[0].snapshot_ext == ".html"
    assert "退出存贷款基准利率" in items[0].title


def test_markdown_marker_line_is_enough_for_traceability(inbound):
    (inbound / "2026-10-18-公开市场业务交易公告.md").write_text(MARKDOWN, encoding="utf-8")

    items = LocalInboundAdapter().discover(_spec(inbound), object())

    assert len(items) == 1
    assert items[0].url.startswith("http://www.pbc.gov.cn/")
    assert items[0].snapshot_ext == ".md"


def test_html_canonical_link_is_used_when_no_sidecar(inbound):
    (inbound / "公告.html").write_text(HTML, encoding="utf-8")
    items = LocalInboundAdapter().discover(_spec(inbound), object())
    assert items and items[0].url.startswith("http://www.pbc.gov.cn/")
    assert items[0].published_at, "HTML 正文里的日期要能被解析出来"


def test_file_without_source_url_is_rejected_and_reported(inbound):
    (inbound / "没有溯源.html").write_text("<html><body><p>" + "央行 " * 60 + "</p></body></html>",
                                           encoding="utf-8")
    adapter = LocalInboundAdapter()
    assert adapter.discover(_spec(inbound), object()) == []
    assert adapter.missing_source_url(_spec(inbound)) == ["没有溯源.html"]


def test_missing_directory_is_not_an_error(tmp_path):
    assert LocalInboundAdapter().discover(_spec(tmp_path / "nope"), object()) == []


def test_end_to_end_inbound_document_lands_without_network(inbound, tmp_path, monkeypatch):
    monkeypatch.setenv("POLICYPULSE_HOME", str(tmp_path))
    paths = default_paths()
    (inbound / "2026-10-19-退出存贷款基准利率.html").write_text(HTML, encoding="utf-8")
    (inbound / "2026-10-19-退出存贷款基准利率.url").write_text(
        "http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html\n", encoding="utf-8")

    (paths.snapshots / "run").mkdir(parents=True, exist_ok=True)
    store = Store(paths.db)
    fetcher = _NoNetworkFetcher()
    docs, failed, dupes = fetch_documents(fetcher, [_spec(inbound)], Settings(),
                                          "2026-10-06", "2026-10-19",
                                          paths.snapshots / "run", "run-inbound", store)

    assert not failed and dupes == 0
    assert len(docs) == 1
    doc = docs[0]
    assert doc.org == "中国人民银行"
    assert doc.published_at == "2026-10-19"
    assert Path(doc.snapshot_path).suffix == ".html"
    assert Path(doc.snapshot_path).exists()
    # 标题里的站点后缀要被清掉，不能把转载站点当成发布机构
    assert doc.title.endswith("存贷款基准利率")
    assert is_pbc_related(doc.title, doc.body)
    store.close()


def test_inbound_respects_cross_run_dedupe(inbound, tmp_path, monkeypatch):
    monkeypatch.setenv("POLICYPULSE_HOME", str(tmp_path))
    paths = default_paths()
    (inbound / "2026-10-18-公开市场业务交易公告.md").write_text(MARKDOWN, encoding="utf-8")
    spec = _spec(inbound)
    (paths.snapshots / "r1").mkdir(parents=True, exist_ok=True)
    (paths.snapshots / "r2").mkdir(parents=True, exist_ok=True)
    store = Store(paths.db)

    first, _, _ = fetch_documents(_NoNetworkFetcher(), [spec], Settings(), "2026-10-06",
                                  "2026-10-19", paths.snapshots / "r1", "r1", store)
    assert len(first) == 1
    store.upsert_document({"doc_id": first[0].doc_id, "url": first[0].url,
                           "content_hash": first[0].content_hash})

    second, _, dupes = fetch_documents(_NoNetworkFetcher(), [spec], Settings(), "2026-10-06",
                                       "2026-10-19", paths.snapshots / "r2", "r2", store)
    # 文件没删：靠正文指纹拦住重复抽取，但文档仍以复用态进本轮报告，
    # 重跑同一天不会把已入库的文档挤出日报
    assert (inbound / "2026-10-18-公开市场业务交易公告.md").exists()
    assert dupes == 1
    assert len(second) == 1 and second[0].reused and second[0].doc_id == first[0].doc_id
    store.close()


def test_inbound_run_never_touches_the_pbc_host(inbound, tmp_path, monkeypatch):
    """合规护栏：pbc.gov.cn 被 Disallow: /，连 robots.txt 都不该去读。"""
    from policypulse.http import Fetcher

    monkeypatch.setenv("POLICYPULSE_HOME", str(tmp_path))
    paths = default_paths()
    (paths.snapshots / "r").mkdir(parents=True, exist_ok=True)
    (inbound / "2026-10-18-公开市场业务交易公告.md").write_text(MARKDOWN, encoding="utf-8")

    with Fetcher(min_interval=0.0, check_robots=True) as fetcher:
        docs, _, _ = fetch_documents(fetcher, [_spec(inbound)], Settings(), "2026-10-06",
                                     "2026-10-19", paths.snapshots / "r", "r", None)
        touched = fetcher.robots.observed()
    assert len(docs) == 1
    assert "www.pbc.gov.cn" not in touched, "本地入库路径不得对 pbc.gov.cn 发起任何请求"


def test_empty_inbound_directory_warns_instead_of_failing(inbound, tmp_path, monkeypatch):
    monkeypatch.setenv("POLICYPULSE_HOME", str(tmp_path))
    paths = default_paths()
    (paths.snapshots / "r").mkdir(parents=True, exist_ok=True)
    fetcher = _NoNetworkFetcher()
    docs, failed, _ = fetch_documents(fetcher, [_spec(inbound)], Settings(), "2026-10-06",
                                      "2026-10-19", paths.snapshots / "r", "r", Store(paths.db))
    assert docs == [] and failed == []
    assert any("inbound" in w for w in fetcher.warnings), "空目录要在告警里可见"
