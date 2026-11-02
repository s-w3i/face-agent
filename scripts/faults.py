"""Named faults: what is wrong, shown subtly on the face and kept in faults.log.

Stdlib only: imported by the chat venv, the tracking venv, serve.py and the stack
supervisor. serve.py owns the live fault table and is the normal writer of
~/.cache/face-agent/faults.log; other processes POST to /api/faults and append
to the file directly only while the display service is unreachable.
"""
from collections import namedtuple
import errno
import json
import os
from pathlib import Path
import socket
import threading
import time
import traceback
from urllib.error import URLError
from urllib.request import Request, urlopen

Fault = namedtuple('Fault', 'severity priority icon title hint')
# severity: error = Kuro cannot do its job; degraded = working on a fallback;
# info = worth logging, never shown on the face. Higher priority = likelier root cause.
CATALOG = {
    'openai_auth': Fault('error', 95, 'key', 'API key rejected',
        'Update the key in the studio (Voice → Set up API key), then set IDLE or restart face-agent.service.'),
    'openai_quota': Fault('error', 95, 'key', 'OpenAI quota used up',
        'Add credit or raise the limit in OpenAI billing, then set IDLE or restart face-agent.service.'),
    'internet_down': Fault('error', 90, 'wifi', 'No internet',
        'Check the Wi-Fi/Ethernet link and the router. Kuro retries by itself.'),
    'mic_disconnected': Fault('error', 85, 'mic', 'Mic disconnected',
        'Replug the ReSpeaker USB cable (try another port or hub). Kuro reconnects by itself.'),
    'mic_permission': Fault('error', 85, 'mic', 'Mic access denied',
        'USB permission denied: add the udev rule and the plugdev group (README → ReSpeaker), then replug.'),
    'openai_unreachable': Fault('error', 80, 'cloud', 'OpenAI unreachable',
        'The internet works but api.openai.com does not answer. Check status.openai.com, DNS or a firewall. Kuro retries by itself.'),
    'ros_status_lost': Fault('error', 75, 'plug', 'ROS status lost',
        'The ROS status bridge stopped. Check stack-status.log and ROS_DOMAIN_ID, then restart face-agent.service.'),
    'mic_worker_failed': Fault('error', 70, 'mic', 'Microphone failed',
        'The microphone worker failed. See microphone-chat.log; Kuro restarts it by itself.'),
    'realtime_unavailable': Fault('error', 65, 'cloud', 'Transcription down',
        'OpenAI Realtime transcription failed although the network works. Kuro reconnects by itself.'),
    'openai_rate_limit': Fault('error', 60, 'cloud', 'OpenAI rate limited',
        'Too many requests. Wait a minute; Kuro retries by itself.'),
    'openai_timeout': Fault('error', 55, 'cloud', 'OpenAI timed out',
        'A slow network or a slow OpenAI response. If it keeps happening, check the connection.'),
    'openai_error': Fault('error', 50, 'cloud', 'OpenAI error',
        'OpenAI returned an error. Run ./doctor.sh --detail for the message.'),
    'speech_failed': Fault('error', 40, 'speaker', 'Speech failed',
        'The spoken reply could not be generated or played. Run ./doctor.sh --detail.'),
    'external_error': Fault('error', 30, 'alert', 'Error set externally',
        'Another ROS node set ERROR. Fix that node, then set IDLE.'),
    'agent_error': Fault('error', 10, 'alert', 'Agent error',
        'An unexpected error. Run ./doctor.sh --detail for the traceback.'),
    'component_restarting': Fault('degraded', 60, 'alert', 'Restarting',
        'A stack component exited and is restarting. Run ./doctor.sh --detail for its last log lines.'),
    'stt_offline': Fault('degraded', 50, 'cloud', 'Offline speech',
        'OpenAI Realtime is unreachable; commands are transcribed locally until it reconnects.'),
    'camera_unavailable': Fault('degraded', 40, 'camera', 'Camera unavailable',
        'Vision questions cannot see. Check the Orbbec USB cable and stack-camera.log.'),
    'tracking_unavailable': Fault('degraded', 35, 'camera', 'Eye tracking off',
        'The eye tracker stopped; chat continues. See tracking-chat.log, then restart face-agent.service.'),
    'tracker_cpu_fallback': Fault('degraded', 30, 'chip', 'Tracking on CPU',
        'The Hailo-8L is unavailable (check hailort.service). Restart face-agent.service to retry it.'),
    'wake_whisper_fallback': Fault('degraded', 20, 'mic', 'Wake word: Whisper',
        'openWakeWord is not in use, so Whisper listens for the wake phrase (more CPU, slower).'),
    'wake_model_missing': Fault('info', 0, 'mic', 'No wake model',
        'Whisper listens for the wake phrase until an openWakeWord model is trained (docs/wake-word-training.md).'),
}
NETWORK_CODES = frozenset(('internet_down', 'openai_unreachable', 'openai_timeout', 'realtime_unavailable'))
DETAIL_LIMIT = 4000
OPENAI_HOST = 'api.openai.com'


def log_path():
    return Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'face-agent' / 'faults.log'


def now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def append_log(entry, path=None):
    """One JSON object per line; never raises (diagnostics must not cause new failures)."""
    path = Path(path) if path else log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as output:
            output.write(json.dumps(dict(time=now_iso(), **entry), default=str) + '\n')
    except OSError:
        pass


def detail_of(error):
    """Short traceback tail for the log; never shown on the display."""
    if error is None:
        return None
    text = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
    return text[-DETAIL_LIMIT:]


def _chain(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


def _names(error):
    return {cls.__name__ for cls in type(error).__mro__}


def classify(error):
    """Map an exception (and its causes) to a CATALOG code without importing optional SDKs."""
    for item in _chain(error):
        if getattr(item, 'fault_code', None) in CATALOG:
            return item.fault_code  # Raisers that know better tag the exception.
        names = _names(item)
        text = str(item).lower()
        code = str(getattr(item, 'code', '') or '').lower()
        if 'AuthenticationError' in names or 'PermissionDeniedError' in names:
            return 'openai_auth'
        if 'RateLimitError' in names:
            return 'openai_quota' if 'insufficient_quota' in (code + text) else 'openai_rate_limit'
        if 'APITimeoutError' in names:
            return 'openai_timeout'
        if 'APIConnectionError' in names:
            return 'openai_unreachable'  # Refined to internet_down by network_code().
        if 'APIStatusError' in names or 'APIError' in names:
            return 'openai_error'
        if 'RealtimeUnavailable' in names:
            if text.startswith('microphone'):
                return 'mic_worker_failed'  # Local gate/control timeouts reuse this class.
            if getattr(item, 'retryable', True) is False:
                return 'openai_quota' if 'quota' in text else 'openai_auth'
            return 'realtime_unavailable'
        if 'MicrophoneDisconnected' in names or 'DeviceUnavailable' in names:
            return 'mic_disconnected'
        if 'USBError' in names:
            number = getattr(item, 'errno', None)
            return 'mic_permission' if number == errno.EACCES else 'mic_disconnected'
        if 'VisionUnavailable' in names:
            return 'camera_unavailable'
        if isinstance(item, socket.gaierror):
            return 'internet_down'
        if isinstance(item, (ConnectionRefusedError, ConnectionResetError)) or (
                isinstance(item, OSError) and item.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN)):
            return 'openai_unreachable'
        if isinstance(item, TimeoutError) and 'openai' in text:
            return 'openai_timeout'
        if 'respeaker' in text:
            return 'mic_disconnected'
        if 'speech' in text and ('playback' in text or 'openai' in text):
            return 'speech_failed'
    return 'agent_error'


def _connects(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def network_code(timeout=2.0):
    """None when api.openai.com:443 accepts a TCP connection; otherwise which side is down.

    No HTTP request and no API key: a TCP handshake costs nothing on the OpenAI bill.
    """
    try:
        socket.getaddrinfo(OPENAI_HOST, 443, type=socket.SOCK_STREAM)
        resolved = True
    except OSError:
        resolved = False
    if resolved and _connects(OPENAI_HOST, 443, timeout):
        return None
    if _connects('1.1.1.1', 443, timeout) or _connects('8.8.8.8', 53, timeout):
        return 'openai_unreachable'
    return 'internet_down'


def diagnose(error, probe=network_code):
    """classify() refined by a live network probe; blocking, so call via asyncio.to_thread."""
    code = classify(error)
    if code in NETWORK_CODES or code == 'speech_failed':
        measured = probe()
        if measured:
            return measured
    return code


class Reporter:
    """Report and clear faults for one component; thread-safe and never raises."""

    def __init__(self, component, url=None, *, timeout=1.0, log=None):
        self.component = component
        self.url = (url or os.environ.get('DOTS_URL') or
                    f"http://127.0.0.1:{os.environ.get('PORT', '5173')}").rstrip('/')
        self.timeout = timeout
        self.log = log
        self.lock = threading.Lock()
        self.fallback = {}  # code -> (message, monotonic) for direct writes while serve is down.

    def _post(self, payload):
        try:
            request = Request(self.url + '/api/faults', data=json.dumps(payload).encode(),
                              headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=self.timeout) as response:
                response.read()
            return True
        except (URLError, OSError, ValueError):
            return False

    def report(self, code, message='', *, detail=None, title=None, auto_clear=True):
        if code not in CATALOG:
            code, message = 'agent_error', f'{code}: {message}'
        message = str(message or '')[:500]
        payload = dict(action='raise', code=code, component=self.component, message=message,
                       autoClear=bool(auto_clear))
        if title:
            payload['title'] = str(title)[:60]
        if detail:
            payload['detail'] = str(detail)[-DETAIL_LIMIT:]
        if self._post(payload):
            with self.lock:
                self.fallback.pop(code, None)
            return
        with self.lock:
            previous = self.fallback.get(code)
            if previous and previous[0] == message and time.monotonic() - previous[1] < 300:
                return  # The display is down too; one line per 5 minutes per code is enough.
            self.fallback[code] = (message, time.monotonic())
        fault = CATALOG[code]
        append_log(dict(event='raise', code=code, severity=fault.severity, component=self.component,
                        title=title or fault.title, message=message, hint=fault.hint, detail=detail,
                        via='direct'), self.log)

    def report_error(self, error, *, code=None, auto_clear=True):
        """Classify (without a network probe) and report an exception with its traceback."""
        self.report(code or classify(error), str(error), detail=detail_of(error), auto_clear=auto_clear)

    def clear(self, code=None):
        """Clear one code, or every fault this component raised."""
        payload = dict(action='clear', component=self.component)
        if code:
            payload['code'] = code
        with self.lock:
            if code:
                had = self.fallback.pop(code, None) is not None
            else:
                had = bool(self.fallback)
                self.fallback.clear()
        if not self._post(payload) and had:
            append_log(dict(event='clear', code=code, component=self.component, via='direct'), self.log)


class NullReporter:
    """Default for code paths built without a reporter (tests, typed chat, legacy modes)."""
    component = 'none'

    def report(self, *args, **kwargs): pass
    def report_error(self, *args, **kwargs): pass
    def clear(self, *args, **kwargs): pass


NULL = NullReporter()


class FaultTable:
    """serve.py's live faults; transitions (not repeats) are written to faults.log when `log` is set."""

    def __init__(self, log=None):
        self.faults = {}
        self.sequence = 0
        self.log = log

    def public(self):
        """Display payload: never includes tracebacks."""
        values = sorted(self.faults.values(), key=lambda value: (value['severity'] != 'error', -value['priority'], -value['since']))
        return dict(sequence=self.sequence, faults=[{key: value for key, value in fault.items() if key != 'detail'}
                                                    for fault in values if fault['severity'] != 'info'])

    def raise_(self, code, component, message='', *, title=None, detail=None, auto_clear=True):
        if code not in CATALOG:
            raise ValueError('Unknown fault code.')
        fault = CATALOG[code]
        message = str(message or '')[:500]
        title = str(title or fault.title)[:60]
        current = self.faults.get(code)
        if current and current['message'] == message and current['title'] == title:
            current['count'] += 1
            current['lastSeen'] = time.time()
            current['autoClear'] = current['autoClear'] and auto_clear
            return False  # Repeats (retry loops) do not reach the file or the display.
        record = dict(code=code, severity=fault.severity, priority=fault.priority, icon=fault.icon,
                      title=title, message=message, hint=fault.hint, component=component,
                      since=current['since'] if current else time.time(), lastSeen=time.time(),
                      count=(current['count'] + 1) if current else 1,
                      autoClear=(current['autoClear'] if current else True) and auto_clear)
        if detail:
            record['detail'] = str(detail)[-DETAIL_LIMIT:]
        self.faults[code] = record
        self.sequence += 1
        if self.log:
            append_log(dict(event='update' if current else 'raise', code=code, severity=fault.severity,
                            component=component, title=title, message=message, hint=fault.hint,
                            detail=record.get('detail')), self.log)
        return True

    def clear(self, code=None, component=None, *, reason='cleared', where=None):
        """Clear by code, by component, or by predicate; returns whether anything changed."""
        removed = [value for value in self.faults.values()
                   if (code is None or value['code'] == code)
                   and (component is None or code is not None or value['component'] == component)
                   and (where is None or where(value))]
        for value in removed:
            del self.faults[value['code']]
            if self.log:
                append_log(dict(event='clear', code=value['code'], component=value['component'], reason=reason,
                                repeats=value['count'], lasted=round(time.time() - value['since'], 1)), self.log)
        if removed:
            self.sequence += 1
        return bool(removed)
