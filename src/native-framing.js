// Fit actual rendered pixels, including native props, rather than guessing by state.
// A small alpha sample avoids scanning full-resolution frames.
const SAMPLE_SIZE = 64;

export function paintedBounds(pixels, size = SAMPLE_SIZE) {
  let left = size, top = size, right = -1, bottom = -1;
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    if (pixels[(y * size + x) * 4 + 3] < 4) continue;
    left = Math.min(left, x); top = Math.min(top, y);
    right = Math.max(right, x); bottom = Math.max(bottom, y);
  }
  if (right < 0) return null;
  // Include faint edges and subpixel features lost in the small sample.
  return { left: Math.max(0, left - 2) / size, top: Math.max(0, top - 2) / size,
    right: Math.min(size, right + 3) / size, bottom: Math.min(size, bottom + 3) / size };
}

export function fittedLayout(bounds, width, height) {
  const margin = Math.min(16, Math.min(width, height) * .025);
  const spanX = bounds.right - bounds.left, spanY = bounds.bottom - bounds.top;
  const size = Math.min((width - margin * 2) / spanX, (height - margin * 2) / spanY);
  return { size, left: (width - spanX * size) / 2 - bounds.left * size,
    top: (height - spanY * size) / 2 - bounds.top * size };
}

export class NativeFraming {
  constructor() {
    this.sample = document.createElement('canvas');
    this.sample.width = this.sample.height = SAMPLE_SIZE;
    this.context = this.sample.getContext('2d', { willReadFrequently: true });
    this.reset();
  }
  reset() { this.bounds = null; this.lastLayout = ''; }
  update(canvas, width, height, rendered) {
    if (rendered && this.context) {
      this.context.clearRect(0, 0, SAMPLE_SIZE, SAMPLE_SIZE);
      this.context.drawImage(canvas, 0, 0, SAMPLE_SIZE, SAMPLE_SIZE);
      const next = paintedBounds(this.context.getImageData(0, 0, SAMPLE_SIZE, SAMPLE_SIZE).data);
      if (next) {
        // Retain the motion envelope until the next action to avoid breathing zoom.
        const previous = this.bounds || next;
        this.bounds = { left: Math.min(previous.left, next.left), top: Math.min(previous.top, next.top),
          right: Math.max(previous.right, next.right), bottom: Math.max(previous.bottom, next.bottom) };
      }
    }
    if (!this.bounds || width <= 0 || height <= 0) return null;
    const layout = fittedLayout(this.bounds, width, height);
    const key = `${layout.size},${layout.left},${layout.top}`;
    if (key === this.lastLayout) return null;
    this.lastLayout = key;
    Object.assign(canvas.style, { width: `${layout.size}px`, height: `${layout.size}px`,
      left: `${layout.left}px`, top: `${layout.top}px` });
    return layout.size;
  }
}
