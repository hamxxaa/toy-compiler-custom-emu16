// Memory map — shared between PC emulator, WASM simulator, and ESP32 firmware.
// Hardware pin assignments live in firmware/src/hw_pins.h.

#define INPUT_ADDRESS      0xADFF
#define VRAM_START_ADDRESS 0xB000
#define VRAM_SIZE          20480     // 160 * 128 bytes
#define PRAM_START_ADDRESS 0xAE00
#define PRAM_SIZE          512       // 256 colors * 2 bytes each
#define MAX_RAM_ADDRESS    0xFFFF
#define STACK_START_ADDRESS 0xADFE

#define SCREEN_WIDTH  160
#define SCREEN_HEIGHT 128