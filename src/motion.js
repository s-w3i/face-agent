// Independent procedural motion, informed by dots' bent spine and fitted eye morphs.
// All deformation is expressed in body space; the base stays on the floor.
const TAU = Math.PI * 2;
const clamp = x => Math.max(0, Math.min(1, x));
const smooth = x => { x = clamp(x); return x * x * (3 - 2 * x); };
const pulse = (t, start, end) => Math.sin(clamp((t - start) / (end - start)) * Math.PI);
export const REST_POSE = Object.freeze({
  y: 0, rx: 0, ry: 0, rz: 0, eye: 1, gazeX: 0, gazeY: 0,
  width: 1, height: 1, depth: 1, taper: 0, bend: 0, forward: 0, curve: 0, ripple: 0, phase: 0,
});
export function motionPose(state, t, pointer = { x: 0, y: 0 }) {
  const breath = Math.sin(t * TAU / 5);
  const p = { ...REST_POSE, height: 1 + breath * 0.012, width: 1 - breath * 0.006,
    ry: Math.sin(t * TAU / 10) * 0.025, gazeX: pointer.x * 0.027, gazeY: pointer.y * 0.02 };
  switch (state) {
    case 'listening': {
      const nod = Math.pow(Math.max(0, Math.sin(t * TAU / 5)), 6);
      Object.assign(p, { height: 1.12 - nod * 0.045, width: 0.94, taper: 0.18,
        bend: -0.14, forward: 0.22 + nod * 0.07, eye: 1.16, ry: 0, gazeX: pointer.x * 0.02 }); break;
    }
    case 'thinking': {
      const sway = Math.sin(t * TAU / 5);
      Object.assign(p, { height: 1.15, width: 0.91, taper: -0.10, bend: 0.22 + sway * 0.20,
        curve: -0.22 - sway * 0.11, forward: -0.08, ripple: 0.045, phase: -t * TAU / 5,
        gazeX: -0.022, gazeY: 0.04, eye: 0.68, ry: -0.10 }); break;
    }
    case 'speaking': {
      // Five-second visual speech phrase with syllables, accents, and a rest.
      const phrase = 0.5 - 0.5 * Math.cos(t * TAU / 5);
      const syllable = 0.5 + 0.5 * Math.sin(t * TAU * 1.6);
      const voice = phrase * syllable;
      Object.assign(p, { height: 0.94 + voice * 0.30, width: 1.04 - voice * 0.14,
        depth: 1 + voice * 0.05, taper: voice * 0.22,
        bend: Math.sin(t * TAU / 5) * phrase * 0.23, curve: Math.sin(t * TAU * 0.8) * phrase * 0.13,
        ripple: phrase * (0.035 + syllable * 0.065), phase: -t * TAU * 0.8,
        forward: voice * 0.11, eye: 0.92 + voice * 0.20, ry: Math.sin(t * TAU / 5) * 0.045 }); break;
    }
    case 'sleeping': {
      const inhale = (breath + 1) / 2;
      Object.assign(p, { height: 0.48 + inhale * 0.025, width: 1.30 - inhale * 0.025, depth: 1.13,
        taper: -0.18, bend: 0.30, curve: -0.12, forward: 0.12, eye: 0,
        ry: 0.03, gazeX: 0, gazeY: 0 }); break;
    }
    case 'happy': {
      const envelope = pulse(t, 0, 3.5), hop = Math.max(0, Math.sin(t * 5.4)) ** 2 * envelope;
      Object.assign(p, { y: hop * 0.25, height: 1 + hop * 0.14, width: 1 - hop * 0.10,
        taper: -0.08 * envelope, bend: Math.sin(t * 8) * 0.16 * envelope,
        curve: -Math.sin(t * 8) * 0.10 * envelope, eye: 1 - envelope, ry: 0 }); break;
    }
    case 'curious': {
      const q = pulse(t, 0, 4);
      Object.assign(p, { height: 1 + q * 0.10, width: 1 - q * 0.055,
        bend: Math.sin(t * 1.8) * 0.34 * q, curve: -Math.sin(t * 1.8) * 0.15 * q,
        ry: Math.sin(t * 2) * 0.25 * q, gazeX: Math.sin(t * 2) * 0.04, eye: 1.13 }); break;
    }
    case 'bounce': {
      const crouch = pulse(t, 0, 0.6), jump = pulse(t, 0.55, 1.65), land = pulse(t, 1.65, 2.35);
      Object.assign(p, { y: jump * 0.63, height: 1 - crouch * 0.30 + jump * 0.20 - land * 0.28,
        width: 1 + crouch * 0.23 - jump * 0.12 + land * 0.21, taper: -land * 0.13,
        depth: 1 + crouch * 0.08 + land * 0.07, bend: jump * 0.08 }); break;
    }
    case 'spin': {
      const q = smooth((t - 0.25) / 2.15), lift = Math.sin(q * Math.PI);
      Object.assign(p, { ry: q * TAU, y: lift * 0.16, height: 1 + lift * 0.10,
        width: 1 - lift * 0.06, curve: Math.sin(q * TAU) * 0.10 }); break;
    }
    case 'stretch': {
      const q = pulse(t, 0, 4);
      Object.assign(p, { height: 1 + q * 0.42, width: 1 - q * 0.22, taper: -q * 0.24,
        bend: q * 0.17, curve: -q * 0.12, eye: 1 - q * 0.9, ry: 0 }); break;
    }
    case 'wobble': {
      const q = pulse(t, 0, 3.5), swing = Math.sin(t * 6) * q;
      Object.assign(p, { bend: swing * 0.46, curve: -swing * 0.22,
        height: 1 - Math.abs(swing) * 0.08, width: 1 + Math.abs(swing) * 0.06, ry: 0 }); break;
    }
    case 'wake': {
      const q = smooth((t - 0.4) / 1.5), stretch = pulse(t, 1, 3.8);
      Object.assign(p, { height: 0.49 + q * 0.51 + stretch * 0.25,
        width: 1.29 - q * 0.29 - stretch * 0.13, depth: 1.13 - q * 0.13,
        taper: -0.18 * (1 - q) - stretch * 0.13, bend: 0.3 * (1 - q), curve: -0.12 * (1 - q),
        forward: 0.12 * (1 - q), eye: q, gazeX: 0, gazeY: 0, ry: 0 }); break;
    }
  }
  return p;
}

// CPU equivalent for anchors, bounds, and tests. Rendering evaluates this on the GPU.
export function deformPoint(point, pose, bottom, span, out = {}) {
  const u = clamp((point.y - bottom) / span);
  const contour = 1 + pose.taper * (u - 0.5) + pose.ripple * Math.sin(Math.PI * u) * Math.sin(2 * TAU * u + pose.phase);
  out.x = point.x * pose.width * contour + pose.bend * u * u + pose.curve * Math.sin(Math.PI * u);
  out.y = bottom + (point.y - bottom) * pose.height;
  out.z = point.z * pose.depth + pose.forward * u * u;
  return out;
}


export const DEFORMATION_GLSL = /* glsl */`
uniform vec4 warpShape, warpBend;
uniform vec3 warpRange;
vec3 warpPosition(vec3 p) {
  if (all(equal(warpShape, vec4(1.0, 1.0, 1.0, 0.0))) && all(equal(warpBend, vec4(0.0)))) return p;
  float u = clamp((p.y - warpRange.x) / warpRange.y, 0.0, 1.0);
  float contour = 1.0 + warpShape.w * (u - 0.5)
    + warpBend.w * sin(3.14159265 * u) * sin(12.56637061 * u + warpRange.z);
  return vec3(p.x * warpShape.x * contour + warpBend.x * u * u + warpBend.z * sin(3.14159265 * u),
    warpRange.x + (p.y - warpRange.x) * warpShape.y,
    p.z * warpShape.z + warpBend.y * u * u);
}
vec3 warpNormal(vec3 n, vec3 p) {
  if (all(equal(warpShape, vec4(1.0, 1.0, 1.0, 0.0))) && all(equal(warpBend, vec4(0.0)))) return normalize(n);
  float rawU = (p.y - warpRange.x) / warpRange.y;
  float u = clamp(rawU, 0.0, 1.0);
  float q = sin(3.14159265 * u), c = cos(3.14159265 * u);
  float s = sin(12.56637061 * u + warpRange.z), w = cos(12.56637061 * u + warpRange.z);
  float contour = 1.0 + warpShape.w * (u - 0.5) + warpBend.w * q * s;
  float derivative = warpShape.w + warpBend.w * (3.14159265 * c * s + 12.56637061 * q * w);
  float dx = (p.x * warpShape.x * derivative + 2.0 * warpBend.x * u + warpBend.z * 3.14159265 * c) / warpRange.y;
  float dz = 2.0 * warpBend.y * u / warpRange.y;
  if (rawU < 0.0 || rawU > 1.0) { dx = 0.0; dz = 0.0; }
  vec3 result = vec3(n.x / (warpShape.x * contour), 0.0, n.z / warpShape.z);
  result.y = (n.y - dx * result.x - dz * result.z) / warpShape.y;
  return normalize(result);
}`;
