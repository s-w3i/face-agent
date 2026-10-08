"""ODAS source tracking and speech-candidate recording for the ReSpeaker raw mics."""
import array
import asyncio
from contextlib import ExitStack
from collections import deque
import json
import math
from pathlib import Path
import re
import sys
import time
import wave

from respeaker import ROOT, DeviceUnavailable, USBError, apply_profile, emit, level_dbfs, save

ODAS = Path.home() / '.cache/face-agent/odas/build/bin/odaslive'
TEMPLATE = ROOT / 'scripts/respeaker_vendor/odas.cfg'
HOP = 128
RATE = 16000


class Utterance:
    """Endpoint one tracked source; another source cannot extend its silence timer."""
    def __init__(self):
        self.history = deque(maxlen=math.ceil(1.2 * RATE / HOP))
        self.pending_id = None
        self.pending_frames = 0
        self.track_id = None
        self.audio = []
        self.silent = 0
        self.directions = deque(maxlen=10)
        self.utterance_id = None

    def direction(self, angle):
        self.directions.append(math.radians(angle))

    def finish(self, reason):
        if self.track_id is None:
            return None
        trim = max(0, self.silent - math.ceil(.2 * RATE / HOP))
        audio = self.audio[:-trim] if trim else self.audio
        x = sum(math.cos(angle) for angle in self.directions)
        y = sum(math.sin(angle) for angle in self.directions)
        doa = math.degrees(math.atan2(y, x)) % 360 if self.directions and math.hypot(x, y) >= len(self.directions) * .6 else None
        event = {'track_id': self.track_id, 'reason': reason, 'pcm': b''.join(audio), 'doa_deg': doa, 'utterance_id': self.utterance_id}
        self.track_id = None
        self.audio = []
        self.silent = 0
        self.pending_id = None
        self.pending_frames = 0
        self.history.clear()
        self.directions.clear()
        return event

    def feed(self, sources, channels, candidates, processed_pcm):
        speaking = {sources[i]['id'] for i in candidates}
        silence = bytes(HOP * 2)
        if self.track_id is None:
            self.history.append(processed_pcm)
            candidate = max(candidates, key=lambda i: sources[i]['activity']) if candidates else None
            ident = sources[candidate]['id'] if candidate is not None else None
            self.pending_frames = self.pending_frames + 1 if ident and ident == self.pending_id else int(bool(ident))
            self.pending_id = ident
            if self.pending_frames >= math.ceil(.2 * RATE / HOP):
                self.track_id = ident
                self.audio = list(self.history)
                return b''.join(self.audio), None
            return silence, None
        data = processed_pcm
        self.audio.append(data)
        self.silent = 0 if self.track_id in speaking else self.silent + 1
        if self.silent >= math.ceil(1 * RATE / HOP):
            return data, self.finish('silence')
        if len(self.audio) >= math.ceil(30 * RATE / HOP):
            return data, self.finish('maximum_duration')
        return data, None


async def json_frames(reader):
    buffer = ''
    decoder = json.JSONDecoder()
    while True:
        chunk = await reader.read(8192)
        if not chunk:
            raise DeviceUnavailable('ODAS audio/metadata stream disconnected.')
        buffer += chunk.decode('utf-8')
        if len(buffer) > 1_000_000:
            raise RuntimeError('ODAS JSON stream exceeded buffer limit.')
        while True:
            buffer = buffer.lstrip()
            try:
                message, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                break
            buffer = buffer[end:]
            if not isinstance(message, dict) or not isinstance(message.get('timeStamp'), int):
                raise RuntimeError('Invalid ODAS frame.')
            yield message


def split_channels(pcm):
    samples = array.array('h', pcm)
    import sys
    if sys.byteorder != 'little':
        samples.byteswap()
    channels = [samples[i::4] for i in range(4)]
    if sys.byteorder != 'little':
        for channel in channels:
            channel.byteswap()
    return [channel.tobytes() for channel in channels]


def azimuth(source):
    return math.degrees(math.atan2(source['y'], source['x'])) % 360


def make_config(source, channelmap, ports):
    config = TEMPLATE.read_text()
    if 'input' in ports:
        replacement = f'interface: {{ type = "socket"; ip = "127.0.0.1"; port = {ports["input"]}; }}'
    else:
        replacement = ('interface: { type = "pulseaudio"; source = ' + json.dumps(source) +
                       '; channelmap = (' + ','.join(json.dumps(c) for c in channelmap) + '); }')
    config = re.sub(r'interface:\s*\{.*?\}', replacement, config, count=1, flags=re.S)
    config = re.sub(r'potential:\s*\{.*?\n    \};',
                    'potential: { format = "undefined"; interface: { type = "blackhole"; }; };',
                    config, count=1, flags=re.S)
    config = config.replace('port = 9000', 'port = ' + str(ports['tracks']))
    # Keep strong coherent sources trackable too; the vendor's narrow energy
    # distribution can discard high-confidence peaks before speech selection.
    config = config.replace('weight = 1.0; mu = 0.4; sigma2 = 0.0025', 'weight = 1.0; mu = 0.75; sigma2 = 0.0225')
    config = config.replace('weight = 1.0; mu = 0.25; sigma2 = 0.0025', 'weight = 1.0; mu = 0.15; sigma2 = 0.0225')
    config = config.replace('gain_pf = 10.0', 'gain_pf = 1.0').replace('gain = 10.0', 'gain = 1.0')
    config = re.sub(r'separated:\s*\{.*?\n    \};',
                    'separated: { fS = 16000; hopSize = 128; nBits = 16; interface: { type = "blackhole"; }; };',
                    config, count=1, flags=re.S)
    config = config.replace('type = "file";\n            path = "postfiltered.raw";',
                            f'type = "socket"; ip = "127.0.0.1"; port = {ports["audio"]};')
    config = re.sub(r'category:\s*\{.*?\n    \}',
                    f'category: {{ format = "json"; interface: {{ type = "socket"; ip = "127.0.0.1"; port = {ports["categories"]}; }}; }}',
                    config, count=1, flags=re.S)
    return config


async def read_controls(state):
    reader = asyncio.StreamReader(limit=4096)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    try:
        while line := await reader.readline():
            value = json.loads(line)
            if not isinstance(value, dict) or set(value) != {'paused', 'sequence'} or type(value['paused']) is not bool or type(value['sequence']) is not int or value['sequence'] <= state['sequence']:
                raise RuntimeError('Invalid microphone capture control.')
            state.update(value)
            state['resume_at'] = time.monotonic() + .5
    finally:
        state['stopping'] = True
        transport.close()


async def run(args, dev, source_info):
    if '6ch' not in str(source_info['sample_specification']):
        raise RuntimeError('ODAS requires six-channel firmware: processed channel 0, raw mics 1–4. No mono downmix is allowed.')
    if not ODAS.exists():
        raise RuntimeError('ODAS is not built. Run scripts/setup_respeaker_odas.sh first.')
    if args.target is not None:
        raise RuntimeError('ODAS tracks moving sources; --target belongs to the legacy DSP mode.')
    apply_profile(dev)
    output = Path(args.wav)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise RuntimeError(f'Recording already exists: {output}')
    paths = {'command': output, 'sources': output.with_name(output.stem + '-sources.wav'),
             'tracks': output.with_name(output.stem + '-tracks.jsonl'),
             'config': output.with_suffix('.cfg'), 'log': output.with_suffix('.log')}
    if any(path.exists() for path in paths.values()):
        raise RuntimeError('An output file already exists; choose a new --wav name.')
    futures, servers, writers = {}, [], []
    process = capture = pump = None
    onset_audio = asyncio.Queue(maxsize=512)
    utterance = Utterance()
    transcriber = None
    # Chat owns the resume decision after calibration and playback-status checks.
    control = dict(paused=bool(getattr(args, 'control_stdin', False)), sequence=0, stopping=False, resume_at=0)
    controller = None
    control_sequence = 0
    acknowledged_sequence = 0
    native_direction = dev.direction
    clip_count = 0
    utterance_sequence = 0

    def save_clip(event):
        nonlocal clip_count
        if event is None:
            return
        clip_count += 1
        path = output if clip_count == 1 else output.with_name(f'{output.stem}-{clip_count:03d}{output.suffix}')
        with path.open('xb') as file, wave.open(file, 'wb') as wav:
            wav.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
            wav.writeframes(event['pcm'])
        emit({'status': 'utterance_saved', 'recording_channel': 0, 'wav': str(path), 'track_id': event['track_id'], 'doa_deg': event['doa_deg'], 'utterance_id': event['utterance_id'],
              'reason': event['reason'], 'duration_seconds': len(event['pcm']) / (RATE * 2)})
        if transcriber:
            try:
                transcriber.commit(path, event['doa_deg'])
            except RuntimeError:
                emit({'status': 'transcription_incomplete', 'wav': str(path)})
    try:
        if getattr(args, 'control_stdin', False):
            controller = asyncio.create_task(read_controls(control))
        if args.command == 'listen' and not args.no_stt:
            from respeaker_stt import Transcriber
            transcriber = await Transcriber.open(args.stt_model, args.stt_delay, args.language)
        ports = {}
        for name in ['tracks', 'categories', 'audio', 'input']:
            future = asyncio.get_running_loop().create_future()
            futures[name] = future
            async def connected(reader, writer, future=future, name=name):
                if future.done():
                    writer.close()
                    return
                writers.append(writer)
                future.set_result((reader, writer) if name == 'input' else reader)
            server = await asyncio.start_server(connected, '127.0.0.1', 0)
            servers.append(server)
            ports[name] = server.sockets[0].getsockname()[1]
        paths['config'].write_text(make_config(source_info['name'], source_info['channel_map'].split(','), ports))
        with paths['log'].open('xb') as log, ExitStack() as stack:
            process = await asyncio.create_subprocess_exec(str(ODAS), '-c', str(paths['config']),
                                                           stdout=log, stderr=log)
            # One capture feeds both the pre-roll and ODAS, preserving hop alignment.
            _, input_writer = await asyncio.wait_for(futures['input'], 20)
            capture = await asyncio.create_subprocess_exec(
                'parec', '--device=' + source_info['name'], '--raw', '--format=s16le',
                '--rate=16000', '--channels=6', '--latency-msec=40', '--channel-map=' + source_info['channel_map'],
                stdout=asyncio.subprocess.PIPE, stderr=log)

            async def feed_input():
                while True:
                    raw = await capture.stdout.readexactly(HOP * 6 * 2)
                    samples = array.array('h', raw)
                    # Byte-preserving extraction of the device's processed mono channel.
                    await onset_audio.put(samples[0::6].tobytes())
                    input_writer.write(raw)
                    await input_writer.drain()

            pump = asyncio.create_task(feed_input())
            readers = await asyncio.wait_for(asyncio.gather(*(futures[n] for n in ['tracks', 'categories', 'audio'])), 20)
            tracks_iter, categories_iter = json_frames(readers[0]), json_frames(readers[1])
            emit({'status': 'settling_adaptive_processing', 'seconds': args.settle_seconds})
            noise = [[] for _ in range(4)]
            thresholds = [-70] * 4
            frame = 0
            warm_frames = math.ceil(args.settle_seconds * RATE / HOP)
            noise_frames = math.ceil(args.noise_seconds * RATE / HOP)
            sources_wav = None
            recorded = accepted = 0
            trace = stack.enter_context(paths['tracks'].open('x'))
            pcm_out = stack.enter_context(open(args.pcm, 'wb', buffering=0)) if args.pcm else None
            while True:
                if control['stopping']:
                    if controller.done() and not controller.cancelled() and controller.exception():
                        raise controller.exception()
                    break
                if control['sequence'] != control_sequence:
                    utterance = Utterance()  # Drop active speech and pre-roll across the playback gate.
                    if transcriber:
                        transcriber.discard()
                    control_sequence = control['sequence']
                if control_sequence != acknowledged_sequence and (control['paused'] or time.monotonic() >= control['resume_at']):
                    acknowledged_sequence = control_sequence
                    emit({'status': 'microphone_control', 'sequence': control_sequence, 'paused': control['paused']})
                if transcriber:
                    transcriber.check()
                try:
                    tracks, categories, pcm, onset_pcm = await asyncio.wait_for(asyncio.gather(
                        anext(tracks_iter), anext(categories_iter), readers[2].readexactly(HOP * 4 * 2), onset_audio.get()), 5)
                except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError) as error:
                    raise DeviceUnavailable('ReSpeaker audio stream stopped.') from error
                if tracks['timeStamp'] != frame + 1 or tracks['timeStamp'] != categories['timeStamp'] or len(tracks.get('src', [])) != 4 or len(categories.get('src', [])) != 4:
                    raise RuntimeError('ODAS audio/track metadata is out of sequence.')
                frame += 1
                channels = split_channels(pcm)
                levels = [level_dbfs(c) for c in channels]
                sources = tracks['src']
                if frame <= warm_frames:
                    continue
                if frame == warm_frames + 1:
                    emit({'status': 'calibrating_background', 'seconds': args.noise_seconds,
                          'instruction': 'Stay silent; keep the usual music, fans and motors running.'})
                if frame <= warm_frames + noise_frames:
                    for i, level in enumerate(levels):
                        # Measure nonspeech residuals; speech-like music is not a noise floor.
                        if sources[i]['id'] and categories['src'][i]['category'] != 'speech' and level > -120:
                            noise[i].append(level)
                    continue
                if sources_wav is None:
                    residual = sorted(value for slot in noise for value in slot)
                    floor = max(-70, min(-40, residual[int(len(residual)*.95)] + 3)) if residual else -60
                    # Slots are reused as sources move, so share the measured residual floor.
                    thresholds = [floor] * 4
                    save(ROOT / 'respeaker-config.json', {'backend': 'odas', 'residual_thresholds_dbfs': thresholds,
                         'settle_seconds': args.settle_seconds, 'noise_seconds': args.noise_seconds,
                         'background_residual_samples': [len(n) for n in noise],
                         'calibrated_at_unix': time.time()})
                    if args.command == 'calibrate':
                        emit({'status': 'background_calibrated', 'residual_thresholds_dbfs': thresholds})
                        return
                    f = stack.enter_context(paths['sources'].open('xb'))
                    sources_wav = stack.enter_context(wave.open(f, 'wb'))
                    sources_wav.setparams((4, 2, RATE, 0, 'NONE', 'not compressed'))
                    emit({'status': 'listening', 'recording_channel': 0, 'first_utterance_wav': str(output), 'sources_wav': str(paths['sources']),
                          'residual_thresholds_dbfs': thresholds,
                          'azimuth_frame': 'ODAS microphone geometry x-axis; native device DOA reported separately'})
                if control['paused'] or time.monotonic() < control['resume_at']:
                    continue
                if recorded % 25 == 0:
                    native_direction = dev.direction
                candidates = [i for i, source in enumerate(sources)
                              if source['id'] and categories['src'][i]['category'] == 'speech'
                              and levels[i] >= thresholds[i]]
                previous_id = utterance.track_id
                data, event = utterance.feed(sources, channels, candidates, onset_pcm)
                if previous_id is None and utterance.track_id is not None:
                    utterance_sequence += 1
                    utterance.utterance_id = utterance_sequence
                    utterance.direction(native_direction)
                    emit({'status': 'utterance_started', 'track_id': utterance.track_id, 'doa_deg': native_direction, 'utterance_id': utterance.utterance_id})
                    if transcriber:
                        transcriber.begin(utterance.track_id, native_direction, utterance.utterance_id)
                if transcriber and (previous_id is not None or utterance.track_id is not None):
                    transcriber.append(data)
                save_clip(event)
                index = next((i for i, source in enumerate(sources) if source['id'] == utterance.track_id), None) if utterance.track_id is not None else None
                accepted += index is not None
                if index in candidates and recorded % 25 == 0:
                    utterance.direction(native_direction)
                sources_wav.writeframesraw(pcm)
                if pcm_out:
                    pcm_out.write(data)
                recorded += 1
                if recorded % 25 == 1:
                    trace.write(json.dumps({'frame': frame, 'tracks': tracks, 'categories': categories}) + '\n')
                if recorded % 25 == 1:
                    emit({'native_doa_deg': native_direction, 'native_vad': bool(dev.is_voice()),
                          'utterance_id': utterance.utterance_id if utterance.track_id is not None else None,
                          'speech_candidate_track_id': sources[index]['id'] if index in candidates else None, 'speech_candidate_azimuth_deg': round(azimuth(sources[index]), 1) if index in candidates else None,
                          'sources': [{'track_id': s['id'], 'azimuth_deg': round(azimuth(s), 1),
                                       'category': categories['src'][i]['category'], 'rms_dbfs': round(levels[i], 1)}
                                      for i, s in enumerate(sources) if s['id']]})
                if args.seconds and recorded * HOP / RATE >= args.seconds:
                    save_clip(utterance.finish('shutdown'))
                    emit({'status': 'stopped', 'recorded_seconds': recorded * HOP / RATE,
                          'accepted_hops': accepted, 'utterances_saved': clip_count})
                    break
    except (DeviceUnavailable, USBError) as error:
        if isinstance(error, DeviceUnavailable) or error.errno == 19:
            # Notify chat before draining old transcripts during cleanup.
            emit({'status': 'microphone_error', 'reconnectable': True})
        raise
    finally:
        if controller:
            controller.cancel()
            await asyncio.gather(controller, return_exceptions=True)
        if pump:
            pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, asyncio.IncompleteReadError, ConnectionError):
                pass
        if capture and capture.returncode is None:
            capture.terminate()
            await capture.wait()
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        for writer in writers:
            writer.close()
        for server in servers:
            server.close()
            await server.wait_closed()
        save_clip(utterance.finish('shutdown'))
        if transcriber:
            await transcriber.close()
