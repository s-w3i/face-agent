#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
sudo apt-get install -y libportaudio2 pulseaudio-utils python3-venv python3-pil
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -r requirements.txt -r requirements-respeaker.txt
# openWakeWord's tflite-runtime dependency has no Python 3.12 wheel; Kuro runs its ONNX models.
.venv/bin/python -m pip install --no-deps openwakeword==0.6.0
wake_dir="${XDG_DATA_HOME:-$HOME/.local/share}/face-agent/wake"
mkdir -p "$wake_dir"
# Shared feature models only (a name that matches no pre-trained wake model).
.venv/bin/python -c 'import sys, openwakeword.utils as u; u.download_models(model_names=["none"], target_directory=sys.argv[1])' "$wake_dir"
rm -f "$wake_dir"/*.tflite
printf 'ReSpeaker dependencies installed. Whisper downloads its model on first use.\n'
if [ ! -f "$wake_dir/hi_kuro.onnx" ]; then
  printf 'No %s yet; sleeping wake detection uses Whisper until it is trained (docs/wake-word-training.md).\n' "$wake_dir/hi_kuro.onnx"
fi
