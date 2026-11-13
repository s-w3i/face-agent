// This overlay selects an existing animation without altering the conversation command.
export class PickVisual {
  constructor() { this.generation = ''; this.sequence = -1; this.mission = null; this.until = 0; }
  receive(value, now) {
    if (value.generation === this.generation && value.sequence <= this.sequence) return false;
    this.generation = value.generation; this.sequence = value.sequence;
    this.mission = value.active ? value.mission_id : null;
    this.until = now + Math.max(0, Math.min(10000, Number(value.ttl_ms) || 0));
    return true;
  }
  state(base, now, blocked = false) {
    return !blocked && this.mission && now < this.until && ['idle', 'listening', 'needs-input'].includes(base) ? 'thinking' : base;
  }
}
