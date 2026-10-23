"""preview：验证环节——某天日报选出的个股，从那天到今天的股价走势 + 汇总统计。

    policypulse preview --date 2026-10-22

取数链：日报 JSON（事件与个股；缺文件时回退库内同窗口的最近一次 run）
      → tongstock 日线（个股 + 沪深300ETF 作基准，均带磁盘缓存）
      → 每股区间统计 + 等权组合汇总
      → 单页 HTML（服务端渲染 SVG，无 <script>）→ 默认自动打开浏览器。

基准价口径：**不晚于信号日的最后一根日线收盘价**。政策多在盘后发布，
拿发布前最后收盘当基准，区间涨跌幅才是「消息出来之后市场给了多少」。
涨跌配色按 A 股惯例：红涨绿跌。
"""

from __future__ import annotations

import html
import json
import re
import statistics
import time
import webbrowser
from collections import Counter
from datetime import date
from pathlib import Path

from policypulse.config import DISCLAIMER, Settings, default_paths
from policypulse.store import Store
from policypulse.tongstock import TongstockClient, TongstockError

# 大盘基准：沪深300ETF。指数代码在本机 tongstock 上取不到日线（index 命令不存在、
# 指数 kline 会挂起），510300 是同一市场口径下能稳定取到的替代物。
BENCHMARK_CODE = "510300"
BENCHMARK_NAME = "沪深300ETF"

# 涨跌配色（A 股惯例：红涨绿跌）
UP_COLOR = "#d03535"
DN_COLOR = "#12805a"


def _esc(text) -> str:
    return html.escape(str(text or ""))


def _pct(value) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def _cls(value) -> str:
    if value is None or value == 0:
        return "flat"
    return "up" if value > 0 else "dn"


# ---------------------------------------------------------------- 取数：当日选中的个股

def selection_from_report(payload: dict) -> list[dict]:
    """日报 JSON → 个股清单（跨事件按代码去重，保留分区与最高分映射）。"""
    out: dict[str, dict] = {}

    def add(t: dict, section: str, event: str) -> None:
        code = str(t.get("ticker_code") or t.get("code") or "")
        if not code:
            return
        row = out.setdefault(code, {
            "ticker_code": code,
            "ticker_name": str(t.get("ticker_name") or t.get("name") or ""),
            "sections": [], "events": [],
            "sector": "", "why": "", "score": 0.0,
        })
        if not row["ticker_name"]:
            row["ticker_name"] = str(t.get("ticker_name") or t.get("name") or "")
        if section and section not in row["sections"]:
            row["sections"].append(section)
        if event and event not in row["events"]:
            row["events"].append(event)
        score = float(t.get("score") or 0.0)
        if score > row["score"]:
            row["score"] = score
            row["sector"] = str(t.get("sector_name") or "")
            row["why"] = str(t.get("why") or "")

    for ev in payload.get("events", []):
        section = str(ev.get("section") or "")
        title = str(ev.get("title") or "")
        for t in ev.get("tickers", []):
            add(t, section, title)
    if not out:
        # 老版 JSON 没有 events：从 conclusions 的个股展开兜底
        for c in payload.get("conclusions", []):
            if not c.get("investable", True):
                continue
            for t in c.get("tickers", []):
                add(t, "", str(c.get("summary") or ""))
    return list(out.values())


def selection_from_store(store: Store, run_id: str) -> list[dict]:
    """库内兜底：该 run 的可投资信号展开行（非投资信号的展开行不进验证清单）。"""
    out: dict[str, dict] = {}
    for sig in store.signals_for_run(run_id):
        if sig["investable"] is not None and not sig["investable"]:
            continue
        for t in store.tickers_for_signal(sig["signal_id"]):
            code = str(t["code"] or "")
            if not code:
                continue
            row = out.setdefault(code, {
                "ticker_code": code, "ticker_name": str(t["name"] or ""),
                "sections": [], "events": [], "sector": "", "why": "", "score": 0.0,
            })
            if not row["ticker_name"]:
                row["ticker_name"] = str(t["name"] or "")
            if str(t["sector_name"] or "") and not row["sector"]:
                row["sector"] = str(t["sector_name"])
            score = float(t["score"] or 0.0)
            if score > row["score"]:
                row["score"] = score
                row["why"] = str(t["why"] or "")
            summary = str(sig["summary"] or "")
            if summary and summary not in row["events"]:
                row["events"].append(summary)
    return list(out.values())


def load_selection(report_json: Path, paths, day: str) -> tuple[list[dict], str]:
    """(个股清单, 取数来源描述)。优先日报 JSON，缺文件回退库内同窗口最近 run。"""
    if report_json is not None and report_json.exists():
        payload = json.loads(report_json.read_text(encoding="utf-8"))
        sel = selection_from_report(payload)
        if sel:
            return sel, f"日报 JSON {report_json.name}"
    store = Store(paths.db)
    try:
        run_id = store.latest_run_for(day) or store.latest_run_for(day, status=None)
        if run_id:
            sel = selection_from_store(store, run_id)
            if sel:
                return sel, f"库内 run {run_id}"
    finally:
        store.close()
    return [], ""


# ---------------------------------------------------------------- 取数：日线

def kline_series(client, code: str, start: str, end: str,
                 timeout: float = 90.0) -> list[dict]:
    """[基准点 … end] 的日线；基准点 = 不晚于 start 的最后一根。

    `-n` 按日历天数 ×2 + 40 估算（交易日约为日历天的 7 成，留足节假日余量），
    结果带 12 小时磁盘缓存——同一天反复 preview 不重复打 TDX。
    """
    days = max((date.fromisoformat(end) - date.fromisoformat(start)).days, 1)
    n = min(1000, max(60, days * 2 + 40))
    key = f"preview_kline_{code}_{start}_{end}_{n}"

    def _load():
        raw = client.run(["kline", "-c", code, "-t", "day", "-n", str(n), "--json"],
                         timeout=timeout)
        payload = json.loads(raw)
        items = payload.get("items") or []
        # 只保留 end 之前的数据（防御：缓存里混入未来/陈旧条目时以窗口为准）
        return [i for i in items if str(i.get("time") or "") <= end]

    items = list(client.cached(key, _load, ttl_hours=12) or [])
    items.sort(key=lambda i: str(i.get("time") or ""))
    if not items:
        return []
    idx = 0
    for i, it in enumerate(items):
        if str(it.get("time") or "") <= start:
            idx = i
        else:
            break
    return items[idx:]


def row_stats(sel: dict, items: list[dict]) -> dict | None:
    """单只股票的区间统计；items 为空返回 None（无行情，由调用方单列）。"""
    if not items:
        return None
    closes = [(str(it["time"]), float(it["close"])) for it in items
              if it.get("close") is not None]
    if not closes or closes[0][1] <= 0:
        return None
    base_close = closes[0][1]
    last_date, last_close = closes[-1]
    norm = [c / base_close * 100.0 for _, c in closes]
    return {
        **sel,
        "base_date": closes[0][0],
        "base_close": round(base_close, 3),
        "last_date": last_date,
        "last_close": round(last_close, 3),
        "pct": round((last_close / base_close - 1) * 100, 2),
        "max_pct": round(max(norm) - 100, 2),
        "min_pct": round(min(norm) - 100, 2),
        "bars": len(closes),
        "dates": [d for d, _ in closes],
        "norm": [round(v, 3) for v in norm],
        "pct_series": [round(v - 100, 3) for v in norm],
        # 基准日之后没有新交易日（数据只到基准日当天）：口径上要能看出来
        "stale": last_date == closes[0][0] and len(closes) == 1,
        # 最新点是当日盘中 `quote` 最新价（日线未落库），不是日线收盘
        "live": bool(items[-1].get("live")),
    }


def aggregate(rows: list[dict], missing: list[dict] | None = None) -> dict:
    """等权汇总：胜率 / 平均 / 中位 / 最佳 / 最差 / 分区分布。"""
    pcts = sorted(float(r["pct"]) for r in rows)
    up = sum(1 for p in pcts if p > 0)
    down = sum(1 for p in pcts if p < 0)
    agg: dict = {
        "total": len(rows) + len(missing or []),
        "priced": len(rows),
        "missing": len(missing or []),
        "up": up, "down": down, "flat": len(pcts) - up - down,
        "avg": round(statistics.mean(pcts), 2) if pcts else None,
        "median": round(statistics.median(pcts), 2) if pcts else None,
        "win_rate": round(up / len(pcts) * 100, 1) if pcts else None,
        "best": None, "worst": None,
        "bench_pct": None, "excess": None,
        "sections": {},
    }
    if rows:
        best = max(rows, key=lambda r: r["pct"])
        worst = min(rows, key=lambda r: r["pct"])
        agg["best"] = {"code": best["ticker_code"], "name": best["ticker_name"],
                       "pct": best["pct"]}
        agg["worst"] = {"code": worst["ticker_code"], "name": worst["ticker_name"],
                        "pct": worst["pct"]}
        counter: Counter = Counter()
        for r in rows:
            for s in (r.get("sections") or ["未分区"]):
                counter[s] += 1
        agg["sections"] = dict(counter.most_common())
    if agg["avg"] is not None and agg["bench_pct"] is not None:
        agg["excess"] = round(agg["avg"] - agg["bench_pct"], 2)
    return agg


def benchmark_pct(client, start: str, end: str) -> float | None:
    """基准区间涨跌幅；取不到就降级为 None，绝不拦 preview 主流程。"""
    try:
        items = kline_series(client, BENCHMARK_CODE, start, end, timeout=60.0)
        if not items:
            return None
        sync_live_prices(client, {BENCHMARK_CODE: items}, end)
        base = float(items[0]["close"])
        last = float(items[-1]["close"])
        if base <= 0:
            return None
        return round((last / base - 1) * 100, 2)
    except Exception:  # noqa: BLE001 - 基准是加分项，任何失败都不拦主流程
        return None


_QUOTE_HEAD_RE = re.compile(r"^(\d{6})\b")
_QUOTE_PRICE_RE = re.compile(r"最新价:\s*([0-9.]+)")


def live_prices(client, codes: list[str], timeout: float | None = None) -> dict[str, float]:
    """批量取当日最新价（日线未落库时用 quote 补「今天」这个点）。

    解析 `quote <code…>` 的文本块：`\n600372 \n  最新价: 10.990\n…`。
    只认本次请求里出现的代码，避免把别的代码误认成同号前缀。

    实测：quote 要连 TDX 实时行情并逐码同步（11 码约 55s），超时按码数给足：
    60s + 12s/码，上限 180s——超时返回 {} 退回只用日线，不拦主流程。
    """
    wanted = {str(c) for c in codes if c}
    if not wanted:
        return {}
    if timeout is None:
        timeout = min(180.0, 60.0 + 12.0 * len(wanted))
    try:
        text = client.run(["quote", *sorted(wanted)], timeout=timeout)
    except Exception:  # noqa: BLE001 - 拿不到盘中价就退回只用日线，不拦主流程
        return {}
    out: dict[str, float] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        head = _QUOTE_HEAD_RE.match(stripped)
        if head:
            current = head.group(1) if head.group(1) in wanted else None
            continue
        if current:
            price = _QUOTE_PRICE_RE.search(stripped)
            if price:
                try:
                    value = float(price.group(1))
                except ValueError:
                    value = 0.0
                if value > 0:
                    out[current] = value
                current = None
    return out


def _align_price(quote_price: float, ref_close: float) -> float | None:
    """把 quote 最新价对齐到日线价格口径。

    本机 tongstock 对部分品种（如 510300）的 quote 返回的是 10 倍价，直接混进
    日线序列会把走势彻底算错。对得上某个 10^k（±30% 盘中波幅）就换算回同一
    口径；对不上宁可不要这个点，也不能把错价写进走势。
    """
    if quote_price <= 0 or ref_close <= 0:
        return None
    ratio = quote_price / ref_close
    for k in (-2, -1, 0, 1, 2):
        factor = 10.0 ** k
        if factor * 0.7 <= ratio <= factor * 1.3:
            return round(quote_price / factor, 3)
    return None


def sync_live_prices(client, series_map: dict[str, list[dict]], today: str) -> None:
    """把每条序列的「今天」对齐到 quote 盘中价（原地修改，带 `live` 标记）。

    - 日线还没到 today（盘中/次日凌晨，股票常见）→ 追加一个 live 点；
    - 已有 today 这根但不是基准（部分品种如 510300 会先放一根**昨收占位**）→
      用对得上口径的 quote 价刷新收盘，否则整段区间会看起来纹丝不动；
    - today 这根就是基准 → 不动：基准必须是信号发布前的价格，刷新会把当天
      涨跌吃进基准里；
    - quote 取不到 / 口径对不上 → 一律保持日线原样（宁缺毋错）。
    """
    codes = [c for c, items in series_map.items() if items]
    quotes = live_prices(client, codes)
    for code in codes:
        price = quotes.get(code)
        if price is None:
            continue
        items = series_map[code]
        last = items[-1]
        aligned = _align_price(price, float(last.get("close") or 0))
        if aligned is None:
            continue
        last_time = str(last.get("time") or "")
        if last_time < today:
            items.append({"time": today, "open": aligned, "high": aligned,
                          "low": aligned, "close": aligned, "volume": 0,
                          "amount": 0.0, "live": True})
        elif last_time == today and len(items) > 1:
            if abs(aligned - float(last.get("close") or 0)) > 1e-9:
                items[-1] = {**last, "close": aligned, "live": True}


def portfolio_series(rows: list[dict]) -> tuple[list[str], list[float]]:
    """等权组合归一化曲线（各自基准 = 100），按交易日并集前进推算均值。"""
    dates = sorted({d for r in rows for d in r["dates"]})
    if not dates:
        return [], []
    maps = [dict(zip(r["dates"], r["norm"])) for r in rows]
    current: list[float | None] = [None] * len(maps)
    out_dates: list[str] = []
    out_vals: list[float] = []
    for d in dates:
        vals = []
        for i, m in enumerate(maps):
            if d in m:
                current[i] = m[d]
            if current[i] is not None:
                vals.append(current[i])
        if vals:
            out_dates.append(d)
            out_vals.append(round(statistics.mean(vals), 3))
    return out_dates, out_vals


# ---------------------------------------------------------------- SVG（服务端渲染，无脚本）

def sparkline(pcts: list[float], width: int = 190, height: int = 40) -> str:
    """迷你走势线（相对基准的涨跌%），带零轴；配色红涨绿跌。"""
    if len(pcts) < 2:
        return (f"<svg class='spark' width='{width}' height='{height}' "
                f"viewBox='0 0 {width} {height}' aria-hidden='true'></svg>")
    lo, hi = min(min(pcts), 0.0), max(max(pcts), 0.0)
    if hi - lo < 1e-9:
        hi = lo + 1.0
    pad = 4
    def x(i: int) -> float:
        return pad + i * (width - 2 * pad) / (len(pcts) - 1)
    def y(v: float) -> float:
        return pad + (hi - v) * (height - 2 * pad) / (hi - lo)
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(pcts))
    zero_y = y(0.0)
    color = UP_COLOR if pcts[-1] >= 0 else DN_COLOR
    return (
        f"<svg class='spark' width='{width}' height='{height}' "
        f"viewBox='0 0 {width} {height}' aria-hidden='true'>"
        f"<line x1='{pad}' y1='{zero_y:.1f}' x2='{width - pad}' y2='{zero_y:.1f}' "
        "stroke='#dbe0e9' stroke-width='1'/>"
        f"<polyline points='{pts}' fill='none' stroke='{color}' stroke-width='1.8' "
        "stroke-linejoin='round' stroke-linecap='round'/></svg>"
    )


def portfolio_chart(dates: list[str], values: list[float],
                    width: int = 1040, height: int = 250) -> str:
    """组合等权走势（基准 = 100）：面积图 + 零轴（=100）+ 坐标标注。"""
    if len(values) < 2 or not dates:
        return "<p class='small'>走势数据不足，无法绘制组合曲线（可能只有一个交易日）。</p>"
    lo, hi = min(min(values), 100.0), max(max(values), 100.0)
    span = (hi - lo) or 1.0
    lo -= span * 0.10
    hi += span * 0.10
    pad_l, pad_r, pad_t, pad_b = 52, 16, 16, 30

    def x(i: int) -> float:
        return pad_l + i * (width - pad_l - pad_r) / (len(values) - 1)

    def y(v: float) -> float:
        return pad_t + (hi - v) * (height - pad_t - pad_b) / (hi - lo)

    line = "M" + " L".join(f"{x(i):.1f} {y(v):.1f}" for i, v in enumerate(values))
    area = (f"{line} L{x(len(values) - 1):.1f} {y(lo):.1f} "
            f"L{x(0):.1f} {y(lo):.1f} Z")
    base_y = y(100.0)
    color = UP_COLOR if values[-1] >= 100 else DN_COLOR
    last_x, last_y = x(len(values) - 1), y(values[-1])
    mid = len(values) // 2
    return f"""<svg class='bigchart' viewBox='0 0 {width} {height}' role='img'
aria-label='组合等权走势，基准 100'>
<defs><linearGradient id='pgrad' x1='0' y1='0' x2='0' y2='1'>
<stop offset='0%' stop-color='{color}' stop-opacity='0.20'/>
<stop offset='100%' stop-color='{color}' stop-opacity='0.02'/>
</linearGradient></defs>
<line x1='{pad_l}' y1='{base_y:.1f}' x2='{width - pad_r}' y2='{base_y:.1f}'
 stroke='#c3cad6' stroke-width='1' stroke-dasharray='5 4'/>
<text x='{pad_l - 8}' y='{base_y + 4:.1f}' text-anchor='end' class='axl'>100</text>
<text x='{pad_l - 8}' y='{y(hi - span * 0.10) + 4:.1f}' text-anchor='end' class='axl'>{hi - 100:+.1f}%</text>
<text x='{pad_l - 8}' y='{y(lo + span * 0.10) + 4:.1f}' text-anchor='end' class='axl'>{lo + 100 - 100:+.1f}%</text>
<path d='{area}' fill='url(#pgrad)'/>
<path d='{line}' fill='none' stroke='{color}' stroke-width='2.4'
 stroke-linejoin='round' stroke-linecap='round'/>
<circle cx='{last_x:.1f}' cy='{last_y:.1f}' r='4' fill='{color}'/>
<text x='{last_x:.1f}' y='{last_y - 10:.1f}' text-anchor='end' class='axl'
 fill='{color}'>{values[-1] - 100:+.2f}%</text>
<text x='{x(0):.1f}' y='{height - 8}' class='axl'>{_esc(dates[0])}</text>
<text x='{x(mid):.1f}' y='{height - 8}' text-anchor='middle' class='axl'>{_esc(dates[mid])}</text>
<text x='{x(len(values) - 1):.1f}' y='{height - 8}' text-anchor='end' class='axl'>{_esc(dates[-1])}</text>
</svg>"""


# ---------------------------------------------------------------- 渲染

_PV_CSS = """
:root{--bg:#f3f5f9;--paper:#fff;--ink:#182230;--muted:#667085;--line:#e4e8ef;
--navy:#132439;--gold:#b98a13;--up:#d03535;--dn:#12805a;--blue:#1d4ed8;
--amber:#b45309;--gray:#667085}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-size:15px;line-height:1.7;
font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Helvetica,Arial,"Microsoft YaHei",sans-serif}
.paper{max-width:1180px;margin:0 auto;padding:26px 20px 64px}
.hero{background:linear-gradient(135deg,#101c30 0%,#1c3352 55%,#274b74 100%);color:#f5f8ff;
border-radius:18px;padding:30px 34px;box-shadow:0 14px 34px rgba(16,28,48,.28)}
.hero h1{margin:0 0 6px;font-size:27px;letter-spacing:.5px}
.hero .sub{color:#bfd2ea;font-size:14px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0 4px}
.kpi{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.16);border-radius:12px;padding:12px 14px}
.kpi .v{font-size:24px;font-weight:700;line-height:1.25}
.kpi .k{font-size:12.5px;color:#c8d8ef;margin-top:2px}
.card{background:var(--paper);border:1px solid var(--line);border-radius:16px;padding:22px 26px;
margin:18px 0;box-shadow:0 2px 10px rgba(16,24,40,.05)}
h2{font-size:20px;margin:6px 0 12px;padding-left:12px;border-left:5px solid var(--navy)}
.meta{color:var(--muted);font-size:13px}
.small{font-size:12.5px;color:var(--muted)}
.up{color:var(--up)} .dn{color:var(--dn)} .flat{color:var(--muted)}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;margin:10px 0 8px;font-size:14px;background:#fff}
th,td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:middle}
th{background:#0f2036;color:#dce7f5;font-weight:600;white-space:nowrap;position:sticky;top:0;
font-size:13px;letter-spacing:.3px}
tbody tr:nth-child(even){background:#f8fafc}
tbody tr:hover{background:#eef4fd}
td code{font-size:12.5px;background:#eef1f6;padding:1px 5px;border-radius:5px}
.tag{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;
border:1px solid transparent;margin-right:5px;white-space:nowrap}
.t-bullish{background:#fdecec;color:#a52828;border-color:#f3c9c9}
.t-concept{background:#eef3ff;color:#2b4bb7;border-color:#cfdbf7}
.t-pending{background:#fdf6e7;color:#8a6212;border-color:#ecd9a8}
.t-other{background:#f2f4f7;color:#667085;border-color:#e4e7ec}
.caveat{background:#fdf9ec;border:1px solid #ead9a6;border-radius:12px;padding:11px 15px;
color:#6d5716;font-size:13.5px;margin:8px 0}
.disc{background:#fff5f5;border:1px solid #f2c9c9;border-radius:12px;padding:12px 16px;
color:#8f2727;font-size:13.5px}
.pills{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
.bigchart{width:100%;height:auto;display:block}
.axl{font-size:11.5px;fill:#7b8698}
.spark{display:block}
.badge{font-size:12px;color:var(--muted);margin-left:6px}
footer{margin-top:26px}
@media print{body{background:#fff}.card{box-shadow:none}}
"""

_SECTION_CLASS = {
    "受益个股": "t-bullish",
    "仅概念关联": "t-concept",
    "待判定：无实质传导证据": "t-pending",
    "非投资信号": "t-other",
}


def _tags(row: dict) -> str:
    sections = row.get("sections") or []
    if not sections:
        return "<span class='tag t-other'>未分区</span>"
    return "".join(f"<span class='tag {_SECTION_CLASS.get(s, 't-other')}'>{_esc(s)}</span>"
                   for s in sections)


def render_preview(day: str, rows: list[dict], missing: list[dict], agg: dict,
                   origin: str, generated_at: str) -> str:
    """单页验证报告：KPI + 组合曲线 + 排行表 + 口径说明（服务端渲染，无脚本）。"""
    latest = max((r["last_date"] for r in rows), default=day)
    bench = agg.get("bench_pct")
    excess = agg.get("excess")

    kpis = [
        (f"{agg['priced']} / {agg['total']}", "股票（有行情 / 全部）"),
        (f"{agg['up']} 涨 · {agg['down']} 跌", f"胜率 {agg['win_rate'] if agg['win_rate'] is not None else '—'}%"),
        (_pct(agg["avg"]), "平均涨跌（等权）"),
        (_pct(agg["median"]), "中位数"),
        (f"{_pct(agg['best']['pct'])}" if agg["best"] else "—",
         f"最佳 {agg['best']['name'] if agg['best'] else ''}"),
        (f"{_pct(agg['worst']['pct'])}" if agg["worst"] else "—",
         f"最差 {agg['worst']['name'] if agg['worst'] else ''}"),
        (_pct(excess) if excess is not None else "—",
         f"超额 vs {_esc(BENCHMARK_NAME)}" if bench is not None
         else f"基准 {_esc(BENCHMARK_NAME)} 无数据"),
    ]
    kpi_html = "".join(
        f"<div class='kpi'><div class='v {'up' if k == '平均涨跌（等权）' and (agg['avg'] or 0) > 0 else ''}"
        f"{' dn' if k == '平均涨跌（等权）' and (agg['avg'] or 0) < 0 else ''}'>{v}</div>"
        f"<div class='k'>{k}</div></div>"
        for v, k in kpis)

    p_dates, p_vals = portfolio_series(rows)
    chart = portfolio_chart(p_dates, p_vals)

    section_pills = "".join(
        f"<span class='tag {_SECTION_CLASS.get(s, 't-other')}'>{_esc(s)} · {n} 只</span>"
        for s, n in (agg.get("sections") or {}).items())

    body_rows = []
    for i, r in enumerate(rows, start=1):
        stale = "<span class='badge'>仅基准日一根K线</span>" if r.get("stale") else ""
        live_mark = "<span class='badge'>盘中</span>" if r.get("live") else ""
        body_rows.append(
            f"<tr><td>{i}</td>"
            f"<td><code>{_esc(r['ticker_code'])}</code></td>"
            f"<td><b>{_esc(r['ticker_name'])}</b>{stale}</td>"
            f"<td>{_tags(r)}</td>"
            f"<td class='small'>{_esc(' · '.join(r.get('events') or [])) or '—'}<br>"
            f"{_esc(r.get('sector') or '')}</td>"
            f"<td>{_esc(r['base_date'])}<br><span class='small'>{r['base_close']:.2f}</span></td>"
            f"<td>{_esc(r['last_date'])}{live_mark}<br><span class='small'>{r['last_close']:.2f}</span></td>"
            f"<td class='{_cls(r['pct'])}'><b>{_pct(r['pct'])}</b></td>"
            f"<td class='{_cls(r['max_pct'])}'>{_pct(r['max_pct'])}</td>"
            f"<td class='{_cls(r['min_pct'])}'>{_pct(r['min_pct'])}</td>"
            f"<td>{sparkline(r['pct_series'])}</td></tr>")

    missing_rows = ""
    if missing:
        cells = "".join(
            f"<tr><td><code>{_esc(m['ticker_code'])}</code></td>"
            f"<td>{_esc(m.get('ticker_name') or '')}</td>"
            f"<td class='small'>{_esc(' · '.join(m.get('events') or [])) or '—'}<br>"
            "日线取数失败或无数据（未计入统计）</td></tr>"
            for m in missing)
        missing_rows = (
            "<h2>无行情数据（不计入统计）</h2><div class='tablewrap'><table>"
            "<thead><tr><th>代码</th><th>名称</th><th>说明</th></tr></thead>"
            f"<tbody>{cells}</tbody></table></div>")

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PolicyPulse 验证 · {_esc(day)}</title>
<style>{_PV_CSS}</style></head><body><div class="paper">
<header class="hero">
<h1>验证 · <span style="color:#ffd97a">{_esc(day)}</span> 日报个股走势</h1>
<p class="sub">信号日 {_esc(day)} → 最新交易日 {_esc(latest)} · 取数来源：{_esc(origin)}
 · 生成于 {_esc(generated_at)} · 数据仅供研究参考</p>
<div class="kpis">{kpi_html}</div>
</header>
<main class="card">
<h2>等权组合走势（各自基准 = 100）</h2>
<p class="meta">每只股票以「不晚于信号日的最后一根日线收盘价」为 100，逐日等权平均。
{f'基准同期：{_esc(BENCHMARK_NAME)} <b class="{_cls(bench)}">{_pct(bench)}</b>。' if bench is not None else ''}
{f'平均超额：<b class="{_cls(excess)}">{_pct(excess)}</b>。' if excess is not None else ''}</p>
{chart}
{f'<div class="pills">分区分布：{section_pills}</div>' if section_pills else ''}
</main>
<main class="card">
<h2>个股区间表现（按涨跌幅排序）</h2>
<div class="tablewrap"><table>
<thead><tr><th>#</th><th>代码</th><th>名称</th><th>分区</th><th>事件 · 板块</th>
<th>基准日/价</th><th>最新日/价</th><th>区间涨跌</th><th>区间最高</th><th>区间最低</th>
<th>走势</th></tr></thead>
<tbody>{''.join(body_rows)}</tbody></table></div>
{missing_rows}
</main>
<main class="card">
<h2>口径说明</h2>
<p class="caveat">基准价 = 不晚于信号日的最后一根日线收盘价（政策多在盘后发布，拿发布前
最后收盘当基准，区间涨跌才是「消息出来之后市场给了多少」）；信号日为非交易日时自动顺延到
之前最近的交易日。若返回数据里基准日之前还有更早的行，以区间内第一根为准。</p>
<p class="caveat">区间涨跌 = 最新交易日收盘 / 基准价 − 1；未做分红、拆分的复权调整
（价格口径以 tongstock 日线原样为准）。汇总为等权平均，每只股票权重相同。
当日日线未落库时（盘中或次日凌晨），用 <code>quote</code> 当日最新价补上「今天」
这个点，表内标「盘中」；当日日线只是昨收占位（部分品种会先放一根占位桩）时，
同样用盘中价刷新那一根的收盘——但作为基准的那根永不用盘中价刷新（基准必须是
信号发布前的价格）。若盘中价与日线价格口径对不上（部分品种 quote 返回
10 倍价），则放弃该点只用日线——宁缺毋错。</p>
<p class="caveat">基准对照为 {_esc(BENCHMARK_NAME)}（{BENCHMARK_CODE}）同期区间涨跌；
本机 tongstock 取不到指数日线，故用同市场口径的 ETF 替代，取不到时留空。</p>
<p class="caveat">涨跌配色按 A 股惯例：红涨绿跌。本页为研究参考，不构成投资建议。</p>
</main>
<footer><p class="disc">{_esc(DISCLAIMER)}</p></footer>
</div></body></html>"""


# ---------------------------------------------------------------- 组装

def build_preview(day: str, paths, settings: Settings, client=None,
                  open_browser: bool = True, report_json: Path | None = None,
                  out: Path | None = None, progress=None) -> Path | None:
    """生成验证页；返回输出路径。找不到当日选中的个股时返回 None。

    `progress`：可选回调 (msg)，供 CLI 打印逐只取数进度。
    """
    today = date.today().isoformat()
    rpath = report_json or (paths.reports / f"{day}.json")
    selection, origin = load_selection(rpath, paths, day)
    if not selection:
        return None

    client = client or TongstockClient(settings, cache_dir=paths.cache)
    log = progress or (lambda msg: None)

    # 名称补全（老 JSON / 库内兜底可能缺名）；tongstock 不可用时留空也照常出页
    if any(not r.get("ticker_name") for r in selection):
        try:
            names = {c: t.name for c, t in client.tickers().items()}
        except TongstockError:
            names = {}
        for r in selection:
            if not r.get("ticker_name"):
                r["ticker_name"] = names.get(r["ticker_code"], "")

    rows: list[dict] = []
    missing: list[dict] = []
    errors: list[Exception] = []
    fetched: dict[str, list[dict]] = {}
    for sel in selection:
        code = sel["ticker_code"]
        log(f"行情 {code} {sel.get('ticker_name') or ''} …")
        try:
            fetched[code] = kline_series(client, code, day, today)
        except TongstockError as exc:
            errors.append(exc)
            fetched[code] = []
    # 当日这根日线要么还没落库（盘中/次日凌晨），要么只是昨收占位——统一拿
    # quote 盘中价对齐，否则刚发完日报就 preview 会全是 +0.00%
    log("对齐当日盘中价（quote）…")
    sync_live_prices(client, fetched, today)
    for sel in selection:
        stats = row_stats(sel, fetched[sel["ticker_code"]])
        if stats is None:
            missing.append(sel)
        else:
            rows.append(stats)
    rows.sort(key=lambda r: -r["pct"])

    # 每只都取不到 = 行情链路坏了（tongstock 挂了 / 数据没同步），要报错而不是出空页
    if not rows and errors and len(errors) == len(selection):
        raise errors[0]

    agg = aggregate(rows, missing)
    agg["bench_pct"] = benchmark_pct(client, day, today)
    if agg["avg"] is not None and agg["bench_pct"] is not None:
        agg["excess"] = round(agg["avg"] - agg["bench_pct"], 2)

    generated = time.strftime("%Y-%m-%d %H:%M:%S")
    log("汇总：" + preview_summary(agg))
    page = render_preview(day, rows, missing, agg, origin, generated)
    out_path = out or (paths.reports / f"{day}-preview.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    if open_browser:
        try:
            webbrowser.open(out_path.resolve().as_uri())
        except Exception:  # noqa: BLE001 - 无头环境打不开浏览器不该让命令失败
            pass
    return out_path


def preview_summary(agg: dict) -> str:
    """CLI 终端摘要行（与页面同一套数字）。"""
    parts = [f"{agg['priced']} 只有行情（共 {agg['total']}）",
             f"{agg['up']} 涨 {agg['down']} 跌 {agg['flat']} 平"]
    if agg["avg"] is not None:
        parts.append(f"平均 {_pct(agg['avg'])}，中位 {_pct(agg['median'])}")
    if agg["bench_pct"] is not None:
        parts.append(f"基准 {BENCHMARK_NAME} {_pct(agg['bench_pct'])}")
        if agg.get("excess") is not None:
            parts.append(f"超额 {_pct(agg['excess'])}")
    return "；".join(parts)
