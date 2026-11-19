"""Source application acceptance; never launches a packaged executable or external target."""
import copy
import json
import os
from pathlib import Path
import socket
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager

import pytest
from tests.profile_test_site import ProfileTestServer

ROOT = Path(__file__).resolve().parents[1]


def request(base, path, data=None):
    headers = {
        "Content-Type": "application/json",
        "X-Shutdown-Token": "source-local-test",
        "X-QA-Request": "1"
    }
    req = urllib.request.Request(base + path, data=None if data is None else json.dumps(data).encode(),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=15) as response:
        body = response.read()
        return json.loads(body) if response.headers.get_content_type() == "application/json" else body


def wait_for(fn, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(.25)
    raise AssertionError("Timed out waiting for source application")


@contextmanager
def source_app(storage):
    storage.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env.pop("QA_TESTER_DB_PATH", None)
    env.pop("QA_TESTER_TEST_DIR", None)
    env.update(QA_TESTER_WRITABLE_DIR=str(storage), QA_TESTER_SERVER_ONLY="1",
               QA_TESTER_SHUTDOWN_TOKEN="source-local-test")
    base = f"http://127.0.0.1:{port}"
    with (storage / f"application-{port}.log").open("w", encoding="utf-8") as log:
        proc = subprocess.Popen([sys.executable, str(ROOT / "desktop.py"), "--port", str(port)],
                                cwd=storage, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            def ready():
                assert proc.poll() is None, "Source app exited before startup"
                try:
                    return request(base, "/")
                except OSError:
                    return False
            wait_for(ready, 30)
            yield base
        finally:
            if proc.poll() is None:
                try:
                    request(base, "/api/system/shutdown", {})
                    proc.wait(timeout=30)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=10)


def start_run(base, project):
    request(base, f"/api/projects/{project}/interactive/start", {})
    wait_for(lambda: request(base, f"/api/projects/{project}/interactive/state").get("state") == "READY", 30)
    run = request(base, "/api/runs", {"project_id": project, "mode": "current",
                                      "enable_authorized_mutations": True})["run_id"]
    terminal = wait_for(lambda: (s if (s := request(base, f"/api/runs/{run}/status")).get("status")
                                in ("COMPLETED", "ABORTED", "FAILED") else None))
    assert terminal["status"] == "COMPLETED", terminal
    return run


def rows(storage, table, run):
    with sqlite3.connect(storage / "data" / "qa-tester.db") as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE run_id=?", (run,))]


@pytest.mark.parametrize("uncertain", [False, True])
def test_source_entry_lifecycle_and_cold_recovery(tmp_path, uncertain):
    storage = tmp_path / "source-app"
    server = ProfileTestServer("127.0.0.1", 0)
    server.attachments["att-preexisting"] = {"id": "att-preexisting", "filename": "existing-resume.txt", "content": b"keep"}
    original = copy.deepcopy(server.experiences)
    attachments = copy.deepcopy(server.attachments)
    if uncertain:
        server.fail_readback_after_reloads = 1
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    evidence = {}
    try:
        with source_app(storage) as base:
            project = request(base, "/api/projects", {"name": "Local source lifecycle", "target_url": server.origin + "/profile?mutation=true",
                                                      "environment": "localhost", "tester_name": "Local test"})["id"]
            run = start_run(base, project)
            journal = rows(storage, "mutation_ownership_records", run)
            results = rows(storage, "tests", run)
            evidence.update(run=run, journal_before_restart=journal, results=results,
                            operations_before_restart=copy.deepcopy(server.operations_log))
            creates = [op for op in server.operations_log if op["op"] == "CREATE"]
            assert creates, evidence
            if uncertain:
                pending = [r for r in journal if not r["remote_cleaned"]]
                assert pending and all(r["remote_status"] == "AMBIGUOUS" for r in pending), journal
                assert len(creates) == 1, "Uncertain cleanup must block subsequent viewport creation"
                assert not [op for op in server.operations_log if op["op"] in ("EDIT", "UPLOAD")]
                assert any(r["status"] == "LIMITED COVERAGE" for r in results)
            else:
                for created in creates:
                    edits = [op for op in server.operations_log if op["op"] == "EDIT" and op["id"] == created["id"]]
                    assert edits and edits[0]["data"]["company"] != created["data"]["company"]
                    assert any(op["op"] == "DELETE" and op["id"] == created["id"] for op in server.operations_log)
                assert not any(op["op"] == "UPLOAD" for op in server.operations_log), "Opaque upload remains unsupported"
                assert any("blocked by network barrier" in (r["actual_result"] or "") and r["status"] == "LIMITED COVERAGE" for r in results)
                assert server.experiences == original
                assert server.attachments == attachments
                assert all(r['remote_status'] == 'CONFIRMED_DELETED' and r['remote_cleaned']
                           for r in journal if r['entity_type'] == 'profile_experience')
                assert all(r['status'] == 'NOT_DISPATCHED' and r['remote_status'] == 'NOT_CREATED'
                           and r['local_cleaned'] and not r['remote_cleaned']
                           for r in journal if r['entity_type'] == 'profile_attachment')
        if uncertain:
            # Real process restart; no journal records are inserted by this test.
            server.fail_readback_after_reloads = None
            with source_app(storage) as base:
                assert rows(storage, "mutation_ownership_records", run) == journal
                recovery_run = start_run(base, project)
                reconciled = rows(storage, "mutation_ownership_records", run)
                evidence.update(recovery_run=recovery_run, journal_after_restart=reconciled,
                                recovery_results=rows(storage, "tests", recovery_run))
                assert {r["id"] for r in reconciled} == {r["id"] for r in journal}
                assert all(r["remote_cleaned"] == 1 and r["remote_status"] == "CONFIRMED_DELETED" for r in reconciled)
                for previous in pending:
                    assert sum(op["op"] == "CREATE" and op["id"] == previous["remote_entity_id"] for op in server.operations_log) == 1
                assert server.experiences == original
                assert server.attachments == attachments
        assert all(server.experiences[k] == v for k, v in original.items())
    finally:
        evidence.update(operations=server.operations_log, final_experiences=server.experiences)
        destination = Path(os.environ.get("QA_MUTATION_EVIDENCE", str(tmp_path)))
        destination.mkdir(parents=True, exist_ok=True)
        (destination / f"source-lifecycle-{uncertain}.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        saved = destination / f"source-app-{uncertain}"
        if storage.exists():
            shutil.copytree(storage, saved, dirs_exist_ok=True)
        server.shutdown()
        server.server_close()
