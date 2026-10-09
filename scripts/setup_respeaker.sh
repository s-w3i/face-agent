#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
sudo apt-get install -y libportaudio2 pulseaudio-utils python3-venv python3-pil
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -r requirements.txt -r requirements-respeaker.txt
printf 'ReSpeaker dependencies installed. Whisper downloads its model on first use.\n'
