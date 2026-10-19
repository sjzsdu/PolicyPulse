"""主题 -> 板块 -> 个股 映射。

主路径：通达信概念板块(block_gn.dat) + 行业板块(block_fg.dat)，经 tongstock CLI 取得。
匹配：精确 -> 别名 -> 包含 -> 模糊(≥0.8)；不达阈值宁缺勿错。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from policypulse.config import Settings, load_theme_alias
from policypulse.tongstock import CONCEPT_FILE, INDUSTRY_FILE, Ticker, TongstockClient

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
    client: TongstockClient,
    settings: Settings,
) -> list[Candidate]:
    """展开板块成分股并做基础过滤；每个板块截断到 Top-N（按市值）。"""
    universe = client.tickers()
    candidates: list[Candidate] = []
    for match in matches:
        codes = [c for c in client.members(match.sector_name, match.block_file)
                 if c in universe]
        picked: list[Candidate] = []
        for code in codes:
            tk = universe[code]
            if not _passes_filters(tk, settings):
                continue
            picked.append(
                Candidate(
                    ticker=tk,
                    sector_name=match.sector_name,
                    sector_source=match.sector_source,
                    match_type=match.match_type,
                    mapping_confidence=match.confidence,
                    mapping_path=f"{match.theme} → {match.sector_name}({match.sector_source}) → {code}",
                )
            )
        picked.sort(key=lambda c: c.ticker.market_cap_yi, reverse=True)
        candidates.extend(picked[: settings.top_n_per_sector])
    return candidates
