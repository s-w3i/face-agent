import test from 'node:test';
import assert from 'node:assert/strict';
import { paintedBounds, fittedLayout } from '../src/native-framing.js';

test('framing includes separated props and padded edges, ignoring transparent space', () => {
  const size = 64, pixels = new Uint8ClampedArray(size * size * 4);
  assert.equal(paintedBounds(pixels), null);
  for (const [x, y] of [[12, 10], [38, 48], [59, 28]]) pixels[(y * size + x) * 4 + 3] = 255;
  assert.deepEqual(paintedBounds(pixels), { left: 10 / 64, top: 8 / 64, right: 62 / 64, bottom: 51 / 64 });
});

test('maximum sizing keeps tall bodies and wide action props inside portrait, Pi and subtitle frames', () => {
  for (const [width, height] of [[747, 905], [800, 480], [480, 320], [747, 740], [320, 180]]) {
    for (const bounds of [
      { left: .2, top: .12, right: .65, bottom: .85 },
      { left: .05, top: .15, right: .98, bottom: .8 },
      { left: 0, top: 0, right: 1, bottom: 1 },
    ]) {
      const { size, left, top } = fittedLayout(bounds, width, height);
      const margin = Math.min(16, Math.min(width, height) * .025);
      assert.ok(left + bounds.left * size >= margin - 1e-6);
      assert.ok(top + bounds.top * size >= margin - 1e-6);
      assert.ok(left + bounds.right * size <= width - margin + 1e-6);
      assert.ok(top + bounds.bottom * size <= height - margin + 1e-6);
      // A larger canvas would cross at least one safe edge.
      assert.ok(Math.abs((bounds.right - bounds.left) * size - (width - margin * 2)) < 1e-6 ||
        Math.abs((bounds.bottom - bounds.top) * size - (height - margin * 2)) < 1e-6);
    }
  }
});
