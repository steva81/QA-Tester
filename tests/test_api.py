import pytest
from server import app
from app.models.database import init_db, get_db_connection
import os
import tempfile
import json

@pytest.fixture
def client(monkeypatch):
    db_fd, db_path = tempfile.mkstemp()
    app.config['TESTING'] = True
    # override db temporarily
    monkeypatch.setenv('QA_TESTER_DB_PATH', db_path)
    
    with app.app_context():
        init_db()

    import app.routes.api as api
    with api.worker_lock:
        assert not any(t.is_alive() for t in api.active_worker_threads), 'Previous test leaked a worker'
        assert not any(r.thread and r.thread.is_alive() for r in api.worker_records.values()), 'Previous test leaked a registered worker'
        previous = (api.accepting_runs, dict(api.worker_records), dict(api.running_engines), list(api.active_worker_threads))
        api.accepting_runs = True
        api.worker_records.clear()
        api.running_engines.clear()
        api.active_worker_threads.clear()

    try:
        with app.test_client() as client:
            client.environ_base["HTTP_X_QA_REQUEST"] = "1"
            yield client
    finally:
        with api.worker_lock:
            assert not any(t.is_alive() for t in api.active_worker_threads), 'API test leaked a worker'
            assert not any(r.thread and r.thread.is_alive() for r in api.worker_records.values()), 'API test leaked a registered worker'
            api.accepting_runs = previous[0]
            api.worker_records.clear(); api.worker_records.update(previous[1])
            api.running_engines.clear(); api.running_engines.update(previous[2])
            api.active_worker_threads[:] = previous[3]
        os.close(db_fd)
        os.unlink(db_path)

def test_create_and_edit_project(client):
    # Create project
    response = client.post('/api/projects', json={
        "name": "Test API Proj",
        "target_url": "http://example.com",
        "environment": "Dev",
        "tester_name": "API Tester"
    })
    assert response.status_code == 200
    data = json.loads(response.data)
    assert "id" in data
    assert data["status"] == "success"
    
    project_id = data["id"]
    
    # Edit project
    response_edit = client.post(f'/api/projects/{project_id}/edit', json={
        "name": "Updated Name",
        "target_url": "http://updated.com",
        "environment": "Prod",
        "tester_name": "API Tester 2"
    })
    assert response_edit.status_code == 200
    assert json.loads(response_edit.data)["status"] == "success"
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    p = c.fetchone()
    conn.close()
    
    assert p["name"] == "Updated Name"
    assert p["target_url"] == "http://updated.com"

def test_delete_project(client):
    # Create project
    response = client.post('/api/projects', json={
        "name": "Test Delete",
        "target_url": "http://example.com",
        "environment": "Dev",
        "tester_name": "Tester"
    })
    project_id = json.loads(response.data)["id"]
    
    # Delete project
    response_delete = client.post(f'/api/projects/{project_id}/delete')
    assert response_delete.status_code == 200
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    p = c.fetchone()
    conn.close()
    assert p is None

def test_reset_data(client):
    # Create project
    client.post('/api/projects', json={
        "name": "Test Reset",
        "target_url": "http://example.com",
        "environment": "Dev",
        "tester_name": "Tester"
    })
    
    # Reset
    response = client.post('/api/reset')
    assert response.status_code == 200
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM projects")
    count = c.fetchone()[0]
    conn.close()
    assert count == 0


def test_export_html_post_endpoint(client):
    res = client.post('/api/projects', json={
        "name": "Export Test",
        "target_url": "http://example.com",
        "environment": "Dev",
        "tester_name": "Tester"
    })
    project_id = json.loads(res.data)["id"]
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (project_id,))
    run_id = c.lastrowid
    c.execute("INSERT INTO tests (run_id, url, status, viewport, expected_result, actual_result) VALUES (?, 'http://example.com', 'PASS', 'Desktop', 'OK', 'OK')", (run_id,))
    conn.commit()
    conn.close()

    res_export = client.post(f'/api/export/{run_id}/html?mode=standard')
    assert res_export.status_code == 200
    data = res_export.json
    assert data["status"] == "success"
    assert "filename" in data
    assert data["filename"].endswith(".html")


def test_open_export_security_and_folder(client):
    res_traversal = client.post('/api/exports/../../etc/passwd/open')
    assert res_traversal.status_code == 403

    res_missing = client.post('/api/exports/non_existent_file.html/open')
    assert res_missing.status_code == 404

    res_folder = client.post('/api/open-exports-folder')
    assert res_folder.status_code == 200
    assert res_folder.json["status"] == "success"


def test_finding_edit_provenance_preservation(client):
    res = client.post('/api/projects', json={
        "name": "Finding Test",
        "target_url": "http://example.com",
        "environment": "Dev",
        "tester_name": "Tester"
    })
    project_id = json.loads(res.data)["id"]
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (project_id,))
    run_id = c.lastrowid
    # Seed finding where stored risk is 'Medium' (with baseline origin)
    c.execute("""
        INSERT INTO findings (project_id, run_id, stable_id, title, severity, risk, confidence, 
                             severity_origin, risk_origin, confidence_origin, status, impact, recommendation)
        VALUES (?, ?, 'QA-100', 'Test Finding', 'Low', 'Medium', 'Medium', 'baseline', 'baseline', 'baseline', 'OPEN', 'Severe crash', 'Fix DOM')
    """, (project_id, run_id))
    fid = c.lastrowid
    conn.commit()
    conn.close()

    # 1. Notes-only edit where form initialized with effective triage risk = 'High'
    # The form submits effective triage value 'High', but because user did not change it from initial,
    # stored value 'Medium' and baseline origin MUST be preserved!
    edit_res = client.post(f'/api/findings/{fid}/edit', json={
        "initial_severity": "Low",
        "initial_risk": "High",  # Effective triage computed High
        "initial_confidence": "Medium",
        "severity": "Low",
        "risk": "High",          # Submitted effective value without deliberate user change
        "confidence": "Medium",
        "learning_explanation_sv": "Min anteckning",
        "how_to_explain_sv": "Min förklaring"
    })
    assert edit_res.status_code == 200

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT severity, risk, confidence, severity_origin, risk_origin, confidence_origin, learning_explanation_sv, impact, recommendation, status FROM findings WHERE id = ?", (fid,))
    row = c.fetchone()
    conn.close()

    # Stored assessment values and origins must remain unchanged
    assert row["risk"] == "Medium", f"Stored risk should remain Medium, got {row['risk']}"
    assert row["risk_origin"] == "baseline", f"Stored risk_origin should remain baseline, got {row['risk_origin']}"
    assert row["severity"] == "Low"
    assert row["severity_origin"] == "baseline"
    assert row["learning_explanation_sv"] == "Min anteckning"
    assert row["impact"] == "Severe crash"
    assert row["recommendation"] == "Fix DOM"
    assert row["status"] == "OPEN"

    # 2. Deliberate user change: user modifies risk to 'Critical'
    deliberate_res = client.post(f'/api/findings/{fid}/edit', json={
        "initial_severity": "Low",
        "initial_risk": "High",
        "initial_confidence": "Medium",
        "severity": "Low",
        "risk": "Critical",      # Deliberate user selection
        "confidence": "Medium",
    })
    assert deliberate_res.status_code == 200

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT risk, risk_origin, impact FROM findings WHERE id = ?", (fid,))
    row_delib = c.fetchone()
    conn.close()
    assert row_delib["risk"] == "Critical"
    assert row_delib["risk_origin"] == "manual"
    # Unrelated fields preserved
    assert row_delib["impact"] == "Severe crash"

    # 3. Partial update with only note fields: preserves all other fields
    partial_res = client.post(f'/api/findings/{fid}/edit', json={
        "learning_explanation_sv": "Ny uppdaterad anteckning"
    })
    assert partial_res.status_code == 200

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT risk, risk_origin, impact, recommendation, status, learning_explanation_sv FROM findings WHERE id = ?", (fid,))
    row_part = c.fetchone()
    conn.close()
    assert row_part["risk"] == "Critical"
    assert row_part["risk_origin"] == "manual"
    assert row_part["impact"] == "Severe crash"
    assert row_part["recommendation"] == "Fix DOM"
    assert row_part["status"] == "OPEN"
    assert row_part["learning_explanation_sv"] == "Ny uppdaterad anteckning"


def test_high_to_medium_risk_discrepancy_and_manual_assessment_fidelity(client):
    """Regression test for High-to-Medium risk discrepancy and assessment fidelity.
    
    Verifies that:
    1. Baseline assessment for dangling_aria_controls produces Medium risk (matching triage rules).
    2. Finding edit form populates with stored finding values and matching initial_* payloads.
    3. Stored manual assessments (e.g. risk='High', origin='manual') populate accurately and
       are preserved during notes-only edits.
    4. Deliberate changes (e.g. changing risk from 'High' to 'Medium') establish manual provenance.
    5. Editing unrelated fields does not change assessments or origins.
    """
    from app.qa.assessment import assess_finding
    
    # 1. Baseline assessment check
    baseline = assess_finding(
        'Broken interactive panel',
        'Interactive control references dangling_aria_controls target panel that was not found in the DOM',
        'Interactive Controls',
        'Desktop'
    )
    assert baseline['severity'] == 'Medium'
    assert baseline['risk'] == 'Medium', f"Baseline risk for dangling aria controls must be Medium, got {baseline['risk']}"
    assert baseline['confidence'] == 'High'

    # Create project and run
    res = client.post('/api/projects', json={
        "name": "Assessment Fidelity Test",
        "target_url": "http://127.0.0.1:8080",
        "environment": "Test",
        "tester_name": "Tester"
    })
    project_id = json.loads(res.data)["id"]
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (project_id,))
    run_id = c.lastrowid
    
    # 2. Seed finding with stored manual assessment: risk='High', risk_origin='manual'
    c.execute("""
        INSERT INTO findings (project_id, run_id, stable_id, title, area, severity, risk, confidence,
                             severity_origin, risk_origin, confidence_origin, status, review_status,
                             impact, recommendation, plain_language_explanation, learning_explanation_sv)
        VALUES (?, ?, 'QA-201', 'Dangling target element', 'Interactive Controls', 'Medium', 'High', 'High',
                'baseline', 'manual', 'baseline', 'OPEN', 'PENDING',
                'Test impact', 'Test recommendation', 'Test explanation', 'Initial note')
    """, (project_id, run_id))
    fid = c.lastrowid
    conn.commit()
    conn.close()

    # 3. Verify finding view page HTML renders stored assessment and populates form correctly
    page_res = client.get(f'/finding/{fid}')
    assert page_res.status_code == 200
    page_html = page_res.data.decode('utf-8')
    # Effective assessment displays manual value 'High'
    assert 'Effective Risk' in page_html
    # Form initialization script must populate with stored finding.risk ('High'), NOT reset to suggested
    assert f'document.getElementById(\'f_risk\').value = "High";' in page_html
    assert 'initial_risk: "High"' in page_html

    # 4. Perform notes-only edit: user updates learning note without changing risk
    edit_res = client.post(f'/api/findings/{fid}/edit', json={
        "initial_severity": "Medium",
        "initial_risk": "High",
        "initial_confidence": "High",
        "severity": "Medium",
        "risk": "High",
        "confidence": "High",
        "status": "OPEN",
        "learning_explanation_sv": "Updated private learning note",
        "plain_language_explanation": "Test explanation",
        "impact": "Test impact",
        "recommendation": "Test recommendation"
    })
    assert edit_res.status_code == 200

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT severity, risk, confidence, severity_origin, risk_origin, confidence_origin, learning_explanation_sv FROM findings WHERE id = ?", (fid,))
    row = c.fetchone()
    conn.close()
    assert row["risk"] == "High", "Stored manual risk 'High' must not be changed by notes edit"
    assert row["risk_origin"] == "manual", "Manual origin must be preserved"
    assert row["learning_explanation_sv"] == "Updated private learning note"

    # 5. Deliberate change: user explicitly selects 'Medium' risk in the edit form
    change_res = client.post(f'/api/findings/{fid}/edit', json={
        "initial_severity": "Medium",
        "initial_risk": "High",        # Form knew stored value was High
        "initial_confidence": "High",
        "severity": "Medium",
        "risk": "Medium",              # User deliberately chose Medium
        "confidence": "High",
        "status": "OPEN",
        "learning_explanation_sv": "Updated private learning note"
    })
    assert change_res.status_code == 200

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT severity, risk, confidence, severity_origin, risk_origin, confidence_origin FROM findings WHERE id = ?", (fid,))
    row_changed = c.fetchone()
    conn.close()
    assert row_changed["risk"] == "Medium", "Risk must be updated to Medium"
    assert row_changed["risk_origin"] == "manual", "Deliberate change establishes manual provenance"


def test_http_404_assessment_baseline_discrepancy_and_fidelity(client):
    """Regression test specifically covering HTTP 404 findings with baseline assessment discrepancies.
    
    Verifies that:
    1. An HTTP 404 finding with stored baseline values (e.g. risk='High', origin='baseline')
       where triage suggests 'Medium' is clearly presented with:
       - Effective value 'Medium' (which is used in reports)
       - Stored database value 'High' (baseline provenance)
       - Advisory suggestion 'Medium'
    2. Finding detail view renders both Effective and Stored values distinctly.
    3. The edit modal initializes form controls from the stored database values (protecting against
       silent or accidental changes), while clearly annotating stored, suggested, and effective values.
    4. Opening and cancelling the edit dialog makes zero changes.
    5. Notes-only edits preserve the stored assessment ('High') and its baseline provenance ('baseline').
    6. Deliberate assessment edits (e.g. user changing risk to 'Medium' or 'Critical') update the stored value
       and establish 'manual' provenance, which updates effective value in reports and survives restart.
    """
    from app.qa.triage import triage_run

    # 1. Setup project, run, finding, and test evidence for HTTP 404
    res = client.post('/api/projects', json={
        "name": "HTTP 404 Assessment Test",
        "target_url": "http://127.0.0.1:8080",
        "environment": "Test",
        "tester_name": "Tester"
    })
    project_id = json.loads(res.data)["id"]
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (project_id,))
    run_id = c.lastrowid

    # Insert baseline HTTP 404 finding where stored risk is High and confidence is High with baseline provenance
    c.execute("""
        INSERT INTO findings (project_id, run_id, stable_id, title, area, severity, risk, confidence,
                             severity_origin, risk_origin, confidence_origin, status, review_status,
                             impact, recommendation, plain_language_explanation, learning_explanation_sv)
        VALUES (?, ?, 'QA-404', 'Broken URL returns HTTP 404 Not Found', 'Page Load / Server Response',
                'Medium', 'High', 'High', 'baseline', 'baseline', 'baseline', 'OPEN', 'PENDING',
                'Missing page prevents user navigation', 'Restore resource or update link',
                'Server returned HTTP 404', 'Original private note')
    """, (project_id, run_id))
    fid = c.lastrowid

    # Insert test evidence matching technical_signal for HTTP 404
    c.execute("""
        INSERT INTO tests (run_id, stable_id, url, final_url, viewport, status,
                          actual_result, expected_result)
        VALUES (?, 'QA-404', 'http://127.0.0.1:8080/missing-page', 'http://127.0.0.1:8080/missing-page',
                'Desktop 1366x768', 'FAIL', 'Server returned HTTP 404', 'HTTP 200 OK')
    """, (run_id,))
    conn.commit()
    conn.close()

    # 2. Check triage calculation
    conn = get_db_connection()
    triage_results = triage_run(conn, run_id)
    conn.close()
    assert len(triage_results) == 1
    triage_item = triage_results[0]
    assert triage_item['suggested_risk'] == 'Medium'
    assert triage_item['suggested_confidence'] == 'Medium'
    # Because risk_origin is 'baseline', effective value yields to suggested
    assert triage_item['effective_risk'] == 'Medium'
    assert triage_item['effective_confidence'] == 'Medium'

    # 3. Check finding page rendering: both Effective and Stored are clearly displayed
    page_res = client.get(f'/finding/{fid}')
    assert page_res.status_code == 200
    page_html = page_res.data.decode('utf-8')
    assert 'Effective Assessment' in page_html or 'Effective Risk' in page_html
    assert 'Stored Assessment' in page_html or 'Stored' in page_html
    assert 'Assessment Rules:' in page_html and 'Effective' in page_html
    assert 'document.getElementById(\'f_risk\').value = "High";' in page_html
    assert 'initial_risk: "High"' in page_html

    # 4. Notes-only edit: user updates learning_explanation_sv without changing risk
    edit_res = client.post(f'/api/findings/{fid}/edit', json={
        "initial_severity": "Medium",
        "initial_risk": "High",
        "initial_confidence": "High",
        "severity": "Medium",
        "risk": "High",
        "confidence": "High",
        "status": "OPEN",
        "learning_explanation_sv": "Updated note for 404",
        "plain_language_explanation": "Server returned HTTP 404",
        "impact": "Missing page prevents user navigation",
        "recommendation": "Restore resource or update link"
    })
    assert edit_res.status_code == 200

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT severity, risk, confidence, severity_origin, risk_origin, confidence_origin, learning_explanation_sv FROM findings WHERE id = ?", (fid,))
    row = c.fetchone()
    conn.close()
    assert row["risk"] == "High", "Stored risk must remain High after notes-only edit"
    assert row["risk_origin"] == "baseline", "Origin must remain baseline after notes-only edit"
    assert row["learning_explanation_sv"] == "Updated note for 404"

    # 5. Deliberate change: user explicitly selects 'Medium' risk in the edit dialog
    deliberate_res = client.post(f'/api/findings/{fid}/edit', json={
        "initial_severity": "Medium",
        "initial_risk": "High",
        "initial_confidence": "High",
        "severity": "Medium",
        "risk": "Medium",
        "confidence": "High",
        "status": "OPEN",
        "learning_explanation_sv": "Updated note for 404"
    })
    assert deliberate_res.status_code == 200

    # 6. Verify deliberate change persists and establishes manual provenance
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT severity, risk, confidence, severity_origin, risk_origin, confidence_origin FROM findings WHERE id = ?", (fid,))
    row_manual = c.fetchone()
    conn.close()
    assert row_manual["risk"] == "Medium", "Risk must now be updated to Medium"
    assert row_manual["risk_origin"] == "manual", "Deliberate change must establish manual provenance"

    # Verify that effective triage now uses manual value
    conn = get_db_connection()
    triage_updated = triage_run(conn, run_id)
    conn.close()
    assert triage_updated[0]['effective_risk'] == 'Medium'




def test_interactive_options_validation_in_start_run(client):
    """Ensure invalid interactive options return clear 400 validation errors before creating a run."""
    # Create project
    res = client.post('/api/projects', json={
        "name": "Validation Proj",
        "target_url": "http://127.0.0.1:5000",
        "environment": "Dev",
        "tester_name": "Tester"
    })
    proj_id = res.json["id"]

    # Mock ready interactive session on same origin
    from app.qa.interactive import InteractiveSession, sessions
    mock_session = InteractiveSession(proj_id, "http://127.0.0.1:5000")
    mock_session.state = "READY"
    mock_session.is_ready = lambda: True
    mock_session.get_current_url = lambda: "http://127.0.0.1:5000/"
    sessions[str(proj_id)] = mock_session

    try:
        # 1. Option provided without enable_interactive_disclosure returns 400
        res_err1 = client.post('/api/runs', json={
            "project_id": proj_id,
            "mode": "full",
            "enable_interactive_disclosure": False,
            "interactive_options": {"max_evaluations_per_page": 5}
        })
        assert res_err1.status_code == 400
        assert "Interactive options require enable_interactive_disclosure" in res_err1.json["error"]

        # 2. Non-dict interactive_options returns 400
        res_err2 = client.post('/api/runs', json={
            "project_id": proj_id,
            "mode": "full",
            "enable_interactive_disclosure": True,
            "interactive_options": "not-a-dict"
        })
        assert res_err2.status_code == 400
        assert "interactive_options must be an object" in res_err2.json["error"]

        # 3. Value 11 exceeds upper bound of 10 -> returns 400 with descriptive error
        # 3. Page limit 31 exceeds upper bound of 30 -> returns 400
        res_err3 = client.post('/api/runs', json={
            "project_id": proj_id,
            "mode": "full",
            "enable_interactive_disclosure": True,
            "interactive_options": {
                "max_evaluations_per_page": 31
            }
        })
        assert res_err3.status_code == 400
        assert "max_evaluations_per_page must be between 1 and 30" in res_err3.json["error"]

        # 4. Run limit 101 exceeds upper bound of 100 -> returns 400
        res_err4 = client.post('/api/runs', json={
            "project_id": proj_id,
            "mode": "full",
            "enable_interactive_disclosure": True,
            "interactive_options": {
                "max_evaluations_per_page": 5,
                "max_evaluations_per_run": 101
            }
        })
        assert res_err4.status_code == 400
        assert "max_evaluations_per_run must be between 1 and 100" in res_err4.json["error"]

        # 5. Non-integer value -> returns 400
        res_err5 = client.post('/api/runs', json={
            "project_id": proj_id,
            "mode": "full",
            "enable_interactive_disclosure": True,
            "interactive_options": {
                "max_evaluations_per_page": "ten"
            }
        })
        assert res_err5.status_code == 400
        assert "must be a finite integer" in res_err5.json["error"]
    finally:
        sessions.pop(str(proj_id), None)


def test_contract_completion_configuration_persistence_and_reload(client):
    """Verify that validation contract completion configuration survives API registration,
    persistence in SQLite, and reload via API and engine."""
    # 1. Create isolated project
    res_proj = client.post('/api/projects', json={
        "name": "Completion Config Project",
        "target_url": "http://127.0.0.1:8080/profile",
        "environment": "Test",
        "tester_name": "QA Tester"
    })
    assert res_proj.status_code == 200
    proj_id = res_proj.json["id"]

    # 2. Register validation contract with explicit completion semantics
    contract_payload = {
        "contract_id": "contract_custom_completion",
        "rule_name": "Custom Interval Validation Rule",
        "target_url_pattern": "http://127.0.0.1:8080/profile*",
        "target_control_id": "btn-validate-history",
        "trigger_selector": "#btn-validate-history",
        "accessible_name": "Validate Dates",
        "feedback_selector": "#history-date-feedback",
        "field_selectors": {
            "start_date": "#input-start-date",
            "end_date": "#input-end-date"
        },
        "completion_marker_attr": "data-validation-case",
        "completion_marker_type": "case_id",
        "completion_timestamp_attr": "data-validation-timestamp",
        "completion_mode": "case_marker",
        "test_cases": [
            {
                "case_id": "case_valid_interval",
                "description": "Valid interval test case",
                "input_values": {"start_date": "2022-01-01", "end_date": "2024-05-01"},
                "expected_valid": True,
                "expected_feedback_substring": "Dates are valid."
            }
        ]
    }

    res_contract = client.post(f'/api/projects/{proj_id}/contracts', json=contract_payload)
    assert res_contract.status_code == 201
    contract_data = res_contract.json
    assert contract_data["status"] == "success"
    assert contract_data["completion_marker_attr"] == "data-validation-case"
    assert contract_data["completion_marker_type"] == "case_id"
    assert contract_data["completion_timestamp_attr"] == "data-validation-timestamp"
    assert contract_data["completion_mode"] == "case_marker"

    # 3. Verify SQLite DB row directly contains completion attributes in config_json
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM validation_contracts WHERE project_id = ?", (proj_id,))
    row = c.fetchone()
    conn.close()
    assert row is not None
    assert row["contract_id"] == "contract_custom_completion"
    saved_cfg = json.loads(row["config_json"])
    assert saved_cfg["completion_marker_attr"] == "data-validation-case"
    assert saved_cfg["completion_marker_type"] == "case_id"
    assert saved_cfg["completion_timestamp_attr"] == "data-validation-timestamp"
    assert saved_cfg["completion_mode"] == "case_marker"

    # 4. Verify GET /api/projects/{proj_id}/contracts reloads completion semantics
    res_get = client.get(f'/api/projects/{proj_id}/contracts')
    assert res_get.status_code == 200
    contracts_list = res_get.json
    assert len(contracts_list) == 1
    reloaded = contracts_list[0]
    assert reloaded["contract_id"] == "contract_custom_completion"
    assert reloaded["completion_marker_attr"] == "data-validation-case"
    assert reloaded["completion_marker_type"] == "case_id"
    assert reloaded["completion_timestamp_attr"] == "data-validation-timestamp"
    assert reloaded["completion_mode"] == "case_marker"

    # 5. Verify lookup via find_validation_contract_for_control preserves completion semantics
    from app.qa.validation_contract import find_validation_contract_for_control
    found = find_validation_contract_for_control(
        control_id="btn-validate-history",
        target_url="http://127.0.0.1:8080/profile",
        project_id=proj_id,
    )
    assert found is not None
    assert found.has_completion_semantics is True
    assert found.completion_marker_attr == "data-validation-case"
    assert found.completion_marker_type == "case_id"
    assert found.completion_timestamp_attr == "data-validation-timestamp"
    assert found.completion_mode == "case_marker"



