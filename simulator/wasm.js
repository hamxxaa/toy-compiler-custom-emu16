/**
 * EmuCore — thin JS shim over the WASM build of emu.cpp.
 *
 * Replaces the old hand-ported cpu.js. The browser now runs the *exact same*
 * emulator core as pc_emu.exe and the ESP32 firmware, so there is no second
 * implementation to keep in sync.
 *
 * It deliberately mirrors the old CPU class surface (memory, getRegWord, pc,
 * flags, running, runBatch, initialize) so display.js / debug.js / app.js need
 * no changes beyond construction.
 */

// CPU memory-map constants (from definitions.h) — kept here so the rest of the
// frontend keeps reading window.EMU_CONSTANTS exactly as before.
const INPUT_ADDRESS       = 0xFFFF;
const MAX_RAM_ADDRESS     = 0xFFFF;
const STACK_START_ADDRESS = 0xFFFC;
const SCREEN_WIDTH        = 160;
const SCREEN_HEIGHT       = 128;
const PIXELS              = SCREEN_WIDTH * SCREEN_HEIGHT;   // composed-framebuffer length

// PPU graphics-RAM region offsets — MUST match the memory map in emulator/ppu.cpp. The PPU has its
// own flat address space (separate from the CPU's), inspected via cpu.ppuMem().
const PPU = {
    PAT:      0x0000,   // 128 x 256B 16x16 patterns (tiles + sprites)
    FONT:     0x8000,   // 128 x 8B 1-bit glyphs
    TILEMAP:  0x8400,   // 32x32 bg tile ids (torus)
    TEXTMAP:  0x8800,   // 20x16 cells x 3B {glyph, fg, bg}
    OAM:      0x8BC0,   // 64 sprites x 6B {pat, x:i16, y:i16, attr}
    PAL:      0x8D40,   // 256 x RGB565
    REGS:     0x8F40,   // scroll_x:u16, scroll_y:u16, ctrl:u16, oam_count:u8
    FB:       0x8F50,   // 160x128 indexed compose target
    MEM_SIZE: 0xDF50,
    REG_SCROLL_X: 0x8F40, REG_SCROLL_Y: 0x8F42, REG_CTRL: 0x8F44, REG_OAM_COUNT: 0x8F46,
};

class EmuCore {
    constructor(module) {
        this.m = module;

        // Bind the exported C functions.
        this._init    = module.cwrap('emu_init',      null,     []);
        this._memPtrF = module.cwrap('emu_mem',       'number', []);
        this._run     = module.cwrap('emu_run_frame', 'number', ['number']);
        this._reg     = module.cwrap('emu_reg',       'number', ['number']);
        this._pcF     = module.cwrap('emu_pc',        'number', []);
        this._flagsF  = module.cwrap('emu_flags',     'number', []);
        this._runF    = module.cwrap('emu_running',   'number', []);
        this._setRomCount = module.cwrap('emu_set_rom_count', null,     ['number']);
        this._romNamesPtr = module.cwrap('emu_rom_names',     'number', []);
        this._pendingRom  = module.cwrap('emu_pending_rom',   'number', []);
        this._targetFps   = module.cwrap('emu_target_fps',    'number', []);
        // Pieces A/B: save namespacing + asset pack
        this._setCurrentRom = module.cwrap('emu_set_current_rom',   null,     ['string']);
        this._assetBuf      = module.cwrap('emu_asset_buf',         'number', []);
        this._assetCap      = module.cwrap('emu_asset_capacity',    'number', []);
        this._commitAsset   = module.cwrap('emu_commit_asset_pack', null,     ['number']);
        // PPU display bridge: the only display path (VRAM/PRAM were reclaimed for more RAM).
        this._ppuEngaged  = module.cwrap('emu_ppu_engaged',     'number', []);
        this._ppuFbPtr    = module.cwrap('emu_ppu_framebuffer', 'number', []);
        // Debug / perf bridge. Wrapped in _opt so a stale cached wasm missing these exports degrades
        // gracefully (returns 0 / no viewer) instead of throwing at construction.
        this._ppuMemPtr   = this._opt('emu_ppu_mem',      'number');
        this._ppuMemSize  = this._opt('emu_ppu_mem_size', 'number');
        this._didPresent  = this._opt('emu_did_present',  'number');
        this._instrCount  = this._opt('emu_instr_count',  'number');
        // APU audio bridge -- optional like the debug/
        // perf bridges above, since it's brand new and a stale cached wasm won't have it yet.
        this._apuRenderPtr = this._opt('emu_apu_render', 'number', ['number']);
        this._apuRateF      = this._opt('emu_apu_rate',   'number');

        this.memPtr = 0;
    }

    // cwrap only if the export exists (older cached wasm may lack it); else a stub returning 0.
    // A silent fallback here looks exactly like a completely different bug downstream (a caller
    // getting all-zero results with no error), so warn loudly when it happens.
    _opt(name, ret, args = []) {
        try {
            if (this.m['_' + name]) return this.m.cwrap(name, ret, args);
        } catch (e) { /* fall through */ }
        console.warn(`EmuCore._opt: export _${name} NOT FOUND on the loaded module -- falling back ` +
            `to a stub that always returns 0. This usually means a stale cached emu.wasm predating ` +
            `this export; hard-refresh (and consider "Empty Cache and Hard Reload" in DevTools).`);
        return () => 0;
    }

    // Reset core (all done in C), then cache the guest-memory base pointer in the WASM heap.
    initialize() {
        this._init();
        this.memPtr = this._memPtrF();
    }

    // Real cumulative executed-instruction count (needs -DEMU_COUNT_INSTRUCTIONS in the wasm build;
    // 0 otherwise). >>>0 keeps it an unsigned 32-bit value so JS-side deltas stay correct.
    get instructionCount() { return this._instrCount() >>> 0; }

    // True iff the LAST runBatch ended because the ROM presented a frame (vs the per-frame budget
    // being hit mid-frame, or a genuine halt). Lets the app measure the effective frame rate and
    // redraw only on a freshly composed frame.
    presentPending() { return this._didPresent() === 1; }

    // Live view of the PPU's graphics RAM (for the debug memory viewer). null if unavailable.
    ppuMem() {
        const p = this._ppuMemPtr(), n = this._ppuMemSize();
        if (!p || !n) return null;
        return this.m.HEAPU8.subarray(p, p + n);
    }

    // Live view of the 64 KB guest memory. Fresh subarray each access keeps it
    // valid; the heap can't move because cpu_instance is static data and memory
    // growth is disabled in the build.
    get memory() {
        return this.m.HEAPU8.subarray(this.memPtr, this.memPtr + 65536);
    }

    // Run up to `maxInstructions` (the per-frame CPU budget). Returns true while still running (a
    // PRESENT yield counts as still-running so the loop keeps pacing). The real executed count is
    // tracked by the C core, not derived from maxInstructions here.
    runBatch(maxInstructions = 100000) {
        return this._run(maxInstructions | 0) === 1;
    }

    getRegWord(i) { return this._reg(i & 7); }

    // --- PPU display ---
    // True once the running ROM has driven the PPU (submitted a PRESENT) -- every ROM does now.
    // Used to gate the palette-warning check (below) on "has the game started presenting yet".
    // The try/catch guards a STALE cached emu.wasm (built before this export existed): it returns
    // false instead of throwing and bricking the whole sim -- if the demo shows black, hard-refresh.
    ppuEngaged() {
        try { return this._ppuEngaged() === 1; } catch (e) { return false; }
    }

    // A live view of the PPU's converted RGB565 framebuffer (SCREEN_WIDTH*SCREEN_HEIGHT). The C side
    // fills it from the indexed frame + palette on each call; the returned pointer is 2-byte aligned.
    ppuFramebuffer() {
        const ptr = this._ppuFbPtr();
        return new Uint16Array(this.m.HEAPU8.buffer, ptr, SCREEN_WIDTH * SCREEN_HEIGHT);
    }

    // --- APU audio bridge ---
    // The APU's sample rate (fixed at build time; see emulator/apu.h APU_RATE).
    apuRate() { return this._apuRateF(); }

    // Render `n` samples from the APU's CURRENT voice state (a pure function of state at call
    // time -- independent of runBatch's cadence, see apu.h) and return an Int16Array VIEW into the
    // WASM heap. The view is only valid until the next emu_apu_render call (the C side reuses one
    // static buffer) -- callers that need to hold onto it (e.g. handing samples to an
    // AudioWorklet) must copy out before rendering again.
    apuRender(n) {
        const ptr = this._apuRenderPtr(n | 0);
        if (!ptr) return new Int16Array(0);
        return new Int16Array(this.m.HEAPU8.buffer, ptr, n | 0);
    }

    get pc()      { return this._pcF(); }
    get flags()   { return this._flagsF(); }
    get running() { return this._runF() === 1; }

    // --- ROM-library bridge for the M7d syscall handler ---
    pendingRom() { return this._pendingRom(); }

    // Target frame rate a ROM requested via sys_set_fps (default 60). The app loop paces to it.
    targetFps() { return this._targetFps(); }

    setRomLibrary(names) {
        const base = this._romNamesPtr();
        const STRIDE = 64;
        const n = Math.min(names.length, 16);
        for (let i = 0; i < n; i++) {
            const b = new TextEncoder().encode(names[i].slice(0, 63));
            this.m.HEAPU8.set(b, base + i * STRIDE);
            this.m.HEAPU8[base + i * STRIDE + b.length] = 0;   // NUL
        }
        this._setRomCount(n);
    }

    // Namespace saves to this game (localStorage key "emu16:<rom>.<slot>"). Call on each ROM boot.
    setCurrentRom(name) {
        this._setCurrentRom(name);
    }

    // Hand the game's asset pack (a Uint8Array of <rom>.pak bytes) to the core for ASSET_* syscalls.
    setAssetPack(bytes) {
        const buf = this._assetBuf();
        const cap = this._assetCap();
        const n = Math.min(bytes.length, cap);
        this.m.HEAPU8.set(bytes.subarray(0, n), buf);
        this._commitAsset(n);
    }
}

window.EmuCore = EmuCore;
window.EMU_CONSTANTS = {
    INPUT_ADDRESS,
    MAX_RAM_ADDRESS,
    STACK_START_ADDRESS,
    SCREEN_WIDTH,
    SCREEN_HEIGHT,
    PIXELS,
    PPU,
};
