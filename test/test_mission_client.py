import asyncio
import json
import queue
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from mission_client import MissionClient

class Tests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_reuses_request(self):
        client=MissionClient(); calls=[]
        async def call(op,**args): calls.append(args); return {'code':'UNKNOWN'}
        client.call=call
        await client.submit('pick up can'); await client.submit('pick up can')
        self.assertEqual(calls[0]['request_id'],calls[1]['request_id'])
        result=await client.submit('pick up bottle')
        self.assertEqual(result['code'],'MISSION_BUSY'); self.assertEqual(len(calls),2)

    async def test_new_explicit_pick_after_terminal_gets_new_id(self):
        client=MissionClient(); calls=[]
        async def call(op,**args): calls.append(args); return {'code':'ACCEPTED'}
        client.call=call
        await client.submit('pick up can'); client.latest={'state':'completed'}
        await client.submit('pick up can')
        self.assertNotEqual(calls[0]['request_id'],calls[1]['request_id'])

    async def test_unavailable_can_be_requested_again(self):
        client=MissionClient()
        async def call(*args,**kwargs): return {'code':'UNAVAILABLE'}
        client.call=call; await client.submit('pick up can')
        self.assertIsNone(client.active_request)

    async def test_correlated_terminal_once(self):
        client=MissionClient(); client.active_request='ours'; client.notices=queue.Queue()
        async def events():
            for rid in ['other','ours','ours']:
                yield (json.dumps({'event':dict(request_id=rid,mission_id='mission',state='completed',phase='completed',message='sequence completed, unverified')})+'\n').encode()
        client.process=SimpleNamespace(stdout=events())
        await client._read()
        self.assertEqual(client.notices.qsize(),0)
        self.assertTrue(client.announcements.empty())
        self.assertEqual(client.latest['request_id'],'ours')

class RecoveryStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejection_preserves_previous_failure(self):
        client=MissionClient();client.active_request='previous'
        failure={'request_id':'previous','mission_id':'m','state':'failed','message':'Base feedback stale'}
        client.latest=failure;client.item='can'
        async def call(*args,**kwargs):return {'code':'PICK_RECOVERY_REQUIRED','message':'Inspect the arm'}
        client.call=call
        await client.submit('pick up can')
        self.assertIs(client.latest,failure)
        self.assertEqual(client.active_request,'previous')
        self.assertEqual(client.mission_status()['submission_rejection']['code'],'PICK_RECOVERY_REQUIRED')

    async def test_retained_status_is_visible_without_announcement_or_ownership(self):
        client=MissionClient()
        async def events():
            yield (json.dumps({'event':{'request_id':'old','mission_id':'m','state':'failed','phase':'failed','message':'Base feedback stale'}})+'\n').encode()
        client.process=SimpleNamespace(stdout=events())
        await client._read()
        self.assertEqual(client.mission_status()['state'],'failed')
        self.assertFalse(client.mission_status()['owned'])
        self.assertTrue(client.announcements.empty())
        client.active_request='new'
        self.assertEqual(client.mission_status()['state'],'unknown')

if __name__=='__main__': unittest.main()

class LiveStateInstructionsTests(unittest.TestCase):
    def test_instructions_replace_old_error_with_current_stage_and_completion(self):
        client=MissionClient();bot=SimpleNamespace(agent=SimpleNamespace(instructions=lambda *args:'Base.',tools=[]))
        client.attach(bot,None);client.active_request='ours'
        client.last_submission={'code':'ACCEPTED'}
        for state,phase,message in [('running','motion_started','I am picking it up now.'),('completed','completed','Released at right and returned to zero.')]:
            client.latest={'request_id':'ours','mission_id':'mission','state':state,'phase':phase,'message':message}
            text=bot.agent.instructions(None,bot.agent)
            self.assertIn('"state": "'+state+'"',text)
            self.assertIn(message,text)
            self.assertIn('supersedes earlier pickup errors',text)
            self.assertIn('Do not mention pickup status for unrelated speech',text)
            self.assertNotIn('submission_rejection',text)

class IncompletePickupTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_object_is_clarified_before_bridge_or_recovery(self):
        from unittest.mock import AsyncMock
        client=MissionClient();client.call=AsyncMock(side_effect=AssertionError('No backend request'))
        client.latest={'state':'failed','message':'Old recovery failure'}
        for text in ('Pick up','Pick up the','Pick up a.','Could you pick up the?','Please grasp it','pick up that one'):
            result=await client.submit(text)
            self.assertEqual(result['code'],'OBJECT_REQUIRED',text)
            self.assertFalse(result['accepted'])
        client.call.assert_not_awaited()
        self.assertEqual(client.latest['message'],'Old recovery failure')
