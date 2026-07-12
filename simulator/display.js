/**
 * Display Renderer — the PPU's composed framebuffer → Canvas
 *
 * The PPU composites tiles/sprites/text and converts through its own palette in C
 * (ppu_convert_rgb565); this class just expands the resulting RGB565 pixels to RGB888
 * and blits them to the canvas. There is no CPU-side VRAM/PRAM path anymore.
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
     * Render a pre-composed RGB565 framebuffer straight to the canvas. `fb565` is a
     * Uint16Array of SCREEN_WIDTH*SCREEN_HEIGHT already-palette-mapped colors — the PPU
     * did the compositing in C, this just expands and blits.
     */
    renderPPU(fb565) {
        const PIXEL_COUNT = fb565.length;
        for (let i = 0; i < PIXEL_COUNT; i++) {
            const [r, g, b] = Display.rgb565toRGB888(fb565[i]);
            const o = i * 4;
            this.pixels[o]     = r;
            this.pixels[o + 1] = g;
            this.pixels[o + 2] = b;
            this.pixels[o + 3] = 255;
        }
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
