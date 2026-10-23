"""SQLite 存储：sources / documents / llm_calls / signals / sectors / sector_members /
tickers / ticker_sector / signal_ticker / runs。单机单文件，零运维。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT,
  docs_fetched INTEGER DEFAULT 0,
  signals_extracted INTEGER DEFAULT 0,
  tickers_mapped INTEGER DEFAULT 0,
  window_start TEXT,
  window_end TEXT,
  score_version TEXT,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  name TEXT, org TEXT, tier TEXT, adapter TEXT, url TEXT, column_name TEXT
);
CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY,
  run_id TEXT,
  source_id TEXT,
  url TEXT NOT NULL,
  canonical_url TEXT,
  title TEXT,
  publisher TEXT,
  org TEXT,
  source_tier TEXT,
  meeting_type TEXT,
  published_at TEXT,
  fetched_at TEXT,
  content_hash TEXT,
  raw_hash TEXT,
  raw_snapshot_path TEXT,
  language TEXT DEFAULT 'zh-CN',
  duplicate_of TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id TEXT, model TEXT, prompt_version TEXT,
  prompt TEXT, raw_response TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS signals (
  signal_id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL,
  run_id TEXT,
  summary TEXT, quote TEXT, direction TEXT, strength INTEGER, horizon TEXT,
  novelty TEXT, instruments TEXT, theme_tags TEXT, affected_regions TEXT,
  affected_sectors_hint TEXT, confidence REAL, quote_verified INTEGER,
  model TEXT, prompt_version TEXT, extracted_at TEXT,
  investable INTEGER, investable_reason TEXT, beneficiary_specificity TEXT
);
CREATE TABLE IF NOT EXISTS sectors (
  sector_name TEXT, sector_source TEXT, member_count INTEGER,
  refreshed_at TEXT,
  PRIMARY KEY (sector_name, sector_source)
);
CREATE TABLE IF NOT EXISTS sector_members (
  sector_name TEXT, sector_source TEXT, code TEXT,
  PRIMARY KEY (sector_name, sector_source, code)
);
CREATE TABLE IF NOT EXISTS tickers (
  code TEXT PRIMARY KEY, name TEXT, exchange TEXT, board TEXT,
  market_cap_yi REAL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ticker_sector (
  code TEXT, sector_name TEXT, sector_source TEXT,
  PRIMARY KEY (code, sector_name, sector_source)
);
CREATE TABLE IF NOT EXISTS signal_ticker (
  signal_id TEXT, code TEXT, why TEXT, score REAL, rank INTEGER,
  mapping_path TEXT, mapping_confidence REAL, sector_name TEXT, sector_source TEXT,
  relevance REAL DEFAULT 1.0, gate_basis TEXT, gate_evidence TEXT,
  PRIMARY KEY (signal_id, code)
);
CREATE TABLE IF NOT EXISTS verify_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id TEXT, url TEXT, checked_at TEXT, status TEXT, old_hash TEXT, new_hash TEXT
);
CREATE TABLE IF NOT EXISTS out_of_window_cache (
  url TEXT PRIMARY KEY,
  published TEXT,
  checked_at TEXT
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """老库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列。"""
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(signal_ticker)")}
        for col, decl in (("relevance", "REAL"), ("gate_basis", "TEXT"),
                          ("gate_evidence", "TEXT")):
            if col not in columns:
                self.conn.execute(f"ALTER TABLE signal_ticker ADD COLUMN {col} {decl}")
                if col == "relevance":
                    self.conn.execute("UPDATE signal_ticker SET relevance=mapping_confidence"
                                      " WHERE relevance IS NULL")
        # 信号级可投资性闸门：老库里的历史信号不回填判定结果（NULL = 未评估），
        # 展示层按当前词表实时判定，避免把历史行硬标成某一侧
        sig_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(signals)")}
        for col, decl in (("investable", "INTEGER"), ("investable_reason", "TEXT"),
                          ("beneficiary_specificity", "TEXT")):
            if col not in sig_columns:
                self.conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {decl}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- runs ----
    def start_run(self, run_id: str, window_start: str, window_end: str, score_version: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO runs(run_id, started_at, status, window_start, window_end, score_version)"
                " VALUES (?,?,?,?,?,?)",
                (run_id, now(), "running", window_start, window_end, score_version),
            )

    def finish_run(self, run_id: str, status: str, docs: int, signals: int, tickers: int,
                   notes: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, docs_fetched=?, signals_extracted=?,"
                " tickers_mapped=?, notes=? WHERE run_id=?",
                (now(), status, docs, signals, tickers, notes, run_id),
            )

    def latest_run_for(self, window_end: str, status: str | None = "ok") -> str | None:
        """窗口结束日为该日的最近一次 run_id（preview 的库内兑底取数口）。

        优先成功 run；status=None 时不限状态（宁可拿旧信号，也不让 preview 空手）。
        """
        sql = ("SELECT run_id FROM runs WHERE window_end=?"
               + (" AND status=?" if status else "")
               + " ORDER BY started_at DESC LIMIT 1")
        args = (window_end, status) if status else (window_end,)
        row = self.conn.execute(sql, args).fetchone()
        return row["run_id"] if row else None

    # ---- documents ----
    def upsert_document(self, row: dict[str, Any]) -> None:
        cols = ["doc_id", "run_id", "source_id", "url", "canonical_url", "title", "publisher",
                "org", "source_tier", "meeting_type", "published_at", "fetched_at",
                "content_hash", "raw_hash", "raw_snapshot_path", "language", "duplicate_of"]
        values = [row.get(c) for c in cols]
        with self.tx() as c:
            c.execute(
                f"INSERT INTO documents({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
                " ON CONFLICT(doc_id) DO UPDATE SET "
                + ",".join(f"{c}=excluded.{c}" for c in cols if c != "doc_id"),
                values,
            )

    def document_exists_hash(self, content_hash: str) -> str | None:
        cur = self.conn.execute("SELECT doc_id FROM documents WHERE content_hash=? LIMIT 1",
                                (content_hash,))
        row = cur.fetchone()
        return row["doc_id"] if row else None

    def documents_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM documents WHERE run_id=? ORDER BY published_at DESC",
                                      (run_id,)))

    # ---- signals ----
    def upsert_signal(self, sig, run_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO signals(signal_id, doc_id, run_id, summary, quote, direction, strength,"
                " horizon, novelty, instruments, theme_tags, affected_regions, affected_sectors_hint,"
                " confidence, quote_verified, model, prompt_version, extracted_at,"
                " investable, investable_reason, beneficiary_specificity)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(signal_id) DO UPDATE SET summary=excluded.summary, quote=excluded.quote,"
                " direction=excluded.direction, strength=excluded.strength, confidence=excluded.confidence,"
                " investable=excluded.investable, investable_reason=excluded.investable_reason,"
                " beneficiary_specificity=excluded.beneficiary_specificity",
                (
                    sig.signal_id, sig.doc_id, run_id, sig.summary, sig.quote, sig.direction,
                    sig.strength, sig.horizon, sig.novelty, json.dumps(sig.instruments, ensure_ascii=False),
                    json.dumps(sig.theme_tags, ensure_ascii=False),
                    json.dumps(sig.affected_regions, ensure_ascii=False),
                    json.dumps(sig.affected_sectors_hint, ensure_ascii=False),
                    sig.confidence, int(sig.quote_verified), sig.model, sig.prompt_version, now(),
                    int(getattr(sig, "investable", False)),
                    getattr(sig, "investable_reason", ""),
                    getattr(sig, "beneficiary_specificity", ""),
                ),
            )

    def signals_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM signals WHERE run_id=? ORDER BY strength DESC, confidence DESC", (run_id,)))

    def signals_for_doc(self, doc_id: str) -> list[sqlite3.Row]:
        """一篇文档此前抽出的全部信号（复用库内文档时按此恢复结论）。"""
        return list(self.conn.execute(
            "SELECT * FROM signals WHERE doc_id=? ORDER BY strength DESC, confidence DESC", (doc_id,)))

    def signal(self, signal_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM signals WHERE signal_id=?", (signal_id,)).fetchone()

    def document(self, doc_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()

    # ---- 股票与映射 ----
    def upsert_ticker(self, code: str, name: str, exchange: str, board: str, market_cap_yi: float) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO tickers(code,name,exchange,board,market_cap_yi,updated_at) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(code) DO UPDATE SET name=excluded.name, market_cap_yi=excluded.market_cap_yi,"
                " updated_at=excluded.updated_at",
                (code, name, exchange, board, market_cap_yi, now()),
            )

    def upsert_sector(self, name: str, source: str, member_count: int) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO sectors(sector_name,sector_source,member_count,refreshed_at) VALUES(?,?,?,?)"
                " ON CONFLICT(sector_name,sector_source) DO UPDATE SET member_count=excluded.member_count,"
                " refreshed_at=excluded.refreshed_at",
                (name, source, member_count, now()),
            )

    def upsert_sector_member(self, sector: str, source: str, code: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO sector_members(sector_name,sector_source,code) VALUES(?,?,?)",
                      (sector, source, code))
            c.execute("INSERT OR IGNORE INTO ticker_sector(code,sector_name,sector_source) VALUES(?,?,?)",
                      (code, sector, source))

    def upsert_signal_ticker(self, signal_id: str, code: str, why: str, score: float, rank_: int,
                             mapping_path: str, mapping_confidence: float, sector: str, source: str,
                             *, relevance: float | None = None, gate_basis: str = "",
                             gate_evidence: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO signal_ticker(signal_id,code,why,score,rank,mapping_path,mapping_confidence,"
                "sector_name,sector_source,relevance,gate_basis,gate_evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(signal_id,code) DO UPDATE SET score=excluded.score, rank=excluded.rank,"
                " relevance=excluded.relevance, gate_basis=excluded.gate_basis,"
                " gate_evidence=excluded.gate_evidence",
                (signal_id, code, why, score, rank_, mapping_path, mapping_confidence, sector, source,
                 relevance if relevance is not None else mapping_confidence, gate_basis, gate_evidence),
            )

    def tickers_for_signal(self, signal_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT st.*, t.name, t.exchange, t.board, t.market_cap_yi FROM signal_ticker st"
            " LEFT JOIN tickers t ON t.code=st.code WHERE st.signal_id=? ORDER BY st.rank",
            (signal_id,)))

    def clear_signal_tickers(self, signal_id: str) -> int:
        """删掉一条信号的个股展开行。

        信号被判非投资后，历史 run 留下的展开行必须一起清掉：只跳过本次展开
        等于「字段加了、表里还在」，重跑后受益表与 show 仍会显示旧个股。
        """
        cur = self.conn.execute("DELETE FROM signal_ticker WHERE signal_id=?", (signal_id,))
        self.conn.commit()
        return cur.rowcount

    def log_llm_call(self, signal_id: str, model: str, prompt_version: str,
                     prompt: str, raw: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO llm_calls(signal_id,model,prompt_version,prompt,raw_response,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (signal_id, model, prompt_version, prompt, raw, now()),
            )

    def log_verify(self, doc_id: str, url: str, status: str, old_hash: str, new_hash: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO verify_log(doc_id,url,checked_at,status,old_hash,new_hash) VALUES(?,?,?,?,?,?)",
                (doc_id, url, now(), status, old_hash, new_hash),
            )

    # ---- 窗口外旧稿缓存 ----
    def remember_out_of_window(self, url: str, published: str) -> None:
        """记下「该 URL 详情页日期在窗口外」的判定，避免下轮重复请求详情页。"""
        with self.tx() as c:
            c.execute(
                "INSERT INTO out_of_window_cache(url,published,checked_at) VALUES(?,?,?)"
                " ON CONFLICT(url) DO UPDATE SET published=excluded.published,"
                " checked_at=excluded.checked_at",
                (url, published, now()),
            )

    def out_of_window_date(self, url: str, max_age_days: int = 30) -> str | None:
        """缓存的窗口外判定日期；过期返回 None（让调用方重新判定）。"""
        row = self.conn.execute(
            "SELECT published, checked_at FROM out_of_window_cache WHERE url=?", (url,)).fetchone()
        if row is None:
            return None
        try:
            checked = time.mktime(time.strptime(row["checked_at"], "%Y-%m-%dT%H:%M:%S%z"))
        except (ValueError, TypeError):
            return None
        if time.time() - checked > max_age_days * 86400:
            return None
        return row["published"]

    def sources_upsert(self, spec) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO sources(id,name,org,tier,adapter,url,column_name) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (spec.id, spec.name, spec.org, spec.tier, spec.adapter, spec.url, spec.column),
            )


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
