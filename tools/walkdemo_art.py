#!/usr/bin/env python3
"""Generate placeholder art for the walkdemo, pure stdlib: a 16-colour master palette + a 2-frame
12x12 character sheet (indexed PNG) + an Aseprite-style JSON + a sprites.list. Normally you'd draw
this in LibreSprite; this stands in so the demo is self-contained and reproducible.

Run it, then feed assets/walkdemo/sprites.list to tools/image_import.py + tools/pack_assets.py.
"""
import json
import os
import struct
import zlib

W = H = 12
PALETTE = [
    (0, 0, 0),        # 0 transparent (never drawn)
    (40, 44, 92),     # 1 background (dark blue)
    (70, 200, 90),    # 2 body (green)
    (24, 24, 28),     # 3 outline (near-black)
    (240, 240, 240),  # 4 eye white
    (230, 90, 70),    # 5 accent (spare)
] + [(i * 8, i * 8, i * 8) for i in range(10)]   # 6..15 grey ramp, pad to 16


def char_frame(frame):
    px = [0] * (W * H)
    cx = cy = 5.5
    for y in range(H):
        for x in range(W):
            d2 = (x - cx) ** 2 + (y - cy) ** 2
            if d2 <= 15:
                px[y * W + x] = 2            # body
            elif d2 <= 24:
                px[y * W + x] = 3            # outline ring
    for ex in (3, 8):                        # eyes
        px[4 * W + ex] = 4
    feet = (3, 4, 7, 8) if frame == 0 else (2, 3, 8, 9)   # shuffle -> a walk cycle
    for fx in feet:
        px[10 * W + fx] = 3
    return px


def write_indexed_png(path, width, height, indices, palette):
    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)
    raw = bytearray()
    for y in range(height):
        raw.append(0)                        # filter type None
        raw += bytes(indices[y * width:(y + 1) * width])
    plte = b"".join(bytes(c) for c in palette)
    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0))
            + chunk(b"PLTE", plte)
            + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(data)


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(repo_root, "assets", "walkdemo")
    os.makedirs(src, exist_ok=True)

    write_indexed_png(os.path.join(src, "master.png"), 1, 1, [0], PALETTE)   # palette carrier

    f0, f1 = char_frame(0), char_frame(1)
    sheet = [0] * (24 * 12)                  # two 12x12 frames side by side
    for y in range(12):
        for x in range(12):
            sheet[y * 24 + x] = f0[y * 12 + x]
            sheet[y * 24 + x + 12] = f1[y * 12 + x]
    write_indexed_png(os.path.join(src, "char.png"), 24, 12, sheet, PALETTE)

    with open(os.path.join(src, "char.json"), "w", encoding="utf-8") as f:
        json.dump({
            "frames": [
                {"filename": "c0", "frame": {"x": 0, "y": 0, "w": 12, "h": 12}, "duration": 150},
                {"filename": "c1", "frame": {"x": 12, "y": 0, "w": 12, "h": 12}, "duration": 150},
            ],
            "meta": {"frameTags": [{"name": "walk", "from": 0, "to": 1, "direction": "forward"}]},
        }, f)

    with open(os.path.join(src, "sprites.list"), "w", encoding="utf-8") as f:
        f.write("master master.png - -\n")
        f.write("char   char.png   char.json  7\n")

    print("wrote art to", src)


if __name__ == "__main__":
    main()
