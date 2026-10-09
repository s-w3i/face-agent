#!/usr/bin/env bash
# Import this login's display/audio environment before the user service starts.
set -e
session_variables=()
for variable in DISPLAY WAYLAND_DISPLAY XAUTHORITY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS XDG_CURRENT_DESKTOP XDG_SESSION_TYPE PATH; do
  if [ -n "${!variable:-}" ]; then session_variables+=("$variable"); fi
done
if [ "${#session_variables[@]}" -gt 0 ]; then
  systemctl --user import-environment "${session_variables[@]}"
fi
exec systemctl --user start face-agent.service
