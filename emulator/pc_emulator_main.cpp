#include <cstdint>
#include <algorithm>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>
#include <filesystem>
#include <chrono>

#include "emu.h"
#include "definitions.h"

namespace fs = std::filesystem;

static uint16_t rgb565_from_rgb(uint8_t red, uint8_t green, uint8_t blue)
{
    return static_cast<uint16_t>(((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3));
}

static uint8_t expand_5_to_8(uint8_t value)
{
    return static_cast<uint8_t>((value << 3) | (value >> 2));
}

static uint8_t expand_6_to_8(uint8_t value)
{
    return static_cast<uint8_t>((value << 2) | (value >> 4));
}

static void init_default_pram()
{
    const uint16_t defaults[8] = {
        0x0000,
        0xFFFF,
        0xF800,
        0x001F,
        0x07E0,
        0xF81F,
        0x07FF,
        0xFFE0
    };

    for (int i = 0; i < 8; ++i)
    {
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)] = static_cast<uint8_t>(defaults[i] & 0xFF);
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1] = static_cast<uint8_t>((defaults[i] >> 8) & 0xFF);
    }

    for (int i = 8; i < 256; ++i)
    {
        uint16_t gray = rgb565_from_rgb(static_cast<uint8_t>(i), static_cast<uint8_t>(i), static_cast<uint8_t>(i));
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)] = static_cast<uint8_t>(gray & 0xFF);
        cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1] = static_cast<uint8_t>((gray >> 8) & 0xFF);
    }
}

static bool load_file(const fs::path &path, std::vector<uint8_t> &bytes)
{
    std::ifstream file(path, std::ios::binary);
    if (!file)
    {
        return false;
    }

    file.seekg(0, std::ios::end);
    std::streamsize size = file.tellg();
    file.seekg(0, std::ios::beg);

    if (size < 0)
    {
        return false;
    }

    bytes.resize(static_cast<std::size_t>(size));
    if (size > 0 && !file.read(reinterpret_cast<char *>(bytes.data()), size))
    {
        return false;
    }

    return true;
}

static bool save_ppm(const fs::path &path, const std::vector<uint16_t> &framebuffer)
{
    std::ofstream file(path, std::ios::binary);
    if (!file)
    {
        return false;
    }

    file << "P6\n" << SCREEN_WIDTH << " " << SCREEN_HEIGHT << "\n255\n";
    for (uint16_t pixel : framebuffer)
    {
        uint8_t red = expand_5_to_8(static_cast<uint8_t>((pixel >> 11) & 0x1F));
        uint8_t green = expand_6_to_8(static_cast<uint8_t>((pixel >> 5) & 0x3F));
        uint8_t blue = expand_5_to_8(static_cast<uint8_t>(pixel & 0x1F));
        file.put(static_cast<char>(red));
        file.put(static_cast<char>(green));
        file.put(static_cast<char>(blue));
    }

    return static_cast<bool>(file);
}

static void build_framebuffer(std::vector<uint16_t> &framebuffer)
{
    uint16_t palette[256];
    for (int i = 0; i < 256; ++i)
    {
        uint16_t low_byte = cpu_instance.memory[PRAM_START_ADDRESS + (i * 2)];
        uint16_t high_byte = cpu_instance.memory[PRAM_START_ADDRESS + (i * 2) + 1];
        palette[i] = static_cast<uint16_t>(low_byte | (high_byte << 8));
    }

    for (int i = 0; i < VRAM_SIZE; ++i)
    {
        uint8_t color_index = cpu_instance.memory[VRAM_START_ADDRESS + i];
        framebuffer[static_cast<std::size_t>(i)] = palette[color_index];
    }
}

static uint64_t fnv1a64(const std::vector<uint16_t> &framebuffer)
{
    uint64_t hash = 1469598103934665603ULL;
    for (uint16_t pixel : framebuffer)
    {
        hash ^= static_cast<uint64_t>(pixel & 0xFF);
        hash *= 1099511628211ULL;
        hash ^= static_cast<uint64_t>((pixel >> 8) & 0xFF);
        hash *= 1099511628211ULL;
    }

    return hash;
}

static std::string hex16(uint16_t value)
{
    std::ostringstream stream;
    stream << "0x" << std::uppercase << std::hex << std::setw(4) << std::setfill('0') << value;
    return stream.str();
}

static std::string hex64(uint64_t value)
{
    std::ostringstream stream;
    stream << "0x" << std::uppercase << std::hex << std::setw(16) << std::setfill('0') << value;
    return stream.str();
}

// ---- PC dev syscall handler (active when --menu is passed) ----
static std::vector<fs::path> g_pc_rom_list;
static std::string g_pc_pending_rom;
static const auto g_pc_start_time = std::chrono::steady_clock::now();

static void build_pc_rom_list(const fs::path &rom_dir)
{
    g_pc_rom_list.clear();
    if (!fs::exists(rom_dir)) return;
    for (const auto &entry : fs::directory_iterator(rom_dir))
    {
        if (entry.path().extension() == ".rom")
            g_pc_rom_list.push_back(entry.path());
    }
    std::sort(g_pc_rom_list.begin(), g_pc_rom_list.end());
}

static void pc_syscall_handler(uint16_t num)
{
    uint16_t r1 = cpu_instance.registers[1].word;
    uint16_t r2 = cpu_instance.registers[2].word;
    switch (num)
    {
    case 1: // LIST_ROMS: R1=dest, R2=max -> R0=count; writes len-prefixed names
    {
        int max_roms = r2 ? static_cast<int>(r2) : static_cast<int>(g_pc_rom_list.size());
        int count = 0;
        uint16_t cursor = r1;
        for (const auto &p : g_pc_rom_list)
        {
            if (count >= max_roms) break;
            std::string name = p.filename().string();
            uint8_t len = static_cast<uint8_t>(std::min(static_cast<int>(name.size()), 255));
            cpu_instance.memory[cursor] = len;
            for (int i = 0; i < len; ++i)
                cpu_instance.memory[cursor + 1 + i] = static_cast<uint8_t>(name[i]);
            cpu_instance.memory[cursor + 1 + len] = 0;
            cursor += static_cast<uint16_t>(1 + len + 1);
            ++count;
        }
        cpu_instance.registers[0].word = static_cast<uint16_t>(count);
        break;
    }
    case 2: // GET_ROM_NAME: R1=index, R2=dest -> R0=length
    {
        if (r1 < static_cast<uint16_t>(g_pc_rom_list.size()))
        {
            std::string name = g_pc_rom_list[r1].filename().string();
            uint8_t len = static_cast<uint8_t>(std::min(static_cast<int>(name.size()), 255));
            for (int i = 0; i < len; ++i)
                cpu_instance.memory[r2 + i] = static_cast<uint8_t>(name[i]);
            cpu_instance.memory[r2 + len] = 0;
            cpu_instance.registers[0].word = len;
        }
        else
        {
            cpu_instance.registers[0].word = 0;
        }
        break;
    }
    case 3: // LOAD_ROM: R1=index -> halt CPU, record pending
    {
        if (r1 < static_cast<uint16_t>(g_pc_rom_list.size()))
            g_pc_pending_rom = g_pc_rom_list[r1].string();
        cpu_instance.running = false;
        break;
    }
    case 4: // RESET -> halt CPU
    {
        g_pc_pending_rom = "reset";
        cpu_instance.running = false;
        break;
    }
    case SYSCALL_TIME: // TIME: R0 = milliseconds since pc_emu start (low 16 bits)
    {
        auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                      std::chrono::steady_clock::now() - g_pc_start_time).count();
        cpu_instance.registers[0].word = static_cast<uint16_t>(ms & 0xFFFF);
        break;
    }
    case 0x7F: // echo (test): R0 = R1 + R2 -- regression-tests the interrupt path
    {
        cpu_instance.registers[0].word = static_cast<uint16_t>(r1 + r2);
        break;
    }
    default:
        break;
    }
}

static void print_usage(const char *program_name)
{
    std::cerr << "Usage: " << program_name << " --rom <path> [--output-dir <dir>] [--frames <count>] [--menu]\n";
}

int main(int argc, char **argv)
{
    fs::path rom_path;
    fs::path output_dir = fs::path("build") / "pc_emulator";
    int frame_limit = 1;
    bool use_menu_handler = false;

    for (int i = 1; i < argc; ++i)
    {
        std::string argument = argv[i];
        if (argument == "--rom" && i + 1 < argc)
        {
            rom_path = argv[++i];
        }
        else if (argument == "--output-dir" && i + 1 < argc)
        {
            output_dir = argv[++i];
        }
        else if (argument == "--frames" && i + 1 < argc)
        {
            frame_limit = std::max(1, std::stoi(argv[++i]));
        }
        else if (argument == "--menu")
        {
            use_menu_handler = true;
        }
        else if (argument == "--help" || argument == "-h")
        {
            print_usage(argv[0]);
            return 0;
        }
        else if (rom_path.empty())
        {
            rom_path = argument;
        }
        else
        {
            std::cerr << "Unknown argument: " << argument << "\n";
            print_usage(argv[0]);
            return 1;
        }
    }

    if (rom_path.empty())
    {
        print_usage(argv[0]);
        return 1;
    }

    std::vector<uint8_t> rom_bytes;
    if (!load_file(rom_path, rom_bytes))
    {
        std::cerr << "Failed to read ROM: " << rom_path.string() << "\n";
        return 1;
    }

    if (rom_bytes.size() > static_cast<std::size_t>(STACK_START_ADDRESS - 1))
    {
        std::cerr << "ROM is too large for memory layout: " << rom_bytes.size() << " bytes\n";
        return 1;
    }

    initialize_cpu();

    if (use_menu_handler)
    {
        build_pc_rom_list(fs::path("build") / "roms");
        emu_set_syscall_handler(pc_syscall_handler);
    }

    for (std::size_t i = 0; i < rom_bytes.size(); ++i)
    {
        cpu_instance.memory[i] = rom_bytes[i];
    }

    init_default_pram();
    // Font is no longer host-loaded: it lives in the ROM's DATA (io.lib's font8x8 array), so it
    // arrived with the ROM copy above. PRAM/VRAM/INPUT are above any ROM image, so they stay host-set.
    for (int i = 0; i < VRAM_SIZE; ++i)
    {
        cpu_instance.memory[VRAM_START_ADDRESS + i] = 0;
    }
    cpu_instance.memory[INPUT_ADDRESS] = 0;

    std::vector<uint16_t> framebuffer(static_cast<std::size_t>(SCREEN_WIDTH * SCREEN_HEIGHT));
    int completed_frames = 0;
    int instruction_batches = 0;

    while (completed_frames < frame_limit)
    {
        run_frame_instructions();
        ++instruction_batches;
        build_framebuffer(framebuffer);
        ++completed_frames;
        if (!cpu_instance.running && !emu_present_pending())
            break;   // genuine HLT, not a frame yield
    }

    build_framebuffer(framebuffer);

    fs::create_directories(output_dir);
    fs::path ppm_path = output_dir / "frame.ppm";
    if (!save_ppm(ppm_path, framebuffer))
    {
        std::cerr << "Failed to write framebuffer image: " << ppm_path.string() << "\n";
        return 1;
    }

    if (!g_pc_pending_rom.empty())
        std::cout << "SYSCALL LOAD_ROM: " << g_pc_pending_rom << '\n';

    uint64_t checksum = fnv1a64(framebuffer);
    uint16_t return_value = cpu_instance.registers[0].word;

    // Print registers for debugging
    std::ostringstream regstream;
    regstream << "REGS ";
    for (int i = 0; i < 8; ++i)
    {
        regstream << "R" << i << "=" << hex16(cpu_instance.registers[i].word);
        if (i < 7) regstream << ' ';
    }
    std::cout << regstream.str() << std::endl;

    std::cout << "RESULT "
              << "halted=" << (cpu_instance.running ? 0 : 1) << ' '
              << "frames=" << completed_frames << ' '
              << "batches=" << instruction_batches << ' '
              << "return=" << return_value << ' '
              << "pc=" << hex16(cpu_instance.pc) << ' '
              << "flags=" << hex16(cpu_instance.flags) << ' '
              << "instr=" << emu_instruction_count() << ' '
              << "checksum=" << hex64(checksum) << ' '
              << "frame=" << ppm_path.string() << '\n';

    return 0;
}