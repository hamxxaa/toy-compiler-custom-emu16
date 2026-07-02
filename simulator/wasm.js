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

// Memory layout constants (from definitions.h) — kept here so the rest of the
// frontend keeps reading window.EMU_CONSTANTS exactly as before.
const INPUT_ADDRESS       = 0xADFF;
const VRAM_START_ADDRESS  = 0xB000;
const VRAM_SIZE           = 20480;   // 160×128
const PRAM_START_ADDRESS  = 0xAE00;
const PRAM_SIZE           = 512;     // 256 colors × 2 bytes
const MAX_RAM_ADDRESS     = 0xFFFF;
const STACK_START_ADDRESS = 0xADFE;
const SCREEN_WIDTH        = 160;
const SCREEN_HEIGHT       = 128;

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
        // PPU display bridge (PPU reboot): drawn instead of VRAM once a ROM engages the PPU.
        this._ppuEngaged  = module.cwrap('emu_ppu_engaged',     'number', []);
        this._ppuFbPtr    = module.cwrap('emu_ppu_framebuffer', 'number', []);

        this.memPtr = 0;
        this.instructionCount = 0;
    }

    // Reset core + lay down default palette / cleared VRAM (all done in C), then
    // cache the guest-memory base pointer in the WASM heap.
    initialize() {
        this._init();
        this.memPtr = this._memPtrF();
        this.instructionCount = 0;
    }

    // Live view of the 64 KB guest memory. Fresh subarray each access keeps it
    // valid; the heap can't move because cpu_instance is static data and memory
    // growth is disabled in the build.
    get memory() {
        return this.m.HEAPU8.subarray(this.memPtr, this.memPtr + 65536);
    }

    // Run up to `maxInstructions`. Returns true while still running.
    runBatch(maxInstructions = 100000) {
        const still = this._run(maxInstructions | 0);
        this.instructionCount += maxInstructions | 0;
        return still === 1;
    }

    getRegWord(i) { return this._reg(i & 7); }

    // --- PPU display ---
    // True once the running ROM has driven the PPU (submitted a PRESENT). The app then draws the
    // PPU's composed frame instead of the legacy VRAM+PRAM path. The try/catch means a STALE cached
    // emu.wasm (built before these exports existed) degrades to the VRAM path instead of throwing and
    // bricking the whole sim — if the PPU demo shows black, hard-refresh to pick up the fresh wasm.
    ppuEngaged() {
        try { return this._ppuEngaged() === 1; } catch (e) { return false; }
    }

    // A live view of the PPU's converted RGB565 framebuffer (SCREEN_WIDTH*SCREEN_HEIGHT). The C side
    // fills it from the indexed frame + palette on each call; the returned pointer is 2-byte aligned.
    ppuFramebuffer() {
        const ptr = this._ppuFbPtr();
        return new Uint16Array(this.m.HEAPU8.buffer, ptr, SCREEN_WIDTH * SCREEN_HEIGHT);
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
    VRAM_START_ADDRESS,
    VRAM_SIZE,
    PRAM_START_ADDRESS,
    PRAM_SIZE,
    MAX_RAM_ADDRESS,
    STACK_START_ADDRESS,
    SCREEN_WIDTH,
    SCREEN_HEIGHT,
};
