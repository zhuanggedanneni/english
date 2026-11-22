class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
  }

  process(inputs) {
    const input = inputs[0][0];
    if (!input || !input.length) return true;
    const outputLength = Math.max(1, Math.floor(input.length / this.ratio));
    const pcm = new Int16Array(outputLength);
    for (let i = 0; i < outputLength; i += 1) {
      const sample = input[Math.min(input.length - 1, Math.floor(i * this.ratio))];
      pcm[i] = Math.max(-32768, Math.min(32767, Math.round(sample * 32768)));
    }
    this.port.postMessage(pcm.buffer, [pcm.buffer]);
    return true;
  }
}

registerProcessor("pcm-processor", PCMProcessor);
