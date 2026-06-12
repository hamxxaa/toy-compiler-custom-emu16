#include <Arduino.h>
#include <SPI.h>
#include <TFT_eSPI.h>
#include <LittleFS.h>
#include "emu.h"
#include "definitions.h"
#include "hw_pins.h"

TFT_eSPI tft = TFT_eSPI();

uint16_t frame_buffer[SCREEN_WIDTH * SCREEN_HEIGHT];

// Development toggles.
constexpr bool ENABLE_FRAMEBUFFER_TEST = false;
constexpr bool ENABLE_DEBUG_LOGS = false;

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

    const int buttons[] = {LEFT_ARROW_PIN, UP_ARROW_PIN, RIGHT_ARROW_PIN, DOWN_ARROW_PIN,
                            BUTTON_A_PIN, BUTTON_B_PIN, BUTTON_X_PIN, BUTTON_Y_PIN};
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
    load_rom_from_flash("/game.rom");

    for (int i = 0; i < VRAM_SIZE; i++)
    {
        cpu_instance.memory[VRAM_START_ADDRESS + i] = 0;
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

    uint32_t frame_start_time = millis();

    get_input();
    cpu_instance.memory[INPUT_ADDRESS] = input_state;

    run_frame_instructions();

    uint16_t current_palette[256];
    for (int i = 0; i < 256; i++)
    {
        uint16_t low_byte  = cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)];
        uint16_t high_byte = cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1];
        current_palette[i] = low_byte | (high_byte << 8);
    }

    for (int i = 0; i < VRAM_SIZE; ++i)
    {
        uint8_t color_index = cpu_instance.memory[VRAM_START_ADDRESS + i];
        frame_buffer[i] = current_palette[color_index];
    }

    tft.pushImageDMA(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT, frame_buffer);

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
    if (digitalRead(LEFT_ARROW_PIN)  == LOW) input_state |= 0x01;
    if (digitalRead(UP_ARROW_PIN)    == LOW) input_state |= 0x02;
    if (digitalRead(RIGHT_ARROW_PIN) == LOW) input_state |= 0x04;
    if (digitalRead(DOWN_ARROW_PIN)  == LOW) input_state |= 0x08;
    if (digitalRead(BUTTON_A_PIN)    == LOW) input_state |= 0x10;
    if (digitalRead(BUTTON_B_PIN)    == LOW) input_state |= 0x20;
    if (digitalRead(BUTTON_X_PIN)    == LOW) input_state |= 0x40;
    if (digitalRead(BUTTON_Y_PIN)    == LOW) input_state |= 0x80;
}

void init_default_pram()
{
    uint16_t defaults[8] = {
        TFT_BLACK, TFT_WHITE, TFT_RED, TFT_BLUE,
        TFT_GREEN, TFT_MAGENTA, TFT_CYAN, TFT_YELLOW
    };
    for (int i = 0; i < 8; i++)
    {
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)]     = defaults[i] & 0xFF;
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1] = (defaults[i] >> 8) & 0xFF;
    }
    // Fill the rest of the palette with a grayscale gradient.
    for (int i = 8; i < 256; i++)
    {
        uint16_t gray = tft.color565(i, i, i);
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)]     = gray & 0xFF;
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1] = (gray >> 8) & 0xFF;
    }
}
