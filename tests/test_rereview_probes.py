import asyncio, threading, os
from pathlib import Path
import pytest
from flask import Flask
from app.models.database import init_db, get_db_connection
from app.routes import api
from app.qa.engine import SmartSmokeQA
from app.qa.interactive import InteractiveSession

@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.paths
    monkeypatch.setattr(app.paths, 'get_writable_dir', lambda: str(tmp_path / 'managed'))
    monkeypatch.setenv('QA_TESTER_DB_PATH', str(tmp_path / 'review.sqlite'))
    app.paths.ensure_dirs(); init_db()
    
    from server import app
    app.config['TESTING'] = True
    with app.test_client() as c: yield c

def seed():
    conn = get_db_connection()
    pid = conn.execute("INSERT INTO projects(name,target_url,environment,tester_name) VALUES ('Probe','https://example.test/','Test','Probe')").lastrowid
    rid = conn.execute("INSERT INTO runs(project_id,status) VALUES (?, 'COMPLETED')", (pid,)).lastrowid
    conn.commit(); conn.close(); return pid,rid

@pytest.mark.parametrize('method', ['POST','PUT','PATCH','DELETE'])
def test_crawl_blocks_mutation_during_link_extraction(method):
    from playwright.async_api import async_playwright
    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                escaped = []
                async def route(r):
                    if r.request.method != 'GET': escaped.append(r.request.method)
                    body = '<title>Probe</title><script>fetch("/write", {method:"' + method + '"}).catch(()=>{})</script>'
                    await r.fulfill(status=200,content_type='text/html',body=body if r.request.resource_type=='document' else 'ok')
                await context.route('**/*',route)
                engine = SmartSmokeQA('https://example.test/',1,1,max_pages=1,max_depth=1,context=context)
                await engine.run()
                assert not escaped, escaped
            finally: await browser.close()
    asyncio.run(check())

def test_finding_description_redacted(client):
    pid,rid = seed()
    api._save_results([dict(url='https://example.test/',final_url='https://example.test/',
        expected_result='No errors',actual_result='Bearer SECRET_SENTINEL',status='OBSERVATION',
        screenshot=None,viewport='Desktop',finding_id='QA-001')],rid,pid)
    conn = get_db_connection(); row = conn.execute('SELECT description FROM findings WHERE run_id=?',(rid,)).fetchone(); conn.close()
    assert 'SECRET_SENTINEL' not in row['description']

def test_reset_failure_preserves_db_state(client, monkeypatch):
    from app.paths import get_screenshots_dir
    pid,_ = seed()
    (Path(get_screenshots_dir())/'blocked.png').write_bytes(b'fixture')
    with monkeypatch.context() as patch:
        patch.setattr(api,'staged_delete',lambda _: (_ for _ in ()).throw(PermissionError('isolated fixture')))
        response=client.post('/api/reset')
    assert response.status_code == 500
    conn=get_db_connection(); row=conn.execute('SELECT id FROM projects WHERE id=?',(pid,)).fetchone(); conn.close()
    assert row is not None

def test_reset_rejects_symlink_escape(client, tmp_path):
    from app.paths import get_exports_dir
    managed=Path(get_exports_dir()); external=tmp_path/'outside'; external.mkdir()
    sentinel=external/'keep.txt'; sentinel.write_text('outside managed storage')
    managed.rmdir()
    try: managed.symlink_to(external,target_is_directory=True)
    except OSError:
        import subprocess
        probe_env = dict(os.environ, QA_REVIEW_LINK=str(managed), QA_REVIEW_TARGET=str(external))
        created = subprocess.run(['powershell', '-NoProfile', '-Command', 'New-Item -ItemType Junction -Path $env:QA_REVIEW_LINK -Target $env:QA_REVIEW_TARGET | Out-Null'], env=probe_env, capture_output=True)
        if created.returncode: pytest.skip('Temporary junction unavailable')
    try:
        response=client.post('/api/reset')
        assert sentinel.exists(), response.json
    finally: os.rmdir(managed) if not managed.is_symlink() else managed.unlink()

def test_delete_preserves_shared_legacy_evidence_of_project_b(client):
    from app.paths import get_screenshots_dir
    a,ra=seed(); b,rb=seed()
    shot=Path(get_screenshots_dir())/'QA-001_Desktop_HTTP_500.png'; shot.write_bytes(b'legacy fixture')
    conn=get_db_connection()
    for rid in (ra,rb): conn.execute("INSERT INTO tests(run_id,url,status,screenshot_path) VALUES (?, 'https://example.test/', 'FAIL', ?)",(rid,str(shot)))
    conn.commit(); conn.close()
    client.post(f'/api/projects/{a}/delete')
    assert shot.exists()

def test_delete_reports_legacy_file_failure(client,monkeypatch):
    from app.paths import get_screenshots_dir
    pid,rid=seed(); shot=Path(get_screenshots_dir())/'legacy-locked.png'; shot.write_bytes(b'fixture')
    conn=get_db_connection();conn.execute("INSERT INTO tests(run_id,url,status,screenshot_path) VALUES (?, 'https://example.test/', 'FAIL', ?)",(rid,str(shot)));conn.commit();conn.close()
    def blocked(files):
        raise PermissionError('isolated fixture')
    with monkeypatch.context() as patch:
        patch.setattr(api,'staged_delete',blocked)
        response=client.post(f'/api/projects/{pid}/delete')
    assert response.status_code >= 400 or response.json.get('status') != 'success'

def test_collected_scope_validated_in_run_api(client,monkeypatch):
    pid,_=seed(); captured=[]
    class Thread:
        def __init__(self,target,args):captured.append(args)
        def start(self):pass
    monkeypatch.setattr(api.threading,'Thread',Thread)
    response=client.post('/api/runs',json=dict(project_id=pid,mode='collected',collected_urls=['https://other.test/private','https://example.test/a#one','https://example.test/a#two']))
    assert response.status_code >= 400 or captured[0][4] == ['https://example.test/a']

@pytest.mark.parametrize('status',['AUTH REQUIRED','REDIRECTED'])
def test_blocked_run_has_no_all_pass_message(client,status):
    from server import app
    pid,rid=seed()
    conn=get_db_connection();conn.execute("INSERT INTO tests(run_id,url,status) VALUES (?, 'https://example.test/', ?)",(rid,status));conn.commit();conn.close()
    with app.test_client() as c: text=c.get(f'/run/{rid}').get_data(as_text=True)
    assert 'All automated checks passed successfully.' not in text

def test_html_report_escapes_project_title(client):
    from server import app
    pid,rid=seed();hostile='</title><script>window.REVIEW_SENTINEL=1</script>'
    conn=get_db_connection();conn.execute('UPDATE projects SET name=? WHERE id=?',(hostile,pid));conn.commit();conn.close()
    with app.test_client() as c:text=c.get(f'/export/{rid}/html').get_data(as_text=True)
    assert hostile not in text

def test_startup_failure_returns_without_deadlock():
    session=InteractiveSession(1,'https://example.test/')
    def failed_init():session._error='simulated initialization failure';session._ready_event.set()
    session._run_loop=failed_init
    errors=[]
    def start():
        try:session.start()
        except Exception as exc:errors.append(str(exc))
    caller=threading.Thread(target=start,daemon=True);caller.start();caller.join(timeout=1)
    assert not caller.is_alive(), 'start() re-enters non-reentrant self.lock through stop()'
    assert errors

@pytest.mark.parametrize('method', ['POST','PUT','PATCH','DELETE'])
def test_current_blocks_mutation_and_preserves_cookie_viewports(method):
    from playwright.async_api import async_playwright
    async def check():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            try:
                context=await browser.new_context(viewport={'width':1111,'height':777})
                await context.add_cookies([dict(name='review_auth',value='fake-cookie',url='https://example.test/')])
                user_page=await context.new_page()
                sizes=[];cookies=[];escaped=[]
                async def route(r):
                    if r.request.resource_type=='document':
                        sizes.append(r.request.frame.page.viewport_size)
                        cookies.append((await r.request.all_headers()).get('cookie',''))
                    if r.request.method != 'GET':escaped.append(r.request.method)
                    body='<title>Probe</title><script>fetch("/write", {method:"'+method+'"}).catch(()=>{})</script>'
                    await r.fulfill(status=200,content_type='text/html',body=body if r.request.resource_type=='document' else 'ok')
                await context.route('**/*',route)
                engine=SmartSmokeQA('https://example.test/',1,1,mode='current',context=context)
                await engine.run()
                assert escaped == []
                assert sizes == [{'width':1366,'height':768},{'width':390,'height':844}]
                assert all('review_auth=fake-cookie' in cookie for cookie in cookies)
                assert user_page.viewport_size == {'width':1111,'height':777}
                assert not user_page.is_closed()
            finally:await browser.close()
    asyncio.run(check())
