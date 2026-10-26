from policypulse.config import Settings
from policypulse.extract import Signal
from policypulse.mapping import Candidate
from policypulse.scoring import (
    SCORE_VERSION,
    liquidity_bucket,
    rank,
    score_candidate,
    select_top,
    why_of,
)
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


def test_rank_respects_max_picks_and_assigns_rank():
    cands = [_cand(f"60000{i}", 100.0 * i) for i in range(1, 9)]
    settings = Settings(max_picks=3)
    ranked = rank(_sig(), cands, settings)
    assert len(ranked) == 3
    assert [c.score for c in ranked] == sorted((c.score for c in ranked), reverse=True)


def _scored_cands(n: int):
    """n 只候选，相关性随序号递增 → 打分随序号递增，便于核对「取的是最高的几只」。"""
    return [_cand(f"60000{i}", 100.0 * i, conf=0.5 + 0.05 * i) for i in range(1, n + 1)]


def test_select_top_sorts_before_truncating():
    """上限是「先排序、再截断」：返回的必须是分数最高的前 5 只，且保持降序。"""
    cands = [_cand(f"60000{i}", 100.0) for i in range(1, 8)]
    for cand, score in zip(cands, [40.0, 90.0, 60.0, 10.0, 80.0, 30.0, 70.0]):
        cand.score = score
    top = select_top(cands, 5)
    assert [c.ticker.code for c in top] == ["600002", "600005", "600007", "600003", "600001"]
    assert [c.score for c in top] == [90.0, 80.0, 70.0, 60.0, 40.0]


def test_select_top_returns_actual_count_when_candidates_run_out():
    """候选不足 5 只时按实际数量返回，不补位。"""
    cands = [_cand("600001", 100.0), _cand("600002", 200.0)]
    cands[0].score, cands[1].score = 50.0, 70.0
    top = select_top(cands, 5)
    assert [c.ticker.code for c in top] == ["600002", "600001"]


def test_select_top_zero_limit_picks_nothing():
    cands = [_cand("600001", 100.0)]
    cands[0].score = 50.0
    assert select_top(cands, 0) == []


def test_rank_defaults_to_five_picks_and_keeps_top_five():
    """默认 5 只：rank 返回的就是全量排名最高的 5 只（不是前 5 只里的随机/市值排序）。"""
    assert Settings().max_picks == 5
    full = rank(_sig(), _scored_cands(8), Settings(max_picks=100))
    capped = rank(_sig(), _scored_cands(8), Settings())
    assert len(capped) == 5
    assert [c.ticker.code for c in capped] == [c.ticker.code for c in full[:5]]
    assert [c.score for c in capped] == sorted((c.score for c in capped), reverse=True)


def test_rank_does_not_pad_when_fewer_candidates_than_cap():
    ranked = rank(_sig(), _scored_cands(3), Settings())
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
