"""主题 -> 板块 -> 个股 映射。

主路径：通达信概念板块(block_gn.dat) + 行业板块(block_fg.dat)，经 tongstock CLI 取得。
匹配：精确 -> 别名 -> 包含 -> 模糊(≥0.8)；不达阈值宁缺勿错。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from policypulse.config import Settings, load_theme_alias, load_theme_business
from policypulse.tongstock import CONCEPT_FILE, INDUSTRY_FILE, Ticker

FUZZY_THRESHOLD = 0.80
MATCH_CONFIDENCE = {"exact": 1.0, "alias": 0.9, "contains": 0.7, "fuzzy": 0.6}


@dataclass
class SectorMatch:
    theme: str
    sector_name: str
    sector_source: str  # 通达信概念 / 通达信行业
    block_file: str
    match_type: str
    confidence: float


def _match_one(theme: str, aliases: list[str], sector_names: list[str]) -> tuple[str, str] | None:
    for alias in aliases:
        if alias in sector_names:
            return alias, "exact" if alias == theme else "alias"
    lower_map = {name.lower(): name for name in sector_names}
    for alias in aliases:
        if alias.lower() in lower_map:
            return lower_map[alias.lower()], "alias"
    for alias in aliases:
        for name in sector_names:
            if alias and (alias in name or name in alias):
                return name, "contains"
    best = (None, 0.0)
    for alias in aliases:
        for name in sector_names:
            ratio = difflib.SequenceMatcher(None, alias, name).ratio()
            if ratio > best[1]:
                best = (name, ratio)
    if best[0] and best[1] >= FUZZY_THRESHOLD:
        return best[0], "fuzzy"
    return None


def match_sectors(
    themes: list[str],
    concept_sectors: list[str],
    industry_sectors: list[str],
    alias_map: dict[str, list[str]] | None = None,
) -> list[SectorMatch]:
    alias_map = alias_map if alias_map is not None else load_theme_alias()
    matches: list[SectorMatch] = []
    for theme in themes:
        aliases = alias_map.get(theme, [theme])
        hit = _match_one(theme, aliases, concept_sectors)
        if hit:
            name, how = hit
            matches.append(SectorMatch(theme, name, "通达信概念", CONCEPT_FILE, how,
                                       MATCH_CONFIDENCE[how]))
            continue
        hit = _match_one(theme, aliases, industry_sectors)
        if hit:
            name, how = hit
            matches.append(SectorMatch(theme, name, "通达信行业", INDUSTRY_FILE, how,
                                       MATCH_CONFIDENCE[how]))
    return matches


@dataclass
class Candidate:
    ticker: Ticker
    sector_name: str
    sector_source: str
    match_type: str
    mapping_confidence: float
    mapping_path: str
    # 公司级相关性：主题↔板块的匹配置信度，再经公司业务画像（F10）闸门修正
    relevance: float = 0.0
    gate_basis: str = ""
    gate_evidence: str = ""
    score: float = 0.0


def _passes_filters(tk: Ticker, settings: Settings) -> bool:
    if tk.is_st:
        return False
    if settings.exclude_bj and tk.exchange == "bj":
        return False
    if tk.market_cap_yi and tk.market_cap_yi < settings.min_market_cap_yi:
        return False
    return True


def build_candidates(
    matches: list[SectorMatch],
    client,
    settings: Settings,
    alias_map: dict[str, list[str]] | None = None,
    business_map: dict[str, list[str]] | None = None,
    gate_stats: dict | None = None,
) -> list[Candidate]:
    """展开板块成分股 → 基础过滤 → 去重 → 公司级相关性闸门。

    旧口径「每板块按市值取 Top-N」已移除：那会让结果锁死在板块内再按市值排，
    板块成员再多也只看最大的几只。这里展开全部成员（只留一个算力上界），
    排序交给打分层的「全局相关性截断」。
    """
    from policypulse.profile import load_profile
    from policypulse.relevance import evaluate_gate, relevance_after_gate, theme_terms

    universe = client.tickers()
    picked: dict[str, Candidate] = {}
    for match in matches:
        codes = [c for c in client.members(match.sector_name, match.block_file)
                 if c in universe]
        for code in codes:
            tk = universe[code]
            if not _passes_filters(tk, settings):
                continue
            cand = Candidate(
                ticker=tk,
                sector_name=match.sector_name,
                sector_source=match.sector_source,
                match_type=match.match_type,
                mapping_confidence=match.confidence,
                mapping_path=f"{match.theme} → {match.sector_name}({match.sector_source}) → {code}",
            )
            # 同一只股被多个板块命中时只留映射置信度最高的一条，避免个股表重复行
            prev = picked.get(code)
            if prev is None or cand.mapping_confidence > prev.mapping_confidence:
                picked[code] = cand

    candidates = sorted(picked.values(),
                        key=lambda c: c.ticker.market_cap_yi, reverse=True)
    candidates = candidates[: settings.max_candidates_per_signal]

    if not settings.relevance_gate:
        for cand in candidates:
            cand.relevance = cand.mapping_confidence
            cand.gate_basis = "disabled"
        if gate_stats is not None:
            gate_stats["disabled"] = gate_stats.get("disabled", 0) + len(candidates)
        return candidates

    alias_map = alias_map if alias_map is not None else load_theme_alias()
    business_map = business_map if business_map is not None else load_theme_business()

    kept: list[Candidate] = []
    for cand in candidates:
        theme = cand.mapping_path.split(" → ")[0]
        terms = theme_terms(theme, alias_map, business_map)
        profile = load_profile(client, cand.ticker.code)
        gate = evaluate_gate(terms, profile)
        cand.relevance = relevance_after_gate(cand.mapping_confidence, gate)
        cand.gate_basis = gate.basis
        cand.gate_evidence = gate.evidence
        if gate_stats is not None:
            gate_stats[gate.basis] = gate_stats.get(gate.basis, 0) + 1
        if not gate.passed and settings.relevance_gate_mode == "drop":
            if gate_stats is not None:
                gate_stats["dropped"] = gate_stats.get("dropped", 0) + 1
            continue
        kept.append(cand)
    return kept
