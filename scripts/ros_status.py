"""ROS status subscriber/client, isolated from the chatbot's Python environment."""
import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Status:
    status: str
    revision: int
    source: str = ''


def bridge_command():
    setup = Path('/opt/ros') / os.environ.get('ROS_DISTRO', 'jazzy') / 'setup.bash'
    if not setup.exists():
        raise RuntimeError('ROS 2 is required for shared robot status. Install ROS 2 or use --no-robot-status.')
    overlay = ROOT / 'ros2/install/local_setup.bash'
    if not overlay.exists():
        result = subprocess.run(['bash', str(ROOT / 'scripts/setup_robot_status.sh')],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError('Build robot status interfaces with bash scripts/setup_robot_status.sh.\n' + result.stdout.decode()[-1500:])
    return ['bash', '-c', 'source "$1"; source "$2"; exec /usr/bin/python3 "$3"',
            'robot-status-bridge', str(setup), str(overlay), str(Path(__file__).resolve())]


def bridge_env():
    env = os.environ.copy()
    env.pop('OPENAI_API_KEY', None)
    return env


class RosStatus:
    def __init__(self):
        self.source = 'face-agent-' + uuid.uuid4().hex
        self.current = None
        self.events = asyncio.Queue()
        self.process = self.reader = self.log = None
        self.pending = {}; self.sequence = 0
        self.available = asyncio.Event()

    async def __aenter__(self):
        path = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'face-agent/robot-status-bridge.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        self.log = path.open('a')
        try:
            command = await asyncio.to_thread(bridge_command)
            self.process = await asyncio.create_subprocess_exec(*command, env=bridge_env(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self.log)
            self.reader = asyncio.create_task(self.receive())
            await asyncio.wait_for(self.available.wait(), 20)
            if self.current is None:
                raise RuntimeError('ROS status bridge stopped. Check ' + str(path))
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *args):
        await self.close()

    def update(self, value):
        state = Status(**value)
        if self.current is None or state.revision > self.current.revision:
            self.current = state
            self.events.put_nowait(state)
            self.available.set()

    async def receive(self):
        try:
            while line := await self.process.stdout.readline():
                value = json.loads(line)
                if 'state' in value:
                    self.update(value['state'])
                if 'request' in value:
                    future = self.pending.pop(value['request'], None)
                    if future and not future.done():
                        future.set_result(value)
        finally:
            self.current = None
            self.available.set()
            self.events.put_nowait(None)
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError('ROS status bridge disconnected.'))

    async def set(self, status, expected_revision=0):
        self.sequence += 1
        ident = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            self.process.stdin.write((json.dumps(dict(request=ident, status=status,
                expected_revision=expected_revision, source=self.source)) + '\n').encode())
            await self.process.stdin.drain()
            result = await asyncio.wait_for(future, 5)
            self.update(result['state'])
            return result['success']
        finally:
            self.pending.pop(ident, None)

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                self.process.terminate()
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.log:
            self.log.close()


class DisplayStatus:
    """Subscribe even when no chatbot is running; ROS never imports web/voice libs."""
    def __init__(self, server):
        import threading
        self.server = server
        self.process = None
        self.closing = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            self.process = subprocess.Popen(bridge_command(), env=bridge_env(), stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=sys.stderr, text=True)
            if self.closing:
                self.process.stdin.close()
            for line in self.process.stdout:
                event = json.loads(line)
                if 'state' in event:
                    self.server.apply_robot_status(event['state'])
            if not self.closing:
                raise RuntimeError('ROS status subscriber stopped.')
        except (OSError, RuntimeError, ValueError) as error:
            print('Display ROS status unavailable: ' + str(error), file=sys.stderr)
            # Losing global control must stop any old playback/transcript.
            if not self.closing:
                self.server.apply_robot_status(dict(status='ERROR', revision=0, source='connection'))

    def close(self):
        self.closing = True
        if self.process:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=3)
        self.thread.join(timeout=3)
        if self.process and self.process.stdout and not self.thread.is_alive():
            self.process.stdout.close()


def serve():
    import queue
    import threading
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
    from robot_status_interfaces.msg import RobotStatus
    from robot_status_interfaces.srv import SetRobotStatus

    rclpy.init()
    node = rclpy.create_node('face_agent_status_' + uuid.uuid4().hex[:8])
    client = node.create_client(SetRobotStatus, '/robot/set_status')
    if not client.wait_for_service(timeout_sec=1):
        path = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'face-agent/robot-status.log'
        with path.open('a') as log:
            # The manager survives subscriber/chat exits and is shared with other nodes.
            subprocess.Popen(['/usr/bin/python3', str(ROOT / 'scripts/robot_status_node.py')],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        if not client.wait_for_service(timeout_sec=10):
            raise RuntimeError('Robot status manager unavailable; check ' + str(path))
    incoming = queue.Queue()
    def receive():
        try:
            for line in sys.stdin:
                incoming.put(json.loads(line))
        finally:
            incoming.put(None)
    threading.Thread(target=receive, daemon=True).start()
    def state_value(state):
        return dict(status=state.status, revision=state.revision, source=state.source)
    def emit(value):
        print(json.dumps(value), flush=True)
    qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.TRANSIENT_LOCAL)
    node.create_subscription(RobotStatus, '/robot/status', lambda state: emit(dict(state=state_value(state))), qos)
    def poll():
        while not incoming.empty():
            value = incoming.get_nowait()
            if value is None:
                rclpy.try_shutdown()
                return
            request = SetRobotStatus.Request(status=value['status'], source=value['source'],
                                              expected_revision=value['expected_revision'])
            future = client.call_async(request)
            def done(future, ident=value['request']):
                result = future.result()
                emit(dict(request=ident, success=result.success, message=result.message,
                          state=state_value(result.current)))
            future.add_done_callback(done)
    node.create_timer(.02, poll)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    serve()
