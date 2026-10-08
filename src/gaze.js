// Frame-local eye bounds keep gaze attached to the baked head, including deformations.
// ponytail: dark capsule eyes on an unobstructed face; unsupported frames keep original eyes.
export function eyeBounds(pixels, size = 512) {
  const visited = new Uint8Array(size * size), candidates = [];
  let left = size, top = size, right = 0, bottom = 0;
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    const i = (y * size + x) * 4, r = pixels[i], g = pixels[i + 1], b = pixels[i + 2];
    if (pixels[i + 3] > 220 && r > 100 && r > g * 1.5 && r > b * 1.1) {
      left = Math.min(left, x); right = Math.max(right, x); top = Math.min(top, y); bottom = Math.max(bottom, y);
    }
  }
  if (right <= left) return [];
  const dark = i => pixels[i * 4 + 3] > 220 && Math.max(pixels[i * 4], pixels[i * 4 + 1], pixels[i * 4 + 2]) < 75;
  const minY = Math.max(1, Math.floor(top + (bottom - top) * .12)), maxY = Math.min(size - 2, Math.ceil(bottom - (bottom - top) * .1));
  for (let y = minY; y <= maxY; y++) for (let x = Math.max(1, left); x < Math.min(size - 1, right); x++) {
    const start = y * size + x;
    if (visited[start] || !dark(start)) continue;
    const queue = [start]; visited[start] = 1;
    let x0 = x, y0 = y, x1 = x, y1 = y;
    for (let q = 0; q < queue.length; q++) {
      const p = queue[q], px = p % size, py = Math.floor(p / size);
      x0 = Math.min(x0, px); x1 = Math.max(x1, px); y0 = Math.min(y0, py); y1 = Math.max(y1, py);
      for (const n of [p - 1, p + 1, p - size, p + size]) {
        const nx = n % size, ny = Math.floor(n / size);
        if (nx < 1 || nx >= size - 1 || ny < minY || ny > maxY || visited[n] || !dark(n)) continue;
        visited[n] = 1; queue.push(n);
      }
    }
    const w = x1 - x0 + 1, h = y1 - y0 + 1;
    if (queue.length >= 8 && w >= 4 && w <= size * .12 && h <= size * .22 && h <= w * 4) candidates.push([x0, y0, x1 + 1, y1 + 1]);
  }
  let best = null, score = Infinity;
  for (const a of candidates) for (const b of candidates) {
    const ax = (a[0] + a[2]) / 2, bx = (b[0] + b[2]) / 2;
    const ay = (a[1] + a[3]) / 2, by = (b[1] + b[3]) / 2;
    const gap = b[0] - a[2], width = Math.max(a[2] - a[0], b[2] - b[0]);
    if (ax >= bx || gap < 4 || gap > (right - left) * .4 || Math.abs(ay - by) > width * .8) continue;
    const next = Math.abs((ax + bx) / 2 - (left + right) / 2) + Math.abs((ay + by) / 2 - (top + (bottom - top) * .4)) * 2 + Math.abs((a[2] - a[0]) - (b[2] - b[0])) * 2;
    if (next < score) { best = [a, b]; score = next; }
  }
  return best ? best.map(([x0, y0, x1, y1]) => [Math.max(0, x0 - 3), Math.max(0, y0 - 3), Math.min(size, x1 + 3), Math.min(size, y1 + 3)]) : [];
}

export class SmoothGaze {
  constructor() { this.target = [0, 0]; this.position = [0, 0]; this.velocity = [0, 0]; this.received = -Infinity; this.detected = false; }
  receive(packet, now) {
    this.detected = packet.detected === true;
    this.received = now - Math.max(0, packet.ageMs || 0);
    if (this.detected) for (const [i, value] of [packet.x, packet.y].entries()) {
      const target = Math.max(-1, Math.min(1, value * 2 - 1));
      if (Math.abs(target - this.target[i]) >= .025) this.target[i] = target;
    }
  }
  step(dt, now, awake = true, enabled = true, response = 140) {
    this.tracking = enabled && awake && this.detected && now - this.received <= 1000;
    const omega = 2 / (response / 1000), decay = Math.exp(-omega * dt);
    for (let i = 0; i < 2; i++) {
      const target = this.tracking ? this.target[i] : 0;
      const change = this.position[i] - target, temp = (this.velocity[i] + omega * change) * dt;
      this.position[i] = target + (change + temp) * decay;
      this.velocity[i] = (this.velocity[i] - omega * temp) * decay;
      if (Math.abs(this.position[i] - target) < .0001 && Math.abs(this.velocity[i]) < .001) { this.position[i] = target; this.velocity[i] = 0; }
    }
    return this.position;
  }
}

// Translate the eye pair intact; ease the surrounding baked fur back to fixed edges.
// Small Canvas 2D slices avoid eye cutouts, inpainting, or another 3D renderer.
export function drawGaze(context, image, sx, sy, eyes, gaze, size = 512) {
  if (eyes?.length !== 2 || Math.max(Math.abs(gaze[0]), Math.abs(gaze[1])) < .0001) return;
  const x0 = Math.min(...eyes.map(e => e[0])), y0 = Math.min(...eyes.map(e => e[1]));
  const x1 = Math.max(...eyes.map(e => e[2])), y1 = Math.max(...eyes.map(e => e[3]));
  // Move the pair together: eye spacing stays intact and the patches cannot overlap.
  const width = Math.min(...eyes.map(e => e[2] - e[0])), h = y1 - y0;
  const mx = Math.min(width * .7, x0, size - x1), my = Math.min(Math.max(8, h * .35), y0, size - y1);
  if (mx < 2 || my < 2) return;
  const dx = gaze[0] * mx * .65, dy = gaze[1] * Math.min(my * .65, h * .2);
  const axis = (a, b, margin, offset) => {
    if (Math.abs(offset) < .0001) return [[a - margin, b + margin], [a - margin, b + margin]];
    const source = [a - margin, a - margin * 2 / 3, a - margin / 3, a, b, b + margin / 3, b + margin * 2 / 3, b + margin];
    const weight = [0, 7 / 27, 20 / 27, 1, 1, 20 / 27, 7 / 27, 0];
    return [source, source.map((v, i) => v + offset * weight[i])];
  };
  const [xs, xd] = axis(x0, x1, mx, dx), [ys, yd] = axis(y0, y1, my, dy);
  for (let row = 0; row < ys.length - 1; row++) for (let col = 0; col < xs.length - 1; col++) {
    context.drawImage(image, sx + xs[col], sy + ys[row], xs[col + 1] - xs[col], ys[row + 1] - ys[row],
      xd[col], yd[row], xd[col + 1] - xd[col], yd[row + 1] - yd[row]);
  }
}
