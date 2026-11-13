#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -f ros2/install/local_setup.bash ]; then bash scripts/setup_robot_status.sh; fi
source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
source ros2/install/local_setup.bash
exec /usr/bin/python3 scripts/robot_status_node.py "$@"
