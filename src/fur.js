import * as THREE from 'three';
import { MeshSurfaceSampler } from 'three/addons/math/MeshSurfaceSampler.js';

// Short, tapered fiber ribbons: one instanced draw, with no per-frame simulation.
// Two density levels share the same buffers so changing quality doesn't allocate.
export function addFuzz(mesh, color, density = 14000, length = 0.04) {
  let seed = 871;
  const random = () => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 4294967296; };
  const strand = new THREE.BufferGeometry();
  strand.setAttribute('position', new THREE.Float32BufferAttribute([
    -0.004, 0, 0, 0.004, 0, 0,
    -0.0028, 0.56, 0.1, 0.0028, 0.56, 0.1,
    0.001, 1, 0.22,
  ], 3));
  strand.setAttribute('color', new THREE.Float32BufferAttribute([
    0.90, 0.90, 0.90, 0.90, 0.90, 0.90,
    0.97, 0.97, 0.97, 0.97, 0.97, 0.97,
    1, 1, 1,
  ], 3));
  strand.setIndex([0, 1, 2, 1, 3, 2, 2, 3, 4]);
  // Shade fibers along the coat's normal, avoiding dark billboard faces.
  strand.setAttribute('normal', new THREE.Float32BufferAttribute(Array.from({ length: 5 }, () => [0, 1, 0]).flat(), 3));
  const material = new THREE.MeshStandardMaterial({ color, roughness: 1, metalness: 0, side: THREE.DoubleSide, vertexColors: true });
  material.onBeforeCompile = shader => {
    shader.fragmentShader = shader.fragmentShader.replace('#include <normal_fragment_begin>', '#include <normal_fragment_begin>\nnormal *= faceDirection;');
  };
  const fibers = new THREE.InstancedMesh(strand, material, density);
  const sampler = new MeshSurfaceSampler(mesh).setRandomGenerator(random).build();
  const point = new THREE.Vector3(), normal = new THREE.Vector3(), up = new THREE.Vector3(0, 1, 0);
  const dummy = new THREE.Object3D();
  const shade = new THREE.Color();
  for (let i = 0; i < density; i++) {
    sampler.sample(point, normal);
    dummy.position.copy(point).addScaledVector(normal, -0.002);
    dummy.quaternion.setFromUnitVectors(up, normal);
    dummy.rotateY(random() * Math.PI * 2);
    const l = length * (0.5 + random() * 0.8);
    dummy.scale.set(0.7 + random() * 0.7, l, l);
    dummy.updateMatrix(); fibers.setMatrixAt(i, dummy.matrix);
    shade.setScalar(0.92 + random() * 0.15); fibers.setColorAt(i, shade);
  }
  fibers.instanceMatrix.needsUpdate = true;
  fibers.instanceColor.needsUpdate = true;
  fibers.userData.fullCount = density;
  mesh.add(fibers);
  return fibers;
}

export function makeFeltTexture() {
  const size = 256, data = new Uint8Array(size * size * 4);
  let seed = 5147;
  for (let i = 0; i < size * size; i++) {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    const n = 145 + (seed >>> 24) * 0.43;
    data[i * 4] = data[i * 4 + 1] = data[i * 4 + 2] = n;
    data[i * 4 + 3] = 255;
  }
  const texture = new THREE.DataTexture(data, size, size, THREE.RGBAFormat);
  texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
  texture.magFilter = THREE.LinearFilter; texture.minFilter = THREE.LinearMipmapLinearFilter;
  texture.generateMipmaps = true; texture.repeat.set(2, 1.4); texture.needsUpdate = true;
  return texture;
}
