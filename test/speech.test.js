import test from 'node:test';
import assert from 'node:assert/strict';
import { SpeechPlayer } from '../src/speech.js';

test('voice startup initializes audio while a browser permission leaves resume pending', async () => {
  const savedContext = globalThis.AudioContext;
  let allowAudio;
  globalThis.AudioContext = class {
    state = 'suspended';
    resume() { return new Promise(resolve => { allowAudio = () => { this.state = 'running'; resolve(); }; }); }
    close() {}
  };
  const player = new SpeechPlayer();
  let ready = false;
  try {
    const enabling = player.enable().then(() => { ready = true; });
    assert.equal(player.context.state, 'suspended');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(ready, false);
    allowAudio(); await enabling;
    assert.equal(player.context.state, 'running');
    assert.equal(ready, true);
  } finally { player.dispose(); globalThis.AudioContext = savedContext; }
});

test('speech measures actual audio, ends cleanly, and cancels delayed decoding', async () => {
  const sources = [];
  const savedContext = globalThis.AudioContext;
  globalThis.AudioContext = class {
    state = 'suspended';
    async resume() { this.state = 'running'; }
    async decodeAudioData() { return {}; }
    createBufferSource() {
      const source = { connect() {}, disconnect() {}, start() { this.started = true; }, stop() { this.stopped = true; } };
      sources.push(source); return source;
    }
    createAnalyser() { return { connect() {}, disconnect() {}, getFloatTimeDomainData(samples) { samples.fill(.05); } }; }
    close() { this.state = 'closed'; }
  };
  let subtitle = '';
  const player = new SpeechPlayer(text => { subtitle = text; });
  let started = 0, ended = 0;
  try {
    const clip = new Blob(['test clip']);
    await assert.rejects(player.play(clip, () => {}, () => {}), /Enable voice/);
    await player.enable();
    await player.play(clip, () => started++, () => ended++, 'Hello, robot.');
    assert.equal(subtitle, 'Hello, robot.');
    assert.equal(started, 1); assert.ok(Math.abs(player.level() - .4) < .001);
    sources[0].onended();
    assert.equal(subtitle, '');
    assert.equal(ended, 1); assert.equal(player.level(), null);
    await player.play(clip, () => started++, () => ended++, 'Second message.');
    player.stop();
    assert.equal(subtitle, '');
    assert.equal(sources[1].stopped, true); assert.equal(sources[1].onended, null);
    assert.equal(ended, 1); assert.equal(player.level(), null);
    let finish;
    player.context.decodeAudioData = () => new Promise(resolve => { finish = resolve; });
    const pending = player.play(clip, () => started++, () => ended++, 'Cancelled message.');
    await new Promise(resolve => setImmediate(resolve));
    player.stop(); finish({}); await pending;
    assert.equal(subtitle, '');
    assert.equal(sources.length, 2); assert.equal(started, 2);
    player.dispose(); assert.equal(player.context.state, 'closed');
  } finally { globalThis.AudioContext = savedContext; }
});

test('streaming starts before completion, schedules PCM in order, and cancels queued audio', async () => {
  const savedContext = globalThis.AudioContext, sources = [];
  globalThis.AudioContext = class {
    state = 'running'; currentTime = 0;
    async resume() {}
    createBuffer(channels, frames, rate) {
      return { duration: frames / rate, samples: new Float32Array(frames), getChannelData() { return this.samples; } };
    }
    createBufferSource() {
      const source = { connect() {}, disconnect() {}, start(at) { this.at = at; }, stop() { this.stopped = true; } };
      sources.push(source); return source;
    }
    createAnalyser() { return { connect() {}, disconnect() {}, getFloatTimeDomainData(samples) { samples.fill(.05); } }; }
    close() {}
  };
  let subtitle = '';
  const player = new SpeechPlayer(text => { subtitle = text; }), encoder = new TextEncoder();
  let started = 0, ended = 0, timing;
  const pcm = new Int16Array(2400); pcm[0] = 16384; pcm[1] = -16384;
  const packet = JSON.stringify({ type: 'audio', pcm: Buffer.from(pcm.buffer).toString('base64'), firstAudioMs: 123, warm: true }) + '\n';
  let feed;
  const response = () => new Response(new ReadableStream({ start(controller) { feed = controller; } }));
  try {
    await player.enable();
    const playing = player.stream(response(), () => started++, () => ended++, value => { timing = value; }, 'Streaming message.');
    assert.equal(subtitle, '');
    feed.enqueue(encoder.encode(packet.slice(0, 13))); feed.enqueue(encoder.encode(packet.slice(13)));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(subtitle, 'Streaming message.');
    assert.equal(started, 1); assert.equal(ended, 0); assert.equal(timing.firstAudioMs, 123);
    assert.equal(sources[0].buffer.samples[0], .5); assert.equal(sources[0].buffer.samples[1], -.5);
    assert.equal(sources[0].at, .08); assert.ok(Math.abs(player.level() - .4) < .001);
    feed.enqueue(encoder.encode(packet + '{"type":"done"}\n')); feed.close();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(ended, 0); assert.ok(Math.abs(sources[1].at - .18) < 1e-10);
    sources[0].onended(); assert.equal(ended, 0);
    assert.equal(subtitle, 'Streaming message.');
    sources[1].onended(); await playing;
    assert.equal(subtitle, '');
    assert.equal(ended, 1); assert.equal(player.level(), null);
    const interrupted = player.stream(response(), () => started++, () => ended++, () => {}, 'Interrupted message.');
    feed.enqueue(encoder.encode(packet)); await new Promise(resolve => setImmediate(resolve));
    player.stop(); await interrupted;
    assert.equal(subtitle, '');
    assert.equal(sources[2].stopped, true); assert.equal(ended, 1);
    const failed = player.stream(response(), () => started++, () => ended++, () => {}, 'Failed message.');
    feed.enqueue(encoder.encode(packet + '{"type":"error","error":"Provider failed"}\n'));
    await assert.rejects(failed, /Provider failed/);
    assert.equal(subtitle, '');
    assert.equal(sources[3].stopped, true); assert.equal(ended, 1); assert.equal(player.level(), null);
  } finally { player.dispose(); globalThis.AudioContext = savedContext; }
});
