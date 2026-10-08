#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
sudo apt-get install -y build-essential cmake libfftw3-dev libconfig-dev libasound2-dev libpulse-dev pulseaudio-utils python3-venv git
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -r requirements-respeaker.txt
odas_dir="$HOME/.cache/face-agent/odas"
mkdir -p "$(dirname "$odas_dir")"
if [ ! -d "$odas_dir/.git" ]; then git clone https://github.com/introlab/odas.git "$odas_dir"; fi
git -C "$odas_dir" checkout bcb845434495e293df3d48f1203b7a86e1852449
cmake -S "$odas_dir" -B "$odas_dir/build"
cmake --build "$odas_dir/build" -j2
