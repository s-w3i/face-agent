// Transcript text is rendered as text, including untrusted microphone input.
// While Kuro is awake and idle in voice mode, the same element says "Listening…"
// and a bar under it drains over the idle window, so people can see that the
// microphone is open and when Kuro will fall asleep.
export const LISTENING_PLACEHOLDER = 'Listening…';

export function connectListeningText(events, element, onChange = () => {}) {
  let generation = '', sequence = -1, transcript = '', listen = null, round = 0;
  function render() {
    const placeholder = !transcript && listen !== null;
    element.textContent = placeholder ? LISTENING_PLACEHOLDER : transcript;
    element.hidden = !transcript && !placeholder;
    element.dataset.placeholder = String(placeholder);
    if (placeholder && listen.seconds) {
      // Alternating two identical keyframe names restarts the CSS animation.
      element.dataset.countdown = listen.round % 2 ? 'b' : 'a';
      element.style?.setProperty('--listen-seconds', listen.seconds + 's');
    } else delete element.dataset.countdown;
    onChange();
  }
  events.addEventListener('listening-text', event => {
    const value = JSON.parse(event.data);
    if (typeof value.text !== 'string' || value.text.length > 12000) return;
    if (value.generation === generation && value.sequence <= sequence) return;
    generation = value.generation; sequence = value.sequence;
    transcript = value.text;
    element.dataset.final = String(value.final === true);
    render();
  });
  return {
    // Call with every display command. Only the voice agent's idle command carries
    // listenSeconds (a number, or null when Kuro will not fall asleep).
    command(command) {
      const listening = command.state === 'idle' && !command.speech && 'listenSeconds' in command;
      const next = listening ? { seconds: command.listenSeconds, round: ++round } : null;
      if (next === null && listen === null) return;
      listen = next; render();
    },
  };
}

export function listeningTop(element, rect) {
  return element?.hidden !== false ? 0 : Math.max(0, element.getBoundingClientRect().bottom - rect.top + 12);
}
