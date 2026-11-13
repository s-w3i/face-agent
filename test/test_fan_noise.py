"""Fan-noise handling offline: DSP profile, energy-gated end of speech, STT noise reduction."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import respeaker as r
import respeaker_noise
import respeaker_profile
import calibrate_mic as cal
from respeaker_realtime import session_config
from respeaker_vendor.tuning import PARAMETERS

RATE = 16000


def fan(seconds, level_db=-50, seed=0):
    """Chassis-fan stand-in: hum at the measured 875/1754 Hz tones plus broadband noise."""
    t = np.arange(round(seconds * RATE)) / RATE
    rng = np.random.default_rng(seed)
    signal = np.sin(2 * np.pi * 875 * t) + .5 * np.sin(2 * np.pi * 1754 * t) + rng.standard_normal(t.size)
    signal *= 32768 * 10 ** (level_db / 20) / np.sqrt(np.mean(signal ** 2))
    return signal


def speech(seconds, level_db=-24):
    t = np.arange(round(seconds * RATE)) / RATE
    signal = np.sin(2 * np.pi * 220 * t) * (1 + np.sin(2 * np.pi * 4 * t))  # Syllable-rate envelope.
    return signal * 32768 * 10 ** (level_db / 20) / np.sqrt(np.mean(signal ** 2))


def pcm(samples):
    return np.clip(samples, -32768, 32767).astype('<i2').tobytes()


def frames(data):
    size = r.FRAME_SAMPLES * 2
    return [data[i:i + size] for i in range(0, len(data) - size + 1, size)]


class FakeDev:
    def __init__(self):
        self.values = {}

    def write(self, name, value):
        self.values[name] = value

    def read(self, name):
        return self.values.get(name, 0)


class EnergyGateTest(unittest.TestCase):
    def test_fan_hum_that_passes_webrtc_vad_counts_as_silence(self):
        floor = r.noise_floor(pcm(fan(4)))
        gate = r.SpeechGate(Mock(is_speech=lambda data, rate: True), floor)
        self.assertFalse(any(gate.is_speech(f) for f in frames(pcm(fan(1, seed=1)))))
        voiced = [gate.is_speech(f) for f in frames(pcm(fan(1, seed=2) + speech(1)))]
        self.assertGreater(np.mean(voiced), .8)  # Only the envelope's troughs between syllables drop out.

    def test_utterance_ends_on_silence_instead_of_running_to_the_maximum(self):
        floor = r.noise_floor(pcm(fan(4)))
        gate = r.SpeechGate(Mock(is_speech=lambda data, rate: True), floor)
        endpoint = r.SpeechEndpoint(.8)
        audio = pcm(np.concatenate([fan(.5) + speech(.5), fan(3, seed=3)]))
        ended = next((i for i, f in enumerate(frames(audio)) if endpoint.update(gate.is_speech(f), r.FRAME_SAMPLES)), None)
        self.assertIsNotNone(ended)
        self.assertAlmostEqual(ended * r.FRAME_SAMPLES / RATE, 1.3, delta=.1)  # 0.5 s speech + 0.8 s silence.

    def test_webrtc_vad_still_decides(self):
        gate = r.SpeechGate(Mock(is_speech=lambda data, rate: False), r.noise_floor(pcm(fan(1))))
        self.assertFalse(gate.is_speech(frames(pcm(speech(.1)))[0]))

    def test_gate_disabled_or_without_floor_is_plain_webrtc_vad(self):
        quiet = frames(pcm(np.zeros(RATE // 10)))[0]
        self.assertTrue(r.SpeechGate(Mock(is_speech=lambda data, rate: True), 100.0, gate_db=0).is_speech(quiet))
        self.assertTrue(r.SpeechGate(Mock(is_speech=lambda data, rate: True), None).is_speech(quiet))

    def test_floor_is_clamped_so_a_ring_full_of_speech_cannot_mute_the_user(self):
        high = 32768 * 10 ** (r.NOISE_FLOOR_LIMITS_DB[1] / 20)
        self.assertAlmostEqual(r.noise_floor(pcm(speech(4, level_db=-10))), high, places=3)
        self.assertGreater(r.noise_floor(pcm(np.zeros(RATE))), 0)
        self.assertIsNone(r.noise_floor(b''))


class ProfileTest(unittest.TestCase):
    def test_applies_the_profile_and_reports_the_chip_values(self):
        dev = FakeDev()
        applied = respeaker_profile.apply(dev, PARAMETERS, environ={}, calibrated={})
        self.assertEqual(dev.values, respeaker_profile.PROFILE)
        self.assertEqual(applied['GAMMAVAD_SR'], respeaker_profile.PROFILE['GAMMAVAD_SR'])

    def test_calibration_file_overrides_defaults_and_env_overrides_calibration(self):
        calibrated = {'parameters': {'GAMMAVAD_SR': 5.0, 'HPFONOFF': 2}}
        profile = respeaker_profile.resolve(PARAMETERS, {}, calibrated)
        self.assertEqual((profile['GAMMAVAD_SR'], profile['HPFONOFF']), (5.0, 2))
        profile = respeaker_profile.resolve(PARAMETERS, {'DOTS_RESPEAKER_GAMMAVAD_SR': '7'}, calibrated)
        self.assertEqual(profile['GAMMAVAD_SR'], 7.0)
        with self.assertRaises(ValueError):
            respeaker_profile.resolve(PARAMETERS, {}, {'parameters': {'VOICEACTIVITY': 1}})

    def test_calibration_file_round_trip_and_missing_file(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as directory:
            environ = {'DOTS_RESPEAKER_CALIBRATION': f'{directory}/cal.json'}
            self.assertEqual(respeaker_profile.load_calibration(environ), {})
            Path(directory, 'cal.json').write_text(json.dumps({'parameters': {'GAMMAVAD_SR': 4}}))
            self.assertEqual(respeaker_profile.resolve(PARAMETERS, environ)['GAMMAVAD_SR'], 4.0)
            Path(directory, 'cal.json').write_text('not json')
            self.assertEqual(respeaker_profile.load_calibration(environ), {})

    def test_env_overrides_one_parameter_or_turns_the_profile_off(self):
        profile = respeaker_profile.resolve(PARAMETERS, {'DOTS_RESPEAKER_GAMMAVAD_SR': '4', 'DOTS_RESPEAKER_hpfonoff': '3'})
        self.assertEqual(profile['GAMMAVAD_SR'], 4.0)
        self.assertEqual(profile['HPFONOFF'], 3)
        self.assertEqual(respeaker_profile.resolve(PARAMETERS, {'DOTS_RESPEAKER_PROFILE': 'off'}), {})
        dev = FakeDev()
        self.assertEqual(respeaker_profile.apply(dev, PARAMETERS, {'DOTS_RESPEAKER_PROFILE': 'off'}), {})
        self.assertEqual(dev.values, {})

    def test_rejects_unknown_read_only_and_out_of_range_overrides(self):
        for environ in ({'DOTS_RESPEAKER_NOPE': '1'}, {'DOTS_RESPEAKER_VOICEACTIVITY': '1'},
                        {'DOTS_RESPEAKER_HPFONOFF': '9'}):
            with self.assertRaises(ValueError):
                respeaker_profile.resolve(PARAMETERS, environ)

    def test_profile_values_are_within_the_chip_ranges(self):
        for name, value in respeaker_profile.PROFILE.items():
            spec = PARAMETERS[name]
            self.assertEqual(spec[5], 'rw', name)
            self.assertTrue(spec[4] <= value <= spec[3], name)


class RealtimeConfigTest(unittest.TestCase):
    def test_noise_reduction_is_off_by_default_and_opt_in(self):
        import respeaker_realtime as rt
        self.assertIsNone(rt.noise_reduction({}))
        self.assertEqual(rt.noise_reduction({'DOTS_STT_NOISE_REDUCTION': 'near_field'}), 'near_field')
        self.assertNotIn('noise_reduction', session_config(reduction=False)['session']['audio']['input'])
        audio = session_config('gpt-live-transcribe', 'en', 'far_field')['session']['audio']['input']
        self.assertEqual(audio['noise_reduction'], {'type': 'far_field'})
        with self.assertRaises(RuntimeError):
            rt.noise_reduction({'DOTS_STT_NOISE_REDUCTION': 'loud'})

    def test_high_delay_by_default_and_configurable(self):
        import respeaker_realtime as rt
        self.assertEqual(rt.stt_delay({}), 'high')
        self.assertEqual(rt.stt_delay({'DOTS_STT_DELAY': 'medium'}), 'medium')
        self.assertEqual(session_config(delay='low')['session']['audio']['input']['transcription']['delay'], 'low')
        with self.assertRaises(RuntimeError):
            rt.stt_delay({'DOTS_STT_DELAY': 'slow'})

    def test_hints_carry_the_robot_name_and_command_words(self):
        import respeaker_realtime as rt
        hints = rt.hints_for('Kuro', {})
        self.assertIn('Kuro', hints['prompt'])
        self.assertEqual(hints['keywords'][0], 'Kuro')
        self.assertIn('green bottle', hints['keywords'])
        custom = rt.hints_for('Dots', {'DOTS_STT_KEYWORDS': 'Dots, red <cup>\n, ,Dots'})
        self.assertEqual(custom['keywords'], ['Dots', 'red  cup'])
        transcription = session_config(hints=hints)['session']['audio']['input']['transcription']
        self.assertEqual((transcription['prompt'], transcription['keywords']), (hints['prompt'], hints['keywords']))
        self.assertNotIn('prompt', session_config()['session']['audio']['input']['transcription'])


class NoiseDoctorTest(unittest.TestCase):
    def test_parse_settings(self):
        self.assertEqual(respeaker_noise.parse_settings(['hpfonoff=2', 'GAMMAVAD_SR=3'], PARAMETERS),
                         {'HPFONOFF': 2.0, 'GAMMAVAD_SR': 3.0})
        for bad in (['NOPE=1'], ['VOICEACTIVITY=1'], ['HPFONOFF=9'], ['HPFONOFF']):
            with self.assertRaises(ValueError):
                respeaker_noise.parse_settings(bad, PARAMETERS)

    def test_analyse_finds_the_fan_tone_and_splits_vad_hits(self):
        mixed = np.concatenate([fan(2), fan(1, seed=4) + speech(1), fan(2, seed=5)])
        audio = np.stack([mixed] + [fan(5, level_db=-44, seed=6 + c) for c in range(5)], axis=1).astype('<i2')
        loud = lambda t: 2 <= t < 3
        flags = [(t, {'VOICEACTIVITY': int(loud(t)), 'SPEECHDETECTED': 0}) for t in np.arange(0, 5, .02)]
        result = respeaker_noise.analyse(audio, flags, RATE, started=0.0)
        self.assertTrue(any(abs(peak['hz'] - 875) < 10 for peak in result['raw_tonal_peaks']))
        self.assertGreater(result['voiceactivity_in_speech_percent'], 90)
        self.assertLess(result['voiceactivity_in_noise_percent'], 10)


class CaptureCalibrationTest(unittest.TestCase):
    def test_calibrated_floor_caps_a_ring_full_of_robot_playback(self):
        playback = pcm(speech(4, level_db=-20))
        self.assertAlmostEqual(20 * np.log10(r.capture_floor(playback, -53) / 32768), -50, delta=.1)
        self.assertGreater(r.capture_floor(playback, None), r.capture_floor(playback, -53))
        quiet = pcm(fan(4, level_db=-60))
        self.assertEqual(r.capture_floor(quiet, -53), r.noise_floor(quiet))

    def test_apply_calibration_fills_only_unset_values(self):
        from types import SimpleNamespace
        args = SimpleNamespace(energy_gate_db=None, preroll_seconds=r.PREROLL_SECONDS)
        r.apply_calibration(args, {'energy_gate_db': 0.0, 'noise_floor_db': -53.0, 'preroll_seconds': .6}, environ={})
        self.assertEqual((args.energy_gate_db, args.noise_floor_db, args.preroll_seconds), (0.0, -53.0, .6))
        args = SimpleNamespace(energy_gate_db=None, preroll_seconds=.1)
        r.apply_calibration(args, {'preroll_seconds': .6}, environ={'DOTS_ENERGY_GATE_DB': '5'})
        self.assertEqual((args.energy_gate_db, args.preroll_seconds), (5.0, .1))
        args = SimpleNamespace(energy_gate_db=None, preroll_seconds=r.PREROLL_SECONDS)
        r.apply_calibration(args, {}, environ={})
        self.assertEqual(args.energy_gate_db, 0.0)  # Uncalibrated: no guessed floor.
        args = SimpleNamespace(energy_gate_db=None, preroll_seconds=r.PREROLL_SECONDS)
        r.apply_calibration(args, {'noise_floor_db': -53.0}, environ={})
        self.assertEqual(args.energy_gate_db, r.ENERGY_GATE_DB)


def round_result(name, vad_on_quiet=(), sentence_hits=(True,) * 5, settings=None):
    """A synthetic calibration round: 10 s fan, then 5 sentences of 4 s each."""
    quiet_end, windows, flags = 10.0, [], []
    audio = [fan(10)]
    for index, sentence in enumerate(cal.SENTENCES):
        start = quiet_end + 4 * index
        audio.append(np.concatenate([fan(.5, seed=index), fan(1.5, seed=index + 9) + speech(1.5), fan(2, seed=index + 20)]))
        windows.append(dict(sentence=sentence, start=start, end=start + 4))
        if sentence_hits[index]:
            flags += [(start + .6 + k * .02, 1) for k in range(20)]
    flags += [(t, 1) for t in vad_on_quiet]
    flags = sorted(flags + [(t, 0) for t in np.arange(0, quiet_end + 20, .02)])
    mono = np.concatenate(audio)
    return dict(name=name, settings=settings or {'GAMMAVAD_SR': 5.0}, quiet=(0.0, quiet_end), windows=windows,
                audio=np.clip(mono, -32768, 32767).astype('<i2')[:, None], flags=flags, channels=1)


class CalibrationAnalysisTest(unittest.TestCase):
    def test_triggers_merge_hits_within_a_second(self):
        flags = [(1.0, 1), (1.02, 1), (1.5, 1), (3.0, 1), (5.0, 0)]
        self.assertEqual(cal.triggers(flags, 0, 10), [1.0, 3.0])
        self.assertEqual(cal.triggers(flags, 2, 10), [3.0])

    def test_word_error_rate(self):
        self.assertEqual(cal.wer('Pick up the soda can.', 'pick up the soda can'), 0)
        self.assertAlmostEqual(cal.wer('Pick up the soda can.', 'pick up so that I can'), 3 / 5)
        self.assertEqual(cal.wer('Can you hear me?', ''), 1)

    def test_round_analysis_counts_false_triggers_caught_sentences_and_accuracy(self):
        result = round_result('a', vad_on_quiet=(3.0, 3.02, 7.0), sentence_hits=(True, True, False, True, True))
        analysis = cal.analyse_round(result, transcribe=lambda pcm: 'pick up the soda can')
        self.assertEqual(analysis['false_trigger_times'], [3.0, 7.0])
        self.assertEqual((analysis['spoken'], analysis['triggered']), (5, 4))
        self.assertAlmostEqual(analysis['onset_latency_max'], .1, delta=.05)
        self.assertEqual(analysis['sentences'][0]['wer'], 0)
        self.assertEqual(analysis['premature_gated'], 0)

    def test_choose_prefers_no_false_triggers_then_coverage_then_accuracy(self):
        rows = [dict(name='noisy', false_triggers_per_min=2.0, spoken=5, triggered=5, wer=0.0, onset_latency_max=.1),
                dict(name='deaf', false_triggers_per_min=0.0, spoken=5, triggered=3, wer=0.0, onset_latency_max=.1),
                dict(name='good', false_triggers_per_min=0.0, spoken=5, triggered=5, wer=.2, onset_latency_max=.3),
                dict(name='best', false_triggers_per_min=0.0, spoken=5, triggered=5, wer=.1, onset_latency_max=.3),
                dict(name='silent', false_triggers_per_min=0.0, spoken=2, triggered=2, wer=0.0, onset_latency_max=.1)]
        self.assertEqual(cal.choose(rows)['name'], 'best')
        self.assertIsNone(cal.choose([rows[-1]]))

    def test_calibration_for_sets_preroll_from_latency_and_keeps_gate_when_it_cuts_nothing(self):
        rounds = [cal.analyse_round(round_result('a', settings={'GAMMAVAD_SR': 5.0, 'HPFONOFF': 1}))]
        saved = cal.calibration_for(rounds[0], rounds)
        self.assertEqual(saved['parameters'], {'GAMMAVAD_SR': 5.0, 'HPFONOFF': 1})
        self.assertEqual(saved['energy_gate_db'], cal.GATE_DB)
        self.assertAlmostEqual(saved['preroll_seconds'], .35, delta=.06)
        self.assertLess(saved['noise_floor_db'], -45)
        respeaker_profile.resolve(PARAMETERS, {}, saved)  # Valid for the worker.


class PrerollTest(unittest.TestCase):
    def test_preroll_never_reaches_back_past_reopening(self):
        from types import SimpleNamespace
        args = SimpleNamespace(preroll_seconds=.8)
        self.assertEqual(r.preroll_seconds(args, SimpleNamespace(opened_at=0.0), now=100.0), .8)
        self.assertAlmostEqual(r.preroll_seconds(args, SimpleNamespace(opened_at=99.5), now=100.0), .5)
        self.assertEqual(r.preroll_seconds(args, SimpleNamespace(opened_at=float('inf')), now=100.0), .8)
        self.assertEqual(r.preroll_seconds(SimpleNamespace(), SimpleNamespace(), now=1.0), r.PREROLL_SECONDS)

    def test_calibration_uses_the_lowest_floor_with_the_same_audio_settings(self):
        rows = [dict(name='a5', settings={'HPFONOFF': 1, 'GAMMAVAD_SR': 5.0}, floor_db=-47.4, onset_latency_max=0,
                     false_triggers_per_min=0, spoken=5, triggered=5, wer=0, premature_plain=0, premature_gated=0),
                dict(name='a8', settings={'HPFONOFF': 1, 'GAMMAVAD_SR': 8.0}, floor_db=-43.3, onset_latency_max=.54,
                     false_triggers_per_min=0, spoken=5, triggered=5, wer=0, premature_plain=0, premature_gated=0),
                dict(name='b8', settings={'HPFONOFF': 2, 'GAMMAVAD_SR': 8.0}, floor_db=-60.0, onset_latency_max=0,
                     false_triggers_per_min=0, spoken=5, triggered=5, wer=0, premature_plain=0, premature_gated=0)]
        self.assertEqual(cal.calibration_for(rows[1], rows)['noise_floor_db'], -47.4)


if __name__ == '__main__':
    unittest.main()
