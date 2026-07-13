/**
 * App Controller — Main Simulation Loop
 *
 * Orchestrates the exact ESP32 loop() sequence:
 *   1. Write input state to memory[INPUT_ADDRESS]
 *   2. Run CPU batch (100k instructions)
 *   3. Convert the PPU's composed framebuffer and render to canvas
 *   4. Update debug panel
 *   5. Sync to ~60 FPS via requestAnimationFrame
 */

class App {
    constructor(core) {
        this.cpu = core;
        this.display = new Display('screen');
        this.input = new InputHandler();
        this.debug = new DebugPanel();
        this.audio = new AudioEngine(core);   // Phase 2 APU prototype (see plans/buzzy-streaming-tanaka.md)

        // Simulation state
        this.isRunning = false;
        this.animFrameId = null;
        this.romLoaded = false;
        this.romData = null;

        // M7d: in-memory ROM library (dropped ROMs accumulate here; the syscall handler lists them).
        this.library = [];        // [{ name, bytes }]
        this.menuIndex = -1;      // index of menu.rom, used by sys_reset()
        this.currentIndex = -1;   // index of the ROM currently running

        // Performance tracking (all measured over a rolling ~500ms window)
        this.fps = 0;     // effective frame rate: game frames PRESENTED per second
        this.ips = 0;     // instructions per second (real, from the C counter)
        this.ipf = 0;     // instructions per presented frame
        this.statsInterval = 500;  // ms

        // FPS cap: 'auto' follows the ROM's sys_set_fps (default 60); a number forces it; 0 = uncapped.
        this.fpsCap = 'auto';

        // CPU budget: max instructions the emulator runs per real-time frame. Simulates CPU power --
        // if a game's logical frame needs more instructions than this, it fragments across several
        // real frames, so the effective FPS drops (models a weaker/overloaded ESP). Default is high
        // (plenty of headroom); dial it down to test performance.
        this.instructionsPerFrame = 60000;

        this._bindControls();
        this._bindROMLoader();
        this._bindSpeedControl();
        this._bindVisibilityWarning();

        // Initial debug update (core already initialized by the bootstrap).
        this.debug.update(this.cpu, this.isRunning);
    }

    // The game loop rides on requestAnimationFrame (see _frame()), which browsers throttle heavily
    // when a tab is hidden/backgrounded (often to ~1Hz). So a backgrounded tab makes frame-counted
    // game timers crawl and button-input polling miss presses. Audio is on the pull model now (the
    // audio thread requests refills on its own, independent of RAF), but the AudioEngine ANSWERS
    // those requests on the main thread -- which is also throttled when hidden -- so the worklet
    // starves and underruns climb while backgrounded too. All of it recovers on refocus. This is a
    // real confound for the Phase 2 latency test (see plans/buzzy-streaming-tanaka.md), so surface it
    // directly instead of leaving "everything got weird for a while" a mystery.
    _bindVisibilityWarning() {
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                this._showStatus('Tab backgrounded -- timing/audio will be inaccurate until it\'s focused again (this is a browser throttling effect, not a bug).', 'warning');
            } else if (this.isRunning) {
                this._showStatus('Tab focused again -- audio recovering.', 'info');
            }
        });
    }

    // --- ROM loading ---

    _bindROMLoader() {
        const dropZone = document.getElementById('drop-zone');
        const fileInput = document.getElementById('rom-file');

        // Drag and drop
        if (dropZone) {
            dropZone.addEventListener('dragover', (e) => {
                e.preventDefault();
                dropZone.classList.add('drag-over');
            });

            dropZone.addEventListener('dragleave', () => {
                dropZone.classList.remove('drag-over');
            });

            dropZone.addEventListener('drop', (e) => {
                e.preventDefault();
                dropZone.classList.remove('drag-over');
                this._loadFiles(e.dataTransfer.files);
            });

            // Click to open file dialog
            dropZone.addEventListener('click', () => {
                if (fileInput) fileInput.click();
            });
        }

        // File input change
        if (fileInput) {
            fileInput.addEventListener('change', (e) => {
                this._loadFiles(e.target.files);
            });
        }
    }

    // Accepts one or more files dropped/selected at once -- e.g. dragging a .rom and its matching
    // .pak together, which used to silently discard everything but files[0]. ROMs are processed
    // before packs so a pack dropped in the same gesture as its ROM finds the ROM already in the
    // library and applies immediately (see _loadROMFile's .pak branch); either order still works
    // (each branch re-checks / re-boots), this just avoids a redundant extra boot.
    _loadFiles(fileList) {
        const files = Array.from(fileList || []);
        files.sort((a, b) => {
            const aPak = a.name.toLowerCase().endsWith('.pak');
            const bPak = b.name.toLowerCase().endsWith('.pak');
            return aPak === bPak ? 0 : (aPak ? 1 : -1);
        });
        files.forEach(f => this._loadROMFile(f));
    }

    _loadROMFile(file) {
        const reader = new FileReader();
        reader.onload = (e) => {
            const bytes = new Uint8Array(e.target.result);
            // A .pak is an asset pack for the same-named ROM, not a ROM itself: stash it by basename
            // so the next boot of that ROM feeds it to the core (Piece B asset streaming).
            if (file.name.toLowerCase().endsWith('.pak')) {
                this.assetPacks = this.assetPacks || {};
                const base = file.name.replace(/\.pak$/i, '');
                this.assetPacks[base] = bytes;
                this._showStatus(`Asset pack ready: ${file.name} (${bytes.length} bytes)`, 'success');
                // If this pack belongs to the ROM that's already loaded, re-boot it so the game's
                // sys_asset_load calls pick it up (drop order then doesn't matter).
                if (this.currentIndex >= 0 &&
                    this.library[this.currentIndex].name.replace(/\.rom$/i, '') === base) {
                    this._bootFromLibrary(this.currentIndex);
                }
                return;
            }
            this._loadROM(bytes, file.name);
        };
        reader.readAsArrayBuffer(file);
    }

    _loadROM(romBytes, name = 'ROM') {
        const { STACK_START_ADDRESS } = window.EMU_CONSTANTS;

        // Validate ROM size
        if (romBytes.length > STACK_START_ADDRESS - 1) {
            this._showStatus(`ROM too large: ${romBytes.length} bytes (max ${STACK_START_ADDRESS - 1})`, 'error');
            return;
        }

        // Accumulate into the library (the syscall handler lists ROMs by name). Re-dropping a
        // same-named file updates its bytes rather than duplicating it.
        let idx = this.library.findIndex(e => e.name === name);
        if (idx < 0) { this.library.push({ name, bytes: romBytes }); idx = this.library.length - 1; }
        else this.library[idx].bytes = romBytes;
        if (name === 'menu.rom') this.menuIndex = idx;

        this._bootFromLibrary(idx);
    }

    // Reset the core, push the current name list to the syscall handler, load a library ROM, run UI.
    // Reused by the drop handler and by the halt-time hot-swap (LOAD_ROM / RESET).
    _bootFromLibrary(index) {
        const entry = this.library[index];
        this.currentIndex = index;
        this.romData = entry.bytes;

        this.cpu.initialize();                                  // zero mem + default palette (C side)
        this.cpu.setRomLibrary(this.library.map(e => e.name)); // feed names to the syscall handler
        this.cpu.memory.set(entry.bytes, 0);                   // load ROM at address 0
        this.audio.reset();   // drop any queued audio from whatever ROM was running before

        // Pieces A/B: namespace saves to this game, and feed its asset pack if one was dropped.
        const base = entry.name.replace(/\.rom$/i, '');
        this.cpu.setCurrentRom(base);
        this.assetPacks = this.assetPacks || {};
        if (this.assetPacks[base]) this.cpu.setAssetPack(this.assetPacks[base]);

        this.romLoaded = true;
        this._paletteChecked = false;   // re-arm the empty-palette warning for this boot
        // Fresh boot -> fresh perf reading. Without this, the FPS/IPS/IPF readout keeps showing
        // whatever the PREVIOUS rom (or a previous run of this rom) last measured -- e.g. still
        // reading ~144 from an earlier Uncap test -- until a new ~500ms sample overwrites it.
        this.fps = 0; this.ips = 0; this.ipf = 0;
        this._resetStatsWindow();
        this._updateStatsUI();
        this._showStatus(`Loaded: ${entry.name} (${entry.bytes.length} bytes)`, 'success');
        this._renderFrame();
        this.debug.update(this.cpu, this.isRunning);

        const dropZone = document.getElementById('drop-zone');
        if (dropZone) {
            dropZone.classList.add('loaded');
            dropZone.querySelector('.drop-text').textContent =
                `${entry.name} (${this.library.length} in library)`;
        }
        document.querySelectorAll('.ctrl-btn').forEach(b => b.disabled = false);
    }

    // --- Controls ---

    _bindControls() {
        document.getElementById('btn-run')?.addEventListener('click', () => this.run());
        document.getElementById('btn-pause')?.addEventListener('click', () => this.pause());
        document.getElementById('btn-step')?.addEventListener('click', () => this.stepFrame());
        document.getElementById('btn-reset')?.addEventListener('click', () => this.reset());
    }

    // CPU-budget slider: instructions the emulator may run per real-time frame (exponential 2K..128K,
    // covering "weaker than an ESP" up to "way more headroom than needed"). The real ESP does roughly
    // 1.9M instr/s -> ~31K per 60fps frame, so ~31K here simulates the ESP itself.
    _bindSpeedControl() {
        const MIN = 2000, MAX = 128000;
        const slider = document.getElementById('speed-slider');
        const label = document.getElementById('speed-label');
        const apply = (val) => {
            const t = val / 100;                                  // 0..1
            let b = MIN * Math.pow(MAX / MIN, t);
            b = Math.round(b / 500) * 500;                        // snap to a tidy step
            this.instructionsPerFrame = b;
            if (label) label.textContent = this._fmt(b) + '/f';
        };
        if (slider) {
            slider.addEventListener('input', (e) => {
                apply(parseInt(e.target.value));
                this._resetStatsWindow();   // don't blend pre/post-change instr/s into one reading
            });
            apply(parseInt(slider.value));                       // sync to the markup's initial value
        }

        // FPS cap dropdown: Auto (follow ROM) | 30 | 60 | 120 | Uncapped.
        const fpsSel = document.getElementById('fps-cap');
        if (fpsSel) {
            fpsSel.addEventListener('change', (e) => {
                const v = e.target.value;
                this.fpsCap = (v === 'auto') ? 'auto' : parseInt(v);
                this._nextStep = undefined;   // restart pacing cleanly
                // Without this, the in-flight ~500ms sampling window still has presents counted
                // under the OLD cap, so the very next reading blends old-rate + new-rate presents
                // into one misleading number (e.g. switching Uncap(144)->60 briefly still reads
                // ~100+ until a full clean window passes). Starting a fresh window makes the next
                // reading reflect only the new target.
                this._resetStatsWindow();
            });
        }
    }

    // Restart the ~500ms FPS/IPS/IPF sampling window from now, so a rate change (FPS cap, CPU
    // budget) doesn't get blended with presents/instructions counted under the old setting.
    _resetStatsWindow() {
        this._presents = 0;
        this._statStart = performance.now();
        this._statInstr = this.cpu.instructionCount;
        this._statPresents = 0;
    }

    // Human-readable count (12.3K / 1.90M).
    _fmt(n) {
        if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
        if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
        return String(Math.round(n));
    }

    // Resolve the active frame-rate cap: the UI override, or (in 'auto') whatever the ROM requested
    // via sys_set_fps (60 by default). 0 = uncapped (step every RAF, i.e. the monitor's refresh rate).
    _targetFps() {
        if (this.fpsCap === 'auto') return this.cpu.targetFps ? this.cpu.targetFps() : 60;
        return this.fpsCap;
    }

    run() {
        if (!this.romLoaded) return;
        if (this.isRunning) return;

        this.isRunning = true;
        this._updateControlState();
        this._nextStep = undefined;          // reset the FPS-cap deadline accumulator
        this._resetStatsWindow();
        this.audio.ensureStarted();          // fire-and-forget: a button click is the user gesture
                                              // autoplay policy needs; CPU/PPU don't wait on it
        this._frame();
    }

    pause() {
        this.isRunning = false;
        if (this.animFrameId) {
            cancelAnimationFrame(this.animFrameId);
            this.animFrameId = null;
        }
        this._updateControlState();
    }

    // Draw the current frame: the PPU's composed output (the only display path now -- VRAM/PRAM
    // were reclaimed for more usable RAM).
    _renderFrame() {
        this.display.renderPPU(this.cpu.ppuFramebuffer());
    }

    // Self-diagnose the #1 cause of a silent black screen: a ROM that streams its palette from an
    // asset pack (sys_ppu_dma(PAL_MASTER, ...)) but whose matching .pak was never dropped. The PPU
    // still composes and presents fine in that case -- every pixel just converts through an all-zero
    // palette -- so there's no error, only a black canvas. Checked once per boot, on the first
    // presented frame (palette DMA runs during game init, before the first present).
    _checkPaletteWarning() {
        if (this._paletteChecked) return;
        this._paletteChecked = true;
        if (!this.cpu.ppuEngaged || !this.cpu.ppuEngaged()) return;
        const mem = this.cpu.ppuMem ? this.cpu.ppuMem() : null;
        if (!mem) return;
        const { PAL } = window.EMU_CONSTANTS.PPU;
        let allZero = true;
        for (let i = 0; i < 32 && allZero; i++) if (mem[PAL + i] !== 0) allZero = false;
        if (allZero) {
            this._showStatus('Screen is black: PPU palette is empty. This ROM streams its palette from a .pak -- drop the matching .pak file too.', 'warning');
        }
    }

    // Step exactly one GAME frame: run CPU-budget batches until the ROM presents (or halts), so one
    // click always advances one visible frame regardless of the budget. Guarded against a ROM that
    // never presents.
    stepFrame() {
        if (!this.romLoaded) return;
        this.pause();

        const { INPUT_ADDRESS } = window.EMU_CONSTANTS;
        this.cpu.memory[INPUT_ADDRESS] = this.input.getState();
        let guard = 0;
        do {
            this.cpu.runBatch(this.instructionsPerFrame);
            guard++;
        } while (!this.cpu.presentPending() && this.cpu.running && guard < 500);

        // cpu.running is legitimately false right after a PRESENT yield (it resumes on the next
        // runBatch) -- that's not a halt. A real halt is "stopped, and not because it just presented".
        const presented = this.cpu.presentPending();
        const halted = !presented && !this.cpu.running;

        this._renderFrame();
        this._checkPaletteWarning();
        this.debug.update(this.cpu, !halted);
        if (halted) this._showStatus('CPU halted', 'warning');
    }

    reset() {
        this.pause();
        if (this.currentIndex >= 0) this._bootFromLibrary(this.currentIndex);
    }

    // --- Main loop ---

    _frame() {
        if (!this.isRunning) return;
        const now = performance.now();

        // NOTE: audio is NOT driven from here. In the pull model (see plans/buzzy-streaming-tanaka.md),
        // the AudioWorklet requests refills on its own audio-thread cadence and AudioEngine answers
        // them via a port message handler -- fully decoupled from this RAF loop and the FPS cap, which
        // is exactly the two-clock behavior we want (and it can't be starved by a 30fps-capped game).

        // 0. FPS cap. RAF keeps firing at the MONITOR'S refresh rate (60/120/144Hz); we only step the
        //    emulator when the next deadline is due, so gameplay runs at the target rate on any
        //    display. A deadline ACCUMULATOR (advance by exactly `interval`, don't reset to `now`) is
        //    robust to RAF jitter -- the old "time since last step" test dropped ~1 in 6 frames.
        const target = this._targetFps();
        if (target > 0) {
            const interval = 1000 / target;
            if (this._nextStep === undefined) this._nextStep = now;
            if (now < this._nextStep - 1) {                       // not due yet (1ms slop)
                this.animFrameId = requestAnimationFrame(() => this._frame());
                return;
            }
            this._nextStep += interval;
            if (this._nextStep < now) this._nextStep = now + interval;  // resync if we fell behind
        } else {
            this._nextStep = undefined;                          // uncapped: step every RAF
        }

        // 1. Input + 2. run one CPU budget's worth of instructions.
        const { INPUT_ADDRESS } = window.EMU_CONSTANTS;
        this.cpu.memory[INPUT_ADDRESS] = this.input.getState();
        const stillRunning = this.cpu.runBatch(this.instructionsPerFrame);
        const presented = this.cpu.presentPending();

        // 3. Redraw only on a freshly composed frame (matches real-hardware cadence; if the budget cut
        //    the frame short, the display holds the last complete frame -- no tearing).
        if (presented) { this._renderFrame(); this._presents++; this._checkPaletteWarning(); }

        // 4. Live stats (effective FPS / instr-per-sec / instr-per-frame), refreshed ~2x/sec.
        this._sampleStats(now);

        // 5. Halt / hot-swap. A present is not a halt; a genuine HLT (or LOAD_ROM/RESET syscall) stops
        //    the CPU without presenting.
        if (!stillRunning && !presented) {
            const n = this.cpu.pendingRom();            // -1 none | >=0 game | -2 reset->menu
            if (n >= 0) {
                this._bootFromLibrary(n);               // LOAD_ROM
            } else if (n === -2) {
                this._bootFromLibrary(this.menuIndex >= 0 ? this.menuIndex : 0);   // RESET -> menu
            } else {
                this.isRunning = false;                 // genuine HLT
                this._renderFrame();
                this.debug.update(this.cpu, this.isRunning);
                this._updateControlState();
                this._showStatus('CPU halted', 'warning');
                return;
            }
        }

        // 6. Next frame.
        this.animFrameId = requestAnimationFrame(() => this._frame());
    }

    // Roll up perf counters over a ~500ms window and push them to the UI + debug panel.
    _sampleStats(now) {
        const elapsed = now - this._statStart;
        if (elapsed < this.statsInterval) return;

        const instrNow = this.cpu.instructionCount;
        const dInstr = (instrNow - this._statInstr) >>> 0;       // uint32 delta (survives one wrap)
        const dPresents = this._presents - this._statPresents;

        this.fps = Math.round((dPresents * 1000) / elapsed);
        this.ips = Math.round((dInstr * 1000) / elapsed);
        this.ipf = dPresents > 0 ? Math.round(dInstr / dPresents) : 0;

        this._statStart = now;
        this._statInstr = instrNow;
        this._statPresents = this._presents;

        this._updateStatsUI();
        this.debug.update(this.cpu, this.isRunning);
    }

    _updateStatsUI() {
        const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
        const cap = this._targetFps();
        set('fps-counter', `${this.fps} FPS`);
        set('stat-fps', String(this.fps));
        set('stat-fps-target', cap === 0 ? 'uncapped' : String(cap));
        set('stat-ips', this._fmt(this.ips) + '/s');
        set('stat-ipf', this._fmt(this.ipf) + '/f');
        set('stat-budget', this._fmt(this.instructionsPerFrame) + '/f');
        // Audio (Phase 2 prototype): buffer depth (the real worklet queue, in ms) is the latency
        // knob's effect; underruns is the glitch counter. Watch both while dragging the CPU-budget
        // slider down to stress-test streaming; lower app.audio.setTargetMs(N) to reduce latency.
        set('stat-audio-buffer', this.audio.ready ? `${this.audio.bufferMs}ms` : 'off');
        set('stat-audio-underruns', this.audio.ready ? String(this.audio.underruns) : '-');
    }

    // --- UI helpers ---

    _updateControlState() {
        const runBtn = document.getElementById('btn-run');
        const pauseBtn = document.getElementById('btn-pause');

        if (runBtn) runBtn.classList.toggle('hidden', this.isRunning);
        if (pauseBtn) pauseBtn.classList.toggle('hidden', !this.isRunning);
    }

    _showStatus(message, type = 'info') {
        const el = document.getElementById('status-message');
        if (el) {
            el.textContent = message;
            el.className = `status-msg ${type}`;
            // Auto-fade
            clearTimeout(this._statusTimeout);
            this._statusTimeout = setTimeout(() => {
                el.classList.add('fade');
            }, 3000);
        }
    }
}

// Boot: instantiate the WASM emulator core, then start the app.
// locateFile cache-busts the emu.wasm fetch (Date.now() -> a fresh URL every page load). This project
// rebuilds the WASM core often; without this, a stale cached emu.wasm can silently run against ROMs
// compiled for a newer PPU memory layout (e.g. a shifted PPU_PAL address), producing a solid-black
// screen with no error. The <script src="emu.js"> tag itself is cache-busted the same way in
// index.html -- both must be busted, since emu.js's own wasm fetch ignores its own script tag's query.
document.addEventListener('DOMContentLoaded', () => {
    createEmu({ locateFile: (path) => path + '?v=' + Date.now() }).then((module) => {
        const core = new EmuCore(module);
        core.initialize();
        window.app = new App(core);
    }).catch((err) => {
        console.error('Failed to load emulator WASM:', err);
        const el = document.getElementById('status-message');
        if (el) {
            el.textContent = 'Failed to load emulator core (emu.wasm). Serve over http://, not file://.';
            el.className = 'status-msg error';
        }
    });
});
