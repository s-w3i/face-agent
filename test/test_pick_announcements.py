import asyncio
import json
import queue
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from mission_client import MissionClient
from chat import mission_input

class Announcements(IsolatedAsyncioTestCase):
    async def feed(self,client,events):
        async def lines():
            for event in events:yield (json.dumps({'event':event})+'\n').encode()
        client.process=SimpleNamespace(stdout=lines());await client._read()

    def event(self,stage,state='running',item='can',request='ours'):
        return dict(request_id=request,mission_id='mission',phase=stage,state=state,item=item,announcement=stage)

    async def test_exact_words_once_and_silent_success(self):
        client=MissionClient();client.active_request='ours';client.notices=queue.Queue()
        events=[self.event('initial'),self.event('initial'),self.event('motion_started'),self.event('at_right')]
        await self.feed(client,events)
        self.assertEqual([client.announcements.get_nowait()['_speech_text'] for _ in range(3)],['I will pick up the can for you.','I am picking it up now.','I will drop the can here.'])
        await self.feed(client,[self.event('completed',state='completed')])
        self.assertTrue(client.announcements.empty());self.assertTrue(client.notices.empty())

    async def test_wrong_mission_and_stale_events_are_discarded(self):
        client=MissionClient();client.active_request='ours'
        await self.feed(client,[self.event('initial',request='other')]);self.assertTrue(client.announcements.empty())
        event=self.event('motion_started');client.latest=self.event('at_right')
        self.assertFalse(client.current_announcement(event))
        client.latest=self.event('completed',state='completed');self.assertFalse(client.current_announcement(self.event('at_right')))
        client.active_request='new';self.assertFalse(client.current_announcement(self.event('at_right')))

    async def test_notification_interrupts_sleeping_input_without_wake(self):
        client=MissionClient();cancelled=asyncio.Event()
        async def sleeping_read():
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        task=asyncio.create_task(mission_input(sleeping_read(),client));await asyncio.sleep(0)
        event=self.event('at_right');client.announcements.put_nowait(event)
        self.assertEqual(await asyncio.wait_for(task,1),event)
        self.assertTrue(cancelled.is_set())

    async def test_only_initial_and_right_acknowledge_actual_playback(self):
        client=MissionClient();calls=[]
        async def call(op,**args):calls.append((op,args));return {'success':True}
        client.call=call
        for stage in ('initial','motion_started','at_right'):await client.acknowledge(self.event(stage),True)
        self.assertEqual([args['stage'] for op,args in calls],['initial','at_right'])

class Playback(IsolatedAsyncioTestCase):
    async def scenario(self, success):
        from unittest.mock import AsyncMock, Mock, patch
        from chat import status_chat_loop
        inputs=asyncio.Queue();order=[]
        class Status:
            source='test'
            def __init__(self):
                self.events=asyncio.Queue();self.current=SimpleNamespace(status='SLEEPING',revision=1,source='test')
            async def set(self,target,expected_revision=None):
                self.current=SimpleNamespace(status=target,revision=self.current.revision+1,source='test')
                self.events.put_nowait(self.current);return True
        status=Status();client=MissionClient();client.active_request='ours'
        event=dict(request_id='ours',mission_id='mission',phase='at_right',state='running',announcement='at_right',item='bottle',_pick_announcement=True,_speech_text='I will drop the bottle here.')
        client.latest=event
        async def acknowledge(event,played):order.append(('ack',played))
        client.acknowledge=AsyncMock(side_effect=acknowledge)
        async def read(*args,**kwargs):return await inputs.get()
        voice=SimpleNamespace(read=read,hold=AsyncMock(),follow_status=AsyncMock(),activity=asyncio.Queue())
        bot=Mock(reply=AsyncMock());args=SimpleNamespace(url='http://test',model=None,speak=True,mission_client=client)
        def request(url,operation,payload=None):
            if operation=='status':return {'robotStatus':{'revision':status.current.revision}}
            if operation=='pick-announcement':
                self.assertNotIn('stream',payload)
                order.append(('say',payload['text']));return {'synthetic':True}
        async def playback(*args):
            self.assertTrue(voice.hold.call_args.args[0]);order.append(('played',success));return success
        with patch('chat.Chatbot',return_value=bot),patch('chat.request',side_effect=request),patch('chat.wait_for_speech',side_effect=playback):
            task=asyncio.create_task(status_chat_loop(args,status,voice=voice))
            client.announcements.put_nowait(event)
            try:
                async with asyncio.timeout(2):
                    while not client.acknowledge.await_count or status.current.status!='SLEEPING':await asyncio.sleep(.01)
                self.assertEqual(order,[('say','I will drop the bottle here.'),('played',success),('ack',success)])
                bot.reply.assert_not_awaited()
                inputs.put_nowait('/quit');self.assertEqual(await task,0)
            finally:
                if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_sleeping_playback_uses_no_model_and_restores_sleep(self):await self.scenario(True)
    async def test_failed_playback_acknowledges_failure(self):await self.scenario(False)

    async def test_accepted_submission_suppresses_generated_acknowledgment(self):
        from unittest.mock import AsyncMock, patch
        from chat import Chatbot
        bot=Chatbot('http://test',vision=False);bot.status_managed=True
        result=SimpleNamespace(to_input_list=lambda:[],final_output='Long unwanted mission explanation')
        async def submitted(*args,**kwargs):
            bot._pick_submission_result={'accepted':True,'code':'ACCEPTED'}
            return result
        with patch('chat.Runner.run',new=AsyncMock(side_effect=submitted)):
            self.assertEqual(await bot.reply('pick up the can'),'')

class RejectionFeedback(IsolatedAsyncioTestCase):
    async def test_rejections_explain_the_actual_block(self):
        from unittest.mock import AsyncMock,patch
        from chat import Chatbot
        bot=Chatbot('http://test',vision=False);bot.status_managed=True
        result=SimpleNamespace(to_input_list=lambda:[],final_output='unused')
        cases={'NAV_STATE_UNCERTAIN':'safety check','MISSION_BUSY':'previous task','UNAVAILABLE':'planner is unavailable'}
        for code,expected in cases.items():
            async def rejected(*args,**kwargs):
                bot._pick_submission_result={'accepted':False,'code':code}
                return result
            with patch('chat.Runner.run',new=AsyncMock(side_effect=rejected)):
                self.assertIn(expected,await bot.reply('pick up the can'))

    async def test_no_reachable_grasp_has_specific_spoken_feedback(self):
        client=MissionClient();client.active_request='ours'
        async def lines():
            event=dict(request_id='ours',mission_id='mission',phase='failed',state='failed',error_code='NO_REACHABLE_PLAN')
            yield (json.dumps({'event':event})+'\n').encode()
        client.process=SimpleNamespace(stdout=lines());await client._read()
        event=client.announcements.get_nowait()
        self.assertEqual(event['_speech_text'],'I could not find a reachable grasp. Please reposition the object and try again.')

class MissionLifetime(IsolatedAsyncioTestCase):
    async def test_pending_mission_stays_awake_then_silent_completion_restores_idle_timeout(self):
        from unittest.mock import AsyncMock,Mock,patch
        from chat import status_chat_loop
        from robot_states import IDLE_SECONDS
        class Status:
            source='test'
            def __init__(self):self.events=asyncio.Queue();self.current=SimpleNamespace(status='IDLE',revision=1,source='test')
            async def set(self,value,expected_revision=None):
                self.current=SimpleNamespace(status=value,revision=self.current.revision+1,source='test');self.events.put_nowait(self.current);return True
        status=Status();client=MissionClient();client.active_request='ours';client.latest={'state':'planned'}
        timeouts=[];waiting=asyncio.Event()
        async def read(timeout=None,**kwargs):
            timeouts.append(timeout)
            if len(timeouts)==1:
                self.assertIsNone(timeout);waiting.set();await asyncio.Event().wait()
            if status.current.status=='IDLE':
                self.assertEqual(timeout,IDLE_SECONDS);raise TimeoutError
            return '/quit'
        voice=SimpleNamespace(read=read,hold=AsyncMock(),follow_status=AsyncMock(),activity=asyncio.Queue())
        args=SimpleNamespace(url='http://unused',model=None,speak=False,mission_client=client)
        bot=Mock(reply=AsyncMock())
        with patch('chat.Chatbot',return_value=bot):
            task=asyncio.create_task(status_chat_loop(args,status,voice=voice))
            try:
                await asyncio.wait_for(waiting.wait(),1)
                self.assertEqual(status.current.status,'IDLE')
                client.latest={'state':'completed'};client.state_changed.set()
                self.assertEqual(await asyncio.wait_for(task,2),0)
                self.assertEqual(status.current.status,'SLEEPING');bot.reply.assert_not_awaited()
                self.assertEqual(timeouts,[None,IDLE_SECONDS,None])
            finally:
                if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_old_accepted_result_cannot_silence_next_turn(self):
        from unittest.mock import AsyncMock,patch
        from chat import Chatbot
        bot=Chatbot('http://unused',vision=False);bot.status_managed=True
        bot._pick_submission_result={'accepted':True,'code':'ACCEPTED'}
        result=SimpleNamespace(to_input_list=lambda:[],final_output='Which object do you mean?')
        with patch('chat.Runner.run',new=AsyncMock(return_value=result)):
            self.assertEqual(await bot.reply('the soda can'),'Which object do you mean?')

    async def test_mission_activity_tracks_pending_and_terminal_feedback(self):
        client=MissionClient();self.assertFalse(client.active)
        client.active_request='request';self.assertTrue(client.active)
        client.latest={'state':'running'};self.assertTrue(client.active)
        client.latest={'state':'needs_clarification'};self.assertFalse(client.active)
        client.latest={'state':'completed'};self.assertFalse(client.active)

class PendingInputSafety(IsolatedAsyncioTestCase):
    async def test_mission_wakeup_preserves_dequeued_voice_command(self):
        from voice_input import VoiceInput
        from unittest.mock import AsyncMock,Mock
        voice=object.__new__(VoiceInput)
        voice.failed=False;voice.guard=Mock();voice.guard.done.return_value=False
        voice.terminal_open=False;voice.accepting=True;voice.awake=True
        voice.inputs=asyncio.Queue();event={'text':'stop','doa_deg':0,'utterance_id':1};voice.inputs.put_nowait(event)
        voice.direction=Mock();voice.api=AsyncMock(return_value={})
        entered=asyncio.Event()
        async def hint(value):entered.set();await asyncio.Event().wait()
        voice.publish_hint=hint
        task=asyncio.create_task(voice.read(None));await asyncio.wait_for(entered.wait(),1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(voice.inputs.get_nowait(),event)
