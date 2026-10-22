"""政策信号抽取。

默认走确定性规则/词表，保证没有 LLM key 也能跑通全链路；
检测到 OpenAI 兼容的 key 时改用 LLM，并用同一套反幻觉校验兜底。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from policypulse.config import Settings, load_theme_alias
from policypulse.text import split_sentences

# 政策动作词：出现在句子里才视为“部署”，否则只是叙述背景
ACTION_CUES = [
    "支持", "推进", "加快", "培育", "扩大", "促进", "加大", "推动", "鼓励", "建设",
    "发展", "落实", "实施", "完善", "优化", "部署", "强化", "提升", "引导", "开展",
    "设立", "安排", "安排资金", "补贴", "减税", "降费", "投资", "试点", "推广",
]
STRENGTH_CUES = {
    5: ["印发", "发布", "出台", "决定", "通过", "批复", "意见", "规划", "纲要", "方案"],
    4: ["专项资金", "亿元", "万亿", "基金", "补贴", "贴息", "税收优惠", "降准", "再贷款",
        "目标", "到2030", "到2027", "翻番", "比例达到"],
    3: ["进一步", "全面", "深入", "扎实", "加快", "大力", "稳步", "有序"],
}
TIGHTEN_CUES = ["收紧", "压降", "严禁", "禁止", "整治", "清理", "淘汰", "限产", "去产能",
                "规范秩序", "反内卷", "遏制", "处罚", "查处", "退坡"]
EASE_CUES = ["支持", "鼓励", "扶持", "减税", "降费", "补贴", "扩大", "放宽", "便利", "利好",
             "激发", "释放", "培育", "壮大", "促进消费"]

INSTRUMENTS = {
    "财政": ["减税", "降费", "补贴", "财政", "专项债", "国债", "税收", "支出", "资金"],
    "货币金融": ["降准", "降息", "再贷款", "利率", "信贷", "融资", "货币政策", "流动性", "汇率"],
    "产业扶持": ["产业", "培育", "试点", "专项", "扶持", "技术创新", "攻关", "产业链"],
    "监管": ["监管", "整治", "规范", "执法", "标准", "准入", "审批", "合规"],
    "区域规划": ["区域", "省", "市", "自贸", "示范区", "城市群", "西部", "长三角", "大湾区"],
    "贸易": ["出口", "进口", "外贸", "关税", "跨境", "外资", "自贸"],
    "价格": ["价格", "电价", "油价", "收费", "成本"],
    "准入": ["准入", "许可", "资质", "负面清单", "开放"],
}
REGIONS = ["长三角", "珠三角", "粤港澳", "大湾区", "京津冀", "成渝", "西部", "东北", "中部",
           "雄安", "海南", "新疆", "西藏", "内蒙古", "广西", "云南", "贵州", "广东", "江苏",
           "浙江", "山东", "四川", "北京", "上海", "重庆", "天津", "福建", "湖北", "湖南",
           "河南", "河北", "陕西", "安徽", "江西", "山西", "辽宁", "吉林", "黑龙江", "甘肃",
           "青海", "宁夏"]

MEETING_TYPES = [
    ("国务院常务会议", ["国务院常务会议"]),
    ("政治局会议", ["中央政治局会议", "政治局会议"]),
    ("新闻发布会", ["新闻发布会", "例行吹风会", "政策吹风会"]),
    ("政策文件", ["印发", "通知", "意见", "批复", "规划", "方案", "办法", "条例", "决定"]),
    ("会议", ["会议", "座谈会", "调研"]),
    ("领导活动", ["会见", "出席", "考察", "调研", "致辞", "演讲"]),
]

HORIZON_CUES = {
    "短期(<1月)": ["近期", "当前", "四季度", "年底前", "本月", "今冬明春", "节前"],
    "长期(>6月)": ["十五五", "十四五", "2030", "2035", "中长期", "长远", "规划期"],
}

NOVELTY_CUES = [
    ("新增", ["首次", "新增", "出台", "印发", "启动", "设立"]),
    ("加码", ["进一步", "加大", "加力", "再加", "扩大", "提升"]),
    ("延续", ["继续", "延续", "保持", "稳定"]),
    ("重申", ["重申", "再次强调", "强调要"]),
    ("收紧", TIGHTEN_CUES),
]


@dataclass
class Signal:
    signal_id: str = ""
    doc_id: str = ""
    summary: str = ""
    quote: str = ""
    direction: str = "中性"
    strength: int = 1
    horizon: str = "中期(1–6月)"
    novelty: str = "延续"
    instruments: list[str] = field(default_factory=list)
    theme_tags: list[str] = field(default_factory=list)
    affected_regions: list[str] = field(default_factory=list)
    affected_sectors_hint: list[str] = field(default_factory=list)
    confidence: float = 0.5
    model: str = "rules-v1"
    prompt_version: str = "rules-v1"
    quote_verified: bool = False
    # 信号级可投资性闸门：保守默认 False，由 investability.assess_signal 判定后写入。
    # False = 泛经济样板表述，仍入库可审计，但不映射板块、不展开个股
    investable: bool = False
    investable_reason: str = ""
    # 受益主体明确性：LLM 抽取时输出的结构化语义判定（枚举见 investability.SPECIFICITY_*）。
    # 规则抽取路径没有该字段（空串 = 未判定），闸门只对明确的「无受益链条」判定生效
    beneficiary_specificity: str = ""


def normalize(text: str) -> str:
    """归一化空白，用于子串校验。"""
    return re.sub(r"\s+", "", text or "")


def classify_meeting_type(title: str) -> str:
    for label, cues in MEETING_TYPES:
        if any(cue in title for cue in cues):
            return label
    return "其他"


def _has_any(text: str, words) -> bool:
    return any(w in text for w in words)


def _match_themes(sentence: str, alias_map: dict[str, list[str]]) -> list[str]:
    hits = []
    for theme, aliases in alias_map.items():
        if any(alias in sentence for alias in aliases):
            hits.append(theme)
    return hits


def _score_sentence(sentence: str, title: str) -> float:
    score = 0.0
    if _has_any(sentence, ACTION_CUES):
        score += 0.25
    if _has_any(sentence, EASE_CUES):
        score += 0.15
    if _has_any(sentence, TIGHTEN_CUES):
        score += 0.10
    if any(cue in sentence for cue in STRENGTH_CUES[4]):
        score += 0.20
    if any(cue in sentence for cue in STRENGTH_CUES[5]):
        score += 0.15
    if any(cue in sentence for cue in STRENGTH_CUES[3]):
        score += 0.10
    if re.search(r"\d+(\.\d+)?(亿|万亿|万)", sentence):
        score += 0.15
    if len(sentence) > 30:
        score += 0.05
    return min(score, 1.0)


def _strength_of(sentence: str, title: str) -> int:
    strength = 1
    if any(c in sentence or c in title for c in STRENGTH_CUES[5]):
        strength = max(strength, 3)
    if any(c in sentence for c in STRENGTH_CUES[4]):
        strength = max(strength, 4)
    if any(c in title for c in STRENGTH_CUES[4]):
        strength = max(strength, 4)
    if _has_any(sentence, STRENGTH_CUES[3]):
        strength = max(strength, 3)
    if "国务院" in title and any(c in title for c in STRENGTH_CUES[5]):
        strength = max(strength, 5)
    if re.search(r"\d+(\.\d+)?(亿|万亿)", sentence):
        strength = max(strength, 4)
    return min(strength, 5)


def _direction_of(sentence: str) -> str:
    tighten = _has_any(sentence, TIGHTEN_CUES)
    ease = _has_any(sentence, EASE_CUES)
    if ease and not tighten:
        return "利好"
    if tighten and not ease:
        return "利空"
    if tighten and ease:
        return "中性"
    return "中性" if not ease else "利好"


def _novelty_of(sentence: str) -> str:
    for label, cues in NOVELTY_CUES:
        if _has_any(sentence, cues):
            return label
    return "延续"


def _horizon_of(sentence: str) -> str:
    for label, cues in HORIZON_CUES.items():
        if _has_any(sentence, cues):
            return label
    return "中期(1–6月)"


def _instruments_of(sentence: str) -> list[str]:
    return [name for name, cues in INSTRUMENTS.items() if _has_any(sentence, cues)]


def _regions_of(sentence: str) -> list[str]:
    return [r for r in REGIONS if r in sentence]


def _summary_of(sentence: str, limit: int = 80) -> str:
    s = sentence.strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _quote_of(sentence: str, limit: int = 200) -> str:
    """quote 必须是原文子串：从原句截断，绝不改写。"""
    s = sentence.strip()
    return s if len(s) <= limit else s[:limit]


def verify_quote(quote: str, body: str) -> bool:
    """反幻觉硬校验：归一化空白后必须是正文子串。"""
    if not quote:
        return False
    return normalize(quote) in normalize(body)


def _overlap(a: str, b: str) -> bool:
    """同一篇里高度重复的句子只保留一次。"""
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if not short:
        return True
    if short in long:
        return True
    step = max(1, len(short) // 4)
    hits = sum(1 for i in range(0, len(short) - 20, step) if short[i : i + 20] in long)
    return hits / max(1, len(range(0, len(short) - 20, step))) > 0.6


def extract_signals(
    doc_id: str,
    title: str,
    body: str,
    alias_map: dict[str, list[str]] | None = None,
    settings: Settings | None = None,
    max_signals: int = 4,
) -> list[Signal]:
    """从一篇正文里抽 0..N 个信号。全部字段可解释，quote 来自原文。"""
    settings = settings or Settings()
    alias_map = alias_map if alias_map is not None else load_theme_alias()

    sentences = split_sentences(body)
    scored: list[tuple[float, str]] = []
    for sent in sentences:
        themes = _match_themes(sent, alias_map)
        base = _score_sentence(sent, title)
        if not themes:
            # 没有主题词的句子映射不到板块：只有很明确的部署句才保留
            if base < 0.50:
                continue
            base *= 0.6
        if base >= 0.30:
            scored.append((base, sent))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    signals: list[Signal] = []
    seen: list[str] = []
    for rank, (base, sent) in enumerate(scored[: max_signals * 3], start=1):
        quote = _quote_of(sent)
        norm_q = normalize(quote)
        if any(_overlap(norm_q, prev) for prev in seen):
            continue
        seen.append(norm_q)

        themes = _match_themes(sent, alias_map)
        confidence = round(min(0.95, base + 0.15 * len(themes)), 2)
        verified = verify_quote(quote, body)
        if not verified:
            confidence = min(confidence, 0.4)
        if confidence < settings.min_confidence:
            continue

        signals.append(
            Signal(
                signal_id=f"{doc_id}#{rank}",
                doc_id=doc_id,
                summary=_summary_of(sent),
                quote=quote,
                direction=_direction_of(sent),
                strength=_strength_of(sent, title),
                horizon=_horizon_of(sent),
                novelty=_novelty_of(sent),
                instruments=_instruments_of(sent),
                theme_tags=themes,
                affected_regions=_regions_of(sent),
                affected_sectors_hint=themes,
                confidence=confidence,
                quote_verified=verified,
            )
        )
    return signals
