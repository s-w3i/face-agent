import test from 'node:test';
import assert from 'node:assert/strict';
import { SmoothGaze, eyeBounds, drawGaze } from '../src/gaze.js';

test('gaze is smooth across frame rates, expires, sleeps, and restores original eye pixels', () => {
  function run(fps) {
    const gaze = new SmoothGaze(); gaze.receive({ detected: true, x: 1, y: 0 }, 0);
    gaze.step(1 / fps, 1000 / fps);
    assert.ok(gaze.position[0] > 0 && gaze.position[0] < .1);
    for (let i = 2; i <= fps / 2; i++) gaze.step(1 / fps, i * 1000 / fps);
    return gaze;
  }
  const a = run(30), b = run(60);
  assert.ok(Math.abs(a.position[0] - b.position[0]) < 1e-8);
  assert.ok(a.position[0] > .98 && a.position[0] <= 1);
  a.step(.03, 1100); assert.equal(a.tracking, false);
  for (let i = 0; i < 100; i++) a.step(.03, 1100 + i * 30);
  assert.deepEqual(a.position, [0, 0]);
  b.step(.03, 550, false); assert.equal(b.tracking, false);
  b.step(.03, 580, true, false); assert.equal(b.tracking, false);
  b.receive({ detected: true, x: 0, y: 0, ageMs: 5000 }, 600);
  b.step(.03, 600); assert.equal(b.tracking, false);
  const size = 64, pixels = new Uint8ClampedArray(size * size * 4);
  const rect = (x, y, w, h, color) => { for (let yy = y; yy < y + h; yy++) for (let xx = x; xx < x + w; xx++) pixels.set(color, (yy * size + xx) * 4); };
  rect(8, 12, 48, 45, [220, 30, 130, 255]);
  rect(18, 27, 5, 10, [30, 30, 30, 255]); rect(38, 27, 5, 10, [30, 30, 30, 255]);
  const eyes = eyeBounds(pixels, size); assert.equal(eyes.length, 2);
  const draws = []; const context = { drawImage: (...args) => draws.push(args) };
  drawGaze(context, {}, 0, 0, eyes, [0, 0], size); assert.equal(draws.length, 0);
  drawGaze(context, {}, 0, 0, eyes, [1, -1], size); assert.equal(draws.length, 49);
  assert.ok(draws.every(args => args[3] > 0 && args[4] > 0 && args[7] > 0 && args[8] > 0));
});
