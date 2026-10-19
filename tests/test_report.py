import json
from pathlib import Path

from policypulse.config import MAPPING_CAVEAT
from policypulse.pipeline import RunResult
from policypulse.report import render_html, render_json, render_markdown


def _result() -> RunResult:
    r = RunResult(run_id="r1", window_start="2026-10-06", window_end="2026-10-19")
    r.stats = {
        "docs": 3, "signals": 4, "with_tickers": 1, "ticker_rows": 2,
        "sources_total": 6, "sources_failed": 1, "tongstock_calls": 5, "llm_enabled": False,
        "duplicates_skipped": 0, "out_of_window_docs": 0, "min_strength_for_report": 3,
    }
    r.failed_sources = ["miit: timeout"]
    r.conclusions = [{
        "signal_id": "doc-1#1", "doc_id": "doc-1", "summary": "加快培育低空经济",
        "direction": "利好", "strength": 4, "horizon": "中期(1–6月)", "novelty": "新增",
        "instruments": ["产业扶持"], "theme_tags": ["低空经济"], "confidence": 0.82,
        "quote": "加快培育低空经济新增长极", "quote_verified": True,
        "source_url": "https://www.gov.cn/a.htm", "publisher": "国务院", "org": "国务院",
        "meeting_type": "国务院常务会议", "published_at": "2026-10-09",
        "fetched_at": "2026-10-19T10:00:00+0800", "content_hash": "abc",
        "raw_snapshot_path": str(Path(__file__).resolve().parents[1] / "data" / "snapshots" / "r1" / "doc-1.html"),
        "model": "rules-v1", "prompt_version": "rules-v1",
        "matched_sectors": ["低空经济"], "out_of_window_fallback": False,
        "tickers": [{
            "ticker_code": "600001", "ticker_name": "测试A", "exchange": "sh", "board": "沪市主板",
            "market_cap": 500.0, "sector_name": "低空经济", "sector_source": "通达信概念",
            "why": "主题「低空经济」直接命中", "score": 82.0, "rank": 1,
            "mapping_path": "低空经济 → 低空经济(通达信概念) → 600001", "mapping_confidence": 1.0,
        }],
    }]
    return r


def test_markdown_carries_disclaimer_and_source():
    md = render_markdown(_result())
    assert "不构成任何投资建议" in md
    assert "https://www.gov.cn/a.htm" in md
    assert "600001" in md
    assert "抓取告警" in md


def test_markdown_lists_signal_traceability_fields():
    md = render_markdown(_result())
    for field in ("信号ID", "引用校验", "快照", "映射路径"):
        assert field in md


def test_json_is_parseable_and_complete():
    import json

    payload = json.loads(render_json(_result()))
    assert payload["run_id"] == "r1"
    assert payload["conclusions"][0]["tickers"][0]["ticker_code"] == "600001"
    assert payload["disclaimer"]


def test_html_escapes_and_links_source():
    hostile = _result()
    hostile.conclusions[0]["tickers"][0]["ticker_name"] = "<script>alert(1)</script>名字"
    out = render_html(hostile)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "https://www.gov.cn/a.htm" in out


def test_html_strength_filter_and_caveat():
    """HTML 与 Markdown 口径一致：力度不足的不进正文，口径说明必须在顶部。"""
    r = _result()
    r.conclusions.append({**r.conclusions[0], "signal_id": "doc-1#2", "strength": 1})
    out = render_html(r)
    assert MAPPING_CAVEAT in out
    assert out.count("<h3>") == 1, "strength=1 的那条不应出现在 HTML 正文"


def test_markdown_strength_filter_reports_count():
    r = _result()
    r.conclusions.append({**r.conclusions[0], "signal_id": "doc-1#2", "strength": 1})
    md = render_markdown(r)
    assert "另有 1 条利好结论力度 < 3" in md
    assert MAPPING_CAVEAT in md


def test_markdown_snapshot_path_is_relative(tmp_path):
    """Markdown 里不能出现本机绝对路径；JSON 里保留绝对路径。"""
    md = render_markdown(_result())
    assert "/Users/" not in md
    assert "data/snapshots/" in md
    payload = json.loads(render_json(_result()))
    assert Path(payload["conclusions"][0]["raw_snapshot_path"]).is_absolute()


def test_out_of_window_fallback_is_marked():
    r = _result()
    r.conclusions[0]["out_of_window_fallback"] = True
    r.stats["out_of_window_docs"] = 1
    md = render_markdown(r)
    assert "窗口外兜底" in md
    assert "来自「栏目窗口内无更新」的兜底抓取" in md
