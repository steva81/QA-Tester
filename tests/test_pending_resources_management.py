import os
import tempfile
import json
import pytest
from server import app
from app.models.database import init_db, get_db_connection
from app.qa.scoped_mutation import ScopedMutationRegistry
from app.qa.reporting import load_report


@pytest.fixture
def client(monkeypatch):
    db_fd, db_path = tempfile.mkstemp()
    app.config['TESTING'] = True
    monkeypatch.setenv('QA_TESTER_DB_PATH', db_path)
    
    with app.app_context():
        init_db()

    with app.test_client() as c:
        c.environ_base["HTTP_X_QA_REQUEST"] = "1"
        yield c

    os.close(db_fd)
    try:
        os.unlink(db_path)
    except OSError:
        pass


def test_pending_records_discovery_and_resolution(client):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Test Pkg', 'http://127.0.0.1:8000', 'local', 'Tester')")
    proj_id = c.lastrowid
    c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (proj_id,))
    run_id = c.lastrowid

    # 1. Insert a pending record (created but delete failed/timeout)
    c.execute(
        """
        INSERT INTO mutation_ownership_records
        (run_id, project_id, target_url, account_context, entity_type, intent_id, run_prefix,
         entity_id, remote_entity_id, entity_name, status, remote_status, local_cleaned, remote_cleaned,
         details_json)
        VALUES (?, ?, 'http://127.0.0.1:8000/profile', 'tester@account', 'profile_experience', 'intent-1', '[QA-TEST-1]',
         'exp-1', 'remote-101', '[QA-TEST-1] Work Experience', 'CREATED', 'CONFIRMED_EXISTS', 1, 0, '{"note":"readback 500"}')
        """,
        (run_id, proj_id),
    )
    pending_id = c.lastrowid

    # 2. Insert a cleaned record
    c.execute(
        """
        INSERT INTO mutation_ownership_records
        (run_id, project_id, target_url, account_context, entity_type, intent_id, run_prefix,
         entity_id, remote_entity_id, entity_name, status, remote_status, local_cleaned, remote_cleaned,
         details_json)
        VALUES (?, ?, 'http://127.0.0.1:8000/profile', 'tester@account', 'profile_experience', 'intent-2', '[QA-TEST-2]',
         'exp-2', 'remote-102', '[QA-TEST-2] Cleaned Item', 'DELETED', 'CONFIRMED_DELETED', 1, 1, '{}')
        """,
        (run_id, proj_id),
    )

    # 3. Insert a not-dispatched record
    c.execute(
        """
        INSERT INTO mutation_ownership_records
        (run_id, project_id, target_url, account_context, entity_type, intent_id, run_prefix,
         entity_id, remote_entity_id, entity_name, status, remote_status, local_cleaned, remote_cleaned,
         details_json)
        VALUES (?, ?, 'http://127.0.0.1:8000/profile', 'tester@account', 'profile_experience', 'intent-3', '[QA-TEST-3]',
         'exp-3', NULL, '[QA-TEST-3] Cancelled Item', 'NOT_DISPATCHED', 'NOT_CREATED', 1, 0, '{}')
        """,
        (run_id, proj_id),
    )
    conn.commit()
    conn.close()

    # Query pending records
    pending = ScopedMutationRegistry.get_pending_records_for_display(project_id=proj_id)
    assert len(pending) == 1
    assert pending[0]['id'] == pending_id
    assert pending[0]['entity_name'] == '[QA-TEST-1] Work Experience'
    assert pending[0]['details']['note'] == 'readback 500'

    # Mark as resolved manually
    ScopedMutationRegistry.mark_record_resolved_manual(pending_id, note='Verified deleted manually in DB', project_id=proj_id)

    # Verify no pending records remain
    pending_after = ScopedMutationRegistry.get_pending_records_for_display(project_id=proj_id)
    assert len(pending_after) == 0

    conn = get_db_connection()
    row = conn.execute("SELECT status, remote_status, remote_cleaned, details_json FROM mutation_ownership_records WHERE id = ?", (pending_id,)).fetchone()
    conn.close()
    assert row['status'] == 'RESOLVED_MANUAL'
    assert row['remote_status'] == 'MANUALLY_RESOLVED'
    # Strict safety invariant: Manual resolution MUST NEVER be marked as server-confirmed remote deletion!
    assert row['remote_cleaned'] == 0
    details = json.loads(row['details_json'])
    assert details['resolved_manual'] is True
    assert details['resolved_manual_note'] == 'Verified deleted manually in DB'
    assert details['server_deletion_confirmed'] is False
    assert 'resolved_manual_at' in details


def test_api_pending_resources_endpoints(client):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('API Pkg', 'http://127.0.0.1:8000', 'local', 'Tester')")
    proj_id = c.lastrowid
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Other Pkg', 'http://127.0.0.1:8000', 'local', 'Tester')")
    other_proj_id = c.lastrowid
    c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (proj_id,))
    run_id = c.lastrowid

    c.execute(
        """
        INSERT INTO mutation_ownership_records
        (run_id, project_id, target_url, account_context, entity_type, intent_id, run_prefix,
         entity_id, remote_entity_id, entity_name, status, remote_status, local_cleaned, remote_cleaned,
         details_json)
        VALUES (?, ?, 'http://127.0.0.1:8000/profile', 'tester@account', 'profile_attachment', 'att-1', '[QA-TEST-A]',
         'att-1', 'doc-99', '[QA-TEST-A] sample.txt', 'CREATED', 'CONFIRMED_EXISTS', 1, 0, '{}')
        """,
        (run_id, proj_id),
    )
    rec_id = c.lastrowid
    conn.commit()
    conn.close()

    # GET project pending resources
    res = client.get(f'/api/projects/{proj_id}/pending-resources')
    assert res.status_code == 200
    data = res.get_json()
    assert data['count'] == 1
    assert data['pending_resources'][0]['id'] == rec_id

    # GET run pending resources
    res_run = client.get(f'/api/runs/{run_id}/pending-resources')
    assert res_run.status_code == 200
    assert res_run.get_json()['count'] == 1

    # Security check: cross-project resolution rejection (403)
    res_cross = client.post(
        f'/api/pending-resources/{rec_id}/resolve',
        json={'project_id': other_proj_id, 'note': 'Attacking another project'}
    )
    assert res_cross.status_code == 403
    assert 'does not belong to specified project' in res_cross.get_json()['error']

    # Project-scoped endpoint mismatch rejection (403)
    res_cross_scoped = client.post(
        f'/api/projects/{other_proj_id}/pending-resources/{rec_id}/resolve',
        json={'note': 'Attacking another project'}
    )
    assert res_cross_scoped.status_code == 403

    # POST resolve pending resource
    res_resolve = client.post(f'/api/pending-resources/{rec_id}/resolve', json={'project_id': proj_id, 'note': 'Target file removed from S3 bucket'})
    assert res_resolve.status_code == 200
    res_data = res_resolve.get_json()
    assert res_data['resolved'] is True
    assert res_data['server_deletion_confirmed'] is False

    # Security check: re-resolving already resolved record must fail with 400
    res_reresolve = client.post(f'/api/pending-resources/{rec_id}/resolve', json={'project_id': proj_id, 'note': 'Duplicate resolve'})
    assert res_reresolve.status_code == 400
    assert 'already resolved or deleted' in res_reresolve.get_json()['error']

    # Check both queries return 0 now
    res_after = client.get(f'/api/projects/{proj_id}/pending-resources')
    assert res_after.get_json()['count'] == 0


def test_reporting_includes_pending_resources(client):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Report Proj', 'http://127.0.0.1:8000', 'local', 'Tester')")
    proj_id = c.lastrowid
    c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (proj_id,))
    run_id = c.lastrowid
    # Insert PASS test check so tests exist
    c.execute("INSERT INTO tests (run_id, url, expected_result, actual_result, status, viewport) VALUES (?, 'http://127.0.0.1:8000', '200 OK', '200 OK', 'PASS', 'Desktop 1366x768')", (run_id,))

    # Insert pending mutation
    c.execute(
        """
        INSERT INTO mutation_ownership_records
        (run_id, project_id, target_url, account_context, entity_type, intent_id, run_prefix,
         entity_id, remote_entity_id, entity_name, status, remote_status, local_cleaned, remote_cleaned,
         details_json)
        VALUES (?, ?, 'http://127.0.0.1:8000/profile', 'tester@account', 'profile_experience', 'intent-rep', '[QA-REP]',
         'exp-rep', 'remote-rep', '[QA-REP] Uncleaned Job', 'CREATED', 'CONFIRMED_EXISTS', 1, 0, '{}')
        """,
        (run_id, proj_id),
    )
    rec_id = c.lastrowid
    conn.commit()

    report = load_report(conn, run_id)
    assert report['pending_resources_count'] == 1
    assert report['outcome'] == 'LIMITED COVERAGE'
    assert any("1 test resource(s) remain PENDING" in reason for reason in report['limitation_reasons'])

    # Resolve
    ScopedMutationRegistry.mark_record_resolved_manual(rec_id, "Cleaned", project_id=proj_id)
    report_resolved = load_report(conn, run_id)
    assert report_resolved['pending_resources_count'] == 0
    assert not any("PENDING in the test environment" in reason for reason in report_resolved['limitation_reasons'])
    conn.close()


def test_contract_lifecycle_endpoints(client):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Contract Proj', 'http://127.0.0.1:8000', 'local', 'Tester')")
    proj_id = c.lastrowid
    conn.commit()
    conn.close()

    # Add contract via API
    add_res = client.post(
        f'/api/projects/{proj_id}/contracts',
        json={
            'contract_id': 'contact-test-contract',
            'rule_name': 'Contact Feedback Validation',
            'target_url_pattern': '*/contact',
            'target_control_id': 'btn-submit',
            'feedback_selector': '#feedback',
            'completion_mode': 'loading_transition',
            'field_selectors': {'email': '#email'},
            'test_cases': [{'case_id': 'bad', 'description': 'Invalid email', 'input_values': {'email': 'bad'}, 'expected_valid': False, 'expected_feedback_substring': 'Invalid email'}],
        },
    )
    assert add_res.status_code == 201
    contract_row_id = add_res.get_json()['id']

    # Get contracts
    get_res = client.get(f'/api/projects/{proj_id}/contracts')
    assert get_res.status_code == 200
    contracts = get_res.get_json()
    assert len(contracts) == 1
    assert contracts[0]['contract_id'] == 'contact-test-contract'

    # Delete contract
    del_res = client.delete(f'/api/projects/{proj_id}/contracts/{contract_row_id}')
    assert del_res.status_code == 200
    assert del_res.get_json()['status'] == 'success'

    # Verify deleted
    get_after = client.get(f'/api/projects/{proj_id}/contracts')
    assert len(get_after.get_json()) == 0

    # Delete non-existent returns 404
    del_404 = client.delete(f'/api/projects/{proj_id}/contracts/99999')
    assert del_404.status_code == 404
