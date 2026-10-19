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


def test_score_version_is_stable():
    assert SCORE_VERSION == "score-v1"


def test_why_is_short_and_distinct():
    cand = _cand("600001", 500.0)
    assert len(why_of(_sig(), cand)) <= 40
    assert "低空经济" in why_of(_sig(), cand)
