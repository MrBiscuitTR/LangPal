// AudioWorklet that ships raw microphone frames to the main thread.
//
// It deliberately does almost nothing. This code runs on the real-time audio
// thread, where an allocation-heavy or slow callback causes an audible glitch,
// so voice activity detection, resampling and WAV encoding all live in app.js.
//
// Per render quantum (128 frames) it posts one message holding a copy of the
// mono samples plus their RMS level, which app.js uses for silence detection
// and barge-in.
class CaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;

    const channel = input[0];
    if (!channel || channel.length === 0) return true;

    // Copy: the audio engine reuses the underlying buffer after we return, so
    // posting it directly would hand the main thread data that mutates.
    const samples = new Float32Array(channel);

    let sumSquares = 0;
    for (let i = 0; i < samples.length; i++) sumSquares += samples[i] * samples[i];
    const rms = Math.sqrt(sumSquares / samples.length);

    // Transfer the buffer rather than structured-cloning it.
    this.port.postMessage({ samples, rms }, [samples.buffer]);

    // Returning true keeps the node alive even while the mic is silent.
    return true;
  }
}

registerProcessor('capture-processor', CaptureProcessor);
