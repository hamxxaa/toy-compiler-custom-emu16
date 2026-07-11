/**
 * Debug Panel — Real-time CPU state visualization
 * 
 * Shows registers, flags, PC, SP, instruction count,
 * and a memory viewer.
 */

class DebugPanel {
    constructor() {
        this.memoryViewAddress = 0x0000;
        this.memoryViewSize = 256;  // bytes to show
        this.ppuViewAddress = (window.EMU_CONSTANTS.PPU && window.EMU_CONSTANTS.PPU.TILEMAP) || 0x8400;
        this._setupMemoryViewer();
        this._setupPPUMemoryViewer();
    }

    _setupMemoryViewer() {
        const addrInput = document.getElementById('mem-address');
        if (addrInput) {
            addrInput.addEventListener('change', (e) => {
                const val = parseInt(e.target.value, 16);
                if (!isNaN(val) && val >= 0 && val <= 0xFFFF) {
                    this.memoryViewAddress = val;
                }
            });
            addrInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    const val = parseInt(e.target.value, 16);
                    if (!isNaN(val) && val >= 0 && val <= 0xFFFF) {
                        this.memoryViewAddress = val;
                    }
                }
            });
        }

        // Quick-jump buttons
        const jumpBtns = document.querySelectorAll('.mem-jump');
        jumpBtns.forEach(btn => {
            btn.addEventListener('click', () => {
                const addr = parseInt(btn.dataset.addr, 16);
                this.memoryViewAddress = addr;
                if (addrInput) addrInput.value = addr.toString(16).toUpperCase().padStart(4, '0');
            });
        });
    }

    _setupPPUMemoryViewer() {
        const addrInput = document.getElementById('ppu-mem-address');
        const apply = (raw) => {
            const val = parseInt(raw, 16);
            if (!isNaN(val) && val >= 0 && val <= 0xFFFF) this.ppuViewAddress = val;
        };
        if (addrInput) {
            addrInput.addEventListener('change', (e) => apply(e.target.value));
            addrInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') apply(e.target.value); });
        }
        document.querySelectorAll('.ppu-mem-jump').forEach(btn => {
            btn.addEventListener('click', () => {
                this.ppuViewAddress = parseInt(btn.dataset.addr, 16);
                if (addrInput) addrInput.value = this.ppuViewAddress.toString(16).toUpperCase().padStart(4, '0');
            });
        });
    }

    /**
     * Update all debug displays. `isRunning` is the App's sim-loop flag; when given, it drives the
     * RUNNING/HALTED label instead of cpu.running. The raw core flag is legitimately false in the
     * instant right after every PRESENT yield (it resumes on the next runBatch), so using it
     * directly made the status flicker to HALTED on almost every sample even while the game runs
     * fine -- isRunning reflects "is the app still driving frames", which is what this label means.
     */
    update(cpu, isRunning) {
        this._updateRegisters(cpu);
        this._updateFlags(cpu);
        this._updateStatus(cpu, isRunning);
        this._updateMemory(cpu);
        this._updatePPU(cpu);
    }

    _updateRegisters(cpu) {
        const regNames = ['R0', 'R1', 'R2', 'R3', 'R4', 'R5', 'R6 (FP)', 'R7 (SP)'];
        for (let i = 0; i < 8; i++) {
            const el = document.getElementById(`reg-r${i}`);
            if (el) {
                const val = cpu.getRegWord(i);
                el.textContent = '0x' + val.toString(16).toUpperCase().padStart(4, '0');
            }
        }

        // PC
        const pcEl = document.getElementById('reg-pc');
        if (pcEl) {
            pcEl.textContent = '0x' + cpu.pc.toString(16).toUpperCase().padStart(4, '0');
        }
    }

    _updateFlags(cpu) {
        const zeroEl = document.getElementById('flag-zero');
        const signEl = document.getElementById('flag-sign');
        const carryEl = document.getElementById('flag-carry');

        if (zeroEl)  this._setFlagState(zeroEl,  cpu.flags & 0x01);
        if (signEl)  this._setFlagState(signEl,  cpu.flags & 0x02);
        if (carryEl) this._setFlagState(carryEl, cpu.flags & 0x04);
    }

    _setFlagState(el, active) {
        if (active) {
            el.classList.add('active');
            el.classList.remove('inactive');
        } else {
            el.classList.remove('active');
            el.classList.add('inactive');
        }
    }

    _updateStatus(cpu, isRunning) {
        const statusEl = document.getElementById('cpu-status');
        if (statusEl) {
            const running = isRunning !== undefined ? isRunning : cpu.running;
            if (running) {
                statusEl.textContent = 'RUNNING';
                statusEl.className = 'status-value running';
            } else {
                statusEl.textContent = 'HALTED';
                statusEl.className = 'status-value halted';
            }
        }

        const instrEl = document.getElementById('instr-count');
        if (instrEl) {
            instrEl.textContent = cpu.instructionCount.toLocaleString();
        }
    }

    _updateMemory(cpu) {
        const memEl = document.getElementById('mem-viewer');
        if (!memEl) return;

        const startAddr = this.memoryViewAddress & 0xFFF0;  // Align to 16
        const lines = [];
        
        for (let row = 0; row < 16; row++) {
            const addr = (startAddr + row * 16) & 0xFFFF;
            let hex = '';
            let ascii = '';
            
            for (let col = 0; col < 16; col++) {
                const byteAddr = (addr + col) & 0xFFFF;
                const byte = cpu.memory[byteAddr];
                hex += byte.toString(16).toUpperCase().padStart(2, '0');
                if (col < 15) hex += ' ';
                ascii += (byte >= 0x20 && byte <= 0x7E) ? String.fromCharCode(byte) : '.';
            }
            
            const addrStr = addr.toString(16).toUpperCase().padStart(4, '0');
            lines.push(`${addrStr}  ${hex}  ${ascii}`);
        }

        memEl.textContent = lines.join('\n');
    }

    /**
     * PPU state (scroll + sprite count, decoded from the REGS region) and the PPU RAM hex viewer.
     * Reads the PPU's own graphics RAM (separate from CPU memory) via cpu.ppuMem().
     */
    _updatePPU(cpu) {
        const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
        const mem = cpu.ppuMem ? cpu.ppuMem() : null;
        if (!mem) {
            set('ppu-scroll-x', '—'); set('ppu-scroll-y', '—'); set('ppu-oam-count', '—');
            return;
        }
        const C = window.EMU_CONSTANTS.PPU;
        const rd16 = (a) => mem[a] | (mem[a + 1] << 8);
        set('ppu-scroll-x', rd16(C.REG_SCROLL_X));
        set('ppu-scroll-y', rd16(C.REG_SCROLL_Y));
        set('ppu-oam-count', mem[C.REG_OAM_COUNT]);

        const el = document.getElementById('ppu-mem-viewer');
        if (!el) return;
        const size = mem.length;
        const start = this.ppuViewAddress & 0xFFF0;
        const lines = [];
        for (let row = 0; row < 16; row++) {
            const addr = start + row * 16;
            if (addr >= size) break;
            let hex = '', ascii = '';
            for (let col = 0; col < 16; col++) {
                const a = addr + col;
                const byte = a < size ? mem[a] : 0;
                hex += byte.toString(16).toUpperCase().padStart(2, '0');
                if (col < 15) hex += ' ';
                ascii += (byte >= 0x20 && byte <= 0x7E) ? String.fromCharCode(byte) : '.';
            }
            lines.push(`${addr.toString(16).toUpperCase().padStart(4, '0')}  ${hex}  ${ascii}`);
        }
        el.textContent = lines.join('\n');
    }

    /**
     * Jump memory viewer to a specific address
     */
    jumpTo(address) {
        this.memoryViewAddress = address & 0xFFFF;
        const addrInput = document.getElementById('mem-address');
        if (addrInput) {
            addrInput.value = address.toString(16).toUpperCase().padStart(4, '0');
        }
    }
}

window.DebugPanel = DebugPanel;
