// song_render — render a packed .song blob straight to a .wav, with NO ROM/compile step. This is the
// fast music-preview path: edit an .mml, compile it (tools/mml.py), and hear it, without going through
// the EMU16 compiler + a ROM + a pak. It links the REAL emulator/apu.cpp, so the preview is
// byte-identical to what the game will play, and it reimplements lib/music.lib's tiny sequencer here
// (kept in lockstep with music.lib's music_frame timing).
//
//   song_render <in.song> <out.wav> [--loops N] [--seconds S]
//
// Default: 2 loops of the song's order list. --seconds overrides with a fixed length.
#include "apu.h"
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

static uint8_t rd(const std::vector<uint8_t> &b, size_t i) { return i < b.size() ? b[i] : 0; }

static void put_u32(std::vector<uint8_t> &v, uint32_t x)
{
    v.push_back(x & 0xFF); v.push_back((x >> 8) & 0xFF);
    v.push_back((x >> 16) & 0xFF); v.push_back((x >> 24) & 0xFF);
}
static void put_u16(std::vector<uint8_t> &v, uint16_t x) { v.push_back(x & 0xFF); v.push_back((x >> 8) & 0xFF); }

static bool write_wav(const char *path, const std::vector<int16_t> &s, int rate)
{
    std::vector<uint8_t> h;
    uint32_t data_bytes = (uint32_t)s.size() * 2;
    for (char c : std::string("RIFF")) h.push_back(c);
    put_u32(h, 36 + data_bytes);
    for (char c : std::string("WAVE")) h.push_back(c);
    for (char c : std::string("fmt ")) h.push_back(c);
    put_u32(h, 16); put_u16(h, 1); put_u16(h, 1);
    put_u32(h, rate); put_u32(h, rate * 2); put_u16(h, 2); put_u16(h, 16);
    for (char c : std::string("data")) h.push_back(c);
    put_u32(h, data_bytes);
    FILE *f = fopen(path, "wb");
    if (!f) return false;
    fwrite(h.data(), 1, h.size(), f);
    if (!s.empty()) fwrite(s.data(), 2, s.size(), f);
    fclose(f);
    return true;
}

int main(int argc, char **argv)
{
    const char *inpath = nullptr, *outpath = nullptr;
    int loops = 2;
    double seconds = -1.0;
    for (int i = 1; i < argc; ++i)
    {
        std::string a = argv[i];
        if (a == "--loops" && i + 1 < argc) loops = std::stoi(argv[++i]);
        else if (a == "--seconds" && i + 1 < argc) seconds = std::stod(argv[++i]);
        else if (!inpath) inpath = argv[i];
        else if (!outpath) outpath = argv[i];
    }
    if (!inpath || !outpath)
    {
        std::fprintf(stderr, "usage: song_render <in.song> <out.wav> [--loops N] [--seconds S]\n");
        return 1;
    }

    FILE *f = fopen(inpath, "rb");
    if (!f) { std::fprintf(stderr, "cannot open %s\n", inpath); return 1; }
    std::vector<uint8_t> b;
    { int c; while ((c = fgetc(f)) != EOF) b.push_back((uint8_t)c); }
    fclose(f);
    if (b.size() < 9) { std::fprintf(stderr, "not a .song blob\n"); return 1; }

    int rows = b[2], order_len = b[4], order_loop = b[5], groove_len = b[6];
    int instr_len = b[7] | (b[8] << 8);
    int groove_off = 9;
    int order_off = groove_off + groove_len;
    int instr_off = order_off + order_len;
    int pat_off = instr_off + instr_len;

    apu_reset();
    apu_receive(&b[instr_off], instr_len);   // upload the song's instruments

    int rate = apu_rate();
    int base = rate / 60, rem = rate % 60, carry = 0;   // samples per frame (22050/60 = 367.5)
    int orderidx = 0, row = 0, tick = 0, gidx = 0, done_loops = 0;
    long max_frames = (seconds > 0) ? (long)(seconds * 60) : 3600;   // safety cap ~60s
    std::vector<int16_t> out;

    for (long frame = 0; frame < max_frames; ++frame)
    {
        if (tick == 0)   // play the current row (mirrors music.lib mus_play_row)
        {
            int p = rd(b, order_off + orderidx);
            int rowbase = pat_off + ((p * rows + row) * 4) * 2;
            uint8_t cmd[64]; int ci = 0;
            for (int c = 0; c < 4; ++c)
            {
                int note = rd(b, rowbase + c * 2), inst = rd(b, rowbase + c * 2 + 1);
                if (note >= 2) { cmd[ci++] = 0x11; cmd[ci++] = c; cmd[ci++] = note; cmd[ci++] = inst; }  // NOTE_ON_INST
                else if (note == 1) { cmd[ci++] = 0x02; cmd[ci++] = c; }                                  // NOTE_OFF
            }
            if (ci) apu_receive(cmd, ci);
        }
        tick++;
        int spd = rd(b, groove_off + gidx); if (spd < 1) spd = 1;
        if (tick >= spd)
        {
            tick = 0;
            row++;
            if (row >= rows)
            {
                row = 0; orderidx++;
                if (rd(b, order_off + orderidx) == 255) { orderidx = order_loop; done_loops++; }
            }
            gidx++; if (gidx >= groove_len) gidx = 0;
        }

        int n = base; carry += rem; if (carry >= 60) { n++; carry -= 60; }
        size_t old = out.size();
        out.resize(old + n);
        apu_render(&out[old], n);

        if (seconds <= 0 && done_loops >= loops) break;
    }

    if (!write_wav(outpath, out, rate)) { std::fprintf(stderr, "cannot write %s\n", outpath); return 1; }
    std::printf("rendered %s -> %s (%zu samples, %.2fs)\n", inpath, outpath, out.size(), (double)out.size() / rate);
    return 0;
}
