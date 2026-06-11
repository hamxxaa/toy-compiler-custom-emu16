#include <cstdint>
#include <algorithm>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>
#include <filesystem>

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

static void print_usage(const char *program_name)
{
    std::cerr << "Usage: " << program_name << " --rom <path> [--output-dir <dir>] [--frames <count>]\n";
}

int main(int argc, char **argv)
{
    fs::path rom_path;
    fs::path output_dir = fs::path("build") / "pc_emulator";
    int frame_limit = 1;

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

    for (std::size_t i = 0; i < rom_bytes.size(); ++i)
    {
        cpu_instance.memory[i] = rom_bytes[i];
    }

    init_default_pram();
    for (int i = 0; i < VRAM_SIZE; ++i)
    {
        cpu_instance.memory[VRAM_START_ADDRESS + i] = 0;
    }
    cpu_instance.memory[INPUT_ADDRESS] = 0;

    std::vector<uint16_t> framebuffer(static_cast<std::size_t>(SCREEN_WIDTH * SCREEN_HEIGHT));
    int completed_frames = 0;
    int instruction_batches = 0;

    while (cpu_instance.running && completed_frames < frame_limit)
    {
        run_20k_instruction();
        ++instruction_batches;
        build_framebuffer(framebuffer);
        ++completed_frames;
    }

    build_framebuffer(framebuffer);

    fs::create_directories(output_dir);
    fs::path ppm_path = output_dir / "frame.ppm";
    if (!save_ppm(ppm_path, framebuffer))
    {
        std::cerr << "Failed to write framebuffer image: " << ppm_path.string() << "\n";
        return 1;
    }

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
              << "checksum=" << hex64(checksum) << ' '
              << "frame=" << ppm_path.string() << '\n';

    return 0;
}