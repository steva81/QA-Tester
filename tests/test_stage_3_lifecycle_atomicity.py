import pytest
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from server import app
from app.models.database import init_db, get_db_connection
from app.data_lifecycle import _mutation_lock
import app.routes.api as api
from app.qa.interactive import sessions, session_lock, start_session, stop_session, InteractiveSession
import app.qa.validation_contract as vc
from app.qa.validation_contract import (
    ValidationContract,
    ValidationContractBinding,
    register_validation_contract,
    clear_validation_contracts,
)


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    """Ensure every test runs with an isolated DB, writable dir, and clean memory state."""
    db_path = str(tmp_path / "test_lifecycle.db")
    monkeypatch.setenv("QA_TESTER_DB_PATH", db_path)
    monkeypatch.setenv("QA_TESTER_WRITABLE_DIR", str(tmp_path))

    with app.app_context():
        init_db()

    clear_validation_contracts()
    with session_lock:
        sessions.clear()
    with api.worker_lock:
        api.worker_records.clear()
        api.running_engines.clear()
        api.active_worker_threads.clear()
        api.accepting_runs = True

    yield

    with session_lock:
        sessions.clear()
    with api.worker_lock:
        api.worker_records.clear()
        api.running_engines.clear()
        api.active_worker_threads.clear()
        api.accepting_runs = True
    clear_validation_contracts()


@pytest.fixture
def client():
    app.config["TESTING"] = True
    return app.test_client()


def _create_test_project(name="Test Project", url="http://127.0.0.1:8080"):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES (?, ?, 'Staging', 'QA Tester')", (name, url))
    pid = c.lastrowid
    conn.commit()
    conn.close()
    return pid


def test_staging_same_basename_restores_exact_bytes(tmp_path):
    a, b = tmp_path / 'screenshots' / 'same.png', tmp_path / 'exports' / 'same.png'
    for path, content in ((a, b'first-original'), (b, b'second-original')):
        path.parent.mkdir()
        path.write_bytes(content)
    staged, directory = api.staged_delete([a, b])
    try:
        api.rollback_staged_delete(staged, directory)
        assert a.read_bytes() == b'first-original'
        assert b.read_bytes() == b'second-original'
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_staging_duplicate_normalized_path_rejected_before_move(tmp_path):
    path = tmp_path / 'original.png'
    path.write_bytes(b'original')
    with pytest.raises(ValueError, match='Duplicate'):
        api.staged_delete([path, path.parent / '.' / path.name])
    assert path.read_bytes() == b'original'


def test_staging_canonical_identity_does_not_replace_move_source(tmp_path, monkeypatch):
    entry, target = tmp_path / 'entry.png', tmp_path / 'target.png'
    entry.write_bytes(b'entry'); target.write_bytes(b'target')
    resolve = Path.resolve
    monkeypatch.setattr(Path, 'resolve', lambda p, *a, **kw: target if p == entry else resolve(p, *a, **kw))
    staged, directory = api.staged_delete([entry])
    assert not entry.exists() and target.read_bytes() == b'target'
    api.rollback_staged_delete(staged, directory)
    assert entry.read_bytes() == b'entry' and target.read_bytes() == b'target'


def test_staging_later_move_failure_restores_prior_files(tmp_path, monkeypatch):
    a, b = tmp_path / 'a.png', tmp_path / 'b.png'
    a.write_bytes(b'A'); b.write_bytes(b'B')
    move = shutil.move
    def fail_second(src, dst):
        if Path(src) == b:
            raise OSError('injected second move failure')
        return move(src, dst)
    monkeypatch.setattr(shutil, 'move', fail_second)
    with pytest.raises(OSError, match='second move'):
        api.staged_delete([a, b])
    assert a.read_bytes() == b'A' and b.read_bytes() == b'B'


def test_failed_restore_preserves_last_copy_and_restores_other_files(tmp_path, monkeypatch):
    a, b = tmp_path / 'a.png', tmp_path / 'b.png'
    a.write_bytes(b'A'); b.write_bytes(b'B')
    staged, directory = api.staged_delete([a, b])
    move = shutil.move
    def fail_a(src, dst):
        if Path(dst) == a:
            raise OSError('injected restore failure')
        return move(src, dst)
    monkeypatch.setattr(shutil, 'move', fail_a)
    try:
        with pytest.raises(RuntimeError) as exc:
            api.rollback_staged_delete(staged, directory)
        assert str(a) in str(exc.value) and directory in str(exc.value)
        assert staged[0][1].read_bytes() == b'A'
        assert b.read_bytes() == b'B'
        assert Path(directory).exists()
    finally:
        monkeypatch.setattr(shutil, 'move', move)
        api.rollback_staged_delete(staged, directory)


@pytest.mark.parametrize('operation', ['reset', 'delete'])
def test_http_rollback_same_names_preserves_entire_state(operation, tmp_path, monkeypatch, client):
    import app.paths as paths
    monkeypatch.setattr(paths, 'get_writable_dir', lambda: str(tmp_path))
    pid = _create_test_project()
    conn = get_db_connection()
    rid = conn.execute("INSERT INTO runs(project_id,status) VALUES(?,'COMPLETED')", (pid,)).lastrowid
    sd, ed = Path(paths.get_screenshots_dir()), Path(paths.get_exports_dir())
    sd.mkdir(exist_ok=True); ed.mkdir(exist_ok=True)
    a, b = sd / f'report_{rid}_same.png', ed / f'report_{rid}_same.png'
    a.write_bytes(b'screenshot'); b.write_bytes(b'export')
    conn.execute("INSERT INTO tests(run_id,url,status,screenshot_path) VALUES(?,'http://127.0.0.1','PASS',?)", (rid, str(a)))
    conn.execute("INSERT INTO mutation_ownership_records(run_id,project_id,target_url,entity_type,entity_id,entity_name,details_json) VALUES(?,?,'http://127.0.0.1','item','x','x','{}')", (rid, pid))
    conn.execute("INSERT INTO validation_contracts(project_id,contract_id,rule_name) VALUES(?,'keep','Keep')", (pid,))
    conn.commit()
    before = '\n'.join(conn.iterdump()); conn.close()
    bindings = list(vc._REGISTERED_BINDINGS)
    connect = api.get_db_connection
    class FailCommit:
        def __init__(self): self.real = connect()
        def cursor(self): return self.real.cursor()
        def commit(self): raise RuntimeError('injected before commit')
        def rollback(self): return self.real.rollback()
        def close(self): return self.real.close()
    monkeypatch.setattr(api, 'get_db_connection', FailCommit)
    response = client.post('/api/reset' if operation == 'reset' else f'/api/projects/{pid}/delete', headers={'X-QA-Request': '1'})
    assert response.status_code == 500
    assert a.read_bytes() == b'screenshot' and b.read_bytes() == b'export'
    conn = connect()
    assert '\n'.join(conn.iterdump()) == before
    conn.close()
    assert vc._REGISTERED_BINDINGS == bindings


def test_reset_and_delete_denied_for_active_starting_stopping_session(client):
    pid = _create_test_project()
    headers = {"X-QA-Request": "1"}

    # 1. Active running session
    active_s = MagicMock()
    active_s.is_running = True
    active_s.state = "READY"
    active_s.thread = None
    with session_lock:
        sessions[str(pid)] = active_s

    res = client.post("/api/reset", headers=headers)
    assert res.status_code == 409
    assert "interactive session is currently active" in res.get_json()["message"]

    res_del = client.post(f"/api/projects/{pid}/delete", headers=headers)
    assert res_del.status_code == 409

    # 2. Starting session
    with session_lock:
        active_s.is_running = False
        active_s.state = "STARTING"
        active_s.thread = None

    res = client.post("/api/reset", headers=headers)
    assert res.status_code == 409

    # 3. Stopping session with thread alive
    alive_t = MagicMock()
    alive_t.is_alive.return_value = True
    with session_lock:
        active_s.is_running = False
        active_s.state = "STOPPING"
        active_s.thread = alive_t

    res = client.post("/api/reset", headers=headers)
    assert res.status_code == 409


def test_reset_and_delete_denied_for_queued_worker_without_engine(client):
    pid = _create_test_project()
    headers = {"X-QA-Request": "1"}

    # Worker record exists before engine is spawned
    with api.worker_lock:
        api.worker_records[999] = MagicMock(engine=None, thread=None)

    res = client.post("/api/reset", headers=headers)
    assert res.status_code == 409
    assert "Background QA runs or workers are currently active" in res.get_json()["message"]

    res_del = client.post(f"/api/projects/{pid}/delete", headers=headers)
    assert res_del.status_code == 409


def test_shutdown_denies_requests_with_503(client):
    pid = _create_test_project()
    headers = {"X-QA-Request": "1"}

    with api.worker_lock:
        api.accepting_runs = False

    res = client.post("/api/reset", headers=headers)
    assert res.status_code == 503
    assert "shutting down" in res.get_json()["message"]

    res_del = client.post(f"/api/projects/{pid}/delete", headers=headers)
    assert res_del.status_code == 503


def test_concurrent_reset_and_project_operation_serialization():
    pid = _create_test_project()
    headers = {"X-QA-Request": "1"}

    results = []
    barrier = threading.Barrier(2)

    def do_reset():
        c1 = app.test_client()
        barrier.wait()
        res = c1.post("/api/reset", headers=headers)
        results.append(("reset", res.status_code))

    def do_delete():
        c2 = app.test_client()
        barrier.wait()
        # Small sleep so reset enters request lock first
        time.sleep(0.01)
        res = c2.post(f"/api/projects/{pid}/delete", headers=headers)
        results.append(("delete", res.status_code))

    t1 = threading.Thread(target=do_reset)
    t2 = threading.Thread(target=do_delete)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Reset succeeds (200), subsequent delete finds project gone (404)
    codes = dict(results)
    assert codes["reset"] == 200
    assert codes["delete"] == 404


def test_stop_timeout_leaves_session_visible_and_blocks_reset_and_double_start(client):
    pid = _create_test_project()
    headers = {"X-QA-Request": "1"}

    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = True

    s = InteractiveSession(pid, "http://127.0.0.1:8080")
    s.thread = mock_thread
    s.is_running = True
    s.state = "READY"

    with session_lock:
        sessions[str(pid)] = s

    # Calling stop_session when thread refuses to terminate
    stopped = stop_session(pid)
    assert stopped is False
    assert s.state == "STOPPING"
    assert str(pid) in sessions

    # HTTP stop endpoint returns 409
    res_stop = client.post(f"/api/projects/{pid}/interactive/stop", headers=headers)
    assert res_stop.status_code == 409

    # Double start while stopping session thread is alive is rejected
    with pytest.raises(RuntimeError, match="Previous interactive session is still stopping"):
        start_session(pid, "http://127.0.0.1:8080")

    # Reset is blocked by stopping session with alive thread
    res_reset = client.post("/api/reset", headers=headers)
    assert res_reset.status_code == 409


def test_stopped_dead_session_cleans_up_idempotently(client):
    pid = _create_test_project()
    headers = {"X-QA-Request": "1"}

    dead_thread = MagicMock()
    dead_thread.is_alive.return_value = False

    s = InteractiveSession(pid, "http://127.0.0.1:8080")
    s.thread = dead_thread
    s.is_running = False
    s.state = "STOPPING"

    with session_lock:
        sessions[str(pid)] = s

    # stop_session cleans up dead thread
    stopped = stop_session(pid)
    assert stopped is True
    assert str(pid) not in sessions

    # Idempotent second call
    assert stop_session(pid) is True

    # Reset is now allowed
    res = client.post("/api/reset", headers=headers)
    assert res.status_code == 200


def test_exception_in_mutating_route_releases_mutation_lock(client):
    headers = {"X-QA-Request": "1"}

    # Mutating route with bad payload or not found
    res = client.post("/api/projects/99999/delete", headers=headers)
    assert res.status_code == 404

    # Verify mutation lock was released
    acquired = _mutation_lock.acquire(blocking=False)
    assert acquired is True
    _mutation_lock.release()

    # Next mutating request functions normally
    pid = _create_test_project()
    res2 = client.post(f"/api/projects/{pid}/delete", headers=headers)
    assert res2.status_code == 200


def test_rollback_before_commit_preserves_db_contracts_and_files(tmp_path, client, monkeypatch):
    pid = _create_test_project()
    headers = {"X-QA-Request": "1"}

    # Setup contract
    contract = ValidationContract(
        contract_id="test_contract_rb",
        rule_name="Test Contract RB",
        target_url_pattern="http://127.0.0.1:8080/*",
        field_selectors={"name": "#name"},
        test_cases=(),
    )
    register_validation_contract(contract, project_id=pid)
    assert any(b.contract.contract_id == "test_contract_rb" for b in vc._REGISTERED_BINDINGS)

    # Insert contract and test record in DB
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO validation_contracts (contract_id, rule_name, project_id, target_url_pattern, config_json) VALUES (?, ?, ?, ?, ?)",
        ("test_contract_rb", "Test Contract RB", pid, "http://127.0.0.1:8080/*", "{}")
    )
    c.execute(
        "INSERT INTO mutation_ownership_records (run_id, project_id, target_url, entity_type, entity_id, entity_name, details_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (10, pid, "http://127.0.0.1:8080", "item", "id-1", "name-1", "{}")
    )
    conn.commit()
    conn.close()

    # Setup a screenshot file
    from app.paths import get_screenshots_dir
    sd = Path(get_screenshots_dir())
    sd.mkdir(parents=True, exist_ok=True)
    shot_file = sd / f"proj{pid}_test.png"
    shot_file.write_text("dummy png content")

    # Monkeypatch execute on connection to fail before commit
    orig_connect = api.get_db_connection

    class FailingConnection:
        def __init__(self, real_conn):
            self._real = real_conn

        def cursor(self):
            return FailingCursor(self._real.cursor())

        def commit(self):
            raise RuntimeError("Simulated DB failure right before commit")

        def rollback(self):
            return self._real.rollback()

        def close(self):
            return self._real.close()

    class FailingCursor:
        def __init__(self, real_cur):
            self._real = real_cur

        def execute(self, sql, params=()):
            return self._real.execute(sql, params)

        def fetchone(self):
            return self._real.fetchone()

        def fetchall(self):
            return self._real.fetchall()

    monkeypatch.setattr(api, "get_db_connection", lambda: FailingConnection(orig_connect()))

    res = client.post(f"/api/projects/{pid}/delete", headers=headers)
    assert res.status_code == 500
    assert "Simulated DB failure right before commit" in res.get_json()["message"]

    # Verify DB preserved
    conn = orig_connect()
    c = conn.cursor()
    c.execute("SELECT id FROM projects WHERE id = ?", (pid,))
    assert c.fetchone() is not None
    c.execute("SELECT id FROM validation_contracts WHERE project_id = ?", (pid,))
    assert c.fetchone() is not None
    c.execute("SELECT project_id FROM mutation_ownership_records WHERE id = 1")
    row = c.fetchone()
    assert row["project_id"] == pid  # Not decoupled (-pid)
    conn.close()

    # Verify bindings in memory preserved
    assert any(b.contract.contract_id == "test_contract_rb" for b in vc._REGISTERED_BINDINGS)

    # Verify screenshot file restored to original path
    assert shot_file.exists()
    assert shot_file.read_text() == "dummy png content"


def test_post_commit_staging_cleanup_error_reports_honest_failure_no_false_restore(tmp_path, client, monkeypatch):
    pid = _create_test_project()
    headers = {"X-QA-Request": "1"}

    # Setup screenshot file
    from app.paths import get_screenshots_dir
    sd = Path(get_screenshots_dir())
    sd.mkdir(parents=True, exist_ok=True)
    shot_file = sd / f"proj{pid}_test.png"
    shot_file.write_text("dummy png content")

    # Monkeypatch commit_staged_delete to simulate cleanup failure
    def mock_commit_staged_delete(temp_dir):
        raise RuntimeError("Permission denied removing staging directory")

    monkeypatch.setattr(api, "commit_staged_delete", mock_commit_staged_delete)

    res = client.post(f"/api/projects/{pid}/delete", headers=headers)
    assert res.status_code == 500
    data = res.get_json()
    assert "staging cleanup failed" in data["message"]
    assert "staged_dir" in data

    # Verify DB IS committed (project is deleted)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id FROM projects WHERE id = ?", (pid,))
    assert c.fetchone() is None
    conn.close()

    # File was moved to staging, NOT restored to original location
    assert not shot_file.exists()


def test_project_delete_only_removes_target_project_contracts(client):
    headers = {"X-QA-Request": "1"}

    # Create two projects
    p1 = _create_test_project("Project 1")
    p2 = _create_test_project("Project 2")

    c1 = ValidationContract(
        contract_id="shared_form",
        rule_name="Shared Form 1",
        target_url_pattern="http://127.0.0.1:8080/p1",
        field_selectors={"name": "#name"},
        test_cases=(),
    )
    c2 = ValidationContract(
        contract_id="shared_form",
        rule_name="Shared Form 2",
        target_url_pattern="http://127.0.0.1:8080/p2",
        field_selectors={"name": "#name"},
        test_cases=(),
    )
    c3 = ValidationContract(
        contract_id="global_form",
        rule_name="Global Form",
        target_url_pattern="*",
        field_selectors={"name": "#name"},
        test_cases=(),
    )

    register_validation_contract(c1, project_id=p1)
    register_validation_contract(c2, project_id=p2)
    register_validation_contract(c3, project_id=None)

    # Insert into DB
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO validation_contracts (contract_id, rule_name, project_id, target_url_pattern, config_json) VALUES (?, ?, ?, ?, ?)",
        ("shared_form", "Shared Form 1", p1, "http://127.0.0.1:8080/p1", "{}")
    )
    c.execute(
        "INSERT INTO validation_contracts (contract_id, rule_name, project_id, target_url_pattern, config_json) VALUES (?, ?, ?, ?, ?)",
        ("shared_form", "Shared Form 2", p2, "http://127.0.0.1:8080/p2", "{}")
    )
    conn.commit()
    conn.close()

    # Delete project 1
    res = client.post(f"/api/projects/{p1}/delete", headers=headers)
    assert res.status_code == 200

    # In DB: p1 contract is gone, p2 contract remains
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT contract_id, project_id FROM validation_contracts")
    db_contracts = c.fetchall()
    assert len(db_contracts) == 1
    assert db_contracts[0]["project_id"] == p2
    conn.close()

    # In memory: p1 binding is gone, p2 and global bindings remain!
    remaining_pids = [b.project_id for b in vc._REGISTERED_BINDINGS]
    assert p1 not in remaining_pids
    assert p2 in remaining_pids
    assert None in remaining_pids
