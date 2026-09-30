"""
Smart File Backup System - Team B: Deduplication & Integrity Engine
Small-scale, dependency-free implementation (Python 3.8+).

Pipeline (matches your DFD): File -> Chunking -> SHA-256 -> Hash Index lookup
 -> Duplicate detection -> Store unique chunks -> Merkle tree -> Integrity verify
"""
import hashlib, json, os, sys, time, uuid

CHUNK_SIZE = 4096            # fixed-size chunking (your ADR decision)
STORE = "backup_store"       # everything is kept inside this folder
CHUNK_DIR = os.path.join(STORE, "chunks")        # D2 - unique data chunks
MANIFEST_DIR = os.path.join(STORE, "manifests")  # D3 - merkle / integrity records
INDEX_FILE = os.path.join(STORE, "hash_index.json")  # D1 - chunk hash index
AUDIT_FILE = os.path.join(STORE, "audit.log")


# ---------- helpers ----------
def init_store():
    os.makedirs(CHUNK_DIR, exist_ok=True)
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    if not os.path.exists(INDEX_FILE):
        save_index({})

def load_index():
    with open(INDEX_FILE) as f:
        return json.load(f)

def save_index(index):
    tmp = INDEX_FILE + ".tmp"          # write-then-rename = safe against crashes
    with open(tmp, "w") as f:
        json.dump(index, f, indent=2)
    os.replace(tmp, INDEX_FILE)

def audit(message):
    with open(AUDIT_FILE, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {message}\n")

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------- 1.0 chunking ----------
def chunk_file(path, chunk_size=CHUNK_SIZE):
    """Generator: yields fixed-size chunks (never loads whole file in memory)."""
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk_size)
            if not data:
                break
            yield data


# ---------- 6.0 Merkle tree ----------
def merkle_root(hashes):
    """Build a Merkle tree bottom-up from chunk hashes and return the root."""
    if not hashes:
        return sha256(b"")
    level = list(hashes)
    while len(level) > 1:
        if len(level) % 2 == 1:            # odd count -> duplicate last node
            level.append(level[-1])
        level = [sha256((level[i] + level[i + 1]).encode())
                 for i in range(0, len(level), 2)]
    return level[0]


# ---------- 3.0 - 5.0 store / dedup ----------
def chunk_path(h):
    return os.path.join(CHUNK_DIR, h)

def store_chunk(data, h, index):
    """Returns True if chunk was a duplicate, False if newly stored."""
    if h in index:
        # Hash match -> verify bytes really match (collision mitigation from your risk register)
        with open(chunk_path(h), "rb") as f:
            if f.read() != data:
                raise RuntimeError(f"Hash collision detected for {h}!")
        index[h]["refcount"] += 1
        return True
    with open(chunk_path(h), "wb") as f:
        f.write(data)
    index[h] = {"size": len(data), "refcount": 1}
    return False


# ---------- main operation (interface contract) ----------
def process_backup(request):
    """
    Request : {file_id, file_path, backup_id, operation}
    Response: {file_id, chunk_count, duplicate_count, integrity_status}
    """
    init_store()
    # Request Handler: validate
    path = request.get("file_path")
    if request.get("operation") != "backup" or not path or not os.path.isfile(path):
        return {"file_id": request.get("file_id"), "chunk_count": 0,
                "duplicate_count": 0, "integrity_status": "INVALID_REQUEST"}

    index = load_index()
    chunk_hashes, duplicates, total_bytes, new_bytes = [], 0, 0, 0
    for data in chunk_file(path):
        h = sha256(data)
        is_dup = store_chunk(data, h, index)
        chunk_hashes.append(h)
        total_bytes += len(data)
        if is_dup:
            duplicates += 1
        else:
            new_bytes += len(data)
    save_index(index)

    root = merkle_root(chunk_hashes)
    manifest = {"file_id": request["file_id"], "backup_id": request["backup_id"],
                "original_name": os.path.basename(path), "size": total_bytes,
                "chunks": chunk_hashes, "merkle_root": root}
    with open(os.path.join(MANIFEST_DIR, request["file_id"] + ".json"), "w") as f:
        json.dump(manifest, f, indent=2)

    status = verify_file(request["file_id"])
    audit(f"BACKUP file={path} id={request['file_id']} chunks={len(chunk_hashes)} "
          f"dups={duplicates} new_bytes={new_bytes} integrity={status}")
    return {"file_id": request["file_id"], "chunk_count": len(chunk_hashes),
            "duplicate_count": duplicates, "integrity_status": status}


# ---------- 7.0 integrity verification ----------
def verify_file(file_id):
    """Re-hash every stored chunk, rebuild Merkle root, compare with saved root."""
    mpath = os.path.join(MANIFEST_DIR, file_id + ".json")
    if not os.path.exists(mpath):
        return "NOT_FOUND"
    with open(mpath) as f:
        m = json.load(f)
    for h in m["chunks"]:
        p = chunk_path(h)
        if not os.path.exists(p):
            return "FAILED (missing chunk)"
        with open(p, "rb") as f:
            if sha256(f.read()) != h:
                return "FAILED (corrupted chunk)"
    if merkle_root(m["chunks"]) != m["merkle_root"]:
        return "FAILED (merkle root mismatch)"
    return "VERIFIED"


def restore_file(file_id, out_path):
    if verify_file(file_id) != "VERIFIED":
        audit(f"RESTORE REFUSED id={file_id}")
        return False
    with open(os.path.join(MANIFEST_DIR, file_id + ".json")) as f:
        m = json.load(f)
    with open(out_path, "wb") as out:
        for h in m["chunks"]:
            with open(chunk_path(h), "rb") as c:
                out.write(c.read())
    audit(f"RESTORE id={file_id} -> {out_path}")
    return True


def stats():
    init_store()
    index = load_index()
    unique_bytes = sum(v["size"] for v in index.values())
    logical_bytes = sum(v["size"] * v["refcount"] for v in index.values())
    saved = logical_bytes - unique_bytes
    pct = (saved / logical_bytes * 100) if logical_bytes else 0
    return {"unique_chunks": len(index), "logical_bytes": logical_bytes,
            "stored_bytes": unique_bytes, "saved_bytes": saved,
            "space_saved_percent": round(pct, 2)}


# ---------- simple CLI ----------
def backup_path(target, backup_id=None):
    backup_id = backup_id or "B" + time.strftime("%Y%m%d%H%M%S")
    files = [target] if os.path.isfile(target) else [
        os.path.join(r, n) for r, _, ns in os.walk(target) for n in ns
        if not os.path.abspath(os.path.join(r, n)).startswith(os.path.abspath(STORE))]
    for p in files:
        res = process_backup({"file_id": "F" + uuid.uuid4().hex[:8], "file_path": p,
                              "backup_id": backup_id, "operation": "backup"})
        print(f"{p}: {res}")

if __name__ == "__main__":
    usage = ("Usage:\n  python dedup_engine.py backup <file_or_folder>\n"
             "  python dedup_engine.py verify <file_id>\n"
             "  python dedup_engine.py restore <file_id> <output_path>\n"
             "  python dedup_engine.py stats")
    if len(sys.argv) < 2: sys.exit(usage)
    cmd = sys.argv[1]
    init_store()
    if cmd == "backup" and len(sys.argv) == 3: backup_path(sys.argv[2])
    elif cmd == "verify" and len(sys.argv) == 3: print(verify_file(sys.argv[2]))
    elif cmd == "restore" and len(sys.argv) == 4: print("Restored" if restore_file(sys.argv[2], sys.argv[3]) else "Restore failed")
    elif cmd == "stats": print(json.dumps(stats(), indent=2))
    else: sys.exit(usage)
