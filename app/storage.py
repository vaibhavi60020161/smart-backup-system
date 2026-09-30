"""Persistence: SQLite (built into Python) for records, plain files for chunk bytes."""
import contextlib
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS chunks(hash TEXT PRIMARY KEY, size INTEGER NOT NULL, refcount INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS results(
  result_id TEXT PRIMARY KEY, file_id TEXT, backup_id TEXT, file_name TEXT, total_size INTEGER,
  chunk_count INTEGER, unique_chunks INTEGER, duplicate_chunks INTEGER, delta_size INTEGER,
  savings_ratio REAL, merkle_root TEXT, file_checksum TEXT, chunking TEXT, chunk_size INTEGER,
  hash_algorithm TEXT, index_version INTEGER, chunk_version TEXT, previous_result_id TEXT,
  created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_results_file ON results(file_id, created_at);
CREATE TABLE IF NOT EXISTS result_chunks(result_id TEXT, seq INTEGER, hash TEXT, size INTEGER,
  PRIMARY KEY(result_id, seq));
CREATE TABLE IF NOT EXISTS idempotency(key TEXT PRIMARY KEY, request_hash TEXT, response TEXT, created_at TEXT);
"""
RESULT_COLS = ["result_id", "file_id", "backup_id", "file_name", "total_size", "chunk_count",
               "unique_chunks", "duplicate_chunks", "delta_size", "savings_ratio", "merkle_root",
               "file_checksum", "chunking", "chunk_size", "hash_algorithm", "index_version",
               "chunk_version", "previous_result_id", "created_at"]


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Store:
    def __init__(self, db_path):
        self.db_path = db_path
        with self._db() as c:
            c.executescript(SCHEMA)

    @contextlib.contextmanager
    def _db(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()          # all-or-nothing per block
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # meta
    def get_meta(self, key, default=None):
        with self._db() as c:
            r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key, value):
        with self._db() as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))

    # chunks
    def load_chunks(self):
        with self._db() as c:
            return c.execute("SELECT hash,size,refcount FROM chunks").fetchall()

    def insert_chunk_if_missing(self, h, size):
        with self._db() as c:
            cur = c.execute("INSERT OR IGNORE INTO chunks VALUES(?,?,0)", (h, size))
            return cur.rowcount == 1

    def list_chunks(self, limit, offset):
        with self._db() as c:
            return c.execute("SELECT hash,size,refcount FROM chunks ORDER BY hash LIMIT ? OFFSET ?",
                             (limit, offset)).fetchall()

    def chunk_totals(self):
        with self._db() as c:
            n, stored, logical, orphan = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(size),0), COALESCE(SUM(size*refcount),0), "
                "COALESCE(SUM(refcount=0),0) FROM chunks").fetchone()
            results, files = c.execute("SELECT COUNT(*), COUNT(DISTINCT file_id) FROM results").fetchone()
        return {"total_chunks": n, "stored_bytes": stored, "logical_bytes": logical,
                "orphan_chunks": orphan, "results": results, "files": files}

    # results
    def commit_result(self, row, ordered_chunks, changes):
        """One transaction: chunk records + refcounts + result + its chunk list."""
        with self._db() as c:
            for h, (size, count, is_new) in changes.items():
                if is_new:
                    c.execute("INSERT INTO chunks VALUES(?,?,?)", (h, size, count))
                else:
                    c.execute("UPDATE chunks SET refcount=refcount+? WHERE hash=?", (count, h))
            c.execute(f"INSERT INTO results VALUES({','.join('?' * len(RESULT_COLS))})",
                      [row[k] for k in RESULT_COLS])
            c.executemany("INSERT INTO result_chunks VALUES(?,?,?,?)",
                          [(row["result_id"], i, h, s) for i, (h, s) in enumerate(ordered_chunks)])

    def get_result(self, result_id):
        with self._db() as c:
            r = c.execute("SELECT * FROM results WHERE result_id=?", (result_id,)).fetchone()
        return dict(zip(RESULT_COLS, r)) if r else None

    def latest_result_for_file(self, file_id):
        with self._db() as c:
            r = c.execute("SELECT * FROM results WHERE file_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                          (file_id,)).fetchone()
        return dict(zip(RESULT_COLS, r)) if r else None

    def result_chunks(self, result_id):
        with self._db() as c:
            return c.execute("SELECT hash,size FROM result_chunks WHERE result_id=? ORDER BY seq",
                             (result_id,)).fetchall()

    # idempotency
    def get_idem(self, key):
        with self._db() as c:
            r = c.execute("SELECT request_hash,response FROM idempotency WHERE key=?", (key,)).fetchone()
        return {"request_hash": r[0], "response": r[1]} if r else None

    def put_idem(self, key, request_hash, response):
        with self._db() as c:
            c.execute("INSERT INTO idempotency VALUES(?,?,?,?)", (key, request_hash, json.dumps(response), now()))
