import './style.css';
import { CharacterScene } from './character.js';
import { DEFAULTS, OPTIONS, COLORS, PRESETS, MOTIONS, cleanConfig, Director } from './model.js';

const paths = {
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.4 1.4m11.2 11.2L19 19M5 19l1.4-1.4M17.6 6.4 19 5"/>',
  ear: '<path d="M8 17c0 5 6 5 6 0 0-3 5-4 5-9A7 7 0 0 0 5 8m4 2V8a3 3 0 0 1 6 0c0 3-4 3-4 6"/>',
  thought: '<path d="M6 15a6 6 0 1 1 3 3"/><circle cx="5" cy="18" r="1.5"/><path d="M2 22h.01M9 9h.01M13 9h.01M17 9h.01"/>',
  sound: '<path d="m4 9 4 0 5-4v14l-5-4H4Zm12-2a7 7 0 0 1 0 10m3-13a11 11 0 0 1 0 16"/>',
  moon: '<path d="M20 14a9 9 0 0 1-10-10A9 9 0 1 0 20 14Z"/>',
  smile: '<circle cx="12" cy="12" r="9"/><path d="M8 14c2 3 6 3 8 0M8 9h.01M16 9h.01"/>',
  search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5M8 10h4m-2-2v4"/>',
  bounce: '<path d="M4 20h16M8 7l4-4 4 4m-4-3v7"/><circle cx="12" cy="15" r="3"/>',
  spin: '<path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 4M4 14l2 4a7 7 0 0 0 12-1"/>',
  stretch: '<path d="M12 3v18m-4-4 4 4 4-4M8 7l4-4 4 4M3 12h3m12 0h3"/>',
  wave: '<path d="M2 12c3-12 5 12 8 0s5 12 8 0 3 0 4 0"/>',
  sunrise: '<path d="M3 18h18M6 15a6 6 0 0 1 12 0M12 2v3M3 7l2 2m14 0 2-2M3 22h18"/>',
  play: '<path d="m8 4 12 8-12 8Z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  reset: '<path d="M3 10a9 9 0 1 1 2 8M3 4v6h6"/>',
  shuffle: '<path d="M3 6h3c4 0 8 12 12 12h3M3 18h3c4 0 8-12 12-12h3m-4-4 4 4-4 4m0 4 4 4-4 4"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  upload: '<path d="M12 16V4m-5 5 5-5 5 5M4 16v5h16v-5"/>',
  expand: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
  check: '<path d="m5 12 4 4 10-10"/>',
  leaf: '<path d="M5 19C1 9 10 3 21 3c0 11-6 18-16 16Zm0 0L16 8"/>',
  cube: '<path d="m12 2 9 5v10l-9 5-9-5V7Zm0 10 9-5M3 7l9 5v10"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
};
const icon = (id, size = 20) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[id] || paths.sun}</svg>`;
const title = text => text[0].toUpperCase() + text.slice(1);
const choices = (key, labels = {}) => OPTIONS[key].map(value => `<button type="button" class="choice ${key === 'shape' ? 'shape-choice' : ''}" data-key="${key}" data-value="${value}" aria-pressed="false">${key === 'shape' ? `<span class="shape-sample ${value}"></span>` : ''}${labels[value] || title(value)}</button>`).join('');
const app = document.querySelector('#app');
app.innerHTML = `
  <header class="topbar">
    <a class="brand" href="./" aria-label="Mimo character studio"><span class="brand-mark"><i></i><i></i></span>mimo<span class="brand-divider"></span><span class="brand-subtitle">character studio</span></a>
    <div class="topbar-right"><a class="text-button renderer-link" href="./original-dots.html">Original dots renderer ↗</a><span class="local-badge"><span></span> A little life, locally</span><button class="text-button" id="export">${icon('download', 16)} Export character</button></div>
  </header>
  <main>
    <section class="intro"><div><div class="eyebrow"><span></span> A STUDY IN FORM, TEXTURE & MOTION</div><h1>Small form.<br><em>Distinct personality.</em></h1><p>Sculptural shapes. Flocked surfaces. Expression through changing form.</p></div><div class="intro-note">Form.<br>Texture.<br>Motion.<span class="scribble">↙</span></div></section>
    <div class="studio-layout">
      <section class="preview-panel" aria-label="Character preview">
        <div class="stage" id="stage">
          <div class="stage-top"><div class="live-label"><span></span> LIVE PREVIEW</div><button class="icon-button" id="fullscreen" aria-label="Expand preview" title="Expand preview">${icon('expand', 17)}</button></div>
          <div id="viewport"></div>
          <div class="loading" id="loading">Bringing your little friend to life…</div>
          <div class="floating-emotion" id="emotion" aria-hidden="true"></div>
          <div class="stage-name"><span class="name-pill" id="name-pill">Mimo</span><span class="stage-caption" id="stage-caption">Just happy to be here.</span></div>
          <div class="stage-bottom"><span>${icon('cube', 14)} Drag to orbit · Scroll to zoom</span><button class="text-button" id="reset-camera">${icon('reset', 14)} Reset view</button></div>
          <div class="floor-lines" aria-hidden="true"></div>
        </div>
        <div class="playback"><div class="now-playing"><button id="pause" class="round-button" aria-label="Pause animation">${icon('pause', 17)}</button><div><span class="small-label" id="playing-label">NOW PLAYING</span><strong id="current-motion">Idle</strong></div><span class="loop-badge" id="loop-badge">LOOP</span></div><div class="playback-right"><label class="speed-label" for="speed">Speed</label><select id="speed" aria-label="Animation speed"><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="1.5">1.5×</option><option value="2">2×</option></select><button class="play-all" id="play-all">${icon('play', 13)} Play all</button></div></div>
        <div class="motion-timeline"><label for="motion-time">Animation time <output id="motion-time-label">0.0 / 5.0 s</output></label><input type="range" id="motion-time" min="0" max="5" step="0.01" value="0" aria-label="Animation time"><span>Scrub to pause and inspect a pose</span></div>
        <div class="animation-library"><div class="section-heading"><div><h2>Motion studies</h2><p>Choose a status. Watch its body change.</p></div><span class="count-badge">12 animations</span></div><div class="motion-grid">${MOTIONS.map((m, i) => `<button class="motion-button" data-motion="${m.id}" aria-pressed="${i === 0}"><span class="motion-icon">${icon(m.icon, 20)}</span><span>${m.name}</span><span class="motion-indicator"></span></button>`).join('')}</div><p id="motion-description" class="motion-description">${MOTIONS[0].description}</p></div>
      </section>
      <aside class="custom-panel" aria-label="Character customization">
        <div class="custom-header"><div><span class="eyebrow">SHAPE YOUR CHARACTER</span><h2>Make it your own</h2></div><button class="icon-button" id="randomize" aria-label="Randomize appearance" title="Surprise me">${icon('shuffle', 18)}</button></div>
        <div class="panel-tabs" role="tablist" aria-label="Studio settings"><button role="tab" id="tab-look" aria-selected="true" aria-controls="panel-look" data-tab="look">Appearance</button><button role="tab" id="tab-behavior" aria-selected="false" aria-controls="panel-behavior" data-tab="behavior" tabindex="-1">Behavior</button><button role="tab" id="tab-scene" aria-selected="false" aria-controls="panel-scene" data-tab="scene" tabindex="-1">Scene</button></div>
        <div class="panel-body" id="panel-look" role="tabpanel" aria-labelledby="tab-look">
          <fieldset class="preset-field"><legend>Reference looks</legend><div class="preset-row">${PRESETS.map(p => `<button class="preset" data-preset="${p.id}" style="--preset:${p.color}" aria-pressed="false"><span class="preset-sample ${p.shape}"></span>${p.label}</button>`).join('')}</div></fieldset>
          <label class="field-label" for="character-name">A name to call your own</label><div class="name-input"><input id="character-name" maxlength="24" value="Mimo" autocomplete="off" spellcheck="false"><span>✧</span></div>
          <fieldset><legend>Body shape</legend><div class="choice-row shapes">${choices('shape')}</div></fieldset>
          <fieldset><legend>Color <span id="color-name">Sage</span></legend><div class="swatch-row">${COLORS.map((color, i) => `<button class="swatch" style="--swatch:${color}" data-color="${color}" aria-label="${['Blue', 'Lime', 'Yellow', 'Pink', 'Lilac', 'Coral', 'Chalk', 'Charcoal'][i]}" aria-pressed="false">${icon('check', 16)}</button>`).join('')}<label class="custom-color" title="Choose any color"><input type="color" id="custom-color" value="#a4b58b" aria-label="Custom body color"><span>+</span></label></div></fieldset>
          <fieldset><legend>Eyes</legend><div class="choice-row">${choices('eyes', { classic: 'Oval', wide: 'Raised', relaxed: 'Relaxed', tiny: 'Dot' })}</div></fieldset>
          <fieldset><legend>Glasses</legend><div class="choice-row">${choices('glasses', { sunglasses: 'Shades' })}</div></fieldset>
          <fieldset><legend>A little extra</legend><div class="choice-row accessory-row">${choices('accessory', { none: 'None', beret: 'Beret', cap: 'Cap', sprout: 'Sprout', halo: 'Halo', headphones: 'Headphones' })}</div></fieldset>
          <fieldset><legend>Material</legend><div class="choice-row">${choices('finish', { flocked: 'Flocked', soft: 'Matte', glossy: 'Gloss' })}</div></fieldset>
        </div>
        <div class="panel-body" id="panel-behavior" role="tabpanel" aria-labelledby="tab-behavior" hidden>
          <div class="panel-explainer">A little personality, even when you’re away.</div>
          <label class="toggle-row"><span><strong>Spontaneous little moves</strong><small>Random animations while idle</small></span><input type="checkbox" id="auto-idle" checked role="switch"><span class="switch"></span></label>
          <label class="field-label" for="idle-interval">Time between idle moves <output id="idle-value">18 seconds</output></label><input type="range" id="idle-interval" min="5" max="60" value="18" step="1"><div class="range-ends"><span>More playful</span><span>More peaceful</span></div>
          <label class="field-label spaced" for="sleep-after">Drift off after</label><select class="full-select" id="sleep-after"><option value="0">Never</option><option value="30">30 seconds of idle</option><option value="60">1 minute of idle</option><option value="180" selected>3 minutes of idle</option><option value="300">5 minutes of idle</option></select>
          <label class="toggle-row"><span><strong>Follow your cursor</strong><small>Curious eyes track your movement</small></span><input type="checkbox" id="follow-pointer" checked role="switch"><span class="switch"></span></label>
          <button class="outline-button full" id="idle-now">${icon('shuffle', 16)} Try an idle surprise</button><p class="settings-note">Manual previews always take priority. Pick any animation to wake your companion. “Play all” runs through every motion once.</p>
        </div>
        <div class="panel-body" id="panel-scene" role="tabpanel" aria-labelledby="tab-scene" hidden>
          <div class="panel-explainer">Set the stage for your little companion.</div>
          <fieldset><legend>Backdrop</legend><div class="backdrops"><button data-backdrop="warm" class="backdrop warm" aria-pressed="true">Studio</button><button data-backdrop="green" class="backdrop green" aria-pressed="false">Stone</button><button data-backdrop="night" class="backdrop night" aria-pressed="false">Dark</button></div></fieldset>
          <label class="field-label spaced" for="quality">Rendering</label><select class="full-select" id="quality"><option value="eco">Lightweight · 30 fps target</option><option value="studio">Studio · 60 fps target</option></select>
          <p class="settings-note">Lightweight limits resolution and frame rate for small devices. Test on your Raspberry Pi to find its best settings.</p>
          <div class="render-stats"><div><span>Frame rate</span><strong id="fps-stat">—</strong></div><div><span>Triangles</span><strong id="tri-stat">—</strong></div><div><span>Draw calls</span><strong id="calls-stat">—</strong></div></div>
          <button class="outline-button full" id="import">${icon('upload', 16)} Import character</button><input type="file" id="import-file" accept=".json,application/json" hidden>
          <p class="settings-note">Export your appearance as a small JSON file, then import it on another device.</p>
        </div>
        <div class="panel-footer"><span id="save-state">${icon('check', 14)} Saved on this device</span><button class="text-button" id="reset-character">Reset</button></div>
      </aside>
    </div>
    <footer class="page-footer"><span>${icon('leaf', 15)} Small footprint. Full of character.</span><span>Made to be a little more you.<span class="footer-dot">✳</span></span></footer>
  </main><div class="toast" id="toast" role="status"></div>`;

const $ = selector => document.querySelector(selector);
let config = { ...DEFAULTS }, storageAvailable = true;
try { config = cleanConfig(JSON.parse(localStorage.getItem('mimo-character-v2') || 'null') || DEFAULTS); } catch { storageAvailable = false; }
const director = new Director();
if (matchMedia('(prefers-reduced-motion: reduce)').matches) { director.auto = false; director.paused = true; $('#auto-idle').checked = false; }
let scene, toastTimer, followPointer = true;
function toast(message) { $('#toast').textContent = message; $('#toast').classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').classList.remove('show'), 3200); }
function save() {
  try { localStorage.setItem('mimo-character-v2', JSON.stringify(config)); storageAvailable = true; } catch { storageAvailable = false; }
  $('#save-state').innerHTML = `${icon(storageAvailable ? 'check' : 'download', 14)} ${storageAvailable ? 'Saved on this device' : 'Use Export to save'} `;
}
function syncConfig(rebuild = true) {
  $('#character-name').value = config.name;
  $('#name-pill').textContent = config.name;
  $('#custom-color').value = config.color;
  document.querySelectorAll('[data-key]').forEach(button => button.setAttribute('aria-pressed', config[button.dataset.key] === button.dataset.value));
  document.querySelectorAll('[data-color]').forEach(button => button.setAttribute('aria-pressed', button.dataset.color === config.color));
  const colorButton = [...document.querySelectorAll('[data-color]')].find(button => button.dataset.color === config.color);
  $('#color-name').textContent = colorButton ? colorButton.getAttribute('aria-label') : config.color.toUpperCase();
  document.querySelectorAll('[data-preset]').forEach(button => { const preset = PRESETS.find(p => p.id === button.dataset.preset); button.setAttribute('aria-pressed', Object.keys(OPTIONS).every(key => preset[key] === config[key]) && preset.color === config.color); });
  if (rebuild && scene) scene.setConfig(config);
}
function syncMotion() {
  const motion = MOTIONS.find(m => m.id === director.state);
  $('#current-motion').textContent = motion.name;
  $('#playing-label').textContent = director.paused ? 'PAUSED' : director.tour ? `PREVIEW ${director.tourIndex + 1} OF ${MOTIONS.length}` : 'NOW PLAYING';
  $('#loop-badge').textContent = motion.loop ? 'LOOP' : 'ONCE';
  $('#motion-description').textContent = motion.description;
  $('#motion-time').max = motion.duration;
  $('#stage-caption').textContent = director.state === 'idle' ? 'Just happy to be here.' : motion.name === 'Sleeping' ? 'Recharging a little.' : `${motion.name}. A little ${config.name} moment.`;
  $('#pause').innerHTML = icon(director.paused ? 'play' : 'pause', 17);
  $('#pause').setAttribute('aria-label', director.paused ? 'Resume animation' : 'Pause animation');
  $('#play-all').innerHTML = `${icon(director.tour ? 'pause' : 'play', 13)} ${director.tour ? 'Stop tour' : 'Play all'}`;
  document.querySelectorAll('[data-motion]').forEach(button => button.setAttribute('aria-pressed', button.dataset.motion === director.state));
  $('#emotion').textContent = ''; 
  $('#emotion').dataset.state = director.state;
  $('#emotion').style.animationPlayState = director.paused ? 'paused' : 'running';
}
try {
  scene = new CharacterScene($('#viewport'), stats => {
    $('#fps-stat').textContent = `${stats.fps} fps`; $('#tri-stat').textContent = stats.triangles.toLocaleString(); $('#calls-stat').textContent = stats.calls;
  }, message => { $('#loading').textContent = message; $('#loading').hidden = false; });
  syncConfig(); $('#loading').hidden = true;
} catch (error) {
  $('#loading').textContent = 'Your browser could not start 3D rendering. Enable hardware acceleration or try an up-to-date Chromium browser.';
  console.error(error);
}
syncConfig(false); syncMotion();
document.querySelectorAll('[data-preset]').forEach(button => button.addEventListener('click', () => {
  const preset = PRESETS.find(p => p.id === button.dataset.preset); config = cleanConfig({ ...preset, name: config.name }); syncConfig(); save(); director.play('idle'); syncMotion();
}));
document.querySelectorAll('[data-key]').forEach(button => button.addEventListener('click', () => {
  config[button.dataset.key] = button.dataset.value; syncConfig(); save();
}));
document.querySelectorAll('[data-color]').forEach(button => button.addEventListener('click', () => { config.color = button.dataset.color; syncConfig(); save(); }));
$('#custom-color').addEventListener('input', e => { config.color = e.target.value; syncConfig(); save(); });
$('#character-name').addEventListener('input', e => { config.name = e.target.value.trim().slice(0, 24) || DEFAULTS.name; $('#name-pill').textContent = config.name; syncMotion(); save(); });
$('#character-name').addEventListener('blur', () => { $('#character-name').value = config.name; });
$('#randomize').addEventListener('click', () => {
  for (const [key, values] of Object.entries(OPTIONS)) config[key] = values[Math.floor(Math.random() * values.length)];
  config.color = COLORS[Math.floor(Math.random() * COLORS.length)]; syncConfig(); save(); toast('A fresh little personality.');
});
$('#reset-character').addEventListener('click', () => { config = { ...DEFAULTS }; syncConfig(); save(); toast('Back to the original Mimo.'); });
document.querySelectorAll('[data-motion]').forEach(button => button.addEventListener('click', () => { director.play(button.dataset.motion); director.paused = false; syncMotion(); }));
$('#pause').addEventListener('click', () => { director.paused = !director.paused; syncMotion(); });
$('#play-all').addEventListener('click', () => { director.tour ? director.stopTour() : director.startTour(); syncMotion(); });
$('#motion-time').addEventListener('input', e => { director.seek(Number(e.target.value)); scene?.update(0, director, true); syncMotion(); });
$('#speed').addEventListener('change', e => { director.speed = Number(e.target.value); });
$('#reset-camera').addEventListener('click', () => scene?.resetCamera());
$('#fullscreen').addEventListener('click', async () => {
  try { if (document.fullscreenElement) await document.exitFullscreen(); else await $('#stage').requestFullscreen(); } catch { toast('Full screen is unavailable in this browser panel.'); }
});
function setTab(tab) {
  document.querySelectorAll('[data-tab]').forEach(button => { const selected = button.dataset.tab === tab; button.setAttribute('aria-selected', selected); button.tabIndex = selected ? 0 : -1; });
  for (const name of ['look', 'behavior', 'scene']) $(`#panel-${name}`).hidden = name !== tab;
}
document.querySelectorAll('[data-tab]').forEach(button => {
  button.addEventListener('click', () => setTab(button.dataset.tab));
  button.addEventListener('keydown', e => { if (!['ArrowLeft', 'ArrowRight'].includes(e.key)) return; e.preventDefault(); const tabs = [...document.querySelectorAll('[data-tab]')]; const next = tabs[(tabs.indexOf(button) + (e.key === 'ArrowRight' ? 1 : 2)) % 3]; setTab(next.dataset.tab); next.focus(); });
});
$('#auto-idle').addEventListener('change', e => { director.auto = e.target.checked; director.nextIdle = director.nextDelay(); director.idleElapsed = 0; });
$('#idle-interval').addEventListener('input', e => { director.interval = Number(e.target.value); director.nextIdle = director.nextDelay(); $('#idle-value').textContent = `${director.interval} seconds`; });
$('#sleep-after').addEventListener('change', e => { director.sleepAfter = Number(e.target.value); director.idleElapsed = 0; });
$('#follow-pointer').addEventListener('change', e => { followPointer = e.target.checked; });
$('#idle-now').addEventListener('click', () => { const motions = ['curious', 'stretch', 'wobble', 'bounce', 'spin']; director.play(motions[Math.floor(Math.random() * motions.length)]); director.paused = false; syncMotion(); });
document.querySelectorAll('[data-backdrop]').forEach(button => button.addEventListener('click', () => { $('#stage').dataset.backdrop = button.dataset.backdrop; document.querySelectorAll('[data-backdrop]').forEach(b => { if (b.tagName === 'BUTTON') b.setAttribute('aria-pressed', b === button); }); }));
$('#quality').addEventListener('change', e => scene?.setQuality(e.target.value));
$('#export').addEventListener('click', () => {
  const data = JSON.stringify({ version: 2, ...config }, null, 2);
  const url = URL.createObjectURL(new Blob([data], { type: 'application/json' }));
  const a = document.createElement('a'); a.href = url; a.download = `${config.name.toLowerCase().replace(/[^a-z0-9_-]/g, '-') || 'mimo'}-character.json`; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000); toast('Your character is ready to take along.');
});
$('#import').addEventListener('click', () => $('#import-file').click());
$('#import-file').addEventListener('change', async e => {
  const file = e.target.files[0]; if (!file) return;
  try {
    if (file.size > 20000) throw new Error('Please choose a small character JSON file.');
    const data = JSON.parse(await file.text());
    if (![1, 2].includes(data.version) || !Object.keys(DEFAULTS).every(key => key in data)) throw new Error('This is not a Mimo character export.');
    config = cleanConfig(data); syncConfig(); save(); toast(`Welcome back, ${config.name}.`);
  } catch (error) { toast(error instanceof SyntaxError ? 'That file is not valid JSON.' : error.message); }
  e.target.value = '';
});
let previousTime = performance.now(), previousMotion = '', previousTourIndex = -1;
function frame(timestamp) {
  const dt = Math.max(0, Math.min((timestamp - previousTime) / 1000, 0.05)); previousTime = timestamp;
  if (!document.hidden) {
    director.tick(dt);
    if (director.state !== previousMotion || director.tourIndex !== previousTourIndex) { syncMotion(); previousMotion = director.state; previousTourIndex = director.tourIndex; }
    const motion = MOTIONS.find(m => m.id === director.state);
    const time = motion.loop && !director.paused ? director.elapsed % motion.duration : Math.min(director.elapsed, motion.duration);
    $('#motion-time').value = time;
    $('#motion-time-label').textContent = `${time.toFixed(1)} / ${motion.duration.toFixed(1)} s`;
    if (scene) { if (!followPointer) scene.pointer.set(0, 0); scene.update(dt, director); scene.render(timestamp); }
  }
  requestAnimationFrame(frame);
}
document.addEventListener('visibilitychange', () => { previousTime = performance.now(); });
requestAnimationFrame(frame);
