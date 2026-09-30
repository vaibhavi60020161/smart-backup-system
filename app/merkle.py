"""B4 - Merkle tree. Rule: leaves are chunk hashes in file order; parent = SHA-256(left+right)
as hex text; an odd node is paired with itself. Same input always gives the same root."""
import hashlib


def _parent(a, b):
    return hashlib.sha256((a + b).encode()).hexdigest()


def merkle_levels(leaves):
    if not leaves:
        return [[hashlib.sha256(b"").hexdigest()]]
    levels = [list(leaves)]
    while len(levels[-1]) > 1:
        lvl = list(levels[-1])
        if len(lvl) % 2:
            lvl.append(lvl[-1])
        levels.append([_parent(lvl[i], lvl[i + 1]) for i in range(0, len(lvl), 2)])
    return levels


def merkle_root(leaves):
    return merkle_levels(leaves)[-1][0]


def merkle_proof(leaves, index):
    """Sibling hashes needed to prove one chunk belongs to the root: O(log n) size."""
    proof = []
    for lvl in merkle_levels(leaves)[:-1]:
        lvl = list(lvl)
        if len(lvl) % 2:
            lvl.append(lvl[-1])
        sib = index ^ 1
        proof.append((lvl[sib], "left" if index % 2 else "right"))
        index //= 2
    return proof


def verify_proof(leaf, proof, root):
    cur = leaf
    for sib, side in proof:
        cur = _parent(sib, cur) if side == "left" else _parent(cur, sib)
    return cur == root
