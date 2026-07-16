// EMU16 APU implementation — see apu.h for the unit's contract and invariants.
//
// The APU has its own voice state (NOT a flat addressable RAM like the PPU): the CPU never
// addresses APU state by offset, it only ever sends small symbolic commands (NOTE_ON/NOTE_OFF/
// SET_VOL/SET_DUTY) that apu_receive applies immediately to a private `Voice` struct array. This
// is why a struct is correct here even though the PPU deliberately avoided one (ppu.cpp) — the
// PPU is DMA'd into by flat address, the APU is only ever spoken to symbolically. If PCM/wavetable
// playback arrives later, THAT sample buffer would be a flat addressable region like PPU patterns;
// the control registers stay a struct regardless.
//
// Integer/fixed-point synthesis only, no float: apu_render must produce byte-identical output on
// every host (pc_emu vs WASM), the same guarantee verify_wasm.js already holds the PPU framebuffer
// to. Floating point could drift between platforms/compilers; fixed-point can't.
#include "apu.h"
#include <cstring>

// ── Voice state ────────────────────────────────────────────────────────────────────────────────
// 0,1 = pulse (square, 4 duty options), 2 = triangle, 3 = noise (15-bit LFSR). `phase` is a full
// 32-bit accumulator representing one complete cycle 0..2^32-1; the top 16 bits (phase>>16) give
// position 0..65535 within that cycle for the waveform shapers below. `inc` is how far `phase`
// advances per sample: one full cycle (a 2^32 wrap) must take exactly `APU_RATE/freq` samples, so
// `inc = freq * 2^32 / APU_RATE` (64-bit intermediate -- `freq << 32` overflows a 32-bit type).
// An ungated voice (gate=0) still advances its phase (so it resumes in a sane place on the next
// NOTE_ON) but contributes silence.
struct Voice
{
    uint32_t phase;    // full-cycle accumulator; phase>>16 gives 0..65535 position within the cycle
    uint32_t inc;      // phase advance per sample (recomputed each frame-tick for musical voices)
    uint16_t freq;     // Hz (raw NOTE_ON path only; NOTE_ON_INST drives pitch from a note number)
    uint16_t lfsr;     // noise only: 15-bit shift register state (never allowed to reach 0)
    uint8_t  vol;      // 0..255 amplitude (driven by the volume macro when inst >= 0)
    uint8_t  duty;     // 0..3 pulse width (driven by the duty macro when inst >= 0)
    uint8_t  gate;     // 1 = sounding, 0 = silent
    int16_t  inst;     // instrument index, or -1 = RAW (no macros, no pitch pipeline)
    uint8_t  note;     // base MIDI note from NOTE_ON_INST; arp/vibrato/slide bend the pitch around it
    uint16_t vol_cur;  // cursor into the volume macro
    uint16_t duty_cur; // cursor into the duty macro
    uint16_t arp_cur;  // cursor into the arpeggio macro
    // Pitch pipeline, 8.8 fixed-point semitones: base = pitch_fp (moves during a slide); the arp macro
    // adds discrete semitone offsets on top; vibrato adds a small oscillation; the sum -> inc via
    // interpolation of NOTE_INC. Layering everything through one fp pitch keeps the effects composable.
    int32_t  pitch_fp;        // current base pitch = semitone * 256
    int32_t  slide_target_fp; // portamento target
    uint16_t slide_rate;      // fp units moved toward the target per frame-tick (0 = no slide)
    uint16_t vib_phase;       // vibrato LFO phase (full cycle = 65536)
    uint16_t vib_timer;       // frame-ticks left before vibrato starts (delayed vibrato)
    uint8_t  noise_mode;      // noise voice: 0 = normal 15-bit hiss, 1 = short/periodic (metallic)
};

// An instrument is a bundle of per-frame MACRO tables (the FamiTracker/FamiStudio model) plus a few
// scalar effect params. A note picks an instrument; the APU steps its macros on a fixed 60 Hz
// "frame-tick" (see step_macros) and the synth reads the resulting per-voice registers each sample.
// A macro: `len` values (0 = unused), one consumed per tick; at the end it jumps to `loop`, or holds
// the last value if loop==255. vol levels 0..15 (scaled to the synth's 0..255); duty 0..3; arp values
// are SIGNED semitone offsets (read as int8). This is the whole "instrument" concept and every later
// timbre feature hangs off it.
#define APU_MAX_INSTRUMENTS 16
#define APU_MACRO_MAXLEN    32
#define APU_FPS             60   // macro step rate (a "music frame"), fixed like the NES 60 Hz NMI
struct Macro { uint8_t len; uint8_t loop; uint8_t data[APU_MACRO_MAXLEN]; };
struct Instrument
{
    Macro   vol;        // volume envelope (levels 0..15) -- the note's loudness shape
    Macro   duty;       // pulse width over time (0..3) -- the note's timbre shape (pulse only)
    Macro   arp;        // arpeggio: signed semitone offsets vs the base note (1-channel chords, blips)
    uint8_t vib_depth;  // vibrato depth in pitch-fp units (0 = no vibrato)
    uint8_t vib_speed;  // vibrato LFO speed (see step_macros: increment = vib_speed << 6 per tick)
    uint8_t vib_delay;  // frame-ticks to wait after note-on before vibrato begins (delayed vibrato)
    uint8_t noise_mode; // noise voice: 0 = normal hiss, 1 = short/periodic (metallic/tonal buzz)
};

struct APUState
{
    Voice       voices[APU_NUM_VOICES];
    Instrument  instruments[APU_MAX_INSTRUMENTS];
    uint32_t    tick_accum;             // += APU_FPS per sample; on >= APU_RATE, one frame-tick fires
    uint32_t    tick_count;             // free-running count of those frame-ticks -- see apu_ticks()
    uint8_t     master_vol;             // overall output gain: 128 = unity, 64 = half, 0 = mute, 255 ~= 2x
};
static APUState apu_instance;

static uint8_t s_inbuf[2048];  // inbound command buffer. Per-frame submits are tiny, but loading a
                               // whole song's instrument-definition stream in one sys_apu_submit
                               // (see lib/music.lib music_load_song) can be up to ~1 KB, so this
                               // matches the PPU's 2 KB rather than the old 256.

// note-number -> phase-increment: NOTE_INC[n] = round(440 * 2^((n-69)/12) * 2^32 / APU_RATE).
// MIDI note numbers (60 = middle C4, 69 = A4 = 440 Hz). BAKED (not pow()'d at init) so it is
// byte-identical on every host -- same cross-check discipline as the rest of the APU.
static const uint32_t NOTE_INC[128] = {
    1592507u, 1687203u, 1787529u, 1893821u, 2006434u, 2125742u, 2252146u, 2386065u,
    2527948u, 2678268u, 2837526u, 3006254u, 3185015u, 3374406u, 3575058u, 3787642u,
    4012867u, 4251485u, 4504291u, 4772130u, 5055896u, 5356535u, 5675051u, 6012507u,
    6370030u, 6748811u, 7150117u, 7575285u, 8025735u, 8502970u, 9008582u, 9544261u,
    10111792u, 10713070u, 11350103u, 12025015u, 12740059u, 13497623u, 14300233u, 15150569u,
    16051469u, 17005939u, 18017165u, 19088521u, 20223584u, 21426141u, 22700205u, 24050030u,
    25480119u, 26995246u, 28600467u, 30301139u, 32102938u, 34011878u, 36034330u, 38177043u,
    40447168u, 42852281u, 45400411u, 48100060u, 50960238u, 53990491u, 57200933u, 60602278u,
    64205876u, 68023757u, 72068660u, 76354085u, 80894335u, 85704563u, 90800821u, 96200119u,
    101920476u, 107980983u, 114401866u, 121204555u, 128411753u, 136047513u, 144137319u, 152708170u,
    161788671u, 171409126u, 181601643u, 192400238u, 203840952u, 215961966u, 228803732u, 242409110u,
    256823506u, 272095026u, 288274639u, 305416341u, 323577341u, 342818251u, 363203285u, 384800477u,
    407681904u, 431923931u, 457607465u, 484818220u, 513647012u, 544190053u, 576549277u, 610832681u,
    647154683u, 685636503u, 726406571u, 769600953u, 815363807u, 863847862u, 915214929u, 969636441u,
    1027294024u, 1088380105u, 1153098554u, 1221665363u, 1294309365u, 1371273005u, 1452813141u, 1539201906u,
    1630727614u, 1727695724u, 1830429858u, 1939272882u, 2054588048u, 2176760211u, 2306197109u, 2443330725u,
};

// A 32-step sine, -127..127, for vibrato's pitch wobble (baked for cross-host determinism).
static const int8_t SINE32[32] = {
    0, 25, 49, 71, 90, 106, 117, 125, 127, 125, 117, 106, 90, 71, 49, 25,
    0, -25, -49, -71, -90, -106, -117, -125, -127, -125, -117, -106, -90, -71, -49, -25,
};

// pitch_fp (8.8 fixed-point semitones) -> phase increment, by linear-interpolating NOTE_INC between
// adjacent semitones. Linear interp is a fine approximation for the small fractional bends vibrato
// and slides produce; a plain integer note (frac 0) returns NOTE_INC[note] exactly, so instrument
// notes with no pitch effects stay bit-identical to Phase A.
static inline uint32_t pitch_to_inc(int32_t pitch_fp)
{
    int n = pitch_fp >> 8;
    if (n <= 0)   return NOTE_INC[0];
    if (n >= 127) return NOTE_INC[127];
    uint32_t a = NOTE_INC[n], b = NOTE_INC[n + 1];
    uint32_t frac = (uint32_t)(pitch_fp & 255);
    return a + (uint32_t)(((uint64_t)(b - a) * frac) >> 8);
}

// Master mix scale: up to APU_NUM_VOICES(4) voices each contributing up to +-255 sums to +-1020;
// <<5 (*32) maps that to +-32640, comfortably inside int16 range with headroom before the clamp
// below ever engages. A single tunable knob if the mix ever needs to get louder/quieter/limited.
#define APU_MASTER_SHIFT 5

void apu_reset()
{
    memset(&apu_instance, 0, sizeof(apu_instance));
    apu_instance.master_vol = 128;          // unity by default (128 reproduces the old fixed gain exactly)
    for (int v = 0; v < APU_NUM_VOICES; ++v)
    {
        apu_instance.voices[v].lfsr = 1;    // must start non-zero or the noise LFSR would stay stuck at 0
        apu_instance.voices[v].inst = -1;   // raw by default (no envelope) until a NOTE_ON_INST assigns one
    }
}

// Read a macro's current value and advance its cursor one step (looping at the end, or holding the
// last value if loop==255). The caller guards m.len > 0.
static inline uint8_t macro_step(const Macro &m, uint16_t &cur)
{
    uint8_t val = m.data[cur];
    uint16_t next = (uint16_t)(cur + 1);
    if (next >= m.len)
        next = (m.loop != 255) ? m.loop : (uint16_t)(m.len - 1);
    cur = next;
    return val;
}

// Frame-tick: advance every gated musical voice's macros + pitch pipeline one 60 Hz frame forward.
// Sample-counted from apu_render so it's identical no matter how the host chunks its render calls.
// Writes into voice.vol/duty/inc, the registers step_voice already reads -- so the synth core never
// had to change; the whole instrument system just drives its registers each frame.
static void step_macros()
{
    for (int i = 0; i < APU_NUM_VOICES; ++i)
    {
        Voice &v = apu_instance.voices[i];
        if (v.inst < 0 || !v.gate) continue;   // raw voice, or released -> macros idle
        Instrument &ins = apu_instance.instruments[v.inst];

        // Volume + duty macros drive the amplitude/timbre registers directly.
        if (ins.vol.len)  v.vol  = (uint8_t)(macro_step(ins.vol,  v.vol_cur) * 17);   // 0..15 -> 0..255
        if (ins.duty.len) v.duty = (uint8_t)(macro_step(ins.duty, v.duty_cur) & 3);

        // ── Pitch pipeline (see the Voice comment): slide -> arp -> vibrato -> inc ──────────────────
        if (v.slide_rate)   // portamento: creep the base pitch toward the target
        {
            if (v.pitch_fp < v.slide_target_fp)
            { v.pitch_fp += v.slide_rate; if (v.pitch_fp > v.slide_target_fp) v.pitch_fp = v.slide_target_fp; }
            else if (v.pitch_fp > v.slide_target_fp)
            { v.pitch_fp -= v.slide_rate; if (v.pitch_fp < v.slide_target_fp) v.pitch_fp = v.slide_target_fp; }
        }
        int32_t pfp = v.pitch_fp;
        if (ins.arp.len)    // arpeggio: add discrete signed semitone offsets (chords, blip attacks)
            pfp += (int32_t)((int8_t)macro_step(ins.arp, v.arp_cur)) << 8;
        if (ins.vib_depth && ins.vib_speed)   // vibrato: a small pitch oscillation (after any delay)
        {
            if (v.vib_timer > 0) v.vib_timer--;
            else
            {
                v.vib_phase = (uint16_t)(v.vib_phase + ((uint16_t)ins.vib_speed << 6));
                int s = SINE32[(v.vib_phase >> 11) & 31];       // -127..127
                pfp += (s * (int)ins.vib_depth) >> 7;           // +- ~vib_depth fp units
            }
        }
        v.inc = pitch_to_inc(pfp);
    }
}

// Advance one voice by one sample and return its signed contribution. Reads `gate`/`duty`/`vol`
// as of NOW (the last command applied wins immediately — no latching), matching the "commands
// apply on submit" invariant in apu.h.
static inline int16_t step_voice(Voice &v, int idx)
{
    int32_t out = 0;
    if (v.gate)
    {
        if (idx < 2)   // pulse (voices 0, 1)
        {
            uint32_t pos = v.phase >> 16;                    // 0..65535 within the cycle
            uint32_t thresh;
            switch (v.duty & 3)
            {
            case 0:  thresh = 65536u / 8;       break;        // 12.5%
            case 1:  thresh = 65536u / 4;       break;        // 25%
            case 2:  thresh = 65536u / 2;       break;        // 50%
            default: thresh = (65536u * 3) / 4; break;        // 75%
            }
            out = (pos < thresh) ? (int32_t)v.vol : -(int32_t)v.vol;
        }
        else if (idx == 2)   // triangle (voice 2) — linear ramp up then down, symmetric about 0
        {
            uint32_t pos = v.phase >> 16;
            int32_t tri = (pos < 32768u) ? ((int32_t)pos - 16384) : (49152 - (int32_t)pos);
            out = (tri * (int32_t)v.vol) >> 14;               // tri in +-16384 -> +-vol
        }
        else   // noise (voice 3) — current LFSR output bit, clocked on each phase wrap below
        {
            out = (v.lfsr & 1) ? (int32_t)v.vol : -(int32_t)v.vol;
        }
    }

    uint32_t before = v.phase;
    v.phase += v.inc;
    if (idx == 3 && v.phase < before)   // noise: clock the LFSR once per period wrap (unsigned overflow = wrap)
    {
        // Feedback tap picks the timbre: bit 1 = the long 15-bit sequence (white hiss); bit 6 = the
        // NES "short mode", a much shorter period that sounds buzzy/metallic/almost-pitched.
        uint16_t tap = v.noise_mode ? (uint16_t)(v.lfsr >> 6) : (uint16_t)(v.lfsr >> 1);
        uint16_t bit = (uint16_t)((v.lfsr ^ tap) & 1);
        v.lfsr = (uint16_t)((v.lfsr >> 1) | (bit << 14));
        if (v.lfsr == 0) v.lfsr = 1;
    }
    return (int16_t)out;
}

int apu_render(int16_t *out, int nframes)
{
    for (int i = 0; i < nframes; ++i)
    {
        // Frame-tick: advance instrument macros 60x/sec, sample-counted (host-chunk-independent).
        apu_instance.tick_accum += APU_FPS;
        if (apu_instance.tick_accum >= (uint32_t)APU_RATE)
        {
            apu_instance.tick_accum -= (uint32_t)APU_RATE;
            apu_instance.tick_count++;
            step_macros();
        }

        int32_t mix = 0;
        for (int v = 0; v < APU_NUM_VOICES; ++v)
            mix += step_voice(apu_instance.voices[v], v);
        // Apply the master volume. `>> (7 - APU_MASTER_SHIFT)` makes master_vol=128 EXACTLY equal the
        // old fixed `<< APU_MASTER_SHIFT` gain, so default output is byte-identical; 64 = half, 0 =
        // mute, 255 ~= 2x (clips). A single multiply per sample -- negligible.
        mix = (mix * (int32_t)apu_instance.master_vol) >> (7 - APU_MASTER_SHIFT);
        if (mix > 32767) mix = 32767;
        if (mix < -32768) mix = -32768;
        out[i] = (int16_t)mix;
    }
    return nframes;
}

int apu_rate() { return APU_RATE; }

// The APU's own 60 Hz clock, exposed so the CPU-side sequencer (lib/music.lib) can time itself off
// the SAME tick that drives instrument macros -- see apu.h for why this is the right clock to use.
uint32_t apu_ticks() { return apu_instance.tick_count; }

// Load a macro from the command stream: [loop][len][data...]. Advances `i` by the DECLARED length so
// the stream stays aligned even if the table was clamped to APU_MACRO_MAXLEN. `mask` clamps each
// value (15 for volume, 3 for duty, 255 = store raw for the signed arpeggio table).
static void load_macro(Macro &m, const uint8_t *b, int &i, uint8_t mask)
{
    uint8_t loop = b[i];
    uint8_t decl = b[i + 1];
    i += 2;
    uint8_t store = (decl > APU_MACRO_MAXLEN) ? APU_MACRO_MAXLEN : decl;
    for (uint8_t k = 0; k < store; ++k) m.data[k] = (uint8_t)(b[i + k] & mask);
    m.len  = store;
    m.loop = loop;
    i += decl;
}

// ── Command decoder — the full wire protocol (fixed from Phase 0, same spirit as ppu.cpp) ────────
// Ops: 0x00 NOP, 0x01 NOTE_ON(raw), 0x02 NOTE_OFF, 0x03 SET_VOL, 0x04 SET_DUTY, 0x05 SET_MASTER,
// 0x10 DEF_INST_VOL, 0x11 NOTE_ON_INST, 0x12 DEF_INST_DUTY, 0x13 DEF_INST_ARP, 0x14 DEF_INST_VIB,
// 0x15 SLIDE_TO, 0x16 DEF_INST_NOISE. Unlike the PPU there is no PRESENT terminator — every command
// takes effect the instant it's decoded.
// `chan` is masked to 0..3 (`&3`), same style as the PPU's tilemap `&31` wrap — always in range.
static bool apu_execute(const uint8_t *b, int len)
{
    bool any = false;
    for (int i = 0; i < len;)
    {
        uint8_t op = b[i++];
        switch (op)
        {
        case 0x00:   // NOP / padding
            any = true;
            break;
        case 0x01:   // NOTE_ON [chan][freq_lo][freq_hi][vol]  (RAW: fixed freq + fixed vol, no envelope)
        {
            int ch = b[i] & 3;
            uint16_t freq = (uint16_t)(b[i + 1] | (b[i + 2] << 8));
            uint8_t  vol  = b[i + 3];
            i += 4;
            Voice &v = apu_instance.voices[ch];
            v.freq = freq;
            v.inc  = (uint32_t)(((uint64_t)freq << 32) / APU_RATE);
            v.vol  = vol;
            v.inst = -1;   // raw: SET_VOL/env don't touch it; the caller drives volume by hand
            v.gate = 1;
            any = true;
            break;
        }
        case 0x02:   // NOTE_OFF [chan]
        {
            int ch = b[i] & 3;
            i += 1;
            apu_instance.voices[ch].gate = 0;
            any = true;
            break;
        }
        case 0x03:   // SET_VOL [chan][vol]  (volume only, no retrigger)
        {
            int ch = b[i] & 3;
            uint8_t vol = b[i + 1];
            i += 2;
            apu_instance.voices[ch].vol = vol;
            any = true;
            break;
        }
        case 0x04:   // SET_DUTY [chan][duty]  (pulse only; harmless no-op on triangle/noise)
        {
            int ch = b[i] & 3;
            uint8_t duty = b[i + 1];
            i += 2;
            apu_instance.voices[ch].duty = (uint8_t)(duty & 3);
            any = true;
            break;
        }
        case 0x05:   // SET_MASTER [vol]  (overall output volume: 128 = normal, 64 = half, 0 = mute)
        {
            apu_instance.master_vol = b[i];
            i += 1;
            any = true;
            break;
        }
        case 0x10:   // DEF_INST_VOL  [id][loop][len][levels 0..15...]  (volume envelope macro)
        {
            uint8_t id = (uint8_t)(b[i] % APU_MAX_INSTRUMENTS);
            i += 1;
            load_macro(apu_instance.instruments[id].vol, b, i, 15);
            any = true;
            break;
        }
        case 0x12:   // DEF_INST_DUTY [id][loop][len][duties 0..3...]  (pulse timbre macro)
        {
            uint8_t id = (uint8_t)(b[i] % APU_MAX_INSTRUMENTS);
            i += 1;
            load_macro(apu_instance.instruments[id].duty, b, i, 3);
            any = true;
            break;
        }
        case 0x13:   // DEF_INST_ARP  [id][loop][len][signed semitone offsets...]  (arpeggio/pitch macro)
        {
            uint8_t id = (uint8_t)(b[i] % APU_MAX_INSTRUMENTS);
            i += 1;
            load_macro(apu_instance.instruments[id].arp, b, i, 255);   // stored raw; read as int8
            any = true;
            break;
        }
        case 0x14:   // DEF_INST_VIB  [id][depth][speed][delay]  (vibrato scalars)
        {
            uint8_t id = (uint8_t)(b[i] % APU_MAX_INSTRUMENTS);
            Instrument &ins = apu_instance.instruments[id];
            ins.vib_depth = b[i + 1];
            ins.vib_speed = b[i + 2];
            ins.vib_delay = b[i + 3];
            i += 4;
            any = true;
            break;
        }
        case 0x16:   // DEF_INST_NOISE [id][mode]  (noise timbre: 0 = hiss, 1 = short/periodic metallic)
        {
            uint8_t id = (uint8_t)(b[i] % APU_MAX_INSTRUMENTS);
            apu_instance.instruments[id].noise_mode = (uint8_t)(b[i + 1] & 1);
            i += 2;
            any = true;
            break;
        }
        case 0x11:   // NOTE_ON_INST [chan][note][inst]  (musical: note-number pitch, macro-driven timbre)
        {
            int ch = b[i] & 3;
            uint8_t note = (uint8_t)(b[i + 1] & 127);
            uint8_t inst = (uint8_t)(b[i + 2] % APU_MAX_INSTRUMENTS);
            i += 3;
            Voice &v = apu_instance.voices[ch];
            v.inst            = (int16_t)inst;
            v.note            = note;
            v.freq            = 0;
            v.gate            = 1;
            v.vol_cur = v.duty_cur = v.arp_cur = 0;   // restart every macro from its beginning
            v.pitch_fp        = (int32_t)note << 8;   // base pitch; a slide/arp/vibrato bends around it
            v.slide_target_fp = v.pitch_fp;
            v.slide_rate      = 0;
            v.vib_phase       = 0;
            Instrument &ins   = apu_instance.instruments[inst];
            v.vib_timer       = ins.vib_delay;
            v.noise_mode      = ins.noise_mode;
            // Prime the registers so the samples before the first frame-tick are already correct.
            v.inc  = pitch_to_inc(v.pitch_fp);
            v.vol  = (ins.vol.len  > 0) ? (uint8_t)(ins.vol.data[0] * 17)   : 0;
            if (ins.duty.len > 0) v.duty = (uint8_t)(ins.duty.data[0] & 3);
            any = true;
            break;
        }
        case 0x15:   // SLIDE_TO [chan][note][rate]  (portamento: glide the sounding note toward `note`)
        {
            int ch = b[i] & 3;
            uint8_t note = (uint8_t)(b[i + 1] & 127);
            uint8_t rate = b[i + 2];
            i += 3;
            Voice &v = apu_instance.voices[ch];
            v.slide_target_fp = (int32_t)note << 8;   // glide from the CURRENT pitch_fp toward here
            v.slide_rate      = rate;                 // fp units/frame (256 = one semitone); 0 = stop
            any = true;
            break;
        }
        default:
            // Unknown op: its length is unknown, so we cannot safely resync — stop. With our own
            // encoder this never happens (mirrors ppu_execute's identical reasoning).
            return any;
        }
    }
    return any;
}

bool apu_receive(const uint8_t *src, int len)
{
    if (len < 0) len = 0;
    if (len > (int)sizeof(s_inbuf)) len = (int)sizeof(s_inbuf);
    memcpy(s_inbuf, src, len);        // the bus copies CPU bytes in (models the same seam as ppu_receive)
    return apu_execute(s_inbuf, len); // the APU only ever reads s_inbuf + its own voice state
}

const uint8_t *apu_state()   { return reinterpret_cast<const uint8_t *>(&apu_instance); }
int            apu_state_size() { return (int)sizeof(apu_instance); }
