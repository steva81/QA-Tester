"""Maintained regressions for context protection and coordinated cancellation."""
import asyncio
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from types import SimpleNamespace

import pytest

from app.qa.engine import SmartSmokeQA
from app.models.database import get_db_connection, init_db
from app.routes import api


@pytest.fixture
def worker_state(tmp_path, monkeypatch):
    import app.qa.interactive as interactive
    monkeypatch.setenv('QA_TESTER_DB_PATH', str(tmp_path / 'workers.sqlite'))
    init_db()
    monkeypatch.setattr(api, 'worker_records', {})
    monkeypatch.setattr(api, 'running_engines', {})
    monkeypatch.setattr(api, 'active_worker_threads', [])
    monkeypatch.setattr(api, 'accepting_runs', True)
    monkeypatch.setattr(interactive, 'sessions', {})
    monkeypatch.setattr(api, 'get_session', lambda _: None)
    yield
    api.shutdown_qa_workers()
    assert not api.worker_records
    assert not api.active_worker_threads
    assert not api.running_engines


def seed():
    conn = get_db_connection()
    pid = conn.execute("INSERT INTO projects(name,target_url,environment,tester_name) VALUES ('Cancel','https://example.test/','Test','QA')").lastrowid
    rid = conn.execute("INSERT INTO runs(project_id,status) VALUES (?, 'RUNNING')", (pid,)).lastrowid
    conn.commit()
    conn.close()
    return pid, rid


def status(rid):
    conn = get_db_connection()
    result = conn.execute('SELECT status FROM runs WHERE id=?', (rid,)).fetchone()[0]
    conn.close()
    return result


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
@pytest.mark.parametrize('scenario', ['main', 'extraction_popup', 'grandchild', 'delayed', 'current', 'collected', 'service_worker'])
def test_server_receives_zero_automated_mutations(method, scenario):
    counts = Counter()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            with lock:
                counts[self.path] += 1
                visit = counts[self.path]
            script = ''
            mutation = f'fetch("/attempt").then(()=>fetch("/write",{{method:"{method}"}})).catch(()=>{{}});'
            if self.path == '/main':
                if scenario == 'service_worker':
                    script = 'navigator.serviceWorker.register("/sw.js").catch(()=>{});' + mutation
                elif scenario in ('main', 'current', 'collected'):
                    script = mutation + 'let f=document.createElement("iframe");f.src="/iframe";document.body.append(f);'
                elif scenario != 'extraction_popup' or visit == 3:
                    script = 'window.open("/popup");'
            elif self.path == '/popup':
                script = mutation + 'window.open("/grandchild");'
                if scenario == 'delayed':
                    script += f'let timer=setInterval(()=>{{if(!opener || opener.closed){{clearInterval(timer);fetch("/after-parent-close").then(()=>fetch("/write",{{method:"{method}"}})).catch(()=>{{}});}}}},20);'
            elif self.path in ('/grandchild', '/iframe'):
                script = f'setTimeout(()=>{{{mutation}}},100);' if scenario == 'delayed' else mutation
            body = ('<title>Local QA</title><body><script>' + script + '</script></body>').encode()
            if self.path == '/sw.js':
                body = f'self.addEventListener("install", () => fetch("/write", {{method:"{method}"}}));'.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript' if self.path == '/sw.js' else 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def mutate(self):
            with lock:
                counts['mutations'] += 1
            self.send_response(200)
            self.end_headers()

        do_POST = do_PUT = do_PATCH = do_DELETE = mutate

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'

    class ObservedEngine(SmartSmokeQA):
        async def _close_tree(self, page):
            if scenario == 'delayed':
                await page.close()
                await asyncio.sleep(.3)
            else:
                # Make extraction's first-request race observable at the real server.
                await asyncio.sleep(.2)
                await super()._close_tree(page)

    async def check():
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
            try:
                context = await browser.new_context(service_workers='allow' if scenario == 'service_worker' else 'block')
                blocked_methods = []
                context.on('requestfailed', lambda request: blocked_methods.append(request.method) if request.method == method else None)
                user = await context.new_page()
                await user.goto(base + '/manual')
                mode = scenario if scenario in ('current', 'collected') else 'crawl'
                results = await ObservedEngine(base + '/main', 1, 1, max_pages=1, max_depth=1,
                                              mode=mode, collected_urls=[base + '/main'], context=context).run()
                await asyncio.sleep(.2)
                assert results
                assert counts['/attempt'] > 0, 'No unsafe request attempt was exercised'
                if scenario in ('extraction_popup', 'grandchild', 'delayed'):
                    assert counts['/popup'] > 0 and counts['/grandchild'] > 0
                if scenario == 'delayed':
                    assert counts['/after-parent-close'] > 0, 'Delayed write after parent teardown was not exercised'
                assert counts['mutations'] == 0, dict(counts)
                assert method in blocked_methods, 'No actual unsafe request was aborted'
                if scenario == 'service_worker':
                    assert counts['/sw.js'] == 0, 'Service worker registration escaped protection'
                assert context.pages == [user], 'QA descendants survived teardown'
                # Manual session remains usable and its mutation policy is restored.
                await user.evaluate(f'fetch("/manual-write",{{method:"{method}"}})')
                assert counts['mutations'] == 1
            finally:
                await browser.close()
    try:
        asyncio.run(check())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def test_exception_is_failed_and_redacted(worker_state, monkeypatch):
    pid, rid = seed()
    class Engine:
        def __init__(self, *args, **kwargs): pass
        async def run(self): raise RuntimeError('Bearer PRIVATE_SENTINEL')
    monkeypatch.setattr(api, 'SmartSmokeQA', Engine)
    api.run_qa_async_in_thread('https://example.test/', pid, rid)
    assert status(rid) == 'FAILED'
    conn = get_db_connection()
    text = conn.execute('SELECT actual_result FROM tests WHERE run_id=?', (rid,)).fetchone()[0]
    conn.close()
    assert 'PRIVATE_SENTINEL' not in text
    assert not api.worker_records


def test_lost_session_is_aborted(worker_state):
    pid, rid = seed()
    api.run_qa_async_in_thread('https://example.test/', pid, rid, mode='current')
    assert status(rid) == 'ABORTED'


def test_shutdown_cancels_slow_worker_and_rejects_new_runs(worker_state, monkeypatch):
    pid, rid = seed()
    started = threading.Event()
    cleaned = threading.Event()
    stopped = threading.Event()
    class Engine:
        def __init__(self, *args, **kwargs): pass
        def stop(self): stopped.set()
        async def run(self):
            started.set()
            try: await asyncio.sleep(600)
            finally: cleaned.set()
    monkeypatch.setattr(api, 'SmartSmokeQA', Engine)
    worker = threading.Thread(target=api.run_qa_async_in_thread, args=('https://example.test/', pid, rid))
    api.active_worker_threads.append(worker)
    worker.start()
    assert started.wait(3)
    before = time.monotonic()
    api.shutdown_qa_workers()
    assert time.monotonic() - before < 3
    assert stopped.is_set() and cleaned.is_set()
    assert not worker.is_alive()
    assert status(rid) == 'ABORTED'
    from server import app
    response = app.test_client().post('/api/runs', json={'project_id': pid}, headers={'X-QA-Request': '1'})
    assert response.status_code == 503
    conn = get_db_connection()
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE status='RUNNING'").fetchone()[0] == 0
    conn.close()


def test_completed_is_preserved_during_shutdown(worker_state, monkeypatch):
    pid, rid = seed()
    class Engine:
        def __init__(self, *args, **kwargs): pass
        async def run(self): return []
    monkeypatch.setattr(api, 'SmartSmokeQA', Engine)
    api.run_qa_async_in_thread('https://example.test/', pid, rid)
    api.shutdown_qa_workers()
    assert status(rid) == 'COMPLETED'


def test_shutdown_timeout_is_terminal_and_late_worker_cannot_save(worker_state, monkeypatch):
    pid, rid = seed()
    started, release = threading.Event(), threading.Event()
    class Engine:
        def __init__(self, *args, **kwargs): pass
        def stop(self): pass
        async def run(self):
            started.set()
            release.wait(5)  # Deliberately uncooperative operation, released by test cleanup.
            return [dict(url='https://example.test/', expected_result='Pass', actual_result='late',
                         status='PASS', viewport='Desktop', screenshot=None, finding_id=None)]
    monkeypatch.setattr(api, 'SmartSmokeQA', Engine)
    monkeypatch.setattr(api, 'WORKER_SHUTDOWN_TIMEOUT', .1)
    worker = threading.Thread(target=api.run_qa_async_in_thread, args=('https://example.test/', pid, rid))
    api.active_worker_threads.append(worker)
    try:
        worker.start()
        assert started.wait(3)
        api.shutdown_qa_workers()
        assert worker.is_alive(), 'The test did not exercise shutdown timeout'
        assert status(rid) == 'ABORTED'
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert status(rid) == 'ABORTED'
    conn = get_db_connection()
    assert conn.execute("SELECT COUNT(*) FROM tests WHERE run_id=? AND actual_result='late'", (rid,)).fetchone()[0] == 0
    conn.close()
    assert not api.worker_records


def test_late_worker_cannot_persist_results_over_aborted(worker_state, monkeypatch):
    pid, rid = seed()
    late = dict(url='https://example.test/', expected_result='Pass', actual_result='late', status='PASS',
                viewport='Desktop', screenshot=None, finding_id=None)
    class Engine:
        def __init__(self, *args, **kwargs): pass
        async def run(self):
            api._mark_terminal(rid, 'QA run interrupted during application shutdown.')
            return [late]
    monkeypatch.setattr(api, 'SmartSmokeQA', Engine)
    api.run_qa_async_in_thread('https://example.test/', pid, rid)
    api._save_results([late], rid, pid)
    assert status(rid) == 'ABORTED'
    conn = get_db_connection()
    assert conn.execute("SELECT COUNT(*) FROM tests WHERE run_id=? AND actual_result='late'", (rid,)).fetchone()[0] == 0
    conn.close()


def test_cancelled_worker_returning_results_is_aborted(worker_state, monkeypatch):
    pid, rid = seed()
    class Engine:
        def __init__(self, *args, **kwargs): pass
        async def run(self):
            api.worker_records[rid].cancel.set()
            return []
    monkeypatch.setattr(api, 'SmartSmokeQA', Engine)
    api.run_qa_async_in_thread('https://example.test/', pid, rid)
    assert status(rid) == 'ABORTED'


def test_persistence_exception_rolls_back_and_marks_failed(worker_state, monkeypatch):
    pid, rid = seed()
    class Engine:
        def __init__(self, *args, **kwargs): pass
        async def run(self): return [{'url': 'https://example.test/'}]
    monkeypatch.setattr(api, 'SmartSmokeQA', Engine)
    api.run_qa_async_in_thread('https://example.test/', pid, rid)
    assert status(rid) == 'FAILED'


def test_shutdown_cleans_real_interactive_browser(worker_state, monkeypatch):
    from app.qa.interactive import InteractiveSession
    import app.qa.interactive as interactive
    from playwright.async_api import async_playwright
    pid, rid = seed()
    started = threading.Event()
    cleaned = threading.Event()
    class Session(InteractiveSession):
        async def _init_playwright(self):
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(chromium_sandbox=True, headless=True)
            self.context = await self.browser.new_context(service_workers='block')
            async def serve(route):
                await route.fulfill(body='<title>Controlled</title>', content_type='text/html')
            await self.context.route('**/*', serve)
            self.page = await self.context.new_page()
            await self.page.goto(self.start_url)
    class PendingEngine(SmartSmokeQA):
        async def _test_page(self, context, *args):
            page = await context.new_page()
            await page.goto(self.target_url)
            started.set()
            try: await asyncio.sleep(600)
            finally: cleaned.set()
    session = Session(pid, 'https://example.test/')
    session.start()
    interactive.sessions[str(pid)] = session
    monkeypatch.setattr(api, 'get_session', lambda _: session)
    monkeypatch.setattr(api, 'SmartSmokeQA', PendingEngine)
    worker = threading.Thread(target=api.run_qa_async_in_thread, args=('https://example.test/', pid, rid, 'current'))
    api.active_worker_threads.append(worker)
    try:
        worker.start()
        assert started.wait(5)
        api.shutdown_qa_workers()
        assert not worker.is_alive() and not session.thread.is_alive()
        assert session.loop.is_closed() and not session.browser.is_connected()
        assert cleaned.is_set() and status(rid) == 'ABORTED'
    finally:
        session.stop()
        worker.join(3)


def test_interactive_future_is_cancelled_and_loop_cleaned(worker_state, monkeypatch):
    import app.qa.interactive as interactive
    pid, rid = seed()
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever)
    loop_thread.start()
    started, cleaned = threading.Event(), threading.Event()
    class Engine:
        def __init__(self, *args, **kwargs): pass
        def stop(self): pass
        async def run(self):
            started.set()
            try: await asyncio.sleep(600)
            finally: cleaned.set()
    session = SimpleNamespace(loop=loop, context=object(), _stop_event=threading.Event())
    def stop():
        session._stop_event.set()
        assert cleaned.wait(3)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(3)
    session.stop = stop
    monkeypatch.setattr(api, 'get_session', lambda _: session)
    monkeypatch.setattr(api, 'SmartSmokeQA', Engine)
    interactive.sessions[str(pid)] = session
    worker = threading.Thread(target=api.run_qa_async_in_thread, args=('https://example.test/', pid, rid, 'current'))
    api.active_worker_threads.append(worker)
    try:
        worker.start()
        assert started.wait(3)
        future = api.worker_records[rid].future
        api.shutdown_qa_workers()
        assert future.cancelled() and cleaned.is_set()
        assert not worker.is_alive() and not loop_thread.is_alive()
        assert not asyncio.all_tasks(loop)
        assert status(rid) == 'ABORTED'
        assert not interactive.sessions
    finally:
        if loop_thread.is_alive():
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(3)
        loop.close()
