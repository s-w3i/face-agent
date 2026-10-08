import './style.css';
import { attachVoiceMotion, voiceMotionPose } from './native-voice.js';
import { REST_POSE } from './motion.js';
import { ORIGINAL_ACTIONS, VOICE_ACTIONS, signatureActions, IdleMovements } from './native-actions.js';
import { SpeechPlayer } from './speech.js';
import { NativeFraming } from './native-framing.js';
import { matchingBake } from './prerendered.js';

const $ = selector => document.querySelector(selector);
const robotView = document.body.dataset.view === 'robot';
document.body.classList.toggle('robot-display', robotView);
const actionButton = action => `<button class="motion-button" data-action="${action.id}" aria-pressed="false" disabled>${action.label}${action.kind === 'voice' ? '<small>Added</small>' : ''}</button>`;
const actionGroup = (label, actions) => `<h3 class="motion-group-title">${label}</h3><div class="motion-grid native-motions">${actions.map(actionButton).join('')}</div>`;
document.querySelector('#app').innerHTML = `
<header class="topbar"><a class="brand" href="./"><span class="brand-mark"><i></i><i></i></span>mimo<span class="brand-divider"></span><span class="brand-subtitle">original dots</span></a><div class="topbar-right"><a class="text-button" href="./robot.html" target="_blank" rel="noopener">Robot display ↗</a><button class="text-button" id="robot-save" disabled>Save configuration</button><button class="text-button" id="native-export" disabled>Export look</button></div></header>
<main><section class="intro"><div><div class="eyebrow">ORIGINAL RENDERER · LOCAL RESOURCES</div><h1>The original.<br><em>In motion.</em></h1><p>The installed dots renderer, using its actual characters and animations.</p></div></section>
<div class="studio-layout"><section class="preview-panel" aria-label="Original dots preview"><div class="stage" id="stage"><div class="stage-top"><div class="live-label"><span></span> ORIGINAL DOTS</div></div><div id="native-viewport"></div><div class="loading" id="native-loading">Loading the original renderer…</div><div class="stage-name"><span class="name-pill" id="native-name">Felipe</span><span class="stage-caption" id="native-caption">Ready</span></div><div class="stage-bottom"><span>Move your cursor to look · All rendering stays local</span></div></div>
<div class="playback"><div class="now-playing"><div><span class="small-label">ANIMATION</span><strong id="native-current">Idle / Ready</strong></div></div><button class="play-all" id="native-tour" disabled>Play all</button></div>
<div class="idle-controls"><label class="toggle-row"><span><strong>Random idle movements</strong><small>Native gaze, gestures, wave and your signature</small></span><input id="native-auto" type="checkbox" role="switch" checked disabled><span class="switch"></span></label><div class="idle-timing"><label for="native-interval">Every <output id="native-interval-label">9–15 s</output></label><input id="native-interval" type="range" min="5" max="60" step="1" value="12" disabled><button class="outline-button" id="native-next" disabled>Try random now</button></div><p id="native-idle-status" role="status">Random movements start while Idle / Ready.</p></div>
<div class="animation-library"><div class="section-heading"><div><h2>Animation library</h2><p>Every action exposed by this original renderer.</p></div></div><p id="native-description" class="motion-description" aria-live="polite">Ready follows your cursor. Sleeping uses the renderer’s Paused activity.</p>
${actionGroup('Original activities', ORIGINAL_ACTIONS.filter(a => a.kind === 'activity'))}
${actionGroup('Original reactions & interactions', ORIGINAL_ACTIONS.filter(a => ['reaction', 'gaze', 'gesture'].includes(a.kind)))}
${actionGroup('Original completions', ORIGINAL_ACTIONS.filter(a => a.kind === 'outcome'))}
<h3 class="motion-group-title">All eight original signatures</h3><p class="settings-note">These previews show each original outfit briefly, then restore your look.</p><div class="motion-grid native-motions" id="native-signatures"></div>
${actionGroup('Added voice motions', VOICE_ACTIONS)}
<div class="voice-controls"><div class="voice-control-heading"><strong>Listening & speaking controls</strong><button class="text-button" id="voice-pause" disabled>Pause body motion</button></div><label class="field-label" for="voice-time">Body motion time <output id="voice-time-label">0.00 s</output></label><input id="voice-time" type="range" min="0" max="5" step="0.01" value="0" disabled>
<label class="field-label" for="voice-strength">Motion strength <output id="voice-strength-label">100%</output></label><input id="voice-strength" type="range" min="0" max="1.5" step="0.05" value="1" disabled>
<label class="field-label" for="voice-source">Speech drive</label><select class="full-select" id="voice-source" disabled><option value="demo">Demo phrase</option><option value="level">Fixed speech level</option></select><label class="field-label" for="voice-level">Speech level <output id="voice-level-label">50%</output></label><input id="voice-level" type="range" min="0" max="1" step="0.01" value="0.5" disabled></div>
<p class="settings-note">Listening and speaking are added body animations on the original model. Use Voice settings for real speech; its audio drives the body motion. The controls above test visual motion without sound.</p></div></section>
<aside class="custom-panel" aria-label="Original character customization"><div class="custom-header"><div><span class="eyebrow">ACTUAL DOTS ASSETS</span><h2>Make it your own</h2></div></div><div class="panel-body">
<label class="field-label" for="native-preset">Original characters</label><select id="native-preset" class="full-select" disabled></select>
<label class="field-label" for="robot-name">Character name</label><input id="robot-name" class="full-input" maxlength="24" value="Felipe" autocomplete="off" disabled>
<section class="speech-settings" aria-label="Voice settings"><h3>Voice</h3><p class="settings-note">AI-generated voice · OpenAI</p><p id="tts-model" class="settings-note"></p><label class="field-label" for="tts-voice">Speaking voice</label><select class="full-select" id="tts-voice" disabled></select><p id="tts-sample-text" class="speech-sample">“Hi, I am Felipe.”</p><div class="speech-buttons"><button id="tts-sample" class="outline-button" disabled>Play sample</button><button id="tts-stop" class="text-button" disabled>Stop</button></div><p id="tts-status" class="settings-note" role="status">Checking voice setup…</p><p id="tts-latency" class="settings-note" role="status"></p><button id="tts-key-open" class="text-button" disabled>Set up API key</button></section>
<div id="native-catalog"></div>
<label class="field-label" for="native-depth">Body depth <output id="native-depth-label">0.5</output></label><input type="range" id="native-depth" min="0" max="1" step="0.01" value="0.5" disabled>
<label class="field-label spaced" for="native-quality">Rendering</label><select class="full-select" id="native-quality" disabled><option value="1">Compact · 30 fps target</option><option value="2">Balanced · 60 fps target</option></select>
<label class="toggle-row"><span><strong>Reduced motion</strong><small>Use the native quiet-motion setting</small></span><input id="native-reduced" type="checkbox" role="switch" disabled><span class="switch"></span></label>
<label class="field-label" for="robot-background">Robot display background</label><select class="full-select" id="robot-background" disabled><option value="light">Light</option><option value="dark">Dark</option></select>
<section class="bake-settings" aria-label="Pre-rendered playback"><h3>Raspberry Pi playback</h3><label class="field-label" for="robot-playback">Robot renderer</label><select class="full-select" id="robot-playback" disabled><option value="live">Live 3D · editable</option><option value="prerendered">Pre-rendered · lightweight</option></select><p class="settings-note">Bake this look on your computer: transparent 512 px frames, all original actions, listening, and 21 speaking poses driven by live audio. Keep this page visible while baking.</p><div class="speech-buttons"><button id="robot-bake" class="outline-button" disabled>Bake saved character</button><button id="robot-bake-cancel" class="text-button" hidden>Cancel</button></div><button id="robot-gaze-prepare" class="outline-button" disabled>Prepare eye tracking</button><p id="robot-bake-status" class="settings-note" role="status">Checking saved animation pack…</p><a id="robot-bake-preview" class="text-button" href="./robot.html?renderer=prerendered" target="_blank" rel="noopener" hidden>Test pre-rendered display ↗</a><label class="toggle-row"><span><strong>Human eye tracking</strong><small>Active while awake when a person target is received</small></span><input id="robot-eye-tracking" type="checkbox" role="switch" checked disabled><span class="switch"></span></label><label class="field-label" for="robot-gaze-response">Gaze response <output id="robot-gaze-response-label">140 ms</output></label><input id="robot-gaze-response" type="range" min="60" max="400" step="10" value="140" disabled><p class="settings-note">No person: original eyes. Sleeping keeps the closed-eye animation. Use Test gaze in the robot display before connecting a camera. Prepare eye tracking adds eye positions to existing body frames.</p><p class="settings-note">Saved in <code>public/prerendered/</code>. Copy this folder and <code>robot-config.json</code> to your Pi. Re-bake after changing the look, motion strength, reduced motion or background.</p></section>
<div class="robot-file-note"><strong>One local configuration</strong><code id="robot-config-path">robot-config.json</code><small>Appearance and settings save automatically. The robot display follows this file.</small></div>
<details class="robot-terminal"><summary>Animation terminal</summary><p>Open the robot display, then enter a state. Try <code>sleeping</code>, <code>speaking 0.8</code>, <code>idle</code>, <code>wave</code>, or <code>say Hello!</code> for speech.</p><form id="robot-command"><label class="field-label" for="robot-command-input">Animation state or speech</label><div><input id="robot-command-input" autocomplete="off" spellcheck="false" list="robot-states" placeholder="say Hi, I am your robot" disabled><button type="submit" disabled>Send</button></div></form><datalist id="robot-states"></datalist><pre id="robot-command-log" aria-live="polite">dots&gt; ready</pre><p>OS terminal: <code>python3 scripts/dotsctl.py</code></p></details>
<div class="render-stats"><div><span>Frame rate</span><strong id="native-fps">—</strong></div><div><span>Triangles</span><strong id="native-triangles">—</strong></div><div><span>Renderer version</span><strong id="native-version">—</strong></div></div>
<button class="outline-button full" id="native-import" disabled>Import original look</button><input type="file" id="native-file" accept=".json,application/json" hidden><p class="settings-note">Appearance saves separately from your procedural character.</p></div><div class="panel-footer"><span id="native-save">Loading local resources</span></div></aside></div>
<footer class="page-footer"><span>Original geometry, materials, fur, and motion.</span><a href="./">Back to procedural studio →</a></footer></main><div id="toast" class="toast" role="status"></div>
<dialog id="tts-key-dialog" class="key-dialog" aria-labelledby="tts-key-title"><form id="tts-key-form"><h2 id="tts-key-title">Add your OpenAI API key</h2><p>Give your dot a voice. The key stays in a private file on this computer, outside the web files and character configuration.</p><label class="field-label" for="tts-key-input">OpenAI API key</label><input id="tts-key-input" class="full-input" type="password" autocomplete="new-password" placeholder="sk-…" required><p><a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener noreferrer">Create an API key ↗</a> · Speech uses your OpenAI API billing.</p><p id="tts-key-error" role="alert"></p><div class="speech-buttons"><button id="tts-key-submit" class="outline-button" type="submit">Save API key</button><button id="tts-key-later" class="text-button" type="button">Later</button></div></form></dialog>`;
if (robotView) {
  document.querySelector('#app').insertAdjacentHTML('beforeend', '<p id="robot-subtitles" class="robot-subtitles" dir="auto" aria-label="Speech subtitles" hidden></p>');
  document.querySelector('#app').insertAdjacentHTML('beforeend', '<div class="robot-hud"><span>AI-generated voice · OpenAI</span><a href="./original-dots.html">Studio</a><button id="robot-fullscreen">Enter fullscreen</button><span id="robot-connection" role="status">Connecting…</span></div>');
  for (const selector of ['.topbar', '.intro', '.playback', '.idle-controls', '.animation-library', '.custom-panel', '.page-footer', '.stage-top', '.stage-name', '.stage-bottom']) $(selector).hidden = true;
}

let engine, character, voice, catalog, bundle, storageKey, appearance, mode, current = 7, name = 'Felipe', presetId = 'blue_beret';
let voiceState = '', voiceTime = 0, voicePaused = false, voicePose = { ...REST_POSE }, voiceNow = 0;
let sequence = 0n, episode = 0n, savePending = false, syncPending = false, signatureAvailable = true;
let quality = 1, lastFrame = 0, statsAt = 0, frames = 0, tourIndex = -1, nextTourAt = 0;
let actions = [...ORIGINAL_ACTIONS, ...VOICE_ACTIONS], tour = [], activeAction = null, previewLook = null, pointerInside = false;
const idleMovements = new IdleMovements();
const framing = robotView ? new NativeFraming() : null;
let configLoaded = false, fileConfig, saveQueued = null, saving = false, events, commandLevel = null, desiredState = 'idle', lastCommand = -1;
let saveAfter = 0;
let commandGeneration = '';
const speech = new SpeechPlayer(text => {
  const subtitles = $('#robot-subtitles');
  if (subtitles) { subtitles.textContent = text; subtitles.hidden = !text; }
});
let speechRequest = 0, pendingSpeech = null, keyConfigured = false, speechFetch = null;
let voiceInfo = null;
let voiceMigrationNote = '';
let toastTimer;
let bakedManifest = null, bakeController = null;
const base = new URL('./local-dots/', location.href);
const encode = bytes => btoa(String.fromCharCode(...bytes));
const decode = value => Uint8Array.from(atob(value), c => c.charCodeAt(0));
function toast(message) { $('#toast').textContent = message; $('#toast').classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').classList.remove('show'), 4000); }
function checkError() { const error = character?.error(); if (error) toast(error); }
async function api(path, value, method = 'POST') {
  const response = await fetch(`/api/${path}`, value === undefined ? { cache: 'no-store' } : {
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value), keepalive: path === 'config',
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The local dots service is unavailable.');
  return result;
}
function settingsSnapshot() {
  return { quality, reducedMotion: $('#native-reduced').checked, autoIdle: $('#native-auto').checked,
    idleInterval: Number($('#native-interval').value), motionStrength: Number($('#voice-strength').value),
    speechSource: $('#voice-source').value, speechLevel: Number($('#voice-level').value), background: $('#robot-background').value, ttsVoice: $('#tts-voice').value || 'marin', playback: $('#robot-playback').value,
    eyeTracking: $('#robot-eye-tracking').checked, gazeResponse: Number($('#robot-gaze-response').value) };
}
function configSnapshot() {
  return { version: 1, renderer: 'original-dots', resourceRevision: bundle.resourceRevision,
    appearance: { name, presetId, signatureAvailable, state: encode(previewLook || character.state()) }, settings: settingsSnapshot() };
}
function applySettings(settings) {
  quality = settings.quality; $('#native-quality').value = quality;
  $('#native-reduced').checked = settings.reducedMotion; $('#native-auto').checked = settings.autoIdle;
  $('#native-interval').value = settings.idleInterval; idleMovements.reset(settings.idleInterval);
  $('#native-interval-label').textContent = `${Math.round(settings.idleInterval * 0.75)}–${Math.round(settings.idleInterval * 1.25)} s`;
  $('#voice-strength').value = settings.motionStrength; $('#voice-strength-label').textContent = `${Math.round(settings.motionStrength * 100)}%`;
  $('#voice-source').value = settings.speechSource; $('#voice-level').value = settings.speechLevel;
  $('#voice-level-label').textContent = `${Math.round(settings.speechLevel * 100)}%`;
  $('#robot-background').value = settings.background;
  $('#robot-playback').value = settings.playback || 'live';
  $('#robot-eye-tracking').checked = settings.eyeTracking !== false; $('#robot-gaze-response').value = settings.gazeResponse || 140;
  $('#robot-gaze-response-label').textContent = `${settings.gazeResponse || 140} ms`;
  const selectedVoice = settings.ttsVoice || 'marin';
  const supportedVoice = voiceInfo.voices.includes(selectedVoice);
  $('#tts-voice').value = supportedVoice ? selectedVoice : 'marin';
  voiceMigrationNote = supportedVoice ? '' : `${selectedVoice[0].toUpperCase() + selectedVoice.slice(1)} is unavailable in the new model. Marin is selected; you can choose another voice.`;
  document.body.dataset.robotBackground = settings.background;
  if (character) { character.setQuality(quality); character.setReducedMotion(settings.reducedMotion); character.setActivityTheme(settings.background === 'dark' && robotView ? 1 : 0); resize(); }
}
function readConfigLook(value) {
  if (value.version !== 1 || value.renderer !== 'original-dots' || value.resourceRevision !== bundle.resourceRevision) throw new Error('The saved configuration needs its matching original renderer bundle.');
  const state = decode(value.appearance.state), error = engine.validateAppearance(state);
  if (error) throw new Error(error);
  return state;
}
async function persistConfig(value) {
  if (robotView) return;
  saveQueued = value;
  if (saving) return;
  saving = true; $('#robot-save').disabled = true;
  try {
    while (saveQueued) {
      const next = saveQueued; saveQueued = null;
      const result = await api('config', next, 'PUT');
      fileConfig = next; syncBakeStatus();
      $('#robot-config-path').textContent = result.path;
      $('#native-save').textContent = 'Configuration saved to robot-config.json';
    }
  } catch (error) { $('#native-save').textContent = `Save failed: ${error.message}`; toast(error.message); }
  finally { saving = false; $('#robot-save').disabled = false; }
}
function connectRobot() {
  events = new EventSource('/api/events');
  events.onopen = () => { $('#robot-connection').textContent = 'Connected'; api('actions', actions.map(({ id, label }) => ({ id, label }))).catch(error => toast(error.message)); };
  events.onerror = () => { $('#robot-connection').textContent = 'Reconnecting…'; };
  events.addEventListener('config-error', event => toast(JSON.parse(event.data).error));
  events.addEventListener('config', event => {
    try {
      const value = JSON.parse(event.data), state = readConfigLook(value);
      if (value.settings.playback === 'prerendered') { location.reload(); return; }
      if (fileConfig && JSON.stringify(value) === JSON.stringify(fileConfig)) return;
      const level = commandLevel; playAction('idle', false, true);
      const error = character.restore(state); if (error) throw new Error(error);
      ({ name, presetId, signatureAvailable } = value.appearance); appearance = state;
      syncName(); applySettings(value.settings); fileConfig = value; syncPending = true;
      playAction(desiredState, false, true); commandLevel = level;
    } catch (error) { toast(error.message); }
  });
  events.addEventListener('command', async event => {
    const command = JSON.parse(event.data);
    if (command.generation === commandGeneration && command.sequence <= lastCommand) return;
    commandGeneration = command.generation; lastCommand = command.sequence; commandLevel = null;
    const definition = actions.find(a => a.id === command.state);
    let error = '';
    if (!definition) error = 'This state is unavailable in the installed renderer.';
    else if (command.state === 'signature' && !signatureAvailable) error = 'Your customized outfit has no original signature. Use a named signature preview or wave.';
    else {
      desiredState = command.state;
      playAction(command.speech ? 'idle' : command.state, false); commandLevel = command.level;
      if (command.speech) {
        pendingSpeech = command;
        resumeRobotVoice();
        if (speech.context?.state !== 'running') toast('This browser blocks automatic audio. Use ./run.sh --robot, or tap anywhere here.');
        return;
      }
    }
    if (error) toast(error);
    api('ack', { generation: command.generation, sequence: command.sequence, state: command.state, error }).catch(error => toast(error.message));
  });
}
function resize() {
  if (!character) return;
  framing?.reset();
  const rect = $('#native-viewport').getBoundingClientRect();
  const size = Math.max(1, Math.round(Math.min(quality === 1 ? 512 : 1024, Math.min(rect.width, rect.height) * (quality === 1 ? 1 : Math.min(devicePixelRatio, 1.5)))));
  character.resize(size, size); character.setDisplayScale(size / Math.min(rect.width, rect.height));
}
function rebuild(activityMode, state = character?.state() || appearance) {
  if (!previewLook) appearance = state;
  if (character) { character.delete(); character = undefined; voice.dispose(); }
  const canvas = document.createElement('canvas'); canvas.id = 'original-character'; canvas.setAttribute('role', 'img'); canvas.setAttribute('aria-label', 'Original dots character');
  $('#native-viewport').replaceChildren(canvas);
  voice = attachVoiceMotion(canvas);
  character = new engine.Character('#original-character', 512, 512, { activities: activityMode });
  character.setQuality(quality); character.setReducedMotion($('#native-reduced').checked);
  character.setActivityTheme(robotView && $('#robot-background').value === 'dark' ? 1 : 0);
  const error = character.restore(state); if (error) throw new Error(error);
  mode = activityMode; resize();
  character.onFirstFrame(() => { $('#native-loading').hidden = true; syncVoiceControls(); });
  voice.update(voicePose);
}
function syncAppearance() {
  for (let category = 0; category < 5; category++) {
    const select = $(`#native-category-${category}`);
    const chosen = catalog[category].find(item => character.isSelected(category, item.id));
    select.value = chosen?.id || '';
    for (const option of select.options) option.disabled = !!option.value && option.value !== chosen?.id && !character.isAvailable(category, option.value);
  }
  $('#native-depth').value = character.depth(); $('#native-depth-label').textContent = character.depth().toFixed(2);
  $('[data-action="signature"]').disabled = !signatureAvailable;
}
function requestSave() { if (!robotView && configLoaded) { savePending = true; saveAfter = performance.now() + 250; $('#native-save').textContent = 'Saving configuration…'; } }
function syncBakeStatus() {
  if (robotView || bakeController) return;
  $('#robot-bake-preview').hidden = !bakedManifest;
  $('#robot-playback option[value="prerendered"]').disabled = !bakedManifest;
  $('#robot-gaze-prepare').disabled = !bakedManifest;
  $('#robot-bake-status').textContent = !bakedManifest ? 'No baked library yet. Bake your saved character here.' :
    matchingBake(configSnapshot(), bakedManifest) ? `Saved library · ${bakedManifest.clips.length} animations · 512 px / 16 fps${bakedManifest.gazeVersion ? ' · eye tracking ready' : ' · prepare eye tracking to add gaze'}` : 'Saved library needs a new bake to match these settings.';
}
async function prepareEyeTracking() {
  if (bakeController || !bakedManifest) return;
  const controller = bakeController = new AbortController();
  const controls = [...document.querySelectorAll('button, select, input')].filter(control => control.id !== 'robot-bake-cancel');
  const disabled = controls.map(control => control.disabled); controls.forEach(control => { control.disabled = true; });
  $('#robot-bake-cancel').hidden = false; $('#robot-bake-cancel').disabled = false; character.setActive(false);
  try {
    const { prepareGaze } = await import('./bake-dots.js');
    bakedManifest = await prepareGaze(await api('prerender'), controller.signal, message => { $('#robot-bake-status').textContent = `Preparing eyes · ${message}`; });
    toast('Eye tracking prepared. Body frames and original eyes are preserved.');
  } catch (error) { toast(error.name === 'AbortError' ? 'Eye preparation cancelled.' : error.message); }
  finally {
    bakeController = null; controls.forEach((control, i) => { control.disabled = disabled[i]; });
    $('#robot-bake-cancel').hidden = true; character?.setActive(true); syncBakeStatus();
  }
}
async function bakeSavedCharacter() {
  if (bakeController) return;
  playAction('idle'); stopTour();
  // Finish native appearance updates before taking a snapshot.
  while (character.hasPendingUpdate()) await new Promise(resolve => requestAnimationFrame(resolve));
  const config = configSnapshot(); savePending = false;
  await persistConfig(config);
  const saved = await api('config');
  if (JSON.stringify(saved) !== JSON.stringify(config)) { toast('Save the current configuration before baking.'); return; }
  const controller = bakeController = new AbortController();
  const controls = [...document.querySelectorAll('button, select, input')].filter(control => control.id !== 'robot-bake-cancel');
  const disabled = controls.map(control => control.disabled); controls.forEach(control => { control.disabled = true; });
  $('#robot-bake-cancel').hidden = false; $('#robot-bake-cancel').disabled = false;
  character.setActive(false);
  try {
    const { bakeDots } = await import('./bake-dots.js');
    bakedManifest = await bakeDots(engine, config, actions, controller.signal, message => { $('#robot-bake-status').textContent = `Baking · ${message}`; });
    toast('Saved pre-rendered library. Open Test pre-rendered display, or select the lightweight robot renderer.');
  } catch (error) { toast(error.name === 'AbortError' ? 'Bake cancelled. Your previous library is preserved.' : error.message); }
  finally {
    bakeController = null; controls.forEach((control, i) => { control.disabled = disabled[i]; });
    $('#robot-bake-cancel').hidden = true; character?.setActive(true); syncBakeStatus();
  }
}
function syncName() {
  $('#native-name').textContent = name; $('#robot-name').value = name;
  $('#tts-sample-text').textContent = `“Hi, I am ${name}.”`;
}
function voiceReady() {
  $('#tts-status').textContent = voiceMigrationNote || (keyConfigured ? (voiceInfo?.source === 'environment' ? 'Ready · API key supplied by the server environment.' : 'Ready · API key saved privately on this computer.') : 'Add an OpenAI API key to enable speech.');
  $('#tts-key-open').textContent = keyConfigured ? 'Change API key' : 'Set up API key';
  $('#tts-key-open').disabled = voiceInfo?.source === 'environment';
}
function stopSpeech() {
  speechRequest++; pendingSpeech = null; speechFetch?.abort(); speechFetch = null; speech.stop();
  $('#tts-sample').disabled = false; $('#tts-sample').textContent = 'Play sample'; $('#tts-stop').disabled = true;
  if (voiceInfo) voiceReady();
}
async function playSpeech(response, text) {
  const onStart = () => {
      pendingSpeech = null; desiredState = 'speaking'; playAction('speaking', true, true);
      $('#tts-status').textContent = 'Streaming · body motion follows the voice.';
      $('#tts-sample').disabled = true; $('#tts-sample').textContent = 'Playing…'; $('#tts-stop').disabled = false;
  };
  const onEnd = () => {
      desiredState = 'idle'; playAction('idle', false);
  };
  if (response.headers.get('Content-Type')?.includes('application/x-ndjson')) {
    await speech.stream(response, onStart, onEnd, packet => {
      $('#tts-latency').textContent = `First audio · ${(packet.firstAudioMs / 1000).toFixed(2)} s · ${packet.warm ? 'warm connection' : 'new connection'}`;
    }, text);
  } else await speech.play(await response.blob(), onStart, onEnd, text);
}
function openKeySetup() {
  $('#tts-key-error').textContent = ''; $('#tts-key-input').value = '';
  $('#tts-key-dialog').showModal();
}
async function resumeRobotVoice() {
  let command, request, error = '';
  try {
    await speech.enable();
    if (speech.context?.state !== 'running' || !pendingSpeech) return;
    command = pendingSpeech; request = speechRequest; pendingSpeech = null;
    speechFetch = new AbortController();
    const response = await fetch(command.speech, { cache: 'no-store', signal: speechFetch.signal });
    if (request !== speechRequest) return;
    if (!response.ok) throw new Error((await response.json()).error || 'Unable to load speech.');
    await playSpeech(response, command.text || '');
  } catch (failure) {
    if (failure.name === 'AbortError' || (request !== undefined && request !== speechRequest)) return;
    error = failure.message; toast(error);
  }
  if (command) api('ack', { generation: command.generation, sequence: command.sequence, state: command.state, error }).catch(error => toast(error.message));
}
async function setupVoice() {
  voiceInfo = await api('voice'); keyConfigured = voiceInfo.configured;
  $('#tts-model').textContent = `Model · ${voiceInfo.model}`;
  for (const voice of voiceInfo.voices) $('#tts-voice').add(new Option(voice[0].toUpperCase() + voice.slice(1), voice));
  voiceReady();
  $('#tts-key-open').addEventListener('click', openKeySetup);
  $('#tts-key-later').addEventListener('click', () => $('#tts-key-dialog').close());
  $('#tts-key-dialog').addEventListener('close', () => { $('#tts-key-input').value = ''; });
  $('#tts-key-form').addEventListener('submit', async event => {
    event.preventDefault(); $('#tts-key-submit').disabled = true; $('#tts-key-error').textContent = '';
    const apiKey = $('#tts-key-input').value.trim(); $('#tts-key-input').value = '';
    try {
      await api('key', { apiKey }); voiceInfo = await api('voice'); keyConfigured = voiceInfo.configured;
      voiceReady(); $('#tts-key-dialog').close(); toast('API key saved privately. Choose a voice and play a sample.');
    } catch (error) { $('#tts-key-error').textContent = error.message; }
    finally { $('#tts-key-submit').disabled = false; }
  });
  $('#tts-sample').addEventListener('click', async () => {
    if (!keyConfigured) { openKeySetup(); return; }
    stopSpeech(); const request = speechRequest, text = `Hi, I am ${name}.`;
    $('#tts-sample').disabled = true; $('#tts-sample').textContent = 'Generating…'; $('#tts-stop').disabled = false;
    $('#tts-status').textContent = 'Generating your greeting…';
    try {
      await speech.enable();
      if (request !== speechRequest) return;
      speechFetch = new AbortController();
      const response = await fetch('/api/speech-stream', { method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: speechFetch.signal,
        body: JSON.stringify({ text, voice: $('#tts-voice').value }) });
      if (!response.ok) throw new Error((await response.json()).error || 'Unable to generate speech.');
      if (request !== speechRequest) return;
      await playSpeech(response, text);
      $('#tts-sample').textContent = 'Play sample';
    } catch (error) {
      if (request !== speechRequest) return;
      stopSpeech(); $('#tts-status').textContent = error.message; toast(error.message);
    }
  });
  $('#tts-stop').addEventListener('click', () => { desiredState = 'idle'; playAction('idle'); });
  $('#tts-voice').addEventListener('change', () => { voiceMigrationNote = ''; if (!speech.source) voiceReady(); requestSave(); });
  $('#robot-name').addEventListener('input', event => {
    name = event.target.value.slice(0, 24); $('#native-name').textContent = name;
    $('#tts-sample-text').textContent = `“Hi, I am ${name}.”`; requestSave();
  });
  if (robotView) {
    const retryVoice = () => { if (speech.context?.state !== 'running') resumeRobotVoice(); };
    document.addEventListener('click', retryVoice);
    document.addEventListener('keydown', retryVoice);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) retryVoice(); });
    // A blocked resume can wait for a gesture; let the character load meanwhile.
    resumeRobotVoice();
  }
}
function syncVoiceControls() {
  const enabled = !!voiceState;
  for (const id of ['voice-time', 'voice-pause', 'voice-strength']) $(`#${id}`).disabled = !enabled;
  $('#voice-source').disabled = voiceState !== 'speaking';
  $('#voice-level').disabled = voiceState !== 'speaking' || $('#voice-source').value !== 'level';
  $('#voice-pause').textContent = voicePaused ? 'Resume body motion' : 'Pause body motion';
  for (const action of VOICE_ACTIONS) $(`[data-action="${action.id}"]`).disabled = !voice?.supported();
}
function stopVoice() { voiceState = ''; voicePaused = false; $('#voice-time').value = 0; syncVoiceControls(); }
function previewControls(locked) {
  for (const id of ['native-preset', 'robot-name', 'native-depth', 'native-import', 'native-export', 'robot-save', ...Array.from({ length: 5 }, (_, i) => `native-category-${i}`)]) $(`#${id}`).disabled = locked;
}
function interruptAction() {
  activeAction = null; character?.onFirstFrame(null);
  if (mode) character?.clearReadyGaze(performance.now() / 1000);
  stopVoice(); idleMovements.reset();
  if (previewLook) {
    appearance = previewLook; previewLook = null;
    const error = character.restore(appearance); if (error) throw new Error(error);
    $('#native-name').textContent = name; previewControls(false); syncAppearance();
  }
}
function startActivity(activity) {
  if (!mode) rebuild(true);
  const result = character.applyActivity({ command: 1, sequence: ++sequence, episodeId: ++episode,
    episodeHighWater: 0n, activity, outcome: 0, entry: 0 });
  if (result !== 0) throw new Error(`The native renderer could not start this activity (${result}).`);
  current = activity;
}
function playAction(id, manual = true, keepSpeech = false) {
  const definition = actions.find(action => action.id === id); if (!definition) return;
  framing?.reset();
  if (!keepSpeech) stopSpeech();
  if (manual) stopTour();
  commandLevel = null;
  interruptAction();
  try {
    if (['reaction', 'gesture', 'hero'].includes(definition.kind)) {
      current = 7;
      if (definition.hero) {
        previewLook = character.state(); previewControls(true);
        $('#native-name').textContent = `${definition.title} · preview`;
      }
      rebuild(false, definition.hero ? engine.presetAppearance(definition.hero) : character.state());
    } else startActivity(definition.activity || 7);
    if (definition.kind === 'voice') { voiceState = id; voiceTime = 0; voicePaused = false; }
    if (definition.duration) {
      const action = activeAction = { definition, elapsed: 0, started: false, cue: 0, released: false, stopped: false };
      character.onFirstFrame(() => {
        if (activeAction !== action) return;
        $('#native-loading').hidden = true; syncVoiceControls();
        if (definition.reaction) {
          const result = character.playReaction(definition.reaction);
          if (result !== 0) { toast(`This original reaction is unavailable (${result}).`); playAction('idle', false); return; }
        }
        action.started = true;
      });
    } else character.onFirstFrame(() => { $('#native-loading').hidden = true; syncVoiceControls(); if (tourIndex >= 0) nextTourAt = performance.now() + 6000; });
    $('#native-current').textContent = definition.label; $('#native-caption').textContent = definition.label;
    $('#native-description').textContent = definition.description;
    document.querySelectorAll('[data-action]').forEach(button => button.setAttribute('aria-pressed', button.dataset.action === id));
    syncVoiceControls(); syncIdleControls();
  } catch (error) { toast(error.message); }
}
function tickAction(dt, seconds) {
  if (!activeAction?.started) return;
  const action = activeAction, definition = action.definition;
  action.elapsed += dt;
  if (definition.kind === 'gaze') {
    if (action.elapsed < 2.5) character.setReadyGaze(...definition.target, seconds);
    else if (!action.released) { character.clearReadyGaze(seconds); action.released = true; }
  }
  if (definition.cues) {
    while (action.cue < definition.cues.length && action.elapsed >= definition.cues[action.cue][0]) {
      const [, phase, x, y] = definition.cues[action.cue++]; character.pointer(phase, 1, x, y, seconds);
    }
  }
  if (definition.kind === 'outcome' && action.elapsed >= 2.5 && !action.stopped) {
    const result = character.applyActivity({ command: 2, sequence: ++sequence, episodeId: episode,
      episodeHighWater: 0n, activity: definition.activity, outcome: definition.outcome, entry: 0 });
    if (result !== 0) toast(`The native completion could not play (${result}).`);
    action.stopped = true;
  }
  if (action.elapsed >= definition.duration) {
    if (tourIndex >= 0) { tourIndex++; stepTour(performance.now()); }
    else { desiredState = 'idle'; playAction('idle', false); }
  }
}
function idleEligible() { return current === 7 && mode && !voiceState && !activeAction && tourIndex < 0; }
function syncIdleControls() {
  $('#native-next').disabled = !idleEligible();
  let status;
  if (tourIndex >= 0) status = 'Random movements wait until Play all finishes.';
  else if (!idleEligible()) status = 'Random movements wait until Idle / Ready.';
  else if (!$('#native-auto').checked) status = 'Automatic movements are off. Try random now still works.';
  else if ($('#native-reduced').checked) status = 'Automatic movements pause with Reduced motion.';
  else if (pointerInside) status = 'Following your cursor. Random movements resume when it leaves.';
  else status = `Next random movement in about ${Math.ceil(idleMovements.remaining)} s.`;
  if ($('#native-idle-status').textContent !== status) $('#native-idle-status').textContent = status;
}
function stopTour() { tourIndex = -1; $('#native-tour').textContent = 'Play all'; }
function stepTour(now) {
  const step = tour[tourIndex];
  if (!step) { stopTour(); playAction('idle', false); return; }
  playAction(step.id, false);
  $('#native-tour').textContent = `Stop · ${tourIndex + 1}/${tour.length}`; nextTourAt = Infinity;
}
function frame(now) {
  if (bakeController) { requestAnimationFrame(frame); return; }
  if (character && !document.hidden && now - lastFrame >= 1000 / (quality === 1 ? 30 : 60)) {
    const interval = 1000 / (quality === 1 ? 30 : 60);
    lastFrame = now - ((now - lastFrame) % interval);
    if (tourIndex >= 0 && !activeAction && now >= nextTourAt) { tourIndex++; stepTour(now); }
    try {
      const dt = Math.min(0.05, Math.max(0, (now - voiceNow) / 1000)); voiceNow = now;
      tickAction(dt, now / 1000);
      const randomAction = idleMovements.tick(dt, { idle: idleEligible() && !pointerInside,
        enabled: $('#native-auto').checked, reduced: $('#native-reduced').checked, signatureAvailable });
      if (randomAction) playAction(randomAction, false);
      syncIdleControls();
      if (voiceState && !voicePaused && !$('#native-reduced').checked) voiceTime = (voiceTime + dt) % 5;
      const target = voiceMotionPose(voiceState, voiceTime, speech.level() ?? commandLevel ?? ($('#voice-source').value === 'level' ? Number($('#voice-level').value) : null),
        Number($('#voice-strength').value), $('#native-reduced').checked);
      const blend = voicePaused || $('#native-reduced').checked ? 1 : 1 - Math.exp(-12 * dt);
      for (const key of Object.keys(REST_POSE)) {
        voicePose[key] += (target[key] - voicePose[key]) * blend;
        if (!voiceState && Math.abs(voicePose[key] - target[key]) < 0.0001) voicePose[key] = target[key];
      }
      voice.update(voicePose);
      if (!voicePaused) $('#voice-time').value = voiceState ? voiceTime : 0;
      $('#voice-time-label').textContent = `${(voiceState ? voiceTime : 0).toFixed(2)} s`;
      const rendered = character.render(now / 1000);
      if (rendered) frames++;
      if (framing) {
        const rect = $('#native-viewport').getBoundingClientRect();
        let bottom = rect.bottom;
        const subtitles = $('#robot-subtitles');
        if (!subtitles.hidden) bottom = Math.min(bottom, subtitles.getBoundingClientRect().top - 12);
        const canvas = $('#original-character');
        const size = framing.update(canvas, rect.width, Math.max(1, bottom - rect.top), rendered);
        if (size) character.setDisplayScale(canvas.width / size);
      }
      if (((savePending && now >= saveAfter) || syncPending) && !previewLook && !character.hasPendingUpdate()) {
        appearance = character.state(); syncAppearance(); syncPending = false;
        if (savePending) { savePending = false; persistConfig(configSnapshot()); }
        checkError();
      }
      if (now - statsAt >= 1000) {
        const stats = character.stats(); $('#native-fps').textContent = `${Math.round(frames * 1000 / (now - statsAt))} fps`;
        $('#native-triangles').textContent = stats.triangles.toLocaleString(); statsAt = now; frames = 0;
      }
    } catch (error) { toast(error.message); }
  }
  requestAnimationFrame(frame);
}

async function start() {
  try {
    if (!crossOriginIsolated) throw new Error('Start this page with pnpm dev or ./run.sh, then reload. The original renderer needs an isolated browser context.');
    await setupVoice();
    const response = await fetch(new URL('bundle.json', base));
    if (!response.ok) throw new Error('Import the installed dots resources with pnpm import:dots, then rebuild and reopen this page.');
    bundle = await response.json(); storageKey = 'mimo-original-dots:' + bundle.resourceRevision;
    const runtime = await import(/* @vite-ignore */ new URL('orbit-characters.mjs', base).href);
    engine = await runtime.default({ locateFile: path => new URL(path, base).href });
    const configResponse = await fetch('/api/config', { cache: 'no-store' });
    if (configResponse.status === 404) fileConfig = null;
    else { fileConfig = await configResponse.json(); if (!configResponse.ok) throw new Error(fileConfig.error || 'Unable to read robot-config.json.'); }
    appearance = engine.presetAppearance('blue_beret');
    if (fileConfig?.appearance) {
      appearance = readConfigLook(fileConfig); ({ name, presetId, signatureAvailable } = fileConfig.appearance);
    } else if (robotView) throw new Error('Save your character from the studio before opening the robot display.');
    else try {
      const saved = JSON.parse(localStorage.getItem(storageKey));
      if (saved && !engine.validateAppearance(decode(saved.state))) { appearance = decode(saved.state); name = saved.name || name; presetId = saved.presetId || ''; signatureAvailable = saved.signatureAvailable !== false; }
    } catch { /* A malformed local save falls back to the original blue character. */ }
    applySettings(fileConfig?.settings || { quality: 1, reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches,
      autoIdle: true, idleInterval: 12, motionStrength: 1, speechSource: 'demo', speechLevel: 0.5, background: 'light' });
    const presets = engine.presets(), heroPresets = engine.hereCharacters(), heroes = new Set(heroPresets.map(p => p.id));
    const signatures = signatureActions(heroPresets);
    actions.push(...signatures); $('#native-signatures').innerHTML = signatures.map(actionButton).join('');
    // The eight outfit previews cover every signature, so the current-look shortcut is omitted.
    tour = actions.filter(action => action.id !== 'signature');
    for (const preset of presets) $('#native-preset').add(new Option(preset.title, preset.id));
    $('#native-preset').value = presetId;
    catalog = Array.from({ length: 5 }, (_, category) => engine.catalog(category));
    for (const [category, label] of ['Body shape', 'Body color', 'Eyes', 'Glasses', 'Accessory'].entries()) {
      const field = document.createElement('label'); field.className = 'field-label'; field.htmlFor = `native-category-${category}`; field.textContent = label;
      const select = document.createElement('select'); select.id = field.htmlFor; select.className = 'full-select';
      if (category === 4) select.add(new Option('None', ''));
      for (const item of catalog[category]) select.add(new Option(item.title, item.id));
      select.addEventListener('change', () => {
        playAction('idle');
        if (category === 4) for (const item of catalog[4]) if (character.isSelected(4, item.id)) character.select(4, item.id);
        if (select.value && !character.select(category, select.value)) toast('This combination is unavailable in the original renderer.');
        signatureAvailable = false; presetId = ''; $('#native-preset').selectedIndex = -1; syncName(); requestSave();
      });
      $('#native-catalog').append(field, select);
    }
    rebuild(true); playAction('idle'); syncAppearance();
    syncName(); $('#native-version').textContent = bundle.sdkVersion;
    $('#native-save').textContent = 'Configuration loaded from robot-config.json';
    document.querySelectorAll('button, select, input').forEach(control => control.disabled = false); syncAppearance(); syncVoiceControls(); syncIdleControls();
    $('#tts-stop').disabled = true; voiceReady();
    configLoaded = true;
    if (!robotView) { bakedManifest = await api('prerender'); syncBakeStatus(); }
    if (!robotView && (!fileConfig?.appearance || !voiceInfo.voices.includes(fileConfig?.settings?.ttsVoice || 'marin'))) requestSave();
    const status = await api('status'); $('#robot-config-path').textContent = status.configFile;
    await api('actions', actions.map(({ id, label }) => ({ id, label })));
    for (const action of actions) $('#robot-states').append(new Option(action.label, action.id));
    if (robotView) connectRobot();
    if (!keyConfigured) openKeySetup();
    $('#robot-save').addEventListener('click', () => { requestSave(); saveAfter = 0; });
    $('#robot-bake').addEventListener('click', () => bakeSavedCharacter().catch(error => toast(error.message)));
    $('#robot-gaze-prepare').addEventListener('click', () => prepareEyeTracking().catch(error => toast(error.message)));
    $('#robot-bake-cancel').addEventListener('click', () => bakeController?.abort());
    for (const id of ['native-quality', 'native-reduced', 'native-auto', 'native-interval', 'voice-strength', 'voice-source', 'voice-level', 'robot-background', 'robot-playback', 'robot-eye-tracking', 'robot-gaze-response']) {
      $(`#${id}`).addEventListener(['native-interval', 'voice-strength', 'voice-level', 'robot-gaze-response'].includes(id) ? 'input' : 'change', requestSave);
    }
    $('#robot-gaze-response').addEventListener('input', event => { $('#robot-gaze-response-label').textContent = `${event.target.value} ms`; });
    $('#robot-background').addEventListener('change', () => { document.body.dataset.robotBackground = $('#robot-background').value; });
    $('#robot-command').addEventListener('submit', async event => {
      event.preventDefault();
      const text = $('#robot-command-input').value.trim(); if (!text) return;
      const words = text.split(/\s+/), state = words[0].toLowerCase();
      const payload = ['speaking', 'speak'].includes(state) && words.length === 2 ? { state, level: Number(words[1]) } : { state: text };
      try {
        if ('level' in payload && !Number.isFinite(payload.level)) throw new Error('Use speaking with a speech level between 0 and 1.');
        const result = state === 'say' ? await api('say', { stream: true, ...(words.length > 1 ? { text: words.slice(1).join(' ') } : {}) }) : await api('command', payload);
        $('#robot-command-log').textContent = `dots> ${text}\n→ ${result.state}${result.displays ? ' · sent to robot display' : ' · open the robot display to play'}`;
        $('#robot-command-input').value = '';
      } catch (error) { $('#robot-command-log').textContent = `dots> ${text}\n${error.message}`; }
    });
    $('#native-preset').addEventListener('change', event => {
      playAction('idle');
      presetId = event.target.value; appearance = engine.presetAppearance(presetId);
      const error = character.restore(appearance); if (error) { toast(error); return; }
      name = presets.find(p => p.id === event.target.value).title; signatureAvailable = heroes.has(event.target.value);
      syncName(); requestSave(); syncIdleControls();
    });
    document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => playAction(button.dataset.action)));
    $('#voice-time').addEventListener('input', event => { stopTour(); voiceTime = Number(event.target.value); voicePaused = true; syncVoiceControls(); });
    for (const event of ['pointerdown', 'keydown']) $('#voice-time').addEventListener(event, () => { stopTour(); voicePaused = true; syncVoiceControls(); });
    $('#voice-pause').addEventListener('click', () => { stopTour(); voicePaused = !voicePaused; syncVoiceControls(); });
    $('#voice-source').addEventListener('change', syncVoiceControls);
    $('#voice-strength').addEventListener('input', event => { $('#voice-strength-label').textContent = `${Math.round(Number(event.target.value) * 100)}%`; });
    $('#voice-level').addEventListener('input', event => { $('#voice-level-label').textContent = `${Math.round(Number(event.target.value) * 100)}%`; });
    $('#native-depth').addEventListener('input', event => { if (!idleEligible()) playAction('idle'); character.setDepth(Number(event.target.value)); signatureAvailable = false; presetId = ''; $('#native-preset').selectedIndex = -1; requestSave(); });
    $('#native-quality').addEventListener('change', event => { quality = Number(event.target.value); character.setQuality(quality); resize(); });
    $('#native-reduced').addEventListener('change', event => { character.setReducedMotion(event.target.checked); idleMovements.reset(); syncIdleControls(); });
    $('#native-auto').addEventListener('change', () => { idleMovements.reset(); syncIdleControls(); });
    $('#native-interval').addEventListener('input', event => {
      const interval = Number(event.target.value); idleMovements.reset(interval);
      $('#native-interval-label').textContent = `${Math.round(interval * 0.75)}–${Math.round(interval * 1.25)} s`; syncIdleControls();
    });
    $('#native-next').addEventListener('click', () => playAction(idleMovements.next(signatureAvailable)));
    $('#native-tour').addEventListener('click', () => { if (tourIndex >= 0) { stopTour(); playAction('idle'); } else { $('#voice-source').value = 'demo'; tourIndex = 0; stepTour(performance.now()); } });
    if (!robotView) {
      $('#native-viewport').addEventListener('pointerenter', () => { pointerInside = true; syncIdleControls(); });
      $('#native-viewport').addEventListener('pointermove', event => {
        if (idleEligible()) { const rect = $('#original-character').getBoundingClientRect(); character.setReadyGaze(Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)), Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)), performance.now() / 1000); }
      });
      $('#native-viewport').addEventListener('pointerleave', () => { pointerInside = false; if (idleEligible()) character.clearReadyGaze(performance.now() / 1000); idleMovements.reset(); syncIdleControls(); });
    }
    $('#native-export').addEventListener('click', () => {
      const data = JSON.stringify({ version: 3, renderer: 'original-dots', resourceRevision: bundle.resourceRevision, name, presetId, signatureAvailable, state: encode(character.state()) }, null, 2);
      const url = URL.createObjectURL(new Blob([data], { type: 'application/json' }));
      const link = document.createElement('a'); link.href = url; link.download = 'original-dot-look.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    $('#native-import').addEventListener('click', () => $('#native-file').click());
    $('#native-file').addEventListener('change', async event => {
      const file = event.target.files[0]; if (!file) return;
      try {
        if (file.size > 100000) throw new Error('Choose a small original dots appearance file.');
        const value = JSON.parse(await file.text());
        if (value.version !== 3 || value.renderer !== 'original-dots' || value.resourceRevision !== bundle.resourceRevision) throw new Error('This look needs its matching original renderer bundle.');
        const state = decode(value.state), error = engine.validateAppearance(state); if (error) throw new Error(error);
        playAction('idle');
        const restoreError = character.restore(state); if (restoreError) throw new Error(restoreError);
        name = String(value.name || 'Custom dot').slice(0, 24); presetId = presets.some(p => p.id === value.presetId) ? value.presetId : ''; $('#native-preset').value = presetId; signatureAvailable = value.signatureAvailable === true;
        syncName(); requestSave(); toast('Original look imported.');
      } catch (error) { toast(error.message); } event.target.value = '';
    });
    new ResizeObserver(resize).observe($('#native-viewport'));
    if (robotView) {
      const fullscreen = () => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen()).catch(error => toast(error.message));
      $('#robot-fullscreen').addEventListener('click', fullscreen);
      $('#native-viewport').addEventListener('dblclick', fullscreen);
      document.addEventListener('keydown', event => { if (event.key.toLowerCase() === 'f' && !event.target.closest('input,select,textarea,dialog')) fullscreen(); });
      document.addEventListener('fullscreenchange', () => { $('#robot-fullscreen').textContent = document.fullscreenElement ? 'Exit fullscreen' : 'Enter fullscreen'; resize(); });
    }
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) bakeController?.abort();
      if (document.hidden && !robotView && savePending) { savePending = false; persistConfig(configSnapshot()); }
      character?.setActive(!document.hidden && !bakeController);
      if (!document.hidden) {
        voiceNow = performance.now(); idleMovements.reset(); pointerInside = false;
        if (activeAction) playAction(activeAction.definition.id, false);
        else nextTourAt = performance.now() + 6000;
      }
    });
    addEventListener('pagehide', () => { bakeController?.abort(); if (!robotView && savePending) persistConfig(configSnapshot()); stopTour(); events?.close(); speech.dispose(); character?.delete(); character = undefined; voice?.dispose(); }, { once: true });
    requestAnimationFrame(frame);
  } catch (error) { $('#native-loading').textContent = error.message; $('#native-save').textContent = 'Original renderer unavailable'; console.error(error); }
}
start();
