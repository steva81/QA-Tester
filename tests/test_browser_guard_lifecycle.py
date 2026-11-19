"""Browser-owned enforcement must outlive every pending destructive decision."""
import asyncio
import json
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler
import pytest
from playwright.async_api import CDPSession, async_playwright
from app.qa.browser_guard import BrowserMutationGuard, SafetyShutdownError
from app.qa.engine import SmartSmokeQA
from app.qa.full_site import FullSiteQA
from test_full_site import site, isolated_state, browser_case


def test_context_close_clears_correlation_after_retained_shutdown(site, monkeypatch):
    async def check():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(chromium_sandbox=True, headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                user = await context.new_page()
                await user.goto(site.base + '/visible')
                class Probe(SmartSmokeQA):
                    async def _execute_queue(self):
                        page = await context.new_page()
                        await page.goto(site.base + '/visible')
                qa = Probe(site.base, 1, 1, mode='collected', context=context)
                async def unavailable(page):
                    raise SafetyShutdownError('injected target cleanup failure')
                monkeypatch.setattr(qa, '_close_tree', unavailable)
                with pytest.raises(SafetyShutdownError, match='interception retained'):
                    await qa.run()
                assert qa._causality.active
                assert context._qa_safety_hold.active and not user.is_closed()
                await context.close()  # Explicit fixture owner action, not QA recovery.
                assert not qa._causality.active
                assert not qa._causality.receipts and not qa._causality.attempts
                # Context destruction clears diagnostic state, not browser Fetch.
                assert context._qa_safety_hold.active
                assert site.mutations == 0
            finally:
                await browser.close()
    asyncio.run(check())


@pytest.mark.parametrize('closed_during_attach', [True, False])
def test_observer_attach_error_is_ignored_only_for_closed_target(closed_during_attach):
    async def check():
        class Page:
            closed = False
            def is_closed(self):
                return self.closed
        class Context:
            async def new_cdp_session(self, page):
                page.closed = closed_during_attach
                raise RuntimeError('observer attach failure')
        page = Page()
        guard = BrowserMutationGuard(Context(), set(), 'http://127.0.0.1')
        if closed_during_attach:
            await guard.observe(page)
            assert not guard.failed
        else:
            with pytest.raises(RuntimeError, match='observer attach failure'):
                await guard.observe(page)
            assert guard.failed
        assert not guard.pending
    asyncio.run(check())


@pytest.mark.parametrize('operation_kind', ['navigation', 'capture'])
def test_failed_quiescence_does_not_wait_forever_for_pending_read(monkeypatch, tmp_path, operation_kind):
    async def check():
        entered, finished = asyncio.Event(), asyncio.Event()
        async def pending(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        async def preparation_failed(*args):
            raise SafetyShutdownError('injected preparation failure')
        class Session:
            async def send(self, command, params=None):
                if command == 'Page.getLayoutMetrics':
                    return dict(cssContentSize=dict(width=800, height=600),
                                cssLayoutViewport=dict(clientWidth=800, clientHeight=600))
                if command == 'Page.captureScreenshot':
                    return await pending()
            async def detach(self):
                pass
        class Context:
            async def new_cdp_session(self, page):
                return Session()
        engine = FullSiteQA('http://127.0.0.1', 1, 1, context=Context())
        engine._causality = SimpleNamespace(original_pages=set())
        monkeypatch.setattr(engine, '_quiesce_and_close_page', preparation_failed)
        monkeypatch.setattr(SmartSmokeQA, '_navigate_page', pending)
        page = SimpleNamespace(is_closed=lambda: False)
        operation = (engine._navigate_once(page, 'http://127.0.0.1') if operation_kind == 'navigation'
                     else engine._capture_image(page, str(tmp_path / 'unused.png')))
        task = asyncio.create_task(operation)
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(SafetyShutdownError, match='injected preparation failure'):
            await asyncio.wait_for(task, 1)
        assert finished.is_set()
    asyncio.run(check())


@pytest.mark.parametrize('ending', ['normal', 'cancel'])
def test_pending_global_abort_drains_before_disable(site, monkeypatch, ending):
    async def check(context, user):
        # Capture the browser primitive before causal logging adds async work.
        # The mutation must be issued synchronously before this popup closes.
        await context.add_init_script('window.__qaTestNativeFetch = window.fetch')
        entered, release, draining = asyncio.Event(), asyncio.Event(), asyncio.Event()
        events = []
        send, stop = CDPSession.send, BrowserMutationGuard.stop
        async def held(session, command, params=None):
            if command == 'Fetch.failRequest':
                events.append('abort_started')
                entered.set()
                await release.wait()
                result = await send(session, command, params)
                events.append('abort_ack')
                return result
            if command == 'Fetch.disable':
                events.append('disable')
            return await send(session, command, params)
        async def observed_stop(guard):
            draining.set()
            try:
                return await stop(guard)
            except Exception:
                print('Guard shutdown errors:', [str(error) for error in guard.errors])
                raise
        monkeypatch.setattr(CDPSession, 'send', held)
        monkeypatch.setattr(BrowserMutationGuard, 'stop', observed_stop)
        class Probe(SmartSmokeQA):
            async def _execute_queue(self):
                parent = await context.new_page()
                await parent.goto(site.base + '/visible')
                async with parent.expect_popup() as pending:
                    await parent.evaluate("window.open('/visible')")
                popup = await pending.value
                await popup.wait_for_load_state()
                try:
                    await popup.evaluate("() => {window.__qaTestNativeFetch('/write',{method:'DELETE',keepalive:true}).catch(()=>{});window.close();}")
                except Exception:
                    if not popup.is_closed():
                        raise
                await asyncio.wait_for(entered.wait(), 3)
                if ending == 'cancel':
                    raise asyncio.CancelledError()
        run = asyncio.create_task(Probe(site.base, 1, 1, mode='collected', context=context).run())
        try:
            await asyncio.wait_for(draining.wait(), 4)
            assert not run.done()
            assert events == ['abort_started']
            assert site.mutations == 0
        finally:
            release.set()
        if ending == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(run, 4)
        else:
            await asyncio.wait_for(run, 4)
        assert events == ['abort_started', 'abort_ack', 'disable']
        await asyncio.sleep(.15)  # Observation only, never a production barrier.
        assert site.mutations == 0
        assert not getattr(context, '_qa_safety_hold', None)
    asyncio.run(browser_case(site, check))


def test_unresolved_global_abort_returns_bounded_failure_without_disable(site, monkeypatch):
    async def check(context, user):
        await context.add_init_script('window.__qaTestNativeFetch = window.fetch')
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        disabled = []
        send = CDPSession.send
        async def held(session, command, params=None):
            if command == 'Fetch.failRequest':
                entered.set()
                await release.wait()
                try:
                    return await send(session, command, params)
                finally:
                    finished.set()
            if command == 'Fetch.disable':
                disabled.append(True)
            return await send(session, command, params)
        monkeypatch.setattr(CDPSession, 'send', held)
        monkeypatch.setattr(BrowserMutationGuard, 'timeout', .05)
        class Probe(SmartSmokeQA):
            async def _execute_queue(self):
                parent = await context.new_page()
                await parent.goto(site.base + '/visible')
                async with parent.expect_popup() as pending:
                    await parent.evaluate("window.open('/visible')")
                popup = await pending.value
                await popup.wait_for_load_state()
                try:
                    await popup.evaluate("() => {window.__qaTestNativeFetch('/write',{method:'DELETE',keepalive:true}).catch(()=>{});window.close();}")
                except Exception:
                    if not popup.is_closed():
                        raise
                await asyncio.wait_for(entered.wait(), 3)
        try:
            with pytest.raises(SafetyShutdownError, match='interception retained'):
                await asyncio.wait_for(Probe(site.base, 1, 1, mode='collected', context=context).run(), 4)
            assert not disabled and site.mutations == 0
            assert context._qa_safety_hold.active
            assert await user.evaluate("fetch('/visible').then(r=>r.status)") == 200
            with pytest.raises(SafetyShutdownError, match='previous automated safety shutdown'):
                await Probe(site.base, 1, 1, mode='collected', context=context).run()
        finally:
            release.set()
        await asyncio.wait_for(finished.wait(), 3)
        assert site.mutations == 0 and not disabled
    asyncio.run(browser_case(site, check))


@pytest.mark.parametrize('command,hang', [
    ('Emulation.setScriptExecutionDisabled', False), ('Page.stopLoading', False),
    ('Emulation.setScriptExecutionDisabled', True)])
def test_quiescence_failure_retains_target_and_guard_bounded(site, monkeypatch, command, hang):
    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                user = await context.new_page()
                await user.goto(site.base + '/login')
                await user.goto(site.base + '/visible')
                send = CDPSession.send
                async def failed(session, method, params=None):
                    if method == command:
                        if hang:
                            await asyncio.Event().wait()
                        raise RuntimeError('Fixture quiescence failure')
                    return await send(session, method, params)
                monkeypatch.setattr(CDPSession, 'send', failed)
                monkeypatch.setattr(SmartSmokeQA, 'safety_command_timeout', .05)
                class Probe(SmartSmokeQA):
                    async def _execute_queue(self):
                        self.owned = await context.new_page()
                        await self.owned.goto(site.base + '/visible')
                        assert await self.owned.evaluate("fetch('/write',{method:'DELETE'}).then(()=>false).catch(()=>true)")
                qa = Probe(site.base, 1, 1, mode='collected', context=context)
                with pytest.raises(SafetyShutdownError, match='interception retained'):
                    await asyncio.wait_for(qa.run(), 4)
                assert not qa.owned.is_closed() and not user.is_closed()
                assert context._qa_safety_hold.active
                if command != 'Page.stopLoading':
                    assert await qa.owned.evaluate("fetch('/write',{method:'DELETE'}).then(()=>false).catch(()=>true)")
                assert site.mutations == 0
                assert await user.evaluate("fetch('/visible').then(r=>r.status)") == 200
                assert any(c['name'] == 'qa_login' for c in await context.cookies())
            finally:
                await browser.close()
    asyncio.run(check())


def test_manual_headers_and_post_are_preserved_while_automated_guard_active(site, monkeypatch):
    seen = []
    send_response = BaseHTTPRequestHandler.send_response
    def recorded(handler, *args, **kwargs):
        if handler.path == '/visible?manual=1':
            seen.append((handler.headers.get('X-QA-Automated-Context'), handler.headers.get('X-Existing')))
        return send_response(handler, *args, **kwargs)
    monkeypatch.setattr(BaseHTTPRequestHandler, 'send_response', recorded)
    async def check(context, user):
        await context.set_extra_http_headers({'X-Existing': 'preserve'})
        class Probe(SmartSmokeQA):
            async def _execute_queue(self):
                page = await context.new_page()
                await page.goto(site.base + '/visible')
                assert await page.evaluate("fetch('/write',{method:'DELETE'}).then(()=>false).catch(()=>true)")
                assert site.mutations == 0
                assert await user.evaluate("fetch('/visible?manual=1').then(r=>r.status)") == 200
                assert await user.evaluate("fetch('/write',{method:'POST'}).then(r=>r.status)") == 200
                assert site.mutations == 1  # Explicitly manual localhost write.
        await Probe(site.base, 1, 1, mode='collected', context=context).run()
        await user.evaluate("fetch('/visible?manual=1')")
        assert seen == [(None, 'preserve'), (None, 'preserve')]
    asyncio.run(browser_case(site, check))


def test_route_ownership_does_not_wait_for_renderer_enable(site, monkeypatch):
    async def check(context, user):
        enabled = False
        entered, release = asyncio.Event(), asyncio.Event()
        send = CDPSession.send
        async def held(session, command, params=None):
            if enabled and command == 'Page.enable':
                entered.set()
                await release.wait()
            return await send(session, command, params)
        monkeypatch.setattr(CDPSession, 'send', held)
        class Probe(SmartSmokeQA):
            async def _execute_queue(self):
                nonlocal enabled
                enabled = True
                page = await context.new_page()
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    await page.goto(site.base + '/visible', timeout=3000)
                    assert await page.evaluate("fetch('/write',{method:'DELETE'}).then(()=>false).catch(()=>true)")
                    assert site.mutations == 0
                finally:
                    release.set()
        await Probe(site.base, 1, 1, mode='collected', context=context).run()
    asyncio.run(browser_case(site, check))


def test_manual_popup_and_frame_remain_manual(site):
    async def check(context, user):
        popup = None
        class Probe(SmartSmokeQA):
            async def _execute_queue(self):
                nonlocal popup
                async with user.expect_popup() as created:
                    await user.evaluate("window.open('/visible')")
                popup = await created.value
                await popup.wait_for_load_state()
                assert await popup.evaluate("fetch('/write',{method:'POST'}).then(r=>r.status)") == 200
                assert await user.evaluate("""async () => {
                    const frame = document.createElement('iframe');
                    document.body.append(frame);
                    try { return (await frame.contentWindow.fetch('/write',{method:'POST'})).status; }
                    finally { frame.remove(); }
                }""") == 200
        await Probe(site.base, 1, 1, mode='collected', context=context).run()
        assert site.mutations == 2  # Explicit manual writes only.
        assert not popup.is_closed() and popup in context.pages
        await popup.close()
    asyncio.run(browser_case(site, check))


@pytest.mark.parametrize('redirect', [None, (307, '/write'), (303, '/logout')])
@pytest.mark.parametrize('sequence', ['single', 'concurrent', 'sequential'])
def test_self_closing_query_is_forwarded_once_without_redirect(site, monkeypatch, redirect, sequence, close_timing='paused'):
    reads = []
    def post(handler):
        payload = json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
        if handler.path != '/api/proxy/graphql' or payload != {'query': '{ viewer { id } }'}:
            site.mutations += 1
        reads.append(handler.path)
        handler.send_response(redirect[0] if redirect else 200)
        if redirect:
            handler.send_header('Location', redirect[1])
        handler.send_header('Content-Length', '0')
        handler.end_headers()
    monkeypatch.setattr(site.server.RequestHandlerClass, 'do_POST', post)
    async def check(context, user):
        if close_timing == 'immediate':
            await context.add_init_script('window.__qaTestNativeFetch = window.fetch')
        expected = {'single': 1, 'concurrent': 3, 'sequential': 2}[sequence]
        entered, release = asyncio.Event(), asyncio.Event()
        native_invocations = []
        if close_timing == 'immediate':
            async def lifecycle_entered(source, calls):
                native_invocations.append(calls)
                assert calls == expected
                await asyncio.wait_for(entered.wait(), 3)
            await context.expose_binding('__qaTestQueryEntered', lifecycle_entered)
        forwarding = 0
        forward = BrowserMutationGuard._forward_query
        async def held_query(guard, request, route):
            nonlocal forwarding
            forwarding += 1
            if forwarding == expected:
                entered.set()
                await release.wait()
            return await forward(guard, request, route)
        monkeypatch.setattr(BrowserMutationGuard, '_forward_query', held_query)
        class Probe(SmartSmokeQA):
            async def _execute_queue(self):
                parent = await context.new_page()
                await parent.goto(site.base + '/visible')
                async with parent.expect_popup() as pending:
                    await parent.evaluate("window.open('/visible')")
                page = await pending.value
                await page.wait_for_load_state()
                try:
                    await page.evaluate("""({sequence, closeSelf}) => { (async () => {
                        let nativeCalls = 0;
                        const query = () => {
                            const promise = (window.__qaTestNativeFetch || window.fetch)('/api/proxy/graphql', {method:'POST',keepalive:true,
                                headers:{'Content-Type':'application/json'},body:JSON.stringify({query:'{ viewer { id } }'})});
                            nativeCalls++;
                            return promise.catch(()=>{});
                        };
                        if (sequence === 'sequential') await query();
                        for (let i=0; i < (sequence === 'concurrent' ? 3 : 1); i++) query();
                        if (closeSelf) {
                            // Native invocation alone is not lifecycle entry:
                            // Chromium can discard it when closing this target.
                            await window.__qaTestQueryEntered(nativeCalls);
                            window.close();
                        }
                    })(); }""", {'sequence': sequence, 'closeSelf': close_timing == 'immediate'})
                    # Causal instrumentation can defer native fetch. Confirm the
                    # final request reached CDP before closing its originating
                    # target, then let that already-paused query be forwarded.
                    if close_timing == 'paused':
                        await asyncio.wait_for(entered.wait(), 3)
                        await page.evaluate('window.close()')
                    else:
                        if not page.is_closed():
                            await page.wait_for_event('close', timeout=3000)
                except Exception:
                    if not page.is_closed():
                        raise
                finally:
                    release.set()
        await Probe(site.base, 1, 1, mode='collected', context=context).run()
        if close_timing == 'immediate':
            assert native_invocations == [expected]
        assert forwarding == expected
        assert reads == ['/api/proxy/graphql'] * expected, 'The two interception layers duplicated or coalesced queries'
        assert site.mutations == 0 and site.counts['/logout'] == 0 and site.counts['/write'] == 0
    asyncio.run(browser_case(site, check))


@pytest.mark.parametrize('redirect', [None, (307, '/write'), (303, '/logout')])
def test_immediate_native_query_self_close_forwards_exactly_once(site, monkeypatch, redirect):
    test_self_closing_query_is_forwarded_once_without_redirect(
        site, monkeypatch, redirect, 'sequential', close_timing='immediate')
