# Team B - Deduplication & Integrity Engine

Part of the Smart File Backup System (RIT CSE Mini Project). Team C calls this service to chunk a file,
find duplicate chunks, build a Merkle tree and verify integrity.

## Technology used

| Purpose | Technology |
|---|---|
| Language | Python 3.10+ |
| REST API + OpenAPI contract | FastAPI + Uvicorn |
| Hashing | SHA-256 (`hashlib`) |
| Chunking | Fixed-size (baseline) and content-defined (rolling "gear" hash) |
| Hash index | Hash map (`dict`) **or** AVL tree, selectable, with an LRU cache in front |
| Integrity | Merkle tree (+ whole-file checksum for comparison) |
| Database | SQLite (built into Python) for records; chunk bytes stored as files |
| Tests | pytest + FastAPI TestClient |

## Setup and run

```
pip install -r requirements.txt
python -m pytest -q                               # 33 tests
python benchmark.py                               # KPI evidence -> benchmark_results.json
python -m uvicorn app.main:app --port 8000        # start the API
```
Open http://localhost:8000/docs to try every endpoint in the browser (click **Authorize**, enter `dev-token`).
The OpenAPI contract is at http://localhost:8000/openapi.json.

Settings (environment variables): `TEAMB_TOKEN` (default `dev-token`), `TEAMB_DATA_DIR`, `TEAMB_INDEX`
(`hash` or `avl`), `TEAMB_CACHE_SIZE`, `TEAMB_ALLOWED_ROOT` (limit which folder file_path may read), `TEAMB_MAX_UPLOAD_MB` (default 200).

## Endpoints (from the blueprint)

| Method | Path | Purpose |
|---|---|---|
| PUT | /api/v1/files/{file_id} | Upload a file (raw bytes as body). Returns a `file_path` for /dedup/compute |
| POST | /api/v1/dedup/compute | Chunk + hash + dedup a file. Needs `Idempotency-Key` header |
| GET | /api/v1/dedup/{result_id} | Read a dedup result (`?include_chunks=true`) |
| GET | /api/v1/chunks/index | Chunk coverage, status, chunk version |
| PUT | /internal/v1/chunks/{chunk_id} | Create/update one chunk (base64 body) |
| POST | /api/v1/integrity/verify | Merkle (or checksum) verification |
| GET | /api/v1/metrics/dedup | Latency, cache and index metrics |
| GET | /internal/v1/hash-index/{key} | Look up one hash |
| POST | /internal/v1/hash-cache/invalidate | Invalidate one key or `all` |
| POST | /internal/v1/hash-index/rebuild | Reload index and bump `index_version` |
| GET | /health | Readiness check (no token) |

All calls except `/health` need `Authorization: Bearer <token>`. `X-Correlation-ID` is echoed back (or generated).
Responses use `{"data":..., "meta":{...}}` and errors use `{"error":{code,message,details}, "meta":{...}}`.

### Example request (Team C -> Team B)
```
POST /api/v1/dedup/compute
Headers: Authorization: Bearer dev-token, Idempotency-Key: backup-001, X-Correlation-ID: abc-1
{"file_id":"F1","file_path":"files/report.pdf","backup_id":"B1","chunking":"fixed","chunk_size":4096}
```
Response `data` includes: `dedup_result_id, chunk_count, unique_chunks, duplicate_chunks, delta_size,
savings_ratio, merkle_root, index_version, chunk_version, chunk_ids`.

## Design rules
- **Merkle rule:** leaves = chunk hashes in file order; parent = SHA-256(left + right) as hex text; an odd node pairs with itself.
- **Duplicate check:** a hash match is confirmed by comparing bytes, so a collision raises `HASH_COLLISION`.
- **All-or-nothing:** chunk records, refcounts and the result are saved in one database transaction.
- **Idempotency:** same key + same request returns the original result (`Idempotent-Replay: true`); same key + different request returns 422.
- **index_version:** returned in every result; `expected_index_version` in a request is rejected with 409 if stale. Only `sha256` is accepted.

## Using MongoDB instead of SQLite
Only `app/storage.py` talks to the database. To switch, keep the same method names and implement them with `pymongo`
(collections: chunks, results, result_chunks, idempotency, meta). Use `update_one(..., {"$inc": {"refcount": n}}, upsert=True)`
for refcounts. Team C still owns the main metadata database; this store is Team B's private chunk index.

## Known limits (state them honestly in the review)
- The content-defined chunker is pure Python, so it is slow (about 1.8 s for 10 MB in our run). Fixed-size is the fast baseline.
- One file per chunk on disk: with 4 KB chunks a 10 MB file writes about 2,500 files. Larger chunks (64 KB) are much faster.
- Local files only; no encryption or login of users (bearer token only). Those belong to the shared services.
- A single process with a lock. It is not tested at 200 requests/s.
