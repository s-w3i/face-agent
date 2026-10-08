"""Exercise real MCP calls against an isolated, headless turtlesim ROS node."""
import asyncio
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from agents.mcp import MCPServerStdio

ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = Path('/opt/ros') / os.environ.get('ROS_DISTRO', 'humble') / 'lib/turtlesim/turtlesim_node'


@unittest.skipUnless(SIMULATOR.exists() and importlib.util.find_spec('rclpy') and importlib.util.find_spec('numpy'), 'Needs ROS 2 turtlesim and requirements-turtlesim.txt.')
class TurtleMCPCheck(unittest.TestCase):
    def test_destinations_rotation_stop_and_disconnect(self):
        async def check():
            env = {key: value for key, value in os.environ.items()
                   if key.startswith(('ROS_', 'RMW_', 'AMENT_', 'CYCLONEDDS_')) or key in ('PYTHONPATH', 'LD_LIBRARY_PATH')}
            env.update(ROS_DOMAIN_ID=str(100 + os.getpid() % 100), ROS_LOCALHOST_ONLY='1', TURTLESIM_REQUIRE_ANNOUNCEMENT='1')
            with tempfile.TemporaryFile() as log:
                sim = subprocess.Popen([str(SIMULATOR)], env={**os.environ, **env, 'QT_QPA_PLATFORM': 'offscreen'}, stdout=log, stderr=log)
                try:
                    async with MCPServerStdio(params={'command': sys.executable, 'args': [str(ROOT / 'scripts/turtlesim_mcp.py')], 'env': env}, client_session_timeout_seconds=15) as mcp:
                        async def call(name, **arguments):
                            result = await mcp.call_tool(name, arguments)
                            if result.is_error:
                                raise ValueError(str(result.content))
                            status = json.loads(result.content[0].text)
                            if name in ('go_to_room', 'move_straight', 'turn_robot', 'rotate_robot'):
                                self.assertEqual(status['state'], 'queued')
                                before = status['pose']
                                await asyncio.sleep(.3)
                                stationary = await call('robot_status')
                                self.assertEqual(stationary['state'], 'queued')
                                for axis in ('x', 'y', 'theta'):
                                    self.assertAlmostEqual(stationary['pose'][axis], before[axis], delta=.02)
                                return await call('start_queued_motion', motion_id=status['motion_id'])
                            return status

                        async def wait_for(predicate, seconds=20):
                            deadline = time.monotonic() + seconds
                            while time.monotonic() < deadline:
                                status = await call('robot_status')
                                if predicate(status): return status
                                await asyncio.sleep(.1)
                            self.fail('Timed out waiting for turtle state: ' + str(status))

                        await wait_for(lambda status: status['connected'])
                        start = (await call('robot_status'))['pose']
                        await call('move_straight', distance=2.0)
                        status = await wait_for(lambda status: status['state'] == 'arrived')
                        self.assertAlmostEqual(status['pose']['x'] - start['x'], 2.0, delta=.15)
                        self.assertAlmostEqual(status['pose']['y'], start['y'], delta=.05)
                        before = status['pose']['theta']
                        await call('turn_robot', direction='left', degrees=30.0)
                        status = await wait_for(lambda status: status['state'] == 'arrived')
                        self.assertAlmostEqual(status['pose']['theta'] - before, math.radians(30), delta=.06)
                        start = status['pose']
                        await call('move_straight', distance=-1.0)
                        status = await wait_for(lambda status: status['state'] == 'arrived')
                        self.assertAlmostEqual(math.hypot(status['pose']['x'] - start['x'], status['pose']['y'] - start['y']), 1.0, delta=.15)
                        before = status['pose']['theta']
                        await call('turn_robot', direction='right')
                        status = await wait_for(lambda status: status['state'] == 'arrived')
                        self.assertAlmostEqual(status['pose']['theta'] - before, -math.pi / 2, delta=.06)
                        with self.assertRaises(ValueError):
                            await call('move_straight', distance=10.0)
                        destinations = await call('list_destinations')
                        self.assertEqual(set(destinations), {'living room', 'bedroom', 'kitchen'})
                        await call('rotate_robot', theta=1.0)
                        status = await wait_for(lambda status: status['state'] == 'arrived')
                        self.assertAlmostEqual(status['pose']['theta'], 1.0, delta=.08)
                        for room, point in destinations.items():
                            await call('go_to_room', destination=room)
                            status = await wait_for(lambda status: status['state'] == 'arrived')
                            self.assertLess(math.hypot(status['pose']['x'] - point['x'], status['pose']['y'] - point['y']), .15)
                        with self.assertRaises(ValueError):
                            await call('go_to_room', destination='unknown room')
                        with self.assertRaises(ValueError):
                            await call('rotate_robot', theta=10.0)
                        await call('rotate_robot', theta=math.pi)
                        self.assertEqual((await call('stop_robot'))['state'], 'stopped')
                        await asyncio.sleep(.3)
                        heading = (await call('robot_status'))['pose']['theta']
                        await asyncio.sleep(.3)
                        self.assertAlmostEqual((await call('robot_status'))['pose']['theta'], heading, delta=.03)
                        await call('go_to_room', destination='living room')
                        self.assertEqual((await call('stop_robot'))['state'], 'stopped')
                        await asyncio.sleep(.2)
                        pose = (await call('robot_status'))['pose']
                        await asyncio.sleep(.3)
                        after = (await call('robot_status'))['pose']
                        self.assertAlmostEqual(pose['x'], after['x'], delta=.02)
                        self.assertAlmostEqual(pose['y'], after['y'], delta=.02)
                        await call('go_to_room', destination='bedroom')
                        sim.terminate(); sim.wait(timeout=5)
                        status = await wait_for(lambda status: status['state'] == 'failed', seconds=5)
                        self.assertFalse(status['connected'])
                finally:
                    if sim.poll() is None:
                        sim.terminate(); sim.wait(timeout=5)
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
