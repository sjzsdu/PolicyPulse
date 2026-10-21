"""公司级相关性闸门：主题 ↔ 公司主营业务 / 营收构成，而不是「主题 ∈ 板块」。

背景：通达信概念板块是宽口径的，「职业教育」板块里有大中矿业、永辉超市，
「新零售」板块里有五粮液、京东方——它们只是板块成员，跟政策主题没有业务交集。
这道闸门用 F10 的公开公司信息做交集判断，无实质交集的个股相关性被压到最低，
不再进入日报正文（数据仍入库，便于审计）。

无 LLM key 也照常工作：规则是确定性的；LLM 不参与闸门判断。
"""

from __future__ import annotations

from dataclasses import dataclass

from policypulse.config import Settings, load_theme_alias, load_theme_business
from policypulse.profile import CompanyProfile

# 2 字以下的词（「消费」「装备」「出口」…）在 F10 里一抓一大把，只作强证据字段的下限
MIN_TERM_LEN = 2
# 经营范围是弱证据（普遍含「技术培训」「教育咨询」类样板词），只认 3 字以上的关键词
MIN_WEAK_TERM_LEN = 3
# 闸门不通过时相关性被压到的上限：低于 min_relevance 截断，正文里不再出现
DEMOTE_CAP = 0.25
# 只有经营范围命中（样板词多）：同样不进正文，但比「完全无交集」留有余地，
# 操作者把 min_relevance 调到 0.45 以下时可以放出来复核
WEAK_CAP = 0.45

BASIS_HIT = "profile_hit"
BASIS_WEAK_HIT = "weak_hit"
BASIS_MISS = "profile_miss"
BASIS_NO_PROFILE = "no_profile"
BASIS_DISABLED = "disabled"


@dataclass
class GateResult:
    passed: bool
    basis: str
    evidence: str
    hits: tuple[str, ...] = ()


def _hits_evidence(prefix: str, hits: tuple[str, ...]) -> str:
    """证据串列全命中词（截断到 3 个），审计时能看到公司凭什么被判相关。"""
    shown = "、".join(hits[:3])
    if len(hits) > 3:
        shown += f" 等 {len(hits)} 个关键词"
    return f"{prefix}「{shown}」"


def theme_terms(theme: str, alias_map: dict[str, list[str]] | None = None,
                business_map: dict[str, list[str]] | None = None) -> list[str]:
    """一个政策主题用于比对公司业务的关键词集合，按长度降序（长词优先命中）。"""
    alias_map = alias_map if alias_map is not None else load_theme_alias()
    business_map = business_map if business_map is not None else load_theme_business()
    raw = [theme]
    raw.extend(alias_map.get(theme, []))
    raw.extend(business_map.get(theme, []))
    terms = {t.strip() for t in raw if t and len(t.strip()) >= MIN_TERM_LEN}
    # 同长度关键词按字典序定序：命中顺序必须与 PYTHONHASHSEED 无关，
    # 否则 evidence 里出现哪个词会随进程哈希种子漂移（闸门判定本身不受影响）
    return sorted(terms, key=lambda t: (-len(t), t))


def evaluate_gate(terms: list[str], profile: CompanyProfile | None) -> GateResult:
    """判定主题与公司业务是否有实质交集。

    - 强证据：主营业务 / 营收构成 / 研究行业命中任一关键词 → 放行并抬分
    - 弱证据：只有经营范围命中（且关键词 ≥3 字）→ 不放行（经营范围满是样板词，
      「乡村振兴帮扶」「技术培训」这类写法不构成业务实质相关），但记为弱命中，
      相关性压到 WEAK_CAP 而不是 DEMOTE_CAP，便于调 min_relevance 复核
    - 无交集 → 不通过，压到 DEMOTE_CAP
    - 拿不到画像 → 放行并标注（不能把「没数据」当成「不相关」）
    """
    if profile is None or not profile.has_profile:
        return GateResult(True, BASIS_NO_PROFILE, "无公司画像（F10 未取到），闸门放行并标注")

    strong = profile.strong_text()
    hits = tuple(t for t in terms if t in strong)
    if hits:
        return GateResult(True, BASIS_HIT, _hits_evidence("主营业务/营收构成命中", hits), hits)

    weak = profile.weak_text()
    weak_hits = tuple(t for t in terms if len(t) >= MIN_WEAK_TERM_LEN and t in weak)
    if weak_hits:
        return GateResult(False, BASIS_WEAK_HIT,
                          _hits_evidence("仅经营范围命中", weak_hits)
                          + "（弱证据，不足以证明业务实质相关）",
                          weak_hits)

    return GateResult(False, BASIS_MISS, "主营业务/营收构成与主题无实质交集")


def relevance_after_gate(mapping_confidence: float, gate: GateResult) -> float:
    """闸门结果折算成 0~1 的公司级相关性，供打分与全局截断使用。"""
    base = max(0.0, min(1.0, mapping_confidence))
    if gate.basis == BASIS_HIT:
        return round(min(1.0, base + 0.15), 3)
    if gate.basis == BASIS_WEAK_HIT:
        return round(min(base, WEAK_CAP), 3)
    if gate.basis == BASIS_MISS:
        return round(min(base, DEMOTE_CAP), 3)
    return round(base, 3)


def gate_caveat(settings: Settings) -> str:
    if not settings.relevance_gate:
        return "公司级相关性闸门：已关闭（relevance_gate=false）"
    return (f"公司级相关性闸门：{settings.relevance_gate_mode} 模式，"
            f"全局相关性截断 min_relevance={settings.min_relevance}")
