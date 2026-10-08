import './style.css';
import { BakedRenderer, bakedFrame, matchingBake } from './prerendered.js';
import { IdleMovements } from './native-actions.js';
import { fittedLayout } from './native-framing.js';
import { SpeechPlayer } from './speech.js';
import { SmoothGaze } from './gaze.js';
import { gazeControls } from './gaze-controls.js';

async function api(path, value) {
  const response = await fetch(`/api/${path}`, value === undefined ? { cache: 'no-store' } : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The local robot service is unavailable.');
  return result;
}

export async function startBakedDisplay(config) {
  document.body.classList.add('robot-display');
  document.querySelector('#app').innerHTML = `<div class="baked-viewport" id="native-viewport"><canvas id="original-character" role="img" aria-label="Pre-rendered dots character"></canvas></div>
    <p id="robot-subtitles" class="robot-subtitles" dir="auto" aria-label="Speech subtitles" hidden></p>
    <div class="baked-loading" id="baked-loading" role="status">Loading saved animation pack…</div>
    <div class="robot-hud"><span>AI-generated voice · OpenAI</span><a href="./original-dots.html">Studio</a><button id="device-key-open">API key</button><button id="robot-fullscreen">Enter fullscreen</button><span id="robot-connection" role="status">Connecting…</span></div><div id="toast" class="toast" role="status"></div>
    <dialog id="device-key-dialog" class="key-dialog" aria-labelledby="device-key-title"><form id="device-key-form"><h2 id="device-key-title">Set up this device’s API key</h2><p>Each device needs its own OpenAI API key setup. The key stays in a private file on this device and is excluded from Git, character configuration and animation packs.</p><label class="field-label" for="device-key-input">OpenAI API key</label><input id="device-key-input" class="full-input" type="password" autocomplete="new-password" placeholder="sk-…" required><p id="device-key-error" role="alert"></p><div class="speech-buttons"><button id="device-key-submit" class="outline-button" type="submit">Save API key</button><button id="device-key-later" class="text-button" type="button">Later</button></div></form></dialog>`;
  const $ = selector => document.querySelector(selector);
  const manifest = await api('prerender');
  if (!manifest || !config) {
    $('#baked-loading').textContent = 'Bake your saved character in the studio first.'; return;
  }
  document.body.dataset.renderer = 'prerendered';
  document.body.dataset.robotBackground = config.settings.background;
  const canvas = $('#original-character'), renderer = new BakedRenderer(canvas, manifest);
  const clips = new Map(manifest.clips.map(clip => [clip.id, clip]));
  const idle = new IdleMovements(); idle.reset(config.settings.idleInterval);
  let active, elapsed = 0, started = 0, commandLevel = null, smoothedLevel = 0, lastFrame = 0, drawn = '';
  let pendingSpeech, speechFetch, request = 0, playTicket = 0, events, lastCommand = -1, generation = '', toastTimer, packRevision;
  const speech = new SpeechPlayer(text => { $('#robot-subtitles').textContent = text; $('#robot-subtitles').hidden = !text; drawn = ''; });
  const toast = message => { $('#toast').textContent = message; $('#toast').classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').classList.remove('show'), 6000); };
  const gaze = new SmoothGaze(), controls = gazeControls(api, toast);
  function openKeySetup() { $('#device-key-input').value = ''; $('#device-key-error').textContent = ''; $('#device-key-dialog').showModal(); }
  $('#device-key-open').addEventListener('click', openKeySetup);
  $('#device-key-later').addEventListener('click', () => $('#device-key-dialog').close());
  $('#device-key-dialog').addEventListener('close', () => { $('#device-key-input').value = ''; });
  $('#device-key-form').addEventListener('submit', async event => {
    event.preventDefault(); $('#device-key-submit').disabled = true; $('#device-key-error').textContent = '';
    const apiKey = $('#device-key-input').value.trim(); $('#device-key-input').value = '';
    try {
      await api('key', { apiKey }); $('#device-key-dialog').close(); toast('API key saved privately on this device.');
    } catch (error) { $('#device-key-error').textContent = error.message; }
    finally { $('#device-key-submit').disabled = false; }
  });
  function status() { $('#robot-connection').textContent = matchingBake(config, manifest) ? 'Pre-rendered · Connected' : 'Pre-rendered · Re-bake updated look in Studio'; }
  function stopSpeech() { request++; pendingSpeech = null; speechFetch?.abort(); speechFetch = null; speech.stop(); }
  async function play(id, keepSpeech = false) {
    const clip = clips.get(id);
    if (!clip) throw new Error('This animation is absent from the saved pack. Re-bake it in the studio.');
    if (!keepSpeech) stopSpeech();
    const ticket = ++playTicket;
    await renderer.prepare(clip);
    if (ticket !== playTicket) return;
    active = clip; elapsed = 0; started = performance.now(); drawn = ''; commandLevel = null; idle.reset();
    canvas.dataset.state = id; $('#baked-loading').hidden = true;
  }
  async function resumeVoice() {
    let command, token, error = '';
    try {
      await speech.enable();
      if (speech.context?.state !== 'running' || !pendingSpeech) return;
      command = pendingSpeech; pendingSpeech = null; token = request;
      await renderer.prepare(clips.get('speaking'));
      if (token !== request) return;
      speechFetch = new AbortController();
      const response = await fetch(command.speech, { cache: 'no-store', signal: speechFetch.signal });
      if (token !== request) return;
      if (!response.ok) throw new Error((await response.json()).error || 'Unable to load speech.');
      const onStart = () => { play('speaking', true).catch(failure => toast(failure.message)); };
      const onEnd = () => { play('idle').catch(failure => toast(failure.message)); };
      if (response.headers.get('Content-Type')?.includes('application/x-ndjson')) await speech.stream(response, onStart, onEnd, () => {}, command.text || '');
      else await speech.play(await response.blob(), onStart, onEnd, command.text || '');
    } catch (failure) {
      if (failure.name === 'AbortError' || (token !== undefined && token !== request)) return;
      error = failure.message; toast(error); play('idle').catch(() => {});
    }
    if (command) api('ack', { generation: command.generation, sequence: command.sequence, state: command.state, error }).catch(failure => toast(failure.message));
  }
  await play('idle');
  renderer.prepare(clips.get('speaking')).catch(error => toast(error.message));
  await api('actions', manifest.clips.map(({ id, label }) => ({ id, label })));
  events = new EventSource('/api/events');
  events.onopen = status;
  events.onerror = () => { $('#robot-connection').textContent = 'Reconnecting…'; };
  events.addEventListener('config-error', event => toast(JSON.parse(event.data).error));
  events.addEventListener('config', event => {
    config = JSON.parse(event.data);
    if (config.settings.playback !== 'prerendered' && new URLSearchParams(location.search).get('renderer') !== 'prerendered') { location.reload(); return; }
    document.body.dataset.robotBackground = config.settings.background; idle.reset(config.settings.idleInterval); status(); drawn = '';
  });
  events.addEventListener('prerender', event => {
    const pack = JSON.parse(event.data);
    if (pack.id && (pack.id !== manifest.id || (pack.gazeVersion || 0) !== (manifest.gazeVersion || 0) || (packRevision !== undefined && packRevision !== pack.revision))) location.reload();
    packRevision = pack.revision;
  });
  events.addEventListener('gaze', event => { gaze.receive(JSON.parse(event.data), performance.now()); });
  events.addEventListener('command', async event => {
    const command = JSON.parse(event.data);
    if (command.generation === generation && command.sequence <= lastCommand) return;
    generation = command.generation; lastCommand = command.sequence;
    let error = '';
    try {
      await play(command.speech ? 'idle' : command.state);
      if (command.generation !== generation || command.sequence !== lastCommand) return;
      commandLevel = command.level;
      if (command.speech) {
        pendingSpeech = command; resumeVoice();
        if (speech.context?.state !== 'running') toast('This browser blocks automatic audio. Use ./run.sh --robot, or tap anywhere.');
        return;
      }
    } catch (failure) { error = failure.message; toast(error); }
    api('ack', { generation: command.generation, sequence: command.sequence, state: command.state, error }).catch(failure => toast(failure.message));
  });
  function frame(now) {
    if (!document.hidden && active && now - lastFrame >= 1000 / 30) {
      const dt = Math.min(.1, (now - lastFrame) / 1000); lastFrame = now;
      elapsed = (now - started) / 1000;
      if (active.id !== 'speaking' && active.loopStart === null && elapsed >= active.count / manifest.fps) play('idle').catch(error => toast(error.message));
      const random = idle.tick(dt, { idle: active.id === 'idle', enabled: config.settings.autoIdle,
        reduced: config.settings.reducedMotion, signatureAvailable: clips.has('signature') });
      if (random && !(gaze.tracking && random.startsWith('look-'))) play(random).catch(error => toast(error.message));
      const level = speech.level() ?? commandLevel ?? (config.settings.speechSource === 'level' ? config.settings.speechLevel : Math.max(0, Math.sin(elapsed * 9) * .65 + Math.sin(elapsed * 3) * .25));
      smoothedLevel += (level - smoothedLevel) * (1 - Math.exp(-15 * dt));
      const eyeGaze = gaze.step(dt, now, active.id !== 'sleeping', config.settings.eyeTracking !== false, config.settings.gazeResponse || 140);
      const index = bakedFrame(active, elapsed, manifest.fps, smoothedLevel), key = `${active.id}:${index}:${eyeGaze.map(v => v.toFixed(4))}`;
      if (key !== drawn && renderer.draw(active, index, active.id === 'sleeping' ? [0, 0] : eyeGaze)) {
        drawn = key; canvas.dataset.frame = index;
      }
      canvas.dataset.gazeX = eyeGaze[0].toFixed(4); canvas.dataset.gazeY = eyeGaze[1].toFixed(4); canvas.dataset.tracking = String(gaze.tracking);
      controls.update(gaze, active.id, !!active.eyes?.[index]?.length, config.settings.eyeTracking !== false);
      if (renderer.error) { toast(renderer.error.message); renderer.error = null; }
      const rect = $('#native-viewport').getBoundingClientRect();
      const subtitles = $('#robot-subtitles');
      const bottom = subtitles.hidden ? rect.bottom : Math.min(rect.bottom, subtitles.getBoundingClientRect().top - 12);
      const layout = fittedLayout(active.bounds, rect.width, Math.max(1, bottom - rect.top));
      Object.assign(canvas.style, { width: `${layout.size}px`, height: `${layout.size}px`, left: `${layout.left}px`, top: `${layout.top}px` });
    } else if (document.hidden) { started += Math.max(0, now - lastFrame); lastFrame = now; }
    requestAnimationFrame(frame);
  }
  const fullscreen = () => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen()).catch(error => toast(error.message));
  $('#robot-fullscreen').addEventListener('click', fullscreen); canvas.addEventListener('dblclick', fullscreen);
  document.addEventListener('keydown', event => { if (event.key.toLowerCase() === 'f' && !event.target.closest('input,dialog')) fullscreen(); if (speech.context?.state !== 'running') resumeVoice(); });
  document.addEventListener('click', () => { if (speech.context?.state !== 'running') resumeVoice(); });
  document.addEventListener('fullscreenchange', () => { $('#robot-fullscreen').textContent = document.fullscreenElement ? 'Exit fullscreen' : 'Enter fullscreen'; });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { lastFrame = performance.now(); idle.reset(); if (speech.context?.state !== 'running') resumeVoice(); } });
  addEventListener('pagehide', () => { events.close(); stopSpeech(); speech.dispose(); renderer.dispose(); controls.dispose(); }, { once: true });
  status(); resumeVoice(); requestAnimationFrame(frame);
  const voice = await api('voice'); $('#device-key-open').disabled = voice.source === 'environment';
  if (!voice.configured) openKeySetup();
}
