"""Check real-robot MCP wiring without cloud calls or physical movement."""
import asyncio
from contextlib import AsyncExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from chat import Chatbot, connect_navigation_mcp, main

ENDPOINT = 'http://192.168.10.10:8000/mcp'
NAMES = ['navigate_to_region', 'get_robot_pose', 'cancel_navigation', 'navigate_to_pose', 'approach_object']


def fake_server():
    return SimpleNamespace(
        list_tools=AsyncMock(return_value=[SimpleNamespace(name=name) for name in NAMES]),
        call_tool=AsyncMock(return_value=SimpleNamespace(
            is_error=False, content=[SimpleNamespace(text=json.dumps({'success': True, 'robot_pose': {'x': 1., 'y': 2., 'yaw': 0.}}))])),
    )


class NavigationMCPCheck(unittest.TestCase):
    def test_connector_uses_lan_http_and_long_navigation_timeout_without_replay(self):
        async def check():
            server = fake_server()
            with patch('chat.MCPServerStreamableHttp') as factory, redirect_stdout(io.StringIO()):
                factory.return_value.__aenter__ = AsyncMock(return_value=server)
                factory.return_value.__aexit__ = AsyncMock()
                async with AsyncExitStack() as stack:
                    self.assertIs(await connect_navigation_mcp(stack, ENDPOINT), server)
                config = factory.call_args.kwargs
                self.assertEqual(config['params']['url'], ENDPOINT)
                self.assertGreaterEqual(config['params']['sse_read_timeout'], 10000)
                self.assertGreaterEqual(config['client_session_timeout_seconds'], 10000)
                self.assertEqual(config['max_retry_attempts'], 0)
                factory.return_value.__aexit__.assert_awaited_once()
        asyncio.run(check())

    def test_wrong_server_missing_navigation_tools_is_rejected(self):
        async def check():
            server = fake_server()
            server.list_tools.return_value = [SimpleNamespace(name='robot_status')]
            with patch('chat.MCPServerStreamableHttp') as factory:
                factory.return_value.__aenter__ = AsyncMock(return_value=server)
                factory.return_value.__aexit__ = AsyncMock()
                async with AsyncExitStack() as stack:
                    with self.assertRaisesRegex(RuntimeError, 'missing required tools'):
                        await connect_navigation_mcp(stack, ENDPOINT)
            server.call_tool.assert_not_called()
        asyncio.run(check())

    def test_readonly_check_needs_no_cloud_key_camera_voice_or_motion(self):
        server = fake_server()
        with patch.dict(os.environ, {'DOTS_NAV_MCP_URL': ENDPOINT}), \
                patch('sys.argv', ['chat.py', '--check-navigation-mcp']), \
                patch('chat.connect_navigation_mcp', AsyncMock(return_value=server)) as connect, \
                patch('chat.load_key') as key, patch('chat.CameraFeed') as camera, \
                patch('chat.VoiceInput') as voice, redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(main()), 0)
            self.assertEqual(connect.call_args.args[1], ENDPOINT)
            key.assert_not_called()
            camera.assert_not_called()
            voice.assert_not_called()
            server.call_tool.assert_awaited_once_with('get_robot_pose', {})

    def test_remote_chat_attaches_tools_without_simulator_polling_or_announcement_hooks(self):
        server = fake_server()
        bot = Mock(sleep_requested=False, reply=AsyncMock(return_value='Hello.'))
        flags = ['chat.py', '--navigation-mcp-url', ENDPOINT, '--no-vision', '--no-track',
                 '--no-robot-status', '--no-ros-wake', '--no-speak']
        with patch('sys.argv', flags), patch('chat.load_key'), \
                patch('chat.connect_navigation_mcp', AsyncMock(side_effect=AssertionError('Startup must stay offline'))) as connector, \
                patch('chat.OnDemandNavigationMCP', return_value=server), \
                patch('chat.Chatbot', return_value=bot) as chatbot, \
                patch('chat.robot_name', return_value='Kuro'), \
                patch('chat.read_terminal', side_effect=['Hi Kuro', '/quit']), \
                patch('chat.request', return_value={}), patch('chat.watch_motion') as watcher, \
                patch('chat.start_after_announcement', AsyncMock()) as announcement, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(main()), 0)
            self.assertEqual(bot.agent.mcp_servers, [server])
            connector.assert_not_awaited()
            self.assertTrue(chatbot.call_args.kwargs['navigation_mcp'])
            watcher.assert_not_called()
            announcement.assert_not_called()

    def test_real_robot_instructions_use_confirmed_nav2_results(self):
        with patch('chat.request', return_value={'appearance': {'name': 'Kuro'}}):
            bot = Chatbot('http://127.0.0.1:5173', vision=False, navigation_mcp=True)
            prompt = bot.agent.instructions(None, bot.agent)
        self.assertIn('navigate_to_region', prompt)
        self.assertIn('cancel_navigation', prompt)
        self.assertIn('success=true', prompt)
        self.assertNotIn('When turtlesim MCP tools are available', prompt)


    def test_mock_mode_refuses_a_real_navigation_endpoint(self):
        async def check():
            server = fake_server()
            with patch('chat.MCPServerStreamableHttp') as factory:
                factory.return_value.__aenter__ = AsyncMock(return_value=server)
                factory.return_value.__aexit__ = AsyncMock()
                async with AsyncExitStack() as stack:
                    with self.assertRaisesRegex(RuntimeError, 'mock-only'):
                        await connect_navigation_mcp(stack, ENDPOINT, require_mock=True)
            server.call_tool.assert_awaited_once_with('get_robot_pose', {})
        asyncio.run(check())

    def test_mock_mode_attaches_only_after_identity_check(self):
        async def check():
            server = fake_server()
            server.call_tool.return_value.content[0].text = json.dumps({
                'success': True, 'mock': True, 'mode': 'mock', 'physical_motion': False})
            with patch('chat.MCPServerStreamableHttp') as factory, redirect_stdout(io.StringIO()):
                factory.return_value.__aenter__ = AsyncMock(return_value=server)
                factory.return_value.__aexit__ = AsyncMock()
                async with AsyncExitStack() as stack:
                    self.assertIs(await connect_navigation_mcp(stack, ENDPOINT, require_mock=True), server)
            server.call_tool.assert_awaited_once_with('get_robot_pose', {})
        asyncio.run(check())

    def test_mock_instructions_do_not_claim_physical_arrival(self):
        with patch('chat.request', return_value={'appearance': {'name': 'Kuro'}}):
            bot = Chatbot('http://127.0.0.1:5173', vision=False,
                          navigation_mcp=True, navigation_mock=True)
            prompt = bot.agent.instructions(None, bot.agent)
        self.assertIn('MOCK TEST MODE', prompt)
        self.assertIn('robot stayed still', prompt)
        self.assertNotIn('control the real robot through Nav2', prompt)


if __name__ == '__main__':
    unittest.main()
