// EMU16 APU — a software audio synthesizer, the PPU's twin (see plans/buzzy-streaming-tanaka.md).
// It is a SEPARATE unit from the CPU: the CPU never produces a sample. It posts sparse voice-state
// updates (a handful of bytes, a few times a frame) via a command stream; the APU free-runs at
// audio rate from that resident state between submits. This is what makes the audio stream
// immune to a bursty/stuttering game loop -- no sample is ever waiting on the CPU.
//
// One deliberate difference from the PPU: APU commands apply IMMEDIATELY on submit (they write
// live voice registers), not latched at a PRESENT. There is no "audio present" -- apu_submit is
// not tied to ppu_present. On one chip this is a function call; on two chips (a second ESP32 as
// an APU co-processor) the same bytes cross a wire.
//
// Phase 0 = the synth core only: voice state + command decode + integer/fixed-point render. No
// host wiring yet (that's sys_apu_submit / emu_apu_render in phases 1-2).
#pragma once
#include <cstdint>

// 4 voices: 0,1 = pulse, 2 = triangle, 3 = noise (NES-ish). All 4 are implemented now; an unused
// voice is simply never NOTE_ON'd (its `gate` stays 0), which is the natural "stub" mechanism --
// no separate mute flag needed.
#define APU_NUM_VOICES 4
#define APU_RATE       22050   // samples/sec, mono, int16. Lo-fi is period-correct and halves the
                                // per-sample synth cost; this is a #define knob, not load-bearing.

// Reset all voice state. Called from initialize_cpu() so every host (pc_emu / WASM / firmware)
// resets the APU together with the CPU + PPU.
void apu_reset();

// Inbound transport (models the same "bus copies bytes in" seam as ppu_receive): decode `len`
// command bytes from `src` and apply them to voice state IMMEDIATELY (no latching, no PRESENT).
// Returns true if at least one recognized command was applied (host convenience; not required
// for correctness -- unlike the PPU there is no "did this end in PRESENT" question).
bool apu_receive(const uint8_t* src, int len);

// Render `nframes` samples (mono, int16) from the CURRENT voice state into `out`, advancing every
// active voice's internal clock. Pure function of voice state at call time -- this is what a host
// calls on its own cadence (once per pc_emu frame; from an audio-rate pump in the browser).
// Returns nframes (always fully renders; there is no failure mode).
int apu_render(int16_t* out, int nframes);

// The sample rate render() produces at. Hosts need this to size buffers / write WAV headers /
// convert to the audio output's native rate.
int apu_rate();

// Debug/host inspection seam (mirrors ppu_mem()/ppu_mem_size()): a raw byte view of voice state.
// NOT part of the render path -- for tooling only (a future memory/audio viewer).
const uint8_t* apu_state();
int apu_state_size();
