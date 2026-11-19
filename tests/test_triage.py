"""Phase 3A offline rules, privacy, manual authority and compatibility."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys

from docx import Document
import pytest

from app.models.database import get_db_connection, init_db
from app.qa.triage import canonical_url, normalize_message, triage_run
from app.routes import api
from test_report_metrics import Text


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.paths as paths
    monkeypatch.setenv('QA_TESTER_DB_PATH', str(tmp_path / 'triage.sqlite'))
    monkeypatch.setattr(paths, 'get_writable_dir', lambda: str(tmp_path))
    paths.ensure_dirs()
    init_db()
    from server import app
    monkeypatch.setitem(app.config, 'TESTING', True)
    c = app.test_client()
    c.environ_base['HTTP_X_QA_REQUEST'] = '1'
    return c


def seed():
    conn = get_db_connection()
    pid = conn.execute("INSERT INTO projects(name,target_url,environment,tester_name) VALUES ('Triage','http://fixture.invalid','Local','Tester')").lastrowid
    rid = conn.execute("INSERT INTO runs(project_id,status) VALUES (?,'RUNNING')", (pid,)).lastrowid
    conn.commit()
    conn.close()
    return pid, rid


def record(fid='QA-001', message='TypeError: Cannot read profile.name of undefined', viewport='Desktop 1366x768', url='http://fixture.invalid/a', status='OBSERVATION', area='JavaScript Execution', title='Uncaught JavaScript exception'):
    return dict(finding_id=fid, actual_result='Console/JS errors detected: ' + message,
                viewport=viewport, url=url, status=status, area=area, title=title,
                expected_result='No errors', screenshot=None)


def save(pid, rid, *records, **kwargs):
    api._save_results(list(records), rid, pid, **kwargs)


def project_rows():
    conn = get_db_connection()
    snapshot = {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')]
                for table in ('projects', 'runs', 'findings', 'tests')}
    conn.close()
    return snapshot


def triage(rid):
    conn = get_db_connection()
    try:
        return triage_run(conn, rid)
    finally:
        conn.close()


def test_same_defect_two_viewports_keeps_one_finding_and_both_evidence(client):
    pid, rid = seed()
    save(pid, rid, record(), record(viewport='Mobile 390x844'))
    before = project_rows()
    result = triage(rid)
    assert len(before['findings']) == len(result) == 1
    assert result[0]['evidence_count'] == 2 and len(result[0]['evidence_ids']) == 2
    assert result[0]['viewports'] == ['Desktop', 'Mobile']
    assert result[0]['suggested_confidence'] == 'High'
    assert result[0]['suggested_severity'] == result[0]['suggested_risk'] == 'Medium'
    assert project_rows() == before


def test_matching_specific_signature_across_pages_forms_related_group(client):
    pid, rid = seed()
    save(pid, rid, record(message='TypeError: Cannot read profile.name at app.js:10:2'),
         record('QA-002', message='TypeError: Cannot read profile.name at app.js:20:7', url='http://fixture.invalid/b', viewport='Mobile 390x844'))
    before = project_rows()
    result = triage(rid)
    assert len(result) == 2  # Related records, not destructive database merges.
    assert len({item['fingerprint'] for item in result}) == 1
    assert all(item['finding_ids'] == [row['id'] for row in before['findings']] for item in result)
    assert all(item['evidence_count'] == 2 and item['page_count'] == 2 for item in result)
    assert project_rows() == before


@pytest.mark.parametrize('left,right', [
    ('TypeError: Cannot read profile.name', 'ReferenceError: account is not defined'),
    ('Error: failed to load profile at profile.js:1:2', 'Error: failed to load settings at settings.js:1:2'),
    ('error', 'error'),
    ('TypeError: Failed to fetch', 'TypeError: Failed to fetch'),
    ('TypeError: Failed to fetch at app.js:1:2', 'TypeError: Failed to fetch at app.js:9:3'),
    ('error request_id=12345678-1234-1234-1234-123456789abc', 'error request_id=87654321-1234-1234-1234-123456789abc'),
    ('error timestamp=2026-10-05T12:30:00Z', 'error timestamp=2026-10-05T15:30:00Z'),
    ('TypeError: Input field id 101 missing', 'TypeError: Input field id 102 missing'),
])
def test_different_or_generic_errors_are_not_collapsed(client, left, right):
    pid, rid = seed()
    save(pid, rid, record(message=left), record('QA-002', message=right))
    result = triage(rid)
    assert len({item['fingerprint'] for item in result}) == 2
    assert all(len(item['finding_ids']) == 1 for item in result)


def test_repeated_evidence_preserves_count_and_is_order_independent(client):
    from app.qa.triage import build_triage
    pid, rid = seed()
    save(pid, rid, record(), record(), record(viewport='Mobile'))
    rows = project_rows()
    before = copy.deepcopy(rows)
    forward = build_triage(rows['findings'], rows['tests'], 'COMPLETED')
    backward = build_triage(rows['findings'][::-1], rows['tests'][::-1], 'COMPLETED')
    assert forward == backward and rows == before
    assert forward[0]['evidence_count'] == len(rows['tests']) == 3


def test_same_signature_never_groups_across_projects_or_runs(client):
    from app.qa.triage import build_triage
    pid, rid = seed()
    other_pid, other_rid = seed()
    save(pid, rid, record())
    save(other_pid, other_rid, record())
    conn = get_db_connection()
    repeat = conn.execute("INSERT INTO runs(project_id,status) VALUES (?,'RUNNING')", (pid,)).lastrowid
    conn.commit()
    conn.close()
    save(pid, repeat, record())
    rows = project_rows()
    result = build_triage(rows['findings'], rows['tests'], 'COMPLETED')
    assert len(result) == 3 and all(len(item['finding_ids']) == 1 for item in result)
    assert triage(rid)[0]['fingerprint'] != triage(other_rid)[0]['fingerprint']


@pytest.mark.parametrize('status', ['SAFETY ACTION', 'DIAGNOSTIC', 'LIMITED COVERAGE', 'REDIRECTED', 'AUTH REQUIRED', 'PASS', 'COVERAGE GAP'])
def test_technical_records_never_create_or_inflate_triage(client, status):
    pid, rid = seed()
    save(pid, rid, record())
    before = triage(rid)
    save(pid, rid, record('QA-TECHNICAL', status=status), record(status=status))
    after = triage(rid)
    assert before == after
    assert len(project_rows()['findings']) == 1


def test_linked_system_marker_or_technical_only_legacy_finding_is_not_triaged(client):
    pid, rid = seed()
    save(pid, rid, record(viewport='System', status='FAIL'), record('QA-002', status='DIAGNOSTIC'))
    assert triage(rid) == []
    conn = get_db_connection()
    conn.execute("INSERT INTO findings(project_id,run_id,stable_id,title) VALUES (?,?,'QA-002','Historical diagnostic')", (pid, rid))
    conn.commit()
    conn.close()
    assert triage(rid) == []


def test_manual_values_and_status_survive_recalculation_new_evidence_and_reload(client):
    pid, rid = seed()
    save(pid, rid, record())
    fid = triage(rid)[0]['finding_id']
    assert project_rows()['findings'][0]['severity_origin'] == 'baseline'
    manual = dict(severity='Low', risk='High', confidence='Low', status='RETEST',
                  impact='Manual impact', recommendation='Manual recommendation',
                  plain_language_explanation='Manual explanation', learning_explanation_sv='PRIVATE-TEST-12345',
                  how_to_explain_sv='Manual spoken explanation')
    assert client.post(f'/api/findings/{fid}/edit', json=manual).status_code == 200
    assert client.post(f'/api/findings/{fid}/review', json={'review_status': 'APPROVED'}).status_code == 200
    save(pid, rid, record(viewport='Mobile'))
    before = project_rows()
    response = client.post(f'/api/runs/{rid}/triage')
    assert response.status_code == 200
    item = response.json['findings'][0]
    assert item['suggested_confidence'] == 'High'
    assert (item['effective_severity'], item['effective_risk'], item['effective_confidence']) == ('Low', 'High', 'Low')
    assert 'manual' in item['reasons']['priority']
    assert client.get(f'/finding/{fid}').status_code == 200
    assert project_rows() == before
    finding = before['findings'][0]
    assert all(finding[key] == value for key, value in manual.items())
    assert finding['review_status'] == 'APPROVED'
    assert all(finding[field + '_origin'] == 'manual' for field in ('severity', 'risk', 'confidence'))


def test_manual_unreviewed_is_not_replaced_by_suggestions(client):
    pid, rid = seed()
    save(pid, rid, record())
    fid = triage(rid)[0]['finding_id']
    client.post(f'/api/findings/{fid}/edit', json=dict(severity='Unreviewed', risk='Unreviewed', confidence='Unreviewed', status='OPEN'))
    client.post(f'/api/findings/{fid}/review', json={'review_status': 'APPROVED'})
    item = triage(rid)[0]
    assert item['effective_severity'] == item['effective_risk'] == item['effective_confidence'] == 'Unreviewed'
    assert item['suggested_severity'] == 'Medium'
    assert item['needs_human_review'] and 'Unreviewed' in item['review_reason']


@pytest.mark.parametrize('status', ['COMPLETED', 'ABORTED', 'FAILED', 'RUNNING'])
def test_zero_findings_and_partial_runs_have_no_fake_or_partial_triage(client, status):
    pid, rid = seed()
    conn = get_db_connection()
    conn.execute('UPDATE runs SET status=? WHERE id=?', (status, rid))
    conn.execute("INSERT INTO tests(run_id,url,status,viewport,actual_result) VALUES (?,'','FAIL','System','Interrupted')", (rid,))
    conn.commit()
    conn.close()
    before = project_rows()
    assert client.post(f'/api/runs/{rid}/triage').json['findings'] == []
    assert client.get(f'/run/{rid}').status_code == 200
    assert project_rows() == before


def test_cancelled_run_preserves_completed_genuine_evidence(client):
    pid, rid = seed()
    save(pid, rid, record(), finish=False)
    api._mark_terminal(rid, 'Local fixture cancelled')
    before = project_rows()
    result = triage(rid)
    assert len(result) == 1 and result[0]['evidence_count'] == 1
    assert result[0]['needs_human_review']
    assert project_rows() == before


@pytest.mark.parametrize('status,review', [('FIXED', 'APPROVED'), ('OPEN', 'REJECTED')])
def test_fixed_or_rejected_findings_have_zero_active_priority(client, status, review):
    pid, rid = seed()
    save(pid, rid, record())
    conn = get_db_connection()
    conn.execute('UPDATE findings SET status=?,review_status=? WHERE run_id=?', (status, review, rid))
    conn.commit()
    conn.close()
    assert triage(rid)[0]['priority_score'] == 0


@pytest.mark.parametrize('review,label', [('PENDING','required'), ('APPROVED','approved by tester'), ('REJECTED','rejected by tester')])
def test_review_decision_consistent_in_ui_and_exports(client, review, label):
    from app.paths import get_exports_dir
    pid, rid = seed()
    save(pid, rid, record(), record(viewport='Mobile'))
    fid = triage(rid)[0]['finding_id']
    conn = get_db_connection()
    conn.execute("UPDATE runs SET status='COMPLETED' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    assert client.post(f'/api/findings/{fid}/review', json={'review_status':review}).status_code == 200
    page = client.get(f'/finding/{fid}').get_data(as_text=True)
    panel = Text(page, 'automatic-triage').text
    assert 'Human review '+label in panel
    outputs = [panel, json.dumps(client.post(f'/api/runs/{rid}/triage').json),
               Text(page,'report-text').text, Text(page,'report-html').text,
               Text(client.get(f'/export/{rid}/html').get_data(as_text=True)).text]
    for format in ('txt','markdown','docx'):
        response = client.post(f'/api/export/{rid}/{format}')
        assert response.status_code == 200
        data = (Path(get_exports_dir()) / response.json['filename']).read_bytes()
        outputs.append('\n'.join(p.text for p in Document(io.BytesIO(data)).paragraphs)
                       if format == 'docx' else data.decode())
    if review == 'REJECTED':
        assert all('approved by tester' not in text and 'tester has approved' not in text for text in outputs)
        assert all('reject' in text.lower() for text in outputs)
    assert project_rows()['findings'][0]['review_status'] == review


def test_priority_formula_and_review_order_use_multiple_factors(client):
    pid, rid = seed()
    http = record('QA-HTTP', status='FAIL', area='Page Load / Server Response', title='Page returns HTTP 500')
    http['actual_result'] = 'Server returned HTTP 500'
    save(pid, rid, record(), http, dict(http, viewport='Mobile'))
    result = triage(rid)
    assert result[0]['suggested_severity'] == result[0]['suggested_risk'] == 'High'
    # 3*3 + 2*3 + 3 + (two viewports=1 + repetition=1) + pending=1.
    assert result[0]['priority_score'] == 21 and result[0]['priority'] == 'High'
    assert result[1]['priority_score'] == 11 and result[1]['priority'] == 'Medium'


def test_frequency_and_url_words_do_not_escalate_cosmetic_or_console_severity(client):
    pid, rid = seed()
    cosmetic = record(area='Responsive Layout', title='Horizontal overflow detected', viewport='Mobile', status='FAIL', url='http://fixture.invalid/payment/admin')
    cosmetic['actual_result'] = 'Horizontal overflow detected on mobile'
    save(pid, rid, *[dict(cosmetic) for _ in range(10)], record('QA-CONSOLE', message='500 records loaded successfully'))
    result = triage(rid)
    assert all(item['suggested_severity'] == 'Low' and item['suggested_risk'] == 'Low' for item in result)
    assert all(item['priority'] != 'High' for item in result)


@pytest.mark.parametrize('same_resource', [True, False])
def test_failed_resources_group_only_matching_technical_request_signature(client, same_resource):
    pid, rid = seed()
    a = record(area='Network', title='Failed GET resource')
    a['actual_result'] = 'Failed resource: script failed: http://fixture.invalid/assets/profile.js (500)'
    b = dict(a, finding_id='QA-002', url='http://fixture.invalid/b')
    if not same_resource:
        b['actual_result'] = b['actual_result'].replace('profile.js', 'settings.js')
    save(pid, rid, a, b)
    assert len({item['fingerprint'] for item in triage(rid)}) == (1 if same_resource else 2)


def test_http_status_alone_does_not_group_unrelated_pages(client):
    pid, rid = seed()
    a = record(area='Page Load / Server Response', title='Page returns HTTP 500', status='FAIL')
    a['actual_result'] = 'Server returned HTTP 500'
    save(pid, rid, a, dict(a, finding_id='QA-002', url='http://fixture.invalid/b'))
    assert len({item['fingerprint'] for item in triage(rid)}) == 2


def test_unsupported_signal_remains_unreviewed_and_requires_human_review(client):
    pid, rid = seed()
    save(pid, rid, record(area='Unknown', title='Unknown finding', message='ambiguous'))
    item = triage(rid)[0]
    assert item['suggested_severity'] == item['suggested_risk'] == 'Unreviewed'
    assert item['suggested_confidence'] == 'Low' and item['needs_human_review']


def test_normalization_is_bounded_and_preserves_target_identity():
    assert normalize_message('TypeError: bad profile at app.js:10:2  2026-10-05T12:30:00Z') == normalize_message('TypeError: bad profile at app.js:90:3\n2026-10-05T15:30:00Z')
    assert normalize_message('request_id=12345678-1234-1234-1234-123456789abc input-101') == 'request_id=<generated-id> input-101'
    assert normalize_message('input-101') != normalize_message('input-102')
    assert normalize_message('Mobile menu failed') == 'Mobile menu failed'
    assert canonical_url('HTTP://user:password@Fixture.invalid:80/a?item=1#top') == 'http://fixture.invalid/a?item=1'
    assert canonical_url('http://fixture.invalid/a?item=1') != canonical_url('http://fixture.invalid/a?item=2')


def test_fingerprint_is_stable_across_python_hash_seeds(client):
    script = "from app.qa.triage import fingerprint; f={'id':1,'project_id':1,'title':'Uncaught JavaScript exception','area':'JavaScript Execution'}; e=[{'actual_result':'Console/JS errors detected: TypeError: Cannot read profile.name','url':'http://fixture.invalid/a'}]; print(fingerprint(f,e)[0])"
    values = []
    for hash_seed in ('1', '999'):
        env = {key: os.environ[key] for key in ('SYSTEMROOT', 'PATH', 'TEMP', 'TMP') if key in os.environ}
        env['PYTHONHASHSEED'] = hash_seed
        response = subprocess.run([sys.executable, '-c', script], env=env, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        assert response.returncode == 0, response.stderr
        values.append(response.stdout.strip())
    assert values[0] == values[1] and len(values[0]) == 64


def test_redacted_identity_does_not_create_false_duplicate_or_leak_payload(client):
    pid, rid = seed()
    save(pid, rid, record(message='TypeError: failed user token=FAKE_TOKEN_ONE'), record('QA-002', message='TypeError: failed user token=FAKE_TOKEN_TWO'))
    result = triage(rid)
    assert len({item['fingerprint'] for item in result}) == 2
    serialized = json.dumps(result)
    assert 'FAKE_TOKEN' not in serialized and 'TypeError:' not in serialized
    assert all(item['suggested_confidence'] == 'Low' for item in result)


def test_private_notes_excluded_from_triage_all_exports_and_clipboards(client):
    from app.paths import get_exports_dir
    pid, rid = seed()
    save(pid, rid, record(), record(viewport='Mobile'))
    fid = triage(rid)[0]['finding_id']
    conn = get_db_connection()
    conn.execute("UPDATE findings SET learning_explanation_sv='PRIVATE-TEST-12345' WHERE id=?", (fid,))
    conn.commit()
    conn.close()
    before = project_rows()
    page = client.get(f'/finding/{fid}').get_data(as_text=True)
    assert 'PRIVATE-TEST-12345' in page and 'Suggested Severity:' in page
    section = Text(page, 'automatic-triage').text
    assert 'PRIVATE-TEST-12345' not in section
    documents = [json.dumps(client.post(f'/api/runs/{rid}/triage').json),
                 Text(page, 'report-text').text, Text(page, 'report-html').text,
                 Text(client.get(f'/run/{rid}').get_data(as_text=True), 'run-report-text').text,
                 Text(client.get(f'/export/{rid}/html').get_data(as_text=True)).text]
    for format in ('txt', 'markdown', 'docx'):
        response = client.post(f'/api/export/{rid}/{format}')
        assert response.status_code == 200
        data = (Path(get_exports_dir()) / response.json['filename']).read_bytes()
        text = '\n'.join(p.text for p in Document(io.BytesIO(data)).paragraphs) if format == 'docx' else data.decode()
        documents.append(text)
    assert all('PRIVATE-TEST-12345' not in text for text in documents)
    assert all('Total Checks:' in text for text in documents[1:])
    assert project_rows() == before


def test_additive_legacy_migration_preserves_all_existing_values_and_notes(client):
    pid, rid = seed()
    save(pid, rid, record())
    conn = get_db_connection()
    conn.execute("UPDATE findings SET severity='Low',risk='High',confidence='Medium',status='RETEST',review_status='APPROVED',learning_explanation_sv='PRIVATE-TEST-12345'")
    for field in ('severity', 'risk', 'confidence'):
        conn.execute(f'ALTER TABLE findings DROP COLUMN {field}_origin')
    conn.commit()
    conn.close()
    before = project_rows()
    init_db()
    init_db()
    after = project_rows()
    origins = {field + '_origin': after['findings'][0].pop(field + '_origin') for field in ('severity', 'risk', 'confidence')}
    assert set(origins.values()) == {'legacy'}
    assert before == after
    item = triage(rid)[0]
    assert (item['effective_severity'], item['effective_risk'], item['effective_confidence']) == ('Low', 'High', 'Medium')


def test_triage_missing_run_returns_404_without_write(client):
    before = project_rows()
    assert client.get('/api/runs/999/triage').status_code == 404
    assert project_rows() == before


def test_automatic_functional_defect_provenance_and_human_override(client):
    """Verify functional defects persist with 'baseline' automatic provenance,
    triage computes suggested values without fake manual authority,
    and genuine human edits override suggestions while preserving human authority."""
    from app.qa.functional_oracle import (
        FunctionalEvaluationResult,
        FunctionalEvidence,
        DefectCategory,
        FunctionalOutcomeVerdict,
    )
    from app.qa.guarded_execution import GuardedExecutionStatus
    from app.qa.functional_reporting_adapter import persist_functional_evaluation_record
    from app.qa.reporting import load_report

    pid, rid = seed()
    conn = get_db_connection()
    conn.execute("UPDATE runs SET status='COMPLETED' WHERE id=?", (rid,))
    conn.commit()

    # 1. Create a confirmed functional defect (dangling aria-controls)
    evidence = FunctionalEvidence(
        action_attempted=True,
        action_confirmed=True,
        expanded_before=False,
        expanded_after=True,
        state_toggled=True,
        controls_count_delta=0,
        fingerprint_changed=False,
        pre_state_fingerprint="fp1",
        post_state_fingerprint="fp2",
        controlled_element_id="missing-panel",
        controlled_element_found=False,
        controlled_element_visible_before=False,
        controlled_element_visible_after=False,
        pre_observation_succeeded=True,
        post_observation_succeeded=True,
        observation_sufficient=True,
        observation_status="COMPLETED",
        rationale="Confirmed functional defect: aria-controls targets missing DOM element",
    )
    eval_res = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.SUPPORTED_FAILURE,
        is_customer_defect=True,
        defect_category=DefectCategory.DANGLING_ARIA_CONTROLS,
        target_control_id="disclosure-btn",
        action_type="click",
        evidence=evidence,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
        diagnostics={
            "confirmed_defect_category": "dangling_aria_controls",
            "defect_reason": "Disclosure specifies aria-controls='missing-panel', but element was not found in the DOM",
        },
    )

    test_id = persist_functional_evaluation_record(
        conn=conn,
        eval_res=eval_res,
        run_id=rid,
        target_url="http://fixture.invalid/profile",
        viewport="Desktop",
        enable_customer_defects=True,
    )
    assert test_id is not None

    # 2. Verify persisted provenance is 'baseline', NOT 'manual'
    f_row = conn.execute(
        "SELECT id, stable_id, severity, severity_origin, risk, risk_origin, confidence, confidence_origin "
        "FROM findings WHERE run_id = ?",
        (rid,),
    ).fetchone()
    assert f_row is not None
    fid = f_row["id"]
    assert f_row["severity_origin"] == "baseline", f"Expected baseline, got {f_row['severity_origin']}"
    assert f_row["risk_origin"] == "baseline"
    assert f_row["confidence_origin"] == "baseline"

    # 3. Verify triage produces accurate suggestion and effective value uses suggested
    triage_items = triage(rid)
    assert len(triage_items) == 1
    t_item = triage_items[0]
    assert t_item["finding_id"] == fid
    assert t_item["suggested_severity"] == "Medium"
    assert t_item["effective_severity"] == "Medium"

    # Verify report loads with baseline provenance without error
    report = load_report(conn, rid)
    assert report is not None
    assert report["customer_finding_count"] == 1
    conn.close()

    # 4. Perform a genuine human edit via API
    edit_res = client.post(f"/api/findings/{fid}/edit", json={
        "severity": "Low",
        "initial_severity": "Medium",
        "risk": "Low",
        "initial_risk": "Medium",
    })
    assert edit_res.status_code == 200

    # 5. Verify database now records genuine 'manual' provenance
    conn2 = get_db_connection()
    f_row_edited = conn2.execute(
        "SELECT severity, severity_origin, risk, risk_origin FROM findings WHERE id = ?",
        (fid,),
    ).fetchone()
    assert f_row_edited["severity"] == "Low"
    assert f_row_edited["severity_origin"] == "manual"
    assert f_row_edited["risk"] == "Low"
    assert f_row_edited["risk_origin"] == "manual"

    # 6. Verify triage prioritizes human manual override over technical suggestion
    triage_edited = triage(rid)
    assert len(triage_edited) == 1
    t_edited = triage_edited[0]
    assert t_edited["suggested_severity"] == "Medium"  # Technical suggestion preserved
    assert t_edited["effective_severity"] == "Low"      # Human override respected!
    assert t_edited["effective_risk"] == "Low"

    # 7. Verify report generation respects the human manual override
    report_edited = load_report(conn2, rid)
    conn2.close()
    finding_rep = report_edited["findings"][0]
    assert finding_rep["triage"]["effective_severity"] == "Low"
    assert finding_rep["triage"]["suggested_severity"] == "Medium"

