import { drawGaze } from './gaze.js';

export function bakedFrame(clip, seconds, fps, level = 0) {
  if (clip.id === 'speaking') return Math.round(Math.max(0, Math.min(1, level)) * (clip.count - 1));
  const frame = Math.floor(Math.max(0, seconds) * fps);
  if (frame < clip.count) return frame;
  return clip.loopStart === null ? clip.count - 1 : clip.loopStart + (frame - clip.loopStart) % (clip.count - clip.loopStart);
}

export function matchingBake(config, manifest) {
  return !!manifest && config.resourceRevision === manifest.config.resourceRevision &&
    config.appearance.state === manifest.config.appearance.state &&
    ['motionStrength', 'reducedMotion', 'background'].every(key => config.settings[key] === manifest.config.settings[key]);
}

export class BakedRenderer {
  constructor(canvas, manifest) {
    this.canvas = canvas; this.context = canvas.getContext('2d'); this.manifest = manifest;
    canvas.width = canvas.height = manifest.size;
    this.cache = new Map(); this.disposed = false;
  }
  sheet(clip, index) {
    const filename = clip.sheets[index];
    let entry = this.cache.get(filename);
    if (entry) { entry.used = performance.now(); return entry; }
    entry = { image: null, used: performance.now(), speaking: clip.id === 'speaking' };
    this.cache.set(filename, entry);
    entry.ready = fetch(`/prerendered/${this.manifest.id}/${filename}`).then(response => {
      if (!response.ok) throw new Error(`Missing animation sheet: ${filename}`);
      return response.blob();
    }).then(createImageBitmap).then(image => {
      if (this.disposed || this.cache.get(filename) !== entry) { image.close(); return; }
      entry.image = image; this.trim();
    }).catch(error => { this.error = error; });
    return entry;
  }
  trim() {
    const ordinary = [...this.cache.entries()].filter(([, entry]) => !entry.speaking);
    ordinary.sort((a, b) => a[1].used - b[1].used);
    while (ordinary.length > 3) {
      const [filename, entry] = ordinary.shift(); entry.image?.close(); this.cache.delete(filename);
    }
  }
  async prepare(clip) {
    await this.sheet(clip, 0).ready;
    if (clip.id === 'speaking') await Promise.all(clip.sheets.map((_, i) => this.sheet(clip, i).ready));
    if (this.error) throw this.error;
  }
  draw(clip, frame, gaze = [0, 0]) {
    const { columns, size } = this.manifest, perSheet = columns * columns;
    const sheetIndex = Math.floor(frame / perSheet), entry = this.sheet(clip, sheetIndex);
    const nextIndex = sheetIndex + 1 < clip.sheets.length ? sheetIndex + 1 : Math.floor((clip.loopStart || 0) / perSheet);
    this.sheet(clip, nextIndex);
    if (!entry.image) return false;
    const slot = frame % perSheet;
    this.context.clearRect(0, 0, size, size);
    this.context.drawImage(entry.image, (slot % columns) * size, Math.floor(slot / columns) * size, size, size, 0, 0, size, size);
    drawGaze(this.context, entry.image, (slot % columns) * size, Math.floor(slot / columns) * size, clip.eyes?.[frame], gaze, size);
    return true;
  }
  dispose() { this.disposed = true; for (const entry of this.cache.values()) entry.image?.close(); this.cache.clear(); }
}
