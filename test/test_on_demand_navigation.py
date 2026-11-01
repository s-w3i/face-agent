"""Boot and ordinary chat do not require the remote robot navigation server."""
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from navigation_mcp import OnDemandNavigationMCP
from chat import main

URL='http://127.0.0.1:18000/mcp'


class OnDemandCheck(unittest.IsolatedAsyncioTestCase):
    async def test_startup_and_tool_discovery_never_connect(self):
        connector=AsyncMock(side_effect=AssertionError('Remote server is offline'))
        server=OnDemandNavigationMCP(URL,connector)
        await server.connect()
        tools=await server.list_tools()
        await server.list_tools()
        await server.cleanup()
        self.assertEqual({tool.name for tool in tools},
                         {'navigate_to_region','get_robot_pose','cancel_navigation','navigate_to_pose','approach_object'})
        connector.assert_not_awaited()
        self.assertEqual((await server.list_prompts()).prompts,[])

    async def test_offline_request_returns_error_and_later_explicit_request_can_reconnect(self):
        result=SimpleNamespace(is_error=False)
        remote=Mock(call_tool=AsyncMock(return_value=result))
        connector=AsyncMock(side_effect=[OSError('offline'),remote])
        server=OnDemandNavigationMCP(URL,connector)
        first=await server.call_tool('get_robot_pose',{})
        self.assertTrue(first.is_error)
        payload=json.loads(first.content[0].text)
        self.assertEqual(payload['error'],'NAVIGATION_SERVER_UNAVAILABLE')
        self.assertFalse(payload['request_sent'])
        self.assertIs(await server.call_tool('get_robot_pose',{}),result)
        self.assertEqual(connector.await_count,2)
        remote.call_tool.assert_awaited_once_with('get_robot_pose',{},meta=None)

    async def test_lost_movement_result_is_not_replayed_or_reported_as_arrival(self):
        remote=Mock(call_tool=AsyncMock(side_effect=OSError('connection dropped')))
        connector=AsyncMock(return_value=remote)
        server=OnDemandNavigationMCP(URL,connector)
        result=await server.call_tool('navigate_to_region',{'region':'kitchen'})
        payload=json.loads(result.content[0].text)
        self.assertTrue(result.is_error)
        self.assertEqual(payload['error'],'NAVIGATION_RESULT_UNKNOWN')
        self.assertTrue(payload['request_sent'])
        connector.assert_awaited_once()
        remote.call_tool.assert_awaited_once()

    async def test_transport_lifetime_and_cancellation_stay_in_call_task(self):
        events=[]
        async def cleanup():events.append(('cleanup',asyncio.current_task()))
        async def connector(stack,*args,**kwargs):
            events.append(('connect',asyncio.current_task()))
            stack.push_async_callback(cleanup)
            return Mock(call_tool=AsyncMock(side_effect=asyncio.CancelledError))
        server=OnDemandNavigationMCP(URL,connector)
        with self.assertRaises(asyncio.CancelledError):
            await server.call_tool('get_robot_pose',{})
        self.assertEqual([name for name,_ in events],['connect','cleanup'])
        self.assertIs(events[0][1],events[1][1])

    async def test_mock_tools_stay_limited_and_identity_guard_is_requested_on_use(self):
        remote=Mock(call_tool=AsyncMock(return_value=SimpleNamespace(is_error=False)))
        connector=AsyncMock(return_value=remote)
        server=OnDemandNavigationMCP(URL,connector,require_mock=True)
        self.assertEqual(len(await server.list_tools()),3)
        denied=await server.call_tool('navigate_to_pose',{'x':1,'y':2})
        self.assertTrue(denied.is_error)
        connector.assert_not_awaited()
        await server.call_tool('get_robot_pose',{})
        self.assertTrue(connector.call_args.kwargs['require_mock'])

    async def test_voice_startup_and_ready_announcement_work_with_remote_server_offline(self):
        status=SimpleNamespace(current=SimpleNamespace(status='SLEEPING',revision=1))
        voice=Mock()
        flags=['chat.py','--mode','voice','--navigation-mcp-url',URL,'--no-vision','--no-track']
        with patch('sys.argv',flags), patch('chat.load_key'), \
                patch('chat.RosStatus') as status_factory,patch('chat.VoiceInput') as voice_factory, \
                patch('chat.request',return_value={'ready':True}), \
                patch('chat.announce_voice_ready',new_callable=AsyncMock) as announce, \
                patch('chat.status_chat_loop',new_callable=AsyncMock,return_value=0) as chat_loop, \
                patch('chat.connect_navigation_mcp',new_callable=AsyncMock,side_effect=AssertionError('Offline')) as connector, \
                redirect_stdout(io.StringIO()):
            status_factory.return_value.__aenter__.return_value=status
            voice_factory.return_value.__aenter__.return_value=voice
            self.assertEqual(await main(),0)
            connector.assert_not_awaited()
            announce.assert_awaited_once()
            self.assertIsInstance(chat_loop.call_args.args[2],OnDemandNavigationMCP)


if __name__=='__main__':unittest.main()
