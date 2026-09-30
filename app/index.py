"""B3 - Hash index. Two interchangeable structures (hash map, AVL tree) + LRU cache in front."""
import threading
import time
from collections import OrderedDict


class HashMapIndex:
    name = "hash"

    def __init__(self):
        self._d = {}

    def get(self, key):
        return self._d.get(key)

    def put(self, key, value):
        self._d[key] = value

    def __len__(self):
        return len(self._d)

    def items(self):
        return iter(self._d.items())

    def height(self):
        return None


class _Node:
    __slots__ = ("key", "value", "left", "right", "height")

    def __init__(self, key, value):
        self.key, self.value, self.left, self.right, self.height = key, value, None, None, 1


def _h(n): return n.height if n else 0
def _upd(n): n.height = 1 + max(_h(n.left), _h(n.right))
def _bal(n): return _h(n.left) - _h(n.right)


def _rot_right(y):
    x = y.left
    y.left, x.right = x.right, y
    _upd(y); _upd(x)
    return x


def _rot_left(x):
    y = x.right
    x.right, y.left = y.left, x
    _upd(x); _upd(y)
    return y


def _rebalance(n):
    _upd(n)
    b = _bal(n)
    if b > 1:
        if _bal(n.left) < 0:
            n.left = _rot_left(n.left)
        return _rot_right(n)
    if b < -1:
        if _bal(n.right) > 0:
            n.right = _rot_right(n.right)
        return _rot_left(n)
    return n


class AVLIndex:
    """Self-balancing binary search tree: lookup and insert are O(log n) worst case."""
    name = "avl"

    def __init__(self):
        self._root, self._size = None, 0

    def get(self, key):
        n = self._root
        while n:
            if key == n.key:
                return n.value
            n = n.left if key < n.key else n.right
        return None

    def put(self, key, value):
        self._root = self._insert(self._root, key, value)

    def _insert(self, n, key, value):
        if n is None:
            self._size += 1
            return _Node(key, value)
        if key == n.key:
            n.value = value
            return n
        if key < n.key:
            n.left = self._insert(n.left, key, value)
        else:
            n.right = self._insert(n.right, key, value)
        return _rebalance(n)

    def __len__(self):
        return self._size

    def items(self):
        stack, n = [], self._root
        while stack or n:
            while n:
                stack.append(n)
                n = n.left
            n = stack.pop()
            yield n.key, n.value
            n = n.right

    def height(self):
        return _h(self._root)


class LRUCache:
    def __init__(self, capacity=50000):
        self.capacity, self.hits, self.misses = capacity, 0, 0
        self._d, self._lock = OrderedDict(), threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                self.hits += 1
                return self._d[key]
            self.misses += 1
            return None

    def put(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.capacity:
                self._d.popitem(last=False)

    def invalidate(self, key=None):
        with self._lock:
            if key is None:
                n = len(self._d)
                self._d.clear()
                return n
            return 1 if self._d.pop(key, None) is not None else 0

    def __len__(self):
        return len(self._d)

    def stats(self):
        total = self.hits + self.misses
        return {"hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
                "size": len(self._d), "capacity": self.capacity}


class IndexManager:
    """lookup(): LRU cache -> index structure. Returns (entry, cache_hit, microseconds)."""

    def __init__(self, index_type="hash", cache_size=50000):
        self.index = AVLIndex() if index_type == "avl" else HashMapIndex()
        self.cache = LRUCache(cache_size)

    def lookup(self, key):
        t = time.perf_counter_ns()
        v = self.cache.get(key)
        hit = v is not None
        if not hit:
            v = self.index.get(key)
            if v is not None:
                self.cache.put(key, v)
        return v, hit, (time.perf_counter_ns() - t) / 1000.0

    def add(self, key, value):
        self.index.put(key, value)
        self.cache.put(key, value)

    def raw_get(self, key):
        return self.index.get(key)
