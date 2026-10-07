// Actions exposed by the copied Orbit 0.11 binding. Gestures drive its native physics.
export const ORIGINAL_ACTIONS = [
  { id: 'idle', label: 'Idle / Ready', kind: 'activity', activity: 7, description: 'Original Ready animation, blinking and breathing, with occasional random movements.' },
  { id: 'thinking', label: 'Thinking', kind: 'activity', activity: 5, description: 'Original thinking activity, including its light-bulb prop.' },
  { id: 'sleeping', label: 'Sleeping', kind: 'activity', activity: 8, description: 'Original Paused activity: closed eyes and sleep marks.' },
  { id: 'working', label: 'Working', kind: 'activity', activity: 1, description: 'Original working activity.' },
  { id: 'searching', label: 'Searching', kind: 'activity', activity: 2, description: 'Original searching activity.' },
  { id: 'creating', label: 'Creating', kind: 'activity', activity: 3, description: 'Original creating activity.' },
  { id: 'needs-input', label: 'Needs input', kind: 'activity', activity: 6, description: 'Original attention request.' },
  { id: 'error', label: 'Error', kind: 'activity', activity: 9, description: 'Original error activity.' },
  { id: 'payment', label: 'Payment', kind: 'activity', activity: 4, description: 'Original payment activity.' },
  { id: 'wave', label: 'Wave', kind: 'reaction', reaction: 1, duration: 8, description: 'Original finite wave reaction, then back to idle.' },
  { id: 'signature', label: 'Your signature', kind: 'reaction', reaction: 2, duration: 8, description: 'The signature of your current original outfit, then back to idle.' },
  { id: 'look-left', label: 'Look left', kind: 'gaze', target: [0.05, 0.4], duration: 4, description: 'Native Ready gaze to the left, easing back into idle.' },
  { id: 'look-right', label: 'Look right', kind: 'gaze', target: [0.95, 0.4], duration: 4, description: 'Native Ready gaze to the right, easing back into idle.' },
  { id: 'look-up', label: 'Look up', kind: 'gaze', target: [0.5, 0.1], duration: 4, description: 'Native Ready gaze upward, easing back into idle.' },
  { id: 'look-down', label: 'Look down', kind: 'gaze', target: [0.5, 0.9], duration: 4, description: 'Native Ready gaze downward, easing back into idle.' },
  { id: 'press', label: 'Press & release', kind: 'gesture', duration: 5, cues: [[0, 0, 0.5, 0.55], [1, 2, 0.5, 0.55], [1.6, 1, 0.5, 0.5]], description: 'A scripted pointer press and release using the original interaction physics.' },
  { id: 'drag', label: 'Drag & release', kind: 'gesture', duration: 5, cues: [[0, 0, 0.5, 0.55], [0.2, 1, 0.55, 0.54], [0.4, 1, 0.62, 0.5], [0.6, 1, 0.7, 0.44], [0.8, 1, 0.63, 0.5], [1, 2, 0.63, 0.5], [1.6, 1, 0.5, 0.5]], description: 'A scripted drag and soft release using the original interaction physics.' },
  { id: 'artifact-ready', label: 'Artifact ready', kind: 'outcome', activity: 3, outcome: 1, duration: 8, description: 'Creating, followed by its native ArtifactReady completion. This is an animation demo.' },
  { id: 'payment-paid', label: 'Payment paid', kind: 'outcome', activity: 4, outcome: 2, duration: 8, description: 'Payment, followed by its native PaymentPaid completion. This is an animation demo.' },
  { id: 'input-received', label: 'Input received', kind: 'outcome', activity: 6, outcome: 3, duration: 8, description: 'Needs input, followed by its native InputReceived completion.' },
  { id: 'thinking-resolved', label: 'Thinking resolved', kind: 'outcome', activity: 5, outcome: 4, duration: 8, description: 'Thinking, followed by its native ThinkingResolved completion.' },
];
export const VOICE_ACTIONS = [
  { id: 'listening', label: 'Listening', kind: 'voice', description: 'Added motion: leans forward, stretches attentively, and makes small acknowledgment nods.' },
  { id: 'speaking', label: 'Speaking', kind: 'voice', description: 'Added motion: changes the body contour with syllables, emphasis, and pauses. Try Fixed speech level to compare silence and full volume.' },
];
export function signatureActions(heroes) {
  return heroes.map(hero => ({ id: `signature:${hero.id}`, label: `${hero.title} signature`, kind: 'hero', hero: hero.id, title: hero.title, reaction: 2, duration: 8,
    description: `${hero.title}’s original signature. Its outfit is shown temporarily; your look returns afterward.` }));
}
export const IDLE_ACTIONS = ['look-left', 'look-right', 'look-up', 'look-down', 'press', 'drag', 'wave', 'signature'];

// A visible-time countdown; callers pause it whenever a status or preview is active.
export class IdleMovements {
  constructor(random = Math.random) { this.random = random; this.interval = 12; this.last = ''; this.reset(); }
  reset(interval = this.interval) {
    this.interval = Math.max(5, Math.min(60, interval));
    this.remaining = this.interval * (0.75 + this.random() * 0.5);
  }
  next(signatureAvailable) {
    const choices = IDLE_ACTIONS.filter(id => (id !== 'signature' || signatureAvailable) && id !== this.last);
    this.last = choices[Math.min(choices.length - 1, Math.floor(this.random() * choices.length))];
    this.reset(); return this.last;
  }
  tick(dt, { idle = true, enabled = true, reduced = false, signatureAvailable = true } = {}) {
    if (!idle || !enabled || reduced) return null;
    this.remaining -= Math.max(0, dt);
    return this.remaining <= 0 ? this.next(signatureAvailable) : null;
  }
}
