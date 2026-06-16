#include <Arduino.h>
#include <SPI.h>
#include <TFT_eSPI.h>
#include <LittleFS.h>
#include "emu.h"
#include "definitions.h"
#include "hw_pins.h"

TFT_eSPI tft = TFT_eSPI();

uint16_t frame_buffer[SCREEN_WIDTH * SCREEN_HEIGHT];
uint8_t  prev_vram[VRAM_SIZE];  // Track previous frame to detect changes

// Development toggles.
constexpr bool ENABLE_FRAMEBUFFER_TEST = false;  // true = solid-color panel self-test (skips the emulator)
constexpr bool ENABLE_DEBUG_LOGS = false;

uint8_t input_state = 0;

// ---- ROM selector state ----
static constexpr int MAX_ROMS = 16;
static String g_rom_list[MAX_ROMS];
static int    g_rom_count   = 0;
static String g_pending_rom = "";

void get_input();
void init_default_pram();
void run_framebuffer_color_test();

static void build_rom_list()
{
    g_rom_count = 0;
    // LittleFS must be mounted before we can list it. setup() runs build_rom_list()
    // before load_rom_from_flash(), so mount here (LittleFS.begin() is idempotent).
    if (!LittleFS.begin(true))
    {
        Serial.println("Error: LittleFS mount failed in build_rom_list!");
        return;
    }
    File root = LittleFS.open("/");
    if (!root) return;
    File f = root.openNextFile();
    while (f && g_rom_count < MAX_ROMS)
    {
        // f.name() may or may not carry a leading '/' depending on the core version;
        // normalize to exactly one so both listing and loading use a valid path.
        String name = f.name();
        if (!name.startsWith("/")) name = String("/") + name;
        if (name.endsWith(".rom"))
            g_rom_list[g_rom_count++] = name;
        f.close();
        f = root.openNextFile();
    }
    root.close();
}

static int write_guest_str(uint16_t dest, const String &s)
{
    int n = s.length();
    for (int i = 0; i < n; ++i)
        cpu_instance.memory[dest + i] = (uint8_t)s[i];
    cpu_instance.memory[dest + n] = 0;
    return n;
}

static void handle_syscall(uint16_t num)
{
    uint16_t r1 = cpu_instance.registers[1].word;
    uint16_t r2 = cpu_instance.registers[2].word;
    switch (num)
    {
    case 1: // LIST_ROMS: R1=dest, R2=max -> R0=count; writes len-prefixed names at dest
    {
        int max_roms = r2 ? (int)r2 : MAX_ROMS;
        int count = 0;
        uint16_t cursor = r1;
        for (int i = 0; i < g_rom_count && count < max_roms; ++i, ++count)
        {
            int len = write_guest_str(cursor + 1, g_rom_list[i]);
            cpu_instance.memory[cursor] = (uint8_t)len;
            cursor += 1 + len + 1;
        }
        cpu_instance.registers[0].word = (uint16_t)count;
        break;
    }
    case 2: // GET_ROM_NAME: R1=index, R2=dest -> R0=length
    {
        if (r1 < (uint16_t)g_rom_count)
        {
            int len = write_guest_str(r2, g_rom_list[r1]);
            cpu_instance.registers[0].word = (uint16_t)len;
        }
        else
        {
            cpu_instance.registers[0].word = 0;
        }
        break;
    }
    case 3: // LOAD_ROM: R1=index -> halt CPU, set pending
    {
        if (r1 < (uint16_t)g_rom_count)
            g_pending_rom = g_rom_list[r1];
        cpu_instance.running = false;
        break;
    }
    case 4: // RESET -> reboot to menu
    {
        g_pending_rom = "/menu.rom";
        cpu_instance.running = false;
        break;
    }
    case SYSCALL_TIME: // TIME: R0 = milliseconds since boot (low 16 bits)
        cpu_instance.registers[0].word = (uint16_t)(millis() & 0xFFFF);
        break;
    default:
        break;
    }
}

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

    if (file_size > static_cast<size_t>(STACK_START_ADDRESS - 1))
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
    // Font is not host-loaded — it ships inside each ROM (io.lib's font8x8 array) and is written by
    // load_rom_from_flash below. PRAM/VRAM/INPUT live above any ROM image, so they stay host-set.
    build_rom_list();
    if (ENABLE_DEBUG_LOGS)
        Serial.printf("build_rom_list: %d ROM(s) found in LittleFS\n", g_rom_count);
    emu_set_syscall_handler(handle_syscall);
    load_rom_from_flash("/menu.rom");

    for (int i = 0; i < VRAM_SIZE; i++)
    {
        cpu_instance.memory[VRAM_START_ADDRESS + i] = 0;
    }

    // Initialize prev_vram to all zeros so first frame always draws
    memset(prev_vram, 0, VRAM_SIZE);
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

    if (!g_pending_rom.isEmpty())
    {
        initialize_cpu();
        init_default_pram();
        // Font ships inside the ROM (io.lib font8x8); load_rom_from_flash below brings it in.
        for (int i = 0; i < VRAM_SIZE; i++)
            cpu_instance.memory[VRAM_START_ADDRESS + i] = 0;
        load_rom_from_flash(g_pending_rom.c_str());
        memset(prev_vram, 0, VRAM_SIZE);  // Reset display tracking when loading new ROM
        g_pending_rom = "";
        return;
    }

    uint16_t current_palette[256];
    for (int i = 0; i < 256; i++)
    {
        uint16_t low_byte  = cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)];
        uint16_t high_byte = cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1];
        current_palette[i] = low_byte | (high_byte << 8);
    }

    // Check if VRAM has changed
    bool vram_changed = memcmp(cpu_instance.memory + VRAM_START_ADDRESS, prev_vram, VRAM_SIZE) != 0;

    if (vram_changed)
    {
        // Update frame buffer only if VRAM changed
        for (int i = 0; i < VRAM_SIZE; ++i)
        {
            uint8_t color_index = cpu_instance.memory[VRAM_START_ADDRESS + i];
            frame_buffer[i] = current_palette[color_index];
        }

        // Push image to display
        tft.pushImage(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT, frame_buffer);

        // Update prev_vram for next comparison
        memcpy(prev_vram, cpu_instance.memory + VRAM_START_ADDRESS, VRAM_SIZE);
    }

    int32_t frame_duration = millis() - frame_start_time;
    if (frame_duration < 16)
    {
        delay(16 - frame_duration);
    }
}

void run_framebuffer_color_test()
{
    // Panel self-test: fill the screen solid green, bypassing the emulator and ROM. If this
    // shows but a ROM stays black, the fault is in the emulator/ROM path, not the display.
    for (int i = 0; i < (SCREEN_WIDTH * SCREEN_HEIGHT); ++i)
        frame_buffer[i] = TFT_GREEN;
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
