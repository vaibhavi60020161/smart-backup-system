import os, random
import pytest
from app.chunking import content_defined_chunks, fixed_chunks
from app.errors import ApiError
from app.engine import Engine
from app.index import AVLIndex, HashMapIndex
from app.merkle import merkle_proof, merkle_root, verify_proof
from .conftest import req

DATA = os.urandom(4096 * 20)


def split(fn, data, *a):
    import io
    return list(fn(io.BytesIO(data), *a))


# ---- chunking ----
def test_fixed_chunks_reassemble():
    parts = split(fixed_chunks, DATA + b"xyz", 4096)
    assert b"".join(parts) == DATA + b"xyz" and len(parts) == 21


def test_content_chunks_deterministic_and_reassemble():
    a, b = split(content_defined_chunks, DATA, 4096), split(content_defined_chunks, DATA, 4096)
    assert a == b and b"".join(a) == DATA
    assert all(len(c) <= 4096 * 4 for c in a)


def test_content_chunking_survives_insert_at_start():
    a = set(split(content_defined_chunks, DATA, 2048))
    b = split(content_defined_chunks, b"INSERTED" + DATA, 2048)
    assert sum(c in a for c in b) / len(b) > 0.9
    fa = set(split(fixed_chunks, DATA, 2048))
    fb = split(fixed_chunks, b"INSERTED" + DATA, 2048)
    assert sum(c in fa for c in fb) == 0            # fixed-size loses everything


# ---- merkle ----
def test_merkle_deterministic_and_proof():
    leaves = [f"{i:064x}" for i in range(7)]         # odd count
    root = merkle_root(leaves)
    assert root == merkle_root(list(leaves)) and root != merkle_root(leaves[::-1])
    for i in range(7):
        assert verify_proof(leaves[i], merkle_proof(leaves, i), root)
    assert not verify_proof("f" * 64, merkle_proof(leaves, 0), root)


# ---- index structures ----
def test_avl_balanced_and_correct():
    keys = [random.getrandbits(64).to_bytes(8, "big").hex() for _ in range(3000)]
    avl, hm = AVLIndex(), HashMapIndex()
    for k in keys:
        avl.put(k, {"k": k}); hm.put(k, {"k": k})
    assert len(avl) == len(hm) == len(set(keys))
    assert all(avl.get(k) == {"k": k} for k in keys) and avl.get("nope") is None
    assert avl.height() <= 1.45 * 12 + 2
    ordered = [k for k, _ in avl.items()]
    assert ordered == sorted(set(keys))


# ---- dedup accuracy ----
def test_identical_partial_unique(engine, make_file):
    a = make_file("a.bin", DATA)
    r1 = engine.compute(req("F1", a))
    assert (r1["chunk_count"], r1["unique_chunks"], r1["duplicate_chunks"]) == (20, 20, 0)
    r2 = engine.compute(req("F2", make_file("b.bin", DATA)))
    assert (r2["unique_chunks"], r2["duplicate_chunks"], r2["delta_size"], r2["savings_ratio"]) == (0, 20, 0, 1.0)
    r3 = engine.compute(req("F3", make_file("c.bin", DATA[:-4096] + os.urandom(4096)), previous_result_id=r1["dedup_result_id"]))
    assert (r3["unique_chunks"], r3["duplicate_chunks"]) == (1, 19) and r3["changed_chunks_vs_previous"] == 1
    r4 = engine.compute(req("F4", make_file("d.bin", os.urandom(4096 * 5))))
    assert r4["duplicate_chunks"] == 0 and r4["savings_ratio"] == 0.0
    assert engine.chunk_index()["coverage"]["orphan_chunks"] == 0


def test_repeat_inside_one_file(engine, make_file):
    block = os.urandom(4096)
    r = engine.compute(req("F1", make_file("r.bin", block * 5)))
    assert (r["chunk_count"], r["unique_chunks"], r["duplicate_chunks"]) == (5, 1, 4)


def test_empty_file(engine, make_file):
    r = engine.compute(req("E", make_file("e.bin", b"")))
    assert r["chunk_count"] == 0 and engine.verify(r["dedup_result_id"])["integrity_status"] == "VERIFIED"


# ---- errors ----
def test_hash_collision_detected(engine, make_file):
    engine.hash_bytes = lambda d: "a" * 64
    with pytest.raises(ApiError) as e:
        engine.compute(req("F1", make_file("x.bin", os.urandom(8192))))
    assert e.value.code == "HASH_COLLISION"
    assert engine.chunk_index()["total_chunks"] == 0        # nothing half-saved


def test_index_version_and_algorithm(engine, make_file):
    f = make_file("a.bin", DATA)
    r = engine.compute(req("F1", f))
    assert r["index_version"] == 1
    with pytest.raises(ApiError) as e:
        engine.compute(req("F2", f, expected_index_version=99))
    assert e.value.code == "STALE_INDEX_VERSION"
    with pytest.raises(ApiError) as e:
        engine.compute(req("F2", f, hash_algorithm="md5"))
    assert e.value.code == "HASH_ALGORITHM_MISMATCH"
    assert engine.rebuild_index()["index_version"] == 2
    assert engine.compute(req("F3", f))["index_version"] == 2
    with pytest.raises(ApiError):
        engine.compute(req("F4", f, expected_index_version=1))


def test_missing_file_and_root_restriction(tmp_path, make_file):
    eng = Engine(str(tmp_path / "s"), allowed_root=str(tmp_path / "ok"))
    with pytest.raises(ApiError) as e:
        eng.compute(req("F", make_file("outside.bin", b"1")))
    assert e.value.code == "PATH_NOT_ALLOWED"
    with pytest.raises(ApiError) as e:
        Engine(str(tmp_path / "s2")).compute(req("F", str(tmp_path / "none.bin")))
    assert e.value.code == "FILE_NOT_FOUND"


# ---- integrity ----
def test_verify_detects_corruption_and_missing(engine, make_file):
    r = engine.compute(req("F1", make_file("a.bin", DATA)))
    rid = r["dedup_result_id"]
    v = engine.verify(rid)
    assert v["integrity_status"] == "VERIFIED" and v["verified_chunks"] == 20
    assert engine.verify(rid, algorithm="checksum")["integrity_status"] == "VERIFIED"
    h0, h1 = r["chunk_ids"][0], r["chunk_ids"][1]
    open(engine.chunk_path(h0), "wb").write(b"tampered")
    os.remove(engine.chunk_path(h1))
    v = engine.verify(file_id="F1")
    assert v["integrity_status"] == "FAILED" and v["corrupted_chunks"] == [h0] and v["missing_chunks"] == [h1]
    assert v["computed_merkle_root"] != v["merkle_root"]
    assert engine.verify(rid, algorithm="checksum")["integrity_status"] == "FAILED"


def test_lost_chunk_file_self_heals(engine, make_file):
    f = make_file("a.bin", DATA)
    r = engine.compute(req("F1", f))
    os.remove(engine.chunk_path(r["chunk_ids"][0]))
    r2 = engine.compute(req("F2", f))
    assert engine.verify(r2["dedup_result_id"])["integrity_status"] == "VERIFIED"


# ---- persistence, cache ----
def test_persistence_across_restart(tmp_path, make_file):
    f = make_file("a.bin", DATA)
    Engine(str(tmp_path / "s")).compute(req("F1", f))
    r = Engine(str(tmp_path / "s")).compute(req("F2", f))
    assert r["duplicate_chunks"] == 20


def test_cache_hit_rate_and_invalidate(engine, make_file):
    f = make_file("a.bin", DATA)
    r = engine.compute(req("F1", f))
    for i in range(4):
        engine.compute(req(f"R{i}", f))
    assert engine.idx.cache.stats()["hit_rate"] >= 0.8
    key = r["chunk_ids"][0]
    assert engine.index_lookup(key)["cache_hit"] is True
    assert engine.invalidate_cache(key)["invalidated"] == 1
    assert engine.index_lookup(key)["cache_hit"] is False
    assert engine.index_lookup(key)["cache_hit"] is True
    assert engine.invalidate_cache()["invalidated"] >= 1
