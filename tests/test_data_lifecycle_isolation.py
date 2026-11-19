"""Tests for data lifecycle isolation:
1. Project deletion cascades validation_contracts deletion and in-memory unregistration.
2. Project deletion decouples mutation_ownership_records into negative historical IDs,
   preserving external resource traceability while preventing pollution of reused project IDs.
3. Data reset (/api/reset) completely clears validation_contracts and decouples all active
   mutation_ownership_records, guaranteeing that a newly created project with reused ID 1 has zero lingering records.
"""
import json
import sqlite3
import pytest
from server import app
from app.models.database import get_db_connection, init_db
from app.qa.scoped_mutation import ScopedMutationRegistry
import app.qa.validation_contract as vc
from app.qa.validation_contract import (
    register_validation_contract,
    ValidationContract,
    ValidationTestCase,
)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.app_context():
        init_db()
    with app.test_client() as client:
        client.environ_base["HTTP_X_QA_REQUEST"] = "1"
        yield client


class TestDataLifecycleIsolation:

    def test_delete_project_cleans_contracts_and_decouples_mutation_records(self, client):
        # 1. Create a project
        resp = client.post("/api/projects", json={
            "name": "Lifecycle Test Project",
            "target_url": "http://127.0.0.1:9999",
            "environment": "staging",
            "tester_name": "QA Tester",
        })
        assert resp.status_code == 200
        p_id = resp.get_json()["id"]

        # 2. Add a validation contract
        contract_cfg = {
            "field_selectors": {"#user": "input[name='user']"},
            "test_cases": [{"case_id": "c1", "description": "case 1", "input_values": {"#user": "val"}, "expected_valid": True}],
        }
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            INSERT INTO validation_contracts (project_id, contract_id, rule_name, target_url_pattern, config_json)
            VALUES (?, ?, ?, ?, ?)
        """, (p_id, "rule-lifecycle-1", "Lifecycle Rule", "*", json.dumps(contract_cfg)))
        c_id = c.lastrowid

        # Also register in memory
        vcontract = ValidationContract(
            contract_id="rule-lifecycle-1",
            rule_name="Lifecycle Rule",
            test_cases=(ValidationTestCase(case_id="c1", description="case 1", input_values={"#user": "val"}, expected_valid=True),),
            field_selectors={"#user": "input[name='user']"},
        )
        register_validation_contract(vcontract, target_url_pattern="*", project_id=p_id)

        # 3. Add a run and a mutation ownership record
        c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (p_id,))
        r_id = c.lastrowid
        c.execute("""
            INSERT INTO mutation_ownership_records (
                run_id, project_id, target_url, entity_type, intent_id, run_prefix,
                entity_id, remote_entity_id, entity_name, status, remote_status,
                local_cleaned, remote_cleaned, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
        """, (
            r_id, p_id, "http://127.0.0.1:9999/profile", "profile_attachment", "intent-99", "[QA-99]",
            "att-local-99", "att-remote-99", "audit-test-file.txt", "CREATED", "CONFIRMED_EXISTS",
            json.dumps({"filename": "audit-test-file.txt", "remote_action": "created"}),
        ))
        m_id = c.lastrowid
        conn.commit()
        conn.close()

        # Verify contracts and pending records exist before delete
        assert len(ScopedMutationRegistry.get_pending_records_for_display(project_id=p_id)) == 1
        assert any(b.contract.contract_id == "rule-lifecycle-1" and b.project_id == p_id for b in vc._REGISTERED_BINDINGS)

        # 4. Delete the project
        del_resp = client.post(f"/api/projects/{p_id}/delete")
        assert del_resp.status_code == 200

        # 5. Verify validation contracts are completely deleted from DB and memory
        conn = get_db_connection()
        c = conn.cursor()
        rem_contracts = c.execute("SELECT * FROM validation_contracts WHERE project_id = ?", (p_id,)).fetchall()
        assert len(rem_contracts) == 0
        assert not any(b.contract.contract_id == "rule-lifecycle-1" and b.project_id == p_id for b in vc._REGISTERED_BINDINGS)

        # 6. Verify mutation ownership record is PRESERVED for auditability but DECOUPLED
        m_row = c.execute("SELECT * FROM mutation_ownership_records WHERE id = ?", (m_id,)).fetchone()
        assert m_row is not None, "Audit record must be preserved"
        assert m_row["project_id"] == -abs(p_id), "Project ID must be negated"
        assert m_row["run_id"] == -abs(r_id), "Run ID must be negated"

        # Check historical detachment metadata
        details = json.loads(m_row["details_json"])
        detachment = details.get("historical_detachment")
        assert detachment is not None
        assert detachment["detached_reason"] == "project_deleted"
        assert detachment["original_project_id"] == p_id
        assert detachment["original_run_id"] == r_id
        assert "Lifecycle Test Project" in detachment["original_project_name"]
        assert "detached_at" in detachment

        # 7. Verify pending records display returns 0 for old project ID and for global queries
        conn.close()
        assert len(ScopedMutationRegistry.get_pending_records_for_display(project_id=p_id)) == 0

    def test_reset_data_cleans_contracts_and_decouples_all_mutation_records(self, client):
        # 1. Create a project and add contracts and mutation records
        resp = client.post("/api/projects", json={
            "name": "Reset Test Project",
            "target_url": "http://127.0.0.1:8888",
            "environment": "staging",
            "tester_name": "QA Tester",
        })
        p_id = resp.get_json()["id"]

        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            INSERT INTO validation_contracts (project_id, contract_id, rule_name, config_json)
            VALUES (?, 'rule-reset-1', 'Reset Rule', '{}')
        """, (p_id,))
        c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (p_id,))
        r_id = c.lastrowid
        c.execute("""
            INSERT INTO mutation_ownership_records (
                run_id, project_id, target_url, entity_type, intent_id, run_prefix,
                entity_id, remote_entity_id, entity_name, status, remote_status,
                local_cleaned, remote_cleaned, details_json
            ) VALUES (?, ?, ?, 'profile_attachment', 'i-1', '[P]', 'e-1', 'rem-1', 'audit.txt', 'CREATED', 'CONFIRMED', 0, 0, '{}')
        """, (r_id, p_id, "http://127.0.0.1:8888"))
        m_id = c.lastrowid
        conn.commit()
        conn.close()

        # 2. Call reset_data
        reset_resp = client.post("/api/reset")
        assert reset_resp.status_code == 200

        # 3. Verify all operational tables are empty and sequence reset
        conn = get_db_connection()
        c = conn.cursor()
        for tbl in ["projects", "runs", "tests", "findings", "crawl_runs", "crawl_pages", "validation_contracts"]:
            count = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            assert count == 0, f"Table {tbl} should be empty after reset"

        # Check sqlite_sequence for validation_contracts
        seq = c.execute("SELECT seq FROM sqlite_sequence WHERE name = 'validation_contracts'").fetchone()
        assert seq is None, "sqlite_sequence for validation_contracts should be cleared"

        # 4. Verify mutation record is preserved with negative IDs and reset detachment
        m_row = c.execute("SELECT * FROM mutation_ownership_records WHERE id = ?", (m_id,)).fetchone()
        assert m_row is not None
        assert m_row["project_id"] == -abs(p_id)
        assert m_row["run_id"] == -abs(r_id)
        details = json.loads(m_row["details_json"])
        assert details["historical_detachment"]["detached_reason"] == "data_reset"
        assert details["historical_detachment"]["original_project_id"] == p_id

        # 5. Create a brand new project after reset (which will receive ID 1 due to sequence reset)
        new_resp = client.post("/api/projects", json={
            "name": "New Project After Reset",
            "target_url": "http://127.0.0.1:7777",
            "environment": "dev",
            "tester_name": "Tester",
        })
        new_p_id = new_resp.get_json()["id"]
        assert new_p_id == 1, "After reset and sequence clear, first project ID should be 1"

        # 6. Verify zero lingering contracts and zero pending resources for project ID 1!
        contracts = c.execute("SELECT * FROM validation_contracts WHERE project_id = ?", (new_p_id,)).fetchall()
        assert len(contracts) == 0, "New project must have 0 validation contracts"

        pending = ScopedMutationRegistry.get_pending_records_for_display(project_id=new_p_id)
        assert len(pending) == 0, "New project 1 must NOT inherit any detached mutation records"

        all_pending = ScopedMutationRegistry.get_pending_records_for_display()
        assert len(all_pending) == 0, "Zero pending records across all active projects"

        conn.close()
