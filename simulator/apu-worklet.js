/**
 * APU AudioWorkletProcessor — the browser's audio-rate consumer AND the driver of the whole
 * streaming pipeline (Phase 2, see plans/buzzy-streaming-tanaka.md). Runs on the browser's
 * dedicated, high-priority audio thread, separate from the main thread that runs the emulator.
 *
 * PULL MODEL (this is the v2 rewrite — see the plan's "Bug #3" for why the old push model was
 * scrapped). This processor OWNS the buffer and decides when to refill it:
 *   - process() drains its queue to the output; an empty queue outputs silence and counts an
 *     underrun (the glitch signal the CPU-budget stress test watches).
 *   - after draining, if the queue has fallen below `targetSamples` and no refill is already in
 *     flight, it asks the main thread for exactly the shortfall via a 'need' message.
 *   - the main thread answers with a 'push' of freshly-rendered samples; on receipt the processor
 *     enqueues them and clears the in-flight flag.
 *
 * Why this can't break the way the push model did: the processor only ever REQUESTS what its own
 * bounded buffer can hold, so the queue can never grow without limit (the old bug: main thread
 * pushing faster than playback → hundreds of seconds of backlog → audio minutes behind the game).
 * And because the processor has zero-lag knowledge of its own fill level (it's the one draining
 * it), there's no stale cross-thread reading to mis-trust (the even-older bug). The main thread
 * does no clock math at all now; it just serves requests.
 *
 * No SharedArrayBuffer: served over plain http (no COOP/COEP headers), and not needed — only
 * finished immutable Float32Array chunks cross the boundary (transferred, not copied).
 */
class APUProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        const opt = (options && options.processorOptions) || {};
        this.targetSamples = opt.targetSamples || 2646;   // buffer depth to maintain (the latency knob)

        this.queue = [];          // FIFO of Float32Array chunks awaiting playback
        this.offset = 0;          // read offset into queue[0]
        this.queued = 0;          // total samples still buffered across all chunks
        this.underruns = 0;       // samples output as silence because the queue ran dry
        this.inFlight = false;    // a 'need' request is outstanding (awaiting its 'push')
        this.requestedAt = -1;    // currentTime the outstanding request was sent (for staleness)
        this._staleSec = 0.25;    // re-arm a request unanswered this long (a dropped/lost message)
        this._sinceStat = 0;

        this.port.onmessage = (e) => {
            const m = e.data;
            if (m.type === 'push') {
                this.inFlight = false;   // request answered (whether we keep the samples or not)
                // Overflow guard: only accept a push if we're still below target. This makes the
                // queue robust to being OVER-served -- e.g. a main-thread freeze-then-thaw flushing
                // several piled-up 'need' requests at once (each re-armed by the staleness timeout
                // below). Redundant pushes are simply dropped, so the queue can never exceed ~target
                // + one chunk no matter how many arrive. Bounded latency, always.
                if (this.queued < this.targetSamples) {
                    this.queue.push(m.samples);
                    this.queued += m.samples.length;
                }
            } else if (m.type === 'reset') {
                this.queue = []; this.offset = 0; this.queued = 0;
                this.underruns = 0; this.inFlight = false; this.requestedAt = -1;
            } else if (m.type === 'config') {
                this.targetSamples = m.targetSamples;   // live latency-knob change
            }
        };
    }

    process(inputs, outputs) {
        const out = outputs[0][0];   // mono
        for (let i = 0; i < out.length; i++) {
            if (this.queue.length === 0) {
                out[i] = 0;
                this.underruns++;
                continue;
            }
            const chunk = this.queue[0];
            out[i] = chunk[this.offset++];
            this.queued--;
            if (this.offset >= chunk.length) {
                this.queue.shift();
                this.offset = 0;
            }
        }

        // Refill request: when below target and either nothing is on the way, OR the outstanding
        // request has gone unanswered longer than _staleSec (a dropped/lost message -- otherwise
        // inFlight would wedge true forever and audio would deadlock). The in-flight flag caps the
        // normal request rate to the round-trip rate (one request, one push, repeat) so it can't spam
        // a request every 128-sample quantum; the staleness escape hatch re-arms at most once per
        // _staleSec, so even a freeze-then-thaw can't pile up thousands of requests. `currentTime` is
        // an AudioWorkletGlobalScope global (seconds), advancing on the audio thread independently of
        // whether the main thread is stalled.
        if (this.queued < this.targetSamples) {
            const stale = this.inFlight && (currentTime - this.requestedAt) > this._staleSec;
            if (!this.inFlight || stale) {
                this.inFlight = true;
                this.requestedAt = currentTime;
                this.port.postMessage({ type: 'need', n: this.targetSamples - this.queued });
            }
        }

        // Periodic diagnostic for the UI (the REAL queue depth + underrun count, straight from the
        // thread that owns them -- no second, possibly-disagreeing number anywhere anymore).
        if (++this._sinceStat >= 8) {
            this._sinceStat = 0;
            this.port.postMessage({ type: 'stat', queued: this.queued, underruns: this.underruns });
        }

        return true;   // keep the processor alive regardless of queue state
    }
}

registerProcessor('apu-processor', APUProcessor);
