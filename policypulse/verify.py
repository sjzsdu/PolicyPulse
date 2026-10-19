"""溯源复核：重抓原文，比对 content_hash。

状态：一致 / 内容已变（政策被修订）/ 链接失效 / 抓取失败
"""

from __future__ import annotations

from dataclasses import dataclass

from policypulse.http import FetchError, Fetcher, RobotsDisallowed, content_hash
from policypulse.text import html_to_text


def _classify(exc: FetchError) -> str:
    """429/503 是被限速；4xx 才是链接本身失效。"""
    msg = str(exc)
    if "HTTP 429" in msg or "HTTP 503" in msg:
        return STATUS_RATE_LIMITED
    if "HTTP 4" in msg:
        return STATUS_GONE
    return STATUS_ERROR
from policypulse.store import Store

STATUS_OK = "一致"
STATUS_CHANGED = "内容已变"
STATUS_GONE = "链接失效"
STATUS_RATE_LIMITED = "被限速、暂不可验"
STATUS_ERROR = "抓取失败"
ALL_STATUSES = (STATUS_OK, STATUS_CHANGED, STATUS_GONE, STATUS_RATE_LIMITED, STATUS_ERROR)


@dataclass
class VerifyResult:
    doc_id: str
    url: str
    status: str
    old_hash: str = ""
    new_hash: str = ""


def verify_documents(store: Store, doc_ids: list[str] | None = None, run_id: str | None = None,
                     fetcher: Fetcher | None = None) -> list[VerifyResult]:
    if run_id:
        rows = store.documents_for_run(run_id)
    elif doc_ids:
        rows = [store.document(d) for d in doc_ids]
        rows = [r for r in rows if r is not None]
    else:
        rows = list(store.conn.execute(
            "SELECT * FROM documents ORDER BY fetched_at DESC LIMIT 50"))

    owns_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(min_interval=2.0)
    results: list[VerifyResult] = []
    try:
        for row in rows:
            # 与原始快照比对：content_hash 是正文指纹（用于去重），
            # raw_hash 才是重抓时可直接复算的原文指纹
            old_hash = row["raw_hash"] if "raw_hash" in row.keys() else ""
            url, old_hash = row["url"], (old_hash or row["content_hash"] or "")
            try:
                html = fetcher.get_text(url)
                new_hash = content_hash(html_to_text(html))
                status = STATUS_OK if new_hash == old_hash else STATUS_CHANGED
            except RobotsDisallowed:
                results.append(VerifyResult(row["doc_id"], url, STATUS_RATE_LIMITED,
                                            old_hash, ""))
                store.log_verify(row["doc_id"], url, STATUS_RATE_LIMITED, old_hash, "")
                continue
            except FetchError as exc:
                # G：429 是被限速，不是链接失效，不能混为一谈
                status = _classify(exc)
                new_hash = ""
            results.append(VerifyResult(row["doc_id"], url, status, old_hash, new_hash))
            store.log_verify(row["doc_id"], url, status, old_hash, new_hash)
    finally:
        if owns_fetcher:
            fetcher.close()
    return results


def render_verify(results: list[VerifyResult]) -> str:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    lines = ["| 状态 | 数量 |", "|---|---|"]
    for status in ALL_STATUSES:
        lines.append(f"| {status} | {counts.get(status, 0)} |")
    lines.append("")
    lines.append("| doc_id | 状态 | URL |")
    lines.append("|---|---|---|")
    for r in results:
        lines.append(f"| `{r.doc_id}` | {r.status} | {r.url} |")
    return "\n".join(lines)
