"""Check the real ROS service and its chatbot acknowledgment without cloud calls."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from ros_wake import RosWake

SETUP = Path('/opt/ros/jazzy/setup.bash')


@unittest.skipUnless(SETUP.is_file(), 'ROS 2 Jazzy not installed')
class RosWakeCheck(unittest.IsolatedAsyncioTestCase):
    async def test_service_waits_for_chat_and_propagates_success_or_failure(self):
        client_code = '''import json,rclpy
from std_srvs.srv import Trigger
rclpy.init()
node=rclpy.create_node('kuro_wake_test_client')
client=node.create_client(Trigger, '/kuro/wake')
assert client.wait_for_service(timeout_sec=15), 'Service not discovered'
for _ in range(2):
    future=client.call_async(Trigger.Request())
    rclpy.spin_until_future_complete(node, future, timeout_sec=15)
    result=future.result()
    print(json.dumps(dict(success=result.success, message=result.message)), flush=True)
node.destroy_node()
rclpy.shutdown()
'''
        with tempfile.TemporaryDirectory() as cache, patch.dict(os.environ, dict(
                ROS_DISTRO='jazzy', ROS_DOMAIN_ID='92', ROS_LOCALHOST_ONLY='1', XDG_CACHE_HOME=cache)):
            async with RosWake() as bridge:
                self.assertIsNotNone(bridge.reader)
                client = await asyncio.create_subprocess_exec(
                    'bash', '-c', 'source "$1"; exec /usr/bin/python3 -c "$2"',
                    'ros-wake-test', str(SETUP), client_code,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    for expected in (True, False):
                        async with asyncio.timeout(18):
                            while bridge.requests.empty():
                                await asyncio.sleep(.05)
                        request = bridge.requests.get_nowait()
                        # Receipt alone must not complete the ROS future.
                        response = asyncio.create_task(client.stdout.readline())
                        await asyncio.sleep(.15)
                        self.assertFalse(response.done())
                        await bridge.complete(request, expected, 'Ready' if expected else 'Realtime unavailable')
                        result = json.loads(await asyncio.wait_for(response, 5))
                        self.assertEqual(result, dict(success=expected, message='Ready' if expected else 'Realtime unavailable'))
                    await asyncio.wait_for(client.wait(), 5)
                    self.assertEqual(client.returncode, 0, (await client.stderr.read()).decode())
                finally:
                    if client.returncode is None:
                        client.terminate()
                        await client.wait()
            self.assertIsNotNone(bridge.process.returncode)


if __name__ == '__main__':
    unittest.main()
