import * as THREE from 'three';
import { DEFORMATION_GLSL } from './motion.js';

const warpGLSL = 'uniform mat4 warpToRig, warpFromRig;\nuniform mat3 warpNormalToRig, warpNormalFromRig;\n' + DEFORMATION_GLSL;

// Body, fitted face parts, and instanced fibers share one deformation field.
// Matrices are tiny CPU updates; body vertices and hairs never get rebuilt per frame.
export function createDeformation(rig, bottom, span) {
  const shape = { value: new THREE.Vector4(1, 1, 1, 0) };
  const bend = { value: new THREE.Vector4() };
  const range = { value: new THREE.Vector3(bottom, span, 0) };
  const bindings = [], originals = new Set(), inverseRig = new THREE.Matrix4();
  rig.traverse(mesh => {
    if (!mesh.isMesh) return;
    for (let parent = mesh; parent !== rig; parent = parent.parent) if (parent.userData.rigidAttachment) return;
    mesh.frustumCulled = false;
    const original = mesh.material, beforeCompile = original.onBeforeCompile;
    const programKey = original.customProgramCacheKey();
    const uniforms = {
      warpShape: shape, warpBend: bend, warpRange: range,
      warpToRig: { value: new THREE.Matrix4() }, warpFromRig: { value: new THREE.Matrix4() },
      warpNormalToRig: { value: new THREE.Matrix3() }, warpNormalFromRig: { value: new THREE.Matrix3() },
    };
    mesh.material = original.clone(); originals.add(original);
    mesh.material.customProgramCacheKey = () => 'body-warp-v1:' + programKey;
    mesh.material.onBeforeCompile = shader => {
      beforeCompile.call(mesh.material, shader);
      Object.assign(shader.uniforms, uniforms);
      shader.vertexShader = warpGLSL + '\n' + shader.vertexShader;
      const normals = THREE.ShaderChunk.defaultnormal_vertex.replace(
        'transformedNormal = normalMatrix * transformedNormal;',
        `vec4 sourcePosition = vec4(position, 1.0);
        #ifdef USE_INSTANCING
          sourcePosition = instanceMatrix * sourcePosition;
        #endif
        vec3 rigPosition = (warpToRig * sourcePosition).xyz;
        transformedNormal = warpNormalToRig * transformedNormal;
        transformedNormal = warpNormal(transformedNormal, rigPosition);
        transformedNormal = normalMatrix * warpNormalFromRig * transformedNormal;`,
      );
      const projection = THREE.ShaderChunk.project_vertex.replace(
        'mvPosition = modelViewMatrix * mvPosition;',
        `mvPosition = warpToRig * mvPosition;
        mvPosition = warpFromRig * vec4(warpPosition(mvPosition.xyz), 1.0);
        mvPosition = modelViewMatrix * mvPosition;`,
      );
      shader.vertexShader = shader.vertexShader.replace('#include <defaultnormal_vertex>', normals)
        .replace('#include <project_vertex>', projection);
    };
    bindings.push({ mesh, uniforms });
  });
  originals.forEach(material => material.dispose());
  return {
    update(pose) {
      shape.value.set(pose.width, pose.height, pose.depth, pose.taper);
      bend.value.set(pose.bend, pose.forward, pose.curve, pose.ripple);
      range.value.z = pose.phase;
      rig.updateMatrixWorld(true); inverseRig.copy(rig.matrixWorld).invert();
      for (const { mesh, uniforms: u } of bindings) {
        u.warpToRig.value.multiplyMatrices(inverseRig, mesh.matrixWorld);
        u.warpFromRig.value.copy(u.warpToRig.value).invert();
        u.warpNormalToRig.value.getNormalMatrix(u.warpToRig.value);
        u.warpNormalFromRig.value.getNormalMatrix(u.warpFromRig.value);
      }
    },
  };
}
