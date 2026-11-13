#!/usr/bin/env bash
# Choose the robot speaker: status | respeaker | jieli | hdmi | <full sink name>
#
# Chromium (the robot display) plays through the PipeWire default sink. WirePlumber
# stores `pactl set-default-sink` in ~/.local/state/wireplumber/default-nodes, so the
# choice survives a reboot without any extra config. Streams that are already playing
# are moved too. Hardware echo cancellation (barge-in while speaking) needs `respeaker`.
set -eu
target="${1:-status}"
state="${XDG_STATE_HOME:-$HOME/.local/state}/wireplumber/default-nodes"

if [ "$target" = status ]; then
  printf 'default sink: %s\n' "$(pactl get-default-sink)"
  printf 'persisted:    %s\n' "$(grep -m1 '^default.configured.audio.sink=' "$state" 2>/dev/null | cut -d= -f2- || echo none)"
  pactl list short sinks | cut -f2
  exit 0
fi

case "$target" in
  respeaker) pattern='ReSpeaker' ;;
  jieli) pattern='Jieli' ;;
  hdmi) pattern='hdmi' ;;
  *) pattern="$target" ;;
esac
matches=$(pactl list short sinks | cut -f2 | grep -F -i -- "$pattern" || true)
count=$(printf '%s' "$matches" | grep -c . || true)
if [ "$count" -ne 1 ]; then
  printf 'Expected one sink matching %s, found %s:\n' "$pattern" "$count" >&2
  pactl list short sinks | cut -f2 >&2
  exit 1
fi
sink="$matches"
pactl set-default-sink "$sink"
for input in $(pactl list short sink-inputs | cut -f1); do
  pactl move-sink-input "$input" "$sink" || true
done
sleep .5
if grep -qxF "default.configured.audio.sink=$sink" "$state" 2>/dev/null; then
  printf 'Default speaker is now %s (persisted in %s).\n' "$sink" "$state"
else
  printf 'Default speaker is now %s, but WirePlumber has not persisted it yet; check %s.\n' "$sink" "$state" >&2
fi
