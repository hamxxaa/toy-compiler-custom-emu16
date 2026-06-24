#!/usr/bin/env python3
"""Test tools/image_import.py end to end (stdlib only): build a master palette PNG + a 2-frame 8x8
sheet PNG + an Aseprite-style JSON, run the importer, and assert the manifest, blob sizes, RGB565
palette bytes, and generated const ids."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from test_png import make_indexed_png  # noqa: E402

PALETTE = [(i * 16, 255 - i * 16, i * 8) for i in range(16)]


def main():
    work = os.path.join(ROOT, "build", "assets_test")
    src = os.path.join(work, "src")
    os.makedirs(src, exist_ok=True)

    # master palette source: a 1x1 indexed PNG carrying the 16-entry palette
    with open(os.path.join(src, "master.png"), "wb") as f:
        f.write(make_indexed_png(1, 1, bytes([0]), PALETTE, 0))

    # 16x8 sheet = two 8x8 frames: frame0 filled with index 3, frame1 with index 7
    sheet = bytearray(16 * 8)
    for r in range(8):
        for c in range(16):
            sheet[r * 16 + c] = 3 if c < 8 else 7
    with open(os.path.join(src, "player.png"), "wb") as f:
        f.write(make_indexed_png(16, 8, bytes(sheet), PALETTE, 0))

    with open(os.path.join(src, "player.json"), "w", encoding="utf-8") as f:
        json.dump({
            "frames": [
                {"filename": "f0", "frame": {"x": 0, "y": 0, "w": 8, "h": 8}, "duration": 125},
                {"filename": "f1", "frame": {"x": 8, "y": 0, "w": 8, "h": 8}, "duration": 125},
            ],
            "meta": {"frameTags": [{"name": "walk", "from": 0, "to": 1, "direction": "forward"}]},
        }, f)

    with open(os.path.join(src, "sprites.list"), "w", encoding="utf-8") as f:
        f.write("palette master.png - -\n")
        f.write("player  player.png player.json 8\n")

    r = subprocess.run([sys.executable, "tools/image_import.py",
                        os.path.join(src, "sprites.list"), work],
                       capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        print("importer failed:\n", r.stdout, r.stderr)
        return 1

    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + msg)
        ok = ok and cond

    manifest = open(os.path.join(work, "assets.manifest")).read()
    check("palette palette 0 0 palette.bin" in manifest, "manifest has palette (id 0)")
    check("player_walk sprite 8 8 player_walk.bin" in manifest, "manifest has player_walk (id 1)")

    pal = open(os.path.join(work, "palette.bin"), "rb").read()
    # entry 0 = (0,255,0) -> rgb565 = 0x07E0 -> little-endian E0 07
    check(len(pal) == 512 and pal[0] == 0xE0 and pal[1] == 0x07, "palette.bin is 512B with entry0 0x07E0")

    sheet_bin = open(os.path.join(work, "player_walk.bin"), "rb").read()
    check(len(sheet_bin) == 8 * 8 * 2, "sheet blob is w*h*count = 128 bytes")
    check(all(b == 3 for b in sheet_bin[:64]) and all(b == 7 for b in sheet_bin[64:]),
          "frame0 = index 3, frame1 = index 7 (slicing + concatenation)")

    consts = open(os.path.join(work, "sprites.gen.txt")).read()
    import re
    norm = re.sub(r" +", " ", consts)   # collapse alignment padding so checks aren't whitespace-brittle
    # symbolic ids: the palette and the clip handle
    for needle in ("const PAL_PALETTE = 0;", "const ANIM_PLAYER_WALK_ID = 1;"):
        check(needle in norm, f"const include has `{needle.strip()}`")
    # the clip registry: id 0 = palette (zero row), id 1 = the 8x8 2-frame clip @125ms/frame
    for needle in ("const NUM_CLIPS = 2;",
                   "var byte clip_w[NUM_CLIPS] = { 0, 8 };",
                   "var byte clip_h[NUM_CLIPS] = { 0, 8 };",
                   "var byte clip_count[NUM_CLIPS] = { 0, 2 };",
                   "var int clip_period[NUM_CLIPS] = { 0, 125 };",
                   "var int clip_buf[NUM_CLIPS];"):
        check(needle in norm, f"registry has `{needle.strip()}`")

    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
