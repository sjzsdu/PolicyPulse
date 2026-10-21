"""政策事件聚类：同文档 / 同发布会的信号聚成一个「政策事件」，个股结论按事件去重。

之前按句子出信号，同一篇通稿会拆出十几条几乎一样的信号，个股表也跟着整表重复。
这里以 doc_id 为主键聚类，再把同机构同日、标题高度相似的文档合并为同一事件，
个股按代码去重（保留相关性与得分最高的那条），并记录它由哪几条信号推出。
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field

# 同机构同日、标题相似度 ≥ 该阈值视为同一次发布（通稿全文 + 新闻稿两种版本）
TITLE_SIMILARITY = 0.7
_WS_RE = re.compile(r"\s+")


@dataclass
class PolicyEvent:
    event_id: str
    title: str
    org: str
    meeting_type: str
    published_at: str
    source_url: str
    snapshot_path: str
    doc_ids: list[str] = field(default_factory=list)
    conclusions: list[dict] = field(default_factory=list)
    tickers: list[dict] = field(default_factory=list)


def _norm_title(title: str) -> str:
    return _WS_RE.sub("", title or "")


def _title_similar(a: str, b: str) -> bool:
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= TITLE_SIMILARITY


def _event_id(doc_ids: list[str]) -> str:
    seed = "|".join(sorted(doc_ids))
    return "evt-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]


def _merge_tickers(conclusions: list[dict]) -> list[dict]:
    """同一事件里重复出现的个股只留一条，保留最高分那条，并记录推导信号。

    信号归属要在**被保留的那条**上累积：如果只在传入的 dict 上挂 id，
    后来的低分行被丢弃时它的信号号也一起丢了。
    """
    best: dict[str, dict] = {}
    order: list[str] = []
    for c in conclusions:
        signal_id = c.get("signal_id", "")
        for t in c.get("tickers", []):
            code = t.get("ticker_code", "")
            if not code:
                continue
            prev = best.get(code)
            if prev is None or t.get("score", 0) > prev.get("score", 0):
                row = dict(t)
                carried = list(prev.get("_signal_ids", [])) if prev else []
                if signal_id not in carried:
                    carried.append(signal_id)
                row["_signal_ids"] = carried
                best[code] = row
            else:
                carried = list(prev.get("_signal_ids", []))
                if signal_id not in carried:
                    carried.append(signal_id)
                    prev["_signal_ids"] = carried
            if code not in order:
                order.append(code)
    rows = []
    for code in order:
        row = dict(best[code])
        row.pop("_signal_ids", None)
        row["signal_ids"] = list(best[code].get("_signal_ids", []))
        rows.append(row)
    rows.sort(key=lambda r: r.get("score", 0), reverse=True)
    return rows


def cluster_events(conclusions: list[dict]) -> list[PolicyEvent]:
    """把结论聚成政策事件；返回按 (日期, 得分) 排序的事件列表。"""
    by_doc: dict[str, list[dict]] = {}
    for c in conclusions:
        by_doc.setdefault(c.get("doc_id", ""), []).append(c)

    events: list[PolicyEvent] = []
    for doc_id, items in by_doc.items():
        head = items[0]
        events.append(PolicyEvent(
            event_id=_event_id([doc_id]),
            title=head.get("doc_title") or head.get("summary", ""),
            org=head.get("org", ""),
            meeting_type=head.get("meeting_type", ""),
            published_at=head.get("published_at", ""),
            source_url=head.get("source_url", ""),
            snapshot_path=head.get("raw_snapshot_path", ""),
            doc_ids=[doc_id],
            conclusions=items,
        ))

    # 同机构同日 + 标题高度相似 → 合并为同一事件
    merged: list[PolicyEvent] = []
    for ev in sorted(events, key=lambda e: (e.published_at, e.org)):
        target = None
        day = (ev.published_at or "")[:10]
        for other in merged:
            if other.org != ev.org or (other.published_at or "")[:10] != day:
                continue
            if _title_similar(other.title, ev.title):
                target = other
                break
        if target is None:
            merged.append(ev)
            continue
        target.doc_ids.extend(ev.doc_ids)
        target.conclusions.extend(ev.conclusions)
        if len(_norm_title(ev.title)) > len(_norm_title(target.title)):
            target.title = ev.title
        if not target.source_url:
            target.source_url = ev.source_url

    for ev in merged:
        ev.event_id = _event_id(ev.doc_ids)
        ev.tickers = _merge_tickers(ev.conclusions)
        ev.conclusions.sort(key=lambda c: (-c.get("strength", 0), -c.get("confidence", 0)))

    merged.sort(key=lambda e: ((e.published_at or "")[:10], -max(
        (t.get("score", 0) for t in e.tickers), default=0.0)), reverse=True)
    return merged


def event_stats(events: list[PolicyEvent]) -> dict:    return {
        "events": len(events),
        "conclusions": sum(len(e.conclusions) for e in events),
        "ticker_rows": sum(len(e.tickers) for e in events),
        "ticker_rows_deduped": sum(len(e.tickers) for e in events),
    }


def merge_tickers(conclusions: list[dict]) -> list[dict]:
    """公开别名：渲染层用它对「已筛过的结论」做同一套个股去重。"""
    return _merge_tickers(conclusions)


def _ensure_events(result) -> list[PolicyEvent]:
    """RunResult 带 events 就用；否则（单测/历史对象）从 conclusions 现算一次。"""
    if getattr(result, "events", None):
        return result.events
    return cluster_events(result.conclusions)
