"""Voice following routing contracts without models, enrollment or robot motion."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from chat import Chatbot
from navigation_mcp import OnDemandNavigationMCP


class FollowingToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_expose_resolution_follow_and_status(self):
        server = OnDemandNavigationMCP('http://unused/mcp', AsyncMock())
        names = {tool.name for tool in await server.list_tools()}
        self.assertTrue({'get_follow_target', 'follow_person', 'get_following_status'} <= names)
        mocked = OnDemandNavigationMCP('http://unused/mcp', AsyncMock(), require_mock=True)
        self.assertFalse({'get_follow_target', 'follow_person', 'get_following_status'} & {t.name for t in await mocked.list_tools()})

    async def test_introductions_expose_named_enrollment(self):
        server = OnDemandNavigationMCP('http://unused/mcp', AsyncMock())
        self.assertIn('remember_person', {t.name for t in await server.list_tools()})
        with patch('chat.robot_name', return_value='Kuro'):
            bot = Chatbot('http://unused', navigation_mcp=True, vision=False)
            instructions = bot.agent.instructions(None, bot.agent)
        for phrase in ['My name is Furia', 'I am Furia', 'Call me Furia', 'call remember_person', 'Reuse an already confirmed identity']:
            self.assertIn(phrase, instructions)

    async def test_explicit_identity_is_forwarded_once_without_enrollment(self):
        remote = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(is_error=False)))
        connector = AsyncMock(return_value=remote)
        server = OnDemandNavigationMCP('http://unused/mcp', connector)
        await server.call_tool('follow_person', {'identity_id': 7})
        remote.call_tool.assert_awaited_once_with('follow_person', {'identity_id': 7}, meta=None)

    async def test_unknown_person_uses_backend_enrollment_route(self):
        remote = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(is_error=False)))
        server = OnDemandNavigationMCP('http://unused/mcp', AsyncMock(return_value=remote))
        await server.call_tool('follow_person', {})
        remote.call_tool.assert_awaited_once_with('follow_person', {}, meta=None)

    async def test_follow_failure_is_not_replayed(self):
        remote = SimpleNamespace(call_tool=AsyncMock(side_effect=ConnectionError('disconnected')))
        server = OnDemandNavigationMCP('http://unused/mcp', AsyncMock(return_value=remote))
        result = await server.call_tool('follow_person', {})
        self.assertTrue(result.is_error)
        self.assertEqual(json.loads(result.content[0].text)['error'], 'NAVIGATION_RESULT_UNKNOWN')
        remote.call_tool.assert_awaited_once()

    async def test_voice_instructions_resolve_identity_and_stop_explicitly(self):
        with patch('chat.robot_name', return_value='Kuro'):
            bot = Chatbot('http://unused', navigation_mcp=True, vision=False)
            instructions = bot.agent.instructions(None, bot.agent)
        for phrase in ['first call get_follow_target', 'identity_id directly', 'enrolls the nearest unknown person without a name',
                       'get_following_status', 'cancellation_confirmed=true', 'Do not claim speaker identification']:
            self.assertIn(phrase, instructions)


if __name__ == '__main__':
    unittest.main()
