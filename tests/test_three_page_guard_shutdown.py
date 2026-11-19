"""Run-34-shaped local success control; no live-state attribution is assumed."""
import asyncio
import json
import pytest
from playwright.async_api import CDPSession
from test_full_site import site, isolated_state, browser_case
from test_graphql_client_metadata import LIVE_SHAPE
from app.qa.full_site import FullSiteQA
from app.qa.browser_guard import BrowserMutationGuard


@pytest.mark.parametrize('late_query', [False, True])
def test_three_page_read_shutdown_resolves_before_disable(site, monkeypatch, late_query):
    site.redirects['/'] = '/one'
    site.links = {'/one': [('/two','Second'),('/three','Third'),('/four','Fourth')],
                  '/two': [], '/three': [], '/four': []}
    reads = []
    def post(handler):
        payload = json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
        if payload != LIVE_SHAPE: site.mutations += 1
        reads.append(payload)
        body = b'{"data":{"talkjsToken":{"token":"fixture","__typename":"Token"}}}'
        handler.send_response(200)
        handler.send_header('Content-Type','application/json')
        handler.send_header('Content-Length',str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    monkeypatch.setattr(site.server.RequestHandlerClass,'do_POST',post)
    for path in ('/one','/two','/three'):
        site.scripts[path] = '''<script>fetch('/api/proxy/graphql', {method:'POST',
          headers:{'Content-Type':'application/json'},body:JSON.stringify(%s)})
          .then(r=>r.json()).then(()=>window.queryDone=true);</script>''' % json.dumps(LIVE_SHAPE)
    guards, ordered = [], []
    start, stop = BrowserMutationGuard.start, BrowserMutationGuard.stop
    async def started(guard):
        guards.append(guard)
        return await start(guard)
    async def stopped(guard):
        live = (await guard.session.send('Target.getTargets'))['targetInfos']
        owned = [i for i in live if i.get('browserContextId') == guard.context_id
                 and i.get('type') in ('page','iframe') and guard.frames.get(i['targetId']) != 'manual']
        ordered.append(dict(stage='stop',owned=len(owned),pending=len(guard.pending),
                            tasks=len(guard.tasks),failed=guard.failed,errors=len(guard.errors)))
        assert not owned
        return await stop(guard)
    monkeypatch.setattr(BrowserMutationGuard,'start',started)
    monkeypatch.setattr(BrowserMutationGuard,'stop',stopped)
    send = CDPSession.send
    async def sent(session, method, params=None):
        if method == 'Fetch.disable':
            guard = next(g for g in guards if g.session is session)
            assert not guard.pending and not guard.tasks and not guard.failed
            ordered.append(dict(stage='disable',pending=0,tasks=0,failed=False))
        try:
            result = await send(session,method,params)
        except Exception as error:
            if method == 'Fetch.fulfillRequest':
                ordered.append(dict(stage='reply.error',error=str(error)))
            raise
        if method in ('Fetch.fulfillRequest','Fetch.failRequest'):
            ordered.append(dict(stage=method+'.ack',id=params['requestId']))
        return result
    monkeypatch.setattr(CDPSession,'send',sent)
    async def check(context,user):
        await context.add_init_script('window.__shutdownNativeFetch = window.fetch')
        entered, release = asyncio.Event(), asyncio.Event()
        cancelled = asyncio.get_running_loop().create_future()
        injecting = False
        forward = BrowserMutationGuard._forward_query
        async def held(guard,request,route):
            if injecting:
                ordered.append(dict(stage='late.forward.enter'))
                entered.set()
                await release.wait()
                assert route.request.frame.page.is_closed()
                assert route.request.failure == 'net::ERR_ABORTED'
            return await forward(guard,request,route)
        monkeypatch.setattr(BrowserMutationGuard,'_forward_query',held)
        def failed(request):
            if injecting and request.method == 'POST' and request.url.endswith('/graphql'):
                ordered.append(dict(stage='original.cancelled',failure=request.failure))
                if not cancelled.done(): cancelled.set_result(request.failure)
        context.on('requestfailed',failed)
        class Probe(FullSiteQA):
            async def _quiesce_and_close_page(self,page):
                nonlocal injecting
                late = (late_query and not page.is_closed() and page.url == site.base+'/three'
                        and getattr(page,'qa_safety',{}).get('viewport','').startswith('Mobile'))
                if late:
                    injecting = True
                    await page.evaluate('p=>{window.__shutdownNativeFetch("/api/proxy/graphql",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(p)}).catch(()=>{});}', LIVE_SHAPE)
                    await asyncio.wait_for(entered.wait(),3)
                await super()._quiesce_and_close_page(page)
                if late:
                    assert await asyncio.wait_for(cancelled,3) == 'net::ERR_ABORTED'
                    ordered.append(dict(stage='owner.closed'))
                    release.set()
            async def _navigate_page(self,page,url):
                response = await super()._navigate_page(page,url)
                if page.url.rsplit('/',1)[-1] in ('one','two','three'):
                    await page.wait_for_function('window.queryDone === true',timeout=3000)
                return response
        qa = Probe(site.base+'/',1,1,context=context,page_limit=3,delay=0,settle_timeout=600)
        results = await qa.run()
        p = qa.get_progress()
        assert p['pages_attempted']==p['pages_fully_evaluated']==3
        assert p['desktop_pages_tested']==p['mobile_pages_tested']==3
        assert p['safety_limited_pages']==p['safety_limited_viewports']==0
        assert p['failed_to_load']==0 and p['page_limit_reached']
        assert qa.terminal_status=='COMPLETED'
        assert sum(r['status']=='PASS' for r in results)==6
        assert not any(r['status'] in ('FAIL','OBSERVATION','SAFETY ACTION','LIMITED COVERAGE') for r in results)
        assert len(reads)==9+int(late_query) and site.mutations==0
        assert not guards[0].active and not getattr(context,'_qa_safety_hold',None)
        assert not guards[0].errors and not guards[0].pending and not guards[0].tasks
        if late_query:
            errors = [e for e in ordered if e['stage']=='reply.error']
            assert len(errors)==1 and errors[0]['error'].endswith('Protocol error (Fetch.fulfillRequest): Invalid InterceptionId.')
        assert ordered[-1]['stage']=='disable'
        print('THREE_PAGE_SHUTDOWN '+json.dumps(dict(reads=len(reads),events=ordered)))
    asyncio.run(browser_case(site,check))
