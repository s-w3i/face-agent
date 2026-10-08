import test from 'node:test';
import assert from 'node:assert/strict';
import { bakedFrame, matchingBake } from '../src/prerendered.js';

test('baked playback loops, finishes finite actions and maps live speech amplitude', () => {
  const clip = { id: 'idle', count: 96, loopStart: 0 };
  assert.equal(bakedFrame(clip, 6.25, 16), 4);
  assert.equal(bakedFrame({ ...clip, loopStart: 16 }, 6.25, 16), 20);
  assert.equal(bakedFrame({ ...clip, id: 'wave', loopStart: null }, 8, 16), 95);
  const speaking = { id: 'speaking', count: 21 };
  assert.equal(bakedFrame(speaking, 200, 16, 0), 0);
  assert.equal(bakedFrame(speaking, 0, 16, .5), 10);
  assert.equal(bakedFrame(speaking, 0, 16, 4), 20);
  const config = { resourceRevision: 'abc', appearance: { state: 'saved', name: 'Shiro' }, settings: { motionStrength: .6, reducedMotion: false, background: 'light' } };
  const manifest = { config: structuredClone(config) };
  assert.ok(matchingBake(config, manifest));
  manifest.config.appearance.name = 'Renamed'; manifest.config.settings.autoIdle = true;
  assert.ok(matchingBake(config, manifest));
  manifest.config.appearance.state = 'other look';
  assert.equal(matchingBake(config, manifest), false);
});
