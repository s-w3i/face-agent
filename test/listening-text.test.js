import test from 'node:test';
import assert from 'node:assert/strict';
import { connectListeningText, listeningTop, LISTENING_PLACEHOLDER } from '../src/listening-text.js';

test('transcripts update safely, ignore older events, clear and reset across service generations', () => {
  let listener, changes = 0;
  const events = { addEventListener: (_, callback) => { listener = callback; } };
  const element = { textContent: '', hidden: true, dataset: {}, getBoundingClientRect: () => ({ bottom: 70 }) };
  connectListeningText(events, element, () => changes++);
  const send = value => listener({ data: JSON.stringify(value) });
  send({ generation: 'a', sequence: 2, text: '<img onerror=alert(1)>', final: false });
  assert.equal(element.textContent, '<img onerror=alert(1)>');
  assert.equal(listeningTop(element, { top: 10 }), 72);
  send({ generation: 'a', sequence: 1, text: 'stale' });
  assert.equal(changes, 1);
  send({ generation: 'a', sequence: 3, text: '', final: true });
  assert.equal(element.hidden, true);
  assert.equal(listeningTop(element, { top: 10 }), 0);
  send({ generation: 'b', sequence: 0, text: 'New session', final: true });
  assert.equal(element.textContent, 'New session');
  assert.equal(element.dataset.final, 'true');
});

test('awake idle shows "Listening…" with a restartable countdown until speech or another state', () => {
  let listener;
  const events = { addEventListener: (_, callback) => { listener = callback; } };
  const properties = {};
  const element = { textContent: '', hidden: true, dataset: {}, style: { setProperty: (name, value) => { properties[name] = value; } } };
  const cue = connectListeningText(events, element);
  const send = value => listener({ data: JSON.stringify(value) });
  cue.command({ state: 'idle', level: null });  // Typed chat or a manual idle: no listening claim.
  assert.equal(element.hidden, true);
  cue.command({ state: 'idle', listenSeconds: 8 });
  assert.equal(element.textContent, LISTENING_PLACEHOLDER);
  assert.equal(element.hidden, false);
  assert.equal(element.dataset.placeholder, 'true');
  assert.equal(properties['--listen-seconds'], '8s');
  const first = element.dataset.countdown;
  cue.command({ state: 'idle', listenSeconds: 8 });  // Back to idle after a reply: the bar restarts.
  assert.notEqual(element.dataset.countdown, first);
  send({ generation: 'a', sequence: 1, text: 'What time', final: false });
  assert.equal(element.textContent, 'What time');
  assert.equal(element.dataset.placeholder, 'false');
  assert.equal(element.dataset.countdown, undefined);
  send({ generation: 'a', sequence: 2, text: '', final: true });  // Discarded utterance: still listening.
  assert.equal(element.textContent, LISTENING_PLACEHOLDER);
  cue.command({ state: 'thinking' });
  assert.equal(element.hidden, true);
  assert.equal(element.dataset.countdown, undefined);
  cue.command({ state: 'idle', listenSeconds: null });  // A mission never sleeps: caption without a bar.
  assert.equal(element.textContent, LISTENING_PLACEHOLDER);
  assert.equal(element.dataset.countdown, undefined);
  cue.command({ state: 'speaking', speech: '/api/audio/x' });
  assert.equal(element.hidden, true);
});
