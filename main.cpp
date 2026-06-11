#include <Arduino.h>
#include <SPI.h>
#include <TFT_eSPI.h>
#include <LittleFS.h>
#include "emu.h"
#include "definitions.h"

TFT_eSPI tft = TFT_eSPI();

uint16_t frame_buffer[SCREEN_WIDTH * SCREEN_HEIGHT];

// Development toggles.
constexpr bool ENABLE_FRAMEBUFFER_TEST = false;
constexpr bool ENABLE_DEBUG_LOGS = true;

uint8_t input_state = 0;

void get_input();
void init_default_pram();
void run_framebuffer_color_test();

void load_rom_from_flash(const char *filename)
{
    if (ENABLE_DEBUG_LOGS)
    {
        Serial.printf("Game loading: %s\n", filename);
    }

    if (!LittleFS.begin(true))
    {
        Serial.println("Error: Failed to initialize LittleFS!");
        return;
    }

    File rom_file = LittleFS.open(filename, "r");

    if (!rom_file)
    {
        Serial.println("Error: ROM file not found in LittleFS!");
        return;
    }

    size_t file_size = rom_file.size();
    if (ENABLE_DEBUG_LOGS)
    {
        Serial.printf("ROM size: %u bytes\n", static_cast<unsigned int>(file_size));
    }

    if (file_size > 0xADFD)
    {
        Serial.println("Error: ROM file size exceeds available RAM!");
        rom_file.close();
        return;
    }

    rom_file.read(cpu_instance.memory, file_size);

    rom_file.close();
    if (ENABLE_DEBUG_LOGS)
    {
        Serial.println("Game loaded successfully!");
    }
}

void setup()
{
    Serial.begin(115200);
    delay(3000);

    const int buttons[] = {LEFT_ARROW_PIN, UP_ARROW_PIN, RIGHT_ARROW_PIN, DOWN_ARROW_PIN, BUTTON_A_PIN, BUTTON_B_PIN, BUTTON_X_PIN, BUTTON_Y_PIN};
    for (int i = 0; i < 8; i++)
    {
        pinMode(buttons[i], INPUT_PULLUP);
    }

    tft.init();
    tft.setRotation(3);
    tft.fillScreen(TFT_BLACK);
    tft.setSwapBytes(true);

    if (ENABLE_FRAMEBUFFER_TEST)
    {
        if (ENABLE_DEBUG_LOGS)
        {
            Serial.println("Framebuffer test mode active.");
        }
        run_framebuffer_color_test();
        return;
    }

    initialize_cpu();

    init_default_pram();

    load_rom_from_flash("/input_colors.rom");

    for (int i = 0; i < VRAM_SIZE; i++)
    {
        cpu_instance.memory[VRAM_START_ADDRESS + i] = 0;
    }

    if (ENABLE_DEBUG_LOGS)
    {
        Serial.println("CPU initialized.");
        Serial.printf("PRAM[4]=0x%02X PRAM[5]=0x%02X\n",
                      cpu_instance.memory[PRAM_START_ADDRESS + 4],
                      cpu_instance.memory[PRAM_START_ADDRESS + 5]);
        Serial.printf("VRAM[0]: %d\n", cpu_instance.memory[VRAM_START_ADDRESS]);
        Serial.printf("VRAM[100]: %d\n", cpu_instance.memory[VRAM_START_ADDRESS + 100]);
        Serial.printf("PC: 0x%04X\n", cpu_instance.pc);
        Serial.printf("R0: 0x%04X\n", cpu_instance.registers[0].word);
    }
}

void loop()
{
    if (ENABLE_FRAMEBUFFER_TEST)
    {
        run_framebuffer_color_test();
        delay(16);
        return;
    }

    /*
      Workflow:
      1. get input from buttons and write to input address in memory
      2. run a batch of instructions (e.g., 20k) to allow the emulated program to process the input and update VRAM
      3. read VRAM from emulated memory and update the display
      4. sync with 16.67ms frame delay for ~60 FPS
    */

    uint32_t frame_start_time = millis();

    get_input();
    if (input_state != 0 && ENABLE_DEBUG_LOGS)
    {
        Serial.printf("Input State: 0x%02X\n", input_state);
    }

    cpu_instance.memory[INPUT_ADDRESS] = input_state;

    run_20k_instruction();

    uint16_t current_palette[256];
    for (int i = 0; i < 256; i++)
    {
        uint16_t low_byte = cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)];
        uint16_t high_byte = cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1];
        current_palette[i] = low_byte | (high_byte << 8);
    }

    static int debug_frame_count = 0;
    if (ENABLE_DEBUG_LOGS && debug_frame_count < 3)
    {
        Serial.printf("=== FRAME %d VRAM->Palette Mapping ===\n", debug_frame_count);
        for (int i = 0; i < 8; i++)
        {
            uint8_t vram_index = cpu_instance.memory[VRAM_START_ADDRESS + i];
            uint16_t color = current_palette[vram_index];
            Serial.printf("VRAM[%d]=%d -> Palette[%d]=0x%04X\n", i, vram_index, vram_index, color);
        }
    }
    debug_frame_count++;

    for (int i = 0; i < VRAM_SIZE; ++i)
    {
        uint8_t color_index = cpu_instance.memory[VRAM_START_ADDRESS + i];
        frame_buffer[i] = current_palette[color_index];
    }

    tft.pushImage(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT, frame_buffer);

    int32_t frame_duration = millis() - frame_start_time;
    if (frame_duration < 16)
    {
        delay(16 - frame_duration);
    }
}

void run_framebuffer_color_test()
{
    for (int i = 0; i < (SCREEN_WIDTH * SCREEN_HEIGHT); ++i)
    {
        frame_buffer[i] = TFT_GREEN;
    }
    tft.pushImage(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT, frame_buffer);
}

void get_input()
{
    input_state = 0;
    if (digitalRead(LEFT_ARROW_PIN) == LOW)
        input_state |= 0x01; // Bit 0: Left
    if (digitalRead(UP_ARROW_PIN) == LOW)
        input_state |= 0x02; // Bit 1: Up
    if (digitalRead(RIGHT_ARROW_PIN) == LOW)
        input_state |= 0x04; // Bit 2: Right
    if (digitalRead(DOWN_ARROW_PIN) == LOW)
        input_state |= 0x08; // Bit 3: Down
    if (digitalRead(BUTTON_A_PIN) == LOW)
        input_state |= 0x10; // Bit 4: A
    if (digitalRead(BUTTON_B_PIN) == LOW)
        input_state |= 0x20; // Bit 5: B
    if (digitalRead(BUTTON_X_PIN) == LOW)
        input_state |= 0x40; // Bit 6: X
    if (digitalRead(BUTTON_Y_PIN) == LOW)
        input_state |= 0x80; // Bit 7: Y
}

void init_default_pram()
{
    uint16_t defaults[8] = {TFT_BLACK, TFT_WHITE, TFT_RED, TFT_BLUE, TFT_GREEN, TFT_MAGENTA, TFT_CYAN, TFT_YELLOW};

    if (ENABLE_DEBUG_LOGS)
    {
        Serial.println("=== PRAM INITIALIZATION ===");
    }
    for (int i = 0; i < 8; i++)
    {
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)] = defaults[i] & 0xFF;
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1] = (defaults[i] >> 8) & 0xFF;
        if (ENABLE_DEBUG_LOGS)
        {
            Serial.printf("Index %d: 0x%04X -> Low: 0x%02X, High: 0x%02X\n", i, defaults[i],
                          cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)],
                          cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1]);
        }
    }

    if (ENABLE_DEBUG_LOGS)
    {
        Serial.println("=== READING BACK PRAM ===");
        for (int i = 0; i < 8; i++)
        {
            uint16_t low_byte = cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)];
            uint16_t high_byte = cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1];
            uint16_t color = low_byte | (high_byte << 8);
            Serial.printf("Index %d: Read 0x%04X (Low: 0x%02X, High: 0x%02X)\n", i, color, low_byte, high_byte);
        }
    }

    // Initialize the rest of the palette with a grayscale gradient
    for (int i = 8; i < 256; i++)
    {
        uint16_t gray = tft.color565(i, i, i);
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)] = gray & 0xFF;
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1] = (gray >> 8) & 0xFF;
    }
}