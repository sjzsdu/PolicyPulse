"""信号级可投资性闸门：泛经济样板表述与无受益链条信号不得映射板块、不得进「受益」表。

背景（reports/2026-10-21.md 与 2026-10-22 的真实翻车）：财政部 APEC 财长会通稿里
「扩大国内需求、持续扩大高水平对外开放、加快培育增长新动能」这类**泛经济样板表述**
被抽成主题「新零售」，下游忠实展开 10 只概念股进「受益」表；次日中国—东帝汶经贸
合作联合委员会（机制性双边会议，无具名受益方 / 订单 / 额度）又被抽成「一带一路」
展开 10 只概念股。公司级相关性闸门（relevance.py）在下游救不了被标错的上游主题，
所以判定必须上移到信号层。

判定分两层：

1. **受益主体明确性（语义判定，LLM 侧）**：抽取时 LLM 按量表输出
   `beneficiary_specificity`（枚举见下）。判「无受益链条」→ 非投资。
   语义判定由 LLM 依据原文完成（「这个政策有没有指名受益主体和具体措施」），
   代码只消费结构化枚举，不做关键词命中——这层专抓 APEC 财长会 / 中—东联委会
   这类「结构性」非投资信号，词表永远追不上它们的改写。
2. **词表校验（确定性兜底，无 LLM 也可跑）**：命中 ≥1 个 `GENERIC_POLICY_MARKERS`
   泛样板词，且信号文本（summary + quote）里不含任何具名产业 / 具体项目词 → 非投资。
   CR-1 再加一层**主题证据校验**：theme_tags 若只由「泛 alias」命中、原文又不含该
   主题的具名证据（theme_business 业务词 / 具名 alias / 主题名本身）→ 非投资。

被拦信号仍入库可审计，但跳过板块匹配与个股展开。
`beneficiary_specificity` 缺失（规则抽取路径 / 老数据）时不启用第 1 层，
仍由第 2 层兜底；`Signal.investable` 字段保守默认 False，只有判定通过才写 True。
"""

from __future__ import annotations

from policypulse.config import Settings, load_theme_alias, load_theme_business

# 受益主体明确性：LLM 抽取时输出的结构化语义判定（prompt 量表钉死这三个枚举值）。
# 代码只精确消费枚举，不对其它自由文本做 contains 匹配——语义判定在 LLM 侧完成。
SPECIFICITY_NAMED = "有明确受益主体"
SPECIFICITY_DIRECTIONAL = "有产业方向但无具体主体"
SPECIFICITY_NONE = "无受益链条"
# LLM 侧量表钉死的三个枚举值（prompt 见 llm.py _SYSTEM）
SPECIFICITY_ENUM: tuple[str, ...] = (SPECIFICITY_NAMED, SPECIFICITY_DIRECTIONAL, SPECIFICITY_NONE)

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

# 具体政策项目名：有明确落点的白名单词（与上面 denylist 的样板词方向相反）。
# CR 反例里「首发经济 / 老旧小区改造 / 县域商业体系 / 以旧换新」只因不在词表内，
# 就被「扩大国内需求」这类样板词连坐判非投资——这类具名项目必须放行。
# 放进白名单只会少拦，不会多拦，无跷跷板风险（denylist 扩词才有）。
CONCRETE_PROGRAM_WORDS: tuple[str, ...] = (
    "首发经济", "首店经济",
    "老旧小区改造", "城镇老旧小区改造",
    "县域商业体系", "以旧换新", "消费品以旧换新",
    "设备更新", "大规模设备更新",
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
) + CONCRETE_PROGRAM_WORDS

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


# 泛需求措辞：只表达「要扩内需 / 要促消费」这种宏观意愿的表述（CR-1）。
# 前一组来自 theme_alias.csv 的原始泛 alias（内需/促消费/消费新业态/商圈 → 新零售，
# 消费升级 → 白酒概念）；后一组是**同义改写族**——通稿与模型输出同样会这么写，
# 但不在 csv 里，例：「大力提振内需，扩大居民消费」漏判实测就靠它穿网。
# 取舍：不把它们加进 denylist（GENERIC_POLICY_MARKERS），只用来做「主题证据不足」的
# 判据，因此不会连坐「大规模设备更新和消费品以旧换新」这类带具名落点的真消费政策。
GENERIC_THEME_ALIASES: frozenset[str] = frozenset({
    # theme_alias.csv 原始泛 alias
    "内需", "促消费", "消费新业态", "商圈", "消费升级",
    # 同义改写族（CR-1 漏判实测穿网句）
    "提振内需", "扩内需", "扩大内需", "扩大国内需求", "扩大居民消费",
    "提振消费", "扩大消费", "释放消费潜力", "消费扩容提质", "培育消费新业态",
})


def theme_evidence(text: str, theme_tags, alias_map: dict[str, list[str]] | None = None,
                   business_map: dict[str, list[str]] | None = None) -> tuple[bool, str]:
    """主题证据校验（CR-1 路线 b）：主题是否只由泛需求措辞支撑且原文无具名证据。

    返回 (ok, 依据)。ok=False 表示信号的主题无法用原文证据支撑 → 判非投资。
    无主题标签、或任一主题有具名证据（主题名本身 / 具名 alias / 业务词）时返回 True。
    """
    tags = [str(t).strip() for t in (theme_tags or []) if str(t).strip()]
    if not tags:
        return True, "无主题标签，跳过主题证据校验"
    alias_map = alias_map if alias_map is not None else load_theme_alias()
    business_map = business_map if business_map is not None else load_theme_business()
    for theme in tags:
        if theme in text:
            return True, f"原文含主题名「{theme}」"
        aliases = alias_map.get(theme) or []
        named = [a for a in aliases
                 if a not in GENERIC_THEME_ALIASES and len(a) >= _MIN_TERM_LEN and a in text]
        if named:
            return True, f"命中具名 alias「{named[0]}」（主题 {theme}）"
        biz = [w.strip() for w in (business_map.get(theme) or [])
               if len(w.strip()) >= _MIN_TERM_LEN and w.strip() in text]
        if biz:
            return True, f"命中业务词「{biz[0]}」（主题 {theme}）"
    phrases = sorted((p for p in GENERIC_THEME_ALIASES if p in text), key=lambda p: -len(p))
    if phrases:
        return False, (f"主题「{'、'.join(tags)}」在原文无具名证据"
                       f"（业务词 / 具名 alias），仅命中泛需求措辞「{phrases[0]}」")
    return True, "主题未被泛需求措辞唯一支撑，不做证据降级"


def specificity_enum_violation(value) -> str | None:
    """O1 审计：`beneficiary_specificity` 非空但不在量表枚举内 → 返回告警文本。

    这是「模型没守量表」的痕迹——第 1 层对非枚举值只能跳过（交词表兜底），
    没有告警的话，模型把结构性稿回成自由文本时闸门全穿也查不到。空值
    （规则抽取路径 / 老数据）与三个枚举值都返回 None。
    """
    text = str(value or "").strip()
    if text and text not in SPECIFICITY_ENUM:
        return (f"beneficiary_specificity 非量表枚举值「{text}」"
                f"（应为 {' / '.join(SPECIFICITY_ENUM)}；模型未守量表）")
    return None


def assess_text(text: str, settings: Settings | None = None,
                business_map: dict[str, list[str]] | None = None,
                theme_tags=None,
                alias_map: dict[str, list[str]] | None = None,
                beneficiary_specificity: str | None = None) -> tuple[bool, str]:
    """对一段信号文本做带开关的判定（入库行、CLI 展示共用同一入口）。

    顺序：受益主体明确性（语义判定，LLM 侧）→ 具名产业词 → 主题证据校验（CR-1）
    → 泛样板词兜底。`beneficiary_specificity` 缺失时跳过第一层（规则路径 / 老数据）。
    """
    settings = settings or Settings()
    if not settings.investable_gate:
        return True, "信号级可投资性闸门已关闭（investable_gate=false）"
    text = text or ""
    # 第 1 层：LLM 语义判定「无受益链条」→ 直接非投资。
    # 这是结构化枚举的精确消费，不是词表命中——判定本身由 LLM 读原文语义得出
    # （机制性双边会议 / 泛样板稿既无具名受益方也无订单额度 → 无受益链条）。
    if (beneficiary_specificity or "").strip() == SPECIFICITY_NONE:
        return False, (f"受益主体明确性语义判定为「{SPECIFICITY_NONE}」"
                       f"（原文未指名受益主体 / 订单 / 额度），不映射板块、不展开个股")
    terms = named_industry_terms(business_map)
    markers = [m for m in GENERIC_POLICY_MARKERS if m in text]
    named_hits = [t for t in terms if t in text]
    if named_hits:
        joined = "、".join(named_hits[:3])
        if markers:
            return True, f"命中泛样板表述「{markers[0]}」，但含具名产业词「{joined}」"
        return True, f"含具名产业词「{joined}」"
    ok, reason = theme_evidence(text, theme_tags, alias_map, business_map)
    if not ok:
        return False, reason
    if markers:
        return False, f"命中泛样板表述「{markers[0]}」且无具名产业词"
    return True, "未命中泛经济样板表述"


def assess_signal(sig, settings: Settings | None = None,
                  business_map: dict[str, list[str]] | None = None,
                  alias_map: dict[str, list[str]] | None = None) -> tuple[bool, str]:
    """判定一条信号是否可投资。闸门开关关闭时一律放行（并如实写明）。"""
    text = f"{getattr(sig, 'summary', '')}{getattr(sig, 'quote', '')}"
    return assess_text(text, settings, business_map,
                       theme_tags=list(getattr(sig, "theme_tags", None) or []),
                       alias_map=alias_map,
                       beneficiary_specificity=getattr(sig, "beneficiary_specificity", ""))
