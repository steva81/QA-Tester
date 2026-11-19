"""Headful browser capability and interference tests, not headless UI claims."""
import asyncio
import json
import os
from pathlib import Path
import pytest
from test_full_site import site, isolated_state


class HiddenTargetSession:
    """CDP transport for a hidden target that Playwright does not expose as Page."""
    def __init__(self, control, session_id):
        self.control, self.session_id = control, session_id
        self.sequence = 0
        self.pending = {}
        control.on('Target.receivedMessageFromTarget', self.receive)

    def receive(self, event):
        if event.get('sessionId') != self.session_id:
            return
        message = json.loads(event['message'])
        future = self.pending.get(message.get('id'))
        if future is not None and not future.done():
            future.set_result(message)

    async def send(self, method, params=None):
        self.sequence += 1
        ident = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.control.send('Target.sendMessageToTarget', {
                'sessionId': self.session_id,
                'message': json.dumps({'id': ident, 'method': method, 'params': params or {}})})
            response = await asyncio.wait_for(future, 5)
            assert 'error' not in response, response
            return response.get('result', {})
        finally:
            self.pending.pop(ident, None)

    async def value(self, expression):
        response = await self.send('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
        assert 'exceptionDetails' not in response, response
        return response['result'].get('value')

    async def detach(self):
        self.control.remove_listener('Target.receivedMessageFromTarget', self.receive)
        await self.control.send('Target.detachFromTarget', {'sessionId': self.session_id})


def test_headful_hidden_target_capability(site, tmp_path):
    async def check():
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser=await p.chromium.launch(chromium_sandbox=True, headless=False)
            try:
                context=await browser.new_context(no_viewport=True,service_workers='block')
                user=await context.new_page()
                await user.goto(site.base+'/login');await user.goto(site.base+'/visible')
                await user.bring_to_front()
                await user.evaluate("localStorage.setItem('auth_fixture','shared');sessionStorage.setItem('tab_fixture','private')")
                identity=await context.new_cdp_session(user)
                await identity.send('Emulation.setFocusEmulationEnabled',dict(enabled=False))
                info=(await identity.send('Target.getTargetInfo'))['targetInfo']
                control=await browser.new_browser_cdp_session()
                observations=[]
                for options in ({'background':True},{'background':True,'hidden':True},
                                {'background':True,'hidden':True,'forTab':False},
                                {'background':True,'newWindow':True,'windowState':'minimized'}):
                    if options.get('hidden'):
                        created=await control.send('Target.createTarget',dict(url='about:blank',browserContextId=info['browserContextId'],**options))
                        attached = await control.send('Target.attachToTarget', dict(targetId=created['targetId'], flatten=False))
                        target = HiddenTargetSession(control, attached['sessionId'])
                        try:
                            navigation = await target.send('Page.navigate', {'url': site.base+'/visible'})
                            assert not navigation.get('errorText'), navigation
                            deadline = asyncio.get_running_loop().time() + 5
                            while await target.value("location.href + '|' + document.readyState") != site.base+'/visible|complete':
                                assert asyncio.get_running_loop().time() < deadline, 'Hidden target did not load'
                                await asyncio.sleep(0.05)
                            assert await target.value("localStorage.getItem('auth_fixture')") == 'shared'
                            assert await target.value("sessionStorage.getItem('tab_fixture')") is None
                            assert await user.evaluate("sessionStorage.getItem('tab_fixture')") == 'private'
                        finally:
                            await target.detach()
                        try:
                            window=await control.send('Browser.getWindowForTarget',dict(targetId=created['targetId']))
                            hidden=False
                        except Exception as exc:
                            assert any(p in str(exc).lower() for p in ('no browser window', 'browser window not found')), str(exc)
                            window=None;hidden=True
                        assert await user.evaluate('document.visibilityState')=='visible'
                        observations.append(dict(options=options,hidden=hidden,window=window))
                        await control.send('Target.closeTarget',dict(targetId=created['targetId']))
                        assert created['targetId'] not in [t['targetId'] for t in (await control.send('Target.getTargets'))['targetInfos']]
                    else:
                        async with context.expect_page() as event:
                            created=await control.send('Target.createTarget',dict(url='about:blank',browserContextId=info['browserContextId'],**options))
                        page=await event.value
                        await page.goto(site.base+'/visible')
                        try:
                            window=await control.send('Browser.getWindowForTarget',dict(targetId=created['targetId']))
                            hidden=False
                        except Exception as exc:
                            assert any(p in str(exc).lower() for p in ('no browser window', 'browser window not found')), str(exc)
                            window=None;hidden=True
                        assert await user.evaluate('document.visibilityState')=='visible'
                        assert await page.evaluate("localStorage.getItem('auth_fixture')")=='shared'
                        assert await page.evaluate("sessionStorage.getItem('tab_fixture')") is None
                        observations.append(dict(options=options,hidden=hidden,window=window))
                        await page.close()
                artifact=Path(os.environ.get('QA_SECURITY_EVIDENCE_DIR', str(tmp_path)))/'hidden_target_capability.json'
                artifact.write_text(json.dumps(dict(version=await control.send('Browser.getVersion'),targets=observations),indent=2))
                assert context.pages==[user] and user.url==site.base+'/visible'
                await control.detach();await identity.detach()
            finally:await browser.close()
    asyncio.run(check())


@pytest.mark.parametrize('ending',['complete','limit','auth','failure','cancel_nav','cancel_mobile','end_session','shutdown','browser_close'])
def test_headful_manual_tab_and_target_cleanup(site,ending):
    import threading
    from test_full_site import seed,live_session,run_worker,worker_status
    from app.routes import api
    from server import app
    pid,rid=seed(site)
    session=live_session(site,pid,headless=False)
    entered=threading.Event()
    before={}
    focus_observations=[]
    async def capture():
        await session.page.bring_to_front()
        control=await session.context.new_cdp_session(session.page)
        # Playwright normally emulates focus and visibility in every target.
        # Disable it on the observer so these assertions measure the selected
        # manual tab, rather than a synthetic always-visible value.
        await control.send('Emulation.setFocusEmulationEnabled',dict(enabled=False))
        before['bounds']=await control.send('Browser.getWindowForTarget')
        before['size']=await session.page.evaluate('() => [innerWidth,innerHeight]')
        assert await session.page.evaluate("String(window.focus).includes('[native code]')")
        await control.detach()
    asyncio.run_coroutine_threadsafe(capture(),session.loop).result(3)
    site.script='''<script>
      window.focus();
      if(Window.prototype.focus)Window.prototype.focus.call(window);
      document.querySelector('#field').focus();
      fetch('/qa-focus-probe').catch(()=>{});
      window.open('/popup');fetch('/write',{method:'POST'}).catch(()=>{});
    </script>'''
    if ending=='auth':site.redirects['/b']='/login'
    if ending=='failure':site.statuses['/b']=500
    if ending in ('cancel_nav','end_session','shutdown','browser_close'):
        site.delays['/b']=.5
        site.on_get=lambda path:entered.set() if path=='/b' else None
    elif ending=='cancel_mobile':
        def entered_mobile(path):
            if path=='/' and site.counts['/']==3:
                entered.set()
                # Delay the actual HTTP response; this is the cancellation
                # fixture, not a timing sleep used to hope cleanup happened.
                site.delays['/']=.5
        site.on_get=entered_mobile
    original_on_get=site.on_get
    def observe_focus(path):
        if original_on_get:original_on_get(path)
        if path=='/qa-focus-probe':
            observation=asyncio.run_coroutine_threadsafe(
                session.page.evaluate('document.visibilityState'),session.loop).result(3)
            focus_observations.append(observation)
    site.on_get=observe_focus
    try:
        worker=threading.Thread(target=api.run_qa_async_in_thread,args=(site.base+'/',pid,rid,'full',None,
            dict(delay=0,settle_timeout=600,page_limit=1 if ending=='limit' else 250)))
        api.active_worker_threads.append(worker);worker.start()
        if ending in ('cancel_nav','cancel_mobile','end_session','shutdown','browser_close'):
            assert entered.wait(10)
            async def during():
                assert session.page.url==site.base+'/visible'
                assert await session.page.evaluate('document.visibilityState')=='visible'
                assert await session.page.evaluate("String(window.focus).includes('[native code]')")
                assert await session.page.evaluate('() => [innerWidth,innerHeight]')==before['size']
            asyncio.run_coroutine_threadsafe(during(),session.loop).result(3)
            if ending.startswith('cancel'):app.test_client().post(f'/api/runs/{rid}/stop',headers={'X-QA-Request':'1'})
            elif ending=='end_session':
                from app.qa.interactive import stop_session
                stop_session(pid)
            elif ending=='shutdown':api.shutdown_qa_workers()
            else:asyncio.run_coroutine_threadsafe(session.browser.close(),session.loop).result(4)
        worker.join(15)
        assert not worker.is_alive()
        state,coverage,tests=worker_status(rid)
        assert state!='RUNNING'
        if ending in ('complete','limit','failure'): assert state=='COMPLETED'
        elif ending=='auth':assert state=='FAILED'
        else:assert state in ('ABORTED','FAILED')
        if ending=='limit':assert coverage['page_limit_reached'] and not coverage['coverage_complete']
        if ending=='failure':
            from PIL import Image
            evidence=[t for t in tests if t['status']=='FAIL' and t['screenshot_path']]
            assert len(evidence)==2, tests
            for record in evidence:
                with Image.open(record['screenshot_path']) as image:
                    assert image.width==(1366 if 'Desktop' in record['viewport'] else 390)
                    assert image.height>=768 if 'Desktop' in record['viewport'] else image.height>=844
                    assert len(image.getcolors(maxcolors=100000) or [])>1, 'Blank evidence'
        if ending not in ('end_session','shutdown','browser_close'):
            async def inspect():
                pending=list(session.qa_tasks)
                if pending:await asyncio.gather(*pending,return_exceptions=True)
                assert session.context.pages==[session.page]
                assert session.page.url==site.base+'/visible' and not session.page.is_closed()
                assert await session.page.evaluate('document.visibilityState')=='visible'
                assert await session.page.evaluate('() => [innerWidth,innerHeight]')==before['size']
                control=await session.context.new_cdp_session(session.page)
                assert await control.send('Browser.getWindowForTarget')==before['bounds']
                await control.detach()
                assert any(c['name']=='qa_login' and c['value']=='yes' for c in await session.context.cookies())
                assert not session.context.service_workers
            asyncio.run_coroutine_threadsafe(inspect(),session.loop).result(5)
        assert site.mutations==0 and site.counts['/popup']==0, 'Unattended popup stole a tab'
        assert len(focus_observations)>=2 and set(focus_observations)=={'visible'}, focus_observations
    finally:session.stop()
