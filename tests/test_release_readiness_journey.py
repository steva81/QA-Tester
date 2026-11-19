"""First-Version Release-Readiness User Journey Verification Suite.

Exercises the complete supported user journey across all six stages:
  project setup → browser/session preparation → run configuration →
  execution/progress → stop or completion → results/report/export.

Covers the 5 core journey scenarios against controlled localhost fixtures
and isolated temporary databases:
  1. Successful run with supported disclosure evaluations (PASS, 0 customer findings)
  2. Successful run with no supported disclosures (COMPLETE, 0 disclosure PASS checks)
  3. Limited coverage run (LIMITED COVERAGE outcome, honest diagnostics, 0 customer findings)
  4. Failed run (auth lost/session broken, FAILED status, safe error handling and reporting)
  5. User-stopped run (/stop endpoint, ABORTED terminal status, retained partial evidence)

Also validates security boundaries (path traversal rejection on open endpoints)
and UI consistency across run outcome badges.
"""

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict
from urllib.parse import urlsplit
import pytest

from app.models.database import get_db_connection, init_db
from app.qa.interactive import InteractiveSession
from app.qa.functional_reporting_adapter import FUNCTIONAL_DISCLOSURE_EXPECTED
from app.qa.reporting import load_report
import app.paths as paths
from app.routes import api
from server import app as flask_app


# ==============================================================================
# Localhost Multi-Scenario Fixture Site
# ==============================================================================

class MultiJourneySite:
    def __init__(self):
        self.counts = Counter()
        self.lock = threading.Lock()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                path = parsed.path
                with site.lock:
                    site.counts[path] += 1

                if path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return

                # 1. Success with working disclosure
                if path == "/success/1":
                    html = """<!doctype html><html><head><title>S1</title></head><body>
<h1>Success Site 1</h1>
<a href="/success/2">Page 2</a>
<div class="accordion-item">
  <button id="tog" aria-expanded="false" aria-controls="pan">
    <span>Expand FAQ Details</span>
  </button>
  <div id="pan" style="display: none;">
    <p>Controlled FAQ content revealed.</p>
  </div>
</div>
<script>
const b = document.getElementById('tog');
const p = document.getElementById('pan');
b.onclick = () => {
    const exp = b.getAttribute('aria-expanded') === 'true';
    b.setAttribute('aria-expanded', String(!exp));
    p.style.display = exp ? 'none' : 'block';
};
</script></body></html>"""
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                    return

                if path == "/success/2":
                    html = """<!doctype html><html><head><title>S2</title></head><body>
<h1>Success Site 2</h1>
<a href="/success/1">Page 1</a>
<p>Clean secondary page without disclosures.</p>
</body></html>"""
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                    return

                # 2. Static site (pure navigation, no disclosures)
                if path == "/static/1":
                    html = """<!doctype html><html><head><title>St1</title></head><body>
<h1>Static Site 1</h1>
<a href="/static/2">Static Page 2</a>
<p>Standard documentation text.</p>
</body></html>"""
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                    return

                if path == "/static/2":
                    html = """<!doctype html><html><head><title>St2</title></head><body>
<h1>Static Site 2</h1>
<a href="/static/1">Static Page 1</a>
<p>Standard about page text.</p>
</body></html>"""
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                    return

                # 3. Limited coverage (inert disclosure button)
                if path == "/limited/1":
                    html = """<!doctype html><html><head><title>Lim</title></head><body>
<h1>Limited Coverage Site</h1>
<button id="inert-btn" aria-expanded="false" aria-controls="missing-target">Inert Disclosure</button>
<script>
document.getElementById('inert-btn').onclick = () => {
    document.getElementById('inert-btn').setAttribute('aria-expanded', 'true');
};
</script></body></html>"""
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                    return

                # 4. Auth fail (redirects to /login, causing terminal FAILED status)
                if path == "/auth-fail/1":
                    html = """<!doctype html><html><head><title>AF1</title></head><body>
<h1>Authenticated App</h1>
<a href="/auth-fail/lost">Protected Action</a>
</body></html>"""
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                    return

                if path == "/auth-fail/lost":
                    self.send_response(302)
                    self.send_header("Location", "/login")
                    self.end_headers()
                    return

                if path == "/login":
                    html = "<!doctype html><html><head><title>Login</title></head><body>Login Required</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                    return

                # 5. Slow responding page to allow clean user stop testing
                if path == "/slow/1":
                    time.sleep(2.0)
                    html = "<!doctype html><html><head><title>Slow</title></head><body>Slow Page</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                    return

                self.send_response(404)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


@pytest.fixture
def journey_site():
    site = MultiJourneySite()
    try:
        yield site
    finally:
        site.close()


@pytest.fixture(autouse=True)
def isolated_journey_environment(tmp_path, monkeypatch):
    """Ensure complete filesystem and registry isolation for each test."""
    import app.qa.interactive as interactive
    from app.paths import ensure_dirs

    db_file = str(tmp_path / "journey_test.sqlite")
    exports_dir = str(tmp_path / "exports")
    screenshots_dir = str(tmp_path / "screenshots")

    monkeypatch.setenv("QA_TESTER_DB_PATH", db_file)
    monkeypatch.setattr(paths, "get_exports_dir", lambda: exports_dir)
    monkeypatch.setattr("app.paths.get_exports_dir", lambda: exports_dir)
    monkeypatch.setattr("app.routes.exports.get_exports_dir", lambda: exports_dir)
    monkeypatch.setattr(paths, "get_screenshots_dir", lambda: screenshots_dir)
    monkeypatch.setattr("app.paths.get_screenshots_dir", lambda: screenshots_dir)
    monkeypatch.setattr(os, "startfile", lambda p: None, raising=False)

    os.makedirs(exports_dir, exist_ok=True)
    os.makedirs(screenshots_dir, exist_ok=True)
    ensure_dirs()
    init_db()

    monkeypatch.setattr(api, "worker_records", {})
    monkeypatch.setattr(api, "running_engines", {})
    monkeypatch.setattr(api, "active_worker_threads", [])
    monkeypatch.setattr(api, "accepting_runs", True)
    monkeypatch.setattr(interactive, "sessions", {})

    yield

    for engine in list(api.running_engines.values()):
        try:
            engine.stop()
        except Exception:
            pass
    for worker in list(api.worker_records.values()):
        try:
            worker.cancel.set()
        except Exception:
            pass
    for t in list(api.active_worker_threads):
        if t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)

    api.worker_records.clear()
    api.running_engines.clear()
    api.active_worker_threads.clear()


def create_managed_session(site_url: str, pid: int) -> InteractiveSession:
    from playwright.async_api import async_playwright

    class ManagedSession(InteractiveSession):
        async def _init_playwright(self):
            self.loop_errors = []
            self.loop.set_exception_handler(lambda loop, error: self.loop_errors.append(error))
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True)
            self.context = await self.browser.new_context(service_workers="block")
            self.page = await self.context.new_page()
            await self.page.goto(site_url)

        def stop(self, timeout=5):
            super().stop(timeout)
            if hasattr(self, "thread") and self.thread:
                self.thread.join(timeout=3)

    session = ManagedSession(pid, site_url)
    session.start()
    from app.qa.interactive import sessions
    sessions[str(pid)] = session
    return session


# ==============================================================================
# Journey 1: Successful Run with Supported Disclosures
# ==============================================================================

def test_journey_1_successful_run_with_disclosure_pass(journey_site):
    """Workflow: Setup -> Session -> Config -> Execution -> Completion -> Reports/Exports."""
    client = flask_app.test_client()

    # 1. Project Setup
    res = client.post("/api/projects", json={
        "name": "Release Target App",
        "target_url": journey_site.base + "/success/1",
        "environment": "Staging",
        "tester_name": "QA Lead"
    })
    assert res.status_code == 200
    pid = res.get_json()["id"]

    # 2. Browser / Session Preparation
    session = create_managed_session(journey_site.base + "/success/1", pid)
    try:
        state_res = client.get(f"/api/projects/{pid}/interactive/state")
        assert state_res.status_code == 200
        assert state_res.get_json()["active"] is True

        # 3. Run Configuration (Full Site QA, page limit 5, disclosure enabled)
        run_res = client.post("/api/runs", json={
            "project_id": pid,
            "mode": "full",
            "page_limit": 5,
            "enable_interactive_disclosure": True,
        })
        assert run_res.status_code == 200
        rid = run_res.get_json()["run_id"]

        # 4. Execution & Completion
        worker = api.worker_records.get(rid)
        assert worker is not None
        if worker.thread:
            worker.thread.join(timeout=35)
            assert not worker.thread.is_alive()

        # Verify Execution status in DB
        conn = get_db_connection()
        run_row = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
        assert run_row["status"] == "COMPLETED"

        # 5. Result Integrity & Classification Provenance
        report = load_report(conn, rid)
        assert report is not None
        assert report["outcome"] in ("COMPLETE", "COMPLETE WITH FINDINGS")
        assert report["coverage"]["coverage_complete"] is True
        assert report["customer_finding_count"] == 0

        # Verify evaluated disclosure controls produce PASS records
        tests = conn.execute("SELECT * FROM tests WHERE run_id=?", (rid,)).fetchall()
        disc_passes = [
            t for t in tests
            if t["status"] == "PASS" and t["expected_result"] == FUNCTIONAL_DISCLOSURE_EXPECTED
        ]
        assert len(disc_passes) > 0, "Verified disclosure control must record PASS"
        for t in disc_passes:
            assert t["stable_id"] is None, "Disclosure record must never have a customer finding stable_id"

        conn.close()

        # 6. Results Review, Report & Exports
        # Web UI Views
        assert client.get(f"/run/{rid}?tab=summary").status_code == 200
        assert client.get(f"/run/{rid}?tab=findings").status_code == 200
        assert client.get(f"/run/{rid}?tab=reports").status_code == 200
        assert client.get(f"/run/{rid}?tab=technical").status_code == 200

        # Previews
        preview_std = client.get(f"/run/{rid}/preview?mode=standard")
        assert preview_std.status_code == 200
        assert "Release Target App" in preview_std.get_data(as_text=True)

        preview_full = client.get(f"/run/{rid}/preview?mode=full")
        assert preview_full.status_code == 200
        assert "Full Technical" in preview_full.get_data(as_text=True)

        # File Exports: HTML, TXT, Markdown, Word (DOCX)
        html_export = client.get(f"/export/{rid}/html?mode=standard")
        assert html_export.status_code == 200
        assert "Release Target App" in html_export.get_data(as_text=True)

        txt_res = client.post(f"/api/export/{rid}/txt?mode=standard")
        assert txt_res.status_code == 200
        txt_filename = txt_res.get_json()["filename"]

        md_res = client.post(f"/api/export/{rid}/markdown?mode=standard")
        assert md_res.status_code == 200
        md_filename = md_res.get_json()["filename"]

        docx_res = client.post(f"/api/export/{rid}/docx?mode=standard")
        assert docx_res.status_code == 200
        docx_filename = docx_res.get_json()["filename"]

        exports_dir = paths.get_exports_dir()
        txt_content = (Path(exports_dir) / txt_filename).read_text(encoding="utf-8")
        assert "Release Target App" in txt_content
        assert "Outcome: COMPLETE" in txt_content
        assert f"Total Checks: {report['total_checks']}" in txt_content

        md_content = (Path(exports_dir) / md_filename).read_text(encoding="utf-8")
        assert "Release Target App" in md_content
        assert "Outcome: COMPLETE" in md_content

        assert (Path(exports_dir) / docx_filename).exists()

        # Test local open endpoint
        open_res = client.post(f"/api/exports/{txt_filename}/open")
        assert open_res.status_code == 200
        assert open_res.get_json()["status"] == "success"

    finally:
        session.stop()


# ==============================================================================
# Journey 2: Successful Run with No Supported Disclosures
# ==============================================================================

def test_journey_2_successful_run_no_supported_disclosures(journey_site):
    """Page has no disclosure elements; run completes honestly without false disclosures."""
    client = flask_app.test_client()

    res = client.post("/api/projects", json={
        "name": "Static Site App",
        "target_url": journey_site.base + "/static/1",
        "environment": "Production",
        "tester_name": "QA Specialist"
    })
    pid = res.get_json()["id"]

    session = create_managed_session(journey_site.base + "/static/1", pid)
    try:
        run_res = client.post("/api/runs", json={
            "project_id": pid,
            "mode": "full",
            "page_limit": 5,
            "enable_interactive_disclosure": True,
        })
        assert run_res.status_code == 200
        rid = run_res.get_json()["run_id"]

        worker = api.worker_records.get(rid)
        if worker and worker.thread:
            worker.thread.join(timeout=35)
            assert not worker.thread.is_alive()

        conn = get_db_connection()
        run_row = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
        assert run_row["status"] == "COMPLETED"

        report = load_report(conn, rid)
        assert report["outcome"] in ("COMPLETE", "COMPLETE WITH FINDINGS")
        assert report["coverage"]["coverage_complete"] is True
        assert report["customer_finding_count"] == 0

        # No disclosure checks should have been evaluated
        tests = conn.execute("SELECT * FROM tests WHERE run_id=?", (rid,)).fetchall()
        disc_checks = [t for t in tests if t["expected_result"] == FUNCTIONAL_DISCLOSURE_EXPECTED]
        assert len(disc_checks) == 0, "No disclosures should be evaluated on static page"

        conn.close()

        # Exports work without issue
        txt_res = client.post(f"/api/export/{rid}/txt?mode=standard")
        assert txt_res.status_code == 200
    finally:
        session.stop()


# ==============================================================================
# Journey 3: Limited Coverage Run
# ==============================================================================

def test_journey_3_limited_coverage_run(journey_site):
    """Inert/unverified disclosure or budget cap triggers LIMITED COVERAGE, not customer bugs."""
    client = flask_app.test_client()

    res = client.post("/api/projects", json={
        "name": "Unverified Control App",
        "target_url": journey_site.base + "/limited/1",
        "environment": "Test",
        "tester_name": "QA Specialist"
    })
    pid = res.get_json()["id"]

    session = create_managed_session(journey_site.base + "/limited/1", pid)
    try:
        run_res = client.post("/api/runs", json={
            "project_id": pid,
            "mode": "full",
            "page_limit": 5,
            "enable_interactive_disclosure": True,
        })
        rid = run_res.get_json()["run_id"]

        worker = api.worker_records.get(rid)
        if worker and worker.thread:
            worker.thread.join(timeout=35)
            assert not worker.thread.is_alive()

        conn = get_db_connection()
        report = load_report(conn, rid)
        assert report is not None
        assert report["outcome"] == "LIMITED COVERAGE"
        assert report["customer_finding_count"] == 0, "Unverified control must not be a customer defect"

        # Check technical record status
        tests = conn.execute("SELECT * FROM tests WHERE run_id=?", (rid,)).fetchall()
        limited_records = [t for t in tests if t["status"] == "LIMITED COVERAGE"]
        assert len(limited_records) > 0, "Must record LIMITED COVERAGE for unverified disclosure control"

        conn.close()

        # Report and export clearly reflect LIMITED COVERAGE
        preview = client.get(f"/run/{rid}/preview?mode=standard").get_data(as_text=True)
        assert "LIMITED COVERAGE" in preview
        assert "evaluation was incomplete" in preview
    finally:
        session.stop()


# ==============================================================================
# Journey 4: Failed Run
# ==============================================================================

def test_journey_4_failed_run(journey_site):
    """Lost authentication during crawl produces truthful FAILED status and handles reporting gracefully."""
    client = flask_app.test_client()

    res = client.post("/api/projects", json={
        "name": "Auth Loss App",
        "target_url": journey_site.base + "/auth-fail/1",
        "environment": "Dev",
        "tester_name": "QA Tester"
    })
    pid = res.get_json()["id"]

    session = create_managed_session(journey_site.base + "/auth-fail/1", pid)
    try:
        run_res = client.post("/api/runs", json={
            "project_id": pid,
            "mode": "full",
            "page_limit": 5,
            "enable_interactive_disclosure": True,
        })
        assert run_res.status_code == 200
        rid = run_res.get_json()["run_id"]

        # Wait for execution to finish
        worker = api.worker_records.get(rid)
        if worker and worker.thread:
            worker.thread.join(timeout=30)

        conn = get_db_connection()
        run_row = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
        assert run_row["status"] == "FAILED"

        report = load_report(conn, rid)
        assert report["outcome"] == "FAILED"
        assert "failed" in report["summary"].lower()

        conn.close()

        # Results view and exports render without crashing
        res_page = client.get(f"/run/{rid}")
        assert res_page.status_code == 200
        assert "FAILED" in res_page.get_data(as_text=True)

        txt_res = client.post(f"/api/export/{rid}/txt?mode=standard")
        assert txt_res.status_code == 200
    finally:
        session.stop()


# ==============================================================================
# Journey 5: User-Stopped Run
# ==============================================================================

def test_journey_5_user_stopped_run(journey_site):
    """User cancellation halts execution, transitions to ABORTED, and retains evidence."""
    client = flask_app.test_client()

    res = client.post("/api/projects", json={
        "name": "Stop Target App",
        "target_url": journey_site.base + "/slow/1",
        "environment": "Dev",
        "tester_name": "QA Tester"
    })
    pid = res.get_json()["id"]

    run_res = client.post("/api/runs", json={
        "project_id": pid,
        "mode": "crawl",
    })
    rid = run_res.get_json()["run_id"]

    # Allow worker to start then immediately send stop request
    time.sleep(0.3)
    stop_res = client.post(f"/api/runs/{rid}/stop")
    assert stop_res.status_code == 200
    assert stop_res.get_json()["status"] in ("stopping", "not_running")

    worker = api.worker_records.get(rid)
    if worker and worker.thread:
        worker.thread.join(timeout=20)

    conn = get_db_connection()
    run_row = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
    assert run_row["status"] == "ABORTED"

    report = load_report(conn, rid)
    assert report["outcome"] in ("INTERRUPTED", "LIMITED COVERAGE")
    assert "interrupted" in report["summary"].lower() or "incomplete" in report["summary"].lower()

    conn.close()

    # Verify run workspace and reports reflect interrupted status
    page = client.get(f"/run/{rid}").get_data(as_text=True)
    assert "ABORTED" in page or "INTERRUPTED" in page


# ==============================================================================
# 6. Security Boundaries: Path Traversal Protection
# ==============================================================================

def test_open_endpoints_directory_traversal_protection():
    """Verify open endpoints strictly reject path traversal attempts."""
    client = flask_app.test_client()

    # Traversal in export open endpoint
    res_export = client.post("/api/exports/../../secret.txt/open")
    assert res_export.status_code == 403
    assert res_export.get_json()["message"] == "Access denied"

    # Traversal in screenshot open endpoint
    res_screenshot = client.post("/api/screenshots/../../secret.png/open")
    assert res_screenshot.status_code == 403
    assert res_screenshot.get_json()["message"] == "Access denied"

    # Open exports folder ensures folder exists and returns success
    res_folder = client.post("/api/open-exports-folder")
    assert res_folder.status_code == 200
    assert res_folder.get_json()["status"] == "success"


# ==============================================================================
# 7. UI Consistency & Badge Classes
# ==============================================================================

def test_project_page_status_badge_tones():
    """Project page renders consistent status badge tones: COMPLETED (success), FAILED (danger), ABORTED (warning)."""
    client = flask_app.test_client()

    conn = get_db_connection()
    pid = conn.execute(
        "INSERT INTO projects(name, target_url, environment, tester_name) VALUES ('Badge App', 'http://127.0.0.1/', 'Test', 'QA')"
    ).lastrowid
    conn.execute("INSERT INTO runs(id, project_id, status, start_time) VALUES (101, ?, 'COMPLETED', '2026-10-17 12:00:00')", (pid,))
    conn.execute("INSERT INTO runs(id, project_id, status, start_time) VALUES (102, ?, 'FAILED', '2026-10-17 12:01:00')", (pid,))
    conn.execute("INSERT INTO runs(id, project_id, status, start_time) VALUES (103, ?, 'ABORTED', '2026-10-17 12:02:00')", (pid,))
    conn.commit()
    conn.close()

    html = client.get(f"/project/{pid}").get_data(as_text=True)
    assert 'Run #101 <span class="badge badge-success">COMPLETED</span>' in html
    assert 'Run #102 <span class="badge badge-danger">FAILED</span>' in html
    assert 'Run #103 <span class="badge badge-warning">ABORTED</span>' in html
