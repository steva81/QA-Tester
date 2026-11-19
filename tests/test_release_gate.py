"""Release-gate regression tests."""
import asyncio, threading
from pathlib import Path
from types import SimpleNamespace
import pytest
from app.models.database import init_db,get_db_connection
from app.routes import api
from flask import Flask

@pytest.fixture
def client(tmp_path,monkeypatch):
    import app.paths
    monkeypatch.setattr(app.paths,'get_writable_dir',lambda:str(tmp_path/'managed'))
    monkeypatch.setenv('QA_TESTER_DB_PATH',str(tmp_path/'gate.sqlite'))
    app.paths.ensure_dirs();init_db()
    from server import app
    app.config['TESTING']=True
    with app.test_client() as c:yield c

def seed():
    conn=get_db_connection()
    pid=conn.execute("INSERT INTO projects(name,target_url,environment,tester_name) VALUES ('Gate','https://example.test/','Test','Gate')").lastrowid
    rid=conn.execute("INSERT INTO runs(project_id,status) VALUES (?, 'RUNNING')",(pid,)).lastrowid
    conn.commit();conn.close();return pid,rid

@pytest.mark.parametrize('status',['COMPLETED','FAILED'])
def test_zero_checks_never_claim_all_pass(client,status):
    pid,rid=seed();conn=get_db_connection();conn.execute('UPDATE runs SET status=? WHERE id=?',(status,rid));conn.commit();conn.close()
    text=client.get(f'/run/{rid}').get_data(as_text=True)
    assert 'All automated checks passed successfully.' not in text

def test_current_origin_check_rejects_prefix_impersonation(client,monkeypatch):
    pid,rid=seed()
    monkeypatch.setattr(api,'get_session',lambda _:SimpleNamespace(is_running=True,loop=True,_stop_event=SimpleNamespace(is_set=lambda:False),context=True,get_current_url=lambda:'https://example.test.attacker.test/private'))
    captured=[]
    class Thread:
        def __init__(self,target,args):captured.append(args)
        def start(self):pass
    monkeypatch.setattr(api.threading,'Thread',Thread)
    response=client.post('/api/runs',json=dict(project_id=pid,mode='current'))
    assert response.status_code >=400 and not captured

def test_worker_failure_sets_terminal_status(client,monkeypatch):
    pid,rid=seed();monkeypatch.setattr(api,'get_session',lambda _:None)
    class FailedEngine:
        def __init__(self,*args,**kwargs):pass
        async def run(self):raise RuntimeError('controlled release-gate failure')
    monkeypatch.setattr(api,'SmartSmokeQA',FailedEngine)
    try:api.run_qa_async_in_thread('https://example.test/',pid,rid)
    except RuntimeError:pass
    finally:api.running_engines.pop(rid,None)
    conn=get_db_connection();status=conn.execute('SELECT status FROM runs WHERE id=?',(rid,)).fetchone()[0];conn.close()
    assert status in ('FAILED','CANCELLED','STOPPED','ABORTED'),status

def test_session_loss_never_uses_anonymous_context(client,monkeypatch):
    pid,rid=seed();monkeypatch.setattr(api,'get_session',lambda _:None);contexts=[]
    class Engine:
        def __init__(self,*args,**kwargs):contexts.append(kwargs.get('context'))
        async def run(self):return []
    monkeypatch.setattr(api,'SmartSmokeQA',Engine)
    api.run_qa_async_in_thread('https://example.test/',pid,rid,mode='current')
    assert not contexts,'Authenticated run silently invoked an anonymous engine'

def test_automated_popup_mutations_are_blocked(client):
    from playwright.async_api import async_playwright
    from app.qa.engine import SmartSmokeQA
    async def check():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            try:
                context=await browser.new_context();escaped=[]
                
                async def context_route(r):
                    if r.request.method!='GET':escaped.append(r.request.method)
                    if r.request.url.endswith('/popup'):
                        body='<title>Popup</title><script>fetch("/write",{method:"POST"}).catch(()=>{})</script>'
                    elif r.request.resource_type=='document':
                        body='<title>Healthy</title><script>window.open("/popup");</script>'
                    else:body='ok'
                    await r.fulfill(status=200,content_type='text/html',body=body)
                await context.route('**/*',context_route)
                await SmartSmokeQA('https://example.test/',1,1,mode='current',context=context).run()
                await asyncio.sleep(.3)
                assert not escaped,escaped
            finally:await browser.close()
    asyncio.run(check())

def test_extraction_popup_mutations_are_blocked(client):
    from playwright.async_api import async_playwright
    from app.qa.engine import SmartSmokeQA
    async def check():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            try:
                context=await browser.new_context();escaped=[]
                async def context_route(r):
                    if r.request.method!='GET':escaped.append(r.request.method)
                    if r.request.url.endswith('/popup'):
                        body='<title>Popup</title><script>fetch("/write",{method:"POST"}).catch(()=>{})</script>'
                    elif r.request.resource_type=='document':
                        body='<title>Healthy</title><script>window.open("/popup");</script>'
                    else:body='ok'
                    await r.fulfill(status=200,content_type='text/html',body=body)
                await context.route('**/*',context_route)
                await SmartSmokeQA('https://example.test/',1,1,mode='crawl',context=context).run()
                await asyncio.sleep(.3)
                assert not escaped,escaped
            finally:await browser.close()
    asyncio.run(check())

def test_hostile_finding_text_is_escaped_in_ui_clipboard_and_export(client):
    pid,rid=seed();payload='<script>window.GATE_SENTINEL=1</script>'
    conn=get_db_connection()
    fid=conn.execute("INSERT INTO findings(project_id,run_id,stable_id,title,description,impact,recommendation,plain_language_explanation) VALUES (?,?,'QA-001','Hostile',?,?,?,?)",(pid,rid,payload,payload,payload,payload)).lastrowid
    conn.commit();conn.close()
    for url in (f'/finding/{fid}',f'/export/{rid}/html'):
        response=client.get(url);assert response.status_code==200
        text=response.get_data(as_text=True)
        assert payload not in text
        assert '&lt;script&gt;' in text

def test_wrapped_label_without_id_passes():
    from playwright.async_api import async_playwright
    from app.qa.engine import SmartSmokeQA
    async def check():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            try:
                context=await browser.new_context()
                await context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body='<title>Healthy</title><label>Name<input></label>'))
                results=await SmartSmokeQA('https://example.test/',1,1,mode='current',context=context).run()
                assert len(results)==2 and all(r['status']=='PASS' for r in results),results
            finally:await browser.close()
    asyncio.run(check())

def test_reset_partial_failure_preserves_evidence(client,monkeypatch):
    from app.paths import get_screenshots_dir
    pid,rid=seed();shots=[Path(get_screenshots_dir())/name for name in ('first.png','second.png')]
    for shot in shots:shot.write_bytes(b'fixture')
    conn=get_db_connection()
    for shot in shots:conn.execute("INSERT INTO tests(run_id,url,status,screenshot_path) VALUES (?, 'https://example.test/', 'FAIL', ?)",(rid,str(shot)))
    conn.commit();conn.close();
    
    import shutil
    original_move = shutil.move
    calls = []
    def failing_move(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise PermissionError('controlled second-file failure')
        return original_move(src, dst)
        
    with monkeypatch.context() as patch:
        patch.setattr(api.shutil, 'move', failing_move)
        response=client.post('/api/reset')
    assert response.status_code==500
    assert all(shot.exists() for shot in shots),'Database evidence references survive while a file has already been deleted'

def test_desktop_shutdown_closes_interactive_session(monkeypatch):
    from app.routes import api
    from app.qa.interactive import sessions
    sessions.clear()
    api.active_worker_threads.clear()
    
    class FakeSession:
        def __init__(self):
            self.stopped = False
        def stop(self):
            self.stopped = True

    sess = FakeSession()
    sessions['123'] = sess
    
    api.shutdown_qa_workers()
    
    assert sess.stopped
    sessions.clear()
