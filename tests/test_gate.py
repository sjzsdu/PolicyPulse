"""公司级相关性闸门（第 2 点）：主题 ↔ 公司主营业务 / 营收构成。"""

from __future__ import annotations

from policypulse.profile import CompanyProfile, load_profile, parse_overview, parse_segments
from policypulse.relevance import evaluate_gate, relevance_after_gate, theme_terms

# 真实 F10 公司概况表格的结构（含值续行 + 后面的「公司简介」「关联方」表）
OVERVIEW_F10 = """\
│公司名称│珠海格力电器股份有限公司│上市日期│1996-11-18│
│通达信研究行业│家用电器-白色家电│证监会行业│电气机械和器材制造业│
│主营业务│生产销售空调、暖气机等家用电器，以及工业制品和智能装备│
│　　　　　│及智能家居解决方案│
│经营范围│货物进出口；技术进出口；代理进出口；技术培训；电器产品的安装、维修及售后服务│
│公司简介│格力电器是一家综合性家电企业，业务涵盖消费电器、工业装备│
│关联方名称│关联方主营业务│关联金额│
│格力地产│房地产开发与销售│12.3亿│
"""

ANALYSIS_F10 = """\
【2.主营构成分析】
空调(行业) 1,234,567 92.3% 1.2%
工业制品(行业) 45,678 3.4% 0.5%
其他(行业) 12,345 4.3% 0.1%
智能装备(产品) 8,900 0.7% 0.2%
"""


def test_overview_parser_keeps_main_business_and_drops_later_tables():
    fields = parse_overview(OVERVIEW_F10)
    assert fields["公司名称"] == "珠海格力电器股份有限公司"
    assert fields["通达信研究行业"] == "家用电器-白色家电"
    # 主营业务必须保留值的续行
    assert fields["主营业务"].startswith("生产销售空调")
    assert "智能家居解决方案" in fields["主营业务"]
    # 后面的「关联方」表里也有叫主营业务的列，绝不能串进来
    assert "房地产开发" not in fields["主营业务"]
    assert "格力地产" not in fields["主营业务"]
    # 「公司简介」长文不属于主营业务
    assert "综合性家电企业" not in fields["主营业务"]


def test_segment_parser_only_takes_industry_and_product_rows():
    segs = parse_segments(ANALYSIS_F10)
    assert segs == ["空调", "工业制品", "其他", "智能装备"]


def _profile(**kwargs) -> CompanyProfile:
    base = dict(code="000651", name="格力电器", industry="家用电器-白色家电",
                main_business="生产销售空调、暖气机等家用电器，以及工业制品和智能装备",
                scope="货物进出口；技术培训", fetched=True)
    base.update(kwargs)
    return CompanyProfile(**base)


def test_gate_hits_on_main_business():
    drone = CompanyProfile(code="002085", name="万丰奥威", industry="航空装备",
                           main_business="通用飞机制造、汽车零部件及金属加工",
                           scope="航空技术咨询", fetched=True)
    gate = evaluate_gate(theme_terms("低空经济"), drone)
    assert gate.passed and gate.basis == "profile_hit"
    assert "通用飞机" in gate.evidence


def test_gate_rejects_board_member_without_business_overlap():
    """格力是「工业母机」板块成员，但主业里没有机床 → 无实质交集。"""
    gate = evaluate_gate(theme_terms("工业母机"), _profile())
    assert not gate.passed and gate.basis == "profile_miss"

    gate2 = evaluate_gate(theme_terms("职业教育"), _profile())
    assert not gate2.passed and gate2.basis == "profile_miss"
    assert relevance_after_gate(1.0, gate2) <= 0.25


def test_gate_rejects_generic_scope_only_match_when_term_is_short():
    """经营范围里满是「技术培训」，但 2 字关键词不得凭它放行；≥3 字才算弱证据。"""
    profile = CompanyProfile(code="600519", name="贵州茅台",
                             industry="食品饮料-白酒", main_business="茅台酒及系列酒的生产与销售",
                             scope="农业种植；农产品加工；技术培训；乡村振兴帮扶",
                             fetched=True)
    assert not evaluate_gate(theme_terms("新零售"), profile).passed
    weak = evaluate_gate(theme_terms("乡村振兴"), profile)
    # 弱证据不放行，但留有余地：相关性压到 WEAK_CAP 而不是 DEMOTE_CAP
    assert not weak.passed and weak.basis == "weak_hit"
    assert relevance_after_gate(1.0, weak) == 0.45


def test_gate_without_profile_fails_open():
    gate = evaluate_gate(["低空经济"], None)
    assert gate.passed and gate.basis == "no_profile"
    assert relevance_after_gate(1.0, gate) == 1.0


def test_gate_hit_raises_relevance_but_miss_caps_it():
    from policypulse.relevance import GateResult

    hit = GateResult(True, "profile_hit", "主营业务命中「低空经济」", ("低空经济",))
    assert relevance_after_gate(0.7, hit) == 0.85
    assert relevance_after_gate(1.0, hit) == 1.0
    miss = GateResult(False, "profile_miss", "无实质交集")
    assert relevance_after_gate(1.0, miss) == 0.25


def test_noise_examples_from_the_before_report_are_filtered():
    """before 产物里的真实噪声：主题与公司业务没有交集就必须过不了闸门。"""
    gehi = CompanyProfile(code="000651", name="格力电器",
                          industry="家用电器-白色家电",
                          main_business="生产销售空调、暖气机等家用电器，以及工业制品和智能装备",
                          scope="货物进出口；技术培训", fetched=True)
    assert not evaluate_gate(theme_terms("工业母机"), gehi).passed

    mining = CompanyProfile(code="001203", name="大中矿业", industry="黑色金属矿采选",
                            main_business="铁矿石开采、选矿、加工和销售",
                            scope="矿山工程施工；技术培训", fetched=True)
    assert not evaluate_gate(theme_terms("职业教育"), mining).passed
    assert not evaluate_gate(theme_terms("新零售"), mining).passed

    maotai = CompanyProfile(code="600519", name="贵州茅台", industry="食品饮料-白酒",
                            main_business="茅台酒及系列酒的生产与销售",
                            scope="农业种植；农产品加工", fetched=True)
    assert not evaluate_gate(theme_terms("乡村振兴"), maotai).passed
    assert not evaluate_gate(theme_terms("新零售"), maotai).passed

    # 反过来，真正做教育/做零售/做机床的不能被误杀
    edu = CompanyProfile(code="000526", name="学大教育", industry="社会服务-教育",
                         main_business="个性化教育培训服务与职业教育",
                         scope="教育咨询服务", fetched=True)
    assert evaluate_gate(theme_terms("职业教育"), edu).passed

    retail = CompanyProfile(code="601933", name="永辉超市", industry="商贸-一般零售",
                            main_business="生鲜及食品超市连锁经营",
                            scope="商业综合体管理服务", fetched=True)
    assert evaluate_gate(theme_terms("新零售"), retail).passed

    machine = CompanyProfile(code="000410", name="沈阳机床", industry="通用机械-机床",
                             main_business="数控机床、普通机床及功能部件的研发制造",
                             scope="机械设备修理", fetched=True)
    assert evaluate_gate(theme_terms("工业母机"), machine).passed


def test_load_profile_degrades_when_client_has_no_f10():
    class _NoF10:
        pass

    p = load_profile(_NoF10(), "600519")
    assert not p.has_profile
    assert evaluate_gate(["低空经济"], p).basis == "no_profile"


def test_theme_business_table_covers_every_alias_theme():
    from policypulse.config import load_theme_alias, load_theme_business

    alias, business = load_theme_alias(), load_theme_business()
    missing = set(alias) - set(business)
    assert not missing, f"这些主题没有业务关键词，闸门只能靠别名兜底：{sorted(missing)}"
