"""Vision tools, SDK image serialization, cancellation, and history hygiene."""
import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from agents.items import ItemHelpers
from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall
from camera_vision import capture_camera, VisionUnavailable, without_camera_images
from chat import Chatbot


def frame():
    return dict(image_url='data:image/jpeg;base64,/9j/2Q==', captured_unix=time.time(),
                captured_at='2026-10-09T12:00:00+00:00', age_seconds=.02, width=1280, height=720)


class VisionCheck(unittest.IsolatedAsyncioTestCase):
    async def test_ros_helper_does_not_receive_credentials_and_stale_frames_fail(self):
        for stale in (False, True):
            value = frame()
            if stale:
                value['captured_unix'] -= 10
            process = Mock(returncode=0, communicate=AsyncMock(return_value=(json.dumps(value).encode(), b'')))
            with patch('camera_vision.Path.exists', return_value=True), \
                    patch.dict(os.environ, {'OPENAI_API_KEY': 'private-key'}), \
                    patch('camera_vision.asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=process) as start:
                if stale:
                    with self.assertRaises(VisionUnavailable):
                        await capture_camera('/test/color/compressed')
                else:
                    result = await capture_camera('/test/color/compressed')
                    self.assertEqual(result['image_url'], value['image_url'])
                self.assertNotIn('OPENAI_API_KEY', start.call_args.kwargs['env'])
                self.assertIn('/test/color/compressed', start.call_args.args)

    async def test_cancelled_turn_stops_snapshot_process(self):
        async def communicate():
            await asyncio.sleep(60)
        process = Mock(returncode=None, pid=12345, communicate=communicate, wait=AsyncMock(return_value=0))
        with patch('camera_vision.Path.exists', return_value=True), \
                patch('camera_vision.asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=process), \
                patch('camera_vision.os.killpg') as kill:
            task = asyncio.create_task(capture_camera())
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            kill.assert_called_once()
            process.wait.assert_awaited_once()

    async def test_tool_returns_real_sdk_image_content_and_bounds_attempts(self):
        bot = Chatbot('http://localhost:5173')
        tool = next(tool for tool in bot.agent.tools if getattr(tool, 'name', '') == 'look_at_camera')
        arguments = json.dumps(dict(question='What object is being held?'))
        context = ToolContext(context=None, tool_name=tool.name, tool_call_id='camera-call', tool_arguments=arguments)
        with patch('chat.capture_camera', new_callable=AsyncMock, return_value=frame()) as capture, redirect_stdout(io.StringIO()) as output:
            result = await tool.on_invoke_tool(context, arguments)
            call = ResponseFunctionToolCall(type='function_call', name=tool.name, call_id='camera-call', arguments=arguments)
            wire = ItemHelpers.tool_call_output_item(call, result)
            self.assertEqual(wire['output'][1]['type'], 'input_image')
            self.assertEqual(wire['output'][1]['detail'], 'high')
            self.assertEqual(wire['output'][1]['image_url'], frame()['image_url'])
            zoom = json.dumps(dict(question='Inspect the object more closely.', crop=[.2, .2, .8, .8]))
            await tool.on_invoke_tool(context, zoom)
            self.assertEqual(capture.call_args.kwargs['crop'], [.2, .2, .8, .8])
            limited = await tool.on_invoke_tool(context, arguments)
            self.assertIn('Two camera attempts', limited)
            self.assertEqual(capture.await_count, 2)
            self.assertNotIn('base64', output.getvalue())

    async def test_camera_failure_is_usable_tool_result_without_image(self):
        bot = Chatbot('http://localhost:5173')
        tool = next(tool for tool in bot.agent.tools if getattr(tool, 'name', '') == 'look_at_camera')
        arguments = json.dumps(dict(question='Inspect the outfit.'))
        context = ToolContext(context=None, tool_name=tool.name, tool_call_id='camera-call', tool_arguments=arguments)
        with patch('chat.capture_camera', new_callable=AsyncMock, side_effect=VisionUnavailable('Camera disconnected.')), redirect_stdout(io.StringIO()):
            result = await tool.on_invoke_tool(context, arguments)
        self.assertIn('No visual evidence', result)
        self.assertIn('do not guess', result)
        self.assertNotIn('data:image', result)

    async def test_reply_prunes_only_camera_images_and_resets_budget(self):
        camera_image = dict(type='input_image', image_url=frame()['image_url'], detail='high')
        history = [dict(type='function_call', name='look_at_camera', call_id='camera-call', arguments='{}'),
                   dict(type='function_call_output', call_id='camera-call', output=[dict(type='input_text', text='Fresh frame.'), camera_image]),
                   dict(role='assistant', content='You are holding a red cup.')]
        bot = Chatbot('http://localhost:5173')
        bot.status_managed = True
        bot.vision_calls = 2
        result = SimpleNamespace(final_output='You are holding a red cup.', to_input_list=lambda: history)
        with patch('chat.Runner.run', new_callable=AsyncMock, return_value=result):
            await bot.reply('What am I holding?')
        self.assertEqual(bot.vision_calls, 0)
        self.assertNotIn('data:image', str(bot.history))
        self.assertEqual(bot.history[-1]['content'], 'You are holding a red cup.')
        self.assertEqual(history[1]['output'][1]['type'], 'input_image')
        other = [dict(type='function_call_output', call_id='other-tool', output=[camera_image])]
        self.assertEqual(without_camera_images(other), other)

    async def test_disabled_vision_exposes_no_camera_tool(self):
        bot = Chatbot('http://localhost:5173', vision=False)
        self.assertNotIn('look_at_camera', [getattr(tool, 'name', '') for tool in bot.agent.tools])


if __name__ == '__main__':
    unittest.main()
