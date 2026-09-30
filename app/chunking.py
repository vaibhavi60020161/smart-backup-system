"""B1 - File chunking. Fixed-size (baseline) and content-defined (rolling hash)."""
import hashlib
import math

# Fixed random-looking table for the rolling ("gear") hash. Same on every run.
_GEAR = [int.from_bytes(hashlib.sha256(bytes([i])).digest()[:4], "big") for i in range(256)]


def fixed_chunks(f, chunk_size):
    """Cut every chunk_size bytes. Simple and fast, but an insert shifts every later chunk."""
    while True:
        data = f.read(chunk_size)
        if not data:
            break
        yield data


def content_defined_chunks(f, avg_size=4096, read_size=1 << 20):
    """Cut where the data itself says so (rolling hash), so an insert only changes nearby chunks.
    min = avg/4, max = avg*4. The boundary decision depends only on the last 32 bytes."""
    min_s, max_s = max(64, avg_size // 4), avg_size * 4
    bits = max(1, round(math.log2(avg_size)))
    mask = ((1 << bits) - 1) << (32 - bits)
    warm = 32
    gear = _GEAR
    buf, h = bytearray(), 0
    while True:
        block = f.read(read_size)
        if not block:
            break
        n, pos = len(block), 0
        while pos < n:
            length = len(buf)
            skip = min_s - warm - length          # bytes we can copy without hashing
            if skip > 0:
                take = min(skip, n - pos)
                buf += block[pos:pos + take]
                pos += take
                continue
            end = min(n, pos + (max_s - length))
            i, cut = pos, -1
            while i < end:
                h = ((h << 1) + gear[block[i]]) & 0xFFFFFFFF
                i += 1
                if length + (i - pos) >= min_s and not (h & mask):
                    cut = i
                    break
            if cut < 0:
                buf += block[pos:end]
                pos = end
                if len(buf) >= max_s:
                    yield bytes(buf)
                    buf, h = bytearray(), 0
            else:
                buf += block[pos:cut]
                pos = cut
                yield bytes(buf)
                buf, h = bytearray(), 0
    if buf:
        yield bytes(buf)
