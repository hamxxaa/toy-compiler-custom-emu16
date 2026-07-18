#!/usr/bin/env python3
"""Test tools/image_import.py pattern rotation (stdlib only): the rotate_pattern primitive (90/180/270
CW, square + non-square), and the `rot=` project-list directive that bakes rotated copies of a sheet's
frames as extra slots. The PPU can't rotate at render, so a rotated tile is a baked pattern slot."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from test_png import make_indexed_png  # noqa: E402
import image_import  # noqa: E402

ok = True


def check(cond, msg):
    global ok
    print(("PASS " if cond else "FAIL ") + msg)
    ok = ok and cond


def main():
    # --- unit: rotate_pattern (turns are 90-deg CLOCKWISE steps; dims swap on 90/270) ---
    check(image_import.rotate_pattern(bytes([1, 2, 3, 4]), 2, 2, 1) == (bytes([3, 1, 4, 2]), 2, 2), "90 CW of 2x2")
    check(image_import.rotate_pattern(bytes([1, 2, 3, 4]), 2, 2, 2) == (bytes([4, 3, 2, 1]), 2, 2), "180 of 2x2")
    check(image_import.rotate_pattern(bytes([1, 2, 3, 4]), 2, 2, 3) == (bytes([2, 4, 1, 3]), 2, 2), "270 CW of 2x2")
    check(image_import.rotate_pattern(bytes([1, 2, 3, 4, 5, 6]), 3, 2, 1) == (bytes([4, 1, 5, 2, 6, 3]), 2, 3),
          "90 CW of 3x2 -> 2x3 (dims swap)")
    check(image_import.rotate_pattern(bytes([1, 2, 3, 4]), 2, 2, 4) == (bytes([1, 2, 3, 4]), 2, 2), "360 == identity")

    PALETTE = [(i, i, i) for i in range(16)]
    work = os.path.join(ROOT, "build", "rotate_test")
    src = os.path.join(work, "src")
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "master.png"), "wb") as f:
        f.write(make_indexed_png(1, 1, bytes([0]), PALETTE, 0))

    # --- integration: `rot=` bakes correct rotated slots after the base frame ---
    tile = bytearray(8 * 8)
    tile[0] = 5
    tile[1] = 6                                   # an asymmetric top-left mark so rotation is observable
    with open(os.path.join(src, "tile.png"), "wb") as f:
        f.write(make_indexed_png(8, 8, bytes(tile), PALETTE, 0))
    with open(os.path.join(src, "tile.json"), "w", encoding="utf-8") as f:
        json.dump({"frames": [{"filename": "t0", "frame": {"x": 0, "y": 0, "w": 8, "h": 8}, "duration": 125}]}, f)
    with open(os.path.join(src, "sprites.list"), "w", encoding="utf-8") as f:
        f.write("palette master.png - -\n")
        f.write("tile tile.png tile.json 8 rot=90,180,270\n")
    r = subprocess.run([sys.executable, "tools/image_import.py", os.path.join(src, "sprites.list"), work],
                       capture_output=True, text=True, cwd=ROOT)
    check(r.returncode == 0, f"importer ran with rot= (rc={r.returncode})\n{r.stderr}")
    if r.returncode == 0:
        blob = open(os.path.join(work, "tile.bin"), "rb").read()
        expected = bytes(tile)
        for deg in (90, 180, 270):
            expected += image_import.rotate_pattern(bytes(tile), 8, 8, deg // 90)[0]
        check(len(blob) == 64 * 4, "rot=90,180,270 baked base + 3 rotations = 256 bytes")
        check(blob == expected, "each rotated slot's bytes match rotate_pattern")

    # --- 90/270 on a non-square frame must fail loudly (its dims would change) ---
    with open(os.path.join(src, "wide.png"), "wb") as f:
        f.write(make_indexed_png(16, 8, bytes(16 * 8), PALETTE, 0))
    with open(os.path.join(src, "wide.json"), "w", encoding="utf-8") as f:
        json.dump({"frames": [{"filename": "w0", "frame": {"x": 0, "y": 0, "w": 16, "h": 8}, "duration": 125}]}, f)
    with open(os.path.join(src, "bad.list"), "w", encoding="utf-8") as f:
        f.write("palette master.png - -\nwide wide.png wide.json 8 rot=90\n")
    r2 = subprocess.run([sys.executable, "tools/image_import.py", os.path.join(src, "bad.list"), work],
                        capture_output=True, text=True, cwd=ROOT)
    check(r2.returncode != 0 and "square" in (r2.stdout + r2.stderr), "90/270 on a non-square frame errors")

    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
