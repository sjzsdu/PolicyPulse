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
        "duplicates_skipped": 0, "sources_no_update": [], "events": 1,
        "gate": {}, "deepdive_count": 0, "min_strength_for_report": 3,
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
        "matched_sectors": ["低空经济"],
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


def test_markdown_keeps_all_out_of_window_warning_details():
    """窗口外剔除改为逐条告警后，明细不能被旧的 15 条展示上限截断。"""
    r = _result()
    r.warnings = [
        f"mofcom_xw: 窗口外剔除 https://example.com/{i}.htm"
        f"（详情页日期 2026-10-14 ∉ [2026-10-19, 2026-10-20]）"
        for i in range(20)
    ]
    md = render_markdown(r)
    shown = [line for line in md.splitlines()
             if line.startswith("- ") and "窗口外剔除" in line]
    assert len(shown) == 20, "20 条剔除明细必须全部可见"
    assert "https://example.com/19.htm" in md, "最后一条也不能被截断"


def test_markdown_deepdive_sources_line_renders():
    """深度研读的溯源行：渲染正确且不依赖 3.12+ 的嵌套同引号 f-string。"""
    from policypulse.deepdive import DeepDive

    r = _result()
    r.deepdives = [DeepDive(
        ticker_code="600001", ticker_name="测试A", policy_title="发展新质生产力",
        policy_summary="加快培育低空经济", policy_quote="加快培育低空经济新增长极",
        policy_url="https://www.gov.cn/a.htm", profile_business="航空装备制造",
        transmission="政策直接利好", competition="龙头地位稳固", impact="订单增加",
        risks=["落地不及预期"],
        sources=[{"label": "政策原文", "ref": "https://www.gov.cn/a.htm"},
                 {"label": "公司画像", "ref": "通达信F10:600001"}],
    )]
    md = render_markdown(r)
    assert ("- 溯源：政策原文（`https://www.gov.cn/a.htm`）; "
            "公司画像（`通达信F10:600001`）") in md
    assert "风险提示：落地不及预期" in md


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


def test_empty_day_reports_nothing_and_keeps_no_fallback_language():
    """第 1 点：兜底抓旧的措辞必须彻底消失；窗口内没稿写「今日无新增政策 / 事件」。"""
    from policypulse.config import EMPTY_DAY_NOTICE

    empty = RunResult(run_id="r0", window_start="2026-10-19", window_end="2026-10-20")
    empty.stats = {
        "docs": 0, "signals": 0, "with_tickers": 0, "ticker_rows": 0, "events": 0,
        "sources_total": 6, "sources_failed": 0, "sources_ok": 6,
        "sources_no_update": ["gov_cn"], "tongstock_calls": 0, "llm_enabled": False,
        "duplicates_skipped": 0, "min_strength_for_report": 3, "gate": {},
    }
    empty.failed_sources = []
    empty.warnings = []
    md = render_markdown(empty)
    assert EMPTY_DAY_NOTICE in md
    assert "窗口外兜底" not in md
    assert "gov_cn" in md, "哪些源没更新要如实写出来"

    # 有产出的报告里同样不能残留兜底措辞
    assert "窗口外兜底" not in render_markdown(_result())


def test_markdown_states_the_company_gate():
    md = render_markdown(_result())
    assert "公司级相关性闸门" in md


def test_markdown_labels_unparsed_profile_gate_rows():
    """闸门统计里「F10 已取未解析」必须与「无画像」分两行标签显示。"""
    r = _result()
    r.stats["gate"] = {"no_profile": 1, "unparsed_profile": 2}
    md = render_markdown(r)
    assert "无画像放行 1" in md
    assert "F10已取未解析放行 2" in md


def _with_compliance(result: RunResult) -> RunResult:
    result.stats.update({
        "pbc_related_docs": 2,
        "pbc_related_sources": ["xinhua_pbc"],
        "sources_detail": [
            {"id": "xinhua_pbc", "org": "中国人民银行（新华网转载）", "tier": "转载",
             "host": "www.news.cn", "adapter": "html_list", "column": "财经 > 央行相关",
             "enabled": True, "robots": "allowed", "robots_reason": "robots.txt 已获取", "docs": 2},
            {"id": "pbc_inbound", "org": "中国人民银行", "tier": "部委",
             "host": "www.pbc.gov.cn", "adapter": "local_inbound", "column": "人工入库",
             "enabled": True, "robots": "本地入库",
             "robots_reason": "不发起网络请求：解析用户自己下载的本地文件", "docs": 0},
            {"id": "pbc_xw", "org": "中国人民银行", "tier": "部委",
             "host": "www.pbc.gov.cn", "adapter": "html_list", "column": "新闻发布",
             "enabled": False, "robots": "未启用",
             "robots_reason": "源已停用，本轮不发起任何请求", "docs": 0},
        ],
    })
    return result


def test_report_shows_pbc_coverage_and_its_source():
    md = render_markdown(_with_compliance(_result()))
    assert "央行（中国人民银行）相关文档" in md
    assert "xinhua_pbc" in md


def test_report_carries_the_per_source_compliance_ledger():
    """合规要看得见：每个源的 robots 判定、停用源不得有任何抓取。"""
    md = render_markdown(_with_compliance(_result()))
    assert "信息源与合规" in md
    assert "pbc_xw" in md and "未启用" in md
    assert "不发起任何请求" in md
    assert "本地入库" in md

    out = render_html(_with_compliance(_result()))
    assert "信息源与合规" in out
    assert "不发起网络请求" in out

    payload = json.loads(render_json(_with_compliance(_result())))
    assert payload["stats"]["sources_detail"][0]["robots"] == "allowed"


def _with_many_tickers(n: int = 8) -> RunResult:
    """一只事件里塞 n 只个股（score 递减），用于核对报告层的选股上限。"""
    r = _result()
    base = dict(r.conclusions[0]["tickers"][0])
    r.conclusions[0]["tickers"] = [
        {**base, "ticker_code": f"6000{i:02d}", "ticker_name": f"测试{i}", "score": 90.0 - i}
        for i in range(1, n + 1)
    ]
    return r


def test_markdown_event_table_is_capped_at_five_picks():
    """5 只上限在报告里同样生效：留下的是 score 最高的 5 只，低分的不出现。"""
    md = render_markdown(_with_many_tickers(8))
    for i in range(1, 6):
        assert f"6000{i:02d}" in md, f"前 5 只里的 6000{i:02d} 必须出现"
    for i in range(6, 9):
        assert f"6000{i:02d}" not in md, f"6000{i:02d} 分数更低，不该出现在日报里"
    assert "选股上限" in md


def test_html_event_table_is_capped_at_five_picks():
    out = render_html(_with_many_tickers(8))
    for i in range(1, 6):
        assert f"6000{i:02d}" in out
    for i in range(6, 9):
        assert f"6000{i:02d}" not in out


def test_json_event_tickers_is_capped_at_five_picks():
    payload = json.loads(render_json(_with_many_tickers(8)))
    tickers = payload["events"][0]["tickers"]
    assert [t["ticker_code"] for t in tickers] == [f"6000{i:02d}" for i in range(1, 6)]
