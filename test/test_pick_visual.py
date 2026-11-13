"""Planning visuals are cosmetic: no ROS, cloud, microphone or motors."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,AsyncMock,patch
from urllib.request import urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from mission_client import MissionClient
from pick_visual import PickVisualSender
from serve import RobotServer
from dotsctl import request

class MissionVisualTests(unittest.IsolatedAsyncioTestCase):
    def event(self,phase,state='running',mid='mission',rid='ours'):
        return dict(mission_id=mid,request_id=rid,phase=phase,state=state,message='test')
    async def test_correlated_visual_order_no_speech_and_stale_events_ignored(self):
        client=MissionClient();client.active_request='ours';client._visual_sender=Mock()
        for event in [self.event('planning_started',rid='other'),self.event('planning_started'),self.event('planning_started'),self.event('motion_started'),self.event('planning_started'),self.event('completed','completed')]:
            client._visual_event(event)
        self.assertEqual([c.args for c in client._visual_sender.set.call_args_list],[('mission',),(None,)])
        self.assertTrue(client.announcements.empty())
    async def test_all_terminal_outcomes_and_planner_reset_clear(self):
        for outcome in ('completed','failed','cancelled','rejected','needs_clarification','reset'):
            client=MissionClient();client.active_request='ours'
            client._visual_event(self.event('planning_started'))
            client._visual_event(self.event(outcome,outcome) if outcome!='reset' else dict(phase='idle',mission_id=None))
            self.assertIsNone(client.visual_mission)
            client._visual_event(self.event('planning_started'))
            self.assertIsNone(client.visual_mission)
    async def test_bridge_disconnect_clears_and_planning_event_does_not_speak(self):
        client=MissionClient();client.active_request='ours';client._visual_sender=Mock()
        async def lines():yield (json.dumps({'event':self.event('planning_started')})+'\n').encode()
        client.process=SimpleNamespace(stdout=lines())
        await client._read()
        self.assertTrue(client.announcements.empty())
        self.assertEqual([c.args for c in client._visual_sender.set.call_args_list],[('mission',),(None,)])
    async def test_sender_refreshes_and_clears_in_order_on_shutdown(self):
        sender=PickVisualSender('unused');sender.send=AsyncMock()
        with patch('pick_visual.REFRESH_SECONDS',.01):
            sender.start();sender.set('first');await asyncio.sleep(.035)
            sender.set('second');await asyncio.sleep(.015);await sender.close()
        calls=[c.args for c in sender.send.call_args_list]
        self.assertGreaterEqual(calls.count(('first',True)),2)
        self.assertLess(calls.index(('first',False)),calls.index(('second',True)))
        self.assertEqual(calls[-1],('second',False))
    async def test_display_failure_does_not_escape(self):
        sender=PickVisualSender('unused')
        with patch('pick_visual.request',side_effect=ValueError('offline')):
            await sender.send('mission',True);await sender.send('mission',False)

class DisplayVisualTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        self.server=RobotServer(('127.0.0.1',0),config_file=root/'config.json',static_root=root/'web',key_file=root/'private/key')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown();self.thread.join(2);self.server.server_close();self.temp.cleanup()
    def send(self,mid,active):return request(self.url,'pick-visual',dict(mission_id=mid,active=active))
    def test_overlay_never_changes_conversation_speech_or_microphone_metadata(self):
        for i,state in enumerate(('LISTENING','SPEAKING','SLEEPING','ERROR'),1):
            self.server.apply_robot_status(dict(status=state,revision=i,source='test'))
            before=json.dumps((self.server.robot_status,self.server.command,self.server.listening_text,self.server.speech_pending()))
            self.send('mission',True)
            self.assertEqual(before,json.dumps((self.server.robot_status,self.server.command,self.server.listening_text,self.server.speech_pending())))
        with patch('serve.time.monotonic',return_value=self.server.pick_visual_until+1):
            self.assertFalse(self.server.pick_visual_value()['active'])
    def test_explicit_clear_retires_mission_and_stale_updates_cannot_replace_new_one(self):
        self.send('first',True);self.send('second',True)
        self.assertFalse(self.send('first',False)['accepted'])
        self.assertFalse(self.send('first',True)['accepted'])
        self.assertEqual(self.server.pick_visual_value()['mission_id'],'second')
        self.send('second',False)
        self.assertFalse(self.send('second',True)['accepted'])
    def test_invalid_payload_is_rejected(self):
        for value in ({'mission_id':'bad/id','active':True},{'mission_id':'x','active':'true'},{'mission_id':'x','active':True,'state':'speaking'}):
            with self.assertRaises(ValueError):request(self.url,'pick-visual',value)
    def test_sse_has_independent_visual_event(self):
        self.send('mission',True)
        with urlopen(self.url+'/api/events',timeout=3) as response:
            for _ in range(100):
                if response.readline().decode().strip()=='event: pick-visual':
                    value=json.loads(response.readline().decode().removeprefix('data: '))
                    self.assertTrue(value['active']);self.assertEqual(value['mission_id'],'mission');break
            else:self.fail('Missing visual event')
