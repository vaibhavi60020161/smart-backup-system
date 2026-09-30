"""KPI evidence for Team B. Run:  python benchmark.py   (writes benchmark_results.json)"""
import json, os, random, shutil, statistics, tempfile, time, tracemalloc
from app.engine import Engine, percentile
from app.index import AVLIndex, HashMapIndex

R = {}
tmp = tempfile.mkdtemp()


def line(t): print("\n" + t + "\n" + "-" * len(t))


# 1) hash-index lookup: hash map vs AVL tree ------------------------------------------
line("1. Hash index lookup (200,000 keys)")
keys = [os.urandom(32).hex() for _ in range(200_000)]
probe = random.sample(keys, 20_000)
R["index"] = {}
for cls in (HashMapIndex, AVLIndex):
    tracemalloc.start()
    idx = cls()
    t = time.perf_counter()
    for k in keys:
        idx.put(k, {"size": 1, "refcount": 1})
    build = time.perf_counter() - t
    mem = tracemalloc.get_traced_memory()[0] / 1e6
    tracemalloc.stop()
    times = []
    for k in probe:
        t = time.perf_counter_ns(); idx.get(k); times.append((time.perf_counter_ns() - t) / 1000)
    R["index"][idx.name] = {"build_s": round(build, 2), "memory_mb": round(mem, 1),
                            "lookup_p50_us": percentile(times, 50), "lookup_p95_us": percentile(times, 95),
                            "avl_height": idx.height()}
    print(idx.name, R["index"][idx.name])
print("KPI: p95 lookup <= 1 ms (1000 us)")

# 2) compute latency for a 10 MB file -------------------------------------------------
line("2. Compute latency, 10 MB random file (fresh store each run)")
big = os.path.join(tmp, "big.bin")
open(big, "wb").write(os.urandom(10 * 1024 * 1024))
R["latency_10mb_ms"] = {}
for mode, runs in (("fixed", 7), ("content", 3)):
    ts = []
    for i in range(runs):
        eng = Engine(os.path.join(tmp, f"lat_{mode}_{i}"))
        t = time.perf_counter()
        eng.compute({"file_id": "F", "file_path": big, "backup_id": "B", "chunking": mode, "chunk_size": 4096})
        ts.append((time.perf_counter() - t) * 1000)
    R["latency_10mb_ms"][mode] = {"runs": runs, "p50": percentile(ts, 50), "p95": percentile(ts, 95)}
    print(mode, R["latency_10mb_ms"][mode])
print("KPI: p95 <= 100 ms per 10 MB file")

# 3) dedup ratio: fixed vs content-defined ---------------------------------------------
line("3. Dedup ratio on a 2 MB file (second backup is a modified copy)")
base = os.urandom(2 * 1024 * 1024)
variants = {"identical": base, "edit_in_middle": base[:1_000_000] + os.urandom(100) + base[1_000_100:],
            "insert_at_start": b"NEW HEADER" + base, "fully_unique": os.urandom(len(base))}
R["dedup_ratio"] = {}
for mode in ("fixed", "content"):
    R["dedup_ratio"][mode] = {}
    for name, data in variants.items():
        eng = Engine(os.path.join(tmp, f"dd_{mode}_{name}"))
        p1, p2 = os.path.join(tmp, "o.bin"), os.path.join(tmp, "m.bin")
        open(p1, "wb").write(base); open(p2, "wb").write(data)
        eng.compute({"file_id": "A", "file_path": p1, "backup_id": "B", "chunking": mode, "chunk_size": 4096})
        r = eng.compute({"file_id": "B", "file_path": p2, "backup_id": "B", "chunking": mode, "chunk_size": 4096})
        R["dedup_ratio"][mode][name] = r["savings_ratio"]
    print(mode, R["dedup_ratio"][mode])

# 4) cache hit rate on repeated backups -------------------------------------------------
line("4. Cache hit rate, same 10 MB file backed up 5 times")
eng = Engine(os.path.join(tmp, "cache"))
for i in range(5):
    eng.compute({"file_id": f"R{i}", "file_path": big, "backup_id": "B", "chunk_size": 4096})
R["cache"] = eng.idx.cache.stats()
t = time.perf_counter(); eng.invalidate_cache(); R["cache"]["invalidate_all_ms"] = round((time.perf_counter() - t) * 1000, 3)
print(R["cache"]); print("KPI: hit rate >= 80%, invalidation <= 2 s")

# 5) integrity verification: Merkle vs whole-file checksum -------------------------------
line("5. Verification latency, 10 MB file")
rid = eng.store.latest_result_for_file("R0")["result_id"]
R["verify_ms"] = {a: eng.verify(rid, algorithm=a, repetitions=5)["verification_latency_ms"] for a in ("merkle", "checksum")}
print(R["verify_ms"])
print("KPI: result available <= 500 ms after a committed backup")

json.dump(R, open("benchmark_results.json", "w"), indent=2)
print("\nSaved benchmark_results.json")
shutil.rmtree(tmp, ignore_errors=True)
