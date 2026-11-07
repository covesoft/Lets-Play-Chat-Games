"""One bounded FIFO per live session. Admission to the queue never awaits."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class LaneUnavailable(RuntimeError):
    pass


class ReceiptClock:
    """Anchor UTC once; elapsed receipts cannot regress after a wall-clock change."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.anchor = datetime.now(UTC)
        self.started = self.loop.time()

    def now(self):
        return self.anchor + timedelta(seconds=self.loop.time() - self.started)


@dataclass(slots=True)
class Request:
    action: str
    data: dict
    received_at: datetime
    result: asyncio.Future


class Lane:
    def __init__(self, handler, on_fault, clock, *, capacity=64):
        self.handler, self.on_fault, self.clock = handler, on_fault, clock
        self.queue = asyncio.Queue(maxsize=capacity)
        self.fenced = False
        self.closed = False
        self.task = asyncio.create_task(self._run())

    def submit(self, action, **data):
        future = asyncio.get_running_loop().create_future()
        if self.closed or self.fenced:
            future.set_exception(LaneUnavailable("This game is paused or closed."))
            return future
        try:
            self.queue.put_nowait(Request(action, data, self.clock.now(), future))
        except asyncio.QueueFull:
            self.fenced = True
            future.set_exception(LaneUnavailable("Game intake is full; recovery is required."))
        return future

    async def _run(self):
        try:
            while True:
                request = await self.queue.get()
                try:
                    result = await self.handler(request)
                    if not request.result.done():
                        request.result.set_result(result)
                except asyncio.CancelledError:
                    if not request.result.done():
                        request.result.set_exception(LaneUnavailable("Game worker stopped."))
                    raise
                except Exception as exc:
                    self.fenced = True
                    if not request.result.done():
                        request.result.set_exception(
                            LaneUnavailable("Game paused after a processing failure.")
                        )
                    # Never propagate content-bearing exception strings to logging.
                    await self.on_fault(type(exc).__name__)
                finally:
                    self.queue.task_done()
                del request
                if self.fenced:
                    while not self.queue.empty():
                        pending = self.queue.get_nowait()
                        if not pending.result.done():
                            pending.result.set_exception(LaneUnavailable("Game intake is paused."))
                        self.queue.task_done()
                    await self.on_fault("intake_fenced")
                    return
        finally:
            while not self.queue.empty():
                request = self.queue.get_nowait()
                if not request.result.done():
                    request.result.set_exception(LaneUnavailable("Game worker stopped."))
                self.queue.task_done()

    async def close(self):
        self.closed = True
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
