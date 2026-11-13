import test from 'node:test';
import assert from 'node:assert/strict';
import { chipView, since, GENERIC_ERROR, DISPLAY_OFFLINE } from '../src/fault-chip.js';

const fault = (code, severity, priority, extra = {}) => ({ code, severity, priority, icon: 'cloud', title: code, since: 1, ...extra });

test('hidden when healthy; degraded alone is shown dimly', () => {
  assert.equal(chipView([], { state: 'idle' }).visible, false);
  const view = chipView([fault('stt_offline', 'degraded', 50)], { state: 'idle' });
  assert.deepEqual([view.visible, view.severity, view.title, view.more], [true, 'degraded', 'stt_offline', '']);
});

test('blocking errors outrank degraded ones, then likelier root causes come first', () => {
  const view = chipView([fault('tracker_cpu_fallback', 'degraded', 30), fault('agent_error', 'error', 10),
    fault('internet_down', 'error', 90, { icon: 'wifi' })], { state: 'error' });
  assert.equal(view.title, 'internet_down');
  assert.equal(view.icon, 'wifi');
  assert.equal(view.more, '+2');
  assert.deepEqual(view.items.map(item => item.code), ['internet_down', 'agent_error', 'tracker_cpu_fallback']);
});

test('ERROR without a reported cause, and a lost service, still say something useful', () => {
  const view = chipView([fault('camera_unavailable', 'degraded', 40)], { state: 'error' });
  assert.equal(view.items[0], GENERIC_ERROR);
  assert.equal(chipView([], { state: 'error', offline: true }).items[0], DISPLAY_OFFLINE);
  // Not in ERROR: no generic chip.
  assert.equal(chipView([], { state: 'thinking' }).visible, false);
});

test('ignores malformed, info-level and unknown-icon faults safely', () => {
  const view = chipView([null, { severity: 'error' }, fault('wake_model_missing', 'info', 0),
    fault('x', 'error', 1, { icon: '<img onerror=alert(1)>' })]);
  assert.equal(view.items.length, 1);
  assert.equal(view.icon, 'alert');
  assert.equal(chipView('nonsense').visible, false);
});

test('since formats local time', () => {
  const date = new Date(2026, 10, 2, 9, 5);
  assert.equal(since(date.getTime() / 1000), 'since 09:05');
  assert.equal(since(undefined), '');
});
