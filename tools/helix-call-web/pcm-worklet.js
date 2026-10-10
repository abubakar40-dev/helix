/* 16 kHz mono PCM16 capture and playback for the HELIX call.
   The box relays PCM to Gemini, so no WebRTC media stack is needed anywhere. */

class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Float32Array(4096);
    this.n = 0;
  }
  process(inputs) {
    const ch = inputs[0][0];
    if (!ch) return true;
    this.buf.set(ch, this.n);
    this.n += ch.length;
    if (this.n >= this.buf.length) {
      // 16 kHz PCM16, little endian
      const out = new Int16Array(this.buf.length);
      for (let i = 0; i < this.buf.length; i++) {
        const s = Math.max(-1, Math.min(1, this.buf[i]));
        out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(out.buffer, [out.buffer]);
      this.n = 0;
    }
    return true;
  }
}
registerProcessor("capture", Capture);

class Playback extends AudioWorkletProcessor {
  constructor() {
    super();
    this.q = [];
    this.cur = null;
    this.idx = 0;
    this.port.onmessage = (e) => {
      const pcm = new Int16Array(e.data);
      // stitch chunks without clicks
      for (let i = 0; i < pcm.length; i++) this.q.push(pcm[i] / 32768);
      if (this.q.length > 32000) this.q.splice(0, this.q.length - 32000);
    };
  }
  process(_, outputs) {
    const out = outputs[0][0];
    for (let i = 0; i < out.length; i++) {
      out[i] = this.q.length ? this.q.shift() : 0;
    }
    return true;
  }
}
registerProcessor("playback", Playback);