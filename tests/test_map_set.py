#!/usr/bin/env python3
"""Tool test for tools/map_set.py: build a tiny 2-map set, assert the exact blob bytes (header
fields + every section), the manifest/id-include output, and that the validation errors (legend
mismatch, base below the sprite-slot line, unknown warp target, unresolved tileset with no
--assets) all fail loudly. STDLIB only (writes minimal indexed PNGs via zlib, like
tests/test_pixel_map.py)."""
import os
import struct
import subprocess
import sys
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "map_set.py")


def _chunk(typ, data):
    return (struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))


def write_indexed_png(path, width, height, indices, palette):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0)
    plte = b"".join(struct.pack("BBB", *c) for c in palette)
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += bytes(indices[y * width:(y + 1) * width])
    idat = zlib.compress(bytes(raw))
    with open(path, "wb") as f:
        f.write(sig + _chunk(b"IHDR", ihdr) + _chunk(b"PLTE", plte)
                + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""))


def run_tool(args):
    return subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True)


PALETTE = [(i, i, i) for i in range(16)]


def write_forest(maps_dir):
    write_indexed_png(os.path.join(maps_dir, "forest.png"), 2, 2, [0, 0, 0, 1], PALETTE)
    with open(os.path.join(maps_dir, "forest.map.txt"), "w") as f:
        f.write(
            "tileset  forest_tiles\n"
            "palette  master\n"
            "base     80\n"
            "entry    0  0 0  down\n"
            "spawn    slime  1 0\n"
            "warp     1 1  cave  0\n"
        )


def write_cave(maps_dir, base=90):
    write_indexed_png(os.path.join(maps_dir, "cave.png"), 2, 1, [1, 0], PALETTE)
    with open(os.path.join(maps_dir, "cave.map.txt"), "w") as f:
        f.write(
            "tileset  forest_tiles\n"
            "palette  master\n"
            f"base     {base}\n"
            "entry    0  0 0  side\n"
        )


def write_legend(path):
    with open(path, "w") as f:
        f.write("0 tile 5 0\n1 tile 6 1\n")   # tile 5 = walkable, tile 6 = solid


def test_happy_path_with_assets():
    with tempfile.TemporaryDirectory() as d:
        maps_dir = os.path.join(d, "maps")
        os.makedirs(maps_dir)
        write_forest(maps_dir)
        write_cave(maps_dir)
        legend_path = os.path.join(d, "legend.txt")
        write_legend(legend_path)

        # Two assets that would precede the maps in a real game's manifest.
        assets_path = os.path.join(d, "sprites.manifest")
        with open(assets_path, "w") as f:
            f.write("master palette 0 0 master.bin\nforest_tiles sprite 16 16 forest_tiles.bin\n")

        out_dir = os.path.join(d, "out")
        r = run_tool([maps_dir, legend_path, out_dir, "--assets", assets_path])
        assert r.returncode == 0, f"tool failed:\n{r.stdout}\n{r.stderr}"

        forest = open(os.path.join(out_dir, "forest.map"), "rb").read()
        cave = open(os.path.join(out_dir, "cave.map"), "rb").read()

        # forest: W=2 H=2 base=80 tileset_id=1(forest_tiles) palette_id=0(master)
        #         flags_count=7 spawn_count=1 warp_count=1 entry_count=1
        expected_header = struct.pack("<2sBBBBBBBBBB4x", b"MP", 1, 2, 2, 80, 1, 0, 7, 1, 1, 1)
        assert forest[:16] == expected_header, f"forest header mismatch: {forest[:16].hex()}"
        off = 16
        assert forest[off:off + 7] == bytes([0, 0, 0, 0, 0, 0, 1]); off += 7    # tile_flags
        assert forest[off:off + 3] == bytes([0, 0, 2]); off += 3               # entry 0: (0,0,down=2)
        assert forest[off:off + 4] == bytes([0, 1, 0, 0]); off += 4            # spawn: (slime=0,1,0,arg0)
        # warp: trigger (1,1) -> cave's id. Maps are processed in SORTED FILENAME order
        # ("cave.png" < "forest.png"), so cave is index 0 -> id = assets offset 2 + 0 = 2.
        assert forest[off:off + 5] == bytes([1, 1, 2, 0, 0]); off += 5
        assert forest[off:off + 4] == bytes([5, 5, 5, 6])                      # world 2x2
        assert len(forest) == off + 4 == 39

        # cave: W=2 H=1 base=90 tileset_id=1 palette_id=0 flags_count=7 spawn=0 warp=0 entry=1
        expected_cave_header = struct.pack("<2sBBBBBBBBBB4x", b"MP", 1, 2, 1, 90, 1, 0, 7, 0, 0, 1)
        assert cave[:16] == expected_cave_header, f"cave header mismatch: {cave[:16].hex()}"
        off = 16
        assert cave[off:off + 7] == bytes([0, 0, 0, 0, 0, 0, 1]); off += 7
        assert cave[off:off + 3] == bytes([0, 0, 0]); off += 3                 # entry 0: (0,0,side=0)
        assert cave[off:off + 2] == bytes([6, 5])                              # world 2x1
        assert len(cave) == off + 2 == 28

        manifest = open(os.path.join(out_dir, "maps.manifest")).read()
        assert "map_forest tilemap 2 2 forest.map" in manifest
        assert "map_cave tilemap 2 1 cave.map" in manifest

        gen = open(os.path.join(out_dir, "maps.gen.txt")).read()
        assert "const SPAWN_SLIME = 0;" in gen

        assert "cave=2" in r.stdout and "forest=3" in r.stdout, r.stdout


def test_without_assets_ids_start_at_zero():
    with tempfile.TemporaryDirectory() as d:
        maps_dir = os.path.join(d, "maps")
        os.makedirs(maps_dir)
        # A map with no tileset/palette references at all (both omitted -> 255) sidesteps needing
        # --assets; only tests the "maps are the only assets" ordering case.
        write_indexed_png(os.path.join(maps_dir, "solo.png"), 1, 1, [0], PALETTE)
        with open(os.path.join(maps_dir, "solo.map.txt"), "w") as f:
            f.write("base  80\nentry 0  0 0  down\n")
        legend_path = os.path.join(d, "legend.txt")
        write_legend(legend_path)
        out_dir = os.path.join(d, "out")

        r = run_tool([maps_dir, legend_path, out_dir])
        assert r.returncode == 0, f"tool failed:\n{r.stdout}\n{r.stderr}"
        blob = open(os.path.join(out_dir, "solo.map"), "rb").read()
        # tileset_id/palette_id both 255 (kept current); tile_base=80
        assert blob[5] == 80 and blob[6] == 255 and blob[7] == 255
        assert "solo=0" in r.stdout, r.stdout


def _expect_fail(maps_dir, legend_path, out_dir, extra_args=(), contains=None):
    r = run_tool([maps_dir, legend_path, out_dir, *extra_args])
    assert r.returncode != 0, f"expected failure but tool succeeded:\n{r.stdout}"
    if contains:
        assert contains in (r.stdout + r.stderr), f"expected '{contains}' in output:\n{r.stderr}"


def test_validation_errors():
    with tempfile.TemporaryDirectory() as d:
        legend_path = os.path.join(d, "legend.txt")
        write_legend(legend_path)

        # (1) legend mismatch: a palette index the legend doesn't define.
        maps_dir = os.path.join(d, "bad_legend")
        os.makedirs(maps_dir)
        write_indexed_png(os.path.join(maps_dir, "x.png"), 1, 1, [9], PALETTE)
        with open(os.path.join(maps_dir, "x.map.txt"), "w") as f:
            f.write("base 80\nentry 0 0 0 down\n")
        _expect_fail(maps_dir, legend_path, os.path.join(d, "out1"), contains="not in the legend")

        # (2) base below the sprite-slot line (default --sprite-slots 80).
        maps_dir = os.path.join(d, "bad_base")
        os.makedirs(maps_dir)
        write_indexed_png(os.path.join(maps_dir, "x.png"), 1, 1, [0], PALETTE)
        with open(os.path.join(maps_dir, "x.map.txt"), "w") as f:
            f.write("base 10\nentry 0 0 0 down\n")
        _expect_fail(maps_dir, legend_path, os.path.join(d, "out2"), contains="sprite region")

        # (3) warp to a map name that doesn't exist in the set.
        maps_dir = os.path.join(d, "bad_warp")
        os.makedirs(maps_dir)
        write_indexed_png(os.path.join(maps_dir, "x.png"), 1, 1, [0], PALETTE)
        with open(os.path.join(maps_dir, "x.map.txt"), "w") as f:
            f.write("base 80\nentry 0 0 0 down\nwarp 0 0 nowhere 0\n")
        _expect_fail(maps_dir, legend_path, os.path.join(d, "out3"), contains="unknown map")

        # (4) tileset name that can't be resolved (no --assets, not another map).
        maps_dir = os.path.join(d, "bad_tileset")
        os.makedirs(maps_dir)
        write_indexed_png(os.path.join(maps_dir, "x.png"), 1, 1, [0], PALETTE)
        with open(os.path.join(maps_dir, "x.map.txt"), "w") as f:
            f.write("tileset ghost_sheet\nbase 80\nentry 0 0 0 down\n")
        _expect_fail(maps_dir, legend_path, os.path.join(d, "out4"), contains="no known pak id")


def main():
    test_happy_path_with_assets()
    test_without_assets_ids_start_at_zero()
    test_validation_errors()
    print("test_map_set: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
