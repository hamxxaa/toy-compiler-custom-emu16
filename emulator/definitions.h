// Memory map — shared between PC emulator, WASM simulator, and ESP32 firmware.
// Hardware pin assignments live in firmware/src/hw_pins.h.
//
// 0x0008–0x0FFF  DATA   compiler globals / arrays (incl. the font, shipped in each ROM)
// 0x1000–0xADFB  CODE
// 0xADFC         stack base (pre-decrement grows DOWN)
// 0xADFE         SYSCALL_PORT  write-triggered host call
// 0xADFF         INPUT
// 0xAE00–0xAFFF  PRAM
// 0xB000–0xFFFF  VRAM

#define SYSCALL_PORT       0xADFE
#define SYSCALL_PRESENT    5      // syscall #: yield the frame to the host (display + pace), then resume
#define SYSCALL_TIME       6      // syscall #: R0 = milliseconds since boot (low 16 bits)
#define INPUT_ADDRESS      0xADFF
#define VRAM_START_ADDRESS 0xB000
#define VRAM_SIZE          20480     // 160 * 128 bytes
#define PRAM_START_ADDRESS 0xAE00
#define PRAM_SIZE          512       // 256 colors * 2 bytes each
#define MAX_RAM_ADDRESS    0xFFFF
#define STACK_START_ADDRESS 0xADFC

#define SCREEN_WIDTH  160
#define SCREEN_HEIGHT 128