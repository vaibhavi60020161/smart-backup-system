# Team B API Guide (for Team C)

**Service:** Deduplication & Integrity Engine  **Version:** v1  **Owner:** Team B (Shubhangi, Vaibhavi, Shreya, Mira)

## 1. Connection details (Team B fills these in)

| Item | Value |
|---|---|
| Base URL | `http://<TEAM_B_IP>:8000` |
| Token | `dev-token` (sent as `Authorization: Bearer dev-token`) |
| Test page | `http://<TEAM_B_IP>:8000/docs` |
| Contract file | `http://<TEAM_B_IP>:8000/openapi.json` |

Both computers must be on the same network. Team B must keep the server running.

## 2. Headers on every call

| Header | Needed | Meaning |
|---|---|---|
| `Authorization` | Always | `Bearer dev-token` |
| `Idempotency-Key` | Only for `/dedup/compute` | A new unique value per backup. Same key + same request returns the old answer and stores nothing twice |
| `X-Correlation-ID` | Optional | Your trace id. We send it back and log it. We create one if missing |

## 3. The flow

1. **Upload the file** (skip if the file is already on Team B's computer)
   `PUT /api/v1/files/{file_id}` with the raw file bytes as the body.
   Returns `{"data": {"file_id", "file_path", "size"}}`. Use the returned `file_path` in step 2.
2. **Chunk and dedup**
   `POST /api/v1/dedup/compute`
   ```json
   {"file_id": "F1", "file_path": "<path>", "backup_id": "B1",
    "chunking": "fixed", "chunk_size": 65536}
   ```
   `chunking` (`fixed` or `content`), `chunk_size` (256 to 1048576) and `hash_algorithm` (only `sha256`) are optional.
   Optional: `expected_index_version`, `previous_result_id`.
3. **Verify integrity**
   `POST /api/v1/integrity/verify` with `{"dedup_result_id": "DR-..."}` (or `{"file_id": "F1"}`)
4. **Read a saved result**
   `GET /api/v1/dedup/{dedup_result_id}` (add `?include_chunks=true` for the chunk list)

## 4. What comes back

Success:
```json
{"data": { ... }, "meta": {"correlation_id": "...", "api_version": "v1"}}
```
Error:
```json
{"error": {"code": "FILE_NOT_FOUND", "message": "...", "details": []}, "meta": {"correlation_id": "..."}}
```

`/dedup/compute` returns: `dedup_result_id`, `chunk_count`, `unique_chunks`, `duplicate_chunks`,
`delta_size` (bytes newly stored), `savings_ratio` (0 to 1), `merkle_root`, `file_checksum`,
`index_version`, `chunk_version`, `chunk_ids`.

`/integrity/verify` returns: `integrity_status` (`VERIFIED` or `FAILED`), `verified_chunks`,
`corrupted_chunks`, `missing_chunks`, `verification_latency_ms`.

## 5. Error codes

| Code | HTTP | Meaning / what to do |
|---|---|---|
| `UNAUTHORIZED` | 401 | Token missing or wrong |
| `MISSING_IDEMPOTENCY_KEY` | 400 | Add the header |
| `VALIDATION_ERROR` | 422 | A field is missing or out of range (see `details`) |
| `IDEMPOTENCY_KEY_REUSED` | 422 | Same key sent with a different request. Use a new key |
| `FILE_NOT_FOUND` | 404 | `file_path` does not exist on Team B's computer |
| `PATH_NOT_ALLOWED` | 403 | `file_path` is outside the allowed folder. Use the upload call |
| `FILE_TOO_LARGE` | 413 | Upload is over the limit (200 MB by default) |
| `HASH_ALGORITHM_MISMATCH` | 409 | Only `sha256` is supported |
| `STALE_INDEX_VERSION` | 409 | `expected_index_version` is old. Read the current one from `/health` |
| `HASH_COLLISION` | 409 | Two different chunks gave the same hash. Nothing was saved |
| `NOT_FOUND` | 404 | Unknown result id |

## 6. Python example

```python
import requests, uuid
BASE, H = "http://<TEAM_B_IP>:8000", {"Authorization": "Bearer dev-token"}

with open("report.pdf", "rb") as f:                                   # 1. upload
    up = requests.put(f"{BASE}/api/v1/files/F1", headers=H, data=f).json()["data"]

r = requests.post(f"{BASE}/api/v1/dedup/compute",                     # 2. dedup
                  headers={**H, "Idempotency-Key": str(uuid.uuid4())},
                  json={"file_id": "F1", "file_path": up["file_path"], "backup_id": "B1"}).json()
result = r["data"]; print(result["unique_chunks"], result["savings_ratio"])

v = requests.post(f"{BASE}/api/v1/integrity/verify", headers=H,       # 3. verify
                  json={"dedup_result_id": result["dedup_result_id"]}).json()
print(v["data"]["integrity_status"])
```
Needs `pip install requests`.

## 7. Agreed rules

- Field names above are frozen. A change needs Team B and Team C to agree first (breaking changes go to `/v2`).
- A retry with the same `Idempotency-Key` never creates a second result.
- Every response carries `X-Correlation-ID` so one backup can be traced across all teams.
- The Merkle root is built from chunk hashes in file order. An odd node pairs with itself.
- Team C owns the backup state and the main database. Team B only stores chunks and its hash index.
