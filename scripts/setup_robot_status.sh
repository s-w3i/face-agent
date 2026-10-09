#!/usr/bin/env bash
set -e
status_root="$(cd "$(dirname "$0")/.." && pwd)"
exec 9> "$status_root/ros2/.build.lock"
flock 9
source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
cmake -S "$status_root/ros2/robot_status_interfaces" -B "$status_root/ros2/build" \
  -DCMAKE_INSTALL_PREFIX="$status_root/ros2/install" -DPython3_EXECUTABLE=/usr/bin/python3 -DPYTHON_EXECUTABLE=/usr/bin/python3
cmake --build "$status_root/ros2/build" --target install --parallel 2
cat > "$status_root/ros2/install/local_setup.bash" <<'SETUP'
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/share/robot_status_interfaces/local_setup.bash"
SETUP
