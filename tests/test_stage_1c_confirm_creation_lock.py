import json
import sqlite3
import threading
import time
import pytest

from app.models.database import get_db_connection, init_db
from app.qa.scoped_mutation import ScopedEntityOwnershipRegistry

class TracingCursor:
    def __init__(self, cursor, trace_callback=None, pre_exec_hook=None):
        self._cursor = cursor
        self._trace_callback = trace_callback
        self._pre_exec_hook = pre_exec_hook

    def execute(self, sql, *args):
        if self._trace_callback:
            self._trace_callback(sql)
        if self._pre_exec_hook:
            self._pre_exec_hook(sql)
        return self._cursor.execute(sql, *args)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    def __iter__(self):
        return iter(self._cursor)


class TracingConnection:
    def __init__(self, conn, trace_callback=None, pre_exec_hook=None):
        self._conn = conn
        self._trace_callback = trace_callback
        self._pre_exec_hook = pre_exec_hook

    def execute(self, sql, *args):
        if self._trace_callback:
            self._trace_callback(sql)
        if self._pre_exec_hook:
            self._pre_exec_hook(sql)
        return self._conn.execute(sql, *args)

    def cursor(self):
        return TracingCursor(self._conn.cursor(), self._trace_callback, self._pre_exec_hook)

    def commit(self):
        if self._trace_callback:
            self._trace_callback("COMMIT")
        return self._conn.commit()

    def rollback(self):
        if self._trace_callback:
            self._trace_callback("ROLLBACK")
        return self._conn.rollback()

    def close(self):
        return self._conn.close()


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "lock_test.sqlite")
    monkeypatch.setenv("QA_TESTER_DB_PATH", db_file)
    from app.paths import ensure_dirs
    ensure_dirs()
    init_db()
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO mutation_ownership_records
        (run_id, project_id, target_url, account_context, entity_type, intent_id, run_prefix,
         entity_id, remote_entity_id, entity_name, status, remote_status, local_cleaned, remote_cleaned,
         details_json)
        VALUES (1, 1, 'http://127.0.0.1:8000/profile', 'tester@account', 'profile_experience', 'intent-1', '[QA-1]',
         'intent-1', NULL, 'Test Entity', 'INTENT_RECORDED', 'PENDING_CONFIRMATION', 0, 0,
         '{"original_intent_meta":"persisted", "proof":"valid-proof"}')
        """
    )
    rec_id = c.lastrowid
    conn.commit()
    conn.close()
    return rec_id, db_file


def test_sqlite_trace_proves_begin_immediate_precedes_select_and_update(test_db):
    rec_id, db_file = test_db
    traced_statements = []
    
    def trace(sql):
        traced_statements.append(sql.strip())

    import app.qa.scoped_mutation as sm
    orig_fn = sm.get_db_connection
    sm.get_db_connection = lambda: TracingConnection(get_db_connection(), trace_callback=trace)
    try:
        ScopedEntityOwnershipRegistry.confirm_creation(
            record_id=rec_id,
            remote_entity_id="remote-555",
            confirmed_name="Test Entity Confirmed",
            details={"extra_info": 42}
        )
    finally:
        sm.get_db_connection = orig_fn

    # Verify statement ordering:
    assert "BEGIN IMMEDIATE" in traced_statements, f"BEGIN IMMEDIATE missing: {traced_statements}"
    idx_begin = traced_statements.index("BEGIN IMMEDIATE")
    select_stmts = [i for i, s in enumerate(traced_statements) if s.startswith("SELECT details_json")]
    assert select_stmts, "SELECT details_json missing"
    assert idx_begin < select_stmts[0], "BEGIN IMMEDIATE must precede SELECT"
    update_stmts = [i for i, s in enumerate(traced_statements) if s.startswith("UPDATE mutation_ownership_records")]
    assert update_stmts, "UPDATE missing"
    assert select_stmts[0] < update_stmts[0], "SELECT must precede UPDATE"
    assert "COMMIT" in traced_statements


def test_concurrent_writer_blocked_during_confirm_creation(test_db):
    rec_id, db_file = test_db
    
    first_read_event = threading.Event()
    continue_confirm_event = threading.Event()
    concurrent_write_result = {}

    def pausing_hook(sql):
        if sql.strip().startswith("SELECT details_json"):
            first_read_event.set()
            continue_confirm_event.wait(timeout=5.0)

    def run_confirm():
        import app.qa.scoped_mutation as sm
        orig_fn = sm.get_db_connection
        sm.get_db_connection = lambda: TracingConnection(get_db_connection(), pre_exec_hook=pausing_hook)
        try:
            ScopedEntityOwnershipRegistry.confirm_creation(
                record_id=rec_id,
                remote_entity_id="remote-lock-test",
                confirmed_name="Test Entity",
                details={"step": "first"}
            )
        finally:
            sm.get_db_connection = orig_fn

    def run_concurrent_writer():
        first_read_event.wait(timeout=5.0)
        # Attempt to write to the database while confirm_creation holds the lock
        conn2 = sqlite3.connect(db_file, timeout=0.1)
        try:
            conn2.execute("BEGIN IMMEDIATE")
            conn2.execute("UPDATE mutation_ownership_records SET status = 'MUTATED' WHERE id = ?", (rec_id,))
            conn2.commit()
            concurrent_write_result["status"] = "SUCCESS_WRITTEN"
        except sqlite3.OperationalError as e:
            concurrent_write_result["status"] = "BLOCKED"
            concurrent_write_result["error"] = str(e)
        finally:
            conn2.close()
            continue_confirm_event.set()

    t_confirm = threading.Thread(target=run_confirm)
    t_writer = threading.Thread(target=run_concurrent_writer)
    
    t_confirm.start()
    t_writer.start()
    
    t_confirm.join(timeout=5.0)
    t_writer.join(timeout=5.0)
    
    assert concurrent_write_result.get("status") == "BLOCKED"
    assert "locked" in concurrent_write_result.get("error", "").lower() or "busy" in concurrent_write_result.get("error", "").lower()


def test_metadata_preservation_and_rollback_on_failure(test_db):
    rec_id, db_file = test_db
    
    def failing_hook(sql):
        if sql.strip().startswith("UPDATE"):
            raise RuntimeError("Injected database failure before commit")

    import app.qa.scoped_mutation as sm
    orig_fn = sm.get_db_connection
    sm.get_db_connection = lambda: TracingConnection(get_db_connection(), pre_exec_hook=failing_hook)
    try:
        with pytest.raises(RuntimeError, match="Injected database failure"):
            ScopedEntityOwnershipRegistry.confirm_creation(
                record_id=rec_id,
                remote_entity_id="remote-should-fail",
                confirmed_name="Test Entity",
            )
    finally:
        sm.get_db_connection = orig_fn

    # Verify original state and metadata are completely untouched
    conn = get_db_connection()
    row = conn.execute("SELECT status, remote_status, remote_entity_id, details_json FROM mutation_ownership_records WHERE id = ?", (rec_id,)).fetchone()
    conn.close()
    
    assert row["status"] == "INTENT_RECORDED"
    assert row["remote_status"] == "PENDING_CONFIRMATION"
    assert row["remote_entity_id"] is None
    details = json.loads(row["details_json"])
    assert details["original_intent_meta"] == "persisted"
    assert details["proof"] == "valid-proof"
    assert "confirmed_at" not in details
