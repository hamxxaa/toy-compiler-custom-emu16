// Memory map — shared between PC emulator, WASM simulator, and ESP32 firmware.
// Hardware pin assignments live in firmware/src/hw_pins.h.
//
// 0x0008–0x7FFF  DATA   compiler globals / arrays (incl. the font, streamed sprite sheets, and the
//                       runtime map buffer for pak-loaded maps) — 32 KB, bumped from 24 KB when the
//                       legacy VRAM/PRAM path was reclaimed (VRAM/PRAM no longer exist; every ROM
//                       drives the PPU now)
// 0x8000–0xFFFB  CODE + stack (shared; stack grows DOWN from the top of this region)
// 0xFFFC         stack base (pre-decrement grows DOWN; floor = STACK_FLOOR below)
// 0xFFFE         SYSCALL_PORT  write-triggered host call
// 0xFFFF         INPUT
//
// The DATA/CODE boundary is CODE_START_ADDRESS in src/backend/EmuBackend.py (compiler-only — the
// emulators just load the flat ROM image). STACK_FLOOR below MUST equal that same boundary: it's
// the emulator core's stack-underflow sanity check (emu.cpp), so the two constants move together
// any time DATA is resized.
#define STACK_FLOOR         0x8000

#define SYSCALL_PORT        0xFFFE
#define SYSCALL_PRESENT     5      // syscall #: yield the frame to the host (display + pace), then resume
#define SYSCALL_TIME        6      // syscall #: R0 = milliseconds since boot (low 16 bits)
#define SYSCALL_SAVE        7      // R1=src  R2=len    R3=slot -> R0 = bytes written (0 = fail)
#define SYSCALL_LOAD        8      // R1=dest R2=maxlen R3=slot -> R0 = bytes read (0 = no save)
#define SYSCALL_SAVE_EXISTS 9      // R1=slot                   -> R0 = 1 if a save exists, else 0
#define SYSCALL_ASSET_INFO  10     // R1=id   R2=dest           -> R0 = length; writes 6-byte header at dest
#define SYSCALL_ASSET_LOAD  11     // R1=id   R2=dest R3=maxlen -> R0 = bytes copied (0 = fail/too big)
#define SYSCALL_SET_FPS     12     // R1=target fps (0=default 60); host paces frames to ~1000/fps ms
// ── PPU reboot (see tiling-scrolling-ricoh.md) ──
#define SYSCALL_PPU_SUBMIT  13     // R1=buf R2=len -> execute a PPU command stream; PRESENT yields the frame
#define SYSCALL_PPU_DMA     14     // (Phase 1) R1=pak_id R2=target R3=dst_off -> DMA pak asset -> PPU region
#define SYSCALL_PPU_UPLOAD  15     // (Phase 1) bulk copy CPU buffer -> PPU region (baked test data, no pak)
#define INPUT_ADDRESS      0xFFFF
#define MAX_RAM_ADDRESS    0xFFFF
#define STACK_START_ADDRESS 0xFFFC

#define SCREEN_WIDTH  160
#define SCREEN_HEIGHT 128