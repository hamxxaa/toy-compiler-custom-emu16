#define INPUT_ADDRESS 0xADFF
#define VRAM_START_ADDRESS 0xB000
#define VRAM_SIZE 20480 // 160x128 = 20480 bytes
#define PRAM_START_ADDRESS 0xAE00
#define PRAM_SIZE 512 // 256 colors * 2 bytes per color = 512 bytes
#define MAX_RAM_ADDRESS 0xFFFF
#define STACK_START_ADDRESS 0xADFE

#define SD_MOSI 35
#define SD_MISO 37
#define SD_SCK 36
#define SD_CS 38

#define LEFT_ARROW_PIN 4
#define UP_ARROW_PIN 5
#define RIGHT_ARROW_PIN 6
#define DOWN_ARROW_PIN 7

#define BUTTON_A_PIN 15
#define BUTTON_B_PIN 16
#define BUTTON_X_PIN 17
#define BUTTON_Y_PIN 18

#define SCREEN_WIDTH 160
#define SCREEN_HEIGHT 128