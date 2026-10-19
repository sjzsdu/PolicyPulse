"""真实链路的冒烟测试，默认跳过：

    pytest -m network          # 抓真实政府站点
    pytest -m tongstock        # 需要本机 tongstock CLI
"""

import pytest

from policypulse.config import load_sources
from policypulse.http import Fetcher
from policypulse.sources.registry import get_adapter


def _network_source_ids() -> list[str]:
    """只测需要联网的源：

    - 关闭的源（如央行 robots 禁止）不该要求它能抓；
    - local_inbound 源读本地目录，目录空着是常态，也不该要求它有条目。
    """
    return [s.id for s in load_sources() if s.enabled and s.adapter != "local_inbound"]


@pytest.mark.network
@pytest.mark.parametrize("source_id", _network_source_ids())
def test_each_enabled_source_discovers_items(source_id):
    spec = next(s for s in load_sources() if s.id == source_id)
    with Fetcher(min_interval=0.5) as fetcher:
        items = get_adapter(spec).discover(spec, fetcher)
    assert items, f"{source_id} 没有发现任何条目"
    assert all(i.url.startswith("http") for i in items)


@pytest.mark.network
def test_reprint_source_covers_the_central_bank_and_is_robots_allowed():
    """WAN-10 的换取法：央行内容走合规转载源，且该源 robots 必须实测为 allowed。"""
    from policypulse.pipeline import PBC_MENTION_RE

    spec = next(s for s in load_sources() if s.id == "xinhua_pbc")
    with Fetcher(min_interval=0.5) as fetcher:
        items = get_adapter(spec).discover(spec, fetcher)
        policy = fetcher.robots.policy(spec.url)
    assert items, "转载源没发现任何央行条目（栏目可能改版，需要改 sources.yaml）"
    assert all(PBC_MENTION_RE.search(i.title) for i in items), "title_pattern 没起到过滤作用"
    assert policy.status == "allowed", f"转载源 robots 判定变化：{policy.reason}"


@pytest.mark.network
def test_disabled_pbc_source_stays_disabled():
    """pbc.gov.cn 明确 Disallow: —— 这条断言是「不绕过」的护栏。"""
    spec = next(s for s in load_sources() if s.id == "pbc_xw")
    assert not spec.enabled
    with Fetcher(min_interval=0.5) as fetcher:
        policy = fetcher.robots.policy(spec.url)
    assert policy.status == "allowed" and not policy.allows(spec.url), "央行若哪天放行，需人工复核后开启"


@pytest.mark.tongstock
def test_tongstock_universe_and_sectors():
    from policypulse.tongstock import CONCEPT_FILE, TongstockClient

    client = TongstockClient()
    tickers = client.tickers()
    sectors = client.sectors(CONCEPT_FILE)
    assert len(tickers) > 3000, "A 股基础信息太少，可能 tongstock 本地库存坏"
    assert len(sectors) > 100
    sample = next(iter(sectors))
    assert client.members(sample, CONCEPT_FILE)
