// Headless probe of the WASM asset path (run from simulator/ with the emsdk node):
//   node debug_walkdemo.js
// Replicates exactly what app.js does: init, load the ROM at 0, set current rom, commit the .pak,
// run a few frames, then check that the palette reached PRAM and the sprite reached VRAM.
const fs = require('fs');
const createEmu = require('./emu.js');

createEmu().then((M) => {
    const init          = M.cwrap('emu_init', null, []);
    const memPtr        = M.cwrap('emu_mem', 'number', []);
    const run           = M.cwrap('emu_run_frame', 'number', ['number']);
    const setCurrentRom = M.cwrap('emu_set_current_rom', null, ['string']);
    const assetBuf      = M.cwrap('emu_asset_buf', 'number', []);
    const assetCap      = M.cwrap('emu_asset_capacity', 'number', []);
    const commitAsset   = M.cwrap('emu_commit_asset_pack', null, ['number']);

    init();
    const mem = memPtr();
    const rom = fs.readFileSync('../build/roms/walkdemo.rom');
    M.HEAPU8.set(rom, mem);                 // load ROM at address 0
    setCurrentRom('walkdemo');

    const pak = fs.readFileSync('../build/roms/walkdemo.pak');
    const n = Math.min(pak.length, assetCap());
    M.HEAPU8.set(pak.subarray(0, n), assetBuf());
    commitAsset(n);
    console.log('pak bytes:', pak.length, 'committed:', n);

    for (let i = 0; i < 5; i++) run(100000);   // a few frames (each ends on sys_present)

    const PRAM = 0xAE00, VRAM = 0xB000;
    const p1 = M.HEAPU8[mem + PRAM + 2] | (M.HEAPU8[mem + PRAM + 3] << 8);
    console.log('PRAM[1] = 0x' + p1.toString(16).padStart(4, '0'),
                '(expect 0x296b = loaded dark-blue; 0xffff = default palette = NOT loaded)');
    const center = M.HEAPU8[mem + VRAM + 64 * 160 + 80];
    console.log('VRAM(80,64) index =', center, '(expect 2 = body; 1 = bg only)');
    const seen = new Set();
    for (let i = 0; i < 20480; i++) seen.add(M.HEAPU8[mem + VRAM + i]);
    console.log('distinct VRAM indices:', [...seen].sort((a, b) => a - b).join(','));
});
