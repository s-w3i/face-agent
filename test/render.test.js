import test from 'node:test';
import assert from 'node:assert/strict';
import { CharacterScene } from '../src/character.js';

test('lightweight mode caps rendered frames under fractional display timestamps', () => {
  let draws = 0;
  const scene = Object.assign(Object.create(CharacterScene.prototype), {
    targetFPS: 30, lastFrame: 0, statsTime: 0, frames: 0,
    controls: { update() {} }, onStats() {},
    renderer: { render() { draws++; }, info: { render: { triangles: 0, calls: 0 } } },
  });
  for (let i = 1; i <= 120; i++) scene.render(i * 16.666);
  assert.ok(draws >= 57 && draws <= 60, `Expected about 60 frames in two seconds, got ${draws}`);
  const low = draws; scene.targetFPS = 60;
  for (let i = 121; i <= 240; i++) scene.render(i * 16.666);
  assert.ok(draws - low >= 115 && draws - low <= 121);
});
