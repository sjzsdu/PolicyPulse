"""preview：日报个股的区间验证（取数、统计、渲染、CLI）全离线用例。

tongstock 用冻结数据的假客户端，不碰网络、不碰 TDX。
"""

from __future__ import annotations

import json
import statistics

import pytest

from policypulse.config import Settings, default_paths
from policypulse.preview import (
    aggregate,
    benchmark_pct,
    build_preview,
    kline_series,
    load_selection,
    portfolio_series,
    render_preview,
    row_stats,
    selection_from_report,
    sparkline,
)


def _bar(day: str, close: float) -> dict:
    return {"time": day, "open": close, "high": close, "low": close,
            "close": close, "volume": 100, "amount": 100.0}


class _FakeClient:
    """冻结日线客户端：按代码返回预置序列，key 走真实 cached 语义的替身。"""

    def __init__(self, series: dict[str, list[dict]]) -> None:
        self.series = series
        self.calls: list[list[str]] = []

    def run(self, args: list[str], timeout: float = 180.0) -> str:
        self.calls.append(list(args))
        if args[0] == "quote":
            return getattr(self, "quote_text", "")
        code = args[args.index("-c") + 1]
        return json.dumps({"code": code, "type": "day",
                           "items": self.series.get(code, [])})

    def cached(self, key, producer, ttl_hours=None):
        return producer()


def _report_payload() -> dict:
    return {
        "events": [
            {"event_id": "ev1", "title": "鼓励储能建设的意见", "section": "受益个股",
             "tickers": [{"ticker_code": "600000", "ticker_name": "甲银行",
                          "sector_name": "银行", "why": "信贷敞口", "score": 3.0}]},
            {"event_id": "ev2", "title": "数据要素行动", "section": "仅概念关联",
             "tickers": [{"ticker_code": "600000", "ticker_name": "甲银行",
                          "sector_name": "银行", "why": "概念沾边", "score": 5.0},
                         {"ticker_code": "000001", "ticker_name": "乙科技",
                          "sector_name": "计算机", "why": "直接受益", "score": 4.0}]},
        ],
        "conclusions": [],
    }


def test_selection_from_report_dedupes_and_keeps_best_mapping():
    sel = selection_from_report(_report_payload())
    assert len(sel) == 2
    row = next(r for r in sel if r["ticker_code"] == "600000")
    assert row["sections"] == ["受益个股", "仅概念关联"]
    assert row["events"] == ["鼓励储能建设的意见", "数据要素行动"]
    assert row["score"] == 5.0
    assert row["sector"] == "银行"


def test_selection_from_report_conclusions_fallback_skips_non_investable():
    payload = {
        "events": [],
        "conclusions": [
            {"investable": True, "summary": "有受益", "signal_id": "s1",
             "tickers": [{"ticker_code": "600000", "ticker_name": "甲"}]},
            {"investable": False, "summary": "泛表述", "signal_id": "s2",
             "tickers": [{"ticker_code": "000002", "ticker_name": "乙"}]},
        ],
    }
    sel = selection_from_report(payload)
    assert [r["ticker_code"] for r in sel] == ["600000"]


def test_load_selection_prefers_report_json(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICYPULSE_HOME", str(tmp_path))
    paths = default_paths()
    paths.reports.mkdir(parents=True, exist_ok=True)
    report = paths.reports / "2026-10-22.json"
    report.write_text(json.dumps(_report_payload(), ensure_ascii=False), encoding="utf-8")

    sel, origin = load_selection(report, paths, "2026-10-22")
    assert len(sel) == 2 and "2026-10-22.json" in origin


def test_load_selection_returns_empty_when_no_report_and_no_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICYPULSE_HOME", str(tmp_path))
    paths = default_paths()
    sel, origin = load_selection(paths.reports / "2000-01-01.json", paths, "2000-01-01")
    assert sel == [] and origin == ""


def test_kline_series_cuts_baseline_at_or_before_start():
    client = _FakeClient({"600000": [
        _bar("2026-10-18", 9.0), _bar("2026-10-19", 9.5),
        _bar("2026-10-21", 10.0), _bar("2026-10-22", 11.0),
    ]})
    items = kline_series(client, "600000", "2026-10-21", "2026-10-23")
    assert [i["time"] for i in items] == ["2026-10-21", "2026-10-22"]
    # 走 --json、按窗口估算 -n
    args = client.calls[0]
    assert args[0] == "kline" and "--json" in args and "-n" in args


def test_kline_series_empty_when_no_data():
    client = _FakeClient({})
    assert kline_series(client, "000000", "2026-10-21", "2026-10-23") == []


def test_row_stats_baseline_and_returns():
    items = [_bar("2026-10-21", 10.0), _bar("2026-10-22", 11.0), _bar("2026-10-23", 12.0)]
    st = row_stats({"ticker_code": "600000", "ticker_name": "甲"}, items)
    assert st["base_date"] == "2026-10-21" and st["base_close"] == 10.0
    assert st["last_date"] == "2026-10-23" and st["last_close"] == 12.0
    assert st["pct"] == 20.0
    assert st["max_pct"] == 20.0 and st["min_pct"] == 0.0
    assert st["pct_series"][0] == 0.0 and st["bars"] == 3


def test_row_stats_none_without_data():
    assert row_stats({"ticker_code": "600000"}, []) is None
    assert row_stats({"ticker_code": "600000"}, [_bar("2026-10-21", 0.0)]) is None


def test_aggregate_math_and_sections():
    rows = [
        {"pct": 10.0, "ticker_code": "A", "ticker_name": "甲", "sections": ["受益个股"]},
        {"pct": -5.0, "ticker_code": "B", "ticker_name": "乙", "sections": ["仅概念关联"]},
        {"pct": 0.0, "ticker_code": "C", "ticker_name": "丙", "sections": ["受益个股"]},
    ]
    agg = aggregate(rows, [{"ticker_code": "D", "ticker_name": "丁"}])
    assert agg["total"] == 4 and agg["priced"] == 3 and agg["missing"] == 1
    assert agg["up"] == 1 and agg["down"] == 1 and agg["flat"] == 1
    assert agg["avg"] == round(statistics.mean([10.0, -5.0, 0.0]), 2)
    assert agg["median"] == 0.0
    assert agg["win_rate"] == 33.3
    assert agg["best"] == {"code": "A", "name": "甲", "pct": 10.0}
    assert agg["worst"] == {"code": "B", "name": "乙", "pct": -5.0}
    assert agg["sections"] == {"受益个股": 2, "仅概念关联": 1}
    assert agg["bench_pct"] is None and agg["excess"] is None


def test_benchmark_pct_best_effort():
    client = _FakeClient({"510300": [_bar("2026-10-21", 4.0), _bar("2026-10-23", 4.4)]})
    assert benchmark_pct(client, "2026-10-21", "2026-10-23") == 10.0
    # 取不到数据 → None，不抛
    assert benchmark_pct(_FakeClient({}), "2026-10-21", "2026-10-23") is None


def test_portfolio_series_equal_weight_and_alignment():
    rows = [
        {"dates": ["2026-10-21", "2026-10-22"], "norm": [100.0, 110.0]},
        {"dates": ["2026-10-22", "2026-10-23"], "norm": [100.0, 90.0]},
    ]
    dates, vals = portfolio_series(rows)
    assert dates == ["2026-10-21", "2026-10-22", "2026-10-23"]
    # 10-21：只有甲已开始；10-22：甲110、乙100 → 105；10-23：甲保持110、乙90 → 100
    assert vals == [100.0, 105.0, 100.0]


def test_sparkline_renders_svg_without_script():
    svg = sparkline([0.0, 1.5, -2.0])
    assert svg.startswith("<svg") and "polyline" in svg
    assert "<script" not in svg
    # 数据不足时给空壳 SVG 而不是崩
    assert sparkline([0.0]).startswith("<svg")


def test_render_preview_html_contract():
    items = [_bar("2026-10-22", 10.0), _bar("2026-10-23", 11.5)]
    sel = {"ticker_code": "600000", "ticker_name": "甲银行",
           "sections": ["受益个股"], "events": ["鼓励储能建设的意见"],
           "sector": "银行", "why": "信贷敞口", "score": 3.0}
    rows = [row_stats(sel, items)]
    agg = aggregate(rows, [])
    agg["bench_pct"] = 1.5
    agg["excess"] = round(agg["avg"] - 1.5, 2)
    page = render_preview("2026-10-22", rows, [], agg,
                          "日报 JSON 2026-10-22.json", "2026-10-23 10:00:00")
    assert "<script" not in page
    assert "600000" in page and "甲银行" in page
    assert "+15.00%" in page
    assert "验证 ·" in page and "口径说明" in page
    assert "沪深300ETF" in page
    assert "红涨绿跌" in page
    assert "<svg" in page  # 组合曲线 + 迷你走势线


def test_build_preview_writes_page_and_opens_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICYPULSE_HOME", str(tmp_path))
    paths = default_paths()
    paths.reports.mkdir(parents=True, exist_ok=True)
    report = paths.reports / "2026-10-22.json"
    report.write_text(json.dumps(_report_payload(), ensure_ascii=False), encoding="utf-8")

    client = _FakeClient({
        "600000": [_bar("2026-10-22", 10.0), _bar("2026-10-23", 11.0)],
        "000001": [_bar("2026-10-22", 20.0), _bar("2026-10-23", 18.0)],
        "510300": [_bar("2026-10-22", 4.0), _bar("2026-10-23", 4.1)],
    })
    opened: list[str] = []
    monkeypatch.setattr("policypulse.preview.webbrowser.open",
                        lambda uri: opened.append(uri))

    out = build_preview("2026-10-22", paths, Settings(), client=client,
                        open_browser=True)
    assert out is not None and out.exists()
    assert out.name == "2026-10-22-preview.html"
    page = out.read_text(encoding="utf-8")
    assert "<script" not in page
    assert "600000" in page and "000001" in page
    assert opened and opened[0].startswith("file://")

    # 排序：甲 +10% 在前，乙 -10% 在后
    assert page.index("甲银行") < page.index("乙科技")
    # 基准 510300 同期 +2.5%，超额 = 0 - 2.5
    assert "+2.50%" in page


def test_live_prices_parses_batch_quote_and_ignores_foreign_codes():
    from policypulse.preview import live_prices

    client = _FakeClient({})
    client.quote_text = (
        "600372 \n  最新价: 10.990\n  涨跌:   -0.160 (-1.43%)\n"
        "510300 \n  最新价: 40.500\n"
        "000001 \n  最新价: 3656.710\n")
    # 只认本次请求里出现的代码：000001 不在请求里，不拿指数价冒充平安银行
    prices = live_prices(client, ["600372", "510300"])
    assert prices == {"600372": 10.99, "510300": 40.5}
    assert live_prices(client, []) == {}


def test_live_prices_returns_empty_when_quote_unavailable():
    from policypulse.preview import live_prices

    class _NoQuote:
        def run(self, args, timeout=180.0):
            raise RuntimeError("boom")

    assert live_prices(_NoQuote(), ["600000"]) == {}


def test_align_price_scale_guard():
    from policypulse.preview import _align_price

    assert _align_price(11.0, 10.0) == 11.0          # 同口径：直接用
    assert _align_price(44.0, 4.0) == 4.4            # quote 是 10 倍价：换算回日线口径
    assert _align_price(405.0, 4.1) == 4.05          # 100 倍价
    assert _align_price(0.44, 4.4) == 4.4            # 0.1 倍价：乘回
    assert _align_price(13.0, 4.0) is None            # 3.25 倍：对不上任何 10^k → 放弃
    assert _align_price(0.0, 4.0) is None
    assert _align_price(4.0, 0.0) is None


def test_build_preview_appends_live_point_when_today_bar_missing(tmp_path, monkeypatch):
    """日线只到信号日（今天还没落库）时，补 quote 盘中价，不能全是 +0.00%。"""
    from datetime import date as _RealDate

    class _FakeDate(_RealDate):
        @classmethod
        def today(cls):
            return cls(2026, 10, 30)

    monkeypatch.setattr("policypulse.preview.date", _FakeDate)
    monkeypatch.setenv("POLICYPULSE_HOME", str(tmp_path))
    paths = default_paths()
    paths.reports.mkdir(parents=True, exist_ok=True)
    report = paths.reports / "2026-10-22.json"
    report.write_text(json.dumps(_report_payload(), ensure_ascii=False), encoding="utf-8")

    client = _FakeClient({
        "600000": [_bar("2026-10-22", 10.0)],
        "000001": [_bar("2026-10-22", 20.0)],
        "510300": [_bar("2026-10-22", 4.0)],
    })
    # 510300 的 quote 是 10 倍价（44.0），需换算回 4.4 → +10%
    client.quote_text = (
        "600000 \n  最新价: 11.000\n"
        "000001 \n  最新价: 19.000\n"
        "510300 \n  最新价: 44.000\n")
    monkeypatch.setattr("policypulse.preview.webbrowser.open", lambda uri: None)

    out = build_preview("2026-10-22", paths, Settings(), client=client,
                        open_browser=False)
    assert out is not None
    page = out.read_text(encoding="utf-8")
    # 甲 +10%、乙 -5%（红涨绿跌的 + / - 都要对）
    assert "+10.00%" in page and "-5.00%" in page
    # 盘中标记 + 基准 510300 同期 +10%，超额 = 2.5 - 10 = -7.5
    assert "盘中" in page
    assert "-7.50%" in page
    # quote 是批量一次调用，不是逐只
    quote_calls = [c for c in client.calls if c[0] == "quote"]
    assert len(quote_calls) == 2  # 个股一批 + 基准一批


def test_sync_live_prices_refreshes_stale_today_stub():
    """当日日线是昨收占位（510300 实况）→ 用盘中价刷新，否则整段纹丝不动。"""
    from policypulse.preview import sync_live_prices

    client = _FakeClient({})
    client.quote_text = "600000 \n  最新价: 11.000\n"
    series = {"600000": [_bar("2026-10-22", 10.0), _bar("2026-10-23", 10.0)]}
    sync_live_prices(client, series, "2026-10-23")
    last = series["600000"][-1]
    assert last["close"] == 11.0 and last["live"] is True
    # 基准那根（10-22）不能被动到
    assert series["600000"][0]["close"] == 10.0


def test_sync_live_prices_never_touches_baseline_day():
    """今天这根就是基准 → 不刷新：否则当天涨跌被吃进基准，区间永远 0%。"""
    from policypulse.preview import sync_live_prices

    client = _FakeClient({})
    client.quote_text = "600000 \n  最新价: 11.000\n"
    series = {"600000": [_bar("2026-10-23", 10.0)]}
    sync_live_prices(client, series, "2026-10-23")
    assert series["600000"][0]["close"] == 10.0
    assert "live" not in series["600000"][0]


def test_sync_live_prices_keeps_kline_when_quote_unavailable():
    from policypulse.preview import sync_live_prices

    class _NoQuote:
        def run(self, args, timeout=180.0):
            raise RuntimeError("boom")

    series = {"600000": [_bar("2026-10-22", 10.0)]}
    sync_live_prices(_NoQuote(), series, "2026-10-23")
    assert series["600000"] == [_bar("2026-10-22", 10.0)]


def test_build_preview_without_report_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICYPULSE_HOME", str(tmp_path))
    paths = default_paths()
    assert build_preview("2000-01-01", paths, Settings(),
                         client=_FakeClient({}), open_browser=False) is None


def test_build_preview_raises_when_all_kline_fails(tmp_path, monkeypatch):
    """行情链路整体挂掉（如 tongstock 不在）必须报错，不能静默出一张空页。"""
    from policypulse.tongstock import TongstockError

    monkeypatch.setenv("POLICYPULSE_HOME", str(tmp_path))
    paths = default_paths()
    paths.reports.mkdir(parents=True, exist_ok=True)
    report = paths.reports / "2026-10-22.json"
    report.write_text(json.dumps(_report_payload(), ensure_ascii=False), encoding="utf-8")

    class _Broken:
        def run(self, args, timeout=180.0):
            raise TongstockError("tongstock not found on PATH")

        def cached(self, key, producer, ttl_hours=None):
            return producer()

    with pytest.raises(TongstockError):
        build_preview("2026-10-22", paths, Settings(), client=_Broken(),
                      open_browser=False)
