#!/usr/bin/env bash
# USB 2 color/depth profile verified above 10 delivered FPS on this Pi.
# Projector level 6 causes USB resets on the current cable; level 5 was stable.
set -e
camera_setup="/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
if [ ! -r "$camera_setup" ]; then
  printf 'ROS setup not found: %s\n' "$camera_setup" >&2
  exit 1
fi
source "$camera_setup"

exec ros2 launch orbbec_camera gemini_330_series.launch.py \
  camera_name:=head_camera \
  enable_color:=true color_width:=640 color_height:=480 \
  color_fps:=15 color_format:=MJPG \
  enable_depth:=true depth_width:=640 depth_height:=480 \
  depth_fps:=15 depth_format:=Y16 \
  depth_registration:=true enable_frame_sync:=true \
  enable_point_cloud:=false enable_colored_point_cloud:=false \
  enable_laser:=true laser_energy_level:=5 \
  color_qos:=SENSOR_DATA depth_qos:=SENSOR_DATA \
  uvc_backend:=v4l2 retry_on_usb3_detection_failure:=false "$@"
