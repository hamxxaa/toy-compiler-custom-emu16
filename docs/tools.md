# Tools reference

Everything in `tools/` is Python, stdlib-only (`tools/png.py` is a hand-written indexed-PNG
reader/writer — no Pillow dependency anywhere in the pipeline). These build the `.pak` from source
art; see [file-formats.md](file-formats.md) for what they produce and
[README → Making a game](../README.md#making-a-game) for the everyday workflow.

## `image_import.py`

Imports LibreSprite/Aseprite indexed PNG + JSON exports into the asset pipeline.

```
python tools/image_import.py <project_list> <out_dir>
```

`<project_list>` (a "`sprites.list`") is whitespace-separated lines, `<name> <src> <json|-> <fps_default|->`,
`#` for comments:
- A row whose `json` field is `-` is the **master palette** source: `<src>` is either a `.gpl`
  (GIMP/LibreSprite palette — the editable source of truth) or an indexed PNG (its embedded PLTE is
  used). Its colors become a 512-byte RGB565 palette asset. **Line order = palette index order —
  append new colors, never reorder or insert**, or every sprite's baked-in color indices shift.
- Every other row is a sprite sheet PNG; each animation tag in its JSON (or the whole sheet, if
  untagged) becomes one concatenated sheet asset (all its frames back to back).

Emits into `<out_dir>`: one `.bin` blob per asset, `assets.manifest` (feeds `pack_assets.py`; asset
order = EPAK id order), and `sprites.gen.txt` (the `const` ids + clip-registry tables a game
`include`s — see [file-formats.md](file-formats.md#build-time-only-files-not-shipped-not-read-at-runtime)).

## `pack_assets.py`

Packs a manifest's worth of asset blobs into one `.pak` file.

```
python tools/pack_assets.py <manifest> <out_basepath>
```

Manifest lines: `<name> <type> <width> <height> <source>` (`#` comments), `type` one of
`raw|sprite|tilemap|text|palette`. Writes `<out_basepath>.pak` (ships next to the `.rom`) and
`<out_basepath>_assets.txt` (`var int ASSET_<NAME> = <id>;` per asset, for source that references
assets by name rather than by hand-tracked id). Full byte layout:
[file-formats.md](file-formats.md#2-pak--the-asset-pack-epak).

## `pixel_map.py`

Author **one** tilemap by painting it: an indexed PNG where each pixel is one 16×16 tile.

```
python tools/pixel_map.py <map.png> <legend.txt> <out.gen.txt> [--name PREFIX]
```

The PNG's width/height are the world's tile dimensions. Each pixel's palette index is looked up in
the legend (`<index> tile <tile_id> <solid 0|1>` per line) to get a PPU pattern slot id and a
solidity bit. Decorations are just tiles too — the tool has no separate object layer; place actual
game objects (player, enemies, NPCs) with ordinary code, not painted pixels. Output is a
brace-wrapped compilation unit: `const MAP_W`/`MAP_H`, `var byte world[MAP_W*MAP_H]` (tile id per
cell, row-major — doubles as the CPU collision map), `var byte tile_flags[N]` (property byte per
tile id, from the legend). This is the **bake-into-the-ROM** path — for a game with multiple maps
loaded from the `.pak` at runtime, use `map_set.py` instead.

## `map_set.py`

Builds a **set** of pak-loadable maps from a folder of painted PNGs + text descriptors — the
multi-map counterpart to `pixel_map.py` (reuses its legend/world/flags parsing internally).

```
python tools/map_set.py <maps_dir> <legend.txt> <out_dir> \
    [--name GAME] [--sprite-slots 80] [--assets <preceding_manifest>]
```

Every `*.png` in `<maps_dir>` must have a matching `<name>.map.txt` descriptor (same basename) — the
tool globs the whole directory, so an orphaned PNG without a descriptor is a hard error, not a silent
skip (this bit during development: dropping an unrelated PNG into a maps folder "just to tidy up"
broke the build). All maps share **one legend**, validated so every map's PNG only uses palette
indices the legend defines — this is what catches "tile 17 = water in forest, lava in cave" before it
becomes a silent, gameplay-breaking bug.

**Descriptor (`<name>.map.txt`) directives:**
```
tileset  forest_tiles      # pak asset NAME the tileset DMAs from (omit -> keep current)
palette  master            # pak asset NAME the palette DMAs from (omit -> keep current)
base     80                # PPU pattern slot the tileset loads at; MUST be >= --sprite-slots
entry    0  6 40  down     # entry id, tile x, tile y, facing (side|up|down) -- id 0 = new-game default
entry    1  30 2  down     # a second entry point, e.g. arrival through a door
spawn    slime  12 8       # type-name, tile x, tile y, [arg]  (no 'player' type -- entries place it)
warp     30 24  cave  0    # trigger tile x,y -> map name 'cave', arrive at ITS entry 0, [arg]
```

**`--assets <manifest>`** — a map's `tileset`/`palette` usually name an *ordinary* pak asset (a
sprite sheet/palette from `image_import.py`'s output), not another map, and this tool only sees the
maps folder — it can't know that asset's real pak id (its line position in the game's *full*
manifest) on its own. Pass `--assets` pointing at the manifest for everything that will be packed
**before** the maps (it only needs to list names in the right order — their source files don't need
to exist yet). The tool then resolves every `tileset NAME`/`palette NAME` against that manifest and
numbers its own maps starting right after it — `maps.manifest` (its own output) is meant to be
concatenated directly onto the end of that preceding manifest before packing. Without `--assets`,
maps are assumed to be the only/first things in the final pack (ids start at 0). An unresolvable
name is a **hard error** (not a silent fallback to "keep current") — a map that explicitly asked for
a tileset failing quietly would be far worse than failing loudly at build time.

**Outputs** (in `<out_dir>`): `<name>.map` (one headered binary blob per map — full byte layout:
[file-formats.md](file-formats.md#3-map-blobs-a-tilemap-asset)), `maps.manifest` (a
`pack_assets.py` manifest fragment, one `tilemap` line per map), `maps.gen.txt` (`const
SPAWN_<NAME> = <id>;` for every spawn type name seen, first-use order).

Caps (validated, not just documented): 96×96 max tile dimensions, 48 spawns / 16 warps / 16 entries
per map.

## `sprite.py`

Converts hand-drawn ASCII art into a packed monochrome byte array.

```
python tools/sprite.py man.txt [name]
```

`#`/`X`/`1`/`*` = a lit pixel, anything else = clear; bits packed MSB-first, rows padded to whole
bytes. **This tool targets the legacy VRAM drawing path** (`draw_sprite`, which no longer exists in
`io.lib` since the PPU reboot) — it still runs and still produces a correct byte array, but nothing
in the current library set consumes that array's format directly. `examples/block_blast.txt` bakes
its PPU-era tile/sprite art as flat 256-byte-per-pattern arrays instead (hand-computed, not run
through this tool — see its own header comment). Useful today only if you're writing a new PPU-side
consumer for hand-drawn monochrome bitmaps yourself.

## `mml.py`

Compiles a song written in **MML** (Music Macro Language — music as text) into a packed `.song` blob.

```
python tools/mml.py <in.mml> <out.song>
```

The blob is loaded at runtime by `music_load_song()` (pack it as a `raw` asset with `pack_assets.py`).
It is **self-contained** — it carries its own instrument definitions — so different songs can use
completely different sounds; that's what makes runtime track-swapping (overworld theme vs fight theme)
one call with a different pak id.

MML in brief: directives `speed N` / `groove A B C` (the tempo / swing table); `inst NAME` blocks with
`vol`/`duty`/`arp` macros (`|` marks the loop point), `vib DEPTH SPD DELAY`, `noise M`; `drum LETTER
INST NOTE` one-letter drum shortcuts; and per-channel tracks `chN NAME <notes>`. Notes are `a`–`g`
(`+`/`#` sharp, `-` flat), `r` rest, octave `o4`/`>`/`<`, lengths `c4`/`c8.`/`c12` (triplet), default
length `l`, `@inst` to switch, `[ … ]N` to repeat. The **row-grid resolution is chosen automatically**
(the LCM of the note lengths used) so triplets and straight notes coexist with the fewest rows; one
pattern, ≤255 rows. Output byte layout: [file-formats.md](file-formats.md#4-song-blobs-song). Sources
live in `assets/music/*.mml` — start with `showcase.mml`, a heavily-commented reference file that
demonstrates every feature above; `examples/block_blast.txt` is the real in-game consumer
(pak-loaded song + SFX over it).

**Fast preview (no ROM):** `python tools/mml.py song.mml --preview` compiles the song and **plays it**
straight away — no ROM, no pak, no EMU16 compile. It renders through `song_render` (a tiny native tool
that links the real `emulator/apu.cpp`, so the preview is byte-identical to the game's audio; build it
with `make song_render`). Also: `--wav out.wav` writes the audio instead of playing, `--seconds N` /
`--loops N` set the length. This is the write→hear loop for composing.

## `png.py`

Not run directly. A minimal stdlib-only indexed-PNG (PLTE-mode) reader/writer used internally by
`image_import.py`, `pixel_map.py`, and `map_set.py`.
