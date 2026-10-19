"""真实链路的冒烟测试，默认跳过：

    pytest -m network          # 抓真实政府站点
    pytest -m tongstock        # 需要本机 tongstock CLI
"""

import pytest

from policypulse.config import load_sources
from policypulse.http import Fetcher
from policypulse.sources.registry import get_adapter


def _enabled_source_ids() -> list[str]:
    """只测启用的源：关闭的源（如央行 robots 禁止）不该要求它能抓。"""
    return [s.id for s in load_sources() if s.enabled]


@pytest.mark.network
@pytest.mark.parametrize("source_id", _enabled_source_ids())
def test_each_enabled_source_discovers_items(source_id):
    spec = next(s for s in load_sources() if s.id == source_id)
    with Fetcher(min_interval=0.5) as fetcher:
        items = get_adapter(spec).discover(spec, fetcher)
    assert items, f"{source_id} 没有发现任何条目"
    assert all(i.url.startswith("http") for i in items)


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
