"""Option B: blocked execution limits validity without losing diagnostic evidence."""
import asyncio
import pytest
from test_full_site import site, isolated_state, browser_case, engine, seed


@pytest.mark.parametrize('loss', ['stop', 'session'])
def test_partial_blocked_viewport_is_persisted_after_terminal_cleanup(site, monkeypatch, loss):
    import threading
    from test_full_site import live_session, run_worker, worker_status
    from app.qa.full_site import FullSiteQA
    from app.qa.interactive import sessions
    from app.routes import api
    from server import app
    site.links['/'] = []
    site.script = '<script>fetch("/write",{method:"POST"}).catch(e=>{console.error({error:e});window.actualBlockCompleted=true});</script>'
    pid, rid = seed(site)
    session = live_session(site, pid)
    entered = threading.Event()
    navigate = FullSiteQA._navigate_page
    async def pause_after_block(self, page, url):
        response = await navigate(self, page, url)
        if hasattr(page, 'qa_safety'):
            await page.wait_for_function('window.actualBlockCompleted===true')
            assert page.qa_safety['blocked']
            entered.set()
            await asyncio.Future()
        return response
    monkeypatch.setattr(FullSiteQA, '_navigate_page', pause_after_block)
    try:
        worker = run_worker(site, pid, rid)
        assert entered.wait(10)
        if loss == 'stop':
            assert app.test_client().post(f'/api/runs/{rid}/stop').json['status'] == 'stopping'
        else:
            sessions.pop(str(pid))
        worker.join(10)
        assert not worker.is_alive()
        state, coverage, rows = worker_status(rid)
        assert state == coverage['phase'] == 'ABORTED'
        assert coverage['desktop_limited'] == 1 and coverage['mobile_limited'] == 0
        assert coverage['pages_attempted'] == 1
        assert coverage['pages_tested'] == coverage['pages_fully_evaluated'] == 0
        assert not coverage['coverage_complete'] and site.mutations == 0
        assert sum(r['status'] == 'LIMITED COVERAGE' for r in rows) == 1
        assert any(r['status'] == 'SAFETY ACTION' for r in rows)
        assert any(r['status'] == 'DIAGNOSTIC' for r in rows)
        assert all(r['stable_id'] is None for r in rows)
        assert not any(r['status'] in ('PASS', 'OBSERVATION') for r in rows)
        # A normal late save must still neither append successful evidence nor
        # replace the terminal coverage with a completed snapshot.
        api._save_results([dict(url=site.base, expected_result='late', actual_result='late',
            status='PASS', viewport='Desktop', screenshot=None, finding_id=None)], rid, pid)
        assert worker_status(rid) == (state, coverage, rows)
        assert rid not in api.worker_records and rid not in api.running_engines
    finally:
        session.stop()


def test_uninspectable_preview_sensitive_fields_are_redacted():
    from app.qa.redaction import redact_string
    raw='{message: GENUINE_DIAGNOSTIC, password: FAKE_PREVIEW_PASSWORD, token: FAKE_PREVIEW_TOKEN}'
    clean=redact_string(raw)
    assert 'GENUINE_DIAGNOSTIC' in clean and '[REDACTED]' in clean
    assert 'FAKE_PREVIEW_PASSWORD' not in clean and 'FAKE_PREVIEW_TOKEN' not in clean


def test_limited_additive_migration_preserves_historical_rows():
    import sqlite3
    from app.models.crawl import create_crawl_tables,get_coverage
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    conn.execute('CREATE TABLE runs(id INTEGER PRIMARY KEY)');conn.execute('INSERT INTO runs VALUES (7)')
    conn.execute('''CREATE TABLE crawl_runs(run_id INTEGER PRIMARY KEY,phase TEXT,current_url TEXT,
        termination_reason TEXT,page_limit INTEGER,query_limit INTEGER,depth_limit INTEGER,url_limit INTEGER,
        navigation_limit INTEGER,page_limit_reached INTEGER DEFAULT 0,discovery_limit_reached INTEGER DEFAULT 0,
        coverage_complete INTEGER DEFAULT 0,pages_tested INTEGER DEFAULT 0)''')
    conn.execute("INSERT INTO crawl_runs(run_id,phase,page_limit,pages_tested) VALUES (7,'COMPLETED',10,3)")
    create_crawl_tables(conn);create_crawl_tables(conn)
    coverage=get_coverage(conn,7)
    assert coverage['pages_tested']==3 and coverage['phase']=='COMPLETED'
    assert coverage['pages_fully_evaluated']==0 and coverage['desktop_limited']==coverage['mobile_limited']==0
    assert coverage['discovery_safety_limited'] is False
    assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]==1
    conn.close()


@pytest.mark.parametrize('release',['before_close','timeout'])
def test_owned_binding_resolution_finishes_before_target_teardown(site,monkeypatch,release):
    from playwright._impl._connection import Connection
    from app.qa.causality import BINDING
    from app.qa.full_site import FullSiteQA
    site.links['/']=[]
    async def check(context,user):
        entered=asyncio.Event();errors=[];held=None
        loop=asyncio.get_running_loop();previous=loop.get_exception_handler()
        loop.set_exception_handler(lambda loop,error:errors.append(error))
        class DeferredReply(asyncio.Future):
            def set_result(self,value):
                self.saved_result=value;entered.set()
            def release_reply(self):
                if not self.done():super().set_result(self.saved_result)
        original_send=Connection._send_message_to_server
        def hold_owned_reply(connection,owner,method,params,no_reply=False):
            nonlocal held
            callback=original_send(connection,owner,method,params,no_reply)
            if method=='resolve' and getattr(owner,'_initializer',{}).get('name')==BINDING and held is None:
                held=DeferredReply();callback.future=held
            return callback
        monkeypatch.setattr(Connection,'_send_message_to_server',hold_owned_reply)
        class Teardown(FullSiteQA):
            async def _execute_queue(self):
                page=await self._new_qa_page(context)
                await self._navigate_page(page,site.base+'/')
                await asyncio.wait_for(entered.wait(),3)
                close=asyncio.create_task(self._close_tree(page))
                await asyncio.sleep(.05)
                assert not page.is_closed(), 'Recipient destroyed before its pending resolution finished'
                if release=='before_close':held.release_reply()
                await asyncio.wait_for(close,2)
                assert page.is_closed()
                assert not self._causality.binding_tasks
                assert held.cancelled() is (release=='timeout')
                await asyncio.sleep(.05)
        try:
            await Teardown(site.base+'/',1,1,context=context,delay=0,settle_timeout=600).run()
            assert held is not None and not errors,errors
            assert site.mutations==0
        finally:
            if held is not None and hasattr(held,'saved_result'):held.release_reply()
            loop.set_exception_handler(previous)
    asyncio.run(browser_case(site,check))

@pytest.mark.parametrize('shape', ['e','{error:e}','{error:{error:String(e)}}','{error:{error:"TypeError: Failed to fetch at fetch (QA observation)"}}'])
def test_blocked_application_traffic_is_limited_not_customer_defect(site,shape):
    site.links['/']=[]
    site.script='<script>fetch("/write",{method:"POST"}).catch(e=>console.error('+shape+'));</script>'
    async def check(context,user):
        qa=engine(site,context);results=await qa.run();progress=qa.get_progress()
        assert site.mutations==0
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2,results
        assert any(r['status']=='SAFETY ACTION' for r in results)
        assert any(r['status']=='DIAGNOSTIC' and 'Failed to fetch' in r['actual_result'] for r in results),results
        assert not any(r['status'] in ('PASS','OBSERVATION','FAIL') for r in results),results
        assert all(r['finding_id'] is None for r in results)
        assert progress['pages_tested']==1 and progress['pages_fully_evaluated']==0
        assert progress['desktop_limited']==progress['mobile_limited']==1
        assert progress['safety_limited_viewports']==2 and not progress['coverage_complete']
        assert progress['discovery_safety_limited']
    asyncio.run(browser_case(site,check))


def test_discovery_only_block_does_not_invent_viewport_limits(site):
    site.links['/']=[]
    site.script='<script>if(innerWidth!==1366 && innerWidth!==390)fetch("/write",{method:"POST"}).catch(()=>{});</script>'
    async def check(context,user):
        qa=engine(site,context);results=await qa.run();p=qa.get_progress()
        assert site.mutations==0 and p['discovery_safety_limited']
        assert not p['coverage_complete'] and p['safety_limited_pages']==1
        assert p['safety_limited_viewports']==0 and p['pages_fully_evaluated']==1
        assert sum(r['status']=='PASS' for r in results)==2
        assert any(r['status']=='SAFETY ACTION' for r in results)
    asyncio.run(browser_case(site,check))


def test_redirect_and_block_are_both_retained(site):
    site.links['/']=[];site.redirects['/']='/destination';site.links['/destination']=[]
    site.scripts['/destination']='<script>fetch("/write",{method:"POST"}).catch(()=>console.error({error:{}}));</script>'
    async def check(context,user):
        qa=engine(site,context);results=await qa.run();p=qa.get_progress()
        assert site.mutations==0 and any(r['status']=='REDIRECTED' for r in results)
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2 and not any(r['status']=='PASS' for r in results)
        assert p['redirected']==1 and p['pages_tested']==1 and p['pages_fully_evaluated']==0 and not p['coverage_complete']
    asyncio.run(browser_case(site,check))


@pytest.mark.parametrize('loss',['cancel','session'])
def test_interruption_preserves_limited_checkpoint(site,loss):
    from test_full_site import live_session,run_worker,worker_status
    from app.routes import api
    import threading
    pid,rid=seed(site)
    site.script='<script>fetch("/write",{method:"POST"}).catch(()=>console.error({error:{}}));</script>'
    session=live_session(site,pid);entered=threading.Event();site.delays['/b']=.5
    site.on_get=lambda path:entered.set() if path=='/b' else None
    try:
        worker=run_worker(site,pid,rid);assert entered.wait(10)
        if loss=='cancel':
            from server import app
            assert app.test_client().post(f'/api/runs/{rid}/stop').json['status']=='stopping'
        else:
            from app.qa.interactive import sessions
            sessions.pop(str(pid))
        worker.join(10);assert not worker.is_alive()
        state,coverage,tests=worker_status(rid)
        assert state in ('ABORTED','FAILED') and coverage['pages_tested']==1
        assert coverage['pages_fully_evaluated']==0 and coverage['safety_limited_viewports']==2
        assert not coverage['coverage_complete'] and not any(t['status']=='PASS' for t in tests)
        assert sum(t['status']=='LIMITED COVERAGE' for t in tests)==2
        assert any(t['status']=='DIAGNOSTIC' for t in tests) and site.mutations==0
        assert site.counts['/c']==0
    finally:session.stop()


def test_service_worker_cannot_bypass_limited_guard(site):
    site.links['/']=[]
    site.script='<script>navigator.serviceWorker.register("/sw.js").catch(()=>{});fetch("/write",{method:"POST"}).catch(()=>{});</script>'
    async def check(context,user):
        qa=engine(site,context);results=await qa.run()
        assert not context.service_workers and site.counts['/sw.js']==0 and site.mutations==0
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2
        assert not any(r['status']=='PASS' for r in results)
    asyncio.run(browser_case(site,check))


def test_cancel_in_blocked_viewport_retains_partial_limit(site):
    site.links['/']=[]
    site.script='<script>fetch("/write",{method:"POST"}).catch(e=>{console.error({error:e});window.actualBlockCompleted=true});</script>'
    async def check(context,user):
        qa=engine(site,context);entered=asyncio.Event();navigate=qa._navigate_page
        async def pause_after_actual_block(page,url):
            response=await navigate(page,url)
            if hasattr(page,'qa_safety'):
                await page.wait_for_function('window.actualBlockCompleted===true')
                assert page.qa_safety['blocked'];entered.set()
                await asyncio.Future()
            return response
        qa._navigate_page=pause_after_actual_block
        task=asyncio.create_task(qa.run());await asyncio.wait_for(entered.wait(),5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        p=qa.snapshot()['progress']
        assert p['desktop_limited']==1 and p['mobile_limited']==0
        assert p['pages_tested']==p['pages_fully_evaluated']==0 and not p['coverage_complete']
        assert p['pages_attempted']==1
        assert sum(r['status']=='LIMITED COVERAGE' for r in qa.results)==1
        assert any(r['status']=='DIAGNOSTIC' and 'Failed to fetch' in r['actual_result'] for r in qa.results)
        assert not any(r['status'] in ('PASS','OBSERVATION') for r in qa.results)
        assert site.mutations==0 and not qa._causality.active
    asyncio.run(browser_case(site,check))


def test_limited_exports_and_clipboard_preserve_truth_and_privacy(site):
    import io,zipfile
    from pathlib import Path
    from app.models.database import get_db_connection
    from app.paths import get_exports_dir
    from app.routes import api
    from html.parser import HTMLParser
    class CopyBlock(HTMLParser):
        def __init__(self, source, identifier):
            super().__init__();self.identifier=identifier;self.depth=0;self.parts=[];self.found=False
            self.feed(source)
        def handle_starttag(self,tag,attrs):
            if dict(attrs).get('id')==self.identifier:
                self.depth=1;self.found=True
            elif self.depth:
                self.depth+=1
        def handle_endtag(self,tag):
            if self.depth:self.depth-=1
        def handle_data(self,data):
            if self.depth:self.parts.append(data)
        def get_text(self):return ''.join(self.parts)
        def __str__(self):return self.get_text()
    pid,rid=seed(site);site.links['/']=[]
    site.script='<script>console.error("INDEPENDENT_BEFORE_ABORT");fetch("/write",{method:"POST"}).catch(()=>console.error({error:{},message:"token=FAKE_LIMITED_SECRET <script>HOSTILE_LIMITED</script>"}));</script>'.replace('<script>HOSTILE_LIMITED</script>','&lt;script&gt;HOSTILE_LIMITED&lt;/script&gt;')
    async def check(context,user):
        qa=engine(site,context);results=await qa.run()
        api._save_results(results,rid,pid,coverage=qa.snapshot())
        conn=get_db_connection()
        fid=conn.execute('SELECT id FROM findings WHERE run_id=?',(rid,)).fetchone()[0]
        rows=[dict(r) for r in conn.execute('SELECT * FROM tests WHERE run_id=?',(rid,))];conn.close()
        assert all('FAKE_LIMITED_SECRET' not in str(r) for r in rows)
        assert sum(r['status']=='LIMITED COVERAGE' for r in rows)==2
        from server import app
        client=app.test_client();html=client.get(f'/export/{rid}/html').get_data(as_text=True)
        results_html=client.get(f'/run/{rid}').get_data(as_text=True)
        documents=[html,results_html]
        copied_run=CopyBlock(results_html,'run-report-text')
        assert copied_run.found and 'Pages fully evaluated: 0' in copied_run.get_text()
        documents.append(str(copied_run))
        page=client.get(f'/finding/{fid}').get_data(as_text=True)
        for name in ('report-text','report-html'):
            copied=CopyBlock(page,name);assert copied.found
            documents.append(str(copied))
            assert 'INDEPENDENT_BEFORE_ABORT' in str(copied) and 'DIAGNOSTIC' in str(copied)
            assert 'Pages fully evaluated: 0' in copied.get_text()
        for format in ('markdown','txt','docx'):
            response=client.post(f'/api/export/{rid}/{format}');assert response.status_code==200
            data=(Path(get_exports_dir())/response.json['filename']).read_bytes()
            if format=='docx':
                with zipfile.ZipFile(io.BytesIO(data)) as archive:data=archive.read('word/document.xml')
            documents.append(data.decode())
        assert all('LIMITED COVERAGE' in d and 'SAFETY ACTION' in d and 'DIAGNOSTIC' in d for d in documents)
        assert all('PRIVATE_LEARNING_SENTINEL' not in d and 'FAKE_LIMITED_SECRET' not in d for d in documents)
        assert all('All automated checks passed' not in d for d in documents)
        assert '<script>HOSTILE_LIMITED</script>' not in html
    asyncio.run(browser_case(site,check))

@pytest.mark.parametrize('before', [True,False])
def test_independence_requires_observation_before_abort(site,before):
    site.links['/']=[]
    block="fetch('/write',{method:'POST'}).catch(()=>console.error('GENUINE_INDEPENDENT_JS'));"
    site.script='<script>'+ ("console.error('GENUINE_INDEPENDENT_JS');fetch('/write',{method:'POST'}).catch(()=>{});" if before else block)+'</script>'
    async def check(context,user):
        results=await engine(site,context).run()
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2
        assert not any(r['status']=='PASS' for r in results)
        evidence=[r for r in results if 'GENUINE_INDEPENDENT_JS' in r['actual_result']]
        assert evidence
        findings=[r for r in results if r['status']=='OBSERVATION']
        if before: assert len(findings)==2 and all(r['finding_id'] for r in findings)
        else: assert not findings and all(r['finding_id'] is None for r in evidence)
        assert site.mutations==0
    asyncio.run(browser_case(site,check))

@pytest.mark.parametrize('error', [False,True])
def test_unperturbed_pages_keep_pass_or_finding(site,error):
    site.links['/']=[];site.script='<script>console.error("GENUINE_MANUAL_FIXTURE_ERROR")</script>' if error else ''
    async def check(context,user):
        qa=engine(site,context);results=await qa.run()
        assert sum(r['status']==('OBSERVATION' if error else 'PASS') for r in results)==2,results
        assert not any(r['status']=='LIMITED COVERAGE' for r in results)
        assert qa.get_progress()['pages_fully_evaluated']==1 and qa.get_progress()['coverage_complete']
    asyncio.run(browser_case(site,check))

@pytest.mark.parametrize('width', [1366,390])
def test_limited_viewports_are_isolated(site,width):
    site.links['/']=[];site.script=f'<script>if(innerWidth==={width})fetch("/write",{{method:"POST"}}).catch(()=>{{}});</script>'
    async def check(context,user):
        qa=engine(site,context);results=await qa.run()
        limited=[r for r in results if r['status']=='LIMITED COVERAGE'];passed=[r for r in results if r['status']=='PASS']
        assert len(limited)==len(passed)==1,results
        assert ('Desktop' if width==1366 else 'Mobile') in limited[0]['viewport']
        assert ('Mobile' if width==1366 else 'Desktop') in passed[0]['viewport']
        assert qa.get_progress()['safety_limited_viewports']==1
    asyncio.run(browser_case(site,check))

@pytest.mark.parametrize('method',['POST','PUT','PATCH','DELETE'])
def test_mutation_methods_stay_blocked_and_limited(site,method):
    site.links['/']=[];site.script=f'<script>fetch("/write",{{method:"{method}"}}).catch(()=>{{}})</script>'
    async def check(context,user):
        results=await engine(site,context).run()
        assert site.mutations==0 and sum(r['status']=='LIMITED COVERAGE' for r in results)==2
        assert any(r['status']=='SAFETY ACTION' and 'Method: '+method in r['actual_result'] for r in results)
        assert not any(r['status']=='PASS' for r in results)
    asyncio.run(browser_case(site,check))

def test_page_and_run_do_not_inherit_limits(site):
    site.links['/']=[('/b','B')];site.links['/b']=[];site.script='<script>fetch("/write",{method:"POST"}).catch(()=>{})</script>'
    async def check(context,user):
        first=engine(site,context);results=await first.run()
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2
        passed=[r for r in results if r['status']=='PASS'];assert len(passed)==2 and all(r['url']==site.base+'/b' for r in passed)
        assert first.get_progress()['pages_tested']==2 and first.get_progress()['pages_fully_evaluated']==1
        site.script='';site.links['/']=[]
        second=engine(site,context);results=await second.run()
        assert sum(r['status']=='PASS' for r in results)==2 and not any(r['status']=='LIMITED COVERAGE' for r in results)
        assert not second.get_progress()['discovery_safety_limited']
    asyncio.run(browser_case(site,check))

def test_cap_and_discovery_limits_remain_honest(site):
    site.script='<script>fetch("/write",{method:"POST"}).catch(()=>{})</script>'
    async def check(context,user):
        qa=engine(site,context,page_limit=1);await qa.run();p=qa.get_progress()
        assert p['page_limit_reached'] and p['pages_tested']==1 and p['pages_fully_evaluated']==0
        assert p['discovery_safety_limited'] and not p['coverage_complete'] and p['coverage_gaps']>0
    asyncio.run(browser_case(site,check))

def test_popup_descendants_limit_owner(site):
    site.links['/']=[];site.links['/popup']=[];site.links['/grandchild']=[]
    site.script='<script>window.open("/popup")</script>'
    async def check(context,user):
        results=await engine(site,context).run()
        assert site.counts['/popup'] > 0 and site.counts['/grandchild'] > 0
        assert site.mutations==0 and sum(r['status']=='LIMITED COVERAGE' for r in results)==2,results
        assert not any(r['status']=='PASS' for r in results)
    asyncio.run(browser_case(site,check))
