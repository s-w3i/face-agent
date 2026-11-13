"""Check the AEC doctor's measurement and comparison without audio hardware."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import respeaker_aec as aec


def tone(level, frames, seed):
    return (np.random.default_rng(seed).standard_normal(frames) * level).astype(np.int16)


class FakeSoundDevice:
    """Quiet floor first, then playback where the raw mics hear the echo and ch0 mostly does not."""

    def __init__(self, ch0_level, channels=6):
        self.ch0_level = ch0_level; self.channels = channels; self.calls = 0

    def query_devices(self, device, kind):
        return dict(max_input_channels=self.channels)

    def rec(self, frames, samplerate, channels, dtype, device):
        self.calls += 1
        data = np.zeros((frames, channels), np.int16)
        if self.calls == 1:
            for c in range(channels):
                data[:, c] = tone(20, frames, c)
        else:
            data[:, 0] = tone(self.ch0_level, frames, 10)
            for c in range(1, channels):
                data[:, c] = tone(3000, frames, 10 + c)
        return data

    def wait(self):
        pass


class AecDoctorTest(unittest.TestCase):
    def run_measure(self, directory, ch0_level, label):
        with patch.object(aec, 'OUTPUT', Path(directory)), \
                patch('respeaker_aec.resolve_sink', side_effect=lambda name: f'alsa_output.{name}'), \
                patch('respeaker_aec.subprocess.Popen', return_value=Mock()) as popen, \
                patch('respeaker_aec.time.sleep'):
            result = aec.measure(FakeSoundDevice(ch0_level), 2, 16000, sink=label, seconds=1,
                                 quiet_seconds=.5, clip=Path(directory) / 'clip.wav', label=label)
        popen.return_value.terminate.assert_called_once()
        return result

    def test_processed_channel_far_below_raw_mics_means_echo_is_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_measure(directory, 100, 'respeaker')
            self.assertGreater(result['echo_suppression_db'], 25)
            self.assertGreater(result['raw_echo_above_floor_db'], 40)
            self.assertTrue(result['aec_reference_present'])
            self.assertEqual(len(result['playback_db']), 6)
            saved = json.loads(Path(result['wav']).with_suffix('.json').read_text())
            self.assertEqual(saved['label'], 'respeaker')
            self.assertTrue(Path(result['wav']).exists())

    def test_compare_reports_channel_zero_reduction_between_sinks(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_measure(directory, 2500, 'jieli')
            self.run_measure(directory, 250, 'respeaker')
            result = aec.compare(directory)
            self.assertEqual(set(result['runs']), {'jieli', 'respeaker'})
            self.assertAlmostEqual(result['ch0_reduction_db'], 20, delta=1)
            self.assertTrue(result['target_met'])

    def test_compare_without_respeaker_run_has_no_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_measure(directory, 2500, 'jieli')
            self.assertNotIn('target_met', aec.compare(directory))

    def test_single_channel_firmware_reports_only_channel_zero(self):
        quiet = np.full((100, 1), 10, np.int16); loud = np.full((100, 1), 1000, np.int16)
        result = aec.analyse(quiet, loud, 1)
        self.assertNotIn('echo_suppression_db', result)
        self.assertAlmostEqual(result['ch0_echo_above_floor_db'], 40, delta=.2)

    def test_sink_alias_must_match_exactly_one_sink(self):
        names = ['alsa_output.usb-Jieli_x.analog-stereo', 'alsa_output.usb-SEEED_ReSpeaker_y.analog-stereo']
        with patch('respeaker_aec.sinks', return_value=names):
            self.assertEqual(aec.resolve_sink('respeaker'), names[1])
            self.assertEqual(aec.resolve_sink('jieli'), names[0])
            with self.assertRaises(RuntimeError):
                aec.resolve_sink('bluetooth')


if __name__ == '__main__':
    unittest.main()
