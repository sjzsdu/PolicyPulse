"""信号级可投资性闸门：泛经济样板表述不得映射板块、不得进「受益」表。

背景（reports/2026-10-21.md 的真实翻车）：财政部 APEC 财长会通稿里
「扩大国内需求、持续扩大高水平对外开放、加快培育增长新动能」这类**泛经济样板表述**
被抽成主题「新零售」，下游忠实展开 10 只概念股进「受益」表——而同日 3 条深度研读
全部判「无实质传导」。公司级相关性闸门（relevance.py）在下游救不了被标错的上游主题，
所以判定必须上移到信号层。

判定规则（**确定性词表匹配，不引入任何 LLM 调用**）：

1. 命中 ≥1 个 `GENERIC_POLICY_MARKERS` 泛样板词，且
2. 信号文本（summary + quote）里不含任何具名产业 / 具体项目词
   → `investable=False`，该信号仍入库可审计，但**跳过板块匹配与个股展开**。

判定不确定时（既无样板词也无具名产业词）按需求文档归为可投资；
`Signal.investable` 字段本身保守默认 False，只有判定通过后才写 True。
"""

from __future__ import annotations

from policypulse.config import Settings, load_theme_business

# 泛经济样板词：出现在任何一篇通稿里都不构成「具体产业 / 具体项目 / 量化措施」。
# 词表刻意只收「换个会议也能原样套用」的表述，避免把真政策误杀成非投资。
GENERIC_POLICY_MARKERS: tuple[str, ...] = (
    "扩大内需",
    "扩大国内需求",
    "高质量发展",
    "高水平对外开放",
    "经济持续向好",
    "持续回升向好",
    "回升向好",
    "增长新动能",
    "新动能培育",
    "新发展格局",
    "双循环",
    "中国式现代化",
    "深化改革开放",
    "改革开放",
    "稳中求进",
    "稳增长",
    "提质增效",
    "行稳致远",
)

# 具名产业 / 具体领域词：命中任意一个即认为信号有落点，不判非投资。
# 与 theme_business.csv 的业务关键词合并使用（见 named_industry_terms）。
NAMED_INDUSTRY_WORDS: tuple[str, ...] = (
    "新能源汽车", "智能网联汽车", "电动汽车", "充电桩", "换电",
    "人工智能", "大模型", "算力", "数据中心", "东数西算", "智能制造", "工业互联网",
    "碳市场", "碳排放权交易", "全国碳市场", "碳定价", "绿证", "碳捕集",
    "工业母机", "数控机床", "机床", "五轴",
    "职业教育", "职业技能培训", "终身职业技能",
    "养老服务", "普惠养老", "银发经济", "适老化",
    "数字经济", "数字化转型", "数字产业化", "产业数字化",
    "低空经济", "通用航空", "无人机", "eVTOL",
    "数据要素", "数据产权", "数据交易", "公共数据",
    "半导体", "集成电路", "芯片", "光刻",
    "创新药", "生物医药", "医疗器械", "CXO",
    "光伏", "风电", "储能", "氢能", "核电", "特高压", "智能电网",
    "商业航天", "卫星互联网", "火箭", "北斗",
    "机器人", "人形机器人", "减速器", "伺服",
    "量子科技", "量子计算", "可控核聚变", "超导",
    "种业", "粮食安全", "高标准农田", "乡村振兴",
    "保障性住房", "城中村改造", "平急两用", "城市更新",
    "航空发动机", "大飞机", "船舶", "高端装备", "首台套",
    "稀土", "锂电", "新材料", "超硬材料",
    "信创", "操作系统", "工业软件", "基础软件",
    "跨境电子商务", "自贸区", "自贸试验区", "海南自贸港", "服务贸易",
    "专项债", "超长期特别国债", "转移支付", "结构性减税", "留抵退税",
    "普惠金融", "科技金融", "绿色金融", "养老金融", "数字金融",
    "全国统一大市场", "负面清单", "市场准入",
)

_MIN_TERM_LEN = 2


def named_industry_terms(business_map: dict[str, list[str]] | None = None) -> tuple[str, ...]:
    """具名产业词表 = 内置常量 ∪ theme_business.csv 的业务关键词（≥2 字）。

    复用业务关键词集是需求文档明确允许的：这些词本就是「具体领域」的白名单。
    1 字词（「碳」「矿」「锂」…）会被剔掉——它们在任何经济稿里都可能出现，
    留着会把泛样板话重新放行。返回按长度降序，命中证据才稳定。
    """
    business_map = business_map if business_map is not None else load_theme_business()
    words = {w.strip() for ws in business_map.values() for w in ws
             if len(w.strip()) >= _MIN_TERM_LEN}
    words.update(NAMED_INDUSTRY_WORDS)
    return tuple(sorted(words, key=lambda t: (-len(t), t)))


def assess_investable(text: str, terms: tuple[str, ...] | None = None) -> tuple[bool, str]:
    """对一段信号文本做确定性判定，返回 (investable, 判定依据)。"""
    text = text or ""
    terms = terms if terms is not None else named_industry_terms()
    markers = [m for m in GENERIC_POLICY_MARKERS if m in text]
    if not markers:
        return True, "未命中泛经济样板表述"
    hits = [t for t in terms if t in text]
    if hits:
        return True, (f"命中泛样板表述「{markers[0]}」，但含具名产业词"
                      f"「{'、'.join(hits[:3])}」")
    return False, f"命中泛样板表述「{markers[0]}」且无具名产业词"


def assess_text(text: str, settings: Settings | None = None,
                business_map: dict[str, list[str]] | None = None) -> tuple[bool, str]:
    """对一段信号文本做带开关的判定（入库行、CLI 展示共用同一入口）。"""
    settings = settings or Settings()
    if not settings.investable_gate:
        return True, "信号级可投资性闸门已关闭（investable_gate=false）"
    return assess_investable(text, named_industry_terms(business_map))


def assess_signal(sig, settings: Settings | None = None,
                  business_map: dict[str, list[str]] | None = None) -> tuple[bool, str]:
    """判定一条信号是否可投资。闸门开关关闭时一律放行（并如实写明）。"""
    text = f"{getattr(sig, 'summary', '')}{getattr(sig, 'quote', '')}"
    return assess_text(text, settings, business_map)
