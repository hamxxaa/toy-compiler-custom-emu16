#!/usr/bin/env python3
"""Build a SET of pak-loadable maps from a folder of painted PNGs + descriptors.

    python tools/map_set.py <maps_dir> <legend.txt> <out_dir> \\
        [--name GAME] [--sprite-slots 80] [--assets <preceding_manifest>]

Where tools/pixel_map.py bakes ONE map into ROM source, this tool packs MANY maps into headered
binary blobs meant for the `.pak` (loaded at runtime via `map_load` in lib/ppuscene.lib -- see
plans/swapping-worldmaps-kojima.md). All maps in the folder share ONE legend, so they share one
tileset vocabulary and one resident tile_flags shape; the tool VALIDATES every map's PNG only uses
palette indices the shared legend defines (the bug this catches: "tile 17 = water in forest, lava
in cave", which is silent and gameplay-breaking without this check).

Per map, two files with the same basename in <maps_dir>:
    <name>.png       painted tilemap (1 pixel = 1 tile), same format tools/pixel_map.py reads.
    <name>.map.txt   descriptor -- everything that isn't tiles (tileset/palette/entries/spawns/warps):

        tileset  forest_tiles      # pak asset NAME the tileset DMAs from (omit -> keep current)
        palette  master            # pak asset NAME the palette DMAs from (omit -> keep current)
        base     80                # PPU pattern slot the tileset loads at; MUST be >= --sprite-slots
        entry    0  6 40  down     # entry id, tile x, tile y, facing (side|up|down) -- id 0 = new-game default
        entry    1  30 2  down     # a second entry point, e.g. arrival through a door
        spawn    slime  12 8       # type-name, tile x, tile y, [arg]  (no 'player' type -- entries place it)
        warp     30 24  cave  0    # trigger tile x,y -> map name 'cave', arrive at ITS entry 0, [arg]

Output (in <out_dir>):
    <name>.map           one headered binary blob per map (see BLOB FORMAT below)
    maps.manifest        a pack_assets.py manifest fragment: one `tilemap` line per map
    maps.gen.txt         `const SPAWN_<NAME> = <id>;` for every spawn type name seen (first-use order)

PAK ID RESOLUTION (--assets): a map's `tileset`/`palette` usually name an ORDINARY pak asset (a
sprite sheet / palette from image_import.py's pipeline), not another map -- and this tool only sees
the maps folder, so it can't know that asset's real pak id (= its line position in the game's FULL
manifest) on its own. Pass `--assets <manifest>` pointing at the manifest for everything that will
be packed BEFORE the maps (it only needs to LIST assets in the right order -- their source files
don't need to exist yet); this tool then:
  - resolves every `tileset NAME` / `palette NAME` against that manifest's names, in order
  - numbers its own maps starting right after it (so maps.manifest is meant to be concatenated
    directly onto the end of that file, unchanged, before packing)
Without --assets, maps are assumed to be the ONLY / FIRST things in the final manifest (ids start
at 0) -- fine for a game whose only assets are maps, or for the phase-4/6 test fixtures.
A `tileset`/`palette` name that resolves to nothing (not in --assets, not another map in this set)
is a hard error -- silently falling back to "keep current" would be a silent, hard-to-debug failure
for a map that clearly asked for a specific tileset.

BLOB FORMAT (little-endian; matches plans/swapping-worldmaps-kojima.md §6 exactly):
    Header (16 bytes):
        0  2   magic "MP"
        2  1   version (=1)
        3  1   W  (tiles, 1..96)
        4  1   H  (tiles, 1..96)
        5  1   tile_base    PPU slot the tileset DMAs to (>= sprite-slots)
        6  1   tileset_id   pak asset id of the pattern sheet (255 = keep current)
        7  1   palette_id   pak asset id of the palette       (255 = keep current)
        8  1   flags_count
        9  1   spawn_count  (<= 48)
       10  1   warp_count   (<= 16)
       11  1   entry_count  (<= 16)
       12  4   reserved (0)
    Body (sections in this order):
        [flags_count] * 1   tile_flags: property per absolute PPU slot id (b0 = solid)
        [entry_count] * 3   entries: { tx, ty, facing }            (facing: 0=side 1=up 2=down)
        [spawn_count] * 4   spawns:  { type, tx, ty, arg }
        [warp_count]  * 5   warps:   { tx, ty, target_map_id, target_entry, arg }
        [W*H]         * 1   world tile ids (absolute PPU slot ids), row-major = the collision grid

STDLIB only (+ tools/png.py, reuses tools/pixel_map.py's legend/world/flags helpers).
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import png              # noqa: E402
import pixel_map        # noqa: E402

MAX_DIM = 96
MAX_SPAWNS = 48
MAX_WARPS = 16
MAX_ENTRIES = 16

FACING = {"side": 0, "up": 1, "down": 2}


class MapDesc:
    def __init__(self, name, desc_path):
        self.name = name             # basename, lowercased -- the warp/manifest identity
        self.desc_path = desc_path
        self.tileset = None          # asset NAME, or None (255 = keep current)
        self.palette = None
        self.base = None             # tile_base (required)
        self.entries = {}            # id -> (tx, ty, facing)
        self.spawns = []             # [(type_name, tx, ty, arg)]
        self.warps = []              # [(tx, ty, target_name, target_entry, arg)]
        # filled in during pass 1, once the PNG is read:
        self.world = None
        self.flags = None
        self.w = None
        self.h = None


def _split(line, lineno, path, minlen, maxlen=None):
    parts = line.split()
    maxlen = maxlen if maxlen is not None else minlen
    if not (minlen <= len(parts) <= maxlen):
        raise SystemExit(f"{path}:{lineno}: '{parts[0] if parts else ''}' expects "
                          f"{minlen}..{maxlen} field(s), got {len(parts) - 1}")
    return parts


def parse_map_desc(path, name):
    d = MapDesc(name, path)
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            kw = line.split()[0]
            if kw == "tileset":
                _, d.tileset = _split(line, lineno, path, 2)
            elif kw == "palette":
                _, d.palette = _split(line, lineno, path, 2)
            elif kw == "base":
                _, base = _split(line, lineno, path, 2)
                d.base = int(base, 0)
            elif kw == "entry":
                _, eid, tx, ty, facing = _split(line, lineno, path, 5)
                eid = int(eid, 0)
                if facing not in FACING:
                    raise SystemExit(f"{path}:{lineno}: unknown facing '{facing}' (want side|up|down)")
                if eid in d.entries:
                    raise SystemExit(f"{path}:{lineno}: duplicate entry id {eid}")
                d.entries[eid] = (int(tx, 0), int(ty, 0), FACING[facing])
            elif kw == "spawn":
                parts = _split(line, lineno, path, 4, 5)
                _, typ, tx, ty = parts[:4]
                arg = int(parts[4], 0) if len(parts) == 5 else 0
                d.spawns.append((typ, int(tx, 0), int(ty, 0), arg))
            elif kw == "warp":
                parts = _split(line, lineno, path, 5, 6)
                _, tx, ty, target, target_entry = parts[:5]
                arg = int(parts[5], 0) if len(parts) == 6 else 0
                d.warps.append((int(tx, 0), int(ty, 0), target.lower(), int(target_entry, 0), arg))
            else:
                raise SystemExit(f"{path}:{lineno}: unknown keyword '{kw}'")

    if d.base is None:
        raise SystemExit(f"{path}: missing required 'base <slot>' line")
    if not d.entries:
        raise SystemExit(f"{path}: needs at least one 'entry' (entry 0 is the new-game default)")
    ids = sorted(d.entries)
    if ids != list(range(len(ids))):
        raise SystemExit(f"{path}: entry ids must be a dense 0..N-1 sequence with no gaps/dupes, "
                          f"got {ids}")
    if len(d.entries) > MAX_ENTRIES:
        raise SystemExit(f"{path}: {len(d.entries)} entries exceeds the cap of {MAX_ENTRIES}")
    if len(d.spawns) > MAX_SPAWNS:
        raise SystemExit(f"{path}: {len(d.spawns)} spawns exceeds the cap of {MAX_SPAWNS}")
    if len(d.warps) > MAX_WARPS:
        raise SystemExit(f"{path}: {len(d.warps)} warps exceeds the cap of {MAX_WARPS}")
    return d


def scan_manifest_names(path):
    """Ordered asset names from a pack_assets.py manifest -- WITHOUT requiring its source files to
    exist yet (unlike pack_assets.parse_manifest, which reads them). Only needed to reconstruct the
    name -> pak-id mapping (id = line position), so existence of the referenced bytes is irrelevant
    here."""
    names = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            names.append(line.split()[0])
    return names


def pack_header(w, h, tile_base, tileset_id, palette_id, flags_count, spawn_count, warp_count, entry_count):
    return struct.pack("<2sBBBBBBBBBB4x", b"MP", 1, w, h, tile_base, tileset_id, palette_id,
                        flags_count, spawn_count, warp_count, entry_count)


def build_blob(d, asset_id):
    tileset_id = asset_id[d.tileset] if d.tileset is not None else 255
    palette_id = asset_id[d.palette] if d.palette is not None else 255

    entries_bytes = b"".join(struct.pack("<BBB", tx, ty, facing)
                              for (tx, ty, facing) in (d.entries[i] for i in sorted(d.entries)))
    spawns_bytes = b"".join(struct.pack("<BBBB", d.spawn_type_ids[typ], tx, ty, arg)
                             for (typ, tx, ty, arg) in d.spawns)
    warps_bytes = b"".join(struct.pack("<BBBBB", tx, ty, asset_id[f"map_{target}"], target_entry, arg)
                            for (tx, ty, target, target_entry, arg) in d.warps)

    header = pack_header(d.w, d.h, d.base, tileset_id, palette_id,
                          len(d.flags), len(d.spawns), len(d.warps), len(d.entries))
    return header + bytes(d.flags) + entries_bytes + spawns_bytes + warps_bytes + bytes(d.world)


def main():
    # Index-based (not a filter over sys.argv): a flag's VALUE token (e.g. the path after
    # --assets) doesn't start with "--", so filtering by prefix alone would leak it into the
    # positional-args list. Consume each recognized flag together with its value.
    argv = sys.argv[1:]
    positional = []
    name = ""
    sprite_slots = 80
    assets_path = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--name" and i + 1 < len(argv):
            name = argv[i + 1]; i += 2
        elif a == "--sprite-slots" and i + 1 < len(argv):
            sprite_slots = int(argv[i + 1], 0); i += 2
        elif a == "--assets" and i + 1 < len(argv):
            assets_path = argv[i + 1]; i += 2
        else:
            positional.append(a); i += 1
    if len(positional) != 3:
        print(__doc__)
        return 1
    maps_dir, legend_path, out_dir = positional

    tiles = pixel_map.parse_legend(legend_path)

    # ---- seed the asset-id namespace from whatever precedes the maps in the real manifest ----
    asset_id = {}
    if assets_path:
        for i, nm in enumerate(scan_manifest_names(assets_path)):
            asset_id[nm] = i
    map_id_offset = len(asset_id)

    png_files = sorted(f for f in os.listdir(maps_dir) if f.lower().endswith(".png"))
    if not png_files:
        raise SystemExit(f"{maps_dir}: no .png files found")

    # ---- pass 1: parse every descriptor + flatten every world (need dims for validation) ----
    maps = []
    for fn in png_files:
        base = fn[:-4]
        map_name = base.lower()
        png_path = os.path.join(maps_dir, fn)
        desc_path = os.path.join(maps_dir, base + ".map.txt")
        if not os.path.exists(desc_path):
            raise SystemExit(f"{maps_dir}: {fn} has no matching descriptor '{base}.map.txt'")

        d = parse_map_desc(desc_path, map_name)
        if d.base < sprite_slots:
            raise SystemExit(f"{desc_path}: base={d.base} would overwrite the sprite region "
                              f"(slots 0..{sprite_slots - 1}); must be >= {sprite_slots}")

        img = png.read(png_path)
        if not (1 <= img.width <= MAX_DIM) or not (1 <= img.height <= MAX_DIM):
            raise SystemExit(f"{png_path}: {img.width}x{img.height} exceeds the {MAX_DIM}x{MAX_DIM} "
                              f"map dimension ceiling")
        d.w, d.h = img.width, img.height
        d.world, _max_tile_used = pixel_map.build_world(png_path, img, tiles)
        d.flags = pixel_map.build_flags(tiles)   # sized to the LEGEND's max, not just this map's

        for (tx, ty, _facing) in d.entries.values():
            if not (0 <= tx < d.w and 0 <= ty < d.h):
                raise SystemExit(f"{desc_path}: entry at ({tx},{ty}) is outside the {d.w}x{d.h} map")
        for (_typ, tx, ty, _arg) in d.spawns:
            if not (0 <= tx < d.w and 0 <= ty < d.h):
                raise SystemExit(f"{desc_path}: spawn at ({tx},{ty}) is outside the {d.w}x{d.h} map")
        for (tx, ty, _target, _te, _arg) in d.warps:
            if not (0 <= tx < d.w and 0 <= ty < d.h):
                raise SystemExit(f"{desc_path}: warp trigger at ({tx},{ty}) is outside the {d.w}x{d.h} map")

        maps.append(d)

    # ---- assign this set's own map ids, continuing right after the seeded --assets namespace ----
    for i, d in enumerate(maps):
        asset_id[f"map_{d.name}"] = map_id_offset + i
    by_name = {d.name: d for d in maps}

    # ---- resolve every tileset/palette reference NOW, so a bad name fails before writing anything ----
    for d in maps:
        for kind, ref in (("tileset", d.tileset), ("palette", d.palette)):
            if ref is not None and ref not in asset_id:
                raise SystemExit(
                    f"{d.desc_path}: {kind} '{ref}' has no known pak id -- list it (in order) in "
                    f"the manifest passed via --assets, or remove the '{kind}' line to keep whatever "
                    f"is currently loaded (255)"
                )

    # spawn type name -> id, first-seen order across maps in this same (sorted-filename) order
    spawn_type_ids = {}
    for d in maps:
        for (typ, _tx, _ty, _arg) in d.spawns:
            if typ not in spawn_type_ids:
                spawn_type_ids[typ] = len(spawn_type_ids)
    for d in maps:
        d.spawn_type_ids = spawn_type_ids

    # ---- validate warp targets now that every map's name + entry list is known ----
    for d in maps:
        for (tx, ty, target, target_entry, _arg) in d.warps:
            if target not in by_name:
                raise SystemExit(f"{d.desc_path}: warp at ({tx},{ty}) targets unknown map '{target}'")
            target_map = by_name[target]
            if not (0 <= target_entry < len(target_map.entries)):
                raise SystemExit(f"{d.desc_path}: warp at ({tx},{ty}) targets entry {target_entry} "
                                  f"of '{target}', which only has {len(target_map.entries)} entries")

    os.makedirs(out_dir, exist_ok=True)

    manifest_lines = []
    for d in maps:
        blob = build_blob(d, asset_id)
        out_path = os.path.join(out_dir, f"{d.name}.map")
        with open(out_path, "wb") as f:
            f.write(blob)
        manifest_lines.append(f"map_{d.name} tilemap {d.w} {d.h} {d.name}.map")
        print(f"map_set: {d.name}: {d.w}x{d.h} tiles, {len(d.entries)} entries, "
              f"{len(d.spawns)} spawns, {len(d.warps)} warps -> {out_path} ({len(blob)} bytes)")

    manifest_path = os.path.join(out_dir, "maps.manifest")
    with open(manifest_path, "w", encoding="utf-8") as f:
        if assets_path:
            f.write(f"# Generated by tools/map_set.py{' for ' + name if name else ''}. Concatenate\n"
                    f"# this AFTER {os.path.basename(assets_path)} (unchanged, same relative order)\n"
                    f"# before packing -- every warp's target_map_id and every tileset/palette id in\n"
                    f"# these blobs was resolved assuming that exact ordering.\n")
        else:
            f.write(f"# Generated by tools/map_set.py{' for ' + name if name else ''}. Pack these\n"
                    f"# lines FIRST (unchanged) in the game's combined manifest -- pak ids are\n"
                    f"# manifest order, and every warp's target_map_id assumed this 0..N-1 order.\n")
        f.write("\n".join(manifest_lines) + "\n")

    gen_path = os.path.join(out_dir, "maps.gen.txt")
    with open(gen_path, "w", encoding="utf-8") as f:
        f.write("// Generated by tools/map_set.py -- spawn type ids (map asset ids come from\n"
                 "// packing maps.manifest; see its own header comment for the ordering rule).\n")
        f.write("{\n")
        for typ, tid in spawn_type_ids.items():
            f.write(f"    const SPAWN_{typ.upper()} = {tid};\n")
        f.write("}\n")

    print(f"map_set: {len(maps)} map(s) -> {manifest_path}, {gen_path}")
    print("map_set: map pak ids (see maps.manifest's header comment for the ordering rule): "
          + ", ".join(f"{d.name}={asset_id['map_' + d.name]}" for d in maps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
