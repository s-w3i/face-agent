import test from 'node:test';
import assert from 'node:assert/strict';
import * as THREE from 'three';
import { MOTIONS, Director, DEFAULTS } from '../src/model.js';
import { REST_POSE, motionPose, deformPoint } from '../src/motion.js';
import { createDeformation } from '../src/deformation.js';
import { CharacterScene } from '../src/character.js';

const bottom = -1, span = 2;
const at = (x, y, z, pose) => deformPoint({ x, y, z }, pose, bottom, span);

test('sleep, attention, and speech have visibly different body silhouettes', () => {
  const sleep = motionPose('sleeping', 2.5), listen = motionPose('listening', 2.5);
  assert.ok(sleep.height < 0.55 && sleep.width > 1.25 && sleep.eye === 0);
  assert.ok(listen.height > 1.08 && listen.forward > 0.2 && listen.eye > 1);
  const phrase = Array.from({ length: 101 }, (_, i) => motionPose('speaking', i / 20));
  const heights = phrase.map(p => p.height);
  assert.ok(Math.max(...heights) - Math.min(...heights) > 0.25, 'Speech needs clear syllable deformation');
  assert.ok(phrase.some(p => p.ripple > 0.08), 'Speech must change the contour');
  assert.equal(phrase[0].ripple, 0, 'The phrase contains a real rest');
  const thinking = motionPose('thinking', 1.25);
  const middle = at(0, 0, 0, thinking), top = at(0, 1, 0, thinking), base = at(0, -1, 0, thinking);
  assert.ok(Math.abs(middle.x - (top.x + base.x) / 2) > 0.1, 'Thinking bends the spine instead of rotating a rigid body');
});

test('every motion keeps a finite, non-inverted surface and a planted base', () => {
  for (const motion of MOTIONS) for (let t = 0; t <= motion.duration; t += 0.05) {
    const p = motionPose(motion.id, t);
    assert.ok(Object.values(p).every(Number.isFinite), `${motion.id} at ${t}`);
    assert.ok(p.height > 0.4 && p.depth > 0.8);
    const base = at(0, bottom, 0, p);
    assert.deepEqual(base, { x: 0, y: bottom, z: 0 });
    for (let y = bottom; y <= 1; y += 0.1) {
      const left = at(-1, y, 0.6, p), right = at(1, y, 0.6, p);
      assert.ok(right.x > left.x + 1, `${motion.id} contour must not fold inside out`);
      assert.ok(Object.values(right).every(Number.isFinite));
    }
  }
});

test('seeking clamps the selected animation and pauses its tour and clock', () => {
  const d = new Director(); d.startTour(); d.play('stretch'); d.seek(2);
  assert.equal(d.elapsed, 2); assert.equal(d.paused, true); assert.equal(d.tour, false);
  d.tick(10); assert.equal(d.elapsed, 2);
  d.seek(100); assert.equal(d.elapsed, 4); d.seek(-1); assert.equal(d.elapsed, 0);
  d.seek(NaN); assert.equal(d.elapsed, 0);
});

test('fur and face shader transforms share body coordinates through nested scaled parts', () => {
  const rig = new THREE.Group(); rig.position.set(1, 2, 3); rig.rotation.y = 0.5;
  const part = new THREE.Mesh(new THREE.SphereGeometry(), new THREE.MeshStandardMaterial());
  part.position.set(-0.24, 0.1, 0.74); part.scale.set(0.085, 0.15, 0.055); rig.add(part);
  const warp = createDeformation(rig, bottom, span); warp.update(motionPose('sleeping', 2));
  const shader = { uniforms: {}, vertexShader: THREE.ShaderLib.standard.vertexShader, fragmentShader: THREE.ShaderLib.standard.fragmentShader };
  part.material.onBeforeCompile(shader);
  const origin = new THREE.Vector3().applyMatrix4(shader.uniforms.warpToRig.value);
  assert.ok(origin.distanceTo(part.position) < 1e-9);
  assert.ok(origin.clone().applyMatrix4(shader.uniforms.warpFromRig.value).length() < 1e-9);
  assert.ok(!shader.vertexShader.includes('}#define'), 'GLSL declarations must end before preprocessor lines');
  assert.equal(shader.uniforms.warpShape.value.y, motionPose('sleeping', 2).height);
  part.geometry.dispose(); part.material.dispose();
});

test('interruptions blend body shapes, pause freezes them, and scrubbing is exact', () => {
  const scene = Object.assign(Object.create(CharacterScene.prototype), {
    rig: new THREE.Group(), accessory: new THREE.Group(), eyes: [], config: DEFAULTS,
    pointer: new THREE.Vector2(), pose: { ...REST_POSE }, baseHeight: 1,
    time: 0, blinkAt: 2.4, blinkTime: -Infinity, deformation: { update() {} },
    shadow: { scale: new THREE.Vector3(), material: {} },
  });
  const d = new Director(); d.auto = false; d.play('sleeping');
  for (let i = 0; i < 120; i++) { d.tick(1 / 60); scene.update(1 / 60, d); }
  assert.ok(scene.pose.height < 0.55);
  const previousHeight = scene.pose.height;
  d.play('speaking'); scene.update(1 / 60, d);
  assert.ok(scene.pose.height > previousHeight && scene.pose.height < 0.65, 'A status interruption must blend');
  d.paused = true; const frozen = { ...scene.pose }; scene.update(0.5, d);
  assert.deepEqual(scene.pose, frozen);
  d.seek(2.5); scene.update(0, d, true);
  assert.deepEqual(scene.pose, motionPose('speaking', 2.5, scene.pointer));
});
