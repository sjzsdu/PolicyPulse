from policypulse.config import Settings
from policypulse.extract import Signal
from policypulse.mapping import Candidate
from policypulse.scoring import SCORE_VERSION, liquidity_bucket, rank, score_candidate, why_of
from policypulse.tongstock import Ticker


def _sig(strength=4, novelty="新增"):
    return Signal(signal_id="s1", doc_id="d1", strength=strength, novelty=novelty,
                  direction="利好", confidence=0.8)


def _cand(code, cap, conf=1.0, sector="低空经济"):
    return Candidate(
        ticker=Ticker(code, f"名称{code}", "sh" if code.startswith("6") else "sz", "主板", cap),
        sector_name=sector, sector_source="通达信概念", match_type="exact",
        mapping_confidence=conf,
        mapping_path=f"低空经济 → {sector}(通达信概念) → {code}",
    )


def test_liquidity_buckets_are_monotonic():
    values = [0, 12, 40, 80, 200, 500, 2000]
    buckets = [liquidity_bucket(v) for v in values]
    assert buckets == sorted(buckets)


def test_higher_strength_scores_higher():
    cand = _cand("600001", 500.0)
    assert score_candidate(_sig(5), cand, 0.5, Settings()) > score_candidate(_sig(2), cand, 0.5, Settings())


def test_exact_mapping_outranks_fuzzy():
    exact = _cand("600001", 500.0, conf=1.0)
    fuzzy = _cand("600002", 500.0, conf=0.6)
    ranked = rank(_sig(), [fuzzy, exact], Settings())
    assert ranked[0].ticker.code == "600001"


def test_novelty_weighting():
    cand = _cand("600001", 500.0)
    assert score_candidate(_sig(novelty="新增"), cand, 0.5, Settings()) > \
           score_candidate(_sig(novelty="重申"), cand, 0.5, Settings())


def test_rank_respects_max_tickers_and_assigns_rank():
    cands = [_cand(f"60000{i}", 100.0 * i) for i in range(1, 9)]
    settings = Settings(max_tickers_per_signal=3)
    ranked = rank(_sig(), cands, settings)
    assert len(ranked) == 3
    assert [c.score for c in ranked] == sorted((c.score for c in ranked), reverse=True)


def test_score_version_and_weights():
    """口径变了版本号必须变：score-v2 = 公司级相关性主导 + 全局相关性截断。"""
    import pytest

    assert SCORE_VERSION == "score-v2"
    w = Settings().weights
    assert w["relevance"] == pytest.approx(0.50)
    assert w["strength"] == pytest.approx(0.25)
    assert sum(w.values()) == pytest.approx(1.0)


def test_rank_drops_below_global_relevance_cutoff():
    """闸门把相关性压到 0.25 的个股不能靠市值/分数挤回正文。"""
    kept = _cand("600001", 5000.0, conf=1.0)
    demoted = _cand("600002", 9000.0, conf=1.0)
    demoted.relevance = 0.25
    ranked = rank(_sig(), [kept, demoted], Settings())
    assert [c.ticker.code for c in ranked] == ["600001"]


def test_why_is_short_and_distinct():
    cand = _cand("600001", 500.0)
    assert len(why_of(_sig(), cand)) <= 40
    assert "低空经济" in why_of(_sig(), cand)
