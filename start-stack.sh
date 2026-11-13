#!/usr/bin/env bash
# Start the entire robot in the foreground, or under the desktop user service.
set -eo pipefail
cd "$(dirname "$0")"
startup_config="${XDG_CONFIG_HOME:-$HOME/.config}/face-agent/startup.env"
if [ -f "$startup_config" ]; then
  set -a
  source "$startup_config"
  set +a
fi
ros_setup="/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
if [ ! -r "$ros_setup" ]; then
  printf 'ROS setup not found: %s\n' "$ros_setup" >&2
  exit 1
fi
source "$ros_setup"
exec /usr/bin/python3 scripts/start_stack.py "$@"
