"""Real-browser downstream-block regressions; no customer-error text blacklist."""
import asyncio
import pytest
from test_full_site import site, isolated_state, browser_case, engine

DERIVATIVE = '''fetch('/write',{method:'POST'}).catch(error => {
 console.error("[GraphQL] query 'getTalkisToken' failed", error);
});'''


def evidence_containing(results, expected):
    """Check retained evidence by target/viewport, independent of old summary wording.

    A safety-perturbed page retains separate DIAGNOSTIC rows for console arguments;
    these are not necessarily part of the legacy 'Console/JS errors' summary.
    Browser-generated blocked-resource diagnostics are not evidence of the fixture's
    expected console value. Every matching diagnostic must remain finding-free.
    """
    matching_targets = {(r['url'], r['viewport']) for r in results
                        if r['status'] in ('OBSERVATION', 'DIAGNOSTIC') and expected in r['actual_result']}
    grouped = {}
    for row in results:
        if row['status'] not in ('OBSERVATION', 'DIAGNOSTIC') or (row['url'], row['viewport']) not in matching_targets:
            continue
        if row['status'] == 'DIAGNOSTIC':
            assert row['finding_id'] is None
        key = (row['url'], row['viewport'])
        if key not in grouped:
            grouped[key] = dict(row)
        else:
            grouped[key]['actual_result'] += '\n' + row['actual_result']
    return list(grouped.values())


def assert_limited_truth(results):
    limited = [r for r in results if r['status'] == 'LIMITED COVERAGE']
    assert len(limited) == 2, results
    assert {r['viewport'].split()[0] for r in limited} == {'Desktop', 'Mobile'}
    assert not any(r['status'] in ('PASS', 'FAIL') for r in results), results
    for result in results:
        if result['status'] == 'OBSERVATION':
            assert result.get('independent_safety') and result['finding_id'], result
        else:
            assert result['finding_id'] is None, result


@pytest.mark.parametrize('interruption', ['cancel','failure'])
def test_partial_binding_installation_cleans_owner_and_allows_next_run(site, interruption):
    from app.qa.causality import SCRIPT
    site.links['/'] = []
    site.script = '<script>'+DERIVATIVE+'</script>'
    async def check(context,user):
        original = context.add_init_script
        entered, release = asyncio.Event(), asyncio.Event()
        first = True
        async def interrupted(script=None, **kwargs):
            nonlocal first
            if script == SCRIPT and first:
                first = False
                entered.set()
                if interruption == 'failure':
                    raise RuntimeError('injected init-script installation failure')
                await release.wait()
            await original(script=script, **kwargs)
        context.add_init_script = interrupted
        task = asyncio.create_task(engine(site,context).run())
        await asyncio.wait_for(entered.wait(),5)
        causal = context._qa_causality
        if interruption == 'cancel':
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RuntimeError,match='injected init-script'):
                await task
        assert not causal.active and context._qa_causality is None
        results = await engine(site,context).run()
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2
        assert not any(r['status']=='PASS' for r in results),results
        assert site.mutations==0 and any(r['status']=='SAFETY ACTION' for r in results)
        assert not any(r['status'] in ('OBSERVATION','FAIL') for r in results),results
    asyncio.run(browser_case(site,check))


@pytest.mark.parametrize('request_type,parent_stack', [('XHR',False),('Fetch',False),('Fetch',True)])
def test_initiator_proof_excludes_other_transport_and_ancestor(request_type,parent_stack):
    from app.qa.causality import BlockCausality
    causal = BlockCausality(None, set())
    page = object()
    for token in ('child','ancestor'):
        causal.attempts[(object(),'doc',token)] = dict(page=page,url='http://localhost/write',method='POST',observed=False)
    stack = {'callFrames':[{'functionName':'_qa_fetch_child'}]}
    ancestor = {'functionName':'_qa_fetch_ancestor'}
    if parent_stack:
        stack['parent'] = {'callFrames':[ancestor]}
    else:
        stack['callFrames'].append(ancestor)
    causal.observed(page, dict(type=request_type,initiator=dict(stack=stack),request=dict(url='http://localhost/write',method='POST')))
    observed = {key[2] for key,value in causal.attempts.items() if value['observed']}
    assert observed == ({'child'} if request_type == 'Fetch' else set())


@pytest.mark.parametrize('nested', ['xhr', 'fetch'])
def test_nested_mutation_cannot_prove_invalid_ancestor_fetch(site, nested):
    site.links['/'] = []
    child = ("const x=new XMLHttpRequest();x.open('POST','/write');x.send();"
             if nested == 'xhr' else "fetch('/write',{method:'POST'}).catch(()=>{});")
    site.script = "<script>fetch('/write',{method:'POST',get body(){"+child+"return new ReadableStream();}}).catch(e=>console.error(e));</script>"
    async def check(context, user):
        results = await engine(site, context).run()
        assert site.mutations == 0
        assert any(r['status'] == 'SAFETY ACTION' for r in results), results
        observations = evidence_containing(results, 'duplex')
        assert len(observations) == 2, results
        assert all('duplex' in r['actual_result'] for r in observations), observations
        assert all(r['finding_id'] is None for r in observations)
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2
        assert not any(r['status'] in ('PASS','OBSERVATION') for r in results)
    asyncio.run(browser_case(site, check))


def test_structured_console_secrets_redacted_before_db_and_official_exports(site):
    import io, json, zipfile
    from pathlib import Path
    from test_full_site import seed
    from app.models.database import get_db_connection
    from app.paths import get_exports_dir
    from app.routes import api
    from server import app
    pid,rid=seed(site)
    site.links['/']=[]
    secrets=['QA_JSON_PASSWORD_9fc','QA_JSON_TOKEN_574a','QA_JSON_COOKIE_aa6','QA_JSON_AUTH_492','574839']
    payload=dict(message='GENUINE_JSON_ERROR',password=secrets[0],nested=dict(access_token=secrets[1]),
                 cookie=[secrets[2]],authorization=dict(value=secrets[3]),session=int(secrets[4]))
    site.script='<script>'+DERIVATIVE+"fetch('/write',{method:'POST'}).catch(e=>console.error("+json.dumps(payload)+",e));</script>"
    async def check(context,user):
        qa=engine(site,context)
        results=await qa.run()
        observations=evidence_containing(results, 'GENUINE_JSON_ERROR')
        assert len(observations)==2 and all('GENUINE_JSON_ERROR' in r['actual_result'] for r in observations)
        assert site.mutations==0
        api._save_results(results,rid,pid)
    asyncio.run(browser_case(site,check))
    conn=get_db_connection()
    stored=str([tuple(r) for r in conn.execute('SELECT actual_result FROM tests WHERE run_id=?',(rid,))])
    stored+=str([tuple(r) for r in conn.execute('SELECT description FROM findings WHERE run_id=?',(rid,))])
    conn.close()
    assert all(secret not in stored for secret in secrets),stored
    assert 'GENUINE_JSON_ERROR' in stored and '[REDACTED]' in stored
    client=app.test_client()
    documents=[client.get(f'/export/{rid}/html').get_data(as_text=True)]
    for format in ('markdown','txt','docx'):
        response=client.post(f'/api/export/{rid}/{format}')
        assert response.status_code==200
        data=(Path(get_exports_dir())/response.json['filename']).read_bytes()
        if format=='docx':
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                data=archive.read('word/document.xml')
        documents.append(data.decode())
    assert all('GENUINE_JSON_ERROR' in doc and 'PRIVATE_LEARNING_SENTINEL' not in doc for doc in documents)
    assert all(secret not in doc for doc in documents for secret in secrets)


def test_uuid_unavailable_still_correlates_real_rejected_fetch(site):
    site.links['/']=[]
    site.script='<script>'+DERIVATIVE+'</script>'
    async def check(context,user):
        # Simulate the missing secure-context-only API, retaining the browser's
        # real random source, native fetch, shared context and mutation guard.
        await context.add_init_script("Object.defineProperty(crypto,'randomUUID',{value:undefined});")
        qa=engine(site,context)
        results=await qa.run()
        assert site.mutations==0 and any(r['status']=='SAFETY ACTION' for r in results)
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2
        assert not any(r['status']=='PASS' for r in results),results
        assert not any(r['status'] in ('OBSERVATION','FAIL') for r in results),results
        assert not qa._causality.active and context._qa_causality is None
    asyncio.run(browser_case(site,check))


@pytest.mark.parametrize('style', ['object','string','uncaught','delayed','many'])
def test_exact_block_rejection_is_safety_not_customer_bug(site, style):
    site.links['/'] = []
    snippets = {
        'object': DERIVATIVE,
        'string': "fetch('/write',{method:'POST'}).catch(e=>console.error('[GraphQL] '+String(e)));",
        'uncaught': "fetch('/write',{method:'POST'});",
        'delayed': "fetch('/write',{method:'POST'}).catch(e=>setTimeout(()=>console.error('[GraphQL]',e),100));",
        'many': "for(let i=0;i<6;i++){"+DERIVATIVE+"}",
    }
    site.script = '<script>'+snippets[style]+'</script>'
    async def check(context,user):
        qa = engine(site,context)
        results = await qa.run()
        assert site.mutations == 0
        assert any(r['status']=='SAFETY ACTION' for r in results)
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2
        assert not any(r['status']=='PASS' for r in results), results
        assert not any(r['status'] in ('OBSERVATION','FAIL') for r in results),results
        assert qa._causality.suppressed > 0
        assert not qa._causality.active and not qa._causality.receipts and not qa._causality.attempts
        assert context._qa_causality is None
    asyncio.run(browser_case(site,check))


@pytest.mark.parametrize('genuine', ['exception','get','graphql','identical_typeerror','mixed_console','mixed_string','mixed_suffix','network_text','mixed_object','mixed_number'])
def test_unrelated_defect_after_block_remains_reported(site,genuine):
    site.links['/'] = []
    extra = {
        'exception': "setTimeout(()=>{throw new Error('GENUINE_SITE_EXCEPTION')},100);",
        'get': "fetch('/blocked-get.js').catch(()=>{});",
        'graphql': "console.error('[GraphQL] genuine server-side failure');",
        'identical_typeerror': "console.error(new TypeError('Failed to fetch'));",
        'mixed_console': "fetch('/write',{method:'POST'}).catch(e=>console.error(e,new Error('GENUINE_MIXED')));",
        'mixed_string': "fetch('/write',{method:'POST'}).catch(e=>console.error(e,'GENUINE_MIXED_STRING'));",
        'mixed_suffix': "fetch('/write',{method:'POST'}).catch(e=>console.error(String(e)+' GENUINE_MIXED_SUFFIX'));",
        'network_text': "fetch('/write',{method:'POST'}).catch(()=>console.error('GENUINE_NETWORK ERR_FAILED '+location.origin+'/write'));",
        'mixed_object': "fetch('/write',{method:'POST'}).catch(e=>console.error({message:'GENUINE_MIXED_OBJECT'},e));",
        'mixed_number': "fetch('/write',{method:'POST'}).catch(e=>console.error(e,404));",
    }
    site.script='<script>'+DERIVATIVE+extra[genuine]+'</script>'
    async def check(context,user):
        if genuine=='get':
            await context.route('**/blocked-get.js',lambda route:route.abort())
        qa=engine(site,context)
        results=await qa.run()
        assert site.mutations==0 and any(r['status']=='SAFETY ACTION' for r in results)
        assert_limited_truth(results)
        expected={'exception':'GENUINE_SITE_EXCEPTION','get':'ERR_FAILED','graphql':'genuine server-side',
                  'identical_typeerror':'Failed to fetch','mixed_console':'GENUINE_MIXED',
                  'mixed_string':'GENUINE_MIXED_STRING','mixed_suffix':'GENUINE_MIXED_SUFFIX','network_text':'GENUINE_NETWORK',
                  'mixed_object':'GENUINE_MIXED_OBJECT','mixed_number':'404'}[genuine]
        observations=evidence_containing(results, expected)
        assert len(observations)==2,results
        assert all(expected in r['actual_result'] for r in observations), observations
    asyncio.run(browser_case(site,check))


def test_genuine_graphql_error_without_guard_block_is_reported(site):
    site.links['/']=[]
    site.script='<script>console.error("[GraphQL] query failed",new TypeError("Failed to fetch"));</script>'
    async def check(context,user):
        results=await engine(site,context).run()
        assert sum(r['status']=='OBSERVATION' for r in results)==2
        assert not any(r['status']=='SAFETY ACTION' for r in results)
    asyncio.run(browser_case(site,check))


@pytest.mark.parametrize('blocked_viewport', [1366,390])
def test_viewport_block_does_not_suppress_other_viewport(site,blocked_viewport):
    site.links['/']=[]
    site.script=f'<script>if(innerWidth==={blocked_viewport}){{{DERIVATIVE}}}else if([1366,390].includes(innerWidth)){{console.error(new TypeError("GENUINE_OTHER_VIEWPORT Failed to fetch"));}}</script>'
    async def check(context,user):
        qa=engine(site,context)
        results=await qa.run()
        assert site.mutations==0
        obs=evidence_containing(results, 'GENUINE_OTHER_VIEWPORT')
        assert len(obs)==1 and 'GENUINE_OTHER_VIEWPORT' in obs[0]['actual_result'],results
        assert ('Mobile' if blocked_viewport==1366 else 'Desktop') in obs[0]['viewport']
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==1
        assert not any(r['status']=='PASS' for r in results)
    asyncio.run(browser_case(site,check))


def test_next_page_and_new_run_do_not_inherit_receipts(site):
    site.links['/']=[('/b','B')];site.links['/b']=[]
    site.script='<script>'+DERIVATIVE+'</script>'
    site.scripts['/b']='<script>console.error(new TypeError("GENUINE_NEXT_PAGE Failed to fetch"));</script>'
    async def check(context,user):
        first=engine(site,context)
        results=await first.run()
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2
        assert not any(r['status']=='PASS' for r in results)
        obs=evidence_containing(results, 'GENUINE_NEXT_PAGE')
        assert len(obs)==2 and all(r['url']==site.base+'/b' for r in obs)
        site.script='<script>console.error(new TypeError("GENUINE_NEW_RUN Failed to fetch"));</script>'
        site.links['/']=[]
        second=engine(site,context)
        results=await second.run()
        assert first._causality.owner!=second._causality.owner
        assert all('GENUINE_NEW_RUN' in r['actual_result'] for r in results if r['status']=='OBSERVATION' or (r['status']=='DIAGNOSTIC' and r['actual_result'].startswith('Console/JS errors detected:')))
        assert sum(r['status']=='OBSERVATION' for r in results)==2
        assert not any(r['status']=='SAFETY ACTION' for r in results)
    asyncio.run(browser_case(site,check))


def test_two_simultaneous_targets_do_not_share_causality(site):
    from app.qa.full_site import FullSiteQA
    site.links['/']=[]
    site.script='<script>'+DERIVATIVE+'</script>'
    site.scripts['/b']='<script>console.error(new TypeError("GENUINE_OTHER_TARGET Failed to fetch"));</script>'
    class Concurrent(FullSiteQA):
        async def _execute_queue(self):
            await asyncio.gather(self._test_page(self.provided_context,self.viewports[0],site.base+'/',0),
                                 self._test_page(self.provided_context,self.viewports[1],site.base+'/b',0))
    async def check(context,user):
        qa=Concurrent(site.base+'/',1,1,context=context,settle_timeout=600)
        results=await qa.run()
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==1
        assert not any(r['status']=='PASS' for r in results)
        obs=evidence_containing(results, 'GENUINE_OTHER_TARGET')
        assert len(obs)==1 and obs[0]['url']==site.base+'/b' and 'GENUINE_OTHER_TARGET' in obs[0]['actual_result']
        assert site.mutations==0
    asyncio.run(browser_case(site,check))


def test_popup_and_grandchild_cannot_mask_parent_error(site):
    site.links['/']=[]
    # Require an actual descendant block before logging the genuine parent
    # error. A fixed timer did not ensure the popup chain ran under suite load.
    site.script='''<script>window.qaDescendantReady=false;
    addEventListener('message',event=>{if(event.data==='qa-descendant-ready'){
      console.error(new TypeError('GENUINE_PARENT Failed to fetch'));
      window.qaDescendantReady=true;
    }});window.open('/popup');</script>'''
    site.scripts['/popup']='<script>'+DERIVATIVE+'</script>'
    site.scripts['/grandchild']='<script>'+DERIVATIVE+'''fetch('/write',{method:'POST'}).catch(()=>{}).finally(()=>window.opener.opener.postMessage('qa-descendant-ready','*'));</script>'''
    async def check(context,user):
        qa=engine(site,context)
        navigate=qa._navigate_page
        async def wait_for_descendant(page,url):
            response=await navigate(page,url)
            if url==site.base+'/':
                await page.wait_for_function('window.qaDescendantReady===true',timeout=5000)
            return response
        qa._navigate_page=wait_for_descendant
        results=await qa.run()
        assert site.counts['/popup']>0 and site.counts['/grandchild']>0 and site.mutations==0, dict(site.counts)
        obs=[r for r in results if r['status']=='DIAGNOSTIC' and 'GENUINE_PARENT' in r['actual_result']]
        assert {r['viewport'].split()[0] for r in obs}=={'Desktop','Mobile'}, results
        assert all(r['finding_id'] is None for r in obs)
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2
        assert not any(r['status'] in ('PASS','OBSERVATION') for r in results)
    asyncio.run(browser_case(site,check))


def test_invalid_fetch_cannot_borrow_another_actual_block(site):
    site.links['/']=[]
    site.script='<script>'+DERIVATIVE+'''fetch('/write',{method:'POST',body:new ReadableStream()}).catch(e=>console.error('GENUINE_INVALID_FETCH',e));</script>'''
    async def check(context,user):
        qa=engine(site,context)
        results=await qa.run()
        obs=evidence_containing(results, 'GENUINE_INVALID_FETCH')
        assert len(obs)==2 and all('duplex' in r['actual_result'].lower() for r in obs),results
        assert site.mutations==0
    asyncio.run(browser_case(site,check))


@pytest.mark.parametrize('stop_kind',['cancel','registry','context','browser','shutdown'])
def test_correlation_interruption_preserves_checkpoint_and_cleans_state(site,stop_kind):
    import threading
    from test_full_site import seed, live_session, run_worker, worker_status
    from app.routes import api
    from server import app
    pid,rid=seed(site)
    session=live_session(site,pid)
    site.script='<script>'+DERIVATIVE+'</script>'
    site.delays['/b']=.4
    entered=threading.Event()
    site.on_get=lambda path:entered.set() if path=='/b' else None
    try:
        worker=run_worker(site,pid,rid)
        assert entered.wait(10)
        qa=api.running_engines[rid]
        if stop_kind=='cancel':
            app.test_client().post(f'/api/runs/{rid}/stop')
        elif stop_kind=='registry':
            from app.qa.interactive import sessions
            sessions.pop(str(pid))
        elif stop_kind in ('context','browser'):
            owner=session.context if stop_kind=='context' else session.browser
            asyncio.run_coroutine_threadsafe(owner.close(),session.loop).result(4)
        else: api.shutdown_qa_workers()
        worker.join(8)
        assert not worker.is_alive()
        session.stop()
        state,coverage,tests=worker_status(rid)
        assert state in ('ABORTED','FAILED') and coverage['pages_tested']==1
        assert sum(t['status']=='LIMITED COVERAGE' for t in tests)==2
        assert not any(t['status']=='PASS' for t in tests)
        assert not any(t['status']=='OBSERVATION' for t in tests)
        assert not qa._causality.active and not qa._causality.receipts and not qa._causality.attempts
        assert not any('QA-BLOCK:' in t['actual_result'] for t in tests)
        api._save_results([],rid,pid)
        assert worker_status(rid)[0]==state
    finally: session.stop()


def test_navigation_and_expiry_invalidate_exact_receipts(site):
    from app.qa.full_site import FullSiteQA
    class Boundaries(FullSiteQA):
        async def _execute_queue(self):
            page=await self._new_qa_page(self.provided_context)
            try:
                await self._navigate_page(page,site.base+'/')
                await page.evaluate("async()=>{try{await fetch('/write',{method:'POST'})}catch(e){window.fixtureError=e;}}")
                stack=await page.evaluate('fixtureError.stack')
                assert self._causality.attributable(page,stack)
                # A second target cannot redeem the first page's receipt.
                other=await self._new_qa_page(self.provided_context)
                await self._navigate_page(other,site.base+'/visible')
                assert not self._causality.attributable(other,stack)
                for receipt in self._causality.receipts.values(): receipt['time']-=self._causality.TTL+1
                assert not self._causality.attributable(page,stack)
                await page.evaluate("async()=>{try{await fetch('/write',{method:'POST'})}catch(e){window.fixtureError=e;}}")
                stack=await page.evaluate('fixtureError.stack')
                assert self._causality.attributable(page,stack)
                await self._navigate_page(page,site.base+'/alias')
                assert page.url==site.base+'/b' and not self._causality.attributable(page,stack)
                assert site.mutations==0
            finally: await self._close_tree(page)
    async def check(context,user):
        qa=Boundaries(site.base+'/',1,1,context=context,settle_timeout=600)
        await qa.run()
        assert not qa._causality.receipts and context._qa_causality is None
    asyncio.run(browser_case(site,check))


@pytest.mark.parametrize('ending',['cancel','context'])
def test_interruption_while_rejection_receipt_is_in_flight(site,monkeypatch,ending):
    import threading
    from app.qa.causality import BlockCausality
    from test_full_site import seed,live_session,run_worker,worker_status
    from app.routes import api
    from server import app
    pid,rid=seed(site)
    session=live_session(site,pid)
    entered=threading.Event()
    gate=asyncio.Event()
    receive=BlockCausality.receive
    async def observed_receive(self,source,data):
        result=await receive(self,source,data)
        if data.get('kind')=='rejected' and result and not entered.is_set():
            # The real network request has already been aborted and the real
            # receipt issued. Pause delivery to test this exact teardown edge.
            entered.set()
            await gate.wait()
        return result
    monkeypatch.setattr(BlockCausality,'receive',observed_receive)
    site.script='<script>'+DERIVATIVE+'</script>'
    try:
        worker=run_worker(site,pid,rid)
        assert entered.wait(10)
        qa=api.running_engines[rid]
        assert qa._causality.receipts and site.mutations==0
        if ending=='cancel':app.test_client().post(f'/api/runs/{rid}/stop')
        else:asyncio.run_coroutine_threadsafe(session.context.close(),session.loop).result(4)
        session.loop.call_soon_threadsafe(gate.set)
        worker.join(8)
        assert not worker.is_alive()
        state,coverage,tests=worker_status(rid)
        assert state in ('ABORTED','FAILED') and coverage['pages_tested']==0
        assert not any(t['status']=='OBSERVATION' for t in tests)
        assert not qa._causality.active and not qa._causality.receipts and not qa._causality.attempts
        assert site.mutations==0
        if ending=='cancel':
            async def inspect():
                pending=list(session.qa_tasks)
                if pending:await asyncio.gather(*pending,return_exceptions=True)
                assert session.context.pages==[session.page]
            asyncio.run_coroutine_threadsafe(inspect(),session.loop).result(4)
    finally:
        if session.loop and session.loop.is_running():session.loop.call_soon_threadsafe(gate.set)
        session.stop()


@pytest.mark.parametrize('shape,expected,blocked', [
    ('{error:e}', None, True),
    ('{error:e,code:123}', '123', True),
    ('{error:e,message:"genuine problem"}', 'genuine problem', True),
    ('{error:new Error("genuine")}', 'genuine', False),
    ('{error:{}}', '"error": {}', False),
    ('{outer:{error:e}}', None, True),
    ('[e,"GENUINE_ARRAY"]', 'GENUINE_ARRAY', True),
    ('{error:e,other:new Error("GENUINE_MANUAL_FIXTURE_ERROR")}', 'GENUINE_MANUAL_FIXTURE_ERROR', True),
    ('{error:e,empty:{}}', '"empty": {}', True),
    ('{error:e,get message(){throw new Error("GETTER_MUST_NOT_RUN")}}', 'Object', True),
])
def test_structured_console_nested_error_provenance(site,shape,expected,blocked):
    site.links['/']=[]
    code = "fetch('/write',{method:'POST'}).catch(e=>console.error("+shape+"));" if blocked else "console.error("+shape+");"
    site.script='<script>'+code+'</script>'
    async def check(context,user):
        qa=engine(site,context)
        results=await qa.run()
        assert site.mutations==0
        assert any(r['status']=='SAFETY ACTION' for r in results)==blocked
        if blocked:
            assert_limited_truth(results)
        if expected is None:
            # Proven derivative errors may be retained as diagnostics, never findings.
            assert not any(r['status'] in ('OBSERVATION', 'FAIL') for r in results), results
            assert qa._causality.suppressed > 0
            assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2
            assert not any(r['status']=='PASS' for r in results),results
        else:
            obs=evidence_containing(results, expected)
            assert len(obs)==2,results
            assert all(expected in r['actual_result'] for r in obs),obs
        assert not qa._causality.receipts and not qa._causality.active
    asyncio.run(browser_case(site,check))


@pytest.mark.parametrize('boundary', ['viewport','page','run'])
def test_structured_console_receipt_cannot_cross_boundary(site,boundary):
    site.links['/']=[]
    derivative="fetch('/write',{method:'POST'}).catch(e=>{localStorage.setItem('oldStack',e.stack);console.error({error:e})});"
    replay="const e=new Error('GENUINE_REPLAY');e.stack=localStorage.getItem('oldStack')||e.stack;console.error({error:e,message:'GENUINE_REPLAY'});"
    if boundary=='viewport':
        site.script='<script>if(innerWidth===1366){'+derivative+'}else if(innerWidth===390){'+replay+'}</script>'
    else:
        site.script='<script>'+derivative+'</script>'
    if boundary=='page':
        site.links['/']=[('/b','B')];site.links['/b']=[]
        site.scripts['/b']='<script>'+replay+'</script>'
    async def check(context,user):
        first=engine(site,context)
        results=await first.run()
        assert any(r['status']=='SAFETY ACTION' for r in results)
        if boundary=='run':
            assert not any(r['status']=='OBSERVATION' for r in results),results
            site.script='<script>'+replay+'</script>'
            second=engine(site,context)
            results=await second.run()
            assert first._causality.owner!=second._causality.owner
            assert not any(r['status']=='SAFETY ACTION' for r in results)
        obs=evidence_containing(results, 'GENUINE_REPLAY')
        assert len(obs)==(1 if boundary=='viewport' else 2),results
        assert all('GENUINE_REPLAY' in r['actual_result'] and 'Failed to fetch' in r['actual_result'] for r in obs),obs
        assert all('QA-BLOCK' not in r['actual_result'] for r in obs)
        if boundary=='viewport': assert 'Mobile' in obs[0]['viewport']
        if boundary=='page': assert all(r['url']==site.base+'/b' for r in obs)
        assert site.mutations==0
    asyncio.run(browser_case(site,check))


@pytest.mark.parametrize('shape', [
    '(()=>{const o={error:e};o.self=o;return o})()',
    '(()=>{const o={error:e};Object.defineProperty(o,"message",{enumerable:true,get(){window.inspectionGetterRan=true;return "BAD"}});return o})()',
    '{error:e,other:new TypeError("Failed to fetch")}',
    '{error:e,value:null}',
    '{error:e,value:false}',
    '{error:e,value:0}',
    '{error:e,value:""}',
])
def test_structured_console_preserves_unproven_values_and_does_not_invoke_getters(site,shape):
    site.links['/']=[]
    site.script='<script>fetch("/write",{method:"POST"}).catch(e=>{console.error('+shape+');setTimeout(()=>{if(window.inspectionGetterRan)fetch("/getter-ran")},200)});</script>'
    async def check(context,user):
        results=await engine(site,context).run()
        diagnostics=[r for r in results if r['status']=='DIAGNOSTIC' and r['actual_result'].startswith('Console/JS errors detected:')]
        assert_limited_truth(results)
        assert len(diagnostics)==2 and all(r['finding_id'] is None for r in diagnostics),results
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2
        assert not any(r['status'] in ('PASS','OBSERVATION') for r in results)
        assert any(r['status']=='SAFETY ACTION' for r in results)
        assert site.counts['/getter-ran']==0 and site.mutations==0
    asyncio.run(browser_case(site,check))


@pytest.mark.parametrize('boundary', ['frame','expiry','navigation','viewport_resize'])
def test_structured_console_live_receipt_scope(site,boundary):
    from app.qa.causality import CONSOLE_ARGUMENT_SCRIPT
    from app.qa.full_site import FullSiteQA
    class Boundaries(FullSiteQA):
        async def _execute_queue(self):
            page=await self._new_qa_page(self.provided_context)
            try:
                await self._set_qa_viewport(page,self.viewports[0])
                await self._navigate_page(page,site.base+'/')
                handle=await page.evaluate_handle("async()=>{try{await fetch('/write',{method:'POST'})}catch(e){return {outer:{error:e}}}}")
                evidence=await handle.evaluate(CONSOLE_ARGUMENT_SCRIPT)
                remaining,removed=self._causality.filter_console_tree(page,evidence['tree'],evidence['generation'],evidence['viewport'])
                assert remaining is None and removed
                if boundary=='frame':
                    await page.evaluate("()=>{const f=document.createElement('iframe');f.src='/b';document.body.append(f)}")
                    frame=page.frames[-1]
                    await frame.wait_for_url(site.base+'/b')
                    stack=evidence['tree']['entries'][0][1]['entries'][0][1]['stack']
                    other=await frame.evaluate_handle("stack=>{const e=new Error('genuine frame');e.stack=stack;return {error:e}}",stack)
                    evidence=await other.evaluate(CONSOLE_ARGUMENT_SCRIPT)
                elif boundary=='expiry':
                    for receipt in self._causality.receipts.values(): receipt['time']-=self._causality.TTL+1
                elif boundary=='navigation':
                    await self._navigate_page(page,site.base+'/b')
                else:
                    await self._set_qa_viewport(page,self.viewports[1])
                    evidence=await handle.evaluate(CONSOLE_ARGUMENT_SCRIPT)
                remaining,removed=self._causality.filter_console_tree(page,evidence['tree'],evidence['generation'],evidence['viewport'])
                assert not removed and remaining is not None
                assert 'Failed to fetch' in str(remaining)
                assert site.mutations==0
            finally:
                await self._close_tree(page)
    async def check(context,user):
        qa=Boundaries(site.base+'/',1,1,context=context,settle_timeout=600)
        await qa.run()
        assert not qa._causality.active and not qa._causality.receipts
    asyncio.run(browser_case(site,check))


@pytest.mark.parametrize('arguments,expected', [
    ('"GENUINE_LEADING",{error:e}', 'GENUINE_LEADING'),
    ('{error:e},"GENUINE_TRAILING"', 'GENUINE_TRAILING'),
    ('null,{error:e}', 'null'),
    ('{error:e},null', 'null'),
])
def test_structured_console_keeps_other_arguments(site,arguments,expected):
    site.links['/']=[]
    site.script='<script>fetch("/write",{method:"POST"}).catch(e=>console.error('+arguments+'));</script>'
    async def check(context,user):
        results=await engine(site,context).run()
        obs=evidence_containing(results, expected)
        assert len(obs)==2 and all(expected in r['actual_result'] for r in obs),results
        assert site.mutations==0 and any(r['status']=='SAFETY ACTION' for r in results)
        assert_limited_truth(results)
    asyncio.run(browser_case(site,check))


def test_structured_console_inspection_does_not_invoke_bound_error_stack_getter(site):
    from app.qa.causality import CONSOLE_ARGUMENT_SCRIPT
    async def check(context,user):
        # Chromium itself reads Error.stack while logging, even with no QA listener.
        # Inspect a handle directly and reset after handle creation to measure only
        # the collector, rather than attributing Chromium preview work to it.
        handle=await user.evaluate_handle("()=>{const e=new Error('genuine');Object.defineProperty(e,'stack',{get:(function(){window.getterCalls++;return 'custom'}).bind(null)});return {error:e}}")
        await user.evaluate('window.getterCalls=0')
        evidence=await handle.evaluate(CONSOLE_ARGUMENT_SCRIPT)
        assert await user.evaluate('window.getterCalls')==0
        assert evidence['tree'] is None and 'Uninspectable' in evidence['opaque']
        await handle.dispose()
    asyncio.run(browser_case(site,check))
