/**
 * Input Handler — Keyboard → Gamepad Bitmask
 * 
 * Replicates get_input() from ESP32 main.cpp:
 *   Bit 0 (0x01): Left   → ArrowLeft
 *   Bit 1 (0x02): Up     → ArrowUp
 *   Bit 2 (0x04): Right  → ArrowRight
 *   Bit 3 (0x08): Down   → ArrowDown
 *   Bit 4 (0x10): A      → Z
 *   Bit 5 (0x20): B      → X
 *   Bit 6 (0x40): X      → A
 *   Bit 7 (0x80): Y      → S
 */

class InputHandler {
    constructor() {
        // Current button bitmask
        this.state = 0;

        // Key → bit mapping
        this.keyMap = {
            'ArrowLeft':  0x01,
            'ArrowUp':    0x02,
            'ArrowRight': 0x04,
            'ArrowDown':  0x08,
            'z':          0x10,
            'Z':          0x10,
            'x':          0x20,
            'X':          0x20,
            'a':          0x40,
            'A':          0x40,
            's':          0x80,
            'S':          0x80,
        };

        // Button display names for the UI
        this.buttons = [
            { name: '←',  bit: 0x01, key: '←' },
            { name: '↑',  bit: 0x02, key: '↑' },
            { name: '→',  bit: 0x04, key: '→' },
            { name: '↓',  bit: 0x08, key: '↓' },
            { name: 'A',  bit: 0x10, key: 'Z' },
            { name: 'B',  bit: 0x20, key: 'X' },
            { name: 'X',  bit: 0x40, key: 'A' },
            { name: 'Y',  bit: 0x80, key: 'S' },
        ];

        this._bindEvents();
    }

    _bindEvents() {
        document.addEventListener('keydown', (e) => {
            const bit = this.keyMap[e.key];
            if (bit !== undefined) {
                this.state |= bit;
                e.preventDefault();
                this._updateUI();
            }
        });

        document.addEventListener('keyup', (e) => {
            const bit = this.keyMap[e.key];
            if (bit !== undefined) {
                this.state &= ~bit;
                e.preventDefault();
                this._updateUI();
            }
        });

        // Handle losing focus — clear all input
        window.addEventListener('blur', () => {
            this.state = 0;
            this._updateUI();
        });
    }

    /**
     * Get the current input state byte
     */
    getState() {
        return this.state;
    }

    /**
     * Update the on-screen button indicators
     */
    _updateUI() {
        for (const btn of this.buttons) {
            const el = document.getElementById(`btn-${btn.name}`);
            if (el) {
                if (this.state & btn.bit) {
                    el.classList.add('active');
                } else {
                    el.classList.remove('active');
                }
            }
        }

        // Update hex display
        const hexEl = document.getElementById('input-hex');
        if (hexEl) {
            hexEl.textContent = '0x' + this.state.toString(16).toUpperCase().padStart(2, '0');
        }
    }
}

window.InputHandler = InputHandler;
