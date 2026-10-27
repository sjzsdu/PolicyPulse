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


def _mofcom_spec(**extra) -> SourceSpec:
    base = {"link_pattern": r"/art/\d{4}/art_", "max_items": 60, "min_title_length": 10,
            "reject_pattern": r"/zfxxgk/"}
    base.update(extra)
    return SourceSpec(
        id="mofcom_xwfb", name="新闻发布", org="商务部", tier="部委",
        adapter="html_list", url="http://www.mofcom.gov.cn/xwfb/index.html", extra=base,
    )


def test_html_list_reads_publish_date_from_list_item():
    """商务部 href 只有年份（`/art/2026/art_<uuid>.html`），日期只在列表项里。

    不解析列表项日期，候选就只能按页面顺序截断——这正是「商务部新闻抓到了但
    用户看不到」的根因之一。
    """
    html = (Path(__file__).parent / "fixtures" / "mofcom_xwfb_list.html").read_text(encoding="utf-8")
    items = HtmlListAdapter().discover(_mofcom_spec(), _FileFetcher(html))
    assert items, "真实商务部列表页应能发现文章链接"
    # 站内年报页被 reject_pattern 排掉；剩下的每一条都能从列表项解析出发布日期
    assert not any("zfwzgznb" in i.url for i in items)
    assert all(i.published_at for i in items), "每一条都应该解析出发布日期"
    by_title = {i.title: i.published_at for i in items}
    assert by_title["商务部新闻发言人就美发布新版对外投资限制拟议规则答记者问"] == "2026-10-24"
    assert by_title["商务部流通发展司负责人谈2026年1-9月我国批发和零售业发展情况"] == "2026-10-26"


def test_html_list_does_not_read_month_range_as_publish_date():
    """标题里的「2026年1-9月」是月份区间，不是发布日期（误判会把稿子挤出窗口）。"""
    html = (
        '<ul><li><a href="/xwfb/rcxwfb/art/2026/art_1.html">'
        "2026年1-9月全国吸收外资5488.1亿元人民币</a></li>"
        '<li><a href="/xwfb/rcxwfb/art/2026/art_2.html">中韩自贸协定第二阶段第17轮谈判在首尔举行</a>'
        "<span>[2026-10-20]</span></li></ul>"
    )
    items = HtmlListAdapter().discover(_mofcom_spec(), _FileFetcher(html))
    assert [i.published_at for i in items] == ["", "2026-10-20"]


def test_html_list_never_borrows_the_neighbour_date():
    """相邻条目的日期不能借：上一条没日期就得留空，交给详情页判定。"""
    html = (
        "<ul>"
        '<li><a href="/xwfb/xwfyrth/art/2026/art_a.html">'
        "商务部新闻发言人就出口管制措施答记者问</a></li>"
        '<li><a href="/xwfb/xwfyrth/art/2026/art_b.html">'
        "商务部新闻发言人就对外投资限制答记者问</a><span>[2026-10-24]</span></li>"
        "</ul>"
    )
    items = HtmlListAdapter().discover(_mofcom_spec(), _FileFetcher(html))
    assert [i.published_at for i in items] == ["", "2026-10-24"]
