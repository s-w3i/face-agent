#!/usr/bin/env bash
# USB 2 color-only Orbbec stream for local face tracking on this Pi.
set -e
camera_setup="/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
if [ ! -r "$camera_setup" ]; then
  printf 'ROS setup not found: %s\n' "$camera_setup" >&2
  exit 1
fi
source "$camera_setup"
exec ros2 launch orbbec_camera gemini_330_series.launch.py \
  camera_name:=head_camera \
  color_width:=640 color_height:=480 color_fps:=15 color_format:=MJPG \
  enable_depth:=false enable_point_cloud:=false enable_frame_sync:=false \
  uvc_backend:=v4l2 retry_on_usb3_detection_failure:=false "$@"
