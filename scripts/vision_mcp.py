"""On-demand Jetson vision MCP; does not block face-agent startup."""
from contextlib import AsyncExitStack
import json
from agents.mcp import MCPServer, MCPServerStreamableHttp
from mcp import types

VISION_INSTRUCTIONS = (
    'Jetson vision MCP is available separately from navigation. For locating an object or asking '
    'which object the user is pointing at, call detect_object with the full visual relation, '
    'for example "the bag the person is pointing at". For pointing, first use look_at_camera '
    'for full-scene context; require a visible person, pointing arm/hand, and plausible target. '
    'If the gesture or intended person is ambiguous, ask for a clearer pose instead of guessing. '
    'After a successful detection call get_detection_image and inspect the crop before describing '
    'the selected bag by visible color/type or distinguishing features. A segmentation score is '
    'not confidence that the pointing intention was correctly understood. Do not claim a verified '
    'pointing ray: selection is visual reasoning, not geometric gesture tracking. '
    'On failure say the target could not be confirmed and ask the user to keep the arm and bag '
    'visible. Do not use an old crop as a new result. "Which bag am I pointing at?" is only a '
    'visual question: never call picking, approach, navigation, or following tools for it. '
    'The detection result is not the navigation confirm_object contract and cannot authorize motion. '
)


class OnDemandVisionMCP(MCPServer):
    def __init__(self, endpoint):
        super().__init__()
        self.endpoint = endpoint
        self.last_observation = None
        self._tools = [types.Tool.model_validate(dict(name=name, description=description,
            inputSchema=schema)) for name, description, schema in [
            ('detect_object', 'Locate one described object using the head camera, including pointing relations. No motion. Replaces the active object TF; may take 180 seconds.',
             {'type':'object','properties':{'object_description':{'type':'string'}},'required':['object_description'],'additionalProperties':False}),
            ('get_status', 'Check Jetson vision service availability; does not run detection.',
             {'type':'object','properties':{},'additionalProperties':False}),
            ('get_detection_image', 'View the crop of this conversation\'s latest successful detection. Run detect_object first.',
             {'type':'object','properties':{},'additionalProperties':False}),
        ]]

    @property
    def name(self): return 'Jetson object vision (on demand)'
    @property
    def cached_tools(self): return self._tools
    async def connect(self): pass
    async def cleanup(self): pass
    async def list_tools(self, run_context=None, agent=None): return self._tools
    async def list_prompts(self): return types.ListPromptsResult(prompts=[])
    async def get_prompt(self, name, arguments=None): raise ValueError('Vision has no prompts')

    @staticmethod
    def payload(result):
        return json.loads(next(part.text for part in result.content if part.type == 'text'))

    @staticmethod
    def error(message):
        return types.CallToolResult.model_validate({'isError':True,
            'content':[{'type':'text','text':json.dumps({'success':False,'message':message})}]})

    async def call_tool(self, tool_name, arguments, meta=None):
        if tool_name not in {tool.name for tool in self._tools}:
            return self.error('Unknown vision tool')
        if tool_name == 'detect_object': self.last_observation = None
        if tool_name == 'get_detection_image' and self.last_observation is None:
            return self.error('No successful detection in this conversation; do not use an old crop')
        result = None
        try:
            async with AsyncExitStack() as stack:
                remote = await stack.enter_async_context(MCPServerStreamableHttp(
                    name='Jetson object vision', cache_tools_list=True,
                    params={'url':self.endpoint,'timeout':10,'sse_read_timeout':210},
                    client_session_timeout_seconds=210, max_retry_attempts=0))
                if tool_name == 'get_detection_image':
                    status = self.payload(await remote.call_tool('get_status', {}, meta=meta))
                    if status.get('crop_stamp') != self.last_observation:
                        return self.error('The crop has changed since your detection; do not describe it as your target')
                print('Vision MCP · ' + tool_name, flush=True)
                result = await remote.call_tool(tool_name, arguments or {}, meta=meta)
                if tool_name == 'detect_object':
                    payload = self.payload(result)
                    if payload.get('success') is True:
                        self.last_observation = payload.get('observation_stamp')
                    print('Vision MCP · detection success=' + str(payload.get('success')),
                          'object=' + str(payload.get('object_name', '')), flush=True)
            return result
        except Exception:
            if result is not None: return result
            return self.error('Vision MCP unavailable or response interrupted. Detection outcome is unconfirmed; do not claim success or automatically retry.')
