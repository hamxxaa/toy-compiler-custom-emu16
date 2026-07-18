#!/usr/bin/env python3
"""Author a tilemap by PAINTING it: an indexed PNG where each PIXEL is one 16x16 tile.

Usage:
    python tools/pixel_map.py <map.png> <legend.txt> <out.gen.txt> [--name PREFIX]

The map PNG's width/height are the world's tile dimensions (1 pixel == 1 tile). Each pixel's palette
index is looked up in the legend to get a tile id (a PPU pattern slot) and whether it's solid.
Decorations (trees, rocks, ...) are just tiles too — paint them directly into the map like any other
terrain (the retro-console way: background tiles are always opaque, so bake each decoration's
surrounding ground color into its own tile art rather than relying on transparency).

Objects (player, enemies, NPCs, ...) are NOT part of this tool — place them with ordinary code
(hardcoded or computed coordinates), the same way you would without a map tool. This tool only
produces the tilemap + its collision flags.

Output is a brace-wrapped compilation unit the game `include`s:
    const MAP_W / MAP_H
    var byte world[MAP_W*MAP_H]      -- tile id per cell (row-major); doubles as the CPU collision map
    var byte tile_flags[N]           -- property byte per tile id (OR of the legend's flag bits)

Legend format (whitespace-separated, '#' comment):
    flags <name>=<bit> ...           -- declare flag bits ONCE; names/meanings are yours, not the tool's
    <index> <tile_id> [flag-name ...]  -- a palette index -> pattern slot, plus any of those flags
Example:
    flags solid=0 hazard=1           # the CODE decides bit 0 = solid (map.lib), bit 1 = hazard, ...
    0 16               # grass  (walkable, no flags)
    1 17 solid         # water  (solid)
    2 18               # flowers
    3 20 solid         # tree   (solid; its own tile art, painted directly into the map)
    4 21 solid hazard  # lava   (solid + hazard)

STDLIB only (+ tools/png.py).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import png  # noqa: E402


def parse_legend(path):
    """-> {index: (tile_id, flags_byte)}.

    Format (whitespace-separated, '#' = comment):
        flags <name>=<bit> <name>=<bit> ...    # declare flag bits (any names; do this before use)
        <index> <tile_id> [flag-name ...]      # PNG palette index -> pattern slot + the named flags
    This tool is MEANING-AGNOSTIC: it just ORs the named bits into a property byte. What each bit
    MEANS is defined by the code/libraries that read the flags table (e.g. lib/map.lib's map_solid_at
    tests bit 0 for solid), so add a tile property with a code constant + a legend bit, tool untouched."""
    flag_bits = {}
    tiles = {}
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if parts[0] == "flags":                      # a flag-bit declaration line
                for tok in parts[1:]:
                    if "=" not in tok:
                        raise SystemExit(f"{path}:{lineno}: flag decl must be name=bit, got '{tok}'")
                    nm, bit = tok.split("=", 1)
                    b = int(bit, 0)
                    if not 0 <= b <= 7:
                        raise SystemExit(f"{path}:{lineno}: flag bit {b} out of range 0..7 (one byte)")
                    flag_bits[nm] = b
                continue
            if len(parts) < 2:
                raise SystemExit(f"{path}:{lineno}: expected '<index> <tile_id> [flag-name ...]'")
            idx, tid = int(parts[0], 0), int(parts[1], 0)
            fl = 0
            for nm in parts[2:]:
                if nm not in flag_bits:
                    raise SystemExit(f"{path}:{lineno}: unknown flag '{nm}' -- declare it first "
                                     f"with a 'flags {nm}=<bit>' line")
                fl |= 1 << flag_bits[nm]
            tiles[idx] = (tid, fl)
    return tiles


def arr_lit(vals):
    return "{ " + ", ".join(str(v) for v in vals) + " }"


def build_world(png_path, img, tiles):
    """Flatten an indexed PNG into a row-major tile-id array via the legend, validating every
    pixel's palette index is present. Shared with tools/map_set.py (a map-set is a folder of these
    same PNG+legend maps, packed into pak blobs instead of emitted as source). Returns
    (world: bytearray, max_tile: int)."""
    W, H = img.width, img.height
    world = bytearray(W * H)
    max_tile = 0
    for ty in range(H):
        for tx in range(W):
            idx = img.index_at(tx, ty)
            if idx not in tiles:
                raise SystemExit(f"{png_path}: palette index {idx} at ({tx},{ty}) is not in the legend")
            tid, _fl = tiles[idx]
            world[ty * W + tx] = tid
            max_tile = max(max_tile, tid)
    return world, max_tile


def build_flags(tiles):
    """-> tile_flags[]: the property byte per tile id (the OR of its legend-declared flag bits), sized
    to the LEGEND's own highest tile id -- NOT whatever a given image happens to use (an image that
    skips the legend's highest-numbered tile would otherwise size this array too small and overflow
    the `flags[tid] |= fl` write below). flags is a lookup table for any tile id the shared legend
    defines, which matters once one legend is shared across a whole map SET (tools/map_set.py) where
    different maps use different subsets of it."""
    max_tile = max((tid for (tid, _fl) in tiles.values()), default=-1)
    flags = [0] * (max_tile + 1)
    for (tid, fl) in tiles.values():
        flags[tid] |= fl
    return flags


def main():
    # Index-based (not a filter over sys.argv): --name's VALUE token doesn't start with "--", so
    # filtering by prefix alone would leak it into the positional-args list.
    argv = sys.argv[1:]
    positional = []
    name = ""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--name" and i + 1 < len(argv):
            name = argv[i + 1] + "_"; i += 2
        else:
            positional.append(a); i += 1
    if len(positional) != 3:
        print(__doc__)
        return 1
    png_path, legend_path, out_path = positional

    img = png.read(png_path)
    tiles = parse_legend(legend_path)
    W, H = img.width, img.height

    world, max_tile = build_world(png_path, img, tiles)
    flags = build_flags(tiles)

    lines = [
        "// Generated by tools/pixel_map.py -- paint a tilemap; include this in your game.",
        "{",
        f"    const {name}MAP_W = {W};",
        f"    const {name}MAP_H = {H};",
        f"    var byte {name}world[{W * H}] = {arr_lit(list(world))};",
        f"    var byte {name}tile_flags[{len(flags)}] = {arr_lit(flags)};",
        "}",
    ]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"pixel_map: {W}x{H} tiles -> {out_path}  (tile ids 0..{max_tile})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
