export const DEFAULTS = { name: 'Mimo', shape: 'blob', color: '#008cff', eyes: 'classic', glasses: 'none', accessory: 'beret', finish: 'flocked' };
export const OPTIONS = {
  shape: ['blob', 'round', 'drop', 'heart', 'squircle'],
  eyes: ['classic', 'wide', 'relaxed', 'tiny'],
  glasses: ['none', 'round', 'square', 'sunglasses'],
  accessory: ['none', 'beret', 'cap', 'sprout', 'halo', 'headphones'],
  finish: ['flocked', 'soft', 'glossy'],
};
export const COLORS = ['#008cff', '#adf22b', '#ffce26', '#ef27ca', '#a897e8', '#ff7445', '#ece7df', '#303136'];
export const PRESETS = [
  { id: 'blue', label: 'Blue', shape: 'blob', color: '#008cff', eyes: 'classic', glasses: 'none', accessory: 'beret', finish: 'flocked' },
  { id: 'green', label: 'Green', shape: 'round', color: '#adf22b', eyes: 'wide', glasses: 'none', accessory: 'none', finish: 'flocked' },
  { id: 'yellow', label: 'Yellow', shape: 'drop', color: '#ffce26', eyes: 'relaxed', glasses: 'round', accessory: 'none', finish: 'flocked' },
  { id: 'pink', label: 'Pink', shape: 'heart', color: '#ef27ca', eyes: 'classic', glasses: 'sunglasses', accessory: 'none', finish: 'flocked' },
];
export const MOTIONS = [
  { id: 'idle', name: 'Idle', icon: 'sun', description: 'Soft breathing and blinking, with a gently changing contour.', duration: 5, loop: true },
  { id: 'listening', name: 'Listening', icon: 'ear', description: 'Stands taller, opens its eyes, and bends forward with a slow nod.', duration: 5, loop: true },
  { id: 'thinking', name: 'Thinking', icon: 'thought', description: 'Narrows and bends into a shifting S curve, eyes looking upward.', duration: 5, loop: true },
  { id: 'speaking', name: 'Speaking', icon: 'sound', description: 'Reshapes with syllable pulses, side ripples, and phrase pauses. Visual demo.', duration: 5, loop: true },
  { id: 'sleeping', name: 'Sleeping', icon: 'moon', description: 'Settles into a wide, curled shape with closed eyes and slow breathing.', duration: 5, loop: true },
  { id: 'happy', name: 'Happy', icon: 'smile', description: 'A happy wiggle and two very enthusiastic little hops.', duration: 3.5 },
  { id: 'curious', name: 'Curious', icon: 'search', description: 'Something caught those curious little eyes.', duration: 4 },
  { id: 'bounce', name: 'Bounce', icon: 'bounce', description: 'A small crouch, a big jump, and a soft landing.', duration: 2.8 },
  { id: 'spin', name: 'Spin', icon: 'spin', description: 'One joyful twirl. Accessories come along for the ride.', duration: 3 },
  { id: 'stretch', name: 'Stretch', icon: 'stretch', description: 'Pulls into a tall, tapered shape, then softly returns to rest.', duration: 4 },
  { id: 'wobble', name: 'Wobble', icon: 'wave', description: 'A wave bends up the body while the base stays planted.', duration: 3.5 },
  { id: 'wake', name: 'Wake up', icon: 'sunrise', description: 'A sleepy start, a bright-eyed stretch, ready again.', duration: 4 },
];
export function cleanConfig(value = {}) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Choose a character JSON file.');
  const result = { ...DEFAULTS };
  if (typeof value.name === 'string') result.name = value.name.trim().slice(0, 24) || DEFAULTS.name;
  if (typeof value.color === 'string' && /^#[\da-f]{6}$/i.test(value.color)) result.color = value.color;
  if (value.shape === 'pebble') result.shape = 'blob';
  for (const [key, choices] of Object.entries(OPTIONS)) if (choices.includes(value[key])) result[key] = value[key];
  return result;
}

// Animation scheduling stays local; a future chatbot only needs to call play().
export class Director {
  constructor(random = Math.random) {
    this.random = random;
    this.state = 'idle'; this.elapsed = 0; this.idleElapsed = 0;
    this.auto = true; this.sleepAfter = 180; this.interval = 18;
    this.paused = false; this.speed = 1; this.tour = false; this.tourIndex = 0;
    this.lastRandom = ''; this.nextIdle = this.nextDelay();
  }
  nextDelay() { return this.interval * (0.8 + this.random() * 0.4); }
  play(id, manual = true) {
    if (!MOTIONS.some(m => m.id === id)) return;
    this.state = id; this.elapsed = 0;
    if (manual) { this.tour = false; this.idleElapsed = 0; this.nextIdle = this.nextDelay(); }
  }
  seek(seconds) {
    if (!Number.isFinite(seconds)) return;
    const motion = MOTIONS.find(m => m.id === this.state);
    this.elapsed = Math.max(0, Math.min(motion.duration, seconds));
    this.paused = true; this.tour = false;
  }
  startTour() { this.tour = true; this.tourIndex = 0; this.paused = false; this.play(MOTIONS[0].id, false); }
  stopTour() { this.tour = false; this.play('idle'); }
  tick(dt) {
    if (this.paused) return;
    this.elapsed += dt * this.speed;
    const motion = MOTIONS.find(m => m.id === this.state);
    if (this.tour && this.elapsed >= motion.duration) {
      this.tourIndex++;
      if (this.tourIndex >= MOTIONS.length) this.stopTour();
      else this.play(MOTIONS[this.tourIndex].id, false);
      return;
    } else if (!this.tour && !motion.loop && this.elapsed >= motion.duration) {
      this.play('idle', false); this.nextIdle = this.nextDelay();
      return;
    }
    if (!this.tour && this.state === 'idle') {
      this.idleElapsed += dt;
      this.nextIdle -= dt;
      if (this.sleepAfter > 0 && this.idleElapsed >= this.sleepAfter) this.play('sleeping', false);
      else if (this.auto && this.nextIdle <= 0) {
        const choices = ['curious', 'stretch', 'wobble', 'bounce', 'spin'].filter(id => id !== this.lastRandom);
        this.lastRandom = choices[Math.floor(this.random() * choices.length)];
        this.play(this.lastRandom, false);
      }
    }
  }
}
