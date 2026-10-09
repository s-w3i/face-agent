"""Bridge the ROS 2 /kuro/wake Trigger service to the running chatbot.

ROS runs in the system Python, keeping its dependencies out of the voice venv.
The service only succeeds after the chatbot has finished waking up.
"""
import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import sys
import time


@dataclass
class WakeRequest:
    sequence: int
    expires: float


class RosWake:
    def __init__(self):
        self.requests = queue.Queue()
        self.process = self.reader = self.log = None

    async def __aenter__(self):
        setup = Path('/opt/ros') / os.environ.get('ROS_DISTRO', 'jazzy') / 'setup.bash'
        if not setup.is_file():
            print('ROS wake service unavailable: ROS 2 setup not found. Wake phrase still works.', file=sys.stderr)
            return self
        log_path = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'face-agent/ros-wake.log'
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log = log_path.open('a')
            env = os.environ.copy()
            env.pop('OPENAI_API_KEY', None)
            self.process = await asyncio.create_subprocess_exec(
                'bash', '-c', 'source "$1"; exec /usr/bin/python3 "$2"',
                'kuro-ros-wake', str(setup), str(Path(__file__).resolve()),
                env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self.log)
            ready = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 15))
            if ready.get('status') != 'ready':
                raise ValueError('ROS wake service did not become ready.')
            self.reader = asyncio.create_task(self.receive())
            print('ROS 2 wake service ready · /kuro/wake (std_srvs/srv/Trigger).', flush=True)
        except (OSError, ValueError, asyncio.TimeoutError):
            await self.close()
            print(f'ROS wake service unavailable. Check {log_path}. Wake phrase still works.', file=sys.stderr)
        return self

    async def __aexit__(self, *exception):
        await self.close()

    async def receive(self):
        while line := await self.process.stdout.readline():
            event = json.loads(line)
            if event.get('status') == 'wake':
                self.requests.put(WakeRequest(event['sequence'], event['expires']))
        print('ROS wake service stopped. Wake phrase still works.', file=sys.stderr)

    async def complete(self, wake, success, message):
        if self.process and self.process.returncode is None:
            try:
                self.process.stdin.write((json.dumps(dict(sequence=wake.sequence, success=success, message=message)) + '\n').encode())
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def close(self):
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                self.process.terminate()
                await self.process.wait()
        if self.log:
            self.log.close()


def serve():
    import threading
    import rclpy
    from std_srvs.srv import Trigger

    rclpy.init()
    node = rclpy.create_node('kuro_wake_service')
    pending = {}
    lock = threading.Lock()
    sequence = 0

    def receive():
        try:
            for line in sys.stdin:
                result = json.loads(line)
                with lock:
                    waiter = pending.get(result['sequence'])
                    if waiter:
                        waiter[1].update(result)
                        waiter[0].set()
        finally:
            with lock:
                for event, result in pending.values():
                    result.update(success=False, message='Chatbot stopped.')
                    event.set()
            rclpy.try_shutdown()

    def wake(request, response):
        nonlocal sequence
        sequence += 1
        ident = sequence
        done, result = threading.Event(), {}
        with lock:
            pending[ident] = done, result
        print(json.dumps(dict(status='wake', sequence=ident, expires=time.monotonic() + 120)), flush=True)
        received = done.wait(120)
        with lock:
            pending.pop(ident, None)
        response.success = received and result.get('success', False)
        response.message = result.get('message', 'Chatbot did not confirm wake within 120 seconds.')
        return response

    node.create_service(Trigger, '/kuro/wake', wake)
    threading.Thread(target=receive, daemon=True).start()
    print(json.dumps(dict(status='ready')), flush=True)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    serve()
