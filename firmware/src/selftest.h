#pragma once

// On-device bring-up self-tests. Each replaces the firmware and exercises one piece of hardware in
// isolation, so a wiring fault reads differently from a software one. Set DIAG to one value to run
// that test; Diag::None runs the firmware normally. The bracketed tag is which build the test needs.
enum class Diag {
    None,          // run the firmware
    Framebuffer,   // fill the TFT solid green                  [single/cpu build]
    AudioTone,     // a raw 440 Hz I2S tone, APU bypassed        [single/cpu build]
    ApuBlip,       // the real APU plays a repeating blip         [single/cpu build]
    WireLoopback,  // byte-perfect check of the inter-chip SPI    [cpu build]
    TestPattern,   // send a bouncing sprite over the wire        [cpu build]
    TftBars,       // color-bar panel test on the display chip    [ppu build]
};
constexpr Diag DIAG = Diag::None;

// Run the selected diagnostic. Returns true if one ran (the caller returns early); false for None.
bool selftest_setup();
bool selftest_loop();
