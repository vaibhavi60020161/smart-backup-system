"""Run: python test_engine.py   (uses a temporary folder, cleans up after)"""
import os, shutil, tempfile, dedup_engine as e

tmp = tempfile.mkdtemp(); os.chdir(tmp)
e.STORE = "store"; e.CHUNK_DIR = "store/chunks"; e.MANIFEST_DIR = "store/manifests"
e.INDEX_FILE = "store/hash_index.json"; e.AUDIT_FILE = "store/audit.log"

def make(name, data): open(name, "wb").write(data); return name
def req(fid, p): return {"file_id": fid, "file_path": p, "backup_id": "B1", "operation": "backup"}

data = os.urandom(4096 * 10)
a = make("a.bin", data)
b = make("b_copy.bin", data)                       # exact duplicate
c = make("c_mod.bin", data[:4096*9] + b"changed!") # last chunk differs

r1 = e.process_backup(req("F1", a)); print("1 original :", r1)
assert r1["duplicate_count"] == 0 and r1["integrity_status"] == "VERIFIED"
r2 = e.process_backup(req("F2", b)); print("2 duplicate:", r2)
assert r2["duplicate_count"] == 10
r3 = e.process_backup(req("F3", c)); print("3 modified :", r3)
assert r3["duplicate_count"] == 9
print("stats      :", e.stats())

assert e.restore_file("F1", "restored.bin") and open("restored.bin","rb").read() == data
print("4 restore OK, byte-identical")

victim = os.path.join(e.CHUNK_DIR, e.json.load(open("store/manifests/F1.json"))["chunks"][0])
open(victim, "wb").write(b"tampered")              # corrupt a stored chunk
print("5 tamper   :", e.verify_file("F1"))
assert e.verify_file("F1").startswith("FAILED")
assert not e.restore_file("F1", "bad.bin")
print("6 request validation:", e.process_backup(req("F9", "nope.txt")))
print("\nALL TESTS PASSED"); shutil.rmtree(tmp)
