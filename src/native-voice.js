import { DEFORMATION_GLSL, REST_POSE, motionPose } from './motion.js';

const clamp = (value, max = 1) => Number.isFinite(value) ? Math.max(0, Math.min(max, value)) : 0;

// A supplied speech level replaces the demo envelope. TTS playback can later
// drive this with its audio envelope without changing the rendering layer.
export function voiceMotionPose(state, time, level = null, strength = 1, reduced = false) {
  if (state !== 'listening' && state !== 'speaking') return { ...REST_POSE };
  const t = reduced ? 0 : Math.max(0, Number.isFinite(time) ? time : 0);
  const pose = motionPose(state, t);
  if (state === 'speaking' && (level !== null || reduced)) {
    const voice = clamp(level ?? 0.5);
    Object.assign(pose, { height: 1 + voice * 0.24, width: 1 - voice * 0.13,
      depth: 1 + voice * 0.05, taper: voice * 0.22, forward: voice * 0.11,
      bend: Math.sin(t * Math.PI * 2 / 5) * voice * 0.16, curve: Math.sin(t * Math.PI * 0.8) * voice * 0.10,
      ripple: voice * 0.095, phase: -t * Math.PI * 1.6 });
  }
  const gain = clamp(strength, 1.5) * (reduced ? 0.35 : 1);
  for (const key of ['width', 'height', 'depth', 'taper', 'bend', 'forward', 'curve', 'ripple']) {
    pose[key] = REST_POSE[key] + (pose[key] - REST_POSE[key]) * gain;
  }
  return pose;
}

export function extendVoiceShader(source) {
  const fur = 'worldPosition=world;worldNormal=transformedNormal;';
  const body = 'worldPosition = world.xyz;';
  if (!source.includes('vec3 signaturePosition(') || !source.includes(fur) || !source.includes(body)) return source;
  return source.replace('vec3 signaturePosition(', DEFORMATION_GLSL + '\nvec3 signaturePosition(')
    .replace(fur, 'transformedNormal=warpNormal(transformedNormal,world); world=warpPosition(world);\n    ' + fur)
    .replace(body, 'n=warpNormal(n,world.xyz); world.xyz=warpPosition(world.xyz);\n    ' + body);
}

// The copied SDK owns geometry, fitting, materials and animation. This adapter
// extends its vertex shader before compilation, on this canvas only. Positions,
// normals and shadow vertices share one field; no vertex readback/rebuilds.
// ponytail: pinned to this SDK's draw layout; keep native activities available
// if a later imported bundle changes its shader layout.
export function attachVoiceMotion(canvas) {
  const originalGetContext = canvas.getContext;
  const shape = new Float32Array([1, 1, 1, 0]), bend = new Float32Array(4), range = new Float32Array([-1, 2, 0]);
  let gl, currentProgram, sourceCount = 0, restore, span = 2, pose = REST_POSE;
  const locations = new WeakMap();
  function upload(program) {
    if (!gl || !program) return;
    let uniforms = locations.get(program);
    if (!uniforms) {
      uniforms = ['warpShape', 'warpBend', 'warpRange'].map(name => gl.getUniformLocation(program, name));
      locations.set(program, uniforms);
    }
    if (uniforms[0] === null) return;
    gl.uniform4fv(uniforms[0], shape); gl.uniform4fv(uniforms[1], bend); gl.uniform3fv(uniforms[2], range);
  }
  function update(nextPose) {
    pose = nextPose;
    shape.set([pose.width, pose.height, pose.depth, pose.taper]);
    bend.set([pose.bend * span / 2, pose.forward * span / 2, pose.curve * span / 2, pose.ripple]);
    range[2] = pose.phase;
    upload(currentProgram);
  }
  canvas.getContext = function (...args) {
    const context = originalGetContext.apply(canvas, args);
    if (!context || args[0] !== 'webgl2' || gl) return context;
    gl = context;
    const shaderSource = gl.shaderSource, useProgram = gl.useProgram, bufferData = gl.bufferData;
    gl.shaderSource = function (shader, source) {
      const extended = extendVoiceShader(source);
      if (extended !== source) sourceCount++;
      return shaderSource.call(gl, shader, extended);
    };
    gl.useProgram = function (program) { useProgram.call(gl, program); currentProgram = program; upload(program); };
    gl.bufferData = function (...values) {
      const [target, source, , start = 0, count = source?.length] = values;
      // First packed DrawBlock entry is the body. Paint bounds describe its
      // neutral surface. Convert to world coordinates for every fitted part.
      if (target === gl.UNIFORM_BUFFER && source instanceof Uint8Array && count >= 368 && count !== 464 && start % 4 === 0) {
        const data = new Float32Array(source.buffer, source.byteOffset + start, 92);
        const height = 1 / data[67], width = 1 / data[66];
        const size = height * Math.abs(data[5]);
        if (height > 0.3 && height < 5 && width > 0.3 && width < 5 && size > 0.3 && size < 6 && data[15] === 1) {
          span = size; range[0] = (data[65] - height / 2) * data[5] + data[13]; range[1] = span;
          update(pose);
        }
      }
      return bufferData.apply(gl, values);
    };
    restore = () => { gl.shaderSource = shaderSource; gl.useProgram = useProgram; gl.bufferData = bufferData; };
    return gl;
  };
  return { update, supported: () => sourceCount > 0,
    dispose() { restore?.(); canvas.getContext = originalGetContext; gl = undefined; currentProgram = undefined; } };
}
