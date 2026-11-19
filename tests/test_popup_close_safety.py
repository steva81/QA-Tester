"""A target must stop producing requests before its interception session dies."""
import asyncio
from http.server import BaseHTTPRequestHandler
import threading
from types import SimpleNamespace
import pytest
from playwright.async_api import Page, Route, CDPSession
from app.qa.causality import BlockCausality
from app.qa.engine import SmartSmokeQA
from app.qa.full_site import FullSiteQA
from test_mutation_shutdown import test_server_receives_zero_automated_mutations as existing_mutation_case
from test_full_site import site, isolated_state, browser_case


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
def test_pending_popup_read_cannot_release_mutation_during_close(monkeypatch, method):
    """Use the original fixture/assertions; align the grandchild read with close.

    The parent's mutation is exercised and aborted normally. The grandchild's
    GET response is withheld until Page.close, reproducing the real race without
    permitting any request or changing the production route handler.
    """
    arrived = threading.Event()
    release = threading.Event()
    responded = threading.Event()
    end_headers = BaseHTTPRequestHandler.end_headers

    def held_response(handler):
        held = handler.path == '/attempt' and handler.headers.get('Referer', '').endswith('/grandchild')
        if held:
            arrived.set()
            assert release.wait(10), 'Grandchild close boundary was not exercised'
        try:
            return end_headers(handler)
        finally:
            if held:
                responded.set()
    monkeypatch.setattr(BaseHTTPRequestHandler, 'end_headers', held_response)

    finish = BlockCausality.finish_page_bindings
    async def ready_to_close(causality, page):
        if page.url.endswith('/grandchild'):
            assert await asyncio.to_thread(arrived.wait, 3), 'Pending read was not exercised'
        await finish(causality, page)
    monkeypatch.setattr(BlockCausality, 'finish_page_bindings', ready_to_close)

    close = Page.close
    async def release_at_close(page, *args, **kwargs):
        if page.url.endswith('/grandchild'):
            release.set()
            assert await asyncio.to_thread(responded.wait, 3)
        return await close(page, *args, **kwargs)
    monkeypatch.setattr(Page, 'close', release_at_close)
    try:
        existing_mutation_case(method, 'extraction_popup')
        assert arrived.is_set() and release.is_set() and responded.is_set()
    finally:
        release.set()


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
def test_paused_mutation_is_cancelled_before_target_destroyed(site, monkeypatch, method):
    """Hold the abort RPC to deterministically exercise an in-flight request."""
    async def check(context, user):
        intercepted = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()
        abort = Route.abort
        async def delayed_abort(route, *args, **kwargs):
            if route.request.url == site.base+'/write':
                intercepted.set()
                await release.wait()
                try:
                    return await abort(route, *args, **kwargs)
                finally:
                    finished.set()
            return await abort(route, *args, **kwargs)
        monkeypatch.setattr(Route, 'abort', delayed_abort)
        class Probe(SmartSmokeQA):
            async def _execute_queue(self):
                page = await self.provided_context.new_page()
                await page.goto(site.base+'/visible')
                await page.evaluate("method => { fetch('/write',{method}).catch(()=>{}); }", method)
                await asyncio.wait_for(intercepted.wait(), 3)
                try:
                    await self._close_tree(page)
                finally:
                    release.set()
                await asyncio.wait_for(finished.wait(), 3)
        await Probe(site.base, 1, 1, mode='collected', context=context).run()
        # Allow late fixture receipt to be observed; never retry a failed check.
        await asyncio.sleep(.2)
        assert intercepted.is_set() and finished.is_set()
        assert site.mutations == 0, 'Target destruction released a paused mutation'
    asyncio.run(browser_case(site, check))


@pytest.mark.parametrize('failed_command', ['Emulation.setScriptExecutionDisabled', 'Page.stopLoading'])
def test_failed_quiescence_must_not_destroy_interception_target(failed_command):
    closed = []
    detached = []
    class Session:
        async def send(self, command, params=None):
            if command == failed_command:
                raise RuntimeError('Fixture protocol failure')
        async def detach(self): detached.append(True)
    class Context:
        async def new_cdp_session(self, page): return Session()
    async def close(): closed.append(True)
    page = SimpleNamespace(context=Context(), is_closed=lambda: False, close=close)
    async def check():
        engine = SmartSmokeQA('https://example.test', 1, 1)
        with pytest.raises(RuntimeError, match='Fixture protocol failure'):
            await engine._quiesce_and_close_page(page)
        assert not closed, 'Failed preparation must not release intercepted traffic by destroying its target'
        assert detached
    asyncio.run(check())


@pytest.mark.parametrize('ending', ['cancel', 'timeout'])
def test_interrupted_capture(site, monkeypatch, tmp_path, ending):
    """Capture interruption must preserve its exception and quiesce before close."""
    async def check(context, user):
        entered = asyncio.Event()
        closed = asyncio.Event()
        ordering = []
        send = CDPSession.send
        async def traced(session, method, params=None):
            if method == 'Page.captureScreenshot':
                entered.set()
                await closed.wait()
                raise RuntimeError('Fixture capture released by target close')
            result = await send(session, method, params)
            if method in ('Emulation.setScriptExecutionDisabled', 'Page.stopLoading'):
                ordering.append(method)
            return result
        monkeypatch.setattr(CDPSession, 'send', traced)
        class Probe(FullSiteQA):
            async def _execute_queue(self):
                page = await self.provided_context.new_page()
                await page.goto(site.base + '/visible')
                page.on('close', lambda: (ordering.append('closed'), closed.set()))
                task = asyncio.create_task(self._capture_image(page, str(tmp_path / 'unused.png')))
                await asyncio.wait_for(entered.wait(), 3)
                if ending == 'cancel':
                    task.cancel()
                with pytest.raises(asyncio.CancelledError if ending == 'cancel' else asyncio.TimeoutError):
                    await asyncio.wait_for(task, 8)
                assert closed.is_set()
                assert ordering == ['Emulation.setScriptExecutionDisabled', 'Page.stopLoading', 'closed']
        await Probe(site.base, 1, 1, context=context, delay=0).run()
        assert site.mutations == 0
    asyncio.run(browser_case(site, check))

"""Temporary localhost stress probes; production policy is unchanged."""

SCRIPTS = {
    'fetch': "fetch('/write',{method:'DELETE'}).catch(()=>{}); window.close();",
    'keepalive': "fetch('/write',{method:'DELETE',keepalive:true}).catch(()=>{}); window.close();",
    'xhr': "const x=new XMLHttpRequest();x.open('DELETE','/write');x.send();window.close();",
    'beacon': "navigator.sendBeacon('/write','fixture');window.close();",
    'pagehide': "addEventListener('pagehide',()=>navigator.sendBeacon('/write','fixture'));location.href='about:blank';",
    'iframe': "const f=document.createElement('iframe');document.body.append(f);f.contentWindow.fetch('/write',{method:'DELETE',keepalive:true}).catch(()=>{});f.remove();window.close();",
}

@pytest.mark.parametrize('attempt', range(5))
@pytest.mark.parametrize('kind', list(SCRIPTS))
def test_closing_popup(site, kind, attempt):
    async def check(context, user):
        class Probe(SmartSmokeQA):
            async def _execute_queue(self):
                page=await self.provided_context.new_page()
                await page.goto(site.base+'/visible')
                async with page.expect_popup() as created:
                    await page.evaluate("window.open('/visible')")
                popup=await created.value
                await popup.wait_for_load_state()
                try: await popup.evaluate('() => {'+SCRIPTS[kind]+'}')
                except Exception:
                    if not popup.is_closed(): raise
                await asyncio.sleep(.25)
        await Probe(site.base,1,1,mode='collected',context=context).run()
        await asyncio.sleep(.25)
        assert site.mutations == 0, (kind, attempt, site.mutations)
    asyncio.run(browser_case(site,check))

@pytest.mark.parametrize('attempt', range(5))
@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
@pytest.mark.parametrize('opener', ['popup', 'top'])
def test_self_close_with_paused_mutation(site, monkeypatch, opener, method, attempt):
    async def check(context, user):
        intercepted = asyncio.Event()
        release = asyncio.Event()
        abort = Route.abort

        async def held_abort(route, *args, **kwargs):
            if route.request.url == site.base + '/write':
                intercepted.set()
                await release.wait()
            return await abort(route, *args, **kwargs)
        monkeypatch.setattr(Route, 'abort', held_abort)

        class Probe(SmartSmokeQA):
            async def _execute_queue(self):
                page = await self.provided_context.new_page()
                await page.goto(site.base + '/visible')
                target = page
                if opener == 'popup':
                    async with page.expect_popup() as created:
                        await page.evaluate("window.open('/visible')")
                    target = await created.value
                    await target.wait_for_load_state()
                await target.evaluate("m => { fetch('/write',{method:m}).catch(()=>{}); }", method)
                await asyncio.wait_for(intercepted.wait(), 3)
                try:
                    await target.evaluate("() => { setTimeout(() => window.close(), 0); }")
                except Exception:
                    pass
                for _ in range(40):
                    if target.is_closed():
                        break
                    await asyncio.sleep(.05)
                await asyncio.sleep(.3)
                release.set()
                await asyncio.sleep(.2)
        await Probe(site.base, 1, 1, mode='collected', context=context).run()
        await asyncio.sleep(.3)
        assert intercepted.is_set()
        assert site.mutations == 0, (opener, method, attempt, site.mutations)
    asyncio.run(browser_case(site, check))
