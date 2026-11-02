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
# Optional Hailo-8L offline STT (Whisper base): HailoRT binding matching the runtime, plus Hailo's 8L
# models. About 310 MB. Without them, offline speech-to-text falls back to CPU faster-whisper.
if command -v hailortcli >/dev/null 2>&1; then
  hailo_version=$(hailortcli --version 2>/dev/null | grep -o '[0-9][0-9.]*' | head -1)
  hailo_wheel=$(ls "${HAILO_WHEEL_DIR:-$HOME/hailo-debs}"/hailort-"$hailo_version"-cp3*-linux_aarch64.whl 2>/dev/null | head -1)
  if [ -n "$hailo_wheel" ]; then
    .venv/bin/python -m pip install "$hailo_wheel"
  fi
  whisper_dir="${XDG_DATA_HOME:-$HOME/.local/share}/face-agent/whisper-hailo8l"
  mkdir -p "$whisper_dir"
  hef_base=https://hailo-csdata.s3.eu-west-2.amazonaws.com/resources/whisper/h8l
  assets="https://hailo-csdata.s3.eu-west-2.amazonaws.com/resources/npy%20files/whisper/decoder_assets/base/decoder_tokenization"
  for url in "$hef_base/base-whisper-encoder-5s_h8l.hef" "$hef_base/base-whisper-decoder-fixed-sequence-matmul-split_h8l.hef" \
      "$assets/onnx_add_input_base.npy" "$assets/token_embedding_weight_base.npy"; do
    wget -q -nc -P "$whisper_dir" "$url" || printf 'Could not download %s; offline STT uses the CPU.\n' "$url" >&2
  done
fi
printf 'ReSpeaker dependencies installed. Whisper downloads its model on first use.\n'
if [ ! -f "$wake_dir/hi_kuro.onnx" ]; then
  printf 'No %s yet; sleeping wake detection uses Whisper until it is trained (docs/wake-word-training.md).\n' "$wake_dir/hi_kuro.onnx"
fi
