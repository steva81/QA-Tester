"""Deterministic Phase 2 fixtures and adversarial Full Site QA regressions."""
import asyncio
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from urllib.parse import urlsplit, parse_qs
import pytest

from app.qa.full_site import FullSiteQA
from app.qa.crawl_policy import normalize_crawl_url, link_reason
from app.utils import get_canonical_origin
from app.models.database import init_db, get_db_connection
from app.models.crawl import get_coverage
from app.routes import api


class LocalSite:
    def __init__(self):
        self.links = {'/': [('/b', 'Community')], '/b': [('/c', 'Academy')], '/c': []}
        self.redirects = {'/alias': '/b', '/loop1': '/loop2', '/loop2': '/loop1'}
        self.statuses = {}
        self.delays = {}
        self.script = ''
        self.scripts = {}
        self.counts = Counter()
        self.cookies = []
        self.viewports = []
        self.on_get = None
        self.mutations = 0
        self.method = 'POST'
        self.lock = threading.Lock()
        site = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                parsed = urlsplit(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                cookie = self.headers.get('Cookie', '')
                with site.lock:
                    site.counts[path] += 1
                    site.cookies.append((path, cookie))
                if site.on_get:
                    site.on_get(path)
                time.sleep(site.delays.get(path, 0))
                if path == '/viewport':
                    with site.lock:
                        site.viewports.append((query['path'][0], int(query['w'][0]), int(query['h'][0])))
                    self.send_response(200)
                    self.send_header('Content-Length', '2')
                    self.end_headers()
                    try: self.wfile.write(b'ok')
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
                    return
                if path in site.redirects:
                    self.send_response(302)
                    self.send_header('Location', site.redirects[path]); self.end_headers(); return
                if path not in ('/login', '/visible', '/broken.js', '/blocked-get.js') and 'qa_login=yes' not in cookie:
                    self.send_response(302); self.send_header('Location', '/login'); self.end_headers(); return
                if path == '/logout':
                    self.send_response(200); self.send_header('Set-Cookie', 'qa_login=; Max-Age=0; Path=/')
                    self.end_headers(); return
                if path == '/broken.js':
                    self.send_response(500); self.end_headers(); return
                if path == '/sw.js':
                    body = b'self.addEventListener("install",()=>fetch("/write",{method:"POST"}));'
                    content_type = 'application/javascript'
                else:
                    links = site.links.get(path, [])
                    if path == '/calendar':
                        day = int(query.get('day', ['0'])[0])
                        links = [(f'/calendar?day={day+1}', 'Next day')]
                    body = ('<title>Local authenticated QA</title><body>' + ''.join(
                        f'<a href="{url}">{label}</a>' for url, label in links) +
                        '<label for="field">Name</label><form><input id="field" name="name"></form>' +
                        '<script>fetch("/viewport?path="+encodeURIComponent(location.pathname)+"&w="+innerWidth+"&h="+innerHeight).then(r=>r.text());</script>' +
                        (site.script if path == '/' else '') +
                        site.scripts.get(path, '') +
                        (f'<script>window.open("/grandchild");fetch("/write",{{method:"{site.method}"}}).catch(()=>{{}});</script>' if path == '/popup' else '') +
                        (f'<script>setTimeout(()=>fetch("/write",{{method:"{site.method}"}}).catch(()=>{{}}),50);</script>' if path == '/grandchild' else '') + '</body>').encode()
                    content_type = 'text/html'
                self.send_response(site.statuses.get(path, 200))
                if path == '/login': self.send_header('Set-Cookie', 'qa_login=yes; Path=/; HttpOnly')
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body))); self.end_headers()
                try: self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
            def mutate(self):
                with site.lock: site.mutations += 1
                self.send_response(200); self.end_headers()
            do_POST = do_PUT = do_PATCH = do_DELETE = mutate
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        assert not self.thread.is_alive()


@pytest.fixture
def site():
    instance = LocalSite()
    try: yield instance
    finally: instance.close()


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    import app.qa.interactive as interactive
    monkeypatch.setenv('QA_TESTER_DB_PATH', str(tmp_path / 'full.sqlite'))
    from app.paths import ensure_dirs
    ensure_dirs()
    init_db()
    monkeypatch.setattr(api, 'worker_records', {})
    monkeypatch.setattr(api, 'running_engines', {})
    monkeypatch.setattr(api, 'active_worker_threads', [])
    monkeypatch.setattr(api, 'accepting_runs', True)
    monkeypatch.setattr(interactive, 'sessions', {})
    yield
    assert not api.worker_records
    assert not api.active_worker_threads
    assert not api.running_engines


async def browser_case(site, check):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        try:
            context = await browser.new_context(service_workers='block', viewport={'width': 1111, 'height': 777})
            user = await context.new_page()
            await user.goto(site.base + '/login')
            await user.goto(site.base + '/visible')
            try:
                await check(context, user)
            except Exception:
                hold = getattr(context, '_qa_safety_hold', None)
                if hold is not None:
                    import traceback
                    print('RETAINED SAFETY GUARD:', 'pending=', hold.pending, 'tasks=', len(hold.tasks))
                    for error in hold.errors:
                        traceback.print_exception(error)
                raise
            assert context.pages == [user], 'QA pages or popup descendants survived'
            assert not user.is_closed() and user.url == site.base + '/visible'
            assert user.viewport_size == {'width': 1111, 'height': 777}
            assert any(c['name'] == 'qa_login' and c['value'] == 'yes' for c in await context.cookies())
        finally: await browser.close()


def engine(site, context, **options):
    return FullSiteQA(site.base + '/', 1, 1, context=context, delay=0, settle_timeout=600, **options)


def test_authenticated_recursive_bfs_and_visible_isolation(site):
    async def check(context, user):
        qa = engine(site, context)
        await qa.run()
        assert list(qa.records) == [site.base + p for p in ('/', '/b', '/c')]
        assert qa.get_progress()['pages_tested'] == 3
        assert qa.get_progress()['desktop_pages_tested'] == qa.get_progress()['mobile_pages_tested'] == 3
        assert qa.get_progress()['coverage_complete']
        for path in ('/', '/b', '/c'):
            assert (path, 1366, 768) in site.viewports and (path, 390, 844) in site.viewports
            assert all('qa_login=yes' in cookie for actual, cookie in site.cookies if actual == path)
        assert qa.records[site.base + '/']['form_count'] == 1
    asyncio.run(browser_case(site, check))


@pytest.mark.parametrize('url', ['https://example.test/x', 'http://example.test:81/x',
                                   'http://example.test.attacker.test/x', 'http://other.test/x',
                                   'javascript:alert(1)', 'mailto:qa@example.test', 'tel:123',
                                   'http://example.test/logout', 'http://example.test/delete',
                                   'http://example.test/remove', 'http://example.test/deactivate',
                                   'http://example.test/account/cancel', 'http://example.test/?action=destroy',
                                   'http://example.test/unsubscribe'])
def test_unsafe_or_external_links_are_excluded(url):
    assert link_reason(url, get_canonical_origin('http://example.test/'))


def test_same_origin_default_port_and_label_filter():
    assert not link_reason('http://EXAMPLE.test:80/community', 'http://example.test:80')
    assert link_reason('http://example.test/action?id=1', 'http://example.test:80', 'Delete account')


def test_conservative_normalization():
    a = normalize_crawl_url('http://EXAMPLE.test:80/b?z=1&a=2&utm_source=x#fragment')
    assert a == normalize_crawl_url('http://example.test/b?a=2&z=1&fbclid=tracking')
    assert a != normalize_crawl_url('http://example.test/b?a=3&z=1')
    assert normalize_crawl_url('http://example.test/b?x=1&x=2') != normalize_crawl_url('http://example.test/b?x=2&x=1')
    assert normalize_crawl_url('http://example.test/b/') != normalize_crawl_url('http://example.test/b')


def test_duplicates_queries_destructive_links_and_coverage(site):
    site.links['/'] = [('/b', 'B'), ('/b#part', 'B'), ('/b?utm_source=mail', 'B'),
                       ('/b?a=1&z=2', 'Filter'), ('/b?z=2&a=1', 'Same filter'), ('/b?a=2&z=2', 'Other filter'),
                       ('/logout', 'Logout'), ('/delete', 'Delete'), ('/remove', 'Remove'), ('/deactivate', 'Deactivate'),
                       ('javascript:alert(1)', 'JS'), ('mailto:qa@example.test', 'Mail'), ('tel:123', 'Phone'),
                       ('http://example.test.attacker.test/x', 'External')]
    site.links['/b'] = []
    async def check(context, user):
        qa = engine(site, context)
        await qa.run()
        progress = qa.get_progress()
        assert progress['pages_discovered'] == 12 and progress['pages_eligible'] == 4
        assert progress['pages_tested'] == 4 and progress['skipped_safely'] == 8
        assert not any(site.counts[p] for p in ('/logout', '/delete', '/remove', '/deactivate'))
    asyncio.run(browser_case(site, check))


def test_page_limit_reports_gap_and_does_not_visit_next_page(site):
    async def check(context, user):
        qa = engine(site, context, page_limit=2)
        await qa.run()
        progress = qa.get_progress()
        assert progress['pages_tested'] == 2 and progress['page_limit_reached']
        assert progress['coverage_gaps'] == 1 and not progress['coverage_complete']
        assert site.counts['/c'] == 0
    asyncio.run(browser_case(site, check))


def test_redirect_alias_and_trailing_slash_do_not_consume_target_slot(site):
    site.redirects['/'] = '/b/'
    site.redirects['/b/'] = '/b'
    site.links['/b'] = []
    async def check(context, user):
        qa = engine(site, context, page_limit=1)
        results = await qa.run()
        progress = qa.get_progress()
        assert progress['pages_tested'] == 1 and not progress['page_limit_reached']
        assert progress['redirected'] == 1
        assert qa.records[site.base + '/']['final_url'] == site.base + '/b'
        assert qa.records[site.base + '/b']['desktop_tested'] and qa.records[site.base + '/b']['mobile_tested']
        assert any(r['status'] == 'REDIRECTED' and r['url'] == site.base + '/' and r['final_url'] == site.base + '/b' for r in results)
    asyncio.run(browser_case(site, check))


def test_redirect_loop_and_navigation_timeout_are_bounded(site):
    site.links['/'] = [('/loop1', 'Loop'), ('/slow', 'Slow')]
    site.delays['/slow'] = .4
    async def check(context, user):
        qa = engine(site, context)
        navigate_once = qa._navigate_once
        async def timeout_only_deliberately_slow_target(page, url):
            # Setup/redirect responses must not time out before this fixture
            # reaches its intentional slow response under full-suite load.
            original_timeout = qa.navigation_timeout
            qa.navigation_timeout = 100 if url == site.base + '/slow' else 2000
            try:
                return await navigate_once(page, url)
            finally:
                qa.navigation_timeout = original_timeout
        qa._navigate_once = timeout_only_deliberately_slow_target
        before = time.monotonic()
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop_errors = []
        loop.set_exception_handler(lambda loop,error: loop_errors.append(error))
        try:
            await qa.run()
            await asyncio.sleep(0)
            assert not loop_errors, loop_errors
        finally:
            loop.set_exception_handler(previous_handler)
        assert time.monotonic() - before < 8
        assert qa.get_progress()['failed_to_load'] == 2
        assert qa.get_progress()['coverage_gaps'] == 2
        assert site.counts['/loop1'] < 30
        assert site.counts['/loop1'] == site.counts['/loop2'] == 10
        assert 1 <= site.counts['/slow'] <= 2
    asyncio.run(browser_case(site, check))


def test_query_explosion_is_bounded_without_collapsing_values(site):
    site.links['/'] = [('/calendar?day=0', 'Calendar')]
    async def check(context, user):
        qa = engine(site, context, query_limit=3)
        await qa.run()
        assert qa.get_progress()['pages_tested'] == 4
        assert not qa.get_progress()['page_limit_reached']
        assert qa.records[site.base + '/calendar?day=3']['state'] == 'COVERAGE GAP'
        assert not qa.get_progress()['coverage_complete']
        assert site.counts['/calendar'] == 9
    asyncio.run(browser_case(site, check))


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
def test_full_site_popup_descendants_and_get_logout_are_blocked(site, method):
    site.links['/'] = []
    site.method = method
    site.script = f'<script>window.open("/popup");fetch("/write",{{method:"{method}"}}).catch(()=>{{}});fetch("/logout").catch(()=>{{}});</script>'
    async def check(context, user):
        qa = engine(site, context)
        results = await qa.run()
        assert site.counts['/popup'] > 0 and site.counts['/grandchild'] > 0
        assert site.mutations == 0 and site.counts['/logout'] == 0
        assert any(r['status'] == 'SAFETY ACTION' for r in results)
        assert not any(r['status'] == 'OBSERVATION' and 'ERR_FAILED' in r['actual_result'] for r in results)
    asyncio.run(browser_case(site, check))


def test_intentional_block_does_not_hide_independent_get_failure(site):
    site.links['/'] = []
    site.script = '<script>fetch("/write",{method:"POST"}).catch(()=>{});</script><script src="/blocked-get.js"></script>'
    async def check(context, user):
        async def fail_get(route): await route.abort()
        await context.route('**/blocked-get.js', fail_get)
        qa = engine(site, context)
        results = await qa.run()
        assert any(r['status'] == 'SAFETY ACTION' for r in results)
        assert any(r['status'] == 'OBSERVATION' and 'ERR_FAILED' in r['actual_result'] for r in results)
    asyncio.run(browser_case(site, check))


def test_auth_loss_stops_crawl_without_anonymous_fallback(site):
    site.redirects['/b'] = '/login'
    async def check(context, user):
        qa = engine(site, context)
        await qa.run()
        assert qa.terminal_status == 'FAILED'
        assert qa.get_progress()['auth_required'] == 1
        assert qa.get_progress()['pages_tested'] == 1
        assert site.counts['/c'] == 0
    asyncio.run(browser_case(site, check))


def seed(site):
    conn = get_db_connection()
    pid = conn.execute("INSERT INTO projects(name,target_url,environment,tester_name,notes) VALUES ('Full QA',?,'Test','QA','PRIVATE_LEARNING_SENTINEL')", (site.base + '/',)).lastrowid
    rid = conn.execute("INSERT INTO runs(project_id,status) VALUES (?, 'RUNNING')", (pid,)).lastrowid
    conn.commit(); conn.close()
    return pid, rid


def live_session(site, pid, headless=True):
    from app.qa.interactive import InteractiveSession
    from playwright.async_api import async_playwright
    class Session(InteractiveSession):
        async def _init_playwright(self):
            self.loop_errors = []
            self.loop.set_exception_handler(lambda loop, error: self.loop_errors.append(error))
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(chromium_sandbox=True, headless=headless,ignore_default_args=['--disable-popup-blocking'] if not headless else None)
            self.context = await self.browser.new_context(service_workers='block', **({'viewport':{'width':1111,'height':777}} if headless else {'no_viewport':True}))
            self.page = await self.context.new_page()
            await self.page.goto(site.base + '/login')
            await self.page.goto(site.base + '/visible')
        def stop(self, timeout=15):
            super().stop(timeout)
            assert not self.thread.is_alive(), 'Interactive loop survived bounded stop'
            assert not self.loop_errors, self.loop_errors
    session = Session(pid, site.base + '/')
    session.start()
    from app.qa.interactive import sessions
    sessions[str(pid)] = session
    return session


def worker_status(rid):
    conn = get_db_connection()
    row = conn.execute('SELECT status FROM runs WHERE id=?', (rid,)).fetchone()
    coverage = get_coverage(conn, rid, True)
    tests = [dict(t) for t in conn.execute('SELECT * FROM tests WHERE run_id=?', (rid,))]
    conn.close()
    return row[0], coverage, tests


def run_worker(site, pid, rid):
    worker = threading.Thread(target=api.run_qa_async_in_thread,
        args=(site.base + '/',pid,rid,'full',None,dict(delay=0,settle_timeout=600)))
    api.active_worker_threads.append(worker)
    worker.start()
    return worker


def test_worker_persists_coverage_progress_and_private_notes_stay_private(site):
    pid, rid = seed(site)
    session = live_session(site, pid)
    try:
        worker = run_worker(site, pid, rid)
        worker.join(12)
        assert not worker.is_alive()
        state, coverage, tests = worker_status(rid)
        assert state == 'COMPLETED' and coverage['pages_tested'] == 3
        assert len(tests) == 6 and coverage['coverage_complete']
        assert session.page.url == site.base + '/visible' and not session.page.is_closed()
        from server import app
        client = app.test_client()
        assert client.get(f'/api/runs/{rid}/status').json['coverage']['pages_tested'] == 3
        body = client.get(f'/run/{rid}').get_data(as_text=True)
        assert 'Full Site QA Coverage' in body and 'PRIVATE_LEARNING_SENTINEL' not in body
        html = client.get(f'/export/{rid}/html').get_data(as_text=True)
        assert 'Pages Tested: 3' in html and 'PRIVATE_LEARNING_SENTINEL' not in html
    finally: session.stop()


@pytest.mark.parametrize('loss', ['registry', 'context', 'browser'])
def test_session_or_context_loss_is_terminal_with_completed_evidence(site, loss):
    pid, rid = seed(site)
    session = live_session(site, pid)
    triggered = threading.Event()
    def lose(path):
        if path == '/b' and not triggered.is_set():
            triggered.set()
            if loss == 'registry':
                from app.qa.interactive import sessions
                sessions.pop(str(pid), None)
            else:
                coroutine = session.context.close() if loss == 'context' else session.browser.close()
                asyncio.run_coroutine_threadsafe(coroutine, session.loop)
    site.on_get = lose
    try:
        worker = run_worker(site, pid, rid)
        worker.join(12)
        assert not worker.is_alive() and triggered.is_set()
        state, coverage, tests = worker_status(rid)
        assert state in ('ABORTED','FAILED')
        assert coverage['pages_tested'] == 1 and coverage['coverage_gaps'] >= 1
        assert sum(t['status']=='PASS' for t in tests) == 2, [(t['status'],t['actual_result']) for t in tests]
        assert site.counts['/c'] == 0
    finally: session.stop()


@pytest.mark.parametrize('stop_kind', ['cancel', 'shutdown'])
def test_cancel_or_shutdown_during_navigation_preserves_evidence_and_terminal_state(site, stop_kind):
    pid, rid = seed(site)
    session = live_session(site, pid)
    site.delays['/b'] = .5
    entered = threading.Event()
    site.on_get = lambda path: entered.set() if path == '/b' else None
    try:
        worker = run_worker(site, pid, rid)
        assert entered.wait(10)
        from server import app
        progress = app.test_client().get(f'/api/runs/{rid}/status').json['coverage']
        assert progress['pages_tested'] == 1 and progress['phase'] == 'DISCOVERING'
        if stop_kind == 'cancel':
            assert app.test_client().post(f'/api/runs/{rid}/stop', headers={'X-QA-Request': '1'}).json['status'] == 'stopping'
        else: api.shutdown_qa_workers()
        worker.join(8)
        assert not worker.is_alive()
        state, coverage, tests = worker_status(rid)
        assert state == 'ABORTED' and coverage['pages_tested'] == 1
        assert sum(t['status']=='PASS' for t in tests) == 2, [(t['status'],t['actual_result']) for t in tests]
        assert coverage['coverage_gaps'] >= 1
        api._save_results([], rid, pid)
        assert worker_status(rid)[0] == 'ABORTED'
        if stop_kind == 'cancel':
            async def inspect():
                # The worker future becomes cancelled before page cleanup has finished.
                for _ in range(50):
                    if session.context.pages == [session.page]: break
                    await asyncio.sleep(.02)
                assert session.context.pages == [session.page]
                assert not session.page.is_closed() and session.page.url == site.base + '/visible'
            asyncio.run_coroutine_threadsafe(inspect(), session.loop).result(3)
    finally: session.stop()


@pytest.mark.parametrize('limit', [0, 1001, -1, True, '250'])
def test_full_site_invalid_limits_rejected_without_admission(site, monkeypatch, limit):
    pid, rid = seed(site)
    from types import SimpleNamespace
    monkeypatch.setattr(api, 'get_session', lambda _: SimpleNamespace(is_ready=lambda: True))
    from server import app
    response = app.test_client().post('/api/runs', json={'project_id':pid,'mode':'full','page_limit':limit}, headers={'X-QA-Request': '1'})
    assert response.status_code == 400 and not api.worker_records


def test_full_site_without_ready_session_fails_closed(site, monkeypatch):
    pid, rid = seed(site)
    monkeypatch.setattr(api, 'get_session', lambda _: None)
    from server import app
    response = app.test_client().post('/api/runs', json={'project_id':pid,'mode':'full'}, headers={'X-QA-Request': '1'})
    assert response.status_code == 400
    api.run_qa_async_in_thread(site.base + '/', pid, rid, 'full')
    assert worker_status(rid)[0] == 'ABORTED' and not site.counts['/']


def test_headful_background_targets_preserve_manual_window_bounds(site):
    async def check():
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(chromium_sandbox=True, headless=False)
            try:
                context = await browser.new_context(no_viewport=True, service_workers='block')
                user = await context.new_page()
                await user.goto(site.base + '/login')
                await user.goto(site.base + '/visible')
                await user.bring_to_front()
                control = await context.new_cdp_session(user)
                initial = await control.send('Browser.getWindowForTarget')
                dimensions = await user.evaluate('() => [innerWidth,innerHeight]')
                class InspectWindow(FullSiteQA):
                    async def _set_qa_viewport(self, page, vp):
                        await super()._set_qa_viewport(page, vp)
                        assert await page.evaluate('() => [innerWidth,innerHeight]') == [vp['width'],vp['height']]
                        assert await control.send('Browser.getWindowForTarget') == initial
                        assert await user.evaluate('() => [innerWidth,innerHeight]') == dimensions
                        assert await user.evaluate('document.visibilityState') == 'visible'
                qa = InspectWindow(site.base + '/',1,1,context=context,delay=0,settle_timeout=600)
                await qa.run()
                assert qa.get_progress()['pages_tested'] == 3
                assert await control.send('Browser.getWindowForTarget') == initial
                assert user.url == site.base + '/visible' and context.pages == [user]
                assert any(c['value']=='yes' for c in await context.cookies() if c['name']=='qa_login')
            finally: await browser.close()
    asyncio.run(check())


@pytest.mark.parametrize('limit,expected,capped', [(1,1,True),(3,3,False),(4,3,False)])
def test_page_limit_boundaries(site, limit, expected, capped):
    async def check(context, user):
        qa = engine(site,context,page_limit=limit)
        await qa.run()
        assert qa.get_progress()['pages_tested'] == expected
        assert qa.get_progress()['page_limit_reached'] is capped
        assert qa.get_progress()['coverage_complete'] is (not capped)
    asyncio.run(browser_case(site,check))


@pytest.mark.parametrize('cap', ['depth','discovery','navigation'])
def test_secondary_caps_report_incomplete_coverage(site, cap):
    options = {'depth':dict(depth_limit=0), 'discovery':dict(url_limit=1), 'navigation':dict(navigation_limit=1)}[cap]
    async def check(context,user):
        qa = engine(site,context,**options)
        await qa.run()
        assert qa.get_progress()['pages_tested'] == 1
        assert qa.get_progress()['coverage_gaps'] >= 1 and not qa.get_progress()['coverage_complete']
        assert site.counts['/b'] == 0
        if cap == 'discovery':
            from app.models.crawl import write_coverage, finish_coverage
            pid,rid = seed(site)
            conn = get_db_connection()
            write_coverage(conn,rid,qa.snapshot())
            finish_coverage(conn,rid,'ABORTED','Cancelled')
            assert get_coverage(conn,rid)['coverage_gaps'] == 1
            conn.rollback(); conn.close()
    asyncio.run(browser_case(site,check))


@pytest.mark.parametrize('destination', ['/logout','http://127.0.0.1:0/impostor','http://127.0.0.1.attacker.test/'])
def test_excluded_redirect_preserves_evidence_without_following(site, destination):
    site.redirects['/b'] = destination
    async def check(context,user):
        qa = engine(site,context)
        results = await qa.run()
        record = qa.records[site.base + '/b']
        assert record['state'] == 'COVERAGE GAP'
        final = site.base + destination if destination.startswith('/') else destination
        assert record['final_url'] == final
        assert any(r['status']=='REDIRECTED' and r['url']==site.base+'/b' and r['final_url']==final for r in results)
        assert site.counts['/logout'] == 0 and site.counts['/c'] == 0
    asyncio.run(browser_case(site,check))


@pytest.mark.parametrize('status', [401,403,429,500])
def test_http_failures_are_honest_and_auth_or_throttle_stops(site,status):
    site.statuses['/b'] = status
    async def check(context,user):
        qa = engine(site,context)
        await qa.run()
        progress = qa.get_progress()
        assert not progress['coverage_complete'] and progress['coverage_gaps'] >= 1
        if status in (401,403):
            assert progress['auth_required'] == 1 and qa.terminal_status == 'FAILED'
        else: assert progress['failed_to_load'] == 1
        if status != 500: assert site.counts['/c'] == 0
    asyncio.run(browser_case(site,check))


def test_cancel_during_mobile_preserves_desktop_checkpoint(site):
    pid,rid = seed(site)
    session = live_session(site,pid)
    entered = threading.Event()
    def delay_mobile(path):
        if path == '/' and site.counts['/'] == 3:
            entered.set()
            time.sleep(.5)
    site.on_get = delay_mobile
    try:
        worker = run_worker(site,pid,rid)
        assert entered.wait(10)
        from server import app
        assert app.test_client().post(f'/api/runs/{rid}/stop', headers={'X-QA-Request': '1'}).status_code == 200
        worker.join(8)
        assert not worker.is_alive()
        state,coverage,tests = worker_status(rid)
        assert state == 'ABORTED'
        assert coverage['desktop_pages_tested'] == 1 and coverage['mobile_pages_tested'] == 0
        assert coverage['pages_tested'] == 0
        completed = [t for t in tests if t['viewport'] != 'System' and t['status'] != 'DIAGNOSTIC']
        assert len(completed) == 1 and completed[0]['viewport']=='Desktop 1366x768'
        assert completed[0]['status'] == 'PASS'
        mobile = [t for t in tests if t['viewport']=='Mobile 390x844']
        assert len(mobile) == 1 and mobile[0]['status'] == 'DIAGNOSTIC'
        assert mobile[0]['stable_id'] is None and 'ERR_ABORTED' in mobile[0]['actual_result']
        assert any(t['viewport']=='System' and t['status']=='FAIL' for t in tests)
    finally: session.stop()


def test_coverage_privacy_and_escaping_in_every_official_export(site):
    import io, zipfile
    from app.models.crawl import write_coverage
    from app.paths import get_exports_dir
    from pathlib import Path
    pid,rid = seed(site)
    class Context: pass
    qa = engine(site,Context())
    raw = site.base + '/private?token=FAKE_PHASE2_SECRET'
    qa._discover(raw,1,label='Private')
    qa.records[raw]['reason'] = '<script>HOSTILE_PHASE2</script> token=FAKE_PHASE2_SECRET'
    conn = get_db_connection()
    write_coverage(conn,rid,qa.snapshot())
    conn.execute("UPDATE runs SET status='COMPLETED',total_tests=1 WHERE id=?",(rid,))
    conn.commit()
    stored = str(get_coverage(conn,rid,True)); conn.close()
    assert 'FAKE_PHASE2_SECRET' not in stored
    from server import app
    client = app.test_client()
    html = client.get(f'/export/{rid}/html').get_data(as_text=True)
    assert '<script>HOSTILE_PHASE2</script>' not in html and '&lt;script&gt;HOSTILE_PHASE2&lt;/script&gt;' in html
    documents = [html,client.get(f'/run/{rid}').get_data(as_text=True)]
    for format in ('markdown','txt','docx'):
        response = client.post(f'/api/export/{rid}/{format}', headers={'X-QA-Request': '1'})
        assert response.status_code == 200
        data = (Path(get_exports_dir())/response.json['filename']).read_bytes()
        if format == 'docx':
            with zipfile.ZipFile(io.BytesIO(data)) as archive: data = archive.read('word/document.xml')
        documents.append(data.decode())
    assert all('PRIVATE_LEARNING_SENTINEL' not in d and 'FAKE_PHASE2_SECRET' not in d for d in documents)
    assert all('coverage' in d.lower() for d in documents)


def test_full_site_api_admission_and_current_seed(site):
    pid,rid = seed(site)
    session = live_session(site,pid)
    try:
        from server import app
        client = app.test_client()
        response = client.post('/api/runs',json={'project_id':pid,'mode':'full','page_limit':1}, headers={'X-QA-Request': '1'})
        assert response.status_code == 200, response.json
        new_id = response.json['run_id']
        deadline = time.monotonic()+12
        while worker_status(new_id)[0]=='RUNNING' and time.monotonic()<deadline: time.sleep(.05)
        state,coverage,tests = worker_status(new_id)
        assert state=='COMPLETED' and coverage['pages_tested']==1 and coverage['page_limit_reached']
        assert any(p['requested_url']==site.base+'/visible' for p in coverage['pages'])
        assert session.page.url==site.base+'/visible'
    finally: session.stop()


@pytest.mark.parametrize('current', ['http://127.0.0.1:0/','https://127.0.0.1/','http://127.0.0.1.attacker.test/'])
def test_full_site_api_rejects_external_current_page(site,monkeypatch,current):
    from types import SimpleNamespace
    pid,rid = seed(site)
    monkeypatch.setattr(api,'get_session',lambda _:SimpleNamespace(is_ready=lambda:True,get_current_url=lambda:current))
    from server import app
    response = app.test_client().post('/api/runs',json={'project_id':pid,'mode':'full'}, headers={'X-QA-Request': '1'})
    assert response.status_code == 400 and not api.worker_records


def test_service_worker_registration_cannot_bypass_guard(site):
    site.links['/']=[]
    site.script='<script>navigator.serviceWorker.register("/sw.js").catch(()=>{});</script>'
    async def check(context,user):
        qa = engine(site,context)
        await qa.run()
        assert not context.service_workers and site.mutations==0 and site.counts['/sw.js']==0
        assert qa.get_progress()['pages_tested']==1
    asyncio.run(browser_case(site,check))
