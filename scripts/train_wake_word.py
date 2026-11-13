#!/usr/bin/env python3
"""Train the "Hi Kuro" openWakeWord model on a Linux PC with an NVIDIA GPU (not on the Pi).

    python3 scripts/train_wake_word.py setup     # venv and pinned upstream code
    python3 scripts/train_wake_word.py data      # about 21 GB of public audio and features
    python3 scripts/train_wake_word.py preview   # listen to how the TTS says the phrases
    python3 scripts/train_wake_word.py train     # writes output/hi_kuro.onnx and hi_kuro.json

See docs/wake-word-training.md. Every step can be re-run and continues where it stopped.
This file only needs the standard library; the `_` commands run inside the training venv.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORK = Path.home() / 'kuro-wake-training'
# Spelled for the TTS: espeak reads "kuro" as "kyoo-roh" and "kuuro" as "koo-roh".
DEFAULT_PHRASES = ['hi kuuro', 'hey kuuro']
MIN_FREE_GB = 45
MAX_WORK_PATH = 90  # The bundled espeak-ng truncates its data path at 160 characters.

# upstream's newest commit (2025-12-30) at the time this was written.
OWW_REPO = 'https://github.com/dscripka/openWakeWord'
OWW_COMMIT = '368c03716d1e'
# The last commit whose generate_samples() still matches what openWakeWord's train.py calls.
PIPER_REPO = 'https://github.com/rhasspy/piper-sample-generator'
PIPER_COMMIT = '213d4d561ab8'
PIPER_MODEL = PIPER_REPO + '/releases/download/v2.0.0/en_US-libritts_r-medium.pt'

HF = 'https://huggingface.co/datasets/'
FEATURES = HF + 'davidscripka/openwakeword_features/resolve/main/'
NEGATIVE_FEATURES = 'openwakeword_features_ACAV100M_2000_hrs_16bit.npy'
VALIDATION_FEATURES = 'validation_set_features.npy'
RIR_LISTING = 'https://huggingface.co/api/datasets/davidscripka/MIT_environmental_impulse_responses/tree/main/16khz'
RIR_BASE = HF + 'davidscripka/MIT_environmental_impulse_responses/resolve/main/'
AUDIOSET_BASE = HF + 'agkphysics/AudioSet/resolve/main/data/bal_train/'

# Words missing from CMUdict. train.py would fetch a phonemizer model for them, but its
# download link is dead (HTTP 403), so their pronunciations are supplied here instead.
PRONUNCIATIONS = {'kuro': 'K UW1 R OW0', 'kuuro': 'K UW1 R OW0'}


def run(*command, **kwargs):
    print('+', ' '.join(str(part) for part in command), flush=True)
    subprocess.run([str(part) for part in command], check=True, **kwargs)


def fail(message):
    sys.exit('error: ' + message)


def venv_python(work):
    return work / 'venv' / 'bin' / 'python'


def download(url, path, attempts=8):
    """Download to `path`, resuming after a dropped connection; a finished file is left alone."""
    path = Path(path)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + '.part')
    for attempt in range(1, attempts + 1):
        have = part.stat().st_size if part.exists() else 0
        request = urllib.request.Request(url, headers={'Range': f'bytes={have}-'} if have else {})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if have and response.status != 206:
                    have = 0  # The server ignored the range, so start again.
                total = have + int(response.headers.get('Content-Length', 0))
                print(f'{path.name}: {total / 1e9:.2f} GB, have {have / 1e9:.2f} GB', flush=True)
                with open(part, 'ab' if have else 'wb') as file:
                    shutil.copyfileobj(response, file, 1024 * 1024)
            if total and part.stat().st_size != total:
                raise OSError(f'got {part.stat().st_size} of {total} bytes')
            part.replace(path)
            return path
        except OSError as error:
            if getattr(error, 'code', None) == 416:  # The .part file already holds everything.
                part.replace(path)
                return path
            if attempt == attempts:
                raise
            print(f'{path.name}: {error}; retrying (attempt {attempt}/{attempts})', flush=True)
            time.sleep(min(60, 5 * attempt))
    return path


def find_python310(work):
    found = shutil.which('python3.10')
    if found:
        return [found, '-m', 'venv']
    uv = shutil.which('uv')
    if uv:
        return [uv, 'venv', '--seed', '--python', '3.10']
    fail('Python 3.10 is required. Install python3.10 and its venv package, or install uv '
         '(https://docs.astral.sh/uv/), then run setup again.')


def clone(repo, commit, target):
    if not (target / '.git').exists():
        run('git', 'clone', repo, target)
    run('git', '-C', target, 'checkout', '--quiet', commit)


def cmd_setup(args):
    work = args.work
    if platform.system() != 'Linux':
        fail('training only runs on Linux (on Windows, run this inside WSL2).')
    if platform.machine() != 'x86_64':
        print(f'warning: {platform.machine()} is not x86_64; the pinned GPU build of torch is x86_64 only.')
    if not shutil.which('nvidia-smi') and not args.allow_cpu:
        fail('nvidia-smi was not found; install the NVIDIA driver first (on WSL2, the Windows driver).')
    if shutil.which('nvidia-smi'):
        run('nvidia-smi', '--query-gpu=name,driver_version,memory.total', '--format=csv,noheader')
    work.mkdir(parents=True, exist_ok=True)
    if not venv_python(work).exists():
        run(*find_python310(work), work / 'venv')
    python = venv_python(work)
    run(python, '-m', 'pip', 'install', '--quiet', '--upgrade', 'pip')
    run(python, '-m', 'pip', 'install', '-r', args.requirements)
    clone(OWW_REPO, OWW_COMMIT, work / 'openwakeword-src')
    run(python, '-m', 'pip', 'install', '--no-deps', '-e', work / 'openwakeword-src')
    clone(PIPER_REPO, PIPER_COMMIT, work / 'piper-sample-generator')
    download(PIPER_MODEL, work / 'piper-sample-generator' / 'models' / 'en_US-libritts_r-medium.pt')
    # The melspectrogram and embedding models, from the same release the Pi downloads.
    run(python, '-c', 'import openwakeword.utils as u; u.download_models(model_names=["none"])', cwd=work)
    run(python, str(Path(__file__).resolve()), '_check-gpu', *(['--allow-cpu'] if args.allow_cpu else []))
    print(f'\nSetup finished in {work}. Next: data')


def cmd_data(args):
    work = args.work
    work.mkdir(parents=True, exist_ok=True)
    if not (work / NEGATIVE_FEATURES).exists():
        free = shutil.disk_usage(work).free / 1e9
        if free < MIN_FREE_GB:
            fail(f'{work} has {free:.0f} GB free; the data and generated clips need about {MIN_FREE_GB} GB.')
    with urllib.request.urlopen(RIR_LISTING, timeout=60) as response:
        rirs = [item['path'] for item in json.load(response) if item['path'].endswith('.wav')]
    for name in rirs:
        download(RIR_BASE + name, work / 'mit_rirs' / Path(name).name)
    print(f'{len(rirs)} room impulse responses ready')
    run(venv_python(work), str(Path(__file__).resolve()), '_audioset', '--work', work, '--parts', args.audioset_parts)
    download(FEATURES + VALIDATION_FEATURES, work / VALIDATION_FEATURES)
    download(FEATURES + NEGATIVE_FEATURES, work / NEGATIVE_FEATURES)
    print('\nData ready. Next: preview, then train')


def inner_audioset(args):
    """AudioSet parquet parts -> 16 kHz mono WAVs for background noise. The parquet is kept until done."""
    import io
    import numpy as np
    import pyarrow.parquet as pq
    from scipy.io import wavfile
    from scipy.signal import resample_poly
    import soundfile

    out = args.work / 'audioset_16k'
    out.mkdir(exist_ok=True)
    for part in range(args.parts):
        marker = out / f'.part{part:02d}.done'
        if marker.exists():
            continue
        name = f'{part:02d}.parquet'
        path = download(AUDIOSET_BASE + name, args.work / 'audioset_parquet' / name)
        written = 0
        for batch in pq.ParquetFile(path).iter_batches(batch_size=16, columns=['video_id', 'audio']):
            for stem, audio in zip(batch.column('video_id').to_pylist(), batch.column('audio').to_pylist()):
                try:
                    pcm, rate = soundfile.read(io.BytesIO(audio['bytes']), dtype='float32', always_2d=True)
                except (RuntimeError, TypeError):
                    continue  # A few clips in the public dump do not decode.
                pcm = pcm.mean(axis=1)
                if rate != 16000:
                    pcm = resample_poly(pcm, 16000, rate)
                wavfile.write(out / f'{stem}.wav', 16000, (np.clip(pcm, -1, 1) * 32767).astype(np.int16))
                written += 1
        if not written:
            fail(f'no audio could be decoded from {name}')
        print(f'{name}: {written} clips', flush=True)
        marker.touch()
        path.unlink()


def inner_check_gpu(args):
    import torch
    if torch.cuda.is_available():
        print(f'torch {torch.__version__} sees {torch.cuda.get_device_name(0)}')
    elif args.allow_cpu:
        print('warning: no usable GPU; running on the CPU.')
    else:
        fail('torch cannot use the GPU. Check that `nvidia-smi` works and re-run setup.')


def add_pronunciations():
    """Teach `pronouncing` the extra words before train.py builds its adversarial phrases."""
    import pronouncing
    pronouncing.init_cmu()
    for word, phones in PRONUNCIATIONS.items():
        if word not in pronouncing.lookup:
            pronouncing.pronunciations.append((word, phones))
            pronouncing.lookup[word].append(phones)
    return pronouncing


def inner_upstream(args):
    """Run openWakeWord's train.py with the pronunciations added and one upstream bug fixed."""
    add_pronunciations()
    path = args.work / 'openwakeword-src' / 'openwakeword' / 'train.py'
    source = path.read_text()
    # The flag defaults to the string "False", which is truthy, so train.py would always
    # attempt the TensorFlow conversion and crash after the ONNX export.
    broken = 'if args.convert_to_tflite:'
    if broken not in source:
        fail(f'{path} changed: expected to find "{broken}".')
    sys.argv = ['train.py'] + args.rest
    exec(compile(source.replace(broken, 'if args.convert_to_tflite is True:'), str(path), 'exec'),
         {'__name__': '__main__', '__file__': str(path)})


def inner_preview(args):
    sys.path.insert(0, str(args.work / 'piper-sample-generator'))
    from generate_samples import generate_samples
    for phrase in args.phrase:
        out = args.work / 'preview' / phrase.replace(' ', '_')
        out.mkdir(parents=True, exist_ok=True)
        generate_samples(text=[phrase], max_samples=args.count, batch_size=min(args.count, 8),
                         noise_scales=[0.98], noise_scale_ws=[0.98], length_scales=[0.75, 1.0, 1.25],
                         output_dir=str(out), auto_reduce_batch_size=True)
        print(f'{phrase!r}: {out}')


def inner_check_words(args):
    pronouncing = add_pronunciations()
    missing = sorted({word for phrase in args.phrase for word in phrase.lower().split()
                      if not pronouncing.phones_for_word(word)})
    if missing:
        fail(f'no pronunciation for {missing}. Add them to PRONUNCIATIONS in scripts/train_wake_word.py '
             '(CMUdict phones, such as "K UW1 R OW0").')


def inner_check_model(args):
    """The export must take the feature shape the Pi feeds it and return one score."""
    import numpy as np
    import onnxruntime
    session = onnxruntime.InferenceSession(str(args.model), providers=['CPUExecutionProvider'])
    shape = session.get_inputs()[0].shape
    if len(shape) != 3 or shape[2] != 96:
        fail(f'{args.model} has an unexpected input shape {shape}')
    score = session.run(None, {session.get_inputs()[0].name: np.zeros((1, shape[1], 96), np.float32)})[0]
    print(f'{args.model.name}: input {shape}, score on silence-like zeros {float(score.squeeze()):.3f}')


def training_config(args):
    work, name = args.work, args.model_name
    return dict(
        model_name=name, target_phrase=args.phrase, custom_negative_phrases=[],
        n_samples=args.samples, n_samples_val=args.samples_val,
        tts_batch_size=args.tts_batch_size, augmentation_batch_size=16,
        piper_sample_generator_path=str(work / 'piper-sample-generator'),
        output_dir=str(work / 'runs'),
        rir_paths=[str(work / 'mit_rirs')],
        background_paths=[str(work / 'audioset_16k')], background_paths_duplication_rate=[1],
        false_positive_validation_data_path=str(work / VALIDATION_FEATURES),
        augmentation_rounds=1,
        feature_data_files={'ACAV100M_sample': str(work / NEGATIVE_FEATURES)},
        batch_n_per_class={'ACAV100M_sample': 1024, 'adversarial_negative': 50, 'positive': 50},
        model_type='dnn', layer_size=32, steps=args.steps,
        max_negative_weight=args.max_negative_weight, target_false_positives_per_hour=0.2)


def cmd_preview(args):
    script = str(Path(__file__).resolve())
    run(venv_python(args.work), script, '_preview', '--work', args.work, '--count', args.count,
        *[part for phrase in args.phrase for part in ('--phrase', phrase)])
    print('\nListen to the WAV files above. To train another pronunciation, pass your own --phrase to preview and train.')


def cmd_train(args):
    work, name = args.work, args.model_name
    python, script = venv_python(work), str(Path(__file__).resolve())
    if not python.exists():
        fail('run setup first.')
    for needed in (work / NEGATIVE_FEATURES, work / VALIDATION_FEATURES, work / 'mit_rirs', work / 'audioset_16k'):
        if not needed.exists():
            fail(f'{needed} is missing; run data first.')
    run(python, script, '_check-gpu', *(['--allow-cpu'] if args.allow_cpu else []))
    run(python, script, '_check-words', *[part for phrase in args.phrase for part in ('--phrase', phrase)])

    run_dir = work / 'runs' / name
    state = work / 'runs' / f'{name}.phrases.json'
    if args.fresh and run_dir.exists():
        shutil.rmtree(run_dir)
        state.unlink(missing_ok=True)
    if state.exists() and json.loads(state.read_text()) != args.phrase:
        fail(f'{run_dir} holds clips for {json.loads(state.read_text())}, not {args.phrase}; '
             'add --fresh to delete them and generate new ones.')
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(args.phrase))
    config = work / f'{name}.yaml'
    config.write_text(json.dumps(training_config(args), indent=2))  # JSON is valid YAML.

    upstream = [python, script, '_upstream', '--work', work, '--', '--training_config', config]
    env = dict(os.environ, PYTHONUNBUFFERED='1')
    run(*upstream, '--generate_clips', cwd=work, env=env)
    run(*upstream, '--augment_clips', '--overwrite', cwd=work, env=env)
    run(*upstream, '--train_model', cwd=work, env=env)

    exported = work / 'runs' / f'{name}.onnx'
    if not exported.exists():
        fail(f'training finished without writing {exported}.')
    out = work / 'output'
    out.mkdir(exist_ok=True)
    shutil.copy2(exported, out / exported.name)
    meta = dict(phrase=args.phrase[0], name=args.robot_name, threshold=0.5)
    (out / f'{name}.json').write_text(json.dumps(meta) + '\n')
    run(python, script, '_check-model', '--model', out / exported.name)
    print(f'\nDone. Copy to the Pi:\n  scp {out}/{name}.onnx {out}/{name}.json '
          '<pi>:~/.local/share/face-agent/wake/')


def parser():
    top = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = top.add_subparsers(dest='command', required=True)

    def command(name, handler, work=True):
        item = sub.add_parser(name)
        item.set_defaults(handler=handler)
        if work:
            item.add_argument('--work', type=Path, default=DEFAULT_WORK,
                              help=f'folder for the venv, data and results (default {DEFAULT_WORK})')
        return item

    def phrases(item):
        item.add_argument('--phrase', action='append', help=f'target phrase, repeatable (default {DEFAULT_PHRASES})')

    item = command('setup', cmd_setup)
    item.add_argument('--requirements', type=Path, default=ROOT / 'requirements-wake-training.txt')
    item.add_argument('--allow-cpu', action='store_true', help='continue without a GPU (for testing only)')

    item = command('data', cmd_data)
    item.add_argument('--audioset-parts', type=int, default=4, help='AudioSet parts for background noise, about 0.7 GB each')

    item = command('preview', cmd_preview)
    phrases(item)
    item.add_argument('--count', type=int, default=6, help='clips per phrase')

    item = command('train', cmd_train)
    phrases(item)
    item.add_argument('--model-name', default='hi_kuro', help='file name of the model')
    item.add_argument('--robot-name', default='Kuro', help='must match the character name in the studio')
    item.add_argument('--samples', type=int, default=50000, help='positive and adversarial training clips')
    item.add_argument('--samples-val', type=int, default=5000, help='validation clips')
    item.add_argument('--steps', type=int, default=50000)
    item.add_argument('--max-negative-weight', type=int, default=1500, help='raise this if the model wakes falsely')
    item.add_argument('--tts-batch-size', type=int, default=64)
    item.add_argument('--fresh', action='store_true', help='delete the clips and features of the previous run first')
    item.add_argument('--allow-cpu', action='store_true', help='continue without a GPU (for testing only)')

    item = command('_check-gpu', inner_check_gpu, work=False)
    item.add_argument('--allow-cpu', action='store_true')
    item = command('_audioset', inner_audioset)
    item.add_argument('--parts', type=int, required=True)
    item = command('_preview', inner_preview)
    phrases(item)
    item.add_argument('--count', type=int, required=True)
    item = command('_check-words', inner_check_words, work=False)
    phrases(item)
    item = command('_check-model', inner_check_model, work=False)
    item.add_argument('--model', type=Path, required=True)
    item = command('_upstream', inner_upstream)
    item.add_argument('rest', nargs=argparse.REMAINDER)
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    if hasattr(args, 'phrase'):
        args.phrase = args.phrase or list(DEFAULT_PHRASES)
    if hasattr(args, 'work'):
        args.work = args.work.expanduser().resolve()
        if len(str(args.work)) > MAX_WORK_PATH:
            fail(f'--work path is longer than {MAX_WORK_PATH} characters; espeak-ng cannot find its data under it.')
    if getattr(args, 'rest', None) and args.rest[0] == '--':
        args.rest = args.rest[1:]
    args.handler(args)


if __name__ == '__main__':
    main()
