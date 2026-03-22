"""In-memory queue for Agent B.

Candidates flow from the rule engine into this queue.
Downstream consumers (Agent B) read from it.
"""
import asyncio


class CandidateQueue:
    def __init__(self, max_size: int = 100):
        self._queue = asyncio.Queue(maxsize=max_size)

    async def put(self, candidate):
        await self._queue.put(candidate)

    async def get(self):
        return await self._queue.get()

    def qsize(self) -> int:
        return self._queue.qsize()
