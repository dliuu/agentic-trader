"""In-memory queue for Agent B.

Candidates flow from the rule engine into this queue.
Downstream consumers (Agent B) read from it.
"""
import asyncio


class CandidateQueue:
    def __init__(self, max_size: int = 100):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_size)

    async def put(self, candidate):
        """Add a candidate. Drops oldest if full."""
        try:
            self._queue.put_nowait(candidate)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait(candidate)

    async def get(self):
        """Get next candidate. Blocks until available."""
        return await self._queue.get()

    def get_nowait(self):
        """Get next candidate without blocking. Raises QueueEmpty if empty."""
        return self._queue.get_nowait()

    @property
    def size(self) -> int:
        return self._queue.qsize()
