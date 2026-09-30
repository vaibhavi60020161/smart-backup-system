import base64, hashlib, os
import pytest
from fastapi.testclient import TestClient
from app.engine import Engine
from app.main import create_app

AUTH = {"Authorization": "Bearer t0k"}
DATA = os.urandom(4096 * 10)


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Engine(str(tmp_path / "s")), token="t0k"))


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "f.bin"; p.write_bytes(DATA); return str(p)


def body(path, **kw):
    d = {"file_id": "F1", "file_path": path, "backup_id": "B1"}; d.update(kw); return d


def post(client, path, key="k1", **kw):
    return client.post("/api/v1/dedup/compute", json=body(path, **kw), headers={**AUTH, "Idempotency-Key": key})


def test_health_open_and_auth_required(client):
    assert client.get("/health").json()["data"]["status"] == "UP"
    r = client.get("/api/v1/metrics/dedup")
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"
    assert client.get("/api/v1/metrics/dedup", headers={"Authorization": "Bearer bad"}).status_code == 401


def test_envelope_and_correlation_id(client, path):
    r = post(client, path)
    j = r.json()
    assert r.status_code == 200 and j["meta"]["api_version"] == "v1"
    assert j["data"]["chunk_count"] == 10 and j["data"]["unique_chunks"] == 10
    assert r.headers["X-Correlation-ID"] == j["meta"]["correlation_id"]
    r = client.get("/api/v1/metrics/dedup", headers={**AUTH, "X-Correlation-ID": "trace-123"})
    assert r.headers["X-Correlation-ID"] == "trace-123" and r.json()["meta"]["correlation_id"] == "trace-123"


def test_idempotency(client, path):
    a, b = post(client, path), post(client, path)
    assert b.headers.get("Idempotent-Replay") == "true"
    assert a.json()["data"]["dedup_result_id"] == b.json()["data"]["dedup_result_id"]
    refs = client.get("/api/v1/chunks/index", headers=AUTH).json()["data"]
    assert refs["logical_bytes"] == len(DATA)                 # counted once, not twice
    r = post(client, path, chunk_size=1024)                   # same key, different request
    assert r.status_code == 422 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    r = client.post("/api/v1/dedup/compute", json=body(path), headers=AUTH)
    assert r.status_code == 400 and r.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"


def test_validation_and_not_found_errors(client, path):
    r = post(client, path, chunk_size=5)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = post(client, path + ".nope", key="k9")
    assert r.status_code == 404 and r.json()["error"]["code"] == "FILE_NOT_FOUND"
    r = post(client, path, key="k8", hash_algorithm="md5")
    assert r.status_code == 409 and r.json()["error"]["code"] == "HASH_ALGORITHM_MISMATCH"
    assert client.get("/api/v1/dedup/DR-nope", headers=AUTH).status_code == 404


def test_full_flow_verify_and_tamper(client, path):
    d = post(client, path, chunking="content", chunk_size=2048).json()["data"]
    rid = d["dedup_result_id"]
    got = client.get(f"/api/v1/dedup/{rid}?include_chunks=true", headers=AUTH).json()["data"]
    assert got["merkle_root"] == d["merkle_root"] and got["chunk_ids"] == d["chunk_ids"]
    v = client.post("/api/v1/integrity/verify", json={"dedup_result_id": rid}, headers=AUTH).json()["data"]
    assert v["integrity_status"] == "VERIFIED" and v["corrupted_chunks"] == []
    eng = client.app.state.engine
    open(eng.chunk_path(d["chunk_ids"][0]), "wb").write(b"bad")
    v = client.post("/api/v1/integrity/verify", json={"file_id": "F1", "algorithm": "merkle", "repetitions": 3}, headers=AUTH).json()["data"]
    assert v["integrity_status"] == "FAILED" and len(v["corrupted_chunks"]) == 1 and len(v["runs_ms"]) == 3
    assert client.post("/api/v1/integrity/verify", json={}, headers=AUTH).status_code == 422


def test_index_lookup_cache_and_rebuild(client, path):
    d = post(client, path).json()["data"]
    key = d["chunk_ids"][0]
    r = client.get(f"/internal/v1/hash-index/{key}", headers=AUTH).json()["data"]
    assert r["entry"]["refcount"] == 1 and r["cache_hit"] is True
    assert client.post("/internal/v1/hash-cache/invalidate", json={"key": key}, headers=AUTH).json()["data"]["invalidated"] == 1
    assert client.get(f"/internal/v1/hash-index/{key}", headers=AUTH).json()["data"]["cache_hit"] is False
    assert client.get("/internal/v1/hash-index/unknown", headers=AUTH).status_code == 404
    assert client.post("/internal/v1/hash-cache/invalidate", json={}, headers=AUTH).status_code == 422
    assert client.post("/internal/v1/hash-index/rebuild", headers=AUTH).json()["data"]["index_version"] == 2
    r = post(client, path, key="k2", expected_index_version=1)
    assert r.status_code == 409 and r.json()["error"]["code"] == "STALE_INDEX_VERSION"


def test_put_chunk(client):
    data = b"hello chunk"; cid = hashlib.sha256(data).hexdigest()
    ok = client.put(f"/internal/v1/chunks/{cid}", json={"data_base64": base64.b64encode(data).decode()}, headers=AUTH)
    assert ok.status_code == 200 and ok.json()["data"]["created"] is True
    again = client.put(f"/internal/v1/chunks/{cid}", json={"data_base64": base64.b64encode(data).decode()}, headers=AUTH)
    assert again.json()["data"]["created"] is False
    bad = client.put(f"/internal/v1/chunks/{'0' * 64}", json={"data_base64": base64.b64encode(data).decode()}, headers=AUTH)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "CHUNK_HASH_MISMATCH"
    assert client.put(f"/internal/v1/chunks/{cid}", json={"data_base64": "!!"}, headers=AUTH).status_code == 422


def test_metrics_and_openapi(client, path):
    post(client, path)
    m = client.get("/api/v1/metrics/dedup", headers=AUTH).json()["data"]
    assert m["compute_ms"]["count"] == 1 and m["lookup_us"]["count"] == 10 and m["index"]["type"] == "hash"
    paths = client.get("/openapi.json").json()["paths"]
    for p in ["/api/v1/dedup/compute", "/api/v1/dedup/{result_id}", "/api/v1/chunks/index",
              "/internal/v1/chunks/{chunk_id}", "/api/v1/integrity/verify", "/api/v1/metrics/dedup",
              "/internal/v1/hash-index/{key}", "/internal/v1/hash-cache/invalidate"]:
        assert p in paths
