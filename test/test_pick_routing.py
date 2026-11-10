import asyncio
import queue
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from chat import Chatbot
from mission_client import MissionClient

class PickRouting(unittest.IsolatedAsyncioTestCase):
    def bot(self):
        bot=Chatbot('http://unused',vision=True);bot.status_managed=True
        client=MissionClient();client.submit=AsyncMock(return_value={'accepted':True,'code':'ACCEPTED'})
        client.attach(bot,queue.Queue())
        return bot,client

    async def test_named_pick_bypasses_head_vision_even_with_negative_visual_history(self):
        for text in ('Pick up the can','Pickup the soda can','Please pick up the bottle','Can you pick up the can?','grasp the pen'):
            bot,client=self.bot()
            bot.history=[{'role':'assistant','content':'I cannot see a can in the head-camera view.'}]
            with patch('chat.Runner.run',new=AsyncMock(side_effect=AssertionError('No conversational camera/model preflight'))):
                self.assertEqual(await bot.reply(text),'')
            client.submit.assert_awaited_once_with(text)
            self.assertEqual(bot.vision_calls,0)
            self.assertIn('unconfirmed',bot.history[-1]['content'])

    async def test_constraints_are_forwarded_unchanged_for_backend_validation(self):
        bot,client=self.bot();text='Pick up the can and return without opening the gripper'
        await bot.reply(text);client.submit.assert_awaited_once_with(text)

    async def test_greeting_is_removed_without_dropping_pickup_clauses(self):
        bot,client=self.bot()
        with patch('chat.robot_name',return_value='Kuro'):
            await bot.reply('Hi Kuro, pick up the can and bottle')
        client.submit.assert_awaited_once_with('pick up the can and bottle')

    async def test_visual_questions_and_negation_keep_normal_conversation_path(self):
        for text in ('Can you see a can?','What am I holding?','Do not pick up the can','Which can should I pick up?'):
            bot,client=self.bot();result=SimpleNamespace(to_input_list=lambda:[],final_output='Visual or conversational reply')
            with patch('chat.Runner.run',new=AsyncMock(return_value=result)) as model:
                self.assertEqual(await bot.reply(text),'Visual or conversational reply')
            client.submit.assert_not_awaited();model.assert_awaited_once()

    async def test_unavailable_is_not_reported_as_missing_object(self):
        bot,client=self.bot();client.submit.return_value={'accepted':False,'code':'UNAVAILABLE'}
        self.assertEqual(await bot.reply('Pick up the can'),'The picking planner is unavailable right now.')
        client.submit.assert_awaited_once()

    async def test_runtime_events_never_dispatch_another_pick(self):
        bot,client=self.bot();result=SimpleNamespace(to_input_list=lambda:[],final_output='Runtime update')
        with patch('chat.Runner.run',new=AsyncMock(return_value=result)):
            await bot.reply('pick up the can',motion_update=True)
        client.submit.assert_not_awaited()
if __name__=='__main__':unittest.main()

class IncompleteRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_named_pick_returns_short_question_without_backend(self):
        bot=Chatbot('http://unused',vision=False);bot.status_managed=True
        client=MissionClient();client.call=AsyncMock(side_effect=AssertionError('No ROS/recovery request'))
        client.attach(bot,queue.Queue())
        for text in ('Pick up the','Can you grasp it?'):
            with patch('chat.Runner.run',new=AsyncMock(side_effect=AssertionError('No model needed'))):
                self.assertEqual(await bot.reply(text),'Which object would you like me to pick up?')
        client.call.assert_not_awaited()
