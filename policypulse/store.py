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
  model TEXT, prompt_version TEXT, extracted_at TEXT
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
  PRIMARY KEY (signal_id, code)
);
CREATE TABLE IF NOT EXISTS verify_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id TEXT, url TEXT, checked_at TEXT, status TEXT, old_hash TEXT, new_hash TEXT
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

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
                " confidence, quote_verified, model, prompt_version, extracted_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(signal_id) DO UPDATE SET summary=excluded.summary, quote=excluded.quote,"
                " direction=excluded.direction, strength=excluded.strength, confidence=excluded.confidence",
                (
                    sig.signal_id, sig.doc_id, run_id, sig.summary, sig.quote, sig.direction,
                    sig.strength, sig.horizon, sig.novelty, json.dumps(sig.instruments, ensure_ascii=False),
                    json.dumps(sig.theme_tags, ensure_ascii=False),
                    json.dumps(sig.affected_regions, ensure_ascii=False),
                    json.dumps(sig.affected_sectors_hint, ensure_ascii=False),
                    sig.confidence, int(sig.quote_verified), sig.model, sig.prompt_version, now(),
                ),
            )

    def signals_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM signals WHERE run_id=? ORDER BY strength DESC, confidence DESC", (run_id,)))

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
                             mapping_path: str, mapping_confidence: float, sector: str, source: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO signal_ticker(signal_id,code,why,score,rank,mapping_path,mapping_confidence,"
                "sector_name,sector_source) VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(signal_id,code) DO UPDATE SET score=excluded.score, rank=excluded.rank",
                (signal_id, code, why, score, rank_, mapping_path, mapping_confidence, sector, source),
            )

    def tickers_for_signal(self, signal_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT st.*, t.name, t.exchange, t.board, t.market_cap_yi FROM signal_ticker st"
            " LEFT JOIN tickers t ON t.code=st.code WHERE st.signal_id=? ORDER BY st.rank",
            (signal_id,)))

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

    def sources_upsert(self, spec) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO sources(id,name,org,tier,adapter,url,column_name) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (spec.id, spec.name, spec.org, spec.tier, spec.adapter, spec.url, spec.column),
            )


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
