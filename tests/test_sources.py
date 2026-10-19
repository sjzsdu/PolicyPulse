from pathlib import Path

from policypulse.config import SourceSpec
from policypulse.sources.html_list import HtmlListAdapter


def _spec(**extra) -> SourceSpec:
    return SourceSpec(
        id="mof_czxw", name="财政新闻", org="财政部", tier="部委",
        adapter="html_list", url="https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/",
        extra=extra,
    )


class _FileFetcher:
    """把已保存的真实列表页当响应返回：不是 mock 数据，是冻结的真实页面。"""

    def __init__(self, html: str) -> None:
        self.html = html

    def get_text(self, url: str) -> str:
        return self.html

    def get_json(self, url: str):
        import json

        return json.loads(self.html)


def test_html_list_discovers_article_links():
    html = (Path(__file__).parent / "fixtures" / "mof_list.html").read_text(encoding="utf-8")
    spec = _spec(link_pattern=r"t\d{8}_\d+\.htm", max_items=30, min_title_length=10)
    items = HtmlListAdapter().discover(spec, _FileFetcher(html))
    assert items, "真实财政部列表页应能发现文章链接"
    for item in items:
        assert item.url.startswith("http")
        assert len(item.title) >= 10
    assert any("202610" in item.url for item in items)


def test_html_list_respects_reject_pattern_and_limit():
    html = (Path(__file__).parent / "fixtures" / "ndrc_list.html").read_text(encoding="utf-8")
    spec = _spec(link_pattern=r"t\d{8}_\d+\.html$", reject_pattern=r"index_\d+\.html$",
                 max_items=5, min_title_length=10)
    items = HtmlListAdapter().discover(spec, _FileFetcher(html))
    assert len(items) <= 5
    assert all("index_" not in i.url for i in items)


def test_gov_json_feed_parses_feed_payload():
    from policypulse.sources.gov_json import GovJsonFeedAdapter

    payload = '[{"TITLE":"国务院关于发展新质生产力的意见","URL":"https://www.gov.cn/a.htm","DOCRELPUBTIME":"2026-10-09"}]'
    spec = SourceSpec(id="gov", name="最新政策", org="国务院", tier="中央",
                      adapter="gov_json_feed", url="https://www.gov.cn/zhengce/zuixin/",
                      extra={"feed_url": "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json"})
    items = GovJsonFeedAdapter().discover(spec, _FileFetcher(payload))
    assert len(items) == 1
    assert items[0].published_at == "2026-10-09"
    assert items[0].url == "https://www.gov.cn/a.htm"
