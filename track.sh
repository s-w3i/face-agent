#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")"
tracking_python="${TRACKING_PYTHON:-python3}"
if ! "$tracking_python" -c 'import rclpy; from sensor_msgs.msg import CompressedImage' >/dev/null 2>&1; then
  printf '%s\n' 'Source your ROS setup first, using the Python interpreter that matches ROS.' 'For Humble: source /opt/ros/humble/setup.bash' >&2
  exit 1
fi
tracking_environment="${XDG_CACHE_HOME:-$HOME/.cache}/face-agent/tracking-venv"
if [ ! -x "$tracking_environment/bin/python" ]; then
  "$tracking_python" -m venv --system-site-packages "$tracking_environment"
fi
if ! "$tracking_environment/bin/python" -c 'import rclpy, cv2, numpy; assert cv2.__version__ == "4.11.0" and int(numpy.__version__.split(".")[0]) < 2' >/dev/null 2>&1; then
  "$tracking_environment/bin/python" -m pip install -r requirements-tracking.txt
fi
"$tracking_environment/bin/python" scripts/setup_tracking.py
exec "$tracking_environment/bin/python" scripts/track_human.py "$@"
