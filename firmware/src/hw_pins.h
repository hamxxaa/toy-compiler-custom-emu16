// ESP32-S3 GPIO pin assignments for the handheld hardware.
// Memory-map constants (SYSCALL_PORT, INPUT_ADDRESS, etc.) are in emulator/definitions.h.
//
// HEADS UP -- pin allocation is split across two files. The DISPLAY's pins are NOT here: they are
// TFT_eSPI build flags in firmware/platformio.ini (currently RST=8, DC=9, CS=10, MOSI=11, SCLK=12).
// Check BOTH files before claiming a GPIO. Also reserved by the ESP32-S3 itself: 0/3/45/46
// (strapping), 19/20 (native USB), 43/44 (UART0 = the debug Serial), 26-32 (flash/PSRAM),
// 48 (onboard RGB LED on the devkitc-1).
#pragma once

// ⚠️ Both boards are ESP32-S3-N16R8 = OCTAL PSRAM, which reserves GPIO 35/36/37. The SD pins below
// (a leftover from a non-PSRAM assumption) therefore CONFLICT with PSRAM once it's enabled — SD is
// only wired, not yet used, so it's harmless today, but MOVE these off 35-37 before wiring SD for
// real (or before enabling PSRAM). The inter-chip wire (wire.h) already avoids 35-37.
#define SD_MOSI 35
#define SD_MISO 37
#define SD_SCK  36
#define SD_CS   38

#define LEFT_ARROW_PIN  4
#define UP_ARROW_PIN    5
#define RIGHT_ARROW_PIN 6
#define DOWN_ARROW_PIN  7

#define BUTTON_A_PIN 15
#define BUTTON_B_PIN 16
#define BUTTON_X_PIN 17
#define BUTTON_Y_PIN 18

// I2S audio out -> MAX98357A Class-D amp -> 8 ohm speaker. Three signal pins; the amp's other pins
// are hard-wired (VIN=5V, GND, GAIN floating = 9dB, SD -- see below). Picked clear of the buttons,
// the SD card, the TFT's platformio.ini pins, and every reserved GPIO listed at the top.
//
// The amp's SD pin is BOTH shutdown AND channel-select-by-voltage ((L+R)/2 vs Left vs Right). We
// dodge that trap entirely by transmitting the SAME mono sample in BOTH I2S slots, which comes out
// correct under all three strap settings -- so SD only has to be "not shutdown" (i.e. not pulled to
// GND).
#define I2S_BCLK_PIN 13   // bit clock      -> amp BCLK
#define I2S_LRC_PIN  14   // word select    -> amp LRC
#define I2S_DOUT_PIN 21   // serial data    -> amp DIN
