import test from 'node:test';
import assert from 'node:assert/strict';
import { Director, MOTIONS, PRESETS, cleanConfig, DEFAULTS } from '../src/model.js';

test('manual actions interrupt idle and every one-shot returns to idle', () => {
  const d = new Director(() => 0.5); d.auto = false;
  for (const motion of MOTIONS) {
    d.play(motion.id); assert.equal(d.state, motion.id);
    d.tick(motion.duration + 0.1);
    assert.equal(d.state, motion.loop ? motion.id : 'idle');
  }
  d.play('spin'); d.tick(1); d.play('listening');
  assert.equal(d.state, 'listening'); assert.equal(d.elapsed, 0);
});

test('tour plays every animation once and manual selection stops the tour', () => {
  const d = new Director(() => 0.5); d.auto = false; d.startTour();
  const seen = [];
  for (const motion of MOTIONS) { seen.push(d.state); d.tick(motion.duration + 0.01); }
  assert.deepEqual(seen, MOTIONS.map(m => m.id)); assert.equal(d.tour, false); assert.equal(d.state, 'idle');
  d.startTour(); d.play('sleeping'); assert.equal(d.tour, false); assert.equal(d.state, 'sleeping');
});

test('random idle choices do not repeat and never interrupt listening', () => {
  const d = new Director(() => 0.5); d.interval = 5; d.nextIdle = 0;
  d.tick(0.1); const first = d.state; assert.notEqual(first, 'idle');
  d.tick(5); assert.equal(d.state, 'idle'); d.nextIdle = 0; d.tick(0.1);
  assert.notEqual(d.state, first);
  d.play('listening'); d.tick(100); assert.equal(d.state, 'listening');
});

test('sleep, wake, pause, and playback speed are predictable', () => {
  const d = new Director(() => 0.5); d.auto = false; d.sleepAfter = 3;
  d.tick(3.1); assert.equal(d.state, 'sleeping');
  d.play('wake'); d.paused = true; d.tick(10); assert.equal(d.elapsed, 0);
  d.paused = false; d.speed = 2; d.tick(1); assert.equal(d.elapsed, 2);
  d.tick(1.1); assert.equal(d.state, 'idle');
  d.sleepAfter = 0; d.tick(1000); assert.equal(d.state, 'idle');
});

test('imported appearance accepts valid values and rejects malformed fields', () => {
  const config = cleanConfig({ name: '  Fern  ', color: '#80AA31', shape: 'squircle', eyes: 'wide', accessory: 'halo' });
  assert.equal(config.name, 'Fern'); assert.equal(config.shape, 'squircle'); assert.equal(config.color, '#80AA31');
  assert.deepEqual(cleanConfig({ name: '', color: 'red', shape: '__proto__', accessory: 'unknown' }), DEFAULTS);
  assert.equal(cleanConfig({ name: 'a'.repeat(100) }).name.length, 24);
  assert.throws(() => cleanConfig(null)); assert.throws(() => cleanConfig([]));
});

test('reference presets survive export/import and legacy pebble shapes migrate', () => {
  for (const { id, label, ...appearance } of PRESETS) {
    const config = cleanConfig({ ...appearance, name: 'Study' });
    assert.deepEqual(cleanConfig(JSON.parse(JSON.stringify({ version: 2, ...config }))), config);
    for (const key of Object.keys(appearance)) assert.equal(config[key], appearance[key]);
  }
  assert.equal(cleanConfig({ shape: 'pebble', color: '#a4b58b' }).shape, 'blob');
});
