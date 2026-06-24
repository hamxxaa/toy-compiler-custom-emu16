// Memory map — shared between PC emulator, WASM simulator, and ESP32 firmware.
// Hardware pin assignments live in firmware/src/hw_pins.h.
//
// 0x0008–0x3FFF  DATA   compiler globals / arrays (incl. the font + streamed sprite sheets)
// 0x4000–0xADFB  CODE
// 0xADFC         stack base (pre-decrement grows DOWN)
// 0xADFE         SYSCALL_PORT  write-triggered host call
// 0xADFF         INPUT
// 0xAE00–0xAFFF  PRAM
// 0xB000–0xFFFF  VRAM

#define SYSCALL_PORT        0xADFE
#define SYSCALL_PRESENT     5      // syscall #: yield the frame to the host (display + pace), then resume
#define SYSCALL_TIME        6      // syscall #: R0 = milliseconds since boot (low 16 bits)
#define SYSCALL_SAVE        7      // R1=src  R2=len    R3=slot -> R0 = bytes written (0 = fail)
#define SYSCALL_LOAD        8      // R1=dest R2=maxlen R3=slot -> R0 = bytes read (0 = no save)
#define SYSCALL_SAVE_EXISTS 9      // R1=slot                   -> R0 = 1 if a save exists, else 0
#define SYSCALL_ASSET_INFO  10     // R1=id   R2=dest           -> R0 = length; writes 6-byte header at dest
#define SYSCALL_ASSET_LOAD  11     // R1=id   R2=dest R3=maxlen -> R0 = bytes copied (0 = fail/too big)
#define SYSCALL_SET_FPS     12     // R1=target fps (0=default 60); host paces frames to ~1000/fps ms
#define INPUT_ADDRESS      0xADFF
#define VRAM_START_ADDRESS 0xB000
#define VRAM_SIZE          20480     // 160 * 128 bytes
#define PRAM_START_ADDRESS 0xAE00
#define PRAM_SIZE          512       // 256 colors * 2 bytes each
#define MAX_RAM_ADDRESS    0xFFFF
#define STACK_START_ADDRESS 0xADFC

#define SCREEN_WIDTH  160
#define SCREEN_HEIGHT 128