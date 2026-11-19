import pytest
import os
import json
from unittest.mock import MagicMock, AsyncMock

from server import app
from app.models.database import init_db, get_db_connection
from app.qa.scoped_mutation import ScopedEntityOwnershipRegistry, ScopedMutationRegistry
from app.qa.validation_contract import (
    ValidationContract,
    register_validation_contract,
    clear_validation_contracts,
    _REGISTERED_BINDINGS,
)
import app.qa.validation_contract as vc


@pytest.mark.parametrize('historical', [False, True])
def test_null_remote_status_visible_and_manual_resolution_is_not_deletion(client, historical):
    pid = _create_project()
    rid = _create_run(pid)
    record = _create_record(-rid if historical else rid, -pid if historical else pid, 'legacy')
    conn = get_db_connection()
    conn.execute('UPDATE mutation_ownership_records SET remote_status=NULL WHERE id=?', (record,))
    conn.commit()
    before = '\n'.join(conn.iterdump())
    if historical:
        pending = ScopedMutationRegistry.get_historical_records(state='pending', limit=1)
        assert [r['record_id'] for r in pending] == [record]
        assert not ScopedMutationRegistry.get_historical_records(before_id=record)
        assert len(ScopedMutationRegistry.get_historical_records(state='all')) == 1
    else:
        assert len(ScopedMutationRegistry.get_pending_records_for_display(project_id=pid)) == 1
        assert len(ScopedMutationRegistry.get_pending_or_uncleaned_records(project_id=pid)) == 1
    assert '\n'.join(conn.iterdump()) == before
    endpoint = f'/api/historical-resources/{record}/resolve' if historical else f'/api/projects/{pid}/pending-resources/{record}/resolve'
    body = {'note': 'Checked in isolated test'}
    if historical:
        body['archive_project_key'] = f'legacy-record:{record}'
    response = client.post(endpoint, json=body, headers={'X-QA-Request': '1'})
    assert response.status_code == 200
    assert response.json['server_deletion_confirmed'] is False
    row = conn.execute('SELECT * FROM mutation_ownership_records WHERE id=?', (record,)).fetchone()
    assert row['remote_cleaned'] == 0
    assert row['remote_status'] == 'MANUALLY_RESOLVED'
    assert json.loads(row['details_json'])['previous_remote_status'] is None
    if historical:
        assert not ScopedMutationRegistry.get_historical_records()
        assert len(ScopedMutationRegistry.get_historical_records(state='all')) == 1
    else:
        assert not ScopedMutationRegistry.get_pending_records_for_display(project_id=pid)
    conn.close()


@pytest.mark.parametrize('status', ['NOT_DISPATCHED', 'NOT_CREATED', 'DELETED', 'RESOLVED_MANUAL'])
def test_null_status_does_not_reopen_terminal_history(status):
    record = _create_record(-1, -1, 'terminal')
    conn = get_db_connection()
    conn.execute('UPDATE mutation_ownership_records SET status=?,remote_status=NULL WHERE id=?', (status, record))
    conn.commit(); conn.close()
    assert not ScopedMutationRegistry.get_historical_records()
    assert not ScopedMutationRegistry.mark_record_resolved_manual(record, 'must not overwrite', archive_project_key=f'legacy-record:{record}')


def test_corrupt_null_status_history_visible_but_immutable(client):
    record = _create_record(-1, -1, 'corrupt')
    conn = get_db_connection()
    conn.execute("UPDATE mutation_ownership_records SET remote_status=NULL,details_json='[invalid' WHERE id=?", (record,))
    conn.commit()
    before = '\n'.join(conn.iterdump())
    assert ScopedMutationRegistry.get_historical_records()[0]['metadata_corrupt'] is True
    response = client.post(f'/api/historical-resources/{record}/resolve', json={'archive_project_key':f'legacy-record:{record}','note':'no'}, headers={'X-QA-Request':'1'})
    assert response.status_code == 409
    assert '\n'.join(conn.iterdump()) == before
    conn.close()


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_stage4.db")
    monkeypatch.setenv("QA_TESTER_DB_PATH", db_path)
    monkeypatch.setenv("QA_TESTER_WRITABLE_DIR", str(tmp_path))

    with app.app_context():
        init_db()

    clear_validation_contracts()
    yield
    clear_validation_contracts()


@pytest.fixture
def client():
    app.config["TESTING"] = True
    return app.test_client()


def _create_project(name="Test Project", url="http://127.0.0.1:8080"):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO projects (name, target_url, environment, tester_name) VALUES (?, ?, 'Staging', 'Tester')",
        (name, url),
    )
    pid = c.lastrowid
    conn.commit()
    conn.close()
    return pid


def _create_run(project_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (project_id,))
    rid = c.lastrowid
    conn.commit()
    conn.close()
    return rid


def _create_record(run_id, project_id, entity_name, remote_id=None, details=None):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO mutation_ownership_records (
            run_id, project_id, target_url, entity_type, entity_id, remote_entity_id,
            entity_name, status, remote_status, details_json
        ) VALUES (?, ?, 'http://127.0.0.1:8080', 'profile_exp', 'eid-1', ?, ?, 'CREATED', 'CONFIRMED_EXISTS', ?)
        """,
        (run_id, project_id, remote_id, entity_name, json.dumps(details or {})),
    )
    rec_id = c.lastrowid
    conn.commit()
    conn.close()
    return rec_id


def test_generation_decoupling_keys_and_isolation(client):
    headers = {"X-QA-Request": "1"}

    # Generation 1: Project 1, Run 1, Record A
    p1 = _create_project("Project Generation 1")
    r1 = _create_run(p1)
    rec_a_id = _create_record(r1, p1, "Record A", remote_id="rem-A")

    # Reset
    res_reset1 = client.post("/api/reset", headers=headers)
    assert res_reset1.status_code == 200

    # Generation 2: New Project 1, Run 1, Record B
    p2 = _create_project("Project Generation 2")
    r2 = _create_run(p2)
    rec_b_id = _create_record(r2, p2, "Record B", remote_id="rem-B")

    # Both records have unique record_id, and now rec_a has negative IDs
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, project_id, run_id, details_json FROM mutation_ownership_records WHERE id = ?", (rec_a_id,))
    row_a = c.fetchone()
    assert row_a["project_id"] == -p1
    assert row_a["run_id"] == -r1
    details_a = json.loads(row_a["details_json"])
    key_a = details_a["historical_detachment"]["archive_project_key"]
    assert key_a

    # Reset again
    res_reset2 = client.post("/api/reset", headers=headers)
    assert res_reset2.status_code == 200

    # Generation 3: New Project
    p3 = _create_project("Project Generation 3")
    r3 = _create_run(p3)

    # 1. A and B have different archive_project_keys and unique record_ids
    c.execute("SELECT id, project_id, run_id, details_json FROM mutation_ownership_records WHERE id = ?", (rec_b_id,))
    row_b = c.fetchone()
    assert row_b["project_id"] == -p2
    assert row_b["run_id"] == -r2
    details_b = json.loads(row_b["details_json"])
    key_b = details_b["historical_detachment"]["archive_project_key"]
    assert key_b
    assert key_a != key_b
    assert rec_a_id != rec_b_id

    # 2. Generation 3 inherits ZERO contracts, pending records or recovery actions
    active_records = ScopedMutationRegistry.get_pending_records_for_display(project_id=p3)
    assert len(active_records) == 0
    assert len(vc._REGISTERED_BINDINGS) == 0

    # 3. History endpoint shows both A and B; active display shows neither
    hist_res = client.get("/api/historical-resources?state=pending")
    assert hist_res.status_code == 200
    hist_items = hist_res.get_json()["historical_resources"]
    hist_ids = [item["record_id"] for item in hist_items]
    assert rec_a_id in hist_ids
    assert rec_b_id in hist_ids

    # 4. Second reset did NOT mutate A's metadata, status, timestamp, or key
    c.execute("SELECT id, project_id, run_id, details_json FROM mutation_ownership_records WHERE id = ?", (rec_a_id,))
    row_a_after = c.fetchone()
    details_a_after = json.loads(row_a_after["details_json"])
    assert details_a_after == details_a

    conn.close()


def test_manual_resolution_independence_and_key_mismatch_denial(client):
    headers = {"X-QA-Request": "1"}

    # Create project, run, record A and record B, then reset
    p1 = _create_project("Project A")
    r1 = _create_run(p1)
    rec_a_id = _create_record(r1, p1, "Record A")
    rec_b_id = _create_record(r1, p1, "Record B")

    client.post("/api/reset", headers=headers)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT details_json FROM mutation_ownership_records WHERE id = ?", (rec_a_id,))
    key_a = json.loads(c.fetchone()["details_json"])["historical_detachment"]["archive_project_key"]
    c.execute("SELECT details_json FROM mutation_ownership_records WHERE id = ?", (rec_b_id,))
    key_b = json.loads(c.fetchone()["details_json"])["historical_detachment"]["archive_project_key"]
    conn.close()

    # Resolve A with wrong key -> 403 Forbidden
    res_wrong_key = client.post(
        f"/api/historical-resources/{rec_a_id}/resolve",
        headers=headers,
        json={"archive_project_key": "wrong-key-xyz", "note": "Verified cleaned"}
    )
    assert res_wrong_key.status_code == 403

    # Resolve A with correct key -> 200 OK
    res_resolve_a = client.post(
        f"/api/historical-resources/{rec_a_id}/resolve",
        headers=headers,
        json={"archive_project_key": key_a, "note": "Verified cleaned"}
    )
    assert res_resolve_a.status_code == 200

    # 5. Resolving A leaves B unchanged (B is still pending)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT status, remote_status, remote_cleaned FROM mutation_ownership_records WHERE id = ?", (rec_a_id,))
    row_a = c.fetchone()
    assert row_a["status"] == "RESOLVED_MANUAL"
    assert row_a["remote_status"] == "MANUALLY_RESOLVED"
    assert row_a["remote_cleaned"] == 0  # Distinguishes manual resolution from server proof!

    c.execute("SELECT status, remote_status, remote_cleaned FROM mutation_ownership_records WHERE id = ?", (rec_b_id,))
    row_b = c.fetchone()
    assert row_b["status"] == "CREATED"
    assert row_b["remote_status"] == "CONFIRMED_EXISTS"
    conn.close()


def test_legacy_record_without_key_supported_and_resolvable(client):
    headers = {"X-QA-Request": "1"}

    # Simulate an older detached record without historical_detachment key
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO mutation_ownership_records (
            run_id, project_id, target_url, entity_type, entity_id, entity_name, status, remote_status, details_json
        ) VALUES (-1, -1, 'http://127.0.0.1:8080', 'item', 'e1', 'Legacy Item', 'CREATED', 'CONFIRMED_EXISTS', '{}')
        """
    )
    leg_id = c.lastrowid
    conn.commit()
    conn.close()

    # 6. Legacy record shows virtual identity legacy-record:<id>
    hist_res = client.get("/api/historical-resources?state=pending")
    items = hist_res.get_json()["historical_resources"]
    leg_item = next(it for it in items if it["record_id"] == leg_id)
    assert leg_item["archive_project_key"] == f"legacy-record:{leg_id}"
    assert leg_item["is_legacy"] is True

    # Can be resolved using the virtual legacy key
    res_resolve = client.post(
        f"/api/historical-resources/{leg_id}/resolve",
        headers=headers,
        json={"archive_project_key": f"legacy-record:{leg_id}", "note": "Legacy record manually verified"}
    )
    assert res_resolve.status_code == 200

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT status, remote_status FROM mutation_ownership_records WHERE id = ?", (leg_id,))
    row = c.fetchone()
    assert row["status"] == "RESOLVED_MANUAL"
    assert row["remote_status"] == "MANUALLY_RESOLVED"
    conn.close()


def test_corrupt_metadata_handling(client):
    headers = {"X-QA-Request": "1"}

    p = _create_project("Project Corrupt Test")
    r = _create_run(p)

    # 7. Corrupt JSON in an active record aborts reset/deletion with 409
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO mutation_ownership_records (
            run_id, project_id, target_url, entity_type, entity_id, entity_name, status, remote_status, details_json
        ) VALUES (?, ?, 'http://127.0.0.1:8080', 'item', 'e1', 'Corrupt Item', 'CREATED', 'CONFIRMED_EXISTS', 'NOT_VALID_JSON{')
        """,
        (r, p),
    )
    corrupt_id = c.lastrowid
    conn.commit()
    conn.close()

    res_reset = client.post("/api/reset", headers=headers)
    assert res_reset.status_code == 409
    assert "Corrupt details_json" in res_reset.get_json()["message"]

    # Active project is NOT deleted (no data loss)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id FROM projects WHERE id = ?", (p,))
    assert c.fetchone() is not None

    # Corrupt historical JSON is visible in history query, but resolve endpoint returns 409
    c.execute(
        """
        INSERT INTO mutation_ownership_records (
            run_id, project_id, target_url, entity_type, entity_id, entity_name, status, remote_status, details_json
        ) VALUES (-1, -1, 'http://127.0.0.1:8080', 'item', 'e2', 'Corrupt Hist', 'CREATED', 'CONFIRMED_EXISTS', 'NOT_VALID_JSON{')
        """
    )
    hist_corrupt_id = c.lastrowid
    conn.commit()
    conn.close()

    res_hist = client.get("/api/historical-resources?state=all")
    items = res_hist.get_json()["historical_resources"]
    hc_item = next(it for it in items if it["record_id"] == hist_corrupt_id)
    assert hc_item["metadata_corrupt"] is True

    # Resolving corrupt historical record rejected with 409
    res_corrupt_resolve = client.post(
        f"/api/historical-resources/{hist_corrupt_id}/resolve",
        headers=headers,
        json={"archive_project_key": f"legacy-record:{hist_corrupt_id}", "note": "Note"}
    )
    assert res_corrupt_resolve.status_code == 409


def test_negative_id_denied_automatic_recovery_and_destructive_authority():
    # 8. Negative/0-IDs cannot be used for automatic recovery or destructive authority
    # Destructive authority check
    assert ScopedMutationRegistry.is_owned_by_run(
        run_id=-1,
        entity_id="eid-1",
        project_id=-1,
        target_url="http://127.0.0.1:8080",
        require_destructive_authority=True
    ) is False

    assert ScopedMutationRegistry.is_owned_by_run(
        run_id=0,
        entity_id="eid-1",
        project_id=0,
        target_url="http://127.0.0.1:8080",
        require_destructive_authority=True
    ) is False

    from app.qa.scoped_mutation import AutonomousProfileWorkflowRunner
    runner = AutonomousProfileWorkflowRunner()
    mock_page = MagicMock()

    import asyncio
    res_neg_proj = asyncio.run(
        runner.reconcile_and_recover_pending_entities(mock_page, project_id=-1, target_url="http://127.0.0.1:8080")
    )
    assert res_neg_proj == []

    res_neg_run = asyncio.run(
        runner.reconcile_and_recover_pending_entities(mock_page, project_id=1, run_id=-5, target_url="http://127.0.0.1:8080")
    )
    assert res_neg_run == []


def test_active_resolve_endpoint_rejects_negative_project_id(client):
    headers = {"X-QA-Request": "1"}

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO mutation_ownership_records (
            run_id, project_id, target_url, entity_type, entity_id, entity_name, status, remote_status, details_json
        ) VALUES (-1, -1, 'http://127.0.0.1:8080', 'item', 'e1', 'Hist Item', 'CREATED', 'CONFIRMED_EXISTS', '{}')
        """
    )
    rec_id = c.lastrowid
    conn.commit()
    conn.close()

    # Active resolve endpoint rejects negative project_id with 400
    res = client.post(
        f"/api/pending-resources/{rec_id}/resolve",
        headers=headers,
        json={"project_id": -1, "note": "Attempt resolve via active endpoint"}
    )
    assert res.status_code == 400


def test_historical_get_pagination_and_query_safety(client):
    # 12. GET on history including pagination and state=all does not mutate rows
    conn = get_db_connection()
    c = conn.cursor()
    for i in range(1, 15):
        c.execute(
            """
            INSERT INTO mutation_ownership_records (
                run_id, project_id, target_url, entity_type, entity_id, entity_name, status, remote_status, details_json
            ) VALUES (-1, -1, 'http://127.0.0.1:8080', 'item', ?, ?, 'CREATED', 'CONFIRMED_EXISTS', '{}')
            """,
            (f"e-{i}", f"Item {i}"),
        )
    conn.commit()
    conn.close()

    res_p1 = client.get("/api/historical-resources?state=pending&limit=5")
    assert res_p1.status_code == 200
    items_p1 = res_p1.get_json()["historical_resources"]
    assert len(items_p1) == 5

    last_id = items_p1[-1]["record_id"]
    res_p2 = client.get(f"/api/historical-resources?state=pending&limit=5&before_id={last_id}")
    assert res_p2.status_code == 200
    items_p2 = res_p2.get_json()["historical_resources"]
    assert len(items_p2) == 5
    assert all(it["record_id"] < last_id for it in items_p2)

    # Invalid state gives 400
    res_bad = client.get("/api/historical-resources?state=invalid_foo")
    assert res_bad.status_code == 400


def test_historical_resources_view_and_disclosures(client):
    # 11. Historical UI works even without active projects and renders disclosures
    res = client.get("/historical-resources")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Historical Resources" in html
    assert "These records do <strong>not</strong> belong to an active QA project workspace." in html
    assert "Automated recovery and automated cleanup do <strong>not</strong> run on historical resources." in html
    assert "Manual resolution records human tester verification in the audit journal; it does <strong>not</strong> prove server-side resource deletion." in html
