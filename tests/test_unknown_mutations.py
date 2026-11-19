"""Unknown request ownership must never make the mutation policy permissive."""
import asyncio
import pytest
from app.qa.engine import SmartSmokeQA


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
def test_missing_frame_ownership_still_aborts(method):
    class Request:
        @property
        def frame(self):
            raise RuntimeError('Frame is not available yet')
    request = Request()
    request.method = method
    class Route:
        aborted = False
        async def abort(self): self.aborted = True
        async def fallback(self): raise AssertionError('Unknown mutation failed open')
    route = Route()
    route.request = request
    class Context:
        pages = []
        service_workers = []
        async def add_init_script(self, script): pass
        def on(self, *args): pass
        def remove_listener(self, *args): pass
        async def route(self, pattern, handler): await handler(route)
        async def unroute(self, *args): pass
    async def check():
        await SmartSmokeQA('https://example.test/', 1, 1, mode='collected', context=Context()).run()
    asyncio.run(check())
    assert route.aborted


def test_cancel_during_guard_installation_removes_guard():
    async def check():
        installed = asyncio.Event()
        class Context:
            pages = []
            service_workers = []
            handler = None
            async def add_init_script(self, script): pass
            def on(self, *args): pass
            def remove_listener(self, *args): pass
            async def route(self, pattern, handler):
                self.handler = handler
                installed.set()
                await asyncio.sleep(600)
            async def unroute(self, pattern, handler):
                assert self.handler is handler
                self.handler = None
        context = Context()
        task = asyncio.create_task(SmartSmokeQA('https://example.test/', 1, 1, mode='collected', context=context).run())
        await installed.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert context.handler is None
        assert not context._qa_run_lock.locked()
    asyncio.run(check())
