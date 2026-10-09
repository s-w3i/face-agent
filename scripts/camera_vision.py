"""Bounded, cancellable ROS snapshots and camera-image history cleanup."""
import asyncio
import json
import math
import os
from pathlib import Path
import signal
import time

from camera_snapshot import DEFAULT_TOPIC, MAX_AGE, validate_crop

ROOT = Path(__file__).resolve().parents[1]


class VisionUnavailable(RuntimeError):
    pass


async def capture_camera(topic=None, crop=None):
    try:
        validate_crop(crop)
    except (ValueError, TypeError) as error:
        raise VisionUnavailable(str(error)) from error
    setup = Path('/opt/ros') / os.environ.get('ROS_DISTRO', 'jazzy') / 'setup.bash'
    if not setup.exists():
        raise VisionUnavailable('ROS 2 is unavailable; start the robot camera on this device.')
    env = os.environ.copy()
    env.pop('OPENAI_API_KEY', None)
    process = None
    try:
        command = ['bash', '-c', 'source "$1"; shift; exec /usr/bin/python3 "$@"', 'kuro-camera',
                   str(setup), str(ROOT / 'scripts/camera_snapshot.py'), '--topic', topic or DEFAULT_TOPIC]
        if crop is not None:
            command.extend(['--crop', *map(str, crop)])
        process = await asyncio.create_subprocess_exec(
            *command,
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=12)
        if len(stdout) > 6_000_000:
            raise VisionUnavailable('The camera snapshot exceeds the size limit.')
        try:
            value = json.loads(stdout)
        except (ValueError, UnicodeDecodeError):
            raise VisionUnavailable('The ROS camera helper failed. Check ROS and python3-pil installation.') from None
        if process.returncode or value.get('error'):
            raise VisionUnavailable(value.get('error', 'The camera helper could not capture a frame.'))
        age = time.time() - value['captured_unix']
        if (not math.isfinite(age) or not -.25 <= age <= MAX_AGE
                or not value['image_url'].startswith('data:image/jpeg;base64,')):
            raise VisionUnavailable('The camera frame expired. Please try looking again.')
        value['age_seconds'] = max(0., age)
        return value
    except (OSError, TimeoutError) as error:
        raise VisionUnavailable('The camera did not respond. Check its ROS node and USB connection.') from error
    except (KeyError, TypeError, AttributeError) as error:
        raise VisionUnavailable('The camera returned an invalid snapshot.') from error
    finally:
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                process.kill()
                await process.wait()


def without_camera_images(history):
    """Keep tool calls and text context, but never replay old camera pixels."""
    calls = {item.get('call_id') for item in history
             if item.get('type') == 'function_call' and item.get('name') == 'look_at_camera'}
    cleaned = []
    for item in history:
        if item.get('type') == 'function_call_output' and item.get('call_id') in calls and isinstance(item.get('output'), list):
            output = [dict(type='input_text', text='Camera image omitted after this completed turn. '
                           'Capture a fresh frame for current visual questions.')
                      if block.get('type') == 'input_image' else block for block in item['output']]
            item = dict(item, output=output)
        cleaned.append(item)
    return cleaned
