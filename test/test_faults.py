import errno
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import faults
from voice_errors import RealtimeUnavailable


def named(name, base=Exception, **attributes):
    """Stand-ins for SDK errors: classify() matches class names, never imports the SDK."""
    return type(name, (base,), attributes)


class ClassifyCheck(unittest.TestCase):
    def test_openai_sdk_errors_by_class_name_and_cause(self):
        APIError = named('APIError')
        APIConnectionError = named('APIConnectionError', APIError)
        APITimeoutError = named('APITimeoutError', APIConnectionError)
        RateLimitError = named('RateLimitError', named('APIStatusError', APIError))
        self.assertEqual(faults.classify(named('AuthenticationError')('bad key')), 'openai_auth')
        self.assertEqual(faults.classify(APITimeoutError('slow')), 'openai_timeout')  # Subclass checked first.
        self.assertEqual(faults.classify(APIConnectionError('down')), 'openai_unreachable')
        self.assertEqual(faults.classify(RateLimitError('Error code: 429 insufficient_quota')), 'openai_quota')
        self.assertEqual(faults.classify(RateLimitError('slow down')), 'openai_rate_limit')
        try:
            try:
                raise APIConnectionError('down')
            except APIConnectionError as error:
                raise RuntimeError('Robot reply failed') from error
        except RuntimeError as wrapped:
            self.assertEqual(faults.classify(wrapped), 'openai_unreachable')

    def test_voice_and_hardware_errors(self):
        self.assertEqual(faults.classify(RealtimeUnavailable('rejected', retryable=False)), 'openai_auth')
        self.assertEqual(faults.classify(RealtimeUnavailable('insufficient quota', retryable=False)), 'openai_quota')
        self.assertEqual(faults.classify(RealtimeUnavailable('timed out after 3 attempts')), 'realtime_unavailable')
        self.assertEqual(faults.classify(RealtimeUnavailable('Microphone playback gate timed out; restarting voice input.')),
                         'mic_worker_failed')
        self.assertEqual(faults.classify(named('MicrophoneDisconnected', RuntimeError)('USB lost')), 'mic_disconnected')
        usb = named('USBError', OSError)
        self.assertEqual(faults.classify(usb(errno.EACCES, 'Access denied')), 'mic_permission')
        self.assertEqual(faults.classify(usb(errno.ENODEV, 'No such device')), 'mic_disconnected')
        self.assertEqual(faults.classify(socket.gaierror(-2, 'Name or service not known')), 'internet_down')
        self.assertEqual(faults.classify(RuntimeError('Speech playback failed or was interrupted.')), 'speech_failed')
        self.assertEqual(faults.classify(ValueError('something odd')), 'agent_error')
        tagged = RuntimeError('Microphone failed: Access denied')
        tagged.fault_code = 'mic_permission'
        self.assertEqual(faults.classify(tagged), 'mic_permission')

    def test_network_probe_distinguishes_internet_from_openai(self):
        reachable = set()
        def connect(address, timeout):
            if address[0] not in reachable:
                raise OSError(errno.ENETUNREACH, 'unreachable')
            return socket.socket()
        with patch('faults.socket.getaddrinfo'), patch('faults.socket.create_connection', side_effect=connect):
            self.assertEqual(faults.network_code(), 'internet_down')
            reachable.add('1.1.1.1')
            self.assertEqual(faults.network_code(), 'openai_unreachable')
            reachable.add(faults.OPENAI_HOST)
            self.assertIsNone(faults.network_code())
        self.assertEqual(faults.diagnose(RealtimeUnavailable('timeout'), probe=lambda: 'internet_down'), 'internet_down')
        self.assertEqual(faults.diagnose(RealtimeUnavailable('timeout'), probe=lambda: None), 'realtime_unavailable')
        self.assertEqual(faults.diagnose(ValueError('x'), probe=lambda: self.fail('no probe for local errors')), 'agent_error')


class TableCheck(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'faults.log'

    def tearDown(self):
        self.directory.cleanup()

    def lines(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()] if self.path.exists() else []

    def test_transitions_are_logged_repeats_are_counted(self):
        table = faults.FaultTable(self.path)
        self.assertTrue(table.raise_('internet_down', 'agent', 'offline', detail='Traceback …'))
        for _ in range(5):
            self.assertFalse(table.raise_('internet_down', 'agent', 'offline'))
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(table.faults['internet_down']['count'], 6)
        self.assertTrue(table.raise_('internet_down', 'agent', 'still offline'))  # A new message is news.
        table.raise_('tracker_cpu_fallback', 'tracker', 'Hailo failed')
        table.raise_('wake_model_missing', 'agent', 'no model')
        public = table.public()
        self.assertEqual([fault['code'] for fault in public['faults']], ['internet_down', 'tracker_cpu_fallback'])
        self.assertNotIn('detail', json.dumps(public))  # Tracebacks never reach the display.
        self.assertTrue(table.clear(where=lambda fault: fault['severity'] == 'error', reason='recovered'))
        self.assertEqual(list(table.faults), ['tracker_cpu_fallback', 'wake_model_missing'])
        self.assertTrue(table.clear(component='tracker'))
        self.assertFalse(table.clear('internet_down'))
        events = self.lines()
        self.assertEqual([event['event'] for event in events], ['raise', 'update', 'raise', 'raise', 'clear', 'clear'])
        self.assertEqual(events[0]['detail'], 'Traceback …')
        self.assertEqual(events[4]['reason'], 'recovered')
        self.assertEqual(events[4]['repeats'], 7)
        with self.assertRaises(ValueError):
            table.raise_('made_up', 'agent')

    def test_memory_only_table_writes_nothing(self):
        with patch.dict(os.environ, {'XDG_CACHE_HOME': self.directory.name}):
            table = faults.FaultTable()
            table.raise_('internet_down', 'agent'); table.clear()
            self.assertFalse(faults.log_path().exists())

    def test_reporter_falls_back_to_the_file_while_the_service_is_down(self):
        with socket.socket() as closed:
            closed.bind(('127.0.0.1', 0)); port = closed.getsockname()[1]
        reporter = faults.Reporter('agent', f'http://127.0.0.1:{port}', log=self.path, timeout=.2)
        reporter.report('mic_disconnected', 'USB lost')
        reporter.report('mic_disconnected', 'USB lost')  # Deduplicated for 5 minutes.
        reporter.report('not_a_code', 'odd')
        reporter.clear('mic_disconnected')
        reporter.clear('mic_disconnected')  # Nothing left to clear.
        events = self.lines()
        self.assertEqual([(event['event'], event['code']) for event in events],
                         [('raise', 'mic_disconnected'), ('raise', 'agent_error'), ('clear', 'mic_disconnected')])
        self.assertTrue(all(event['via'] == 'direct' for event in events))
        self.assertIn('not_a_code: odd', events[1]['message'])
        faults.NULL.report('internet_down'); faults.NULL.clear()  # No-op default never raises.


if __name__ == '__main__':
    unittest.main()
