#!/usr/bin/env python3
"""Tool test for tools/pixel_map.py: paint a tiny indexed PNG, run the tool, check the emitted
world[] / tile_flags[] arrays. STDLIB only (writes a minimal indexed PNG via zlib)."""
import os
import struct
import subprocess
import sys
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _chunk(typ, data):
    return (struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))


def write_indexed_png(path, width, height, indices, palette):
    """Minimal 8-bit indexed (color type 3) PNG."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0)
    plte = b"".join(struct.pack("BBB", *c) for c in palette)
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (none)
        raw += bytes(indices[y * width:(y + 1) * width])
    idat = zlib.compress(bytes(raw))
    with open(path, "wb") as f:
        f.write(sig + _chunk(b"IHDR", ihdr) + _chunk(b"PLTE", plte)
                + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""))


def main():
    with tempfile.TemporaryDirectory() as d:
        png_path = os.path.join(d, "map.png")
        legend_path = os.path.join(d, "legend.txt")
        out_path = os.path.join(d, "map.gen.txt")

        # 4x3 tilemap: 0=grass, 1=water(solid), 2=tree(solid, just another tile).
        indices = [0, 0, 1, 0,
                   0, 2, 1, 0,
                   0, 0, 2, 0]
        palette = [(i, i, i) for i in range(16)]
        write_indexed_png(png_path, 4, 3, indices, palette)
        with open(legend_path, "w") as f:
            f.write("flags solid=0\n0 16\n1 17 solid\n2 20 solid\n")

        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "pixel_map.py"),
                            png_path, legend_path, out_path],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"tool failed: {r.stdout}\n{r.stderr}"
        out = open(out_path).read()

        checks = {
            "const MAP_W = 4;": "map width",
            "const MAP_H = 3;": "map height",
            "world[12] = { 16, 16, 17, 16, 16, 20, 17, 16, 16, 16, 20, 16 }": "world tilemap",
            # tile_flags[0..20]; water(17) and tree(20) solid
            "tile_flags[21] = { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 1 }": "tile flags",
        }
        for needle, what in checks.items():
            assert needle in out, f"FAIL ({what}): missing `{needle}`\n---\n{out}"

        # an index missing from the legend must be a hard error
        write_indexed_png(png_path, 1, 1, [9], palette)
        r2 = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "pixel_map.py"),
                             png_path, legend_path, out_path],
                            capture_output=True, text=True)
        assert r2.returncode != 0, "expected a failure for a palette index not in the legend"

    print("test_pixel_map: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
