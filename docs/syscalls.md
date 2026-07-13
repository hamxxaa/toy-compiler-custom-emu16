# Syscalls

A ROM asks the host to do something outside the CPU's own power — list/load games, persist a save,
stream an asset, drive the PPU, pace a frame — by **byte-storing** a syscall number to
`SYSCALL_PORT` (`0xFFFE`). It must be a byte store: a word store would also write `0xFFFF`
(`INPUT`) and clobber the button state. Arguments go in `R1`-`R3`; the result comes back in `R0`.
Every host (firmware, `pc_emu`, the browser) registers its own handler for the same numbers — the
*meaning* of each syscall is fixed, but *how* it's fulfilled (LittleFS vs. a CLI-arg file vs.
`localStorage`) is entirely host-specific. Definitions:
[`emulator/definitions.h`](../emulator/definitions.h).

Calling convention matches ordinary functions ([memory-map.md](memory-map.md#calling-convention)):
the `lib/sys.lib` / `lib/ppu.lib` wrappers are naked `asm` functions whose bodies write the port
after their arguments are already in place.

## Reference

| # | Wrapper | Args (R1/R2/R3) | Result (R0) | Does |
|---|---|---|---|---|
| 1 | `sys_list_roms(dest, max)` | dest, max | count | Writes `[len][name][NUL]` records at `dest`, up to `max` entries; returns how many |
| 2 | `sys_get_rom_name(index, dest)` | index, dest | length | Writes one ROM name + NUL at `dest` |
| 3 | `sys_load_rom(index)` | index | — | Halts the CPU; host loads ROM `index` next frame |
| 4 | `sys_reset()` | — | — | Halts the CPU; host reloads `/menu.rom` next frame |
| 5 | `sys_present()` | — | — | Yields the finished frame: host displays it, paces to the target FPS, then resumes you — a vblank |
| 6 | `sys_time()` | — | ms (low 16 bits) | Milliseconds since boot, wraps every ~65 s |
| 7 | `sys_save(src, len, slot)` | src, len, slot | bytes written (0=fail) | Writes `len` bytes from `src` to per-ROM, per-slot storage |
| 8 | `sys_load(dest, maxlen, slot)` | dest, maxlen, slot | bytes read (0=none) | Reads a save slot back into `dest` |
| 9 | `sys_save_exists(slot)` | slot | 1/0 | Checks presence without reading |
| 10 | `sys_asset_info(id, dest)` | id, dest | length (0=bad id) | Writes a 6-byte `{type,w,h,_,len_lo,len_hi}` header at `dest`, so the game can size a buffer first |
| 11 | `sys_asset_load(id, dest, maxlen)` | id, dest, maxlen | bytes copied (0=fail/too big) | Copies asset `id`'s bytes from the `.pak` into guest memory at `dest` |
| 12 | `sys_set_fps(fps)` | fps (0=default 60) | — | Requests a frame-pace target; also sizes the per-frame instruction budget |
| 13 | `sys_ppu_submit(buf, len)` | buf, len | — | Executes a PPU command stream; a trailing `PRESENT` composes + yields the frame |
| 14 | `sys_ppu_dma(pak_id, ppu_addr)` | pak_id, ppu_addr | bytes streamed | Streams a `.pak` asset straight into PPU RAM (tilesets, sheets, palettes) — no CPU-side buffer |
| 15 | `sys_ppu_upload(ppu_addr, cpu_src, len)` | ppu_addr, cpu_src, len | bytes copied | Copies a CPU buffer into PPU RAM (baked/computed data, no pak involved) |
| 16 | `sys_apu_submit(buf, len)` | buf, len | — | Executes an APU command stream — applies **immediately** (no `PRESENT`-style latch), since audio is a continuous stream, not a per-frame image |

Wrappers live in `lib/sys.lib` (1-9, 12, 16) and `lib/ppu.lib` (13-15); asset-related wrappers (10-11)
are also in `sys.lib` since they're used regardless of whether a game touches the PPU. `sys_apu_submit`
sits in `sys.lib` too — `lib/apu.lib` builds the command batch CPU-side and just calls it to flush.

## Notes & per-host differences

- **`sys_present()` is the frame boundary.** For PPU games the per-frame pattern is always
  `cmd_reset()` → build commands → `ppu_present()` (which itself calls `sys_ppu_submit` ending in
  `PRESENT`), so the game loop runs exactly once per displayed frame — no tearing, no double-buffering
  needed on the game's side.
- **`sys_set_fps` is honored differently per host**: the firmware paces real frame timing
  (`g_frame_target_ms`, checked in `main.cpp`'s `loop()`) and it also **sizes the instruction budget**
  (a lower target FPS gives a longer per-frame budget); the browser sim does its best via
  `requestAnimationFrame` throttling; the one-shot `pc_emu` CLI runner (which just executes N frames
  and exits, with no real-time concept) treats it as a no-op.
- **`0x7F` (ECHO)** is not a real syscall — it's a PC-only diagnostic (`pc_emulator_main.cpp`:
  `R0 = R1 + R2`) that `tests/test_syscall_echo.txt` uses to regression-test the whole
  port-write → handler → arg-read → result-write path end to end. It doesn't exist on firmware or
  in the browser.
- **Asset syscalls (10/11) need a `.pak` loaded first.** Each host lazily loads `<rom-basename>.pak`
  the first time an asset syscall runs (or on ROM load, for the firmware) — see
  [file-formats.md](file-formats.md#2-pak--the-asset-pack-epak). If no matching `.pak` exists, both
  return 0/length-0 rather than erroring.
- **Save/asset paths are namespaced by the running ROM's basename** (`g_current_rom` in firmware,
  `g_pc_current_rom` in `pc_emu`, a JS-side `setCurrentRom()` call in the browser) — two different
  games' save slot `0` never collide, and switching games (`sys_reset`/`sys_load_rom`) re-namespaces
  automatically. See each host's own doc ([firmware.md](firmware.md),
  [pc-emulator.md](pc-emulator.md), [simulator.md](simulator.md)) for exact paths.
- **`examples/menu.txt`** is an ordinary ROM, not special-cased by the host — it just happens to be
  the one the firmware boots by default and the one `sys_reset`/syscall 4 returns to. It lists and
  launches other ROMs purely through syscalls 1-4.
- **`sys_apu_submit` has no frame boundary**, unlike `sys_ppu_submit`. The APU free-runs at audio rate
  from resident voice state; the CPU just pokes register-style commands a few times a frame (usually
  once, from `music_frame()`) and they take effect the instant the syscall returns — see
  [Architecture → APU](architecture.md#apu).

For the byte-level PPU command stream that `sys_ppu_submit` executes, see
[Architecture → PPU](architecture.md#ppu). For the APU's command stream, see
[libraries.md → apu.lib](libraries.md#apulib--the-sound-chip-interface).
