"""ReSpeaker XVF3000 DSP settings applied every time the microphone worker starts.

The chip keeps its parameters in RAM only, so they are lost on power loss but can also
linger from a manual `tuning.py` session; writing them at start makes them predictable.

Precedence: PROFILE below < calibration file (scripts/calibrate_mic.py) < environment.
    DOTS_RESPEAKER_PROFILE=off          leave the chip as it is
    DOTS_RESPEAKER_GAMMAVAD_SR=4        any writable parameter, by name
See docs/fan-noise.md.
"""
import json
import os
from pathlib import Path

# Floats are linear factors, as the chip takes them (e.g. GAMMAVAD_SR 3.0 ≈ 9.5 dB).
# Conservative defaults until calibrate_mic.py has measured this robot's fans.
PROFILE = {
    'HPFONOFF': 1,          # 70 Hz high-pass (factory).
    'GAMMAVAD_SR': 6.0,     # Hardware VAD threshold: factory 1.5 fires on the fans; 3 still false-triggered in use; 15 needed shouting.
    'STATNOISEONOFF': 1,    # Stationary noise suppression (factory strength).
    'STATNOISEONOFF_SR': 1,
    'GAMMA_NS': 1.0,
    'GAMMA_NS_SR': 1.0,
    'MIN_NS': 0.15,
    'MIN_NS_SR': 0.15,
}
ENV_PREFIX = 'DOTS_RESPEAKER_'
SKIP_ENV = {ENV_PREFIX + 'PROFILE', ENV_PREFIX + 'CALIBRATION'}


def calibration_path(environ=os.environ):
    if environ.get(ENV_PREFIX + 'CALIBRATION'):
        return Path(environ[ENV_PREFIX + 'CALIBRATION']).expanduser()
    return Path(environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'mimo-dots' / 'respeaker-calibration.json'


def load_calibration(environ=os.environ):
    """The saved calibration ({} if none or unreadable)."""
    try:
        data = json.loads(calibration_path(environ).read_text())
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _checked(parameters, name, value, source):
    spec = parameters.get(name)
    if spec is None or spec[5] != 'rw':
        raise ValueError(f'{source}: {name} is not a writable ReSpeaker parameter.')
    number = float(value)
    if not spec[4] <= number <= spec[3]:
        raise ValueError(f'{source}: {name}={value} is outside {spec[4]}..{spec[3]}.')
    return int(number) if spec[2] == 'int' else number


def resolve(parameters, environ=os.environ, calibrated=None):
    """The profile to write, with calibration and env overrides; {} when disabled."""
    if environ.get(ENV_PREFIX + 'PROFILE', '').strip().lower() in ('off', '0', 'false', 'no'):
        return {}
    profile = dict(PROFILE)
    calibrated = load_calibration(environ) if calibrated is None else calibrated
    for name, value in (calibrated.get('parameters') or {}).items():
        profile[name] = _checked(parameters, name, value, 'calibration')
    for key, value in environ.items():
        if key.startswith(ENV_PREFIX) and key not in SKIP_ENV:
            name = key[len(ENV_PREFIX):].upper()
            profile[name] = _checked(parameters, name, value, key)
    return profile


def apply(dev, parameters, environ=os.environ, calibrated=None):
    """Write the profile and return what the chip reports back, by name."""
    profile = resolve(parameters, environ, calibrated)
    for name, value in profile.items():
        dev.write(name, value)
    return {name: round(dev.read(name), 3) for name in profile}
