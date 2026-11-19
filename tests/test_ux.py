"""Navigation contracts and actual browser workflow for the unified workspace."""
import json
from pathlib import Path
import threading

import pytest
from test_triage import client, seed, save, record
from test_report_metrics import Text
from manual_ux import seed_ux_fixture
from app.models.database import get_db_connection
from app.qa.reporting import load_report
from app.qa.report_renderers import render_text


@pytest.mark.parametrize('route', ['/', '/projects', '/run-test', '/findings', '/history', '/reports'])
def test_legacy_entry_points_remain_usable(client, route):
    fixture = seed_ux_fixture()
    response = client.get(route)
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert 'aria-label="Main navigation"' in text
    assert 'href="/projects"' in text
    assert f'/project/{fixture["project_id"]}' in text or route in ('/', '/findings', '/history', '/reports', '/run-test')



def test_latest_completed_result_excludes_newer_running_and_failed_runs(client):
    fixture = seed_ux_fixture()
    conn = get_db_connection()
    for status in ('RUNNING', 'FAILED'):
        conn.execute("INSERT INTO runs(project_id,status,start_time) VALUES (?,?,'2026-10-06 12:00:00')", (fixture['project_id'], status))
    conn.commit(); conn.close()
    page = client.get(f'/project/{fixture["project_id"]}').get_data(as_text=True)
    assert f'id="latest-results" href="/run/{fixture["limited_id"]}"' in page
    assert f'/run/{fixture["completed_id"]}' in page
    assert 'View latest results' in client.get('/projects').get_data(as_text=True)


@pytest.mark.parametrize('tab', ['summary', 'findings', 'reports', 'technical', 'unknown'])
def test_run_tabs_preserve_context_counts_and_private_clipboards(client, tab):
    fixture = seed_ux_fixture(); rid = fixture['limited_id']
    page = client.get(f'/run/{rid}?tab={tab}').get_data(as_text=True)
    assert 'Customer portal' in page and f'Run #{rid}' in page
    for destination in ('summary', 'findings', 'reports', 'technical'):
        assert f'/run/{rid}?tab={destination}' in page
    assert 'PRIVATE-TEST-12345' not in page
    conn = get_db_connection(); report = load_report(conn, rid); conn.close()
    assert report['total_checks'] == 114 and report['customer_finding_count'] == 3
    assert Text(page, 'run-report-text').text == render_text(report)
    assert Text(page, 'run-report-full-text').text == render_text(report, 'full')
    if tab in ('summary', 'unknown'):
        assert 'LIMITED COVERAGE' in page and 'Full Site QA Coverage' in page


@pytest.mark.parametrize('mode', ['standard', 'full'])
def test_preview_uses_selected_canonical_mode_without_export_files(client, mode):
    fixture = seed_ux_fixture()
    from app.paths import get_exports_dir
    before = set(Path(get_exports_dir()).iterdir())
    response = client.get(f'/run/{fixture["limited_id"]}/preview?mode={mode}')
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'PRIVATE-TEST-12345' not in page and '114' in page
    assert ('Every Technical Record' in page) == (mode == 'full')
    assert set(Path(get_exports_dir()).iterdir()) == before
    assert client.get(f'/run/{fixture["limited_id"]}/preview?mode=invalid').status_code == 400


@pytest.mark.parametrize('path', ['/project/999', '/project/999/dry-run', '/run/999/preview'])
def test_missing_workspace_has_safe_404(client, path):
    assert client.get(path).status_code == 404


def test_browser_normal_workflow_modes_edit_and_window_sizes(client, tmp_path):
    from playwright.sync_api import sync_playwright, expect
    from werkzeug.serving import make_server
    from server import app
    fixture = seed_ux_fixture(); rid = fixture['limited_id']; pid = fixture['project_id']
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width':1280, 'height':800}, permissions=['clipboard-read','clipboard-write'])
            context.add_init_script("""window.copiedReport=''; window.copiedHtml='';
                Object.defineProperty(navigator,'clipboard',{value:{
                    writeText:async text=>{window.copiedReport=text},
                    write:async items=>{window.copiedReport=await (await items[0].getType('text/plain')).text();
                        window.copiedHtml=await (await items[0].getType('text/html')).text();}
                }});""")
            page = context.new_page(); errors=[]
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(base)
            assert page.locator('nav[aria-label="Main navigation"] a').count() == 2
            page.get_by_role('link', name='View projects').click()
            page.get_by_role('link', name='Open workspace').click()
            assert page.url == f'{base}/project/{pid}'
            page.get_by_role('link', name='View latest results').click()
            assert page.url == f'{base}/run/{rid}'
            expect(page.locator('#executive-summary')).to_contain_text('LIMITED COVERAGE')
            for width in (1280,1000,800,600):
                page.set_viewport_size({'width':width,'height':800})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.set_viewport_size({'width':1280,'height':800})
            page.get_by_role('navigation', name='Run workspace').get_by_role('link', name='Technical', exact=True).click()
            expect(page.locator('#technical-count')).to_have_text('Showing 114 records')
            assert page.locator('.diagnostic-group').count() == 2
            assert page.locator('.diagnostic-group[open]').count() == 0
            for width in (1280,800,600):
                page.set_viewport_size({'width':width,'height':800})
                page.evaluate('window.scrollTo(0,500)')
                page.wait_for_timeout(100)
                bounds=page.locator('.run-nav').bounding_box()
                assert bounds and 0 <= bounds['y'] <= 1 and bounds['height'] <= 60
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.select_option('#technical-filter','DIAGNOSTIC')
            expect(page.locator('#technical-count')).to_have_text('Showing 102 records')
            page.locator('.diagnostic-group > summary').first.click()
            page.locator('.diagnostic-group .technical-record > summary').first.click()
            expect(page.locator('.diagnostic-group .technical-record').first).to_contain_text('Unverified fixture diagnostic record 1')
            page.select_option('#technical-filter','PASS')
            expect(page.locator('#technical-count')).to_have_text('Showing 2 records')
            assert page.locator('.diagnostic-group:visible').count() == 0
            page.select_option('#technical-filter','FAIL')
            expect(page.locator('#technical-empty')).to_be_visible()
            page.select_option('#technical-filter','all')
            expect(page.locator('#technical-count')).to_have_text('Showing 114 records')
            page.set_viewport_size({'width':1280,'height':800})
            page.get_by_role('navigation', name='Run workspace').get_by_role('link', name='Reports', exact=True).click()
            for mode in ('standard','full'):
                page.get_by_role('radio',name='Full Technical' if mode == 'full' else 'Standard',exact=True).check()
                expect(page.get_by_role('radio',name='Full Technical' if mode == 'full' else 'Standard',exact=True)).to_be_checked()
                page.get_by_role('button', name='Preview report', exact=True).click()
                expect(page.locator('iframe')).to_have_attribute('src',f'/run/{rid}/preview?mode={mode}')
                expect(page.locator('[data-preview-mode]')).to_have_text('Full Technical' if mode == 'full' else 'Standard')
                page.get_by_role('button', name='Copy report', exact=True).click()
                expect(page.locator('#toast')).to_contain_text('Report copied')
                expect(page.locator('[data-copy]')).to_be_enabled()
                conn=get_db_connection();report=load_report(conn,rid);conn.close()
                assert page.evaluate('window.copiedReport') == render_text(report,mode)
                copied_html = page.evaluate('window.copiedHtml')
                assert 'PRIVATE-TEST-12345' not in copied_html
                assert 'QA Test Report' in copied_html
                assert ('Every Technical Record' in copied_html) == (mode == 'full')
                expect(page.get_by_role('link',name='HTML',exact=True)).to_have_attribute('href',f'/export/{rid}/html?mode={mode}')
                for label,extension in [('Word','docx'),('TXT','txt'),('Markdown','markdown')]:
                    with page.expect_response(lambda r: f'/api/export/{rid}/{extension}?mode={mode}' in r.url) as export_response:
                        page.get_by_role('button',name=label,exact=True).click()
                    assert export_response.value.ok
                    data=export_response.value.json()
                    expect(page.locator('[data-export-result]')).to_contain_text(data['filename'])
                    expect(page.locator('#toast')).to_contain_text('Created ' + data['filename'])
                    from app.paths import get_exports_dir
                    exported=Path(get_exports_dir())/data['filename']
                    assert exported.exists()
                    if extension!='docx':
                        content=exported.read_text(encoding='utf-8')
                        assert 'PRIVATE-TEST-12345' not in content
                        assert ('Every Technical Record' in content)==(mode=='full')
            page.get_by_role('navigation',name='Run workspace').get_by_role('link',name='Findings',exact=True).click()
            page.get_by_role('link',name='Review Finding',exact=True).first.click()
            expect(page.locator('#automatic-triage')).to_contain_text('Suggestions assist review')
            assert page.locator('.assessment-warning').count() == 3
            assert page.locator('.assessment-success').count() == 1
            assert page.get_by_role('navigation',name='Run workspace').get_by_role('link',name='Summary',exact=True).count()==1
            page.get_by_role('button',name='Edit Finding',exact=True).click()
            expect(page.locator('#f_status')).to_be_focused()
            page.select_option('#f_sev','High'); page.select_option('#f_risk','Low')
            page.fill('#f_expl_sv_learn','PRIVATE-UX-EDIT')
            page.get_by_role('button',name='Save Changes',exact=True).click()
            expect(page.locator('#edit-form')).to_be_hidden()
            expect(page.locator('#toast')).to_have_text('Finding saved')
            finding_id=int(page.url.rsplit('/',1)[1])
            conn=get_db_connection();f=dict(conn.execute('SELECT * FROM findings WHERE id=?',(finding_id,)).fetchone());conn.close()
            assert f['severity']=='High' and f['risk']=='Low'
            assert f['severity_origin'] == 'manual' and f['risk_origin'] == 'manual'
            assert f['confidence_origin'] == 'baseline'
            assert 'PRIVATE-UX-EDIT' not in Text(page.content(),'report-text').text
            page.get_by_role('navigation',name='Run workspace').get_by_role('link',name='Reports',exact=True).click()
            expect(page.get_by_role('radio',name='Full Technical',exact=True)).to_be_checked()
            for width in (1000,800,600):
                page.set_viewport_size({'width':width,'height':800})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.get_by_role('link',name='＋ Start New QA Test',exact=True).click()
            expect(page.locator('#btnStart')).to_be_disabled()
            page.select_option('#test-mode','full');page.check('#auth_confirm')
            expect(page.locator('#btnStart')).to_be_disabled()  # no READY session
            # Verify the new setup sends all authenticated modes and page limits
            # to the unchanged API. The QA worker itself is covered by engine tests.
            captured = []
            page.route('**/interactive/state', lambda route: route.fulfill(json={
                'active':True, 'state':'READY', 'current_url':'http://127.0.0.1/fixture',
                'collected_urls':['http://127.0.0.1/fixture/one']}))
            def capture_start(route):
                captured.append(route.request.post_data_json)
                route.fulfill(json={'status':'started', 'run_id':rid})
            page.route('**/api/runs', capture_start)
            for mode in ('full', 'current', 'collected'):
                page.goto(f'{base}/project/{pid}/dry-run')
                expect(page.locator('#session-status')).to_contain_text('READY')
                page.select_option('#test-mode',mode); page.check('#auth_confirm')
                if mode == 'full':
                    page.fill('#fullPageLimit','0')
                    page.get_by_role('button',name='Start QA Test',exact=True).click()
                    expect(page.locator('#startError')).to_contain_text('whole number between 1 and 1000')
                    assert not captured
                    page.fill('#fullPageLimit','7')
                page.get_by_role('button',name='Start QA Test',exact=True).click()
                page.wait_for_url(f'{base}/run/{rid}')
                assert captured[-1]['project_id'] == pid and captured[-1]['mode'] == mode
                if mode == 'full': assert captured[-1]['page_limit'] == 7
                if mode == 'collected': assert captured[-1]['collected_urls'] == ['http://127.0.0.1/fixture/one']
            assert not errors
            browser.close()
    finally:
        server.shutdown();thread.join(timeout=5);server.server_close()
        assert not thread.is_alive()


def test_refinement_has_single_finding_list_and_compact_coverage(client):
    fixture=seed_ux_fixture(); rid=fixture['limited_id']
    for tab in ('summary','findings'):
        page=client.get(f'/run/{rid}?tab={tab}').get_data(as_text=True)
        assert page.count('data-finding-list') == 1
        conn=get_db_connection(); ids=[row[0] for row in conn.execute('SELECT id FROM findings WHERE run_id=?',(rid,))];conn.close()
        for fid in ids: assert page.count(f'data-finding-id="{fid}"') == 1
        assert 'PRIVATE-TEST-12345' not in page
    page=client.get(f'/run/{rid}').get_data(as_text=True)
    assert 'Show coverage details' in page and 'Pages attempted' in page and 'Fully evaluated' in page
    assert page.index('coverage-details') < page.index('Discovery safety-limited')
    home=client.get('/').get_data(as_text=True)
    assert 'Manage projects' not in home and 'All projects →' not in home


def test_design_system_controls_feedback_and_action_failure(client):
    from playwright.sync_api import sync_playwright, expect
    from werkzeug.serving import make_server
    from server import app
    fixture = seed_ux_fixture(); pid = fixture['project_id']; rid = fixture['limited_id']
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width':1280,'height':800}); errors=[]
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base)
            assert page.locator('.action-card').count() == 3
            expect(page.locator('.action-card-primary')).to_contain_text('Start a new test')
            expect(page.locator('.continue-section')).to_be_visible()
            page.get_by_role('link', name='View projects', exact=True).click()
            page.get_by_role('link', name='Open workspace', exact=True).click()
            assert 'btn-primary' in page.get_by_role('link',name='View results',exact=True).first.get_attribute('class')
            assert 'btn-report' in page.get_by_role('link',name='Reports',exact=True).first.get_attribute('class')
            page.get_by_role('button',name='Edit Project',exact=True).click()
            page.fill('#e_env','Design validation')
            page.get_by_role('button',name='Save Changes',exact=True).click()
            expect(page.locator('#toast')).to_have_text('Project saved')
            expect(page.locator('.workspace-meta')).to_contain_text('Design validation')
            conn=get_db_connection(); assert conn.execute('SELECT environment FROM projects WHERE id=?',(pid,)).fetchone()[0] == 'Design validation';conn.close()
            page.get_by_text('Delete this project',exact=True).click()
            page.get_by_role('button',name='Delete Project',exact=True).click()
            expect(page.get_by_role('button',name='Permanently delete project')).to_have_class('btn btn-danger')
            page.locator('#deleteProjectModal').get_by_role('button',name='Cancel',exact=True).click()
            page.get_by_role('link',name='View latest results',exact=True).click()
            assert page.locator('.metric-findings,.metric-review,.metric-priority,.metric-checks').count() == 4
            page.get_by_role('navigation',name='Run workspace').get_by_role('link',name='Findings',exact=True).click()
            page.get_by_role('link',name='Review Finding',exact=True).first.click()
            page.get_by_role('button',name='Edit Finding',exact=True).click()
            for field,value,tone in [('f_sev','High','danger'),('f_sev','Low','success'),('f_risk','Medium','warning'),('f_conf','High','success'),('f_conf','Low','warning'),('f_status','REJECTED','danger'),('f_status','OPEN','neutral')]:
                page.select_option('#'+field,value)
                expect(page.locator('#'+field)).to_have_attribute('data-tone',tone)
                expect(page.locator('#'+field)).to_have_value(value)
            page.locator('#f_status').focus()
            assert page.locator('#f_status').evaluate('(el)=>getComputedStyle(el).outlineStyle') != 'none'
            for width in (800,600):
                page.set_viewport_size({'width':width,'height':800})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.keyboard.press('Escape')
            expect(page.get_by_role('button',name='Edit Finding',exact=True)).to_be_focused()
            requests=[]
            def fail_review(route):
                requests.append(route.request.post_data_json)
                route.fulfill(status=503,json={'status':'error'})
            page.route('**/api/findings/*/review',fail_review)
            # Two immediate clicks exercise the pending guard before the response arrives.
            page.evaluate("""() => {const buttons=Array.from(document.querySelectorAll('button')); const approve=buttons.find(b=>b.textContent.trim()==='Approve finding'); approve.click(); window.reviewBusy=approve.disabled&&approve.getAttribute('aria-busy')==='true'; buttons.find(b=>b.textContent.trim()==='Reject finding').click();}""")
            expect(page.locator('#toast')).to_have_text('Failed to update status.')
            assert page.evaluate('window.reviewBusy')
            assert requests == [{'review_status':'APPROVED'}]
            expect(page.get_by_role('button',name='Approve finding',exact=True)).to_be_enabled()
            expect(page.get_by_role('button',name='Approve finding',exact=True)).not_to_have_attribute('aria-busy','true')
            expect(page.locator('#toast')).to_have_attribute('role','alert')
            page.unroute('**/api/findings/*/review',fail_review)
            page.get_by_role('button',name='Approve finding',exact=True).click()
            expect(page.locator('#toast')).to_have_text('Review approved')
            page.get_by_role('button',name='Reject finding',exact=True).click()
            expect(page.locator('#toast')).to_have_text('Review rejected')
            expect(page.locator('.badge').filter(has_text='Rejected').first).to_have_attribute('data-tone','danger')
            assert not errors
            browser.close()
    finally:
        server.shutdown();thread.join(timeout=5);server.server_close()
        assert not thread.is_alive()


def test_diagnostic_groups_keep_every_original_record_and_exact_counts(client):
    fixture=seed_ux_fixture();rid=fixture['limited_id']
    page=client.get(f'/run/{rid}?tab=technical').get_data(as_text=True)
    assert page.count('class="diagnostic-group"') == 2
    assert page.count('data-record-count="51"') == 2
    assert page.count('data-record-status="DIAGNOSTIC"') == 102
    conn=get_db_connection();ids=[r[0] for r in conn.execute('SELECT id FROM tests WHERE run_id=?',(rid,))];conn.close()
    assert len(ids)==114
    for identifier in ids: assert page.count(f'data-record-id="{identifier}"')==1
    assert 'PRIVATE-TEST-12345' not in page


@pytest.mark.parametrize('status', ['COMPLETED','ABORTED','FAILED'])
def test_refined_empty_outcomes_preserve_next_action_and_evidence(client,status):
    pid,rid=seed()
    save(pid,rid,record(fid=None,status='PASS',message='Local test completes'))
    conn=get_db_connection();conn.execute('UPDATE runs SET status=? WHERE id=?',(status,rid));conn.commit();conn.close()
    page=client.get(f'/run/{rid}').get_data(as_text=True)
    assert 'Next action:' in page and 'No customer findings recorded' in page
    assert 'No related themes' in page
    technical=client.get(f'/run/{rid}?tab=technical').get_data(as_text=True)
    assert 'No diagnostic records' in technical and 'data-record-status="PASS"' in technical
