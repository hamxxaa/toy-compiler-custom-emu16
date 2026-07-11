# Libraries reference

`lib/*.lib` files are plain EMU16 source — `include`d exactly like any other file, no special
library format or build step. This is the full function-by-function reference; for the practical
"what do I include to make a game" tour, see the [README](../README.md#making-a-game).

Each library has one job:

| Library | Job |
|---|---|
| [`io.lib`](#iolib--cpu-side-io) | Generic memory peek/poke, input reading, buttons. Nothing PPU-shaped. |
| [`sys.lib`](#syslib--host-syscall-wrappers) | Every host syscall wrapper. Only syscalls. |
| [`ppu.lib`](#ppulib--the-graphics-interface) | Builds the PPU command stream: scroll, sprites, text plane, palette, font. |
| [`map.lib`](#maplib--pak-loaded-maps) | Loads a pak map blob, reads its spawns/warps/entries, answers tile-collision queries. |
| [`scene.lib`](#scenelib--camera--sprite-cast) | Reflects the resident world onto the screen: camera + streaming + Y-sorted cast. Nothing else. |
| [`game.lib`](#gamelib--small-gameplay-helpers) | PRNG + AABB collision. |
| [`event.lib`](#eventlib--the-eventstate-spine) | 256 save-backed flags. |

Include order matters for two reasons: (1) the compiler dedups `include` **by file path**, so
including the same library from two different files is safe (no duplicate-symbol error) as long as
it's literally the same path; (2) a few libraries expect to be included *after* something else that
defines constants they read (noted per-library below) — get this backwards and you'll get an
"undefined identifier" at compile time, not a subtle runtime bug.

## `io.lib` — CPU-side I/O

No PPU knowledge, no drawing, no font data — just generic memory access, input, and button state.
Every routine is a NAKED asm function: no prologue/epilogue, the asm body supplies its own `RET`.
Calling convention (matches the compiler): arg1→R1, arg2→R2, arg3→R3, return value→R0.

- `poke_byte(addr, val)` / `peek_byte(addr)` — raw single-byte memory access.
- `poke(addr, val)` / `peek(addr)` — raw little-endian word access.
- `read_input()` — the raw button bitmask at `INPUT` (`0xADFF`).
- `BTN_LEFT` `BTN_UP` `BTN_RIGHT` `BTN_DOWN` `BTN_A` `BTN_B` `BTN_X` `BTN_Y` — `const` bit masks for
  `read_input()`/`button()`.
- `button(mask)` — true if the given button is currently held.
- `buttons_update()` — snapshots the current button state into `btn_prev` for next frame's edge
  detection. **Call exactly once per frame**, after that frame's `button_pressed`/`button_released`
  checks (they compare against the *previous* frame's snapshot — updating first would erase the
  edge before you can see it).
- `button_pressed(mask)` / `button_released(mask)` — edge detection: true only on the frame the
  button transitions (press / release respectively). Requires `buttons_update()` once per frame to
  be meaningful.

## `sys.lib` — host syscall wrappers

Every real host syscall, wrapper-per-number, and nothing else. Thin naked-`asm` bodies around
syscalls 1-15; see [syscalls.md](syscalls.md#reference) for full semantics of each.
`sys_list_roms` `sys_get_rom_name` `sys_load_rom` `sys_reset` `sys_present` `sys_time` `sys_save`
`sys_load` `sys_save_exists` `sys_asset_info` `sys_asset_load` `sys_set_fps` `sys_ppu_submit`
`sys_ppu_dma` `sys_ppu_upload`.

**Deliberate exception:** syscall `0x7F` (ECHO, a PC-only test diagnostic — see
[syscalls.md](syscalls.md#notes--per-host-differences)) has **no wrapper here**. No shipped game
should ever call it, so it doesn't belong in the file every real game includes; the one test that
needs it (`tests/test_syscall_echo.txt`) carries its own tiny local wrapper instead, using the exact
same naked-asm pattern as everything else in this file.

## `ppu.lib` — the graphics interface

The CPU-side command-buffer builder for the PPU, plus the font it uploads (font data has nowhere
else sensible to live — it's PPU-consumed, and nothing else currently justifies a separate `font.lib`,
so it stays here). Includes `sys.lib` for the three `sys_ppu_*` wrappers it calls internally. See
[Architecture → PPU](architecture.md#ppu) for the wire format and the "PPU never touches CPU memory"
invariant.

**Not here:** CPU-side tile collision and pak-loaded maps (see [`map.lib`](#maplib--pak-loaded-maps));
camera/streaming and the sprite cast (see [`scene.lib`](#scenelib--camera--sprite-cast)).

**Frame shape:**
```c
cmd_reset();
// ... build commands: ppu_backdrop / ppu_scroll / oam_set / tile_text ... ...
ppu_present();   // appends OAM + PRESENT, flushes to the PPU (compose + display + pace)
```

**Font:** `var byte font8x8[768]` — the baked-in 8×8 bitmap font (ASCII 32-127). Lives in the ROM's
DATA section (loads with the ROM on every host, no separate load, no load-order dependency) and is
consumed **only** via `ppu_upload_font`, below — nothing else needs it, which is why it moved here
from `io.lib`.

**Region/sentinel constants:** `PPU_PAT` `PPU_FONT` `PPU_TILEMAP` `PPU_PAL` (flat PPU-RAM addresses,
must match `emulator/ppu.cpp` — see [memory-map.md](memory-map.md#ppu-graphics-ram)); `GLYPH_SOLID`
(96, a full 8×8 block for dialog-box fills); `GLYPH_BLANK` (97, an all-zero glyph distinct from slot
0's "skip this cell" sentinel — needed so a space character can still get an opaque background);
`PPU_NO_BG` (255, the TEXT command's "transparent background" sentinel).

**Command buffer:**
- `cmd_reset()` — start a fresh frame (empties the command buffer and the OAM shadow, via `oam_clear()`).
- `cu8(v)` / `cu16(v)` — append one byte / one little-endian word (low-level; most callers use the
  higher-level functions below instead).
- `ppu_backdrop(rgb565)` — set palette entry 0 (shows wherever nothing else draws).
- `ppu_scroll(x, y)` — set background scroll (window-relative 0-511, wraps as a torus). Call every
  frame before `ppu_present` to move the camera.

**Sprites (OAM shadow)** — build during frame logic, `ppu_present` emits it as one OAM command.
`(x,y)` is **screen-space** (do world→screen conversion yourself, or use `scene.lib`'s `scene_draw`).
`attr`: bit0 flipx, bit1 flipy, bit2 behind-background. Emit back-to-front (smallest feet-Y first)
for correct Y-sort.
- `oam_clear()` — empty the sprite list (also done by `cmd_reset`; call directly mid-frame to
  rebuild only OAM).
- `oam_set(pat, x, y, attr)` — add a 16×16 sprite.
- `ppu_emit_oam()` — append the OAM command for the current shadow list (exposed so tests can inspect
  the bytes without presenting, which would yield the frame).
- `ppu_present()` — emit OAM + PRESENT and submit. **The** end-of-frame call.

**Text plane** (20×16 cells, `{glyph, fg, bg}` each, the whole screen) — `glyph = ascii - 32`
(space = 0 = empty cell, *skipped entirely* by the compositor, not just blank).
- `tile_text(cx, cy, s, color)` — draw a NUL-terminated string, transparent background (HUD over the
  world). Clips to the 20-cell row width — does **not** wrap to the next row.
- `tile_text_bg(cx, cy, s, fg, bg)` — same, but "off" pixels also paint `bg` (opaque cell) — use for
  dialog/menu text so letters don't show the tilemap through the gaps between strokes. A space in the
  string is substituted with `GLYPH_BLANK` so the background fill still applies to it.
- `tile_fill(cx, cy, w, h, glyph, color)` — fill a block of cells with one glyph/color (a dialog box
  body = `GLYPH_SOLID`; erase = glyph 0).
- `tile_clear(cx, cy, w, h)` — shorthand for `tile_fill(..., 0, 0)`.
- `tile_number(cx, cy, n, color)` — draw a signed decimal integer (HUD scores/counters).
- **No bounds-clamping**: a string longer than the remaining columns on its row is **not** truncated
  by these functions — it spills into the next row's cells (or past `PPU_TEXTMAP` into OAM, for a
  long enough string on the last row). Callers with untrusted-length strings (e.g. `examples/menu.txt`
  displaying ROM filenames) must truncate themselves before calling.

**Palette (live effects):**
- `ppu_pal1(idx, rgb565)` — rewrite one palette entry; shows next `PRESENT`, no recompose needed.
  Flash/fade/cycle by editing entries per-frame; restore by re-DMAing the palette blob.
- `ppu_upload_font(src)` — upload the 8×8 font: `src` is a 768-byte ASCII 32-127 bitmap (`font8x8`,
  above) to slots 0-95, plus `GLYPH_SOLID`/`GLYPH_BLANK` synthesized at slots 96/97. Call once at
  startup; the PPU has **no default font**.

**Internal only** (used by the functions above, not meant to be called directly, but not
mechanically private — EMU16 has no visibility modifiers):
- `ppu_rdbyte(addr)` — reads one byte of guest memory. Duplicates `io.lib`'s `peek_byte` on purpose,
  so `ppu.lib` has no dependency on `io.lib`.
- `ppu_strlen(s)` — NUL-terminated string length, used by `tile_text_bg`.

## `map.lib` — pak-loaded maps

Parses a headered map blob from the `.pak` into the resident world, exposes its spawns/warps/entry
points, and answers CPU-side tile-collision queries against whatever world is currently resident —
whether that world came from `map_load()` or was baked into the ROM at compile time by
`tools/pixel_map.py`. Includes `ppu.lib` (for `PPU_PAT`/`PPU_TILEMAP`/`PPU_PAL` and `ppu_rdbyte`) and
`sys.lib` (for `sys_asset_load`/`sys_ppu_dma`/`sys_ppu_upload`). Blob format:
[file-formats.md](file-formats.md#3-map-blobs-a-tilemap-asset).

- `const PPU_SPRITE_SLOTS = 80` — the split point between boot-resident sprite patterns (0-79) and
  the per-map tileset region (80-127). Retune by changing this one constant.
- `const MAX_MAP_BLOB = 9728` — compile-time ceiling for the largest map this game can load (a 96×96
  world plus worst-case header/flags/entries/spawns/warps sections).
- `var byte map_buf[MAX_MAP_BLOB]` — the whole loaded blob; every `cur_*` global below is a pointer
  **into** this buffer, valid only until the next `map_load()`.

#### Map loading

- `int map_load(pak_id)` — load map `pak_id` from the `.pak`: parse the header, DMA its
  tileset+palette (skipped per-field if the header says 255 = keep current), set the section
  pointers, and push tiles — one bulk upload if the map fits the 32×32 PPU window, else it sets
  `cur_stream=1` so the frame loop should call `scene_stream()` instead of a one-time upload. Returns
  1 on success, 0 on a bad id, bad magic, or a blob too large for `map_buf`. Loading a new map simply
  overwrites `map_buf` and every `cur_*` pointer — there's no separate "unload" step.
- `cur_w`, `cur_h` — the loaded map's tile dimensions.
- `cur_world`, `cur_flags` — pass directly to `map_solid_at`/`scene_stream` as `cmap`/`flags`/`world`.
- `map_spawn_count()`, `map_spawn_field(i, f)` — `f`: 0=type 1=tx 2=ty 3=arg.
- `map_warp_count()`, `map_warp_field(i, f)` — `f`: 0=tx 1=ty 2=target_map_id 3=target_entry 4=arg.
- `map_entry_field(e, f)` — `f`: 0=tx 1=ty 2=facing (0=side 1=up 2=down).
- `map_warp_at(ptx, pty)` — index of the warp whose trigger tile is `(ptx,pty)`, or `-1` if none.
  The game-side pattern (see `examples/arena.txt`) is: on movement, check `map_warp_at` at the
  player's new tile; if found, look up its fields and call `map_load(target_map_id)` +
  place the player at `target_entry`.

**Tile collision** (CPU-side, the source of truth — invariant I1: the PPU never sees this. Renamed
from `ppu_solid_at`, since it's a map/collision query, not something that builds a PPU command):
- `map_solid_at(cmap, map_w, flags, wx, wy)` — nonzero if the tile under world pixel `(wx,wy)` is
  solid. `cmap` is a byte-per-tile grid, `flags` is a property table indexed by tile id (bit 0 =
  solid). Works against either `cur_world`/`cur_flags` (pak-loaded) or a compile-time-baked
  `world[]`/`tile_flags[]` (`tools/pixel_map.py`) — it only ever needs the two byte arrays, not the
  map system itself. Caller keeps `(wx,wy)` in-bounds.

## `scene.lib` — camera + sprite cast

Reflects the CPU-resident world onto the screen. Nothing else — it doesn't parse maps, load assets,
or know whether the world it's streaming was pak-loaded or compiled in; `scene_stream` just takes a
`(world, map_w, map_h)` triple. Includes `ppu.lib` (for `cu8`, `oam_set`, `ppu_scroll`).

**Typical frame:**
```c
scene_follow(hero_x, hero_y, MAP_W, MAP_H);   // clamp camera to the world
cmd_reset();
scene_stream(&world, MAP_W, MAP_H);            // scroll + push the visible tiles
scene_clear();
scene_obj(hero_pat, hero_x, hero_y, hero_attr);
scene_obj(TREE_PAT, tree_x, tree_y, 0);        // ... more objects
scene_draw();                                  // Y-sort -> OAM
ppu_present();
```

**Camera & streaming:**
- `scene_follow(wx, wy, map_w, map_h)` — center the camera on world pixel `(wx,wy)`, clamped to a
  `map_w × map_h` tile world (16px/tile). A world smaller than the screen pins the camera at 0.
- `scene_stream(world, map_w, map_h)` — push the visible window of `world` into the PPU's 32×32
  tilemap torus and set the scroll registers. Emits one 1-cell `TM_EDIT` per visible tile (~99/frame)
  — correct for any world size/wrap position; edge-only streaming (only pushing the newly-revealed
  strip as the camera crosses a tile boundary) is a possible future optimization, not implemented.

**Sprite cast (world-space, Y-sorted):**
- `scene_clear()` — empty the cast for this frame.
- `scene_obj(pat, wx, wy, attr)` — add an object at world coordinates.
- `scene_draw()` — insertion-sorts the cast by world Y, then emits back-to-front as OAM (nearer/lower
  objects draw on top), converting world→screen with the current camera.

## `game.lib` — small gameplay helpers

Includes `sys.lib` (for `sys_time`, used by `srand_time`) — safe to also include `sys.lib` yourself
in the same program, the compiler dedups by path.

- `srand(seed)` — seed the PRNG (0 is remapped, since xorshift can't start at 0).
- `srand_time()` — seed from `sys_time()`, so each run differs. Call once at startup.
- `rand()` — 16-bit xorshift, next pseudo-random value (full range, may be negative).
- `rand_range(n)` — pseudo-random int in `[0, n)` for `n > 0`.
- `aabb(ax, ay, aw, ah, bx, by, bw, bh)` — 1 if box A overlaps box B, else 0. Integer AABB overlap,
  used for hit detection / simple physics.

## `event.lib` — the event/state spine

256 one-bit, save-backed flags — the gate for one-time triggers, gated doors, branching dialog, and
saved progress. Events themselves are ordinary game functions dispatched by a `switch` on an id (an
O(1) jump table — see the [README](../README.md#making-a-game)); this library only manages the flags.
A dialog box on the PPU model is just a text-plane rectangle (`tile_fill` + `tile_text_bg` from
`ppu.lib`) drawn directly by the game — there's no separate dialog-widget API.

- `const NUM_FLAGS = 256`, `const FLAG_BYTES = 32`.
- `flag_get(n)` / `flag_set(n)` / `flag_clear(n)` — bit `n` of the flag set.
- `flags_reset()` — zero every flag (new game).
- `flags_save(slot)` / `flags_load(slot)` — persist/restore the whole 32-byte flag blob via
  `sys_save`/`sys_load`. This is the simplest possible save format — no header, no versioning; a game
  that saves more than flags wraps its own struct around this (or writes a separate blob to a
  different slot).
