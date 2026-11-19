"""Independent review: requirement tests; failures document unresolved defects."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest
from flask import Flask
from app.qa.engine import SmartSmokeQA
from app.qa.interactive import InteractiveSession
from app.models.database import init_db, get_db_connection
from app.routes import api

@pytest.fixture
def review_client(tmp_path, monkeypatch):
    monkeypatch.setenv('QA_TESTER_DB_PATH', str(tmp_path / 'review.sqlite'))
    init_db()
    from app.paths import ensure_dirs
    ensure_dirs()
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(api.api_bp)
    return app.test_client()

def project(client, url='https://example.test/start'):
    return client.post('/api/projects', json=dict(name='Review', target_url=url,
        environment='Test', tester_name='Review')).json['id']

def test_current_page_api_uses_live_url(review_client, monkeypatch):
    pid = project(review_client)
    monkeypatch.setattr(api, 'get_session', lambda _: SimpleNamespace(
        get_current_url=lambda: 'https://example.test/account', is_running=True))
    captured = []
    class Thread:
        def __init__(self, target, args): captured.append(args)
        def start(self): pass
    monkeypatch.setattr(api.threading, 'Thread', Thread)
    assert review_client.post('/api/runs', json=dict(project_id=pid, mode='current')).status_code == 200
    assert captured[0][0] == 'https://example.test/account'

def test_collected_urls_reject_other_origin_and_deduplicate_fragments():
    session = InteractiveSession(1, 'https://example.test/')
    session.add_collected_url('https://example.test/a#one')
    session.add_collected_url('https://example.test/a#two')
    session.add_collected_url('https://other.test/private')
    assert session.collected_urls == {'https://example.test/a'}

def test_delete_removes_owned_files_and_preserves_other_project(review_client):
    from app.paths import get_screenshots_dir, get_exports_dir
    pid, other = project(review_client), project(review_client)
    shot = Path(get_screenshots_dir()) / 'review-owned.png'
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b'isolated review fixture')
    conn = get_db_connection()
    rid = conn.execute("INSERT INTO runs(project_id,status) VALUES (?, 'COMPLETED')", (pid,)).lastrowid
    conn.execute("INSERT INTO tests(run_id,url,status,screenshot_path) VALUES (?, 'https://example.test/', 'FAIL', ?)", (rid, str(shot)))
    conn.execute("INSERT INTO findings(project_id,run_id,stable_id,title) VALUES (?,?,'QA-001','Review')", (pid,rid))
    conn.commit(); conn.close()
    export = Path(get_exports_dir()) / f'report_{rid}_review.txt'
    export.parent.mkdir(parents=True, exist_ok=True); export.write_text('isolated report')
    assert review_client.post(f'/api/projects/{pid}/delete').status_code == 200
    conn = get_db_connection()
    assert conn.execute('SELECT id FROM projects WHERE id=?', (other,)).fetchone()
    assert not conn.execute('SELECT id FROM tests WHERE run_id=?', (rid,)).fetchone()
    conn.close()
    assert not shot.exists() and not export.exists()

def test_reset_reports_filesystem_failure(review_client, monkeypatch):
    from app.paths import get_screenshots_dir
    project(review_client)
    shot = Path(get_screenshots_dir()) / 'review-locked.png'
    shot.parent.mkdir(parents=True, exist_ok=True); shot.write_bytes(b'fixture')
    def denied(files): raise PermissionError('review simulated failure')
    with monkeypatch.context() as patch:
        patch.setattr(api, 'staged_delete', denied)
        response = review_client.post('/api/reset')
    assert response.status_code >= 400 or response.json.get('status') != 'success'

def test_screenshot_paths_are_unique_across_runs(tmp_path):
    class Page:
        async def screenshot(self, path, full_page): Path(path).write_bytes(b'fixture')
    a, b = SmartSmokeQA('https://example.test',1,1), SmartSmokeQA('https://example.test',2,2)
    async def capture():
        return (await a.capture_screenshot(Page(),'HTTP_500','QA-001','Desktop'),
                await b.capture_screenshot(Page(),'HTTP_500','QA-001','Desktop'))
    from app.paths import get_screenshots_dir
    Path(get_screenshots_dir()).mkdir(parents=True, exist_ok=True)
    first, second = asyncio.run(capture())
    assert first != second

@pytest.mark.parametrize('case', ['borrowed_mobile', 'valid_label', 'unsafe_post'])
def test_browser_requirements(case):
    from playwright.async_api import async_playwright
    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={'width':1366,'height':768})
            methods = []
            body = '<title>Healthy</title>'
            if case == 'borrowed_mobile': body += '<meta name="viewport" content="width=device-width"><div style="width:500px">Wide</div>'
            if case == 'valid_label': body += '<label for="user">User</label><input id="user">'
            if case == 'unsafe_post': body += '<script>fetch("/write", {method:"POST",body:"fixture"})</script>'
            async def route(r):
                methods.append(r.request.method)
                await r.fulfill(status=200, content_type='text/html', body=body if r.request.resource_type == 'document' else 'ok')
            await context.route('**/*', route)
            engine = SmartSmokeQA('https://example.test/',1,1,mode='current',context=context)
            results = await engine.run()
            await browser.close()
            if case == 'borrowed_mobile':
                assert results[0]['status'] == 'PASS'
                assert results[1]['status'] == 'FAIL', results
            elif case == 'valid_label': assert all(r['status'] == 'PASS' for r in results), results
            else: assert 'POST' not in methods, methods
    asyncio.run(check())

@pytest.mark.parametrize('kind', ['html','docx','txt','markdown','clipboard'])
def test_private_learning_notes_excluded(review_client, kind):
    import zipfile
    from server import app as full_app
    from app.paths import get_exports_dir
    pid = project(review_client)
    conn = get_db_connection()
    rid = conn.execute("INSERT INTO runs(project_id,status) VALUES (?, 'COMPLETED')", (pid,)).lastrowid
    fid = conn.execute("INSERT INTO findings(project_id,run_id,stable_id,title,description,impact,recommendation,plain_language_explanation,learning_explanation_sv,how_to_explain_sv) VALUES (?,?,'QA-001','Review','Description','Impact','Recommendation','Explanation','PRIVATE_LEARN_SENTINEL','PRIVATE_EXPLAIN_SENTINEL')", (pid,rid)).lastrowid
    conn.commit(); conn.close()
    with full_app.test_client() as client:
        if kind == 'clipboard':
            text = client.get(f'/finding/{fid}').get_data(as_text=True)
            text = text.split('<div id="report-text" style="display: none;">')[1].split('<div style="background:')[0]
            # Both hidden clipboard blocks precede the visible learning-note panel.
            text = text.split('<div class="card"')[0]
        elif kind == 'html':
            text = client.get(f'/export/{rid}/html').get_data(as_text=True)
        else:
            response = client.post(f'/api/export/{rid}/{kind}')
            assert response.status_code == 200
            path = Path(get_exports_dir()) / response.json['filename']
            if kind == 'docx':
                with zipfile.ZipFile(path) as z: text = z.read('word/document.xml').decode()
            else: text = path.read_text(encoding='utf-8')
    assert 'PRIVATE_LEARN_SENTINEL' not in text
    assert 'PRIVATE_EXPLAIN_SENTINEL' not in text

def test_error_evidence_redacts_secrets(review_client):
    pid = project(review_client, 'https://example.test/?access_token=TOKEN_SENTINEL')
    conn = get_db_connection()
    rid = conn.execute("INSERT INTO runs(project_id,status) VALUES (?, 'RUNNING')", (pid,)).lastrowid
    conn.commit(); conn.close()
    api._save_results([dict(url='https://example.test/?access_token=TOKEN_SENTINEL',
        final_url='https://example.test/',expected_result='No JS errors',
        actual_result='Console/JS errors detected: Bearer TOKEN_SENTINEL',status='OBSERVATION',
        viewport='Desktop',screenshot=None,finding_id='QA-001')],rid,pid)
    conn = get_db_connection()
    row = conn.execute('SELECT url,actual_result FROM tests WHERE run_id=?',(rid,)).fetchone()
    conn.close()
    assert 'TOKEN_SENTINEL' not in ' '.join(row)


def test_harmless_root_url_normalization():
    engine = SmartSmokeQA('https://example.test',1,1)
    assert engine.normalize_url('https://example.test') == engine.normalize_url('https://example.test/')

