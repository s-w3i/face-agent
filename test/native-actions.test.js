import test from 'node:test';
import assert from 'node:assert/strict';
import { IdleMovements, ORIGINAL_ACTIONS, IDLE_ACTIONS } from '../src/native-actions.js';

test('idle movements wait during other states, disabled automation, and reduced motion', () => {
  const idle = new IdleMovements(() => 0.5);
  idle.reset(10);
  for (const gate of [{ idle: false }, { enabled: false }, { reduced: true }]) {
    assert.equal(idle.tick(100, gate), null);
    assert.equal(idle.remaining, 10);
  }
  assert.equal(idle.tick(9.9), null);
  assert.ok(IDLE_ACTIONS.includes(idle.tick(0.2)));
  assert.equal(idle.remaining, 10);
});

test('random native actions never repeat consecutively or change a custom outfit', () => {
  let seed = 42;
  const idle = new IdleMovements(() => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32));
  let last = '';
  const seen = new Set();
  for (let i = 0; i < 1000; i++) {
    const id = idle.next(false);
    assert.notEqual(id, last); assert.notEqual(id, 'signature');
    assert.ok(ORIGINAL_ACTIONS.some(a => a.id === id && !a.hero));
    last = id; seen.add(id);
  }
  assert.equal(seen.size, IDLE_ACTIONS.length - 1);
  assert.ok(idle.remaining >= idle.interval * 0.75 && idle.remaining < idle.interval * 1.25);
});

test('a manual state change gives idle a fresh interval instead of an overdue movement', () => {
  const idle = new IdleMovements(() => 0.5);
  idle.tick(11.9);
  idle.reset(); // The same reset used by manual selections and finite-action completion.
  assert.equal(idle.tick(1), null);
  assert.equal(idle.remaining, 11);
  idle.reset(60);
  assert.equal(idle.tick(59), null);
});
