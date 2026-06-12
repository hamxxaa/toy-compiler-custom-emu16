/**
 * App Controller — Main Simulation Loop
 * 
 * Orchestrates the exact ESP32 loop() sequence:
 *   1. Write input state to memory[0xADFF]
 *   2. Run CPU batch (100k instructions)
 *   3. Build framebuffer from VRAM+PRAM and render to canvas
 *   4. Update debug panel
 *   5. Sync to ~60 FPS via requestAnimationFrame
 */

class App {
    constructor(core) {
        this.cpu = core;
        this.display = new Display('screen');
        this.input = new InputHandler();
        this.debug = new DebugPanel();

        // Simulation state
        this.isRunning = false;
        this.animFrameId = null;
        this.romLoaded = false;
        this.romData = null;

        // Performance tracking
        this.lastFrameTime = 0;
        this.fps = 0;
        this.frameCount = 0;
        this.fpsUpdateInterval = 500;  // ms
        this.lastFpsUpdate = 0;

        // Speed control
        this.instructionsPerFrame = 100000;

        this._bindControls();
        this._bindROMLoader();
        this._bindSpeedControl();

        // Initial debug update (core already initialized by the bootstrap).
        this.debug.update(this.cpu);
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
                const file = e.dataTransfer.files[0];
                if (file) this._loadROMFile(file);
            });

            // Click to open file dialog
            dropZone.addEventListener('click', () => {
                if (fileInput) fileInput.click();
            });
        }

        // File input change
        if (fileInput) {
            fileInput.addEventListener('change', (e) => {
                const file = e.target.files[0];
                if (file) this._loadROMFile(file);
            });
        }
    }

    _loadROMFile(file) {
        const reader = new FileReader();
        reader.onload = (e) => {
            const bytes = new Uint8Array(e.target.result);
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

        // Store ROM data for reset
        this.romData = romBytes;

        // Reset core: zeroes memory, lays down default palette, clears VRAM/input (all in C).
        this.cpu.initialize();

        // Load ROM into guest memory at address 0 (after init, which zeroes everything).
        this.cpu.memory.set(romBytes, 0);

        this.romLoaded = true;
        this._showStatus(`Loaded: ${name} (${romBytes.length} bytes)`, 'success');
        
        // Update UI
        this.display.render(this.cpu.memory);
        this.debug.update(this.cpu);

        // Update drop zone appearance
        const dropZone = document.getElementById('drop-zone');
        if (dropZone) {
            dropZone.classList.add('loaded');
            dropZone.querySelector('.drop-text').textContent = `${name} (${romBytes.length}B)`;
        }

        // Enable buttons
        document.querySelectorAll('.ctrl-btn').forEach(b => b.disabled = false);
    }

    // --- Controls ---

    _bindControls() {
        document.getElementById('btn-run')?.addEventListener('click', () => this.run());
        document.getElementById('btn-pause')?.addEventListener('click', () => this.pause());
        document.getElementById('btn-step')?.addEventListener('click', () => this.stepFrame());
        document.getElementById('btn-reset')?.addEventListener('click', () => this.reset());
    }

    _bindSpeedControl() {
        const slider = document.getElementById('speed-slider');
        const label = document.getElementById('speed-label');
        if (slider) {
            slider.addEventListener('input', (e) => {
                const val = parseInt(e.target.value);
                // Exponential scale: 1k to 1M
                this.instructionsPerFrame = Math.pow(10, 3 + val * 3 / 100);
                if (label) {
                    if (this.instructionsPerFrame >= 1000000) {
                        label.textContent = (this.instructionsPerFrame / 1000000).toFixed(1) + 'M';
                    } else {
                        label.textContent = Math.round(this.instructionsPerFrame / 1000).toFixed(0) + 'K';
                    }
                }
            });
        }
    }

    run() {
        if (!this.romLoaded) return;
        if (this.isRunning) return;
        
        this.isRunning = true;
        this._updateControlState();
        this.lastFrameTime = performance.now();
        this.lastFpsUpdate = this.lastFrameTime;
        this.frameCount = 0;
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

    stepFrame() {
        if (!this.romLoaded) return;
        this.pause();

        // Run one batch
        const { INPUT_ADDRESS } = window.EMU_CONSTANTS;
        this.cpu.memory[INPUT_ADDRESS] = this.input.getState();
        this.cpu.runBatch(this.instructionsPerFrame);
        this.display.render(this.cpu.memory);
        this.debug.update(this.cpu);

        if (!this.cpu.running) {
            this._showStatus('CPU halted', 'warning');
        }
    }

    reset() {
        this.pause();
        if (this.romData) {
            this._loadROM(this.romData, document.querySelector('.drop-text')?.textContent || 'ROM');
        }
    }

    // --- Main loop ---

    _frame() {
        if (!this.isRunning) return;

        const now = performance.now();

        // 1. Write input state to memory
        const { INPUT_ADDRESS } = window.EMU_CONSTANTS;
        this.cpu.memory[INPUT_ADDRESS] = this.input.getState();

        // 2. Run CPU batch
        const stillRunning = this.cpu.runBatch(this.instructionsPerFrame);

        // 3. Render display
        this.display.render(this.cpu.memory);

        // 4. Update debug panel (throttled to avoid DOM thrashing)
        this.frameCount++;
        if (now - this.lastFpsUpdate >= this.fpsUpdateInterval) {
            this.fps = Math.round((this.frameCount * 1000) / (now - this.lastFpsUpdate));
            this.lastFpsUpdate = now;
            this.frameCount = 0;
            this.debug.update(this.cpu);

            const fpsEl = document.getElementById('fps-counter');
            if (fpsEl) fpsEl.textContent = `${this.fps} FPS`;
        }

        // 5. Check if CPU halted
        if (!stillRunning) {
            this.isRunning = false;
            this.debug.update(this.cpu);
            this._updateControlState();
            this._showStatus('CPU halted', 'warning');
            return;
        }

        // 6. Next frame
        this.animFrameId = requestAnimationFrame(() => this._frame());
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
document.addEventListener('DOMContentLoaded', () => {
    createEmu().then((module) => {
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
