#!/usr/bin/env bash
# What is wrong with Kuro: active faults, live checks and recent history. See --help.
cd "$(dirname "$0")" && exec /usr/bin/python3 scripts/doctor.py "$@"
