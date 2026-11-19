"""Phase 3B deterministic offline reporting and privacy contracts."""
import copy
import io
import json
from pathlib import Path

from docx import Document
import pytest
from app.qa.reporting import build_report, load_report, presentation
from app.qa.report_renderers import render_text, render_markdown, render_html, render_docx
from app.models.database import get_db_connection
from test_triage import client, seed, save, record, project_rows
from test_report_metrics import Text

PRIVATE = 'PRIVATE-TEST-12345'


def facts(status='COMPLETED', customer=False, record_status='PASS', coverage=None):
    run = dict(id=1, project_id=1, status=status, start_time='2026-10-05 12:00:00', total_tests=999)
    project = dict(id=1, name='Reporting', target_url='http://fixture.invalid', environment='Local', tester_name='Tester')
    tests = [dict(id=1, run_id=1, status=record_status, viewport='Desktop', url='http://fixture.invalid/a',
                  final_url=None, stable_id='QA-1' if customer else None, timestamp=None, screenshot_path=None,
                  expected_result='No errors', actual_result='Console/JS errors detected: TypeError: Cannot read customerProfile.name')]
    findings = [dict(id=1, project_id=1, run_id=1, stable_id='QA-1', title='JavaScript exception',
                     area='JavaScript Execution', severity='Medium', risk='Medium', confidence='Medium',
                     severity_origin='baseline', risk_origin='baseline', confidence_origin='baseline',
                     status='OPEN', review_status='PENDING', learning_explanation_sv=PRIVATE,
                     description='Customer evidence', impact='Review', recommendation='Retest')] if customer else []
    return run, project, findings, tests, coverage


def coverage(**changes):
    from app.models.crawl import METRICS
    c = dict.fromkeys(METRICS, 0)
    c.update(mode='full', phase='COMPLETED', pages_discovered=4, pages_eligible=4, pages_attempted=4,
             pages_tested=4, pages_fully_evaluated=4, desktop_pages_tested=4, mobile_pages_tested=4,
             page_limit=10, page_limit_reached=False, discovery_limit_reached=False,
             discovery_safety_limited=False, coverage_complete=True, pages=[])
    c.update(changes)
    return c


@pytest.mark.parametrize('status,customer,record_status,c,outcome', [
    ('COMPLETED', False, 'PASS', None, 'COMPLETE'),
    ('COMPLETED', True, 'OBSERVATION', None, 'COMPLETE WITH FINDINGS'),
    ('COMPLETED', False, 'PASS', coverage(coverage_complete=False), 'LIMITED COVERAGE'),
    ('COMPLETED', True, 'FAIL', coverage(coverage_complete=False), 'LIMITED COVERAGE'),
    ('COMPLETED', False, 'SAFETY ACTION', None, 'LIMITED COVERAGE'),
    ('COMPLETED', False, 'DIAGNOSTIC', None, 'LIMITED COVERAGE'),
    ('COMPLETED', False, 'AUTH REQUIRED', None, 'LIMITED COVERAGE'),
    ('COMPLETED', False, 'REDIRECTED', None, 'LIMITED COVERAGE'),
    ('COMPLETED', False, 'PASS', coverage(page_limit_reached=True), 'LIMITED COVERAGE'),
    ('COMPLETED', False, 'PASS', coverage(discovery_safety_limited=True), 'LIMITED COVERAGE'),
    ('COMPLETED', False, 'PASS', coverage(discovery_limit_reached=True), 'LIMITED COVERAGE'),
    ('COMPLETED', False, 'PASS', coverage(auth_required=1), 'LIMITED COVERAGE'),
    ('COMPLETED', False, 'PASS', coverage(redirected=1), 'LIMITED COVERAGE'),
    ('COMPLETED', False, 'PASS', coverage(failed_to_load=1), 'LIMITED COVERAGE'),
    ('ABORTED', True, 'OBSERVATION', coverage(), 'INTERRUPTED'),
    ('RUNNING', False, 'PASS', None, 'INTERRUPTED'),
    ('FAILED', True, 'OBSERVATION', None, 'FAILED'),
])
def test_truthful_outcomes(status, customer, record_status, c, outcome):
    report = build_report(*facts(status, customer, record_status, c))
    assert report['outcome'] == outcome
    assert report['customer_finding_count'] == int(customer)
    assert report['total_checks'] == 1  # Stored run.total_tests is deliberately stale.
    assert report['run']['total_tests'] == 1
    if not customer and outcome == 'COMPLETE':
        assert report['summary'] == 'No customer findings were recorded within completed coverage.'
    if not customer and outcome == 'LIMITED COVERAGE':
        assert report['summary'] == 'No customer findings were confirmed, but evaluation was incomplete.'
    if outcome == 'FAILED':
        assert 'system execution' in report['summary']
    if outcome == 'INTERRUPTED':
        assert 'partial records are retained' in report['summary']


def test_empty_completed_run_has_no_fake_success():
    f = list(facts()); f[3] = []
    report = build_report(*f)
    assert report['outcome'] == 'LIMITED COVERAGE' and report['total_checks'] == 0


def mixed():
    f = list(facts(customer=True, record_status='OBSERVATION', coverage=coverage(
        coverage_complete=False, pages_fully_evaluated=3, desktop_limited=1, mobile_limited=1,
        safety_limited_pages=1, safety_limited_viewports=2, coverage_gaps=1,
        page_limit=4, page_limit_reached=True, discovery_safety_limited=True)))
    for ident, url in [(2, 'http://fixture.invalid/b'), (3, 'http://fixture.invalid/c')]:
        finding = dict(f[2][0], id=ident, stable_id=f'QA-{ident}')
        row = dict(f[3][0], id=ident, stable_id=f'QA-{ident}', url=url, viewport='Mobile')
        if ident == 3:
            row['actual_result'] = 'Console/JS errors detected: ReferenceError: accountSettings is not defined'
        f[2].append(finding); f[3].append(row)
    for status in ('PASS', 'SAFETY ACTION', 'LIMITED COVERAGE', 'DIAGNOSTIC', 'REDIRECTED', 'AUTH REQUIRED'):
        f[3].append(dict(f[3][0], id=len(f[3])+1, stable_id=None, status=status,
                         actual_result=f'RAW-TECH-{status}', viewport='Desktop', url='http://fixture.invalid/safety'))
    f[3].append(dict(f[3][0], id=10, status='FAIL', viewport='System', stable_id='QA-1', actual_result='Execution interruption'))
    return f


def test_mixed_customer_filter_grouping_and_original_evidence():
    source = mixed(); before = copy.deepcopy(source)
    report = build_report(*source)
    assert source == before
    assert report['customer_finding_count'] == 3 and report['total_checks'] == 10
    assert len(report['related_groups']) == 1
    assert report['related_groups'][0]['bug_ids'] == ['QA-1', 'QA-2']
    assert sum(len(f['evidence']) for f in report['findings']) == 3
    assert [f['triage']['evidence_count'] for f in report['findings']][:2] == [2, 2]
    text = render_text(report)
    assert 'Related by matching technical evidence' in text
    assert 'not proven duplicates' in text
    assert 'fingerprint' not in json.dumps(report)
    assert 'diagnostic records retained' in text
    assert 'diagnostic errors' not in text


def test_priority_order_and_manual_authority_are_reused():
    f = mixed()
    for finding, value in zip(f[2], ('Low', 'High', 'Medium')):
        for field in ('severity', 'risk', 'confidence'):
            finding[field] = value; finding[field + '_origin'] = 'manual'
    f[2][0]['review_status'] = 'APPROVED'
    report = build_report(*f)
    assert [x['triage']['priority'] for x in report['findings']] == ['High', 'Medium', 'Low']
    assert [x['id'] for x in report['findings']] == [2, 3, 1]
    assert report['findings'][0]['triage']['effective_severity'] == 'High'
    assert report['findings'][0]['triage']['suggested_severity'] == 'Medium'
    full_text = render_text(report, 'full')
    assert 'Effective Severity: High' in full_text and 'Suggested Severity: Medium' in full_text
    std_text = render_text(report, 'standard')
    assert 'High' in std_text


def test_priority_ties_follow_accepted_id_order():
    f = mixed(); f[2] = f[2][:2]; f[3] = f[3][:2]
    f[2].reverse()
    report = build_report(*f)
    assert [x['id'] for x in report['findings']] == [1, 2]


def test_review_queue_uses_existing_statuses_and_triage():
    f = mixed()
    f[2][0].update(review_status='APPROVED', status='RETEST')
    f[2][1].update(review_status='REJECTED', status='FIXED')
    report = build_report(*f)
    assert report['review_counts'] == {'APPROVED': 1, 'REJECTED': 1, 'Pending review': 1}
    assert {x['id'] for x in report['review_queue']} == {1, 3}
    assert next(x for x in report['findings'] if x['id'] == 2)['triage']['priority_score'] == 0


def text_for(format, report, mode):
    if format == 'txt': return render_text(report, mode)
    if format == 'md': return render_markdown(report, mode)
    if format == 'html': return Text(render_html(report, mode)).text
    doc = render_docx(report, mode)
    paras = [p.text for p in doc.paragraphs]
    tables = ['\n'.join(' | '.join(c.text for c in row.cells) for row in t.rows) for t in doc.tables]
    return '\n'.join(paras + tables)


@pytest.mark.parametrize('format', ['txt', 'md', 'html', 'docx'])
@pytest.mark.parametrize('mode', ['standard', 'full'])
def test_cross_format_canonical_values_privacy_and_modes(format, mode):
    report = build_report(*mixed())
    text = text_for(format, report, mode)
    assert 'Total Checks: 10' in text and 'Customer findings: 3' in text
    assert 'Outcome: LIMITED COVERAGE' in text
    assert PRIVATE not in text and 'learning_explanation_sv' not in json.dumps(report)
    assert ('RAW-TECH-DIAGNOSTIC' in text) == (mode == 'full')
    if mode == 'full':
        assert 'Pages attempted: 4' in text and 'Pages fully evaluated: 3' in text
        assert 'Desktop limited: 1' in text and 'Mobile limited: 1' in text
        assert 'Page limit: 4' in text and 'Page limit reached: yes' in text
        assert 'Discovery safety limited: yes' in text
        assert 'High priority: 2' in text and 'Medium priority: 1' in text and 'Low priority: 0' in text
        assert 'Human review / retest queue: 3' in text
        assert 'PASS: 1' in text and 'OBSERVATION: 3' in text and 'FAIL: 1' in text
        for row in report['tests']:
            assert f'Record ID: {row["id"]}' in text
            assert row['actual_result'] in text
    else:
        assert 'Pages Attempted' in text
        assert 'Pages Fully Evaluated' in text
        assert 'High priority: 2' in text
        assert 'Medium priority: 1' in text
        assert '3 finding(s) currently pending human review.' in text
    if format == 'md':
        assert '## Executive Summary' in text and '<p>' not in text


@pytest.mark.parametrize('format', ['txt', 'md', 'html', 'docx'])
@pytest.mark.parametrize('mode', ['standard', 'full'])
def test_no_findings_and_ordinary_qa_formats(format, mode):
    report = build_report(*facts())
    text = text_for(format, report, mode)
    assert 'No customer findings were recorded within completed coverage.' in text
    assert report['coverage'] is None
    assert 'Pages attempted:' not in text and 'Full Site QA Coverage' not in text


def test_docx_and_other_formats_section_order():
    report = build_report(*mixed())
    expected = ['Run / Project Details', 'Executive Summary', 'Coverage', 'Review Queue',
                'Prioritized Findings', 'Related Themes', 'Detailed Customer Findings', 'Technical Appendix — Technical Data']
    for format in ('txt', 'md', 'html', 'docx'):
        text = text_for(format, report, 'full')
        positions = [text.index(label) for label in expected]
        assert positions == sorted(positions)
    doc = render_docx(report, 'full')
    headings = [p.text for p in doc.paragraphs if p.style.name == 'Heading 1']
    assert headings == expected


@pytest.mark.parametrize('format', ['txt', 'md', 'html', 'docx'])
@pytest.mark.parametrize('mode', ['standard', 'full'])
def test_historical_and_editorial_secret_redaction(format, mode):
    f = mixed()
    secret = 'token=TOKEN-123\nAuthorization: Basic AUTH-123\nCookie: session=COOKIE-123\n{"api_key":"KEY-123","password":"PWD-123"}\nhttp://user:CREDS-123@fixture.invalid/a'
    f[1]['target_url'] = 'http://user:CREDS-123@fixture.invalid/?token=TOKEN-123'
    f[2][0]['description'] = secret
    f[3][0]['actual_result'] = secret
    report = build_report(*f)
    text = text_for(format, report, mode)
    for value in ('TOKEN-123', 'AUTH-123', 'COOKIE-123', 'KEY-123', 'PWD-123', 'CREDS-123', PRIVATE):
        assert value not in text and value not in json.dumps(report)


def seeded_report():
    pid, rid = seed()
    save(pid, rid, record(), record('QA-002', url='http://fixture.invalid/b', viewport='Mobile'),
         record(status='DIAGNOSTIC', fid=None, message='Raw diagnostics retained'),
         record(status='LIMITED COVERAGE', fid=None))
    conn = get_db_connection()
    conn.execute('UPDATE findings SET learning_explanation_sv=? WHERE run_id=?', (PRIVATE, rid))
    conn.commit(); conn.close()
    return pid, rid


def test_report_query_allowlist_and_one_triage_computation(client, monkeypatch):
    _, rid = seeded_report()
    import app.qa.reporting as reporting
    calls = []
    original = reporting.build_triage
    def count(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(reporting, 'build_triage', count)
    conn = get_db_connection(); queries = []; conn.set_trace_callback(queries.append)
    before = project_rows()
    report = load_report(conn, rid); conn.close()
    assert len(calls) == 1
    assert all('learning_explanation_sv' not in query and 'SELECT * FROM findings' not in query for query in queries)
    assert PRIVATE not in json.dumps(report)
    assert project_rows() == before
    from app.qa.triage import triage_run
    conn = get_db_connection(); accepted = triage_run(conn, rid); conn.close()
    assert [{k: v for k, v in t.items() if k != 'fingerprint'} for t in accepted] == report['triage']


def test_both_clipboard_paths_are_canonical_and_private_editor_stays_separate(client):
    _, rid = seeded_report()
    conn = get_db_connection(); report = load_report(conn, rid); conn.close()
    results = client.get(f'/run/{rid}').get_data(as_text=True)
    assert Text(results, 'run-report-text').text == render_text(report)
    assert Text(results, 'run-report-full-text').text == render_text(report, 'full')
    for f in report['findings']:
        page = client.get(f'/finding/{f["id"]}').get_data(as_text=True)
        assert PRIVATE in page
        assert Text(page, 'report-text').text == render_text(report)
        assert Text(page, 'report-full-text').text == render_text(report, 'full')
        for identifier in ('report-html', 'report-full-html'):
            text = Text(page, identifier).text
            assert PRIVATE not in text and 'Total Checks: 4' in text
    reports_page = client.get(f'/run/{rid}?tab=reports').get_data(as_text=True)
    assert 'value="standard"' in reports_page and 'value="full"' in reports_page
    assert 'All automated checks passed' not in results


@pytest.mark.parametrize('endpoint,method', [('/run/999', 'get'), ('/finding/999', 'get'),
    ('/export/999/html', 'get'), ('/api/export/999/txt', 'post'),
    ('/api/export/999/markdown', 'post'), ('/api/export/999/docx', 'post')])
def test_missing_runs_or_findings_return_404(client, endpoint, method):
    response = getattr(client, method)(endpoint)
    assert response.status_code == 404
    if endpoint.startswith('/api/export/'):
        assert response.json['status'] == 'error' and 'not found' in response.json['message']


@pytest.mark.parametrize('format', ['txt', 'markdown', 'docx', 'html'])
def test_export_mode_selection_validation_and_compatibility(client, format):
    _, rid = seeded_report()
    if format == 'html':
        url = f'/export/{rid}/html'; call = client.get
    else:
        url = f'/api/export/{rid}/{format}'; call = client.post
    assert call(url+'?mode=invalid').status_code == 400
    for mode in (None, 'standard', 'full'):
        response = call(url + ('?mode='+mode if mode else ''))
        assert response.status_code == 200
        if format == 'html': text = Text(response.get_data(as_text=True)).text
        else:
            from app.paths import get_exports_dir
            data = (Path(get_exports_dir()) / response.json['filename']).read_bytes()
            text = '\n'.join(p.text for p in Document(io.BytesIO(data)).paragraphs) if format == 'docx' else data.decode()
        assert 'Total Checks: 4' in text and 'Customer findings: 2' in text
        assert PRIVATE not in text
        assert ('Every Technical Record' in text) == (mode != 'standard')


def test_orphaned_project_report_returns_404(client):
    pid, rid = seeded_report()
    conn = get_db_connection(); conn.execute('DELETE FROM projects WHERE id=?', (pid,)); conn.commit(); conn.close()
    assert client.get(f'/run/{rid}').status_code == 404


def test_export_save_error_is_actionable_without_mutation(client, monkeypatch):
    _, rid = seeded_report(); before = project_rows()
    def blocked(*args, **kwargs): raise PermissionError('Cannot save')
    monkeypatch.setattr(Path, 'write_text', blocked)
    response = client.post(f'/api/export/{rid}/txt')
    assert response.status_code == 500 and response.json['status'] == 'error'
    assert 'could not be saved' in response.json['message']
    assert project_rows() == before


def test_private_editor_write_does_not_change_reporting_facts(client):
    _, rid = seeded_report()
    conn = get_db_connection(); before = load_report(conn, rid)
    fid = before['findings'][0]['id']
    conn.execute('UPDATE findings SET learning_explanation_sv=? WHERE id=?', ('Private changed ' + PRIVATE, fid))
    conn.commit(); after = load_report(conn, rid); conn.close()
    assert before == after


def test_actual_private_editor_api_write_stays_separate(client):
    _, rid = seeded_report()
    conn = get_db_connection(); report = load_report(conn, rid); conn.close()
    fid = report['findings'][0]['id']
    response = client.post(f'/api/findings/{fid}/edit', json=dict(
        severity='Low', risk='High', confidence='Low', status='RETEST', impact='Human impact',
        recommendation='Retest', plain_language_explanation='Human explanation',
        learning_explanation_sv=PRIVATE, how_to_explain_sv='Private explanation'))
    assert response.status_code == 200
    conn = get_db_connection(); report = load_report(conn, rid); conn.close()
    assert PRIVATE not in json.dumps(report)
    assert next(f for f in report['findings'] if f['id'] == fid)['triage']['effective_risk'] == 'High'
    page = client.get(f'/finding/{fid}').get_data(as_text=True)
    assert PRIVATE in page and PRIVATE not in Text(page, 'report-full-text').text


def test_optional_missing_and_corrupt_images_keep_text_report(client):
    from app.paths import get_screenshots_dir
    f = mixed()
    path = Path(get_screenshots_dir()) / 'corrupt.png'
    path.write_bytes(b'not an image')
    f[3][0]['screenshot_path'] = str(path)
    f[3][1]['screenshot_path'] = 'missing.png'
    report = build_report(*f)
    text = text_for('docx', report, 'full')
    assert 'Screenshot image could not be embedded' in text
    assert 'Screenshot not found: missing.png' in text
    assert 'Total Checks: 10' in text


def test_manual_helper_fixture_is_disposable_and_truthful(client):
    from manual_reporting import seed_reporting_fixture
    rid = seed_reporting_fixture()
    conn = get_db_connection(); report = load_report(conn, rid); conn.close()
    assert report['total_checks'] == 114 and report['customer_finding_count'] == 3
    assert report['stats']['DIAGNOSTIC'] == 102
    assert report['outcome'] == 'LIMITED COVERAGE'
    assert report['coverage']['pages_attempted'] == 5 and report['coverage']['pages_fully_evaluated'] == 4
    assert len(report['related_groups']) == 1
    assert len(report['related_groups'][0]['finding_ids']) == 2
    assert sum(len(f['evidence']) for f in report['findings']) == 6
    assert len(render_text(report, 'full')) > 5 * len(render_text(report))
    assert PRIVATE not in json.dumps(report)


def test_invalid_control_characters_do_not_break_docx_export():
    f = mixed(); f[3][0]['actual_result'] += '\x00\x08'
    doc = render_docx(build_report(*f), 'full')
    out = io.BytesIO(); doc.save(out)
    assert out.getvalue().startswith(b'PK')


def test_unlinked_saved_records_are_archive_only_and_escaped():
    f = list(facts(customer=True, record_status='DIAGNOSTIC'))
    f[2][0]['description'] = '<script>ARCHIVE_ONLY</script>'
    report = build_report(*f)
    assert report['customer_finding_count'] == 0 and report['triage'] == [] and report['review_queue'] == []
    assert len(report['unlinked_findings']) == 1 and report['total_checks'] == 1
    assert PRIVATE not in json.dumps(report)
    assert 'ARCHIVE_ONLY' not in render_text(report)
    assert 'ARCHIVE_ONLY' in render_text(report, 'full')
    html = render_html(report, 'full')
    assert '&lt;script&gt;ARCHIVE_ONLY&lt;/script&gt;' in html
    assert '<script>ARCHIVE_ONLY</script>' not in html
    assert 'not confirmed customer findings' in html


@pytest.mark.parametrize('state', ['FAILED TO LOAD', 'LIMITED COVERAGE', 'COVERAGE GAP', 'FAILED', 'LIMITED', 'SKIPPED'])
def test_standard_coverage_reports_incomplete_pages_with_empty_reason(state):
    """Incomplete crawl pages with empty reasons must never fall through to 'All ... evaluated without errors'."""
    page = {
        'requested_url': 'http://fixture.invalid/broken',
        'final_url': 'http://fixture.invalid/broken',
        'state': state,
        'reason': '',  # Empty reason!
        'desktop_limited': 0,
        'mobile_limited': 0,
        'discovery_limited': 0,
    }
    cov = coverage(
        coverage_complete=False,
        pages_attempted=1,
        pages_fully_evaluated=0,
        pages=[page]
    )
    f = list(facts(coverage=cov))
    report = build_report(*f)

    # In Standard mode:
    std_text = render_text(report, 'standard')
    assert 'All 1 discovered pages were safely reachable and evaluated.' not in std_text
    assert f'{state}: http://fixture.invalid/broken -> http://fixture.invalid/broken' in std_text

    # In HTML:
    html_text = render_html(report, 'standard')
    assert 'evaluated without errors' not in html_text
    assert state in html_text


def test_standard_coverage_reports_desktop_mobile_limited_pages_with_empty_reason():
    """Pages with desktop_limited or mobile_limited must never fall through to complete evaluation."""
    page = {
        'requested_url': 'http://fixture.invalid/partial',
        'final_url': 'http://fixture.invalid/partial',
        'state': 'LIMITED COVERAGE',
        'reason': '',
        'desktop_limited': 1,
        'mobile_limited': 0,
        'discovery_limited': 0,
    }
    cov = coverage(
        coverage_complete=False,
        pages_attempted=1,
        pages_fully_evaluated=0,
        pages=[page]
    )
    f = list(facts(coverage=cov))
    report = build_report(*f)

    std_text = render_text(report, 'standard')
    assert 'All 1 discovered pages were safely reachable and evaluated.' not in std_text
    assert 'LIMITED COVERAGE: http://fixture.invalid/partial -> http://fixture.invalid/partial' in std_text


def test_standard_coverage_reports_complete_when_all_evaluated_cleanly():
    """When all pages are TESTED without limits/gaps, Standard reports success using actual persisted model."""
    page = {
        'requested_url': 'http://fixture.invalid/clean',
        'final_url': 'http://fixture.invalid/clean',
        'state': 'TESTED',
        'reason': '',
        'desktop_tested': True,
        'mobile_tested': True,
        'desktop_limited': 0,
        'mobile_limited': 0,
        'discovery_limited': 0,
    }
    cov = coverage(
        coverage_complete=True,
        pages_attempted=1,
        pages_fully_evaluated=1,
        coverage_gaps=0,
        page_limit_reached=False,
        discovery_safety_limited=False,
        pages=[page]
    )
    f = list(facts(coverage=cov))
    report = build_report(*f)

    std_text = render_text(report, 'standard')
    assert 'All 1 discovered pages were safely reachable and evaluated.' in std_text
    assert 'without errors' not in std_text


def test_standard_coverage_incomplete_viewport_reports_problem():
    """Partial viewport coverage (e.g. desktop tested but mobile not) reports coverage limitation."""
    page = {
        'requested_url': 'http://fixture.invalid/partial-viewport',
        'final_url': 'http://fixture.invalid/partial-viewport',
        'state': 'TESTED',
        'reason': '',
        'desktop_tested': True,
        'mobile_tested': False,
        'desktop_limited': 0,
        'mobile_limited': 0,
        'discovery_limited': 0,
    }
    cov = coverage(
        coverage_complete=False,
        pages_attempted=1,
        pages_fully_evaluated=0,
        pages=[page]
    )
    f = list(facts(coverage=cov))
    report = build_report(*f)

    std_text = render_text(report, 'standard')
    assert 'All 1 discovered pages were safely reachable and evaluated.' not in std_text
    assert 'Incomplete viewport coverage' in std_text


def test_fully_evaluated_page_with_findings_does_not_imply_defect_free():
    """A fully evaluated TESTED page with findings records evaluation while reporting defects."""
    page = {
        'requested_url': 'http://fixture.invalid/page-with-bug',
        'final_url': 'http://fixture.invalid/page-with-bug',
        'state': 'TESTED',
        'reason': '',
        'desktop_tested': True,
        'mobile_tested': True,
        'desktop_limited': 0,
        'mobile_limited': 0,
        'discovery_limited': 0,
    }
    cov = coverage(
        coverage_complete=True,
        pages_attempted=1,
        pages_fully_evaluated=1,
        coverage_gaps=0,
        page_limit_reached=False,
        discovery_safety_limited=False,
        pages=[page]
    )
    f = list(facts(customer=True, record_status='FAIL', coverage=cov))
    report = build_report(*f)

    std_text = render_text(report, 'standard')
    assert 'All 1 discovered pages were safely reachable and evaluated.' in std_text
    assert 'Customer findings: 1' in std_text
    assert 'Findings Overview' in std_text
    assert 'without errors' not in std_text
    assert 'Evaluation coverage describes reached pages and viewports' in std_text
