"""One transaction check: validated sheets, complete packs, safe cancellation."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from prerender import RenderPacks
from serve import validate_config


class RenderPackCheck(unittest.TestCase):
    def test_publish_and_cancel_preserve_the_last_complete_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RenderPacks(directory)
            identifier = store.begin()
            webp = b'RIFF' + b'\x00' * 4 + b'WEBPVP8X' + b'\x00' * 4 + b'\x10\x00\x00\x00' + (2047).to_bytes(3, 'little') * 2
            with self.assertRaises(ValueError): store.upload(identifier, '../escape-0.webp', webp)
            with self.assertRaises(ValueError): store.upload(identifier, 'idle-0.webp', b'broken')
            store.upload(identifier, 'idle-0.webp', webp)
            config = json.loads((Path(__file__).resolve().parents[1] / 'robot-config.default.json').read_text())
            ids = 'idle thinking sleeping working searching creating needs-input error payment wave look-left look-right look-up look-down press drag artifact-ready payment-paid input-received thinking-resolved listening speaking'.split()
            ids.extend('signature:' + hero for hero in ('blue_beret', 'alfred', 'purple_heart', 'lime_frog', 'coral_monocle', 'gus', 'blue_spectacles', 'lime_headphones'))
            clips = [dict(id=i, label=i, count=21 if i == 'speaking' else 1, loopStart=None,
                bounds=dict(left=.1, top=.1, right=.9, bottom=.9), sheets=['idle-0.webp'] * (2 if i == 'speaking' else 1)) for i in ids]
            manifest = dict(version=1, id=identifier, config=config, size=512, fps=16, columns=4, clips=clips)
            bad = copy.deepcopy(manifest); bad['clips'][0]['sheets'] = ['missing-0.webp']
            with self.assertRaises(ValueError): store.publish(identifier, bad, validate_config)
            bad = copy.deepcopy(manifest); bad['config']['settings']['apiKey'] = 'must never be public'
            with self.assertRaises(ValueError): store.publish(identifier, bad, validate_config)
            self.assertFalse((Path(directory) / 'manifest.json').exists())
            store.publish(identifier, manifest, validate_config)
            self.assertEqual(json.loads((Path(directory) / 'manifest.json').read_text()), manifest)
            anchors = {c['id']: [[] for _ in range(c['count'])] for c in clips}
            anchors['idle'] = [[[100, 100, 120, 140], [140, 100, 160, 140]]]
            bad = copy.deepcopy(anchors); bad['idle'][0][0][0] = -1
            with self.assertRaises(ValueError): store.add_gaze(identifier, bad)
            store.add_gaze(identifier, anchors)
            saved = json.loads((Path(directory) / 'manifest.json').read_text())
            self.assertEqual(saved['clips'][0]['eyes'], anchors['idle'])
            self.assertEqual(saved['gazeVersion'], 1)
            cancelled = store.begin(); store.cancel(cancelled)
            self.assertFalse((Path(directory) / cancelled).exists())
            self.assertTrue((Path(directory) / identifier / 'idle-0.webp').exists())
            self.assertEqual(json.loads((Path(directory) / 'manifest.json').read_text())['id'], identifier)


if __name__ == '__main__': unittest.main()
