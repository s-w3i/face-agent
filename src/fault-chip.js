// A small corner chip that names what is wrong (no internet, mic unplugged…).
// Fault text comes from local processes but is still rendered as text only.
const ICONS = {
  wifi: '<path d="M2 8.5a15 15 0 0 1 20 0M5 12a10 10 0 0 1 14 0M8.5 15.5a5 5 0 0 1 7 0"/><circle cx="12" cy="19" r="1"/><path d="M3 3l18 18"/>',
  cloud: '<path d="M7 18h10a4 4 0 0 0 .5-8 6 6 0 0 0-11.5 1.5A3.3 3.3 0 0 0 7 18z"/><path d="M12 10v4M12 16.5v.5"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M15 8l2 2"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M3 3l18 18"/>',
  camera: '<path d="M3 8h4l2-3h6l2 3h4v11H3z"/><circle cx="12" cy="13" r="3.5"/><path d="M3 3l18 18"/>',
  chip: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
  plug: '<path d="M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5"/>',
  speaker: '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M17 9l4 6M21 9l-4 6"/>',
  alert: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 17.5v.5"/>',
};
const OFFLINE_DELAY_MS = 5000;
const DETAILS_OPEN_MS = 20000;

export const GENERIC_ERROR = Object.freeze({ code: 'unexplained_error', severity: 'error', priority: -1, icon: 'alert',
  title: 'Error', message: 'Kuro is in ERROR but no cause was reported.', hint: 'Run ./doctor.sh on the robot.' });
export const DISPLAY_OFFLINE = Object.freeze({ code: 'display_offline', severity: 'error', priority: 100, icon: 'plug',
  title: 'Service offline', message: 'The face lost its connection to the local robot service.',
  hint: 'Check stack-display.log; face-agent.service restarts it by itself.' });

const rank = fault => [fault.severity === 'error' ? 0 : 1, -(fault.priority ?? 0), -(fault.since ?? 0)];
function compare(a, b) {
  const [x, y] = [rank(a), rank(b)];
  for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] - y[i];
  return 0;
}

// Pure view model, testable without a DOM.
export function chipView(faults, { state = '', offline = false } = {}) {
  let items = (Array.isArray(faults) ? faults : []).filter(fault => fault && typeof fault.title === 'string' &&
    (fault.severity === 'error' || fault.severity === 'degraded'));
  if (state === 'error' && !items.some(fault => fault.severity === 'error')) items = [GENERIC_ERROR, ...items];
  if (offline) items = [DISPLAY_OFFLINE, ...items];
  items = [...items].sort(compare);
  if (!items.length) return { visible: false, items };
  const top = items[0];
  return { visible: true, severity: top.severity, icon: ICONS[top.icon] ? top.icon : 'alert', title: top.title,
    more: items.length > 1 ? `+${items.length - 1}` : '', items };
}

export function since(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '';
  const date = new Date(seconds * 1000);
  return `since ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function connectFaults(events, root) {
  let faults = [], state = '', offline = false, offlineTimer, closeTimer;
  root.className = 'fault-chip'; root.hidden = true; root.setAttribute('role', 'status');
  const button = element('button', 'fault-chip-button'); button.type = 'button'; button.setAttribute('aria-expanded', 'false');
  const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  icon.setAttribute('viewBox', '0 0 24 24'); icon.setAttribute('aria-hidden', 'true'); icon.classList.add('fault-icon');
  const title = element('span', 'fault-title'), more = element('span', 'fault-more');
  button.append(icon, title, more);
  const details = element('div', 'fault-details'); details.hidden = true;
  root.replaceChildren(button, details);

  function render() {
    const view = chipView(faults, { state, offline });
    root.hidden = !view.visible;
    if (!view.visible) { details.hidden = true; button.setAttribute('aria-expanded', 'false'); return; }
    root.dataset.severity = view.severity;
    icon.innerHTML = ICONS[view.icon]; // Constant markup only; never fault text.
    title.textContent = view.title; more.textContent = view.more;
    details.replaceChildren(...view.items.map(fault => {
      const item = element('div', 'fault-item');
      item.append(element('strong', '', fault.title));
      if (fault.message) item.append(element('p', '', fault.message));
      if (fault.hint) item.append(element('p', 'fault-hint', fault.hint));
      const when = [since(fault.since), fault.count > 1 ? `seen ${fault.count}×` : ''].filter(Boolean).join(' · ');
      if (when) item.append(element('small', '', when));
      return item;
    }), element('small', 'fault-doctor', 'Details on the robot: ./doctor.sh'));
  }
  function toggle(open = details.hidden) {
    details.hidden = !open; button.setAttribute('aria-expanded', String(open));
    clearTimeout(closeTimer);
    if (open) closeTimer = setTimeout(() => toggle(false), DETAILS_OPEN_MS);
  }
  button.addEventListener('click', event => { event.stopPropagation(); toggle(); });

  events.addEventListener('faults', event => {
    const value = JSON.parse(event.data);
    faults = Array.isArray(value.faults) ? value.faults : [];
    render();
  });
  events.addEventListener('command', event => { state = JSON.parse(event.data).state || ''; render(); });
  events.addEventListener('open', () => { clearTimeout(offlineTimer); offlineTimer = undefined; if (offline) { offline = false; render(); } });
  events.addEventListener('error', () => {
    // EventSource reconnects by itself; only a lasting outage deserves the chip.
    if (offlineTimer === undefined && !offline) offlineTimer = setTimeout(() => { offlineTimer = undefined; offline = true; render(); }, OFFLINE_DELAY_MS);
  });
  render();
  return { render };
}
