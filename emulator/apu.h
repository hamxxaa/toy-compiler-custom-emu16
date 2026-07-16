// EMU16 APU — a software audio synthesizer, the PPU's twin.
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

// A free-running count of the APU's 60 Hz frame-ticks -- the same tick that steps instrument macros
// (volume/duty/arp envelopes, vibrato, slides). It is SAMPLE-COUNTED inside apu_render, so it
// advances with rendered audio and nothing else: not with the game's frame rate, not with wall time.
//
// This exists so the CPU-side song sequencer (lib/music.lib, via sys_apu_ticks) can advance on the
// SAME clock its envelopes run on. Previously the sequencer advanced once per music_frame() call --
// i.e. once per GAME frame -- so on any host that missed 60 fps the song dragged (measured: ~44 fps
// on the ESP32 => music at 73% speed) while envelopes still ticked at 60. Two clocks; the music was
// on the wrong one. Timing off this counter makes drift between them structurally impossible.
//
// Per-host behaviour falls out for free, which is the point:
//   * firmware / browser -- apu_render is driven by real audio hardware, so this is true 60 Hz
//     real time, and the tempo is correct at ANY game frame rate.
//   * pc_emu -- renders exactly APU_RATE/60 samples per emulated frame, so this advances exactly
//     once per frame: identical to the old behaviour, keeping the runner deterministic and every
//     existing audio/sequencer regression test valid.
// Wraps at 2^32 (~2.3 years); callers only ever take DIFFERENCES, so wraparound is a non-issue.
uint32_t apu_ticks();

// Debug/host inspection seam (mirrors ppu_mem()/ppu_mem_size()): a raw byte view of voice state.
// NOT part of the render path -- for tooling only (a future memory/audio viewer).
const uint8_t* apu_state();
int apu_state_size();
