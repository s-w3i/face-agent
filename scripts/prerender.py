"""Atomic publication of local WebP animation packs; no credentials in assets."""
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
import uuid


class RenderPacks:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.pending = None

    def begin(self):
        with self.lock:
            if self.pending:
                shutil.rmtree(self.root / self.pending, ignore_errors=True)
            self.pending = uuid.uuid4().hex
            (self.root / self.pending).mkdir(parents=True)
            return self.pending

    def upload(self, identifier, filename, data):
        with self.lock:
            if identifier != self.pending or not re.fullmatch(r'[a-z0-9-]{1,80}-[0-9]{1,3}\.webp', filename):
                raise ValueError('Invalid or expired render upload.')
            if len(data) < 30 or data[:4] != b'RIFF' or data[8:12] != b'WEBP' or data[12:16] != b'VP8X' or data[20] & 2:
                raise ValueError('Upload a still WebP sprite sheet with transparency.')
            width = 1 + int.from_bytes(data[24:27], 'little')
            height = 1 + int.from_bytes(data[27:30], 'little')
            if width != 2048 or height != 2048:
                raise ValueError('Sprite sheets must be 2048 × 2048 pixels.')
            folder = self.root / identifier
            if sum(p.stat().st_size for p in folder.iterdir()) + len(data) > 300_000_000:
                raise ValueError('Animation pack exceeds 300 MB.')
            (folder / filename).write_bytes(data)

    def publish(self, identifier, manifest, validate_config):
        with self.lock:
            if identifier != self.pending or not isinstance(manifest, dict):
                raise ValueError('Invalid or expired render upload.')
            fields = {'version', 'id', 'config', 'size', 'fps', 'columns', 'clips'}
            if not fields <= set(manifest) or set(manifest) - fields - {'gazeVersion'} or manifest.get('version') != 1 or manifest.get('id') != identifier or ('gazeVersion' in manifest and (type(manifest['gazeVersion']) is not int or manifest['gazeVersion'] != 1)):
                raise ValueError('Invalid animation manifest.')
            validate_config(manifest['config'])
            if (manifest.get('size'), manifest.get('fps'), manifest.get('columns')) != (512, 16, 4):
                raise ValueError('Unsupported animation pack resolution or timing.')
            clips = manifest.get('clips')
            if not isinstance(clips, list) or not 30 <= len(clips) <= 40:
                raise ValueError('The pack must contain the full animation library.')
            identifiers = set()
            for clip in clips:
                fields = {'id', 'label', 'count', 'loopStart', 'bounds', 'sheets'}
                if not isinstance(clip, dict) or not fields <= set(clip) or set(clip) - fields - {'eyes'}:
                    raise ValueError('Invalid animation clip.')
                if not isinstance(clip['id'], str) or not re.fullmatch(r'[a-z0-9_:-]{1,100}', clip['id']) or clip['id'] in identifiers or not isinstance(clip['label'], str) or len(clip['label']) > 100:
                    raise ValueError('Invalid or duplicate animation identifier.')
                identifiers.add(clip['id'])
                count, loop = clip['count'], clip['loopStart']
                if type(count) is not int or not 1 <= count <= 160 or (loop is not None and (type(loop) is not int or not 0 <= loop < count)):
                    raise ValueError('Invalid animation length.')
                if 'eyes' in clip: self.validate_eyes(clip['eyes'], count)
                bounds = clip['bounds']
                if not isinstance(bounds, dict) or set(bounds) != {'left', 'top', 'right', 'bottom'} or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in bounds.values()) or bounds['left'] >= bounds['right'] or bounds['top'] >= bounds['bottom']:
                    raise ValueError('Invalid animation bounds.')
                sheets = clip['sheets']
                if not isinstance(sheets, list) or len(sheets) != math.ceil(count / 16):
                    raise ValueError('Invalid sprite sheet count.')
                for filename in sheets:
                    if not isinstance(filename, str) or not re.fullmatch(r'[a-z0-9-]{1,80}-[0-9]{1,3}\.webp', filename) or not (self.root / identifier / filename).is_file():
                        raise ValueError('The pack has missing sprite sheets.')
            required = set('idle thinking sleeping working searching creating needs-input error payment wave look-left look-right look-up look-down press drag artifact-ready payment-paid input-received thinking-resolved listening speaking'.split())
            required.update('signature:' + hero for hero in ('blue_beret', 'alfred', 'purple_heart', 'lime_frog', 'coral_monocle', 'gus', 'blue_spectacles', 'lime_headphones'))
            if not required <= identifiers:
                raise ValueError('The pack is missing required states.')
            if next(c for c in clips if c['id'] == 'speaking')['count'] != 21:
                raise ValueError('Speaking needs 21 audio-driven poses.')
            temporary = self.root / 'manifest.json.tmp'
            temporary.write_text(json.dumps(manifest, allow_nan=False))
            os.replace(temporary, self.root / 'manifest.json')
            self.pending = None

    @staticmethod
    def validate_eyes(eyes, count):
        if not isinstance(eyes, list) or len(eyes) != count:
            raise ValueError('Eye anchors must match the body frame count.')
        for frame in eyes:
            if not isinstance(frame, list) or len(frame) not in (0, 2): raise ValueError('Each supported frame needs two eye bounds.')
            for eye in frame:
                if not isinstance(eye, list) or len(eye) != 4 or any(type(v) is not int or not 0 <= v <= 512 for v in eye) or eye[0] >= eye[2] or eye[1] >= eye[3]:
                    raise ValueError('Invalid eye bounds.')

    def add_gaze(self, identifier, clips):
        with self.lock:
            manifest = json.loads((self.root / 'manifest.json').read_text())
            if manifest['id'] != identifier or not isinstance(clips, dict) or set(clips) != {c['id'] for c in manifest['clips']}:
                raise ValueError('The animation pack changed. Prepare its eye data again.')
            for clip in manifest['clips']:
                self.validate_eyes(clips[clip['id']], clip['count']); clip['eyes'] = clips[clip['id']]
            manifest['gazeVersion'] = 1
            temporary = self.root / 'manifest.json.tmp'
            temporary.write_text(json.dumps(manifest, allow_nan=False, separators=(',', ':')))
            os.replace(temporary, self.root / 'manifest.json')

    def cancel(self, identifier):
        with self.lock:
            if identifier == self.pending:
                shutil.rmtree(self.root / identifier, ignore_errors=True)
                self.pending = None
