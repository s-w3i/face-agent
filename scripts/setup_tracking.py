#!/usr/bin/env python3
"""Download checksum-verified OpenCV models to this device's cache."""
import hashlib
import os
from pathlib import Path
import tempfile
from urllib.request import urlopen

REVISION = '47534e27c9851bb1128ccc0102f1145e27f23f98'
MODELS = (
    ('face_detection_yunet', 'face_detection_yunet_2023mar.onnx',
     '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4', 232589),
    ('face_recognition_sface', 'face_recognition_sface_2021dec.onnx',
     '0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79', 38696353),
)
MODEL_DIR = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'face-agent/tracking'


def model_paths(directory=MODEL_DIR):
    return [Path(directory) / model[1] for model in MODELS]


def ensure_models(directory=MODEL_DIR):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for folder, name, expected, size in MODELS:
        path = directory / name
        if path.is_file() and path.stat().st_size == size and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
            continue
        print(f'Downloading {name} ({size / 1024 / 1024:.1f} MiB)…', flush=True)
        descriptor, temporary = tempfile.mkstemp(prefix=name + '.', dir=directory)
        try:
            digest = hashlib.sha256(); total = 0
            url = f'https://media.githubusercontent.com/media/opencv/opencv_zoo/{REVISION}/models/{folder}/{name}'
            with os.fdopen(descriptor, 'wb') as output, urlopen(url, timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > size:
                        raise ValueError(f'Unexpected size for {name}.')
                    output.write(chunk); digest.update(chunk)
                output.flush(); os.fsync(output.fileno())
            if total != size or digest.hexdigest() != expected:
                raise ValueError(f'Checksum failed for {name}; the previous model was preserved.')
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return model_paths(directory)


if __name__ == '__main__':
    ensure_models()
