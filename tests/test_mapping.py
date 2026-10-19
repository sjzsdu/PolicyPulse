from policypulse.config import Settings
from policypulse.extract import Signal
from policypulse.mapping import Candidate, SectorMatch, build_candidates, match_sectors
from policypulse.tongstock import Ticker

CONCEPTS = ["低空经济", "算力租赁", "乡村振兴", "白酒概念"]
INDUSTRIES = ["航空装备", "软件服务"]


class _FakeClient:
    """只替换 TDX 读取层，映射逻辑本身是真实代码路径。"""

    def __init__(self, members):
        self._members = members
        self.calls = 0

    def sectors(self, block_file="block_gn.dat"):
        return {name: len(self._members.get(name, [])) for name in CONCEPTS} if "gn" in block_file else {n: 0 for n in INDUSTRIES}

    def members(self, sector, block_file="block_gn.dat"):
        return self._members.get(sector, [])

    def tickers(self):
        return {
            "000001": Ticker("000001", "平安银行", "sz", "深市主板", 2200.0),
            "600519": Ticker("600519", "贵州茅台", "sh", "沪市主板", 15000.0),
            "000998": Ticker("000998", "ST农科", "sz", "深市主板", 80.0),
            "300750": Ticker("300750", "宁德时代", "sz", "创业板", 9000.0),
            "002001": Ticker("002001", "小市值股", "sz", "深市主板", 12.0),
        }


def test_exact_and_alias_match():
    matches = match_sectors(["低空经济"], CONCEPTS, INDUSTRIES)
    assert matches and matches[0].sector_name == "低空经济"
    assert matches[0].match_type == "exact"
    assert matches[0].confidence == 1.0


def test_fuzzy_match_is_confident_or_absent():
    matches = match_sectors(["人工智能"], CONCEPTS, INDUSTRIES)
    assert matches == [] or matches[0].confidence <= 0.9


def test_unmatched_theme_is_dropped():
    assert match_sectors(["完全不存在的主题XYZ"], CONCEPTS, INDUSTRIES) == []


def test_build_candidates_filters_st_and_small_cap():
    client = _FakeClient({"低空经济": ["000001", "000998", "002001", "600519"]})
    matches = [SectorMatch("低空经济", "低空经济", "通达信概念", "block_gn.dat", "exact", 1.0)]
    cands = build_candidates(matches, client, Settings())
    codes = [c.ticker.code for c in cands]
    assert "000998" not in codes, "ST 股应被过滤"
    assert "002001" not in codes, "市值低于阈值的应被过滤"
    assert codes == ["600519", "000001"], "按市值降序"


def test_top_n_per_sector_is_honored():
    settings = Settings(top_n_per_sector=1)
    client = _FakeClient({"低空经济": ["000001", "600519"]})
    matches = [SectorMatch("低空经济", "低空经济", "通达信概念", "block_gn.dat", "exact", 1.0)]
    assert len(build_candidates(matches, client, settings)) == 1


def test_mapping_path_is_recorded():
    client = _FakeClient({"低空经济": ["600519"]})
    matches = [SectorMatch("低空经济", "低空经济", "通达信概念", "block_gn.dat", "alias", 0.9)]
    cand = build_candidates(matches, client, Settings())[0]
    assert cand.mapping_path == "低空经济 → 低空经济(通达信概念) → 600519"
    assert cand.mapping_confidence == 0.9
