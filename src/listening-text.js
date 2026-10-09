// Transcript text is rendered as text, including untrusted microphone input.
export function connectListeningText(events, element, onChange = () => {}) {
  let generation = '', sequence = -1;
  events.addEventListener('listening-text', event => {
    const value = JSON.parse(event.data);
    if (typeof value.text !== 'string' || value.text.length > 12000) return;
    if (value.generation === generation && value.sequence <= sequence) return;
    generation = value.generation; sequence = value.sequence;
    element.textContent = value.text;
    element.hidden = !value.text;
    element.dataset.final = String(value.final === true);
    onChange();
  });
}

export function listeningTop(element, rect) {
  return element?.hidden !== false ? 0 : Math.max(0, element.getBoundingClientRect().bottom - rect.top + 12);
}
