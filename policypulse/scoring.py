"""可解释打分：加权规则，权重外置于 Settings，只输出相对排序。

score-v2：公司级相关性主导（≈0.5），市值流动性降为并列决胜位；
排序键是 (score, relevance, 市值)，不再有「每板块 Top-N」的硬上限，
而是先按全局相关性截断（min_relevance）再取 Top-N。
"""

from __future__ import annotations

from policypulse.config import Settings
from policypulse.extract import Signal
from policypulse.mapping import Candidate

NOVELTY_WEIGHT = {"新增": 1.0, "加码": 0.85, "延续": 0.5, "重申": 0.35, "收紧": 0.2}
SCORE_VERSION = "score-v2"


def liquidity_bucket(market_cap_yi: float) -> float:
    if market_cap_yi <= 0:
        return 0.3  # 无市值数据：排在有市值的小盘之下，不清零
    if market_cap_yi >= 1000:
        return 1.0
    if market_cap_yi >= 300:
        return 0.85
    if market_cap_yi >= 100:
        return 0.7
    if market_cap_yi >= 50:
        return 0.55
    return 0.35


def score_candidate(signal: Signal, cand: Candidate, breadth_norm: float,
                    settings: Settings) -> float:
    w = settings.weights
    strength_norm = signal.strength / 5.0
    # 相关性 = 主题↔板块匹配置信度，再经公司业务画像闸门修正后的值
    relevance = cand.relevance or cand.mapping_confidence
    novelty = NOVELTY_WEIGHT.get(signal.novelty, 0.5)
    raw = (
        w["strength"] * strength_norm
        + w["relevance"] * relevance
        + w["liquidity"] * liquidity_bucket(cand.ticker.market_cap_yi)
        + w["breadth"] * breadth_norm
        + w["novelty"] * novelty
    )
    return round(min(1.0, raw) * 100, 2)


def rank(signal: Signal, candidates: list[Candidate], settings: Settings) -> list[Candidate]:
    """全局相关性截断 + 加权打分排序。

    板块内受益面广度按“该板块贡献的候选占全部候选比例”归一化；
    闸门不通过的候选相关性被压到 DEMOTE_CAP，会被 min_relevance 截断挡在正文之外。
    """
    total = max(len(candidates), 1)
    per_sector: dict[str, int] = {}
    for c in candidates:
        per_sector[c.sector_name] = per_sector.get(c.sector_name, 0) + 1

    scored: list[Candidate] = []
    for cand in candidates:
        if not cand.relevance:
            cand.relevance = cand.mapping_confidence
        breadth = min(1.0, per_sector[cand.sector_name] / max(total, 1) * 3)
        cand.score = score_candidate(signal, cand, breadth, settings)
        scored.append(cand)

    kept = [c for c in scored if c.relevance >= settings.min_relevance]
    kept.sort(key=lambda c: (c.score, c.relevance, c.ticker.market_cap_yi), reverse=True)
    return kept[: settings.max_tickers_per_signal]


def why_of(signal: Signal, cand: Candidate) -> str:
    """一句话受益理由（≤40 字），把主题与板块的差别写出来。"""
    theme = cand.mapping_path.split(" → ")[0]
    if theme == cand.sector_name:
        base = f"主题「{theme}」直接命中{cand.sector_source}，力度{signal.strength}"
    else:
        base = f"主题「{theme}」映射到{cand.sector_name}（{cand.match_type}）"
    if cand.gate_basis == "weak_hit":
        return f"{base}；业务弱证据"
    return base[:40]
