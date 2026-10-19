"""溯源复核：重抓原文，比对 content_hash。

状态：一致 / 内容已变（政策被修订）/ 链接失效 / 抓取失败

两类文档的复核方式不同：

- 网络源：重抓 URL 复算原文指纹（429/503 归「被限速」，不算链接失效）。
- 本地 inbound 源（用户自己下载的原稿）：源站 robots 禁止抓取，**不能**去重抓，
  改为与落盘的本地原件复算指纹，状态 `一致（本地原件）`。这不是妥协：本地原件
  就是这条结论的原始证据，`policypulse show` 里的快照路径可打开核对。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from policypulse.http import FetchError, Fetcher, RobotsDisallowed, content_hash
from policypulse.text import html_to_text


def _field(row, name: str, default=None):
    """documents 行可能是 sqlite3.Row 也可能是测试里的 dict，缺列要有兜底。"""
    try:
        value = row[name]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


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
# 本地 inbound 源：与用户下载的原件比对（源站 robots 禁止抓取，不去重抓）
STATUS_LOCAL_OK = "一致（本地原件）"
STATUS_LOCAL_MISSING = "本地原件缺失"
# 这两种状态都算「复核通过」，cli 的退出码据此判断
OK_STATUSES = (STATUS_OK, STATUS_LOCAL_OK)
ALL_STATUSES = (STATUS_OK, STATUS_LOCAL_OK, STATUS_CHANGED, STATUS_GONE,
                STATUS_RATE_LIMITED, STATUS_ERROR, STATUS_LOCAL_MISSING)


@dataclass
class VerifyResult:
    doc_id: str
    url: str
    status: str
    old_hash: str = ""
    new_hash: str = ""


def verify_documents(store: Store, doc_ids: list[str] | None = None, run_id: str | None = None,
                     fetcher: Fetcher | None = None,
                     local_source_ids: set[str] | None = None) -> list[VerifyResult]:
    if run_id:
        rows = store.documents_for_run(run_id)
    elif doc_ids:
        rows = [store.document(d) for d in doc_ids]
        rows = [r for r in rows if r is not None]
    else:
        rows = list(store.conn.execute(
            "SELECT * FROM documents ORDER BY fetched_at DESC LIMIT 50"))

    if local_source_ids is None:
        local_source_ids = _local_inbound_source_ids()

    owns_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(min_interval=2.0)
    results: list[VerifyResult] = []
    try:
        for row in rows:
            # 与原始快照比对：content_hash 是正文指纹（用于去重），
            # raw_hash 才是重抓时可直接复算的原文指纹
            old_hash = _field(row, "raw_hash") or ""
            url, old_hash = row["url"], (old_hash or _field(row, "content_hash") or "")

            if _field(row, "source_id") in local_source_ids:
                results.append(_verify_local(row, url, old_hash, store))
                continue

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


def _local_inbound_source_ids() -> set[str]:
    """sources.yaml 里用 local_inbound 适配器的源 id（这些源不能联网重抓）。"""
    from policypulse.config import load_sources

    try:
        return {s.id for s in load_sources() if s.adapter == "local_inbound"}
    except Exception:  # noqa: BLE001 - sources.yaml 读不出来就退化成全部联网复核
        return set()


def _verify_local(row, url: str, old_hash: str, store: Store) -> VerifyResult:
    """本地 inbound：与落盘的原件复算指纹，不去重抓 robots 禁止的源站。"""
    path = Path(_field(row, "raw_snapshot_path") or "")
    if not path.is_file():
        status, new_hash = STATUS_LOCAL_MISSING, ""
    else:
        new_hash = content_hash(html_to_text(path.read_text(encoding="utf-8", errors="replace")))
        status = STATUS_LOCAL_OK if new_hash == old_hash else STATUS_CHANGED
    store.log_verify(row["doc_id"], url, status, old_hash, new_hash)
    return VerifyResult(row["doc_id"], url, status, old_hash, new_hash)


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
