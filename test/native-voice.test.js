import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { attachVoiceMotion, extendVoiceShader, voiceMotionPose } from '../src/native-voice.js';
import { REST_POSE, deformPoint } from '../src/motion.js';

test('added voice motions keep silence neutral and every surface finite at maximum strength', () => {
  for (let t = 0; t <= 5; t += 0.05) {
    const silence = voiceMotionPose('speaking', t, 0);
    for (const key of ['width', 'height', 'depth', 'taper', 'bend', 'forward', 'curve', 'ripple']) assert.equal(silence[key], REST_POSE[key]);
    for (const state of ['speaking', 'listening']) for (const level of [null, 0, 0.5, 1]) {
      const pose = voiceMotionPose(state, t, level, 1.5);
      assert.ok(Object.values(pose).every(Number.isFinite));
      assert.deepEqual(deformPoint({ x: 0, y: -1, z: 0 }, pose, -1, 2), { x: 0, y: -1, z: 0 });
      assert.ok(deformPoint({ x: 1, y: 0, z: 0 }, pose, -1, 2).x > deformPoint({ x: -1, y: 0, z: 0 }, pose, -1, 2).x);
    }
  }
  assert.deepEqual(voiceMotionPose('speaking', 1, null, 1, true), voiceMotionPose('speaking', 4, null, 1, true));
  assert.deepEqual(voiceMotionPose('ready', 2), REST_POSE);
});

test('canvas adapter preserves WebGL overloads, applies the shared body bounds, and disposes its hooks', () => {
  const uploads = new Map(), allocations = [];
  const gl = { UNIFORM_BUFFER: 35345, shaderSource() {}, useProgram() {},
    bufferData(...args) { allocations.push(args); }, getUniformLocation(program, name) { return name; },
    uniform4fv(location, values) { uploads.set(location, [...values]); },
    uniform3fv(location, values) { uploads.set(location, [...values]); } };
  const canvas = { getContext() { return gl; } }, originalGetContext = canvas.getContext, originalAllocate = gl.bufferData;
  const motion = attachVoiceMotion(canvas); canvas.getContext('webgl2');
  gl.useProgram({}); motion.update(voiceMotionPose('speaking', 0, 1));
  gl.bufferData(gl.UNIFORM_BUFFER, 64, 35048);
  assert.deepEqual(allocations[0], [gl.UNIFORM_BUFFER, 64, 35048], 'Numeric allocation must retain the three-argument overload');
  const packed = new Uint8Array(400), floats = new Float32Array(packed.buffer, 16, 92);
  floats[5] = 2; floats[13] = 0.25; floats[15] = 1; floats[65] = 0; floats[66] = 0.5; floats[67] = 0.5;
  gl.bufferData(gl.UNIFORM_BUFFER, packed, 35048, 16, 368);
  assert.deepEqual(uploads.get('warpRange').slice(0, 2), [-1.75, 4]);
  assert.ok(Math.abs(uploads.get('warpShape')[1] - 1.24) < 1e-6);
  motion.dispose(); assert.equal(canvas.getContext, originalGetContext); assert.equal(gl.bufferData, originalAllocate);
});

const wasmPath = new URL('../public/local-dots/orbit-characters.wasm', import.meta.url);
test('the installed vertex shader accepts deformation in both the surface and fur passes', { skip: !existsSync(wasmPath) }, () => {
  const bytes = readFileSync(wasmPath), start = bytes.indexOf('#version 300 es\nprecision highp float;\nlayout(location=0)');
  assert.ok(start >= 0);
  const source = bytes.subarray(start, bytes.indexOf(0, start)).toString();
  const patched = extendVoiceShader(source);
  assert.notEqual(source, patched);
  assert.ok(patched.includes('transformedNormal=warpNormal(transformedNormal,world); world=warpPosition(world);'));
  assert.ok(patched.includes('n=warpNormal(n,world.xyz); world.xyz=warpPosition(world.xyz);'));
  assert.ok(patched.indexOf('world=warpPosition(world);') < patched.indexOf('shadowPosition=f.lightViewProjection*vec4(world,1);'));
  assert.equal(extendVoiceShader('unrecognized shader'), 'unrecognized shader');
});
