import { attachVoiceMotion, voiceMotionPose } from './native-voice.js';
import { REST_POSE } from './motion.js';
import { paintedBounds } from './native-framing.js';
import { eyeBounds } from './gaze.js';

const nextFrame = () => new Promise(resolve => requestAnimationFrame(resolve));
const SIZE = 512, FPS = 16, COLUMNS = 4, PER_SHEET = 16;

async function request(path, body, signal, binary = false) {
  const response = await fetch(`/api/prerender${path}`, { method: 'POST', signal,
    headers: { 'Content-Type': binary ? 'image/webp' : 'application/json' }, body: binary ? body : JSON.stringify(body) });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Unable to save animation pack.');
  return result;
}

// Capture the native geometry, fur, accessories and props. The Pi only draws pixels.
export async function bakeDots(engine, config, actions, signal, progress) {
  const { id } = await request('', {}, signal);
  const manifest = { version: 1, id, config, size: SIZE, fps: FPS, columns: COLUMNS, clips: [], gazeVersion: 1 };
  const state = Uint8Array.from(atob(config.appearance.state), c => c.charCodeAt(0));
  const sheet = document.createElement('canvas'); sheet.width = sheet.height = SIZE * COLUMNS;
  const context = sheet.getContext('2d');
  const snapshot = document.createElement('canvas'); snapshot.width = snapshot.height = SIZE;
  const snapshotContext = snapshot.getContext('2d', { willReadFrequently: true });
  const sample = document.createElement('canvas'); sample.width = sample.height = 64;
  const sampleContext = sample.getContext('2d', { willReadFrequently: true });
  const library = actions.filter(a => a.id !== 'signature' || config.appearance.signatureAvailable);
  let canvas, character, voice;
  const release = () => { character?.delete(); character = null; voice?.dispose(); canvas?.remove(); };
  try {
    for (const [index, action] of library.entries()) {
      signal.throwIfAborted(); release();
      canvas = document.createElement('canvas'); canvas.id = 'bake-character';
      Object.assign(canvas.style, { position: 'fixed', left: '-10000px', width: `${SIZE}px`, height: `${SIZE}px` });
      document.body.append(canvas);
      voice = attachVoiceMotion(canvas);
      const bound = !['reaction', 'gesture', 'hero'].includes(action.kind);
      character = new engine.Character('#bake-character', SIZE, SIZE, { activities: bound });
      character.setQuality(2); character.setDisplayScale(1); character.setReducedMotion(config.settings.reducedMotion);
      character.setActivityTheme(config.settings.background === 'dark' ? 1 : 0);
      const error = character.restore(action.hero ? engine.presetAppearance(action.hero) : state);
      if (error) throw new Error(error);
      let sequence = 0n;
      const activity = command => character.applyActivity({ sequence: ++sequence, episodeId: 1n, episodeHighWater: 0n,
        command, activity: action.activity || 7, outcome: command === 2 ? action.outcome : 0, entry: 0 });
      if (bound && activity(1) !== 0) throw new Error(`Unable to bake ${action.label}.`);
      let ready = false; character.onFirstFrame(() => { ready = true; });
      const render = seconds => {
        if (character.render(seconds)) {
          snapshotContext.clearRect(0, 0, SIZE, SIZE); snapshotContext.drawImage(canvas, 0, 0);
        }
      };
      snapshotContext.clearRect(0, 0, SIZE, SIZE);
      const deadline = performance.now() + 20000;
      while (!ready) {
        signal.throwIfAborted();
        if (performance.now() > deadline) throw new Error(`Renderer timed out preparing ${action.label}.`);
        await nextFrame(); render(performance.now() / 1000);
      }
      if (action.reaction && character.playReaction(action.reaction) !== 0) throw new Error(`Signature unavailable: ${action.label}.`);
      const speaking = action.id === 'speaking';
      const count = speaking ? 21 : (action.duration || (action.id === 'listening' ? 5 : 6)) * FPS;
      const clip = { id: action.id, label: action.label, count, loopStart: action.duration || speaking ? null : 0, bounds: null, sheets: [], eyes: [] };
      let cue = 0, stopped = false, released = false;
      let clock = performance.now() / 1000;
      // Settle looping statuses before capture; finite interactions keep their entry.
      if (!action.duration) for (let step = 0; step < FPS; step++) {
        signal.throwIfAborted(); await nextFrame(); clock += 1 / FPS; render(clock);
      }
      for (let frame = 0; frame < count; frame++) {
        signal.throwIfAborted(); await nextFrame(); clock += 1 / FPS;
        const elapsed = frame / FPS;
        if (action.kind === 'gaze') {
          if (elapsed < 2.5) character.setReadyGaze(...action.target, clock);
          else if (!released) { character.clearReadyGaze(clock); released = true; }
        }
        while (action.cues && cue < action.cues.length && elapsed >= action.cues[cue][0]) {
          const [, phase, x, y] = action.cues[cue++]; character.pointer(phase, 1, x, y, clock);
        }
        if (action.kind === 'outcome' && elapsed >= 2.5 && !stopped) {
          if (activity(2) !== 0) throw new Error(`Unable to bake completion: ${action.label}.`);
          stopped = true;
        }
        voice.update(action.kind === 'voice' ? voiceMotionPose(action.id, speaking ? 0 : elapsed, speaking ? frame / 20 : null,
          config.settings.motionStrength, config.settings.reducedMotion) : REST_POSE);
        // render(false) means no new submission; retain the last completed pixels.
        render(clock);
        clip.eyes.push(action.hero || action.id === 'sleeping' ? [] : eyeBounds(snapshotContext.getImageData(0, 0, SIZE, SIZE).data));
        const slot = frame % PER_SHEET;
        if (slot === 0) context.clearRect(0, 0, sheet.width, sheet.height);
        context.drawImage(snapshot, (slot % COLUMNS) * SIZE, Math.floor(slot / COLUMNS) * SIZE);
        sampleContext.clearRect(0, 0, 64, 64); sampleContext.drawImage(snapshot, 0, 0, 64, 64);
        const bounds = paintedBounds(sampleContext.getImageData(0, 0, 64, 64).data);
        if (bounds) {
          const previous = clip.bounds || bounds;
          clip.bounds = { left: Math.min(previous.left, bounds.left), top: Math.min(previous.top, bounds.top),
            right: Math.max(previous.right, bounds.right), bottom: Math.max(previous.bottom, bounds.bottom) };
        }
        progress(`${index + 1}/${library.length} · ${action.label} · ${Math.round((frame + 1) / count * 100)}%`);
        if (slot === PER_SHEET - 1 || frame === count - 1) {
          const blob = await new Promise(resolve => sheet.toBlob(resolve, 'image/webp', .9));
          if (!blob || blob.type !== 'image/webp') throw new Error('This browser cannot export WebP. Use Chromium on your computer.');
          const filename = `${action.id.replace(/[:_]/g, '-')}-${clip.sheets.length}.webp`;
          await request(`/${id}/${filename}`, blob, signal, true); clip.sheets.push(filename);
        }
      }
      if (!clip.bounds) throw new Error(`Empty animation: ${action.label}.`);
      manifest.clips.push(clip);
    }
    await request(`/${id}`, manifest, signal);
    return manifest;
  } catch (error) {
    await request(`/${id}/cancel`, {}).catch(() => {});
    throw error;
  } finally { release(); }
}

export async function prepareGaze(manifest, signal, progress) {
  const canvas = document.createElement('canvas'); canvas.width = canvas.height = manifest.size;
  const context = canvas.getContext('2d', { willReadFrequently: true }), clips = {};
  for (const [index, clip] of manifest.clips.entries()) {
    const eyes = clips[clip.id] = [];
    for (const [sheetIndex, filename] of clip.sheets.entries()) {
      signal.throwIfAborted();
      const response = await fetch(`/prerendered/${manifest.id}/${filename}`, { signal });
      if (!response.ok) throw new Error('Missing body frames. Bake this character again.');
      const bitmap = await createImageBitmap(await response.blob());
      try {
        for (let slot = 0; slot < 16 && sheetIndex * 16 + slot < clip.count; slot++) {
          context.clearRect(0, 0, canvas.width, canvas.height);
          context.drawImage(bitmap, slot % 4 * canvas.width, Math.floor(slot / 4) * canvas.height, canvas.width, canvas.height, 0, 0, canvas.width, canvas.height);
          eyes.push(clip.id.startsWith('signature:') || clip.id === 'sleeping' ? [] : eyeBounds(context.getImageData(0, 0, canvas.width, canvas.height).data));
        }
      } finally { bitmap.close(); }
      progress(`${index + 1}/${manifest.clips.length} · ${clip.label}`); await nextFrame();
    }
  }
  await request(`/${manifest.id}/gaze`, clips, signal);
  return { ...manifest, gazeVersion: 1, clips: manifest.clips.map(clip => ({ ...clip, eyes: clips[clip.id] })) };
}
