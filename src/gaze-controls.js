export function gazeControls(api, onError) {
  document.querySelector('.robot-hud').insertAdjacentHTML('beforeend', '<button id="gaze-test-open" aria-expanded="false">Test gaze</button>');
  document.querySelector('#app').insertAdjacentHTML('beforeend', `<aside id="gaze-test" class="gaze-test" aria-label="Eye tracking test" hidden><div class="gaze-test-heading"><strong>Eye tracking test</strong><button id="gaze-test-close" aria-label="Close gaze test">×</button></div><p>Simulate a camera target. Body animations continue independently.</p><label><input id="gaze-person" type="checkbox"> Simulated person</label><label><input id="gaze-pointer" type="checkbox"> Follow pointer</label><label for="gaze-x">Target X <output id="gaze-x-label">0.50</output></label><input id="gaze-x" type="range" min="0" max="1" step="0.01" value="0.5"><label for="gaze-y">Target Y <output id="gaze-y-label">0.50</output></label><input id="gaze-y" type="range" min="0" max="1" step="0.01" value="0.5"><div class="gaze-test-actions">${['idle','listening','speaking','wave','drag','sleeping'].map(id => `<button data-gaze-state="${id}">${id}</button>`).join('')}</div><button id="gaze-lost">No person · original eyes</button><output id="gaze-readout" role="status">Waiting for target</output></aside>`);
  const $ = selector => document.querySelector(selector);
  const person = $('#gaze-person'), pointer = $('#gaze-pointer'), x = $('#gaze-x'), y = $('#gaze-y');
  let busy = false;
  async function send() {
    if (busy) return;
    busy = true;
    try { await api('gaze', person.checked ? { detected: true, x: Number(x.value), y: Number(y.value) } : { detected: false }); }
    catch (error) { onError(error.message); }
    finally { busy = false; }
  }
  const labels = () => { $('#gaze-x-label').textContent = Number(x.value).toFixed(2); $('#gaze-y-label').textContent = Number(y.value).toFixed(2); };
  $('#gaze-test-open').addEventListener('click', () => { $('#gaze-test').hidden = !$('#gaze-test').hidden; $('#gaze-test-open').setAttribute('aria-expanded', String(!$('#gaze-test').hidden)); });
  $('#gaze-test-close').addEventListener('click', () => { $('#gaze-test').hidden = true; $('#gaze-test-open').setAttribute('aria-expanded', 'false'); });
  person.addEventListener('change', () => { if (!person.checked) pointer.checked = false; send(); });
  pointer.addEventListener('change', () => { person.checked = pointer.checked; send(); });
  for (const slider of [x, y]) slider.addEventListener('input', () => { person.checked = true; labels(); send(); });
  document.addEventListener('pointermove', event => {
    if (!pointer.checked || event.target.closest('#gaze-test,dialog,.robot-hud')) return;
    x.value = event.clientX / document.documentElement.clientWidth; y.value = event.clientY / document.documentElement.clientHeight; labels();
  });
  $('#gaze-lost').addEventListener('click', () => { person.checked = pointer.checked = false; send(); });
  for (const button of document.querySelectorAll('[data-gaze-state]')) button.addEventListener('click', () => api('command', { state: button.dataset.gazeState }).catch(error => onError(error.message)));
  const timer = setInterval(() => { if (person.checked && !document.hidden) send(); }, 200);
  return { update(gaze, state, supported, enabled) {
    const description = !enabled ? 'Tracking disabled' : state === 'sleeping' ? 'Sleeping · original closed eyes' : !supported ? 'Original eyes · no eye data for this frame' : gaze.tracking ? 'Tracking person' : 'No person · original eyes';
    $('#gaze-readout').textContent = `${description} · smooth ${(gaze.position[0] / 2 + .5).toFixed(2)}, ${(gaze.position[1] / 2 + .5).toFixed(2)}`;
  }, dispose() { clearInterval(timer); } };
}
