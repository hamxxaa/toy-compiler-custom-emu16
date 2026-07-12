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
#include <cstring>

#include "emu.h"
#include "definitions.h"
#include "ppu.h"

namespace fs = std::filesystem;

static uint8_t expand_5_to_8(uint8_t value)
{
    return static_cast<uint8_t>((value << 3) | (value >> 2));
}

static uint8_t expand_6_to_8(uint8_t value)
{
    return static_cast<uint8_t>((value << 2) | (value >> 4));
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

// Convert the PPU's composed indexed framebuffer through its palette. Every ROM drives the PPU
// now (the legacy VRAM+PRAM path was reclaimed), so this is the only frame source.
static void present_frame(std::vector<uint16_t> &framebuffer)
{
    ppu_convert_rgb565(framebuffer.data());
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

// Basename (no extension) of the loaded ROM + the dir it came from. Used to namespace saves
// (saves/<rom>.<slot>) and to find the asset pack (<rom-dir>/<rom>.pak), mirroring the firmware's
// per-ROM LittleFS files.
static std::string g_pc_current_rom;
static fs::path    g_pc_rom_dir;

static std::string pc_save_path(uint16_t slot)
{
    return "saves/" + g_pc_current_rom + "." + std::to_string(slot);
}

// ---- asset pack (Piece B): lazily load <rom-dir>/<rom>.pak and cache its TOC ----
static const int HEADER_LEN = 8;   // EPAK header size, mirrors tools/pack_assets.py
struct PcPakEntry { uint8_t type, w, h; uint32_t offset, length; };
static std::vector<uint8_t> g_pc_pak;
static std::vector<PcPakEntry> g_pc_toc;
static bool g_pc_pak_loaded = false;

static void pc_load_pak_if_needed()
{
    if (g_pc_pak_loaded) return;
    g_pc_pak_loaded = true;
    fs::path pak = g_pc_rom_dir / (g_pc_current_rom + ".pak");
    if (!load_file(pak, g_pc_pak)) return;                  // no pack -> TOC stays empty
    if (g_pc_pak.size() < HEADER_LEN || std::memcmp(g_pc_pak.data(), "EPAK", 4) != 0)
    {
        g_pc_pak.clear();
        return;
    }
    uint16_t count = static_cast<uint16_t>(g_pc_pak[6] | (g_pc_pak[7] << 8));
    if (g_pc_pak.size() < static_cast<size_t>(HEADER_LEN + 12 * count))
    {
        g_pc_pak.clear();
        return;
    }
    for (uint16_t i = 0; i < count; ++i)
    {
        const uint8_t *e = &g_pc_pak[HEADER_LEN + 12 * i];
        PcPakEntry pe;
        pe.type   = e[0];
        pe.w      = e[1];
        pe.h      = e[2];
        pe.offset = static_cast<uint32_t>(e[4]) | (e[5] << 8) | (e[6] << 16) | (static_cast<uint32_t>(e[7]) << 24);
        pe.length = static_cast<uint32_t>(e[8]) | (e[9] << 8) | (e[10] << 16) | (static_cast<uint32_t>(e[11]) << 24);
        g_pc_toc.push_back(pe);
    }
}

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
    uint16_t r3 = cpu_instance.registers[3].word;
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
    case SYSCALL_SAVE: // 7: R1=src R2=len R3=slot -> R0 = bytes written (0 = fail)
    {
        uint32_t len = std::min<uint32_t>(r2, 65536u - r1);
        fs::create_directories("saves");
        std::ofstream f(pc_save_path(r3), std::ios::binary | std::ios::trunc);
        if (f)
            f.write(reinterpret_cast<const char *>(&cpu_instance.memory[r1]), len);
        cpu_instance.registers[0].word = f ? static_cast<uint16_t>(len) : 0;
        break;
    }
    case SYSCALL_LOAD: // 8: R1=dest R2=maxlen R3=slot -> R0 = bytes read (0 = none)
    {
        uint32_t maxlen = std::min<uint32_t>(r2, 65536u - r1);
        std::ifstream f(pc_save_path(r3), std::ios::binary);
        if (f)
            f.read(reinterpret_cast<char *>(&cpu_instance.memory[r1]), maxlen);
        cpu_instance.registers[0].word = f ? static_cast<uint16_t>(f.gcount()) : 0;
        break;
    }
    case SYSCALL_SAVE_EXISTS: // 9: R1=slot -> R0 = 1/0
        cpu_instance.registers[0].word = fs::exists(pc_save_path(r1)) ? 1 : 0;
        break;
    case SYSCALL_ASSET_INFO: // 10: R1=id R2=dest -> R0=length; writes 6-byte header at dest
    {
        pc_load_pak_if_needed();
        if (r1 >= g_pc_toc.size()) { cpu_instance.registers[0].word = 0; break; }
        const PcPakEntry &e = g_pc_toc[r1];
        cpu_instance.memory[r2 + 0] = e.type;
        cpu_instance.memory[r2 + 1] = e.w;
        cpu_instance.memory[r2 + 2] = e.h;
        cpu_instance.memory[r2 + 3] = 0;
        cpu_instance.memory[r2 + 4] = static_cast<uint8_t>(e.length & 0xFF);
        cpu_instance.memory[r2 + 5] = static_cast<uint8_t>((e.length >> 8) & 0xFF);
        cpu_instance.registers[0].word = static_cast<uint16_t>(e.length);
        break;
    }
    case SYSCALL_ASSET_LOAD: // 11: R1=id R2=dest R3=maxlen -> R0 = bytes copied (0 = fail/too big)
    {
        pc_load_pak_if_needed();
        if (r1 >= g_pc_toc.size()) { cpu_instance.registers[0].word = 0; break; }
        const PcPakEntry &e = g_pc_toc[r1];
        if (e.length > r3 || static_cast<uint32_t>(r2) + e.length > 65536u ||
            e.offset + e.length > g_pc_pak.size())
        {
            cpu_instance.registers[0].word = 0;
            break;
        }
        std::memcpy(&cpu_instance.memory[r2], &g_pc_pak[e.offset], e.length);
        cpu_instance.registers[0].word = static_cast<uint16_t>(e.length);
        break;
    }
    case SYSCALL_SET_FPS: // 12: no-op on the one-shot PC runner (it runs N frames, no real-time pacing)
        break;
    case SYSCALL_PPU_SUBMIT: // 13: R1=buf R2=len -> feed a PPU command stream; PRESENT yields the frame
    {
        uint32_t len = r2;
        if (static_cast<uint32_t>(r1) + len > 65536u) len = 65536u - r1;
        if (ppu_receive(&cpu_instance.memory[r1], static_cast<int>(len)))
            emu_request_present();
        break;
    }
    case SYSCALL_PPU_DMA: // 14: R1=pak_id R2=ppu_addr -> R0 = bytes streamed pak->PPU RAM
    {
        pc_load_pak_if_needed();
        if (r1 >= g_pc_toc.size()) { cpu_instance.registers[0].word = 0; break; }
        const PcPakEntry &e = g_pc_toc[r1];
        if (e.offset + e.length > g_pc_pak.size()) { cpu_instance.registers[0].word = 0; break; }
        cpu_instance.registers[0].word =
            static_cast<uint16_t>(ppu_write(r2, &g_pc_pak[e.offset], e.length));
        break;
    }
    case SYSCALL_PPU_UPLOAD: // 15: R1=ppu_addr R2=cpu_src R3=len -> R0 = bytes copied CPU->PPU RAM
    {
        uint32_t len = std::min<uint32_t>(r3, 65536u - r2);
        cpu_instance.registers[0].word =
            static_cast<uint16_t>(ppu_write(r1, &cpu_instance.memory[r2], len));
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
    int hold_input = -1;   // --hold-input N: force the INPUT byte to N every frame (headless input sim)

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
        else if (argument == "--hold-input" && i + 1 < argc)
        {
            hold_input = std::stoi(argv[++i], nullptr, 0);   // e.g. 4 = RIGHT held (camera scrolls)
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

    // Per-ROM namespacing for saves + asset pack (mirrors firmware's LittleFS files).
    g_pc_current_rom = rom_path.stem().string();
    g_pc_rom_dir = rom_path.has_parent_path() ? rom_path.parent_path() : fs::path(".");

    if (use_menu_handler)
        build_pc_rom_list(fs::path("build") / "roms");
    // Register the handler unconditionally so save/load/asset syscalls work in headless tests;
    // the ROM-list cases (1-3) simply return empty when --menu wasn't passed.
    emu_set_syscall_handler(pc_syscall_handler);

    for (std::size_t i = 0; i < rom_bytes.size(); ++i)
    {
        cpu_instance.memory[i] = rom_bytes[i];
    }

    // Font is no longer host-loaded: it lives in the ROM's DATA (io.lib's font8x8 array), so it
    // arrived with the ROM copy above. INPUT is above any ROM image, so it stays host-set.
    cpu_instance.memory[INPUT_ADDRESS] = 0;

    std::vector<uint16_t> framebuffer(static_cast<std::size_t>(SCREEN_WIDTH * SCREEN_HEIGHT));
    int completed_frames = 0;
    int instruction_batches = 0;

    while (completed_frames < frame_limit)
    {
        if (hold_input >= 0)
            cpu_instance.memory[INPUT_ADDRESS] = static_cast<uint8_t>(hold_input);   // simulate a held button
        run_frame_instructions();
        ++instruction_batches;
        present_frame(framebuffer);
        ++completed_frames;
        if (!cpu_instance.running && !emu_present_pending())
            break;   // genuine HLT, not a frame yield
    }

    present_frame(framebuffer);

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