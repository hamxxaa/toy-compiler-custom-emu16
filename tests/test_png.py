#!/usr/bin/env python3
"""Round-trip test for tools/png.py: build a known 4x4 indexed PNG with the stdlib (every scanline
filter), read it back, and assert the indices + palette survive. No third-party deps."""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import png  # noqa: E402


def _chunk(ctype, body):
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def _encode_row(row, prev, ft):
    """Apply PNG filter `ft` to one scanline (the inverse of png._unfilter)."""
    w = len(row)
    enc = bytearray(w)
    for x in range(w):
        left = row[x - 1] if x > 0 else 0
        upleft = prev[x - 1] if x > 0 else 0
        if ft == 0:
            pred = 0
        elif ft == 1:
            pred = left
        elif ft == 2:
            pred = prev[x]
        elif ft == 3:
            pred = (left + prev[x]) >> 1
        else:
            pred = _paeth(left, prev[x], upleft)
        enc[x] = (row[x] - pred) & 0xFF
    return enc


def make_indexed_png(width, height, indices, palette, ft):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0)   # 8-bit indexed, non-interlaced
    plte = b"".join(bytes(c) for c in palette)
    raw = bytearray()
    prev = bytearray(width)
    for y in range(height):
        row = bytearray(indices[y * width:(y + 1) * width])
        raw.append(ft)
        raw += _encode_row(row, prev, ft)
        prev = row
    idat = zlib.compress(bytes(raw))
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"PLTE", plte) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def main():
    width, height = 4, 4
    indices = bytes(range(16))                                    # 0,1,...,15
    palette = [(i * 16, 255 - i * 16, i * 8) for i in range(16)]
    out_dir = os.path.join(os.path.dirname(__file__), "assets")
    os.makedirs(out_dir, exist_ok=True)

    ok = True
    for ft in range(5):                                           # None, Sub, Up, Average, Paeth
        path = os.path.join(out_dir, f"png_ft{ft}.png")
        with open(path, "wb") as f:
            f.write(make_indexed_png(width, height, indices, palette, ft))
        img = png.read(path)
        if (img.width, img.height) != (width, height):
            print(f"FAIL ft{ft}: dims {img.width}x{img.height}"); ok = False
        elif bytes(img.indices) != indices:
            print(f"FAIL ft{ft}: indices {list(img.indices)}"); ok = False
        elif img.palette[:16] != palette:
            print(f"FAIL ft{ft}: palette mismatch"); ok = False
        else:
            print(f"PASS ft{ft} (filter {ft})")
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
