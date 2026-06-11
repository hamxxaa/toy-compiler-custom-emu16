/**
 * Display Renderer — VRAM + Palette RAM → Canvas
 * 
 * Replicates the framebuffer building from ESP32 main.cpp loop():
 *   1. Read 256-entry palette from PRAM (RGB565, little-endian)
 *   2. For each VRAM byte: lookup palette[color_index] → RGB565
 *   3. Convert RGB565 → RGB888 → canvas pixel
 */

class Display {
    constructor(canvasId) {
        this.canvas = document.getElementById(canvasId);
        this.ctx = this.canvas.getContext('2d');
        
        const { SCREEN_WIDTH, SCREEN_HEIGHT } = window.EMU_CONSTANTS;
        
        // Set canvas internal resolution to native 160×128
        this.canvas.width = SCREEN_WIDTH;
        this.canvas.height = SCREEN_HEIGHT;
        
        // ImageData for direct pixel manipulation
        this.imageData = this.ctx.createImageData(SCREEN_WIDTH, SCREEN_HEIGHT);
        this.pixels = this.imageData.data;  // Uint8ClampedArray (RGBA)
        
        // Clear to black
        this.clear();
    }

    /**
     * Expand 5-bit color component to 8-bit (matches expand_5_to_8 in pc_emulator_main.cpp)
     */
    static expand5to8(value) {
        return ((value << 3) | (value >> 2)) & 0xFF;
    }

    /**
     * Expand 6-bit color component to 8-bit (matches expand_6_to_8 in pc_emulator_main.cpp)
     */
    static expand6to8(value) {
        return ((value << 2) | (value >> 4)) & 0xFF;
    }

    /**
     * Convert RGB565 to [R, G, B] array (8-bit per channel)
     */
    static rgb565toRGB888(rgb565) {
        const r5 = (rgb565 >> 11) & 0x1F;
        const g6 = (rgb565 >> 5) & 0x3F;
        const b5 = rgb565 & 0x1F;
        return [
            Display.expand5to8(r5),
            Display.expand6to8(g6),
            Display.expand5to8(b5)
        ];
    }

    /**
     * Render the current VRAM state to the canvas.
     * Reads directly from CPU memory.
     */
    render(memory) {
        const { VRAM_START_ADDRESS, PRAM_START_ADDRESS, VRAM_SIZE } = window.EMU_CONSTANTS;

        // 1. Build palette from PRAM (256 entries, each 2 bytes LE → RGB565)
        const palette = new Uint16Array(256);
        for (let i = 0; i < 256; i++) {
            const lo = memory[PRAM_START_ADDRESS + (i * 2)];
            const hi = memory[PRAM_START_ADDRESS + (i * 2) + 1];
            palette[i] = lo | (hi << 8);
        }

        // 2. Map VRAM → palette → RGB888 → canvas pixels
        for (let i = 0; i < VRAM_SIZE; i++) {
            const colorIndex = memory[VRAM_START_ADDRESS + i];
            const rgb565 = palette[colorIndex];
            const [r, g, b] = Display.rgb565toRGB888(rgb565);

            const pixelOffset = i * 4;
            this.pixels[pixelOffset]     = r;
            this.pixels[pixelOffset + 1] = g;
            this.pixels[pixelOffset + 2] = b;
            this.pixels[pixelOffset + 3] = 255;  // Alpha = opaque
        }

        // 3. Draw to canvas
        this.ctx.putImageData(this.imageData, 0, 0);
    }

    /**
     * Clear the display to black
     */
    clear() {
        for (let i = 0; i < this.pixels.length; i += 4) {
            this.pixels[i]     = 0;
            this.pixels[i + 1] = 0;
            this.pixels[i + 2] = 0;
            this.pixels[i + 3] = 255;
        }
        this.ctx.putImageData(this.imageData, 0, 0);
    }
}

window.Display = Display;
