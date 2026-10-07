// Native Web Audio keeps playback and amplitude analysis lightweight on the Pi.
export class SpeechPlayer {
  constructor() { this.context = null; this.source = null; this.generation = 0; this.sources = new Set(); }

  async enable() {
    this.context ||= new AudioContext();
    await this.context.resume();
  }

  stop() {
    this.generation++;
    this.reader?.cancel().catch(() => {}); this.reader = null;
    this.finishStream?.(); this.finishStream = null;
    for (const source of this.sources) { source.onended = null; source.stop(); source.disconnect(); }
    this.sources.clear();
    if (this.source) {
      this.source.onended = null;
      if (!this.streaming) { this.source.stop(); this.source.disconnect(); }
      this.source = null;
    }
    this.analyser?.disconnect(); this.streaming = false;
  }

  async play(blob, onStart, onEnd) {
    this.stop();
    const generation = this.generation;
    this.context ||= new AudioContext();
    if (this.context.state !== 'running') throw new Error('Enable voice on the robot display to allow sound.');
    const buffer = await this.context.decodeAudioData(await blob.arrayBuffer());
    if (generation !== this.generation) return;
    const source = this.context.createBufferSource();
    this.analyser = this.context.createAnalyser(); this.analyser.fftSize = 512;
    this.samples = new Float32Array(this.analyser.fftSize);
    source.buffer = buffer; source.connect(this.analyser); this.analyser.connect(this.context.destination);
    this.source = source;
    source.onended = () => {
      if (this.source !== source) return;
      this.source = null; source.disconnect(); this.analyser.disconnect(); onEnd();
    };
    onStart(); source.start();
  }

  async stream(response, onStart, onEnd, onTiming = () => {}) {
    this.stop(); const generation = this.generation;
    this.context ||= new AudioContext();
    if (this.context.state !== 'running') throw new Error('Enable voice on the robot display to allow sound.');
    const reader = response.body.getReader(); this.reader = reader; this.streaming = true;
    this.analyser = this.context.createAnalyser(); this.analyser.fftSize = 512;
    this.samples = new Float32Array(512); this.analyser.connect(this.context.destination);
    const decoder = new TextDecoder(); let pending = '', received = false, complete = false, at = 0;
    let resolvePlayback; const playback = new Promise(resolve => { resolvePlayback = resolve; });
    this.finishStream = resolvePlayback;
    const finish = () => {
      if (!complete || this.sources.size || generation !== this.generation) return;
      this.source = null; this.analyser.disconnect(); this.finishStream = null;
      onEnd(); resolvePlayback();
    };
    const event = packet => {
      if (packet.type === 'error') throw new Error(packet.error);
      if (packet.type === 'done') { complete = true; finish(); return; }
      if (packet.type !== 'audio') return;
      const bytes = Uint8Array.from(atob(packet.pcm), character => character.charCodeAt(0));
      if (!bytes.length || bytes.length % 2) throw new Error('Invalid speech audio.');
      const pcm = new DataView(bytes.buffer), buffer = this.context.createBuffer(1, bytes.length / 2, 24000);
      const samples = buffer.getChannelData(0);
      for (let i = 0; i < samples.length; i++) samples[i] = pcm.getInt16(i * 2, true) / 32768;
      const source = this.context.createBufferSource(); source.buffer = buffer; source.connect(this.analyser);
      this.sources.add(source); this.source ||= source;
      const first = !received; received = true;
      // A short initial cushion smooths network jitter; chunks use the audio clock.
      at = Math.max(at, this.context.currentTime + (first ? .08 : .01));
      source.onended = () => {
        this.sources.delete(source); source.disconnect();
        if (this.source === source) this.source = this.sources.values().next().value || null;
        finish();
      };
      source.start(at); at += buffer.duration;
      if (first) { onTiming(packet); onStart(); }
    };
    try {
      while (generation === this.generation && !complete) {
        const { value, done } = await reader.read();
        if (generation !== this.generation) return;
        pending += decoder.decode(value, { stream: !done });
        const lines = pending.split('\n'); pending = lines.pop();
        for (const line of lines) if (line.trim()) event(JSON.parse(line));
        if (done) { if (pending.trim()) event(JSON.parse(pending)); break; }
      }
      if (generation !== this.generation) return;
      if (!received || !complete) throw new Error('Speech stream ended before completion. Please try again.');
      await playback;
    } catch (error) {
      if (generation !== this.generation) return;
      this.stop(); throw error;
    } finally {
      await reader.cancel().catch(() => {}); reader.releaseLock();
      if (this.reader === reader) this.reader = null;
    }
  }

  level() {
    if (!this.source) return null;
    this.analyser.getFloatTimeDomainData(this.samples);
    const rms = Math.sqrt(this.samples.reduce((sum, sample) => sum + sample * sample, 0) / this.samples.length);
    return Math.min(1, rms * 8);
  }

  dispose() { this.stop(); this.context?.close(); }
}
