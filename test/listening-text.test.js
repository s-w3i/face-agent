import test from 'node:test';
import assert from 'node:assert/strict';
import { connectListeningText, listeningTop } from '../src/listening-text.js';

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
