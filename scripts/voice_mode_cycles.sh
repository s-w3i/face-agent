#!/usr/bin/env bash
# Usage: scripts/voice_mode_cycles.sh [CYCLES]  (live stack must be running)
# Drive N sleep->awake->sleep cycles through /robot/set_status; log worker pid/threads/RSS
# and how long the mode switch to Realtime took. No voice turns, no TTS.

cycles="${1:-50}"
cd "$(dirname "$0")/.."
set -a; source "$HOME/.config/face-agent/startup.env"; set +a
source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash" >/dev/null 2>&1
source ros2/install/local_setup.bash >/dev/null 2>&1
log="$HOME/.cache/face-agent/stack-agent.log"
status() { .venv/bin/python -c "import sys;sys.path.insert(0,'scripts');from dotsctl import request;print((request('http://127.0.0.1:5173','status').get('robotStatus') or {}).get('status'))"; }
worker() { pgrep -f "scripts/respeaker.py --diagnostics --control-stdin" | head -1; }
for i in $(seq 1 "$cycles"); do
  for _ in $(seq 1 120); do [ "$(status)" = SLEEPING ] && break; sleep 0.5; done
  sleep 1
  lines=$(wc -l < "$log")
  start=$(date +%s.%N)
  ros2 service call /robot/set_status robot_status_interfaces/srv/SetRobotStatus "{status: IDLE}" >/dev/null 2>&1
  ready=""
  for _ in $(seq 1 300); do
    if tail -n +"$((lines + 1))" "$log" | grep -q "Realtime transcription ready."; then ready=$(date +%s.%N); break; fi
    sleep 0.05
  done
  pid=$(worker)
  threads=$(ls /proc/"$pid"/task 2>/dev/null | wc -l)
  rss=$(awk '/VmRSS/ {print $2}' /proc/"$pid"/status 2>/dev/null)
  switch=$( [ -n "$ready" ] && echo "$ready - $start" | bc || echo NA)
  echo "cycle=$i pid=$pid threads=$threads rss_kb=$rss idle_to_ready_s=$switch"
done
