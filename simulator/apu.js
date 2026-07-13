/**
 * AudioEngine — main-thread half of the APU streaming path (Phase 2, see
 * plans/buzzy-streaming-tanaka.md). Owns the AudioContext + AudioWorkletNode.
 *
 * PULL MODEL (v2 rewrite — see the plan's "Bug #3"). The worklet drives everything; this class is
 * now almost stateless. It does exactly three things:
 *   1. sets up the AudioContext + worklet (once, on a user gesture);
 *   2. answers the worklet's 'need' requests by rendering that many samples from the APU's CURRENT
 *      voice state and sending them back;
 *   3. surfaces the worklet's reported queue depth + underrun count for the UI.
 *
 * There is deliberately NO push cadence, NO ctx.currentTime scheduling, NO pump() called from the
 * RAF loop anymore. The previous versions tried to DECIDE how much to buffer from the main thread
 * and got it wrong in both directions (stale-reading underruns, then unbounded overfill that put
 * playback minutes behind the game). The worklet, which has zero-lag knowledge of its own buffer,
 * is the only thing that decides now; this class just serves. Because the worklet only ever requests
 * what its bounded buffer can hold, runaway growth is structurally impossible.
 *
 * targetMs is the latency knob: the worklet keeps ~this many ms queued. Lower it (setTargetMs, or
 * app.audio.setTargetMs(N) from the console) to test how low the latency can go before underruns
 * appear -- that number is the real answer this whole prototype exists to find.
 */
class AudioEngine {
    constructor(core) {
        this.core = core;          // EmuCore -- apuRate() / apuRender(n)
        this.ctx = null;
        this.node = null;
        this.ready = false;
        this._starting = false;

        this.targetMs = 120;       // buffer depth the worklet maintains (the latency knob)
        this._maxChunk = 4096;     // must match APU_RENDER_CAP in emulator/emu_wasm.cpp

        // Diagnostics, reported by the worklet (the single source of truth now -- no second number).
        this.queuedMs = 0;
        this.underruns = 0;
    }

    _targetSamples() {
        const rate = this.ctx ? this.ctx.sampleRate : this.core.apuRate();
        return Math.round((this.targetMs / 1000) * rate);
    }

    // Must be called from within a user-gesture call stack (autoplay policy) -- app.js calls this
    // from run(), a button click handler. Safe to call repeatedly; only sets up once.
    async ensureStarted() {
        if (this.ready) {
            if (this.ctx.state === 'suspended') await this.ctx.resume();
            return;
        }
        if (this._starting) return;
        this._starting = true;
        try {
            const AC = window.AudioContext || window.webkitAudioContext;
            // Request the synth's native rate directly; the browser resamples the final output to
            // hardware rate transparently.
            this.ctx = new AC({ sampleRate: this.core.apuRate() });
            if (this.ctx.sampleRate !== this.core.apuRate()) {
                console.warn(`AudioEngine: requested ${this.core.apuRate()}Hz, browser gave ` +
                    `${this.ctx.sampleRate}Hz -- playback will be pitched/sped by ` +
                    `${(this.ctx.sampleRate / this.core.apuRate()).toFixed(3)}x.`);
            }
            await this.ctx.audioWorklet.addModule('apu-worklet.js?v=' + Date.now());
            this.node = new AudioWorkletNode(this.ctx, 'apu-processor', {
                numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
                processorOptions: { targetSamples: this._targetSamples() },
            });
            this.node.port.onmessage = (e) => this._onMessage(e.data);
            this.node.connect(this.ctx.destination);

            // Prime the buffer so the first few audio quanta aren't silence-with-an-underrun while
            // the first request round-trips. Voice state is silent this early anyway (no notes yet),
            // so this is just zero-filled headroom.
            const primed = this._render(this._targetSamples());
            if (primed) this._send(primed);

            this.ready = true;
        } catch (err) {
            console.error('AudioEngine: failed to start:', err);
        }
        this._starting = false;
    }

    _onMessage(m) {
        if (m.type === 'need') {
            // The worklet is short by m.n samples. Render exactly that (capped) from the APU's
            // CURRENT voice state and send it straight back. No scheduling, no clock -- the worklet
            // asked because IT knows it needs them, right now.
            const f32 = this._render(m.n);
            if (f32) this._send(f32);
        } else if (m.type === 'stat') {
            this.underruns = m.underruns;
            this.queuedMs = this.ctx ? Math.round((m.queued / this.ctx.sampleRate) * 1000) : 0;
        }
    }

    // Render up to `n` samples (capped at the C-side static buffer) into a fresh Float32Array, or
    // null if there's nothing to send.
    _render(n) {
        n = Math.min(n | 0, this._maxChunk);
        if (n <= 0) return null;
        const int16 = this.core.apuRender(n);
        if (int16.length === 0) return null;
        const f32 = new Float32Array(int16.length);
        for (let i = 0; i < int16.length; i++) f32[i] = int16[i] / 32768;
        return f32;
    }

    _send(f32) {
        this.node.port.postMessage({ type: 'push', samples: f32 }, [f32.buffer]);
    }

    // Drop whatever's queued (e.g. on ROM boot/reset) so stale audio from a previous ROM doesn't
    // bleed into the new one. The worklet will re-request from empty on its next process().
    reset() {
        if (this.node) this.node.port.postMessage({ type: 'reset' });
        this.queuedMs = 0;
        this.underruns = 0;
    }

    // Live change to the latency knob (e.g. app.audio.setTargetMs(50) from the console during the
    // latency test). Pushes the new target down to the worklet, which is what actually enforces it.
    setTargetMs(ms) {
        this.targetMs = ms;
        if (this.ready) this.node.port.postMessage({ type: 'config', targetSamples: this._targetSamples() });
    }

    // The real queued depth (ms), straight from the worklet. One honest number.
    get bufferMs() { return this.queuedMs; }
}

window.AudioEngine = AudioEngine;
