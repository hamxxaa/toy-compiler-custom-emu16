# File formats

EMU16 has four on-disk formats: the program (`.rom`), the asset pack (`.pak`), save files, and — as
an asset *inside* a `.pak` — the map blob. This doc explains what each contains, how it's built, and
how the host reads it at runtime.

## The key split: address space vs. storage

Two different "spaces," and the formats live in different ones:

- **The 64 KB CPU address space** — what the CPU sees (RAM at runtime): `bootstrap · DATA · CODE ·
  stack` (see [memory-map.md](memory-map.md)). It is *volatile* and re-created every boot.
- **Storage** — ESP32 flash (LittleFS, → SD later) · PC disk · browser `localStorage`. Files live
  here, *outside* the address space, and are moved in/out by **loading** (a `.rom` becomes RAM) or by
  **syscalls** (`.pak`/save bytes ↔ guest buffers — see [syscalls.md](syscalls.md)).

---

## 1. `.rom` — the program

A **flat image of low memory, with no header**. The compiler
([`src/backend/EmuBackend.py`](../src/backend/EmuBackend.py)) emits one byte array that maps 1:1
onto addresses `0x0000 … code_end`:

```
0x0000–0x0007   Bootstrap   LDI SP ; JMP CODE_START (0x8000)
0x0008–0x7FFF   DATA        globals, arrays, the 8×8 font, baked array-literals + string literals
0x8000–…        CODE        entry block (run global initializers → call main) + every function
```

- **Built:** `python main.py game.txt --save-rom game` → `build/roms/game.rom`.
- **Loaded:** the host copies the whole file **verbatim** into `memory[0]`. No parsing — the file
  *is* the RAM image of `0x0000…code_end`.
- **Booted:** the CPU starts at `pc = 0`; the bootstrap sets the stack pointer and jumps to
  `CODE_START`, whose entry block initializes globals and calls `main`.
- **Limits:** must fit below the stack (`size < 0xFFFC`). INPUT is **not** in the ROM — it sits above
  the image and the host initializes it (zero input) before the ROM's first frame runs.

A `.rom` is self-contained code + data. It carries **no** art and **no** save state. Full layout
rationale (why DATA is sized the way it is, what's actually inside it): see
[memory-map.md](memory-map.md#whats-actually-in-data).

---

## 2. `.pak` — the asset pack (EPAK)

Bundled art/data that lives in storage and is **streamed in piece-by-piece** — never loaded whole
into the 64 KB. One pack per ROM, namespaced by ROM basename (`game.pak` next to `game.rom`). Built
by [`tools/pack_assets.py`](../tools/pack_assets.py) (see [tools.md](tools.md)).

```
Offset  Size    Field
 0       4      magic "EPAK"
 4       1      version (=1)
 5       1      reserved
 6       2      asset_count            (little-endian)
 8       12·N   TOC — one 12-byte entry per asset (asset id = TOC index):
                  +0  1  type     0=raw 1=sprite 2=tilemap 3=text 4=palette
                  +1  1  width
                  +2  1  height
                  +3  1  reserved
                  +4  4  offset   (little-endian, from file start)
                  +8  4  length   (little-endian)
 …               concatenated blob bytes
```

- Sprite blobs are **byte-per-pixel palette indices** (`length = w·h·frame_count`; index 0 =
  transparent). Palette blobs are 512 bytes of RGB565. Text blobs are NUL-terminated. **Tilemap**
  blobs (`type=2`) are, since the map system, the [headered map blob](#3-map-blobs-a-tilemap-asset)
  below, not raw tile bytes.
- **Read at runtime:** on ROM load the host parses the small TOC into RAM (firmware caches it + keeps
  the file handle; PC reads the whole pak into memory; the browser holds it in a heap buffer). Then:
  - `sys_asset_info(id, dest)` → writes `{type, w, h, _, len_lo, len_hi}` and returns the length, so
    the game can size a buffer first.
  - `sys_asset_load(id, dest, max)` → seeks to `offset` and copies `length` bytes into guest memory
    (a RAM scratch buffer, or — for a full map blob — `map.lib`'s `map_buf`).
  - `sys_ppu_dma(id, ppu_addr)` → same seek, but streams straight into PPU RAM instead of CPU memory
    (tilesets, sprite sheets, palettes — anything the CPU doesn't need to inspect byte-by-byte).
- This is why a multi-megabyte pack works on a 64 KB machine: the pack stays in storage; only the
  current room/sprite is resident. **Call asset loads on room/scene transitions, not per frame**
  (they hit flash/SD).

---

## 3. Map blobs (a tilemap asset)

Since the map system (`plans/swapping-worldmaps-kojima.md`), a `.pak` tilemap asset (`type=2`) is not
raw tile bytes but a **headered blob**: everything one map needs to become the resident world —
dimensions, its own tileset/palette references, spawns, warps, entry points, and the tile grid
itself — in one `sys_asset_load`. Built by [`tools/map_set.py`](../tools/map_set.py) (see
[tools.md](tools.md#map_setpy)); parsed at runtime by `map_load()` in `lib/map.lib` (see
[libraries.md](libraries.md#map-loading)).

```
Header (16 bytes, little-endian):
 0   2   magic "MP"
 2   1   version (=1)
 3   1   W              tiles wide (1..96)
 4   1   H              tiles tall (1..96)
 5   1   tile_base      PPU pattern slot the tileset DMAs to (>= PPU_SPRITE_SLOTS)
 6   1   tileset_id     pak asset id of the pattern sheet (255 = keep current)
 7   1   palette_id     pak asset id of the palette       (255 = keep current)
 8   1   flags_count
 9   1   spawn_count    (<= 48)
10   1   warp_count     (<= 16)
11   1   entry_count    (<= 16)
12   4   reserved (0)

Body (sections in this fixed order):
 [flags_count] x 1   tile_flags: one property byte per absolute PPU pattern slot id (b0 = solid)
 [entry_count] x 3   entries: { tx, ty, facing }         facing: 0=side 1=up 2=down
 [spawn_count] x 4   spawns:  { type, tx, ty, arg }       type is a game-defined id, not engine-known
 [warp_count]  x 5   warps:   { tx, ty, target_map_id, target_entry, arg }
 [W*H]         x 1   world tile ids (absolute PPU slot ids), row-major — doubles as the collision grid
```

- **World tile ids are absolute PPU pattern slots**, not indices into some per-map palette — so
  `map_solid_at`/`scene_stream` (which already take a raw world buffer + a `tile_flags` table) need
  **zero changes** to work with pak-loaded maps; they were written against exactly this shape.
- **Pattern slots are split by the named constant `PPU_SPRITE_SLOTS = 80`**: slots 0-79 are
  boot-resident sprites, slots 80-127 are the "tileset region" that DMAs in fresh on every
  `map_load()` (so `tile_base` must be `>= 80`, tool-validated). This is what lets an unbounded number
  of distinct maps share the same 48-slot tileset budget — only one map's terrain patterns are ever
  resident at a time.
- **Entries replace a hardcoded spawn point.** A warp targets `(target_map_id, target_entry)` **by
  number**, not by name — names only exist at authoring time (`tools/map_set.py` resolves them to
  ids when it builds the manifest); the compiled game never sees a map or entry name, only small
  integers.
- **255 in `tileset_id`/`palette_id` means "keep whatever's currently DMA'd."** This lets two maps
  that share one tileset (e.g. a game whose only terrain sheet never changes) skip re-DMAing it on
  every transition — `map_load()` checks for `255` before calling `sys_ppu_dma`.
- **`MAX_MAP_BLOB = 9728` bytes** in `map.lib` is the compile-time ceiling
  (96×96 world = 9,216 B, plus the header and the worst-case flags/entries/spawns/warps sections,
  rounded up) — see [memory-map.md](memory-map.md#whats-actually-in-data) for how that competes with
  everything else in DATA.

---

## 4. Song blobs (`.song`)

A packed song for the music engine (`lib/music.lib`, `music_load_song`), produced by
[`tools/mml.py`](../tools/mml.py) from an MML source and packed as a `raw` `.pak` asset. Like a map
blob, it is **self-contained** — it carries its own instrument definitions — so different songs use
different sounds and swapping a track is one `music_load_song(pak_id, …)` call.

Layout (little-endian):

```
Header (9 bytes):
  0  version (=1)
  1  channels (=4)
  2  rows          (rows in the single pattern; <=255)
  3  num_patterns  (=1 in v1)
  4  order_len
  5  order_loop    (order index to restart at when the order list ends)
  6  groove_len
  7  instr_len     (u16: byte 7 low, byte 8 high)
Sections, in order, immediately after the header:
  groove[groove_len]     per-row frame counts, cycled (the tempo / swing table)
  order[order_len]       pattern indices, 255-terminated (loops to order_loop)
  instruments[instr_len] a ready-made APU DEF_INST_* command stream (see below)
  patterns               rows * channels * 2 bytes: cells [note, inst]
                         note 0 = hold, 1 = note-off, >=2 = MIDI note
```

The clean trick: the **instrument section is a pre-built APU command stream** (`DEF_INST_VOL`/`DUTY`/
`ARP`/`VIB`/`NOISE` bytes, the same the `apu_inst_*` helpers emit), so the loader just
`sys_apu_submit`s it — no runtime instrument parsing. (The APU's inbound buffer is 2 KB to fit a whole
song's instruments in one submit.) The row grid is chosen by the tool (LCM of the note lengths) so
triplets and straight rhythms coexist with the fewest rows.

---

## 5. Save files

Per-ROM, per-slot, and **format-less from the engine's view** — the byte layout is the game's
business.

- **Where:** `/saves/<rom>.<slot>` (LittleFS / SD), `saves/<rom>.<slot>` (PC), `localStorage`
  key `emu16:<rom>.<slot>` (browser, base64). `<rom>` is the running ROM's basename, so games never
  collide.
- **Write:** the game fills a `var byte save_buf[N]`, then `sys_save(&save_buf, len, slot)`. The host
  writes the bytes verbatim; it does not interpret them.
- **Read:** `sys_load(&save_buf, max, slot)` copies them back (returns bytes read, 0 if none);
  `sys_save_exists(slot)` checks presence.
- **Convention (game-side, not enforced):** lead the blob with a magic + version byte so a newer
  build can reject/migrate a stale save; everything after is your fields. `lib/event.lib`'s
  `flags_save`/`flags_load` is the simplest possible example — it just persists the 32-byte flag
  bitset with no header at all, since flags alone are rarely enough to need versioning.

The host storage backend is isolated in [`firmware/src/storage.h`](../firmware/src/storage.h)
(`STORAGE_FS` macro) — the single seam where LittleFS swaps for SD, for both `.pak` reads and saves.
See [firmware.md](firmware.md#storage).

---

## Build-time-only files (not shipped, not read at runtime)

These are inputs/intermediates of the build, not runtime formats — full tool usage in
[tools.md](tools.md):

- **`sprites.list`** — the importer's asset list (`<name> <src> <json|-> <fps|->` per line). The row
  whose json is `-` names the **master palette** source: a `.gpl` (GIMP/LibreSprite palette — the
  editable, git-friendly source of truth) or an indexed PNG (its embedded PLTE). It becomes a
  512-byte (256-entry RGB565) palette asset. Every sprite is byte-per-pixel *indices* into this one
  shared palette, so the color at each index is fixed across every sheet — **append** colors to the
  master (line order = index = palette order), never reorder or insert, and keep index 0 as the
  transparent slot.
- **manifest** (`name type w h source` per line) — tells `pack_assets.py` what to bundle, and fixes
  asset ids (= line order = TOC index).
- **`<name>.map.txt`** — a map's descriptor sidecar for `map_set.py` (tileset/palette/entries/spawns/
  warps — see [tools.md](tools.md#map_setpy)); paired with a painted PNG of the same basename.
- **generated `const` include** (`*_assets.txt` / `sprites.gen.txt` / `maps.gen.txt`) — asset,
  animation, and spawn-type ids; it's `include`d by the game source and **compiled into the `.rom`**,
  so the ids end up as plain literals. This is the fixed ABI between a tool and the library that
  consumes its output (e.g. `sprites.gen.txt`'s clip registry tables and `lib/anim.lib`) — include
  the generated file **before** the library that reads it.
- **`.bin` intermediates + the LibreSprite PNG/JSON** — the importer's inputs/outputs that flow into
  the pack, then are discarded.

---

## Lifecycle

```
BUILD                                          DEPLOY              RUNTIME (inside the 64 KB)
game.txt + libs ─compiler→ game.rom ───────────────────────────► memory[0]  (verbatim)  ─► CPU runs
art.png + json ─importer→ bins + manifest ─┐
map.png + .map.txt ─map_set→ .map blob ─────┼─pack→ game.pak ─┐
   (+ const includes compiled INTO game.rom)┘                └──► TOC cached ─► sys_asset_load /
                                                                                 sys_ppu_dma ─► RAM/PPU
                                              (created at runtime) ◄── sys_save ──  save_buf
                                                                  ──► sys_load ──►  save_buf
```

## Where each file lives, per platform

| File   | ESP32 (now)                          | ESP32 (SD later)   | PC emulator                | Browser sim                          |
|--------|---------------------------------------|--------------------|-----------------------------|----------------------------------------|
| `.rom` | LittleFS `/game.rom` → `memory[0]`   | SD `/game.rom`     | `--rom path` → `memory[0]` | dropped file → heap `memory[0]`      |
| `.pak` | LittleFS `/game.pak` (TOC cached)    | SD `/game.pak`     | `roms/game.pak` read once  | dropped `.pak` → heap buffer         |
| saves  | LittleFS `/saves/<rom>.<slot>`       | SD `/saves/…`      | `saves/<rom>.<slot>`       | `localStorage emu16:<rom>.<slot>`    |

**In one line:** `.rom` is the program (a raw RAM image, loaded whole and run); `.pak` is the bundled
art/data (stays in storage, streamed by id, and since the map system may itself contain headered map
blobs); saves are opaque per-game blobs. The compiler makes the `.rom`, the importer + packer + map
tools make the `.pak`, and the game itself makes the saves at runtime.
