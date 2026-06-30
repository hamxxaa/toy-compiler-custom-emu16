# EMU16 file formats

EMU16 now has three on-disk formats: the program (`.rom`), the asset pack (`.pak`), and save files.
This doc explains what each contains, how it's built, and how the host reads it at runtime.

## The key split: address space vs. storage

Two different "spaces," and the formats live in different ones:

- **The 64 KB address space** — what the CPU sees (RAM at runtime): `bootstrap · DATA · CODE · stack ·
  PRAM · VRAM` (see the memory map in [README](README.md)). It is *volatile* and re-created every boot.
- **Storage** — ESP32 flash (LittleFS, → SD later) · PC disk · browser `localStorage`. Files live here,
  *outside* the address space, and are moved in/out by **loading** (a `.rom` becomes RAM) or by
  **syscalls** (`.pak`/save bytes ↔ guest buffers).

---

## 1. `.rom` — the program

A **flat image of low memory, with no header**. The compiler ([`src/backend/EmuBackend.py`](src/backend/EmuBackend.py))
emits one byte array that maps 1:1 onto addresses `0x0000 … code_end`:

```
0x0000–0x0007   Bootstrap   LDI SP ; JMP 0x1000
0x0008–0x0FFF   DATA        globals, arrays, the 8×8 font, and baked array-literals + string literals
0x1000–…        CODE        entry block (run global initializers → call main) + every function
```

- **Built:** `python main.py game.txt --save-rom game` → `build/roms/game.rom`.
- **Loaded:** the host copies the whole file **verbatim** into `memory[0]`. No parsing — the file *is*
  the RAM image of `0x0000…code_end`.
- **Booted:** the CPU starts at `pc = 0`; the bootstrap sets the stack pointer and jumps to `0x1000`,
  whose entry block initializes globals and calls `main`.
- **Limits:** must fit below the stack (`size < 0xADFC`). PRAM/VRAM/INPUT are **not** in the ROM — they
  sit above the image and the host initializes them (default palette, cleared VRAM, zero input).

A `.rom` is self-contained code + data. It carries **no** art and **no** save state.

---

## 2. `.pak` — the asset pack (EPAK)

Bundled art/data that lives in storage and is **streamed in piece-by-piece** — never loaded whole into
the 64 KB. One pack per ROM, namespaced by ROM basename (`game.pak` next to `game.rom`). Built by
[`tools/pack_assets.py`](tools/pack_assets.py).

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
  transparent). Palette blobs are 512 bytes of RGB565. Text blobs are NUL-terminated.
- **Read at runtime:** on ROM load the host parses the small TOC into RAM (firmware caches it + keeps
  the file handle; PC reads the whole pak into memory; the browser holds it in a heap buffer). Then:
  - `sys_asset_info(id, dest)` → writes `{type, w, h, _, len_lo, len_hi}` and returns the length, so the
    game can size a buffer first.
  - `sys_asset_load(id, dest, max)` → seeks to `offset` and copies `length` bytes into guest memory
    (a RAM scratch buffer, or straight into VRAM for a full-screen background).
- This is why a multi-megabyte pack works on a 64 KB machine: the pack stays in storage; only the
  current room/sprite is resident. **Call asset loads on room/scene transitions, not per frame**
  (they hit flash/SD).

---

## 3. Save files

Per-ROM, per-slot, and **format-less from the engine's view** — the byte layout is the game's business.

- **Where:** `/saves/<rom>.<slot>` (LittleFS / SD), `saves/<rom>.<slot>` (PC), `localStorage`
  key `emu16:<rom>.<slot>` (browser, base64). `<rom>` is the running ROM's basename, so games never
  collide.
- **Write:** the game fills a `var byte save_buf[N]`, then `sys_save(&save_buf, len, slot)`. The host
  writes the bytes verbatim; it does not interpret them.
- **Read:** `sys_load(&save_buf, max, slot)` copies them back (returns bytes read, 0 if none);
  `sys_save_exists(slot)` checks presence.
- **Convention (game-side, not enforced):** lead the blob with a magic + version byte so a newer build
  can reject/migrate a stale save; everything after is your fields.

The host storage backend is isolated in [`firmware/src/storage.h`](firmware/src/storage.h)
(`STORAGE_FS` macro) — the single seam where LittleFS swaps for SD, for both `.pak` reads and saves.

---

## Build-time-only files (not shipped, not read at runtime)

These are inputs/intermediates of the build, not runtime formats:

- **`sprites.list`** — the importer's asset list (`<name> <src> <json|-> <fps|->` per line). The row
  whose json is `-` names the **master palette source**: a `.gpl` (GIMP/LibreSprite palette — the
  editable, git-friendly source of truth) or an indexed PNG (its embedded PLTE). It becomes a 512-byte
  (256-entry RGB565) palette asset → **PRAM**, loaded once at startup. Every sprite is byte-per-pixel
  *indices* into this one shared palette, so the color at each index is fixed across every sheet —
  **append** colors to the master (line order = index = PRAM order), never reorder or insert, and
  keep index 0 as the transparent slot. The importer reads `.gpl` as plain text (header/comments/
  `Name:`/`Columns:` skipped; first three ints per line = R G B).
- **manifest** (`name type w h source` per line) — tells `pack_assets.py` what to bundle.
- **generated `const` include** (`*_assets.txt` / `sprites.gen.txt`) — asset & animation ids; it's
  `include`d by the game source and **compiled into the `.rom`**, so the ids end up as plain literals.
  For sprites it also emits the **clip registry** — `const NUM_CLIPS` plus the `clip_w/h/count/period[]`
  metadata tables (indexed by id; palette rows are zero) and a zeroed `clip_buf[]` for runtime sheet
  addresses. This is the fixed ABI between the importer and `lib/anim.lib`, which reads those tables;
  include the generated file **before** `anim.lib`.
- **`.bin` intermediates + the LibreSprite PNG/JSON** — the importer's inputs/outputs that flow into the
  pack, then are discarded.

---

## Lifecycle

```
BUILD                                          DEPLOY              RUNTIME (inside the 64 KB)
game.txt + libs ─compiler→ game.rom ───────────────────────────► memory[0]  (verbatim)  ─► CPU runs
art.png + json ─importer→ bins + manifest ─pack→ game.pak ─┐
   (+ const include compiled INTO game.rom)                └────────► TOC cached ─► sys_asset_load ─► RAM/VRAM
                                              (created at runtime) ◄── sys_save ──  save_buf
                                                                  ──► sys_load ──►  save_buf
```

## Where each file lives, per platform

| File   | ESP32 (now)                          | ESP32 (SD later)   | PC emulator                | Browser sim                          |
|--------|--------------------------------------|--------------------|----------------------------|--------------------------------------|
| `.rom` | LittleFS `/game.rom` → `memory[0]`   | SD `/game.rom`     | `--rom path` → `memory[0]` | dropped file → heap `memory[0]`      |
| `.pak` | LittleFS `/game.pak` (TOC cached)    | SD `/game.pak`     | `roms/game.pak` read once  | dropped `.pak` → heap buffer         |
| saves  | LittleFS `/saves/<rom>.<slot>`       | SD `/saves/…`      | `saves/<rom>.<slot>`       | `localStorage emu16:<rom>.<slot>`    |

**In one line:** `.rom` is the program (a raw RAM image, loaded whole and run); `.pak` is the bundled
art/data (stays in storage, streamed by id); saves are opaque per-game blobs. The compiler makes the
`.rom`, the importer + packer make the `.pak`, and the game itself makes the saves at runtime.
