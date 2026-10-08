#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")"
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--robot" ]; }; then
  printf '%s\n' 'Usage: ./run.sh [--robot]' '--robot opens the display with automatic voice playback.' >&2
  exit 1
fi
for asset in dist/index.html dist/original-dots.html dist/robot.html dist/local-dots/orbit-characters.mjs dist/local-dots/orbit-characters.wasm dist/local-dots/orbit-characters.data dist/local-dots/bundle.json; do
  if [ ! -f "$asset" ]; then
    printf '%s\n' "Missing bundled file: $asset. Include dist/ when pushing this project." 'For development, rebuild with pnpm install --frozen-lockfile && pnpm build (Node 22.12+).' >&2
    exit 1
  fi
done
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'Install Python 3.10 or newer before launching.' >&2
  exit 1
fi
python3 -c 'import sys; sys.exit("Python 3.10 or newer is required.") if sys.version_info < (3, 10) else None'

if [ ! -x .venv/bin/python ] || ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  printf '%s\n' 'Setting up the local Python environment…'
  if ! python3 -m venv .venv; then
    printf '%s\n' 'On Raspberry Pi OS, install venv support with: sudo apt install python3-venv' 'Then run this launcher again.' >&2
    exit 1
  fi
fi
if ! .venv/bin/python -c 'from importlib.metadata import version; assert version("websockets").split(".", 1)[0] == "16"; from websockets.sync.client import connect' >/dev/null 2>&1; then
  printf '%s\n' 'Installing the voice dependency (first launch needs internet)…'
  .venv/bin/python -m pip install -r requirements.txt
fi

robot_config_path="${DOTS_CONFIG:-robot-config.json}"
if [ ! -e "$robot_config_path" ]; then
  mkdir -p -- "$(dirname "$robot_config_path")"
  cp -- robot-config.default.json "$robot_config_path"
fi
if [ "${1:-}" = "--robot" ]; then
  exec .venv/bin/python scripts/launch_robot.py
fi
exec .venv/bin/python scripts/serve.py
