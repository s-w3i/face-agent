import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { ParametricGeometry } from 'three/addons/geometries/ParametricGeometry.js';
import { addFuzz, makeFeltTexture } from './fur.js';
import { REST_POSE, motionPose, deformPoint } from './motion.js';
import { createDeformation } from './deformation.js';

const TAU = Math.PI * 2;

export class CharacterScene {
  constructor(host, onStats, onError) {
    this.host = host; this.onStats = onStats;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(32, 1, 0.1, 40);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' });
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.NeutralToneMapping;
    this.renderer.toneMappingExposure = 1;
    this.renderer.domElement.setAttribute('aria-label', 'Interactive 3D character. Drag to rotate, scroll to zoom.');
    this.renderer.domElement.setAttribute('role', 'img');
    host.appendChild(this.renderer.domElement);
    this.renderer.domElement.addEventListener('webglcontextlost', e => { e.preventDefault(); onError('The 3D display was interrupted. Reload the page to restore it.'); });

    const pmrem = new THREE.PMREMGenerator(this.renderer);
    const room = new RoomEnvironment();
    this.environment = pmrem.fromScene(room, 0.04, 0.1, 100);
    this.scene.environment = this.environment.texture;
    this.scene.environmentIntensity = 0.32;
    room.dispose(); pmrem.dispose();
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x55535b, 0.85));
    const key = new THREE.DirectionalLight(0xfffaf6, 2.8); key.position.set(-3, 5, 4); this.scene.add(key);
    const fill = new THREE.DirectionalLight(0xeaf1ff, 0.5); fill.position.set(4, 2, 2); this.scene.add(fill);
    const rim = new THREE.DirectionalLight(0xffffff, 1.5); rim.position.set(2, 4, -3); this.scene.add(rim);
    this.feltTexture = makeFeltTexture(); this.fuzzMeshes = [];

    const shadowCanvas = document.createElement('canvas'); shadowCanvas.width = shadowCanvas.height = 128;
    const ctx = shadowCanvas.getContext('2d');
    const gradient = ctx.createRadialGradient(64, 64, 4, 64, 64, 64);
    gradient.addColorStop(0, 'rgba(25,25,30,0.42)'); gradient.addColorStop(0.35, 'rgba(25,25,30,0.20)'); gradient.addColorStop(1, 'rgba(25,25,30,0)');
    ctx.fillStyle = gradient; ctx.fillRect(0, 0, 128, 128);
    this.shadow = new THREE.Mesh(new THREE.PlaneGeometry(3.9, 3.2), new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(shadowCanvas), transparent: true, depthWrite: false }));
    this.shadow.rotation.x = -Math.PI / 2; this.shadow.position.y = 0.01; this.scene.add(this.shadow);
    this.character = new THREE.Group(); this.scene.add(this.character);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true; this.controls.dampingFactor = 0.08; this.controls.enablePan = false;
    this.controls.minDistance = 4.4; this.controls.maxDistance = 12;
    this.controls.minPolarAngle = 0.45; this.controls.maxPolarAngle = 1.7;
    this.resetCamera();
    this.pointer = new THREE.Vector2();
    host.addEventListener('pointermove', e => {
      const rect = host.getBoundingClientRect();
      this.pointer.set((e.clientX - rect.left) / rect.width * 2 - 1, -((e.clientY - rect.top) / rect.height * 2 - 1));
    });
    host.addEventListener('pointerleave', () => this.pointer.set(0, 0));
    this.pose = { ...REST_POSE };
    this.time = 0; this.blinkAt = 2.4; this.blinkTime = 0;
    this.frames = 0; this.statsTime = 0; this.lastFrame = 0;
    this.setQuality('eco');
    this.resizeObserver = new ResizeObserver(() => this.resize()); this.resizeObserver.observe(host);
  }
  resetCamera() { this.camera.position.set(0.1, 2.35, 8.2); this.controls.target.set(0, 1.45, 0); this.controls.update(); }
  setQuality(quality) {
    this.quality = quality; this.targetFPS = quality === 'eco' ? 30 : 60;
    for (const mesh of this.fuzzMeshes) mesh.count = Math.floor(mesh.userData.fullCount * (quality === 'eco' ? 0.5 : 1));
    this.resize();
  }
  resize() {
    const { width, height } = this.host.getBoundingClientRect();
    if (!width || !height) return;
    const cap = this.quality === 'eco' ? 900 : 1800;
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, this.quality === 'eco' ? 1 : 1.75, cap / Math.max(width, height)));
    this.renderer.setSize(width, height);
    this.camera.aspect = width / height; this.camera.updateProjectionMatrix();
  }
  setConfig(config) {
    this.config = { ...config };
    const geometries = new Set(), materials = new Set();
    this.character.traverse(object => {
      if (object.isInstancedMesh) object.dispose();
      if (object.geometry) geometries.add(object.geometry);
      if (object.material) materials.add(object.material);
    });
    geometries.forEach(g => g.dispose()); materials.forEach(m => m.dispose());
    this.character.clear(); this.fuzzMeshes = [];
    const material = (color, roughness = 0.65, metalness = 0) => new THREE.MeshStandardMaterial({ color, roughness, metalness });
    const bodyMat = material(config.color, config.finish === 'glossy' ? 0.23 : 0.95);
    if (config.finish === 'flocked') { bodyMat.map = this.feltTexture; bodyMat.bumpMap = this.feltTexture; bodyMat.bumpScale = 0.045; }
    const black = material('#090909', 0.27);
    const frameMat = material('#131313', 0.48);
    const white = material('#fffefa', 0.35);
    const feltBlack = material('#242424', 1); feltBlack.map = this.feltTexture; feltBlack.bumpMap = this.feltTexture; feltBlack.bumpScale = 0.035;
    const sphere = new THREE.SphereGeometry(1, 28, 20);
    const ellipsoid = (parent, mat, position, scale) => {
      const mesh = new THREE.Mesh(sphere, mat); mesh.position.set(...position); mesh.scale.set(...scale); parent.add(mesh); return mesh;
    };
    const fur = (mesh, color, density, length) => {
      const fibers = addFuzz(mesh, color, density, length); this.fuzzMeshes.push(fibers);
      fibers.count = Math.floor(density * (this.quality === 'eco' ? 0.5 : 1));
    };
    const curve = (parent, points, radius, mat, segments = 24) => {
      const path = new THREE.CatmullRomCurve3(points.map(p => new THREE.Vector3(...p)));
      const mesh = new THREE.Mesh(new THREE.TubeGeometry(path, segments, radius, 7, false), mat); parent.add(mesh); return mesh;
    };
    this.rig = new THREE.Group(); this.character.add(this.rig);
    let width = 1, height = 1, depth = 0.74;
    if (config.shape === 'blob') { width = 1.03; height = 1.02; depth = 0.74; }
    if (config.shape === 'round') { width = 1.02; height = 0.93; depth = 0.82; }
    if (config.shape === 'drop') { width = 0.94; height = 1.08; depth = 0.70; }
    if (config.shape === 'heart') { width = 1.10; height = 1.14; depth = 0.68; }
    const bodyPoint = (u, v, target) => {
      const theta = u * TAU, phi = v * Math.PI;
      const y = Math.cos(phi), ring = Math.sin(phi);
      if (config.shape === 'blob') {
        const x = Math.cos(theta) * ring, z = Math.sin(theta) * ring;
        const angle = Math.atan2(x, y), radius2 = x * x + y * y;
        const contour = 1 + radius2 * radius2 * (0.115 * Math.cos(angle * 6 + 0.35) + 0.035 * Math.sin(angle * 3));
        target.set(x * contour * width, y * contour * height, z * depth);
      } else if (config.shape === 'heart') {
        const hx = Math.sin(theta) * (0.16 + 0.84 * Math.pow(Math.sin(theta), 2));
        const hy = (13 * Math.cos(theta) - 5 * Math.cos(2 * theta) - 2 * Math.cos(3 * theta) - Math.cos(4 * theta)) / 16;
        target.set(hx * ring * width, hy * ring * height + 0.1, Math.cos(phi) * depth);
      } else if (config.shape === 'squircle') {
        const p = x => Math.sign(x) * Math.pow(Math.abs(x), 0.62);
        target.set(p(Math.cos(theta) * ring) * width, p(y) * height, p(Math.sin(theta) * ring) * depth);
      } else {
        let radius = ring;
        if (config.shape === 'drop') radius = Math.pow(ring, 0.78) * (0.9 - 0.45 * y);
        target.set(Math.cos(theta) * radius * width, y * height, Math.sin(theta) * radius * depth);
      }
    };
    const geometry = new ParametricGeometry(bodyPoint, 64, 40);
    // ParametricGeometry has zero tangents at its poles. Supply their limiting normals.
    const normals = geometry.getAttribute('normal');
    for (let u = 0; u <= 64; u++) {
      const frontBack = config.shape === 'heart';
      normals.setXYZ(u, 0, frontBack ? 0 : 1, frontBack ? 1 : 0);
      normals.setXYZ(40 * 65 + u, 0, frontBack ? 0 : -1, frontBack ? -1 : 0);
    }
    if (config.shape === 'heart') {
      const points = geometry.getAttribute('position'), uv = geometry.getAttribute('uv');
      for (let i = 0; i < points.count; i++) uv.setXY(i, points.getX(i) / (width * 2) + 0.5, points.getY(i) / (height * 2) + 0.5);
    }
    this.body = new THREE.Mesh(geometry, bodyMat); this.rig.add(this.body);
    if (config.finish === 'flocked') fur(this.body, config.color, 18000, 0.028);

    // Place features on the actual surface, including lobed and heart silhouettes.
    const ray = new THREE.Raycaster();
    this.body.updateMatrixWorld(true);
    const frontZ = (x, y) => {
      ray.set(new THREE.Vector3(x, y, 3), new THREE.Vector3(0, 0, -1));
      return ray.intersectObject(this.body, false)[0]?.point.z ?? depth;
    };
    this.eyes = [];
    const wide = config.eyes === 'wide';
    const eyeX = wide ? 0.32 : 0.24;
    const eyeY = wide ? height * 0.79 : 0.10;
    for (const side of [-1, 1]) {
      const x = side * eyeX, z = frontZ(x, eyeY);
      const eye = new THREE.Group(); eye.position.set(x, eyeY, z + (wide ? 0.04 : 0.016)); this.rig.add(eye);
      let sclera = null;
      if (wide) {
        const socket = ellipsoid(eye, bodyMat, [0, 0, -0.02], [0.28, 0.32, 0.2]);
        if (config.finish === 'flocked') fur(socket, config.color, 1600, 0.07);
        sclera = ellipsoid(eye, white, [0, 0, 0.15], [0.21, 0.25, 0.14]);
      }
      const eyeScale = config.eyes === 'tiny' ? [0.065, 0.075, 0.055] : wide ? [0.13, 0.165, 0.082] : [0.085, 0.15, 0.055];
      const pupil = ellipsoid(eye, black, [0, 0, wide ? 0.255 : 0.015], eyeScale);
      const lid = curve(eye, [[-0.105, 0.04, 0.03], [-0.067, -0.045, 0.048], [0, -0.075, 0.054], [0.067, -0.045, 0.048], [0.105, 0.04, 0.03]], 0.019, black, 18);
      lid.position.z = wide ? 0.28 : 0.01; lid.visible = false;
      this.eyes.push({ group: eye, sclera, pupil, lid, scaleY: eyeScale[1], baseX: x, baseY: eyeY, baseZ: z, wide });
    }
    if (config.glasses !== 'none') {
      const glasses = new THREE.Group(); glasses.position.set(0, wide ? eyeY : 0.1, 0); this.rig.add(glasses);
      const spacing = wide ? 0.32 : 0.25;
      const frameDepth = frontZ(spacing, glasses.position.y) + (wide ? 0.36 : 0.13);
      for (const side of [-1, 1]) {
        const points = [];
        for (let i = 0; i <= 40; i++) {
          const a = i / 40 * TAU, e = config.glasses === 'square' ? 0.45 : 1;
          const p = n => Math.sign(n) * Math.pow(Math.abs(n), e);
          points.push([side * spacing + 0.23 * p(Math.cos(a)), 0.28 * p(Math.sin(a)), frameDepth]);
        }
        curve(glasses, points, 0.026, frameMat, 40);
        ray.set(new THREE.Vector3(side * 3, glasses.position.y, 0), new THREE.Vector3(-side, 0, 0));
        const edge = ray.intersectObject(this.body, false)[0]?.point.x ?? side * width;
        const templeX = edge * 0.84;
        curve(glasses, [[side * (spacing + 0.23), 0.025, frameDepth], [templeX, 0.025, frontZ(templeX, glasses.position.y) + 0.035], [edge * 0.985, 0.025, 0.08]], 0.024, frameMat, 14);
        if (config.glasses === 'sunglasses') ellipsoid(glasses, feltBlack, [side * spacing, 0, frameDepth], [0.246, 0.295, 0.035]);
      }
      curve(glasses, [[-0.06, 0.02, frameDepth], [0, 0.055, frameDepth + 0.01], [0.06, 0.02, frameDepth]], 0.022, frameMat, 10);
    }
    this.accessory = new THREE.Group(); this.rig.add(this.accessory);
    if (config.accessory === 'beret') {
      const beret = new THREE.Group(); beret.position.set(-0.07, height - 0.09, -0.005); beret.rotation.z = 0.19; beret.rotation.x = -0.08; this.accessory.add(beret);
      const crown = ellipsoid(beret, feltBlack, [-0.04, 0.055, 0], [0.91, 0.26, 0.76]);
      const band = ellipsoid(beret, feltBlack, [0, -0.075, 0.025], [0.74, 0.10, 0.62]);
      const stalk = ellipsoid(beret, feltBlack, [0.02, 0.30, 0], [0.085, 0.115, 0.085]);
      fur(crown, '#242424', 4500, 0.035); fur(band, '#242424', 600, 0.035); fur(stalk, '#242424', 450, 0.04);
    } else if (config.accessory === 'cap') {
      const cap = new THREE.Group(); cap.position.set(0, height - 0.04, 0); cap.rotation.z = -0.18; this.accessory.add(cap);
      const crown = ellipsoid(cap, feltBlack, [0, 0.03, 0], [0.68, 0.26, 0.59]);
      ellipsoid(cap, feltBlack, [0.08, -0.07, 0.5], [0.67, 0.04, 0.43]); fur(crown, '#171717', 3500, 0.03);
    } else if (config.accessory === 'sprout') {
      const green = material('#355b2d');
      curve(this.accessory, [[0, height - 0.04, 0], [0.01, height + 0.14, 0], [0.06, height + 0.25, 0]], 0.025, green, 12);
      const left = ellipsoid(this.accessory, green, [-0.11, height + 0.21, 0], [0.2, 0.045, 0.09]); left.rotation.z = -0.4;
      const right = ellipsoid(this.accessory, green, [0.20, height + 0.3, 0], [0.22, 0.04, 0.1]); right.rotation.z = 0.4;
    } else if (config.accessory === 'halo') {
      const halo = new THREE.Mesh(new THREE.TorusGeometry(0.48, 0.028, 8, 48), material('#d2aa65', 0.32, 0.65));
      halo.rotation.x = Math.PI / 2 + 0.13; halo.position.set(0, height + 0.27, 0); this.accessory.add(halo);
    } else if (config.accessory === 'headphones') {
      const points = [];
      for (let i = 0; i <= 32; i++) { const a = i / 32 * Math.PI; points.push([Math.cos(a) * (width + 0.07), Math.sin(a) * (height + 0.07), -0.03]); }
      curve(this.accessory, points, 0.052, frameMat, 32);
      for (const side of [-1, 1]) ellipsoid(this.accessory, frameMat, [side * (width + 0.035), 0.04, -0.03], [0.13, 0.28, 0.22]);
    }
    this.accessoryAnchor = new THREE.Vector3(0, height, 0);
    if (config.accessory !== 'headphones') {
      this.accessory.userData.rigidAttachment = true;
      this.accessory.position.copy(this.accessoryAnchor);
      for (const child of this.accessory.children) child.position.y -= height;
    }
    geometry.computeBoundingBox();
    this.bodyBottom = geometry.boundingBox.min.y;
    this.bodySpan = geometry.boundingBox.max.y - this.bodyBottom;
    this.baseHeight = -geometry.boundingBox.min.y + 0.065;
    this.rig.position.y = this.baseHeight;
    this.deformation = createDeformation(this.rig, this.bodyBottom, this.bodySpan);
    this.deformation.update(this.pose);
  }
  update(dt, director, snap = false) {
    if (!this.rig) return;
    const t = director.elapsed, state = director.state;
    if (snap) { this.time = t; this.blinkTime = -Infinity; this.blinkAt = t + 2.4; }
    else if (!director.paused) this.time += dt * director.speed;
    const now = this.time;
    const desired = motionPose(state, t, this.pointer);
    if (this.previousState === 'spin' && state !== 'spin') this.pose.ry = ((this.pose.ry + Math.PI) % TAU) - Math.PI;
    this.previousState = state;
    if (snap) Object.assign(this.pose, desired);
    else if (!director.paused) {
      const blend = 1 - Math.exp(-dt * 10 * director.speed);
      for (const key in desired) this.pose[key] = THREE.MathUtils.lerp(this.pose[key], desired[key], blend);
      if (now >= this.blinkAt) { this.blinkTime = now; this.blinkAt = now + 2.5 + Math.random() * 3.5; }
    }
    const p = this.pose;
    const blink = now - this.blinkTime < 0.16 ? Math.abs((now - this.blinkTime) / 0.08 - 1) : 1;
    const relaxed = this.config.eyes === 'relaxed' && !['listening', 'curious', 'wake'].includes(state);
    const eyeOpen = (relaxed ? 0 : p.eye) * blink;
    this.rig.position.y = this.baseHeight + p.y;
    this.rig.rotation.set(p.rx, p.ry, p.rz);
    this.rig.scale.setScalar(1);
    for (const eye of this.eyes) {
      eye.pupil.visible = eyeOpen > 0.18;
      if (eye.sclera) { eye.sclera.visible = eyeOpen > 0.18; eye.sclera.scale.y = 0.25 * Math.max(0.1, eyeOpen); }
      eye.lid.visible = eyeOpen <= 0.18;
      eye.pupil.scale.y = eye.scaleY * Math.max(0.1, eyeOpen);
      if (eye.wide) { eye.pupil.position.x = p.gazeX; eye.pupil.position.y = p.gazeY; }
      else { eye.group.position.x = eye.baseX + p.gazeX; eye.group.position.y = eye.baseY + p.gazeY; }
    }
    if (this.accessory.userData.rigidAttachment) {
      deformPoint(this.accessoryAnchor, p, this.bodyBottom, this.bodySpan, this.accessory.position);
      const u = Math.min(1, (this.accessoryAnchor.y - this.bodyBottom) / this.bodySpan);
      this.accessory.rotation.z = -Math.atan((2 * p.bend * u + p.curve * Math.PI * Math.cos(Math.PI * u)) / (this.bodySpan * p.height));
      this.accessory.rotation.x = Math.atan(2 * p.forward * u / (this.bodySpan * p.height));
    } else this.accessory.rotation.z = Math.sin(now * 1.7) * 0.009;
    this.deformation.update(p);
    this.shadow.scale.set(0.87 * p.width + Math.max(0, p.y) * 0.35, 0.87 * p.depth + Math.max(0, p.y) * 0.35, 1);
    this.shadow.material.opacity = 0.88 - Math.max(0, p.y) * 0.5;
  }
  render(timestamp) {
    const frameInterval = 1000 / this.targetFPS;
    if (timestamp - this.lastFrame < frameInterval) return;
    this.lastFrame = timestamp - ((timestamp - this.lastFrame) % frameInterval);
    this.controls.update(); this.renderer.render(this.scene, this.camera); this.frames++;
    if (timestamp - this.statsTime >= 1000) {
      this.onStats({ fps: Math.round(this.frames * 1000 / (timestamp - this.statsTime)), triangles: this.renderer.info.render.triangles, calls: this.renderer.info.render.calls });
      this.frames = 0; this.statsTime = timestamp;
    }
  }
}
