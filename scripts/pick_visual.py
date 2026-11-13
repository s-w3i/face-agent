"""Cosmetic display lease; never changes robot input or commands hardware."""
import asyncio
import sys
from dotsctl import request

REFRESH_SECONDS=3.

class PickVisualSender:
    def __init__(self,url):
        self.url=url;self.mission_id=None;self.changed=asyncio.Event();self.stopping=False;self.task=None
    def start(self):self.task=asyncio.create_task(self.run())
    def set(self,mission_id):
        if mission_id!=self.mission_id:
            self.mission_id=mission_id;self.changed.set()
    async def send(self,mission_id,active):
        try:
            await asyncio.to_thread(request,self.url,'pick-visual',{'mission_id':mission_id,'active':active},timeout=2)
        except (ValueError,OSError,TimeoutError) as error:
            print('Picking visual unavailable: '+str(error),file=sys.stderr,flush=True)
    async def run(self):
        last=None
        try:
            while not self.stopping:
                self.changed.clear();target=self.mission_id
                if last and last!=target:
                    await self.send(last,False);last=None
                if target:
                    last=target;await self.send(target,True)
                try:await asyncio.wait_for(self.changed.wait(),REFRESH_SECONDS)
                except TimeoutError:pass
        finally:
            if last:await self.send(last,False)
    async def close(self):
        self.stopping=True;self.changed.set()
        if self.task:await self.task
