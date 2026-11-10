"""Expose local tool definitions; connect to the remote robot only on a tool call."""
from contextlib import AsyncExitStack
import json
import re
from pathlib import Path

from agents.mcp import MCPServer
from mcp import types


class OnDemandNavigationMCP(MCPServer):
    def __init__(self, endpoint, connector, *, require_mock=False):
        super().__init__()
        self.endpoint = endpoint
        self.connector = connector
        self.require_mock = require_mock
        definitions = json.loads(Path(__file__).with_name('navigation_tool_manifest.json').read_text())
        if require_mock:
            definitions = [tool for tool in definitions if tool['name'] in
                           ('navigate_to_region', 'get_robot_pose', 'cancel_navigation')]
        self._tools = [types.Tool.model_validate(tool) for tool in definitions]

    @property
    def name(self):
        return 'Robot navigation (on demand)'

    @property
    def cached_tools(self):
        return self._tools

    async def connect(self):
        # This adapter is local. Remote connection is owned by call_tool's task.
        pass

    async def cleanup(self):
        pass

    async def list_tools(self, run_context=None, agent=None):
        return self._tools

    async def list_prompts(self):
        return types.ListPromptsResult(prompts=[])

    async def get_prompt(self, name, arguments=None):
        raise ValueError('Navigation does not provide prompts.')

    @staticmethod
    def error(code, message, *, request_sent=False):
        payload = dict(success=False, error=code, message=message, request_sent=request_sent)
        return types.CallToolResult.model_validate(dict(
            isError=True, content=[dict(type='text', text=json.dumps(payload))],
            structuredContent=payload))

    @staticmethod
    def enrollment_result(result):
        payload = getattr(result, 'structured_content', None)
        if not isinstance(payload, dict):
            try:
                payload = json.loads(next(part.text for part in result.content if part.type == 'text'))
            except (StopIteration, ValueError, AttributeError, TypeError):
                payload = {}
        if not isinstance(payload, dict): payload = {}
        error = payload.get('error')
        code = error.get('code') if isinstance(error, dict) else error
        if payload.get('success') is True:
            status = 'confirmed'
        elif isinstance(code, str) and re.fullmatch(r'[A-Z_]{1,64}', code):
            status = code
        else:
            status = 'unconfirmed'
        print('Enrollment MCP result: ' + status, flush=True)
        return result

    async def call_tool(self, tool_name, arguments, meta=None):
        if tool_name not in {tool.name for tool in self._tools}:
            return self.error('TOOL_NOT_AVAILABLE', 'This navigation tool is not configured.')
        sent = False
        result = None
        try:
            # Connect, call once, and disconnect in the same task. There is no
            # background probe or automatic replay of movement after an outage.
            async with AsyncExitStack() as stack:
                remote = await self.connector(stack, self.endpoint, require_mock=self.require_mock)
                sent = True
                result = await remote.call_tool(tool_name, arguments or {}, meta=meta)
            return self.enrollment_result(result) if tool_name == 'remember_person' else result
        except Exception:
            if result is not None:
                return self.enrollment_result(result) if tool_name == 'remember_person' else result
            if sent:
                failure = self.error('NAVIGATION_RESULT_UNKNOWN',
                    'The connection failed during the request. Its outcome is unconfirmed. '
                    'Do not claim arrival or automatically repeat movement; check the robot first.',
                    request_sent=True)
                return self.enrollment_result(failure) if tool_name == 'remember_person' else failure
            failure = self.error('NAVIGATION_SERVER_UNAVAILABLE',
                'The remote navigation server is unavailable. No navigation command was sent. '
                'Tell the user navigation is unavailable; normal conversation can continue. '
                'A later explicit navigation request can try connecting again.')
            return self.enrollment_result(failure) if tool_name == 'remember_person' else failure
