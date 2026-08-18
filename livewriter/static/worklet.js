/* Mic capture worklet: resample the context rate down to 24 kHz mono pcm16
   and post 480-sample (20 ms) Int16 frames — the exact format the realtime
   transcription session expects. Linear interpolation is plenty for speech. */
class PCM16 extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 24000;
    this.pos = 0;          // fractional read position into the stream
    this.tail = null;      // last input sample carried across process() calls
    this.out = new Int16Array(480);
    this.outN = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch || !ch.length) return true;
    let src = ch;
    if (this.tail !== null) {
      src = new Float32Array(ch.length + 1);
      src[0] = this.tail;
      src.set(ch, 1);
    }
    const last = src.length - 1;
    while (this.pos < last) {
      const i = Math.floor(this.pos);
      const f = this.pos - i;
      const s = src[i] + (src[i + 1] - src[i]) * f;
      const v = Math.max(-1, Math.min(1, s));
      this.out[this.outN++] = v < 0 ? v * 0x8000 : v * 0x7fff;
      if (this.outN === 480) {
        this.port.postMessage(this.out.buffer.slice(0));
        this.outN = 0;
      }
      this.pos += this.ratio;
    }
    this.pos -= last;
    this.tail = src[last];
    return true;
  }
}
registerProcessor('pcm16', PCM16);
