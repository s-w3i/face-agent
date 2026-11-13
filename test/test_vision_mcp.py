import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from vision_mcp import OnDemandVisionMCP, VISION_INSTRUCTIONS


def reply(payload):
    return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps(payload))])


class VisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_is_offline_and_old_crop_rejected(self):
        with patch('vision_mcp.MCPServerStreamableHttp') as factory:
            adapter=OnDemandVisionMCP('http://example/mcp')
            await adapter.connect()
            self.assertEqual({t.name for t in await adapter.list_tools()},
                {'detect_object','get_status','get_detection_image'})
            result=await adapter.call_tool('get_detection_image',{})
            self.assertFalse(json.loads(result.content[0].text)['success'])
            factory.assert_not_called()

    async def test_success_image_and_failed_replacement(self):
        stamp={'sec':123,'nanosec':0}
        detection=reply({'success':True,'object_name':'bag','observation_stamp':stamp})
        image=SimpleNamespace(content=[SimpleNamespace(type='image')])
        remote=AsyncMock()
        remote.call_tool.side_effect=[detection,reply({'crop_stamp':stamp}),image,reply({'success':False})]
        manager=AsyncMock();manager.__aenter__.return_value=remote
        with patch('vision_mcp.MCPServerStreamableHttp',return_value=manager):
            adapter=OnDemandVisionMCP('http://example/mcp')
            self.assertIs(await adapter.call_tool('detect_object',{'object_description':'pointed bag'}),detection)
            self.assertIs(await adapter.call_tool('get_detection_image',{}),image)
            await adapter.call_tool('detect_object',{'object_description':'missing'})
            self.assertIsNone(adapter.last_observation)

    async def test_other_clients_crop_is_not_used(self):
        remote=AsyncMock();remote.call_tool.return_value=reply({'crop_stamp':{'sec':456,'nanosec':0}})
        manager=AsyncMock();manager.__aenter__.return_value=remote
        with patch('vision_mcp.MCPServerStreamableHttp',return_value=manager):
            adapter=OnDemandVisionMCP('http://example/mcp');adapter.last_observation={'sec':123,'nanosec':0}
            result=await adapter.call_tool('get_detection_image',{})
            self.assertFalse(json.loads(result.content[0].text)['success'])
            remote.call_tool.assert_awaited_once_with('get_status',{},meta=None)

    async def test_offline_is_not_retried(self):
        manager=AsyncMock();manager.__aenter__.side_effect=OSError('offline')
        with patch('vision_mcp.MCPServerStreamableHttp',return_value=manager) as factory:
            result=await OnDemandVisionMCP('http://example/mcp').call_tool('detect_object',{'object_description':'bag'})
            self.assertFalse(json.loads(result.content[0].text)['success'])
            self.assertEqual(factory.call_count,1)

    def test_pointing_is_not_motion_or_certainty(self):
        self.assertIn('never call picking',VISION_INSTRUCTIONS)
        self.assertIn('not confidence',VISION_INSTRUCTIONS)

if __name__=='__main__':unittest.main()
