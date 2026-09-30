"""Team B core: chunk -> hash -> index lookup -> dedup -> Merkle root -> verify."""
import base64
import hashlib
import json
import os
import threading
import time
import uuid
from collections import deque

from .chunking import content_defined_chunks, fixed_chunks
from .errors import ApiError
from .index import IndexManager
from .merkle import merkle_root
from .storage import RESULT_COLS, Store, now

HASH_ALGORITHM = "sha256"
CHUNK_VERSION = "chunk-v1"


def percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))], 3)


def summary(values):
    return {"count": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95),
            "max": round(max(values), 3) if values else 0.0}


class Metrics:
    def __init__(self):
        self.compute_ms, self.verify_ms = deque(maxlen=5000), deque(maxlen=5000)
        self.lookup_us = deque(maxlen=100000)
        self.requests = self.failed = 0


class Engine:
    def __init__(self, data_dir="backup_store_b", index_type="hash", cache_size=50000,
                 verify_duplicates=True, allowed_root=None):
        self.data_dir = data_dir
        self.chunk_dir = os.path.join(data_dir, "chunks")
        os.makedirs(self.chunk_dir, exist_ok=True)
        self.store = Store(os.path.join(data_dir, "team_b.db"))
        self.idx = IndexManager(index_type, cache_size)
        self.verify_duplicates = verify_duplicates
        self.allowed_root = os.path.abspath(allowed_root) if allowed_root else None
        self.metrics = Metrics()
        self._lock = threading.RLock()
        if self.store.get_meta("hash_algorithm") is None:
            self.store.set_meta("hash_algorithm", HASH_ALGORITHM)
            self.store.set_meta("index_version", 1)
        self.hash_algorithm = self.store.get_meta("hash_algorithm")
        self.index_version = int(self.store.get_meta("index_version"))
        self._load_index()

    @classmethod
    def from_env(cls):
        return cls(os.getenv("TEAMB_DATA_DIR", "backup_store_b"), os.getenv("TEAMB_INDEX", "hash"),
                   int(os.getenv("TEAMB_CACHE_SIZE", "50000")), allowed_root=os.getenv("TEAMB_ALLOWED_ROOT"))

    # ---------- helpers ----------
    def hash_bytes(self, data):
        return hashlib.sha256(data).hexdigest()

    def _load_index(self):
        for h, size, refs in self.store.load_chunks():
            self.idx.index.put(h, {"size": size, "refcount": refs})

    def chunk_path(self, h):
        d = os.path.join(self.chunk_dir, h[:2])
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, h)

    def _write_chunk(self, h, data):
        p = self.chunk_path(h)
        tmp = p + f".tmp{uuid.uuid4().hex[:6]}"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)                      # atomic: never a half-written chunk
        return p

    def _check_duplicate(self, h, data):
        """Hash matched -> confirm the bytes really match (collision mitigation)."""
        p = self.chunk_path(h)
        if not os.path.exists(p):               # chunk file lost: self-heal from the incoming data
            self._write_chunk(h, data)
            return
        if not self.verify_duplicates:
            return
        with open(p, "rb") as f:
            if f.read() != data:
                raise ApiError(409, "HASH_COLLISION", f"Different data produced the same hash {h[:16]}...")

    def _check_path(self, path):
        real = os.path.abspath(path)
        if self.allowed_root and os.path.commonpath([real, self.allowed_root]) != self.allowed_root:
            raise ApiError(403, "PATH_NOT_ALLOWED", "file_path is outside the allowed folder")
        if not os.path.isfile(real):
            raise ApiError(404, "FILE_NOT_FOUND", f"No such file: {path}")
        return real

    # ---------- B1 + B2: chunk, hash, dedup ----------
    def compute(self, req):
        t0 = time.perf_counter()
        alg = req.get("hash_algorithm", HASH_ALGORITHM)
        if alg != self.hash_algorithm:
            raise ApiError(409, "HASH_ALGORITHM_MISMATCH",
                           f"Index uses {self.hash_algorithm}; request asked for {alg}")
        chunking, size = req.get("chunking", "fixed"), req.get("chunk_size", 4096)
        with self._lock:
            exp = req.get("expected_index_version")
            if exp is not None and exp != self.index_version:
                raise ApiError(409, "STALE_INDEX_VERSION",
                               f"Request used index_version {exp}; current is {self.index_version}")
            path = self._check_path(req["file_path"])
            prev_hashes = None
            if req.get("previous_result_id"):
                if not self.store.get_result(req["previous_result_id"]):
                    raise ApiError(404, "PREVIOUS_RESULT_NOT_FOUND", "previous_result_id does not exist")
                prev_hashes = {h for h, _ in self.store.result_chunks(req["previous_result_id"])}

            pending, order, created = {}, [], []     # pending: hash -> [size, count, is_new]
            total = delta = 0
            checksum = hashlib.sha256()
            try:
                with open(path, "rb") as f:
                    gen = content_defined_chunks(f, size) if chunking == "content" else fixed_chunks(f, size)
                    for data in gen:
                        h = self.hash_bytes(data)
                        checksum.update(data)
                        entry = pending.get(h)
                        if entry is not None:                       # repeated inside this file
                            self._check_duplicate(h, data)
                            entry[1] += 1
                        else:
                            found, _hit, us = self.idx.lookup(h)
                            self.metrics.lookup_us.append(us)
                            if found is not None:
                                self._check_duplicate(h, data)
                                pending[h] = [len(data), 1, False]
                            else:
                                created.append(self._write_chunk(h, data))
                                pending[h] = [len(data), 1, True]
                                delta += len(data)
                        order.append((h, len(data)))
                        total += len(data)

                hashes = [h for h, _ in order]
                unique = sum(1 for v in pending.values() if v[2])
                row = {
                    "result_id": "DR-" + uuid.uuid4().hex[:12], "file_id": req["file_id"],
                    "backup_id": req.get("backup_id"), "file_name": os.path.basename(path),
                    "total_size": total, "chunk_count": len(order), "unique_chunks": unique,
                    "duplicate_chunks": len(order) - unique, "delta_size": delta,
                    "savings_ratio": round(1 - delta / total, 4) if total else 0.0,
                    "merkle_root": merkle_root(hashes), "file_checksum": checksum.hexdigest(),
                    "chunking": chunking, "chunk_size": size, "hash_algorithm": alg,
                    "index_version": self.index_version, "chunk_version": CHUNK_VERSION,
                    "previous_result_id": req.get("previous_result_id"), "created_at": now()}
                self.store.commit_result(row, order, {h: tuple(v) for h, v in pending.items()})
            except Exception:
                for p in created:                    # undo files written by this failed call
                    try: os.remove(p)
                    except OSError: pass
                raise

            for h, (sz, n, is_new) in pending.items():           # DB committed -> update memory
                cur = self.idx.raw_get(h)
                if is_new or cur is None:
                    self.idx.add(h, {"size": sz, "refcount": n})
                else:
                    cur["refcount"] += n

            out = {k: row[k] for k in RESULT_COLS if k != "file_name"}
            out["dedup_result_id"] = out.pop("result_id")
            if prev_hashes is not None:
                changed = [(h, s) for h, s in order if h not in prev_hashes]
                out["changed_chunks_vs_previous"] = len(changed)
                out["delta_vs_previous_bytes"] = sum(s for _, s in changed)
            out["chunk_ids"] = hashes
            self.metrics.compute_ms.append((time.perf_counter() - t0) * 1000)
            return out

    def run_idempotent(self, key, request_hash, fn):
        """Same key + same request -> original answer, nothing stored twice."""
        with self._lock:
            rec = self.store.get_idem(key)
            if rec:
                if rec["request_hash"] != request_hash:
                    raise ApiError(422, "IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was used for a different request")
                return json.loads(rec["response"]), True
            out = fn()
            self.store.put_idem(key, request_hash, out)
            return out, False

    def get_result(self, result_id, include_chunks=False):
        r = self.store.get_result(result_id)
        if not r:
            raise ApiError(404, "NOT_FOUND", "dedup result not found")
        r["dedup_result_id"] = r.pop("result_id")
        if include_chunks:
            r["chunk_ids"] = [h for h, _ in self.store.result_chunks(r["dedup_result_id"])]
        return r

    # ---------- B4: integrity ----------
    def verify(self, result_id=None, file_id=None, algorithm="merkle", repetitions=1):
        r = self.store.get_result(result_id) if result_id else self.store.latest_result_for_file(file_id)
        if not r:
            raise ApiError(404, "NOT_FOUND", "No dedup result found for that id")
        chunks = self.store.result_chunks(r["result_id"])
        runs, corrupted, missing, computed_root, computed_sum = [], [], [], None, None
        for _ in range(repetitions):
            t0 = time.perf_counter()
            corrupted, missing, actual = [], [], []
            if algorithm == "merkle":
                for h, _size in chunks:
                    p = self.chunk_path(h)
                    if not os.path.exists(p):
                        missing.append(h); actual.append("MISSING"); continue
                    with open(p, "rb") as f:
                        got = self.hash_bytes(f.read())
                    actual.append(got)
                    if got != h:
                        corrupted.append(h)
                computed_root = merkle_root(actual)
                ok = not corrupted and not missing and computed_root == r["merkle_root"]
            else:                                         # whole-file checksum (baseline to compare with Merkle)
                s = hashlib.sha256()
                for h, _size in chunks:
                    p = self.chunk_path(h)
                    if not os.path.exists(p):
                        missing.append(h); continue
                    with open(p, "rb") as f:
                        s.update(f.read())
                computed_sum = s.hexdigest()
                ok = not missing and computed_sum == r["file_checksum"]
            runs.append((time.perf_counter() - t0) * 1000)
        self.metrics.verify_ms.extend(runs)
        return {
            "dedup_result_id": r["result_id"], "file_id": r["file_id"], "algorithm": algorithm,
            "integrity_status": "VERIFIED" if ok else "FAILED",
            "merkle_root": r["merkle_root"], "computed_merkle_root": computed_root,
            "expected_checksum": r["file_checksum"], "computed_checksum": computed_sum,
            "total_chunks": len(chunks), "verified_chunks": len(chunks) - len(corrupted) - len(missing),
            "corrupted_chunks": corrupted, "missing_chunks": missing,
            "verification_latency_ms": round(sum(runs) / len(runs), 3), "runs_ms": [round(x, 3) for x in runs],
            "complexity": {"time": "O(n) hashing + O(n) tree build", "space": "O(n) hashes",
                           "proof_size": "O(log n) per chunk"}}

    # ---------- chunk registry + index ----------
    def put_chunk(self, chunk_id, data):
        if self.hash_bytes(data) != chunk_id:
            raise ApiError(422, "CHUNK_HASH_MISMATCH", "chunkId does not match the SHA-256 of the data")
        with self._lock:
            created = not os.path.exists(self.chunk_path(chunk_id))
            self._write_chunk(chunk_id, data)
            self.store.insert_chunk_if_missing(chunk_id, len(data))
            if self.idx.raw_get(chunk_id) is None:
                self.idx.add(chunk_id, {"size": len(data), "refcount": 0})
            return {"chunk_id": chunk_id, "size": len(data), "created": created,
                    "refcount": self.idx.raw_get(chunk_id)["refcount"]}

    def chunk_index(self, limit=50, offset=0):
        t = self.store.chunk_totals()
        return {"chunk_version": CHUNK_VERSION, "index_version": self.index_version,
                "index_type": self.idx.index.name, "total_chunks": t["total_chunks"],
                "stored_bytes": t["stored_bytes"], "logical_bytes": t["logical_bytes"],
                "coverage": {"files": t["files"], "results": t["results"],
                             "referenced_chunks": t["total_chunks"] - t["orphan_chunks"],
                             "orphan_chunks": t["orphan_chunks"]},
                "limit": limit, "offset": offset,
                "items": [{"chunk_id": h, "size": s, "refcount": r} for h, s, r in self.store.list_chunks(limit, offset)]}

    def index_lookup(self, key):
        with self._lock:
            v, hit, us = self.idx.lookup(key)
        if v is None:
            raise ApiError(404, "NOT_FOUND", "hash not in index")
        return {"key": key, "entry": v, "lookup_time_us": round(us, 2), "cache_hit": hit,
                "index_type": self.idx.index.name, "index_version": self.index_version}

    def invalidate_cache(self, key=None):
        t0 = time.perf_counter()
        n = self.idx.cache.invalidate(key)
        return {"invalidated": n, "duration_ms": round((time.perf_counter() - t0) * 1000, 3)}

    def rebuild_index(self):
        """Reload the index from the database and bump index_version (old results become 'stale')."""
        with self._lock:
            self.idx = IndexManager(self.idx.index.name, self.idx.cache.capacity)
            self._load_index()
            self.index_version += 1
            self.store.set_meta("index_version", self.index_version)
            return {"index_version": self.index_version, "entries": len(self.idx.index)}

    def metrics_report(self):
        m = self.metrics
        return {"index": {"type": self.idx.index.name, "entries": len(self.idx.index),
                          "avl_height": self.idx.index.height(), "index_version": self.index_version},
                "cache": self.idx.cache.stats(), "lookup_us": summary(list(m.lookup_us)),
                "compute_ms": summary(list(m.compute_ms)), "verify_ms": summary(list(m.verify_ms)),
                "requests": {"total": m.requests, "failed_5xx": m.failed}}
