#!/usr/bin/env bash
# Use Ubuntu's AccountsService and its normal administrator authentication dialog.
set -e
if [ "$(id -u)" -eq 0 ]; then
  printf '%s\n' 'Run this from your desktop user account, without sudo.' >&2
  exit 1
fi
gdbus call --system --interactive --timeout 300 \
  --dest org.freedesktop.Accounts \
  --object-path "/org/freedesktop/Accounts/User$(id -u)" \
  --method org.freedesktop.Accounts.User.SetAutomaticLogin true
printf '%s\n' 'Automatic desktop login enabled for the next boot. The current session was not restarted.'
