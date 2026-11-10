import test from 'node:test';
import assert from 'node:assert/strict';
import { PickVisual } from '../src/pick-visual.js';
const event = (sequence, active = true, mission_id = 'mission') => ({generation:'server',sequence,active,mission_id,ttl_ms:10000});
test('planning selects existing thinking only over idle/listening and expires', () => {
  const visual = new PickVisual(); visual.receive(event(1), 100);
  for (const base of ['idle','listening','needs-input']) assert.equal(visual.state(base, 200),'thinking');
  for (const base of ['speaking','sleeping','error','wave','working','thinking']) assert.equal(visual.state(base,200),base);
  assert.equal(visual.state('idle',200,true),'idle');
  assert.equal(visual.state('idle',10100),'idle');
});
test('duplicates and stale events cannot restart the lease; refresh and clear are correlated', () => {
  const visual = new PickVisual(); visual.receive(event(2),0);
  assert.equal(visual.receive(event(2),9000),false);assert.equal(visual.receive(event(1),9000),false);
  assert.equal(visual.state('idle',10001),'idle');
  visual.receive(event(3),10002);assert.equal(visual.state('idle',11000),'thinking');
  visual.receive(event(4,false),11001);assert.equal(visual.state('idle',11002),'idle');
});
test('speech temporarily wins and the planning pose resumes only while leased', () => {
  const visual=new PickVisual();visual.receive(event(1),0);
  assert.equal(visual.state('speaking',1),'speaking');assert.equal(visual.state('listening',2),'thinking');
  visual.receive(event(2,false),3);assert.equal(visual.state('listening',4),'listening');
});
