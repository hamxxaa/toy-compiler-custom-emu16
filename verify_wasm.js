// Headless verification that the WASM build of emu.cpp matches pc_emu.exe.
// Runs each test ROM through the same core the browser uses and checks R0.
//
//   node verify_wasm.js
//
// (ROMs must already be built — run `python run_tests.py` first, which compiles them.)

const fs = require('fs');
const path = require('path');
const createEmu = require('./simulator/emu.js');

const ROM_DIR = path.join(__dirname, 'build', 'roms');

// (rom_name, expected R0) — mirrors run_tests.py
const TESTS = [
    ['test_mul', 42], ['test_div', 6], ['test_byte', 300],
    ['test_asm', 42], ['test_asm_loop', 15], ['test_include', 42],
    ['test_plot', 99], ['test_fill', 7], ['test_palette', 1234],
    ['demo', 42], ['test_hex', 255], ['test_array_int', 42],
    ['test_array_byte', 7], ['test_pointer', 99], ['test_deref_write', 77],
    ['test_bitand', 11], ['test_bitor', 255], ['test_bitxor', 240],
    ['test_bitnot', 255], ['test_shift', 36], ['test_logical', 3],
    ['test_else', 2], ['test_elseif', 30],
    // Font rendering: proves the font ships in the ROM (no host load) and renders on WASM.
    ['test_draw_char', 1], ['test_draw_string', 1],
    // M7d: the browser syscall handler now implements echo (0x7F), so the interrupt path verifies on WASM.
    ['test_syscall_echo', 42],
    // Phase 0: the O(1) switch jump table exercises JMP register-mode (the one shared-core ISA change).
    ['test_switch', 427],
    // PPU reboot Phase 0: the command-buffer seam (SYSCALL_PPU_SUBMIT -> PPU decode) runs on WASM too.
    ['test_ppu_backdrop', 1565],
    // Phase 1: bulk upload into PPU RAM + scroll encoding (test_ppu_dma is pc_emu-only — needs a pak).
    ['test_ppu_tiles', 264],
    // Phase 2: OAM sprite encoding.
    ['test_ppu_sprites', 4178],
    // Phase 3: text-plane/palette encoding + CPU-side tile collision.
    ['test_ppu_text', 3764],
    ['test_ppu_collide', 31],
    // Event foundation (guest libs; deterministic — test_flags uses save/load so it's pc_emu-only).
    ['test_dialog', 7], ['test_zone', 31], ['test_interact', 127], ['test_events', 255],
];

(async () => {
    const Module = await createEmu();
    const init     = Module.cwrap('emu_init', null, []);
    const memPtrF  = Module.cwrap('emu_mem', 'number', []);
    const runFrame = Module.cwrap('emu_run_frame', 'number', ['number']);
    const reg      = Module.cwrap('emu_reg', 'number', ['number']);

    let allPass = true;
    console.log(`${'Test'.padEnd(20)} ${'expect'.padStart(7)} ${'got'.padStart(7)}`);
    console.log('-'.repeat(40));

    for (const [name, expected] of TESTS) {
        const romPath = path.join(ROM_DIR, name + '.rom');
        if (!fs.existsSync(romPath)) {
            console.log(`${name.padEnd(20)} ${String(expected).padStart(7)} ${'NOROM'.padStart(7)}  FAIL`);
            allPass = false;
            continue;
        }
        const rom = fs.readFileSync(romPath);

        init();                                   // zero mem + default palette
        const base = memPtrF();
        Module.HEAPU8.set(rom, base);             // load ROM at address 0
        runFrame(100000);                         // one frame

        const got = reg(0);
        const ok = got === expected;
        if (!ok) allPass = false;
        console.log(`${name.padEnd(20)} ${String(expected).padStart(7)} ${String(got).padStart(7)}  ${ok ? 'PASS' : 'FAIL'}`);
    }

    console.log('-'.repeat(40));
    console.log(`ALL PASS: ${allPass}`);
    process.exit(allPass ? 0 : 1);
})();
