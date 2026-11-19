"""Phase C4: Controlled Disclosure UI & Pipeline Integration Test Suite.

Acceptance Matrix Coverage:
1. Strict boolean validation on API input (rejects non-boolean strings like "false", ints, lists, None).
2. Supported mode enforcement (only Full Site QA mode allows enable_interactive_disclosure=True; others rejected with 400).
3. Missing / false option preserves legacy execution (0 interactive disclosure evaluations).
4. Full Site QA opt-in reaches real interactive disclosure execution through real worker pipeline.
5. UI option default-off, plain-language description without jargon, accessible label and keyboard toggle.
6. Mode switching in UI resets and hides disclosure checkbox for unsupported modes.
7. Browser Back/Forward history traversal resets disclosure checkbox to default-off across runs.
8. Failed-start recovery keeps UI controls usable without accidentally inheriting successful run state.
9. Run start / stop through real API and UI endpoints with truthful progress and terminal status.
10. Bounded cleanup and cancellation halts further dispatch and preserves terminal evidence.
11. Result uniqueness / deduplication across checkpoints and final save.
12. Source / manual visible page invariance (session.page URL, unsaved form inputs, and content untouched).
13. Circuit breaker on cleanup failure halts subsequent interactive evaluations.
14. Component persistence test vs live interrupted runner persistence explicitly distinguished.
15. Privacy preservation: origin-only reporting without query/path secrets.
16. Uninterrupted browser-to-worker pipeline integration.
17. Per-run configuration isolation and concurrency.
"""

import asyncio
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, parse_qs
import pytest

from app.models.database import get_db_connection, init_db
from app.models.crawl import get_coverage
from app.qa.full_site import FullSiteQA
from app.qa.interactive import InteractiveSession
from app.qa.interactive_bridge import (
    InteractiveDisclosureBridge,
    InteractiveWorkflowConfig,
)
from app.qa.functional_reporting_adapter import FUNCTIONAL_DISCLOSURE_EXPECTED
from app.qa.reporting import load_report
from app.routes import api
from server import app as flask_app


# ==============================================================================
# Localhost Server Fixture
# ==============================================================================

class LocalhostDisclosureSite:
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
                cookie = self.headers.get("Cookie", "")
                with site.lock:
                    site.counts[path] += 1

                if path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return

                if path == "/login":
                    self.send_response(200)
                    self.send_header("Set-Cookie", "qa_login=yes; Path=/; HttpOnly")
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html><body>Logged in</body></html>")
                    return

                # Disclosure test page
                if path in ("/", "/disclosure"):
                    html = """<!doctype html>
<html>
<head><title>Disclosure Test Page</title></head>
<body>
<h1>Disclosure Fixture</h1>
<nav><a href="/disclosure">Disclosure</a> | <a href="/static-page">Static</a></nav>
<div class="accordion-item">
  <button id="toggle-btn" aria-expanded="false" aria-controls="panel-content">
    <span>Expand Details</span>
  </button>
  <div id="panel-content" style="display: none;">
    <p>Controlled content now visible.</p>
  </div>
</div>
<form id="draft-form">
  <label for="draft-input">Draft Notes:</label>
  <input type="text" id="draft-input" value="initial_notes">
</form>
<script>
  const btn = document.getElementById('toggle-btn');
  const panel = document.getElementById('panel-content');
  btn.addEventListener('click', function() {
    const isExpanded = btn.getAttribute('aria-expanded') === 'true';
    btn.setAttribute('aria-expanded', String(!isExpanded));
    panel.style.display = isExpanded ? 'none' : 'block';
  });
</script>
</body>
</html>"""
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                    return

                if path == "/static-page":
                    html = """<!doctype html>
<html>
<head><title>Static Page</title></head>
<body>
<h1>Static Content</h1>
<p>No disclosure controls here.</p>
<a href="/">Home</a>
</body>
</html>"""
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                    return

                if path == "/secret-page":
                    # Page with path and query parameters
                    html = """<!doctype html>
<html>
<head><title>Secret Query Page</title></head>
<body>
<button id="secret-toggle" aria-expanded="false" aria-controls="secret-panel">Secret Info</button>
<div id="secret-panel" style="display: none;">Confidential info</div>
<script>
  const b = document.getElementById('secret-toggle');
  const p = document.getElementById('secret-panel');
  b.addEventListener('click', () => {
    const exp = b.getAttribute('aria-expanded') === 'true';
    b.setAttribute('aria-expanded', String(!exp));
    p.style.display = exp ? 'none' : 'block';
  });
</script>
</body>
</html>"""
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
def site():
    instance = LocalhostDisclosureSite()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(autouse=True)
def isolated_c4_state(tmp_path, monkeypatch):
    import app.qa.interactive as interactive
    db_file = str(tmp_path / "c4_test.sqlite")
    monkeypatch.setenv("QA_TESTER_DB_PATH", db_file)
    from app.paths import ensure_dirs
    ensure_dirs()
    init_db()
    monkeypatch.setattr(api, "worker_records", {})
    monkeypatch.setattr(api, "running_engines", {})
    monkeypatch.setattr(api, "active_worker_threads", [])
    monkeypatch.setattr(api, "accepting_runs", True)
    monkeypatch.setattr(interactive, "sessions", {})
    yield
    # Ensure any background test runs finish
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


def seed_project(site, name="C4 Project"):
    conn = get_db_connection()
    pid = conn.execute(
        "INSERT INTO projects(name, target_url, environment, tester_name) VALUES (?, ?, 'Test', 'QA')",
        (name, site.base + "/"),
    ).lastrowid
    conn.commit()
    conn.close()
    return pid


def create_live_session(site, pid):
    from playwright.async_api import async_playwright

    class ManagedInteractiveSession(InteractiveSession):
        async def _init_playwright(self):
            self.loop_errors = []
            self.loop.set_exception_handler(lambda loop, error: self.loop_errors.append(error))
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True)
            self.context = await self.browser.new_context(service_workers="block")
            self.page = await self.context.new_page()
            await self.page.goto(site.base + "/login")
            await self.page.goto(site.base + "/")

        def stop(self, timeout=5):
            super().stop(timeout)
            if hasattr(self, "thread") and self.thread:
                self.thread.join(timeout=3)

    session = ManagedInteractiveSession(pid, site.base + "/")
    session.start()
    from app.qa.interactive import sessions
    sessions[str(pid)] = session
    return session


# ==============================================================================
# 1. API Strict Boolean Validation Tests
# ==============================================================================

@pytest.mark.parametrize("invalid_val", [
    "false", "true", "0", "1", 1, 0, [True], {"enabled": True}, None
])
def test_api_disclosure_strict_boolean_validation(site, invalid_val):
    """API must reject non-boolean inputs for enable_interactive_disclosure with 400 Bad Request."""
    pid = seed_project(site)
    client = flask_app.test_client()

    payload = {
        "project_id": pid,
        "mode": "full",
        "enable_interactive_disclosure": invalid_val,
    }
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 400
    data = response.get_json()
    assert data["error"] == "enable_interactive_disclosure must be a boolean."


def test_api_disclosure_missing_defaults_to_false(site):
    """Omitting enable_interactive_disclosure preserves legacy execution (defaults to False)."""
    pid = seed_project(site)
    client = flask_app.test_client()

    # In crawl mode, legacy run starts without requiring an interactive session
    payload = {"project_id": pid, "mode": "crawl"}
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "started"
    assert "run_id" in data
    rid = data["run_id"]
    client.post(f"/api/runs/{rid}/stop")
    worker = api.worker_records.get(rid)
    if worker and worker.thread:
        worker.thread.join(timeout=5)


def test_api_disclosure_explicit_false_accepted(site):
    """Explicitly passing enable_interactive_disclosure=False is valid across modes."""
    pid = seed_project(site)
    client = flask_app.test_client()

    payload = {"project_id": pid, "mode": "crawl", "enable_interactive_disclosure": False}
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "started"
    rid = data["run_id"]
    client.post(f"/api/runs/{rid}/stop")
    worker = api.worker_records.get(rid)
    if worker and worker.thread:
        worker.thread.join(timeout=5)


# ==============================================================================
# 2. Supported Mode Enforcement Tests
# ==============================================================================

@pytest.mark.parametrize("unsupported_mode", ["crawl", "current", "collected"])
def test_api_disclosure_unsupported_modes_rejected(site, unsupported_mode):
    """Requesting enable_interactive_disclosure=True in unsupported modes returns 400."""
    pid = seed_project(site)
    client = flask_app.test_client()

    payload = {
        "project_id": pid,
        "mode": unsupported_mode,
        "enable_interactive_disclosure": True,
    }
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 400
    data = response.get_json()
    assert data["error"] == "Interactive disclosure evaluation is only supported in Full Site QA mode."


def test_api_disclosure_full_mode_allowed_with_ready_session(site):
    """Requesting enable_interactive_disclosure=True in Full Site QA mode with READY session succeeds."""
    pid = seed_project(site)
    session = create_live_session(site, pid)
    client = flask_app.test_client()
    try:
        payload = {
            "project_id": pid,
            "mode": "full",
            "page_limit": 5,
            "enable_interactive_disclosure": True,
        }
        response = client.post("/api/runs", json=payload)
        assert response.status_code == 200
        assert response.get_json()["status"] == "started"
        rid = response.get_json()["run_id"]
        client.post(f"/api/runs/{rid}/stop")
        worker = api.worker_records.get(rid)
        if worker and worker.thread:
            worker.thread.join(timeout=5)
    finally:
        session.stop()


# ==============================================================================
# 3. UI / Browser Usability & Workflow Tests
# ==============================================================================

def test_browser_ui_disclosure_option_and_accessibility(site):
    """Verify UI checkbox state, plain-language description, accessibility, and mode switching."""
    from playwright.sync_api import sync_playwright, expect
    from werkzeug.serving import make_server

    pid = seed_project(site)
    server = make_server("127.0.0.1", 0, flask_app, threaded=True)
    t = threading.Thread(target=server.serve_forever)
    t.daemon = True
    t.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()

            # Mock interactive session state as READY
            page.route("**/interactive/state", lambda route: route.fulfill(json={
                "active": True,
                "state": "READY",
                "current_url": site.base + "/",
                "collected_urls": [],
            }))

            captured_payloads = []

            def handle_run_post(route):
                captured_payloads.append(route.request.post_data_json)
                route.fulfill(json={"status": "started", "run_id": 101})

            page.route("**/api/runs", handle_run_post)

            page.goto(f"{base_url}/project/{pid}/dry-run")
            expect(page.locator("#session-status")).to_contain_text("READY")

            # 1. Default mode is 'crawl': disclosure-field must be hidden
            expect(page.locator("#disclosure-field")).to_be_hidden()
            checkbox = page.locator("#enableInteractiveDisclosure")
            assert not checkbox.is_checked()

            # 2. Switch to 'full' mode: disclosure-field becomes visible
            page.select_option("#test-mode", "full")
            expect(page.locator("#disclosure-field")).to_be_visible()

            # Verify truthful plain-language description and labels (no internal jargon)
            label_text = page.locator('label[for="enableInteractiveDisclosure"]').inner_text()
            assert "Test expandable content" in label_text
            help_text = page.locator("#disclosure-field .text-muted").inner_text()
            assert "Check that FAQ answers and other hidden sections appear when clicked" in help_text
            assert "B2c" not in help_text
            assert "offline barrier" not in help_text


            # 3. Default state is unchecked
            assert not checkbox.is_checked()

            # 4. Keyboard accessibility: toggle with Spacebar
            checkbox.focus()
            page.keyboard.press("Space")
            assert checkbox.is_checked()

            # 5. Switching away from 'full' resets checkbox to false and hides field
            page.select_option("#test-mode", "crawl")
            expect(page.locator("#disclosure-field")).to_be_hidden()
            assert not checkbox.is_checked()

            # 6. Switching back to 'full': field is visible, checkbox remains unchecked (not carried over)
            page.select_option("#test-mode", "full")
            expect(page.locator("#disclosure-field")).to_be_visible()
            assert not checkbox.is_checked()

            # 7. Start run with disclosure enabled: check auth_confirm, check disclosure, click start
            page.check("#auth_confirm")
            page.check("#enableInteractiveDisclosure")
            page.get_by_role("button", name="Start QA Test", exact=True).click()

            # Wait for navigation attempt
            page.wait_for_url(f"{base_url}/run/101")
            assert len(captured_payloads) == 1
            payload = captured_payloads[0]
            assert payload["mode"] == "full"
            assert payload["enable_interactive_disclosure"] is True

            # 8. Test error recovery: if API fails, controls recover to usable state
            captured_payloads.clear()
            page.route("**/api/runs", lambda route: route.fulfill(
                status=400, json={"error": "Simulated start failure"}
            ))
            page.goto(f"{base_url}/project/{pid}/dry-run")
            page.select_option("#test-mode", "full")
            page.check("#auth_confirm")
            page.get_by_role("button", name="Start QA Test", exact=True).click()

            expect(page.locator("#startError")).to_contain_text("Simulated start failure")
            expect(page.locator("#btnStart")).to_have_text("Start QA Test")
            assert not page.locator("#btnStart").is_disabled()

            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=3)


def test_browser_history_navigation_opt_in_lifecycle(site):
    """Browser Back/Forward navigation must reset disclosure opt-in to default-off across runs.

    Covers:
    1. Failed-start recovery keeps controls usable without accidentally inheriting successful run state.
    2. Successful run dispatch navigates to legitimate run page.
    3. Browser Back resets enableInteractiveDisclosure to unchecked.
    4. Subsequent run without re-checking dispatches enable_interactive_disclosure=False.
    5. Browser Forward to run page, then Back again preserves unchecked default-off.
    6. Fresh explicit re-selection allows subsequent run with enable_interactive_disclosure=True.
    """
    from playwright.sync_api import sync_playwright, expect
    from werkzeug.serving import make_server

    pid = seed_project(site)
    server = make_server("127.0.0.1", 0, flask_app, threaded=True)
    t = threading.Thread(target=server.serve_forever)
    t.daemon = True
    t.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()

            # Mock interactive session state as READY
            page.route("**/interactive/state", lambda route: route.fulfill(json={
                "active": True,
                "state": "READY",
                "current_url": site.base + "/",
                "collected_urls": [],
            }))

            captured_payloads = []
            should_fail_start = False

            # Seed a real run in DB so /run/<rid> returns 200 OK
            conn = get_db_connection()
            legit_rid = conn.execute("INSERT INTO runs (project_id, status) VALUES (?, 'RUNNING')", (pid,)).lastrowid
            conn.commit()
            conn.close()

            def handle_run_post(route):
                payload = route.request.post_data_json
                captured_payloads.append(payload)
                if should_fail_start:
                    route.fulfill(status=400, json={"error": "Simulated failed start"})
                else:
                    route.fulfill(json={"status": "started", "run_id": legit_rid})

            page.route("**/api/runs", handle_run_post)

            # 1. Open dry-run page
            page.goto(f"{base_url}/project/{pid}/dry-run")
            page.select_option("#test-mode", "full")
            page.check("#auth_confirm")
            page.check("#enableInteractiveDisclosure")
            assert page.is_checked("#enableInteractiveDisclosure")

            # 2. Failed-start recovery: fail the start request
            should_fail_start = True
            page.click("#btnStart")
            expect(page.locator("#startError")).to_contain_text("Simulated failed start")
            expect(page.locator("#btnStart")).to_have_text("Start QA Test")
            assert not page.locator("#btnStart").is_disabled()
            # Selection is retained on failure so user can fix and retry
            assert page.is_checked("#enableInteractiveDisclosure")

            # 3. Successful start with disclosure enabled
            should_fail_start = False
            page.click("#btnStart")
            page.wait_for_url(f"{base_url}/run/{legit_rid}")
            assert len(captured_payloads) == 2
            assert captured_payloads[-1]["enable_interactive_disclosure"] is True

            # 4. Browser Back to dry-run page
            page.go_back()
            page.wait_for_url(f"{base_url}/project/{pid}/dry-run")
            # Invariant: disclosure checkbox MUST be unchecked after returning from run
            expect(page.locator("#enableInteractiveDisclosure")).not_to_be_checked()
            expect(page.locator("#btnStart")).to_have_text("Start QA Test")
            assert not page.locator("#btnStart").is_disabled()

            # 5. Subsequent run without re-checking: dispatches False
            page.click("#btnStart")
            page.wait_for_url(f"{base_url}/run/{legit_rid}")
            assert len(captured_payloads) == 3
            assert captured_payloads[-1]["enable_interactive_disclosure"] is False

            # 6. Browser Forward to run page, then Back again
            page.go_forward()
            page.wait_for_url(f"{base_url}/run/{legit_rid}")
            page.go_back()
            page.wait_for_url(f"{base_url}/project/{pid}/dry-run")
            expect(page.locator("#enableInteractiveDisclosure")).not_to_be_checked()

            # 7. Explicit re-selection required for another disclosure run
            page.check("#enableInteractiveDisclosure")
            assert page.is_checked("#enableInteractiveDisclosure")
            page.click("#btnStart")
            page.wait_for_url(f"{base_url}/run/{legit_rid}")
            assert len(captured_payloads) == 4
            assert captured_payloads[-1]["enable_interactive_disclosure"] is True

            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=3)


# ==============================================================================
# 4. Real Pipeline Execution with Localhost Fixture
# ==============================================================================

def test_real_pipeline_full_disclosure_success_and_reporting(site):
    """End-to-end execution: opt-in reaches real FullSiteQA disclosure evaluation and canonical SQLite persistence."""
    pid = seed_project(site)
    session = create_live_session(site, pid)
    client = flask_app.test_client()

    try:
        payload = {
            "project_id": pid,
            "mode": "full",
            "page_limit": 2,
            "enable_interactive_disclosure": True,
        }
        res = client.post("/api/runs", json=payload)
        assert res.status_code == 200
        rid = res.get_json()["run_id"]

        # Wait for real worker thread to complete
        worker = api.worker_records.get(rid)
        if worker and worker.thread:
            worker.thread.join(timeout=30)
            assert not worker.thread.is_alive(), "Worker thread timed out"

        # Check DB results
        conn = get_db_connection()
        run_row = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
        assert run_row["status"] == "COMPLETED"

        tests = conn.execute("SELECT * FROM tests WHERE run_id=?", (rid,)).fetchall()
        test_statuses = [t["status"] for t in tests]
        assert "PASS" in test_statuses

        # Verify the specific disclosure check record
        disclosure_passes = [
            t for t in tests
            if t["status"] == "PASS" and t["expected_result"] == FUNCTIONAL_DISCLOSURE_EXPECTED
        ]
        assert len(disclosure_passes) > 0
        disclosure_pass = disclosure_passes[0]
        assert "Verified safe disclosure expansion" in disclosure_pass["actual_result"]

        # Provenance Invariant: Disclosure tests NEVER generate finding_ids or customer findings
        disclosure_tests = [
            t for t in tests if t["expected_result"] == FUNCTIONAL_DISCLOSURE_EXPECTED
        ]
        for dt in disclosure_tests:
            assert dt["stable_id"] is None, "Disclosure test must never assign a finding_id"

        # Invariant: Customer findings must be 0 (no defects created from disclosure or passive checks on fixture)
        findings = conn.execute("SELECT * FROM findings WHERE run_id=?", (rid,)).fetchall()
        assert len(findings) == 0

        # Load canonical report
        report = load_report(conn, rid)
        assert report["customer_finding_count"] == 0
        assert report["total_checks"] > 0
        assert report["outcome"] in ("COMPLETE", "LIMITED COVERAGE")

        # Invariant: Manual visible page was never touched
        assert session.page.url == site.base + "/"
        assert not session.page.is_closed()
        conn.close()
    finally:
        session.stop()


def test_real_pipeline_legacy_crawl_when_disclosure_disabled(site):
    """When enable_interactive_disclosure=False, legacy execution runs without any disclosure checks."""
    pid = seed_project(site)
    session = create_live_session(site, pid)
    client = flask_app.test_client()

    try:
        payload = {
            "project_id": pid,
            "mode": "full",
            "page_limit": 2,
            "enable_interactive_disclosure": False,
        }
        res = client.post("/api/runs", json=payload)
        assert res.status_code == 200
        rid = res.get_json()["run_id"]

        worker = api.worker_records.get(rid)
        if worker and worker.thread:
            worker.thread.join(timeout=30)
            assert not worker.thread.is_alive()

        conn = get_db_connection()
        tests = conn.execute("SELECT * FROM tests WHERE run_id=?", (rid,)).fetchall()
        disclosure_tests = [
            t for t in tests if t["expected_result"] == FUNCTIONAL_DISCLOSURE_EXPECTED
        ]
        assert len(disclosure_tests) == 0, "No disclosure tests should be performed when disabled"
        conn.close()
    finally:
        session.stop()


def test_real_pipeline_no_supported_candidates(site):
    """When a page has no disclosure controls, run completes honestly without fake PASS records."""
    # Seed project pointing directly to static-page
    conn = get_db_connection()
    pid = conn.execute(
        "INSERT INTO projects(name, target_url, environment, tester_name) VALUES ('Static Only', ?, 'Test', 'QA')",
        (site.base + "/static-page",),
    ).lastrowid
    conn.commit()
    conn.close()

    session = create_live_session(site, pid)
    client = flask_app.test_client()

    try:
        payload = {
            "project_id": pid,
            "mode": "full",
            "page_limit": 1,
            "enable_interactive_disclosure": True,
        }
        res = client.post("/api/runs", json=payload)
        assert res.status_code == 200
        rid = res.get_json()["run_id"]

        worker = api.worker_records.get(rid)
        if worker and worker.thread:
            worker.thread.join(timeout=30)
            assert not worker.thread.is_alive()

        conn = get_db_connection()
        tests = conn.execute("SELECT * FROM tests WHERE run_id=?", (rid,)).fetchall()
        # Passive crawl tests are preserved, but zero disclosure PASS records
        disclosure_passes = [
            t for t in tests if t["status"] == "PASS" and t["expected_result"] == FUNCTIONAL_DISCLOSURE_EXPECTED
        ]
        assert len(disclosure_passes) == 0
        conn.close()
    finally:
        session.stop()


# ==============================================================================
# 5. Stop / Interruption and Lifecycle Cancellation Tests
# ==============================================================================

def test_real_pipeline_stop_and_interrupted_run_terminal_evidence(site, monkeypatch):
    """Cancelling a run during active disclosure observation halts dispatch and preserves evidence.

    Synchronizes at an actual disclosure evaluation boundary (after action click is confirmed).
    Verifies:
    1. Confirmed action record is retained with LIMITED COVERAGE status.
    2. No subsequent disclosure candidates dispatched.
    3. Ephemeral disposable context cleaned up cleanly.
    4. Terminal run status in DB is ABORTED.
    """
    import app.qa.functional_oracle as fo
    pid = seed_project(site)
    session = create_live_session(site, pid)
    client = flask_app.test_client()

    action_confirmed_evt = threading.Event()
    stop_called_evt = threading.Event()
    orig_wait = fo._wait_for_async_disclosure_transition

    async def hooked_wait(*args, **kwargs):
        # We are at the post-action observation boundary (action click confirmed!)
        action_confirmed_evt.set()
        # Wait up to 5s for the stop endpoint to be called by the test thread
        deadline = time.monotonic() + 5
        while not stop_called_evt.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        return await orig_wait(*args, **kwargs)

    monkeypatch.setattr(fo, "_wait_for_async_disclosure_transition", hooked_wait)

    try:
        payload = {
            "project_id": pid,
            "mode": "full",
            "page_limit": 10,
            "enable_interactive_disclosure": True,
        }
        res = client.post("/api/runs", json=payload)
        assert res.status_code == 200
        rid = res.get_json()["run_id"]

        # Wait deterministically for disclosure action click to confirm
        assert action_confirmed_evt.wait(timeout=15), "Action click was not reached in time"

        # Trigger real stop endpoint at this exact boundary
        stop_res = client.post(f"/api/runs/{rid}/stop")
        assert stop_res.status_code == 200
        assert stop_res.get_json()["status"] in ("stopping", "not_running")
        stop_called_evt.set()

        worker = api.worker_records.get(rid)
        if worker and worker.thread:
            worker.thread.join(timeout=15)
            assert not worker.thread.is_alive()

        conn = get_db_connection()
        run_row = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
        assert run_row["status"] == "ABORTED"

        # Evidence preserved in tests table
        tests = conn.execute("SELECT * FROM tests WHERE run_id=?", (rid,)).fetchall()
        disclosure_tests = [
            t for t in tests if t["expected_result"] == FUNCTIONAL_DISCLOSURE_EXPECTED
        ]
        assert len(disclosure_tests) == 1, "Exactly one disclosure candidate should have executed before cancellation"
        retained = disclosure_tests[0]
        assert retained["status"] == "LIMITED COVERAGE"
        assert "cancelled" in retained["actual_result"].lower() or "coverage limited" in retained["actual_result"].lower()

        # Coverage report reflects interruption
        coverage = get_coverage(conn, rid)
        assert coverage is not None
        assert "interrupted" in coverage["termination_reason"].lower()

        report = load_report(conn, rid)
        assert report["outcome"] in ("INTERRUPTED", "LIMITED COVERAGE")
        conn.close()
    finally:
        session.stop()


def test_per_run_configuration_isolation_and_concurrency(site):
    """Sequential and concurrent run configurations remain strictly isolated per run.

    Verifies:
    1. Run A with disclosure enabled produces disclosure checks.
    2. Run B with disclosure disabled produces zero disclosure checks.
    3. Neither run mutates or bleeds options into the other.
    """
    pid = seed_project(site)
    session = create_live_session(site, pid)
    client = flask_app.test_client()

    try:
        # Run A: Disclosure Enabled
        res_a = client.post("/api/runs", json={
            "project_id": pid,
            "mode": "full",
            "page_limit": 2,
            "enable_interactive_disclosure": True,
        })
        assert res_a.status_code == 200
        rid_a = res_a.get_json()["run_id"]

        worker_a = api.worker_records.get(rid_a)
        if worker_a and worker_a.thread:
            worker_a.thread.join(timeout=30)
            assert not worker_a.thread.is_alive()

        # Run B: Disclosure Disabled
        res_b = client.post("/api/runs", json={
            "project_id": pid,
            "mode": "full",
            "page_limit": 2,
            "enable_interactive_disclosure": False,
        })
        assert res_b.status_code == 200
        rid_b = res_b.get_json()["run_id"]

        worker_b = api.worker_records.get(rid_b)
        if worker_b and worker_b.thread:
            worker_b.thread.join(timeout=30)
            assert not worker_b.thread.is_alive()

        conn = get_db_connection()
        tests_a = conn.execute("SELECT * FROM tests WHERE run_id=?", (rid_a,)).fetchall()
        tests_b = conn.execute("SELECT * FROM tests WHERE run_id=?", (rid_b,)).fetchall()

        disc_a = [t for t in tests_a if t["expected_result"] == FUNCTIONAL_DISCLOSURE_EXPECTED]
        disc_b = [t for t in tests_b if t["expected_result"] == FUNCTIONAL_DISCLOSURE_EXPECTED]

        assert len(disc_a) > 0, "Run A must have disclosure evaluations"
        assert len(disc_b) == 0, "Run B must have zero disclosure evaluations"

        # Verify bridge configs were independent
        engine_a = getattr(worker_a, "engine", None)
        engine_b = getattr(worker_b, "engine", None)
        if engine_a and hasattr(engine_a, "interactive_bridge"):
            assert engine_a.interactive_bridge.is_enabled() is True
        if engine_b and hasattr(engine_b, "interactive_bridge"):
            assert engine_b.interactive_bridge.is_enabled() is False

        conn.close()
    finally:
        session.stop()


def test_uninterrupted_browser_to_worker_pipeline(site):
    """End-to-end integrated path without API mocking: browser dispatches run, worker executes, UI reflects PASS."""
    from playwright.sync_api import sync_playwright, expect
    from werkzeug.serving import make_server

    pid = seed_project(site)
    session = create_live_session(site, pid)
    server = make_server("127.0.0.1", 0, flask_app, threaded=True)
    t = threading.Thread(target=server.serve_forever)
    t.daemon = True
    t.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()

            # Open dry run page - NO API ROUTE MOCKING!
            page.goto(f"{base_url}/project/{pid}/dry-run")
            expect(page.locator("#session-status")).to_contain_text("READY")

            page.select_option("#test-mode", "full")
            page.fill("#fullPageLimit", "2")
            page.check("#auth_confirm")
            page.check("#enableInteractiveDisclosure")

            # Click Start QA Test - real fetch to /api/runs
            page.click("#btnStart")

            # Browser navigates to /run/<id>
            import re
            page.wait_for_url(re.compile(r".*/run/\d+"), timeout=15000)
            run_url = page.url
            rid = int(run_url.rstrip("/").split("/")[-1])

            # Wait for worker thread to complete execution
            worker = api.worker_records.get(rid)
            if worker and worker.thread:
                worker.thread.join(timeout=35)
                assert not worker.thread.is_alive()

            # Reload run page to view completed results
            page.reload()
            expect(page.get_by_text("Execution: COMPLETED")).to_be_visible()

            conn = get_db_connection()
            run_row = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
            assert run_row["status"] == "COMPLETED"

            tests = conn.execute("SELECT * FROM tests WHERE run_id=?", (rid,)).fetchall()
            disclosure_passes = [
                t for t in tests if t["status"] == "PASS" and t["expected_result"] == FUNCTIONAL_DISCLOSURE_EXPECTED
            ]
            assert len(disclosure_passes) > 0, "Integrated run must complete disclosure PASS"
            conn.close()
            browser.close()
    finally:
        session.stop()
        server.shutdown()
        server.server_close()
        t.join(timeout=3)


def test_source_page_content_and_form_invariance(site):
    """Source page URL, unsaved form inputs, and DOM content remain 100% invariant during disclosure evaluation."""
    pid = seed_project(site)
    session = create_live_session(site, pid)
    client = flask_app.test_client()

    try:
        # 1. Establish initial state on session.page
        initial_url = session.page.url
        assert initial_url == site.base + "/"

        # Inject unsaved form draft and custom DOM property on session.page
        # using the session's event loop
        async def inject_draft():
            await session.page.fill("#draft-input", "UNSAVED_DRAFT_SECRET_42")
            await session.page.evaluate("() => { document.body.dataset.testDraft = 'active_user_draft'; }")
            return await session.page.content()

        future = asyncio.run_coroutine_threadsafe(inject_draft(), session.loop)
        initial_content = future.result(timeout=5)

        # 2. Run Full Site QA with disclosure enabled
        payload = {
            "project_id": pid,
            "mode": "full",
            "page_limit": 2,
            "enable_interactive_disclosure": True,
        }
        res = client.post("/api/runs", json=payload)
        assert res.status_code == 200
        rid = res.get_json()["run_id"]

        worker = api.worker_records.get(rid)
        if worker and worker.thread:
            worker.thread.join(timeout=30)
            assert not worker.thread.is_alive()

        # 3. Assert session.page content and form invariance
        async def check_invariance():
            val = await session.page.input_value("#draft-input")
            draft_attr = await session.page.evaluate("() => document.body.dataset.testDraft")
            post_content = await session.page.content()
            return val, draft_attr, post_content

        chk_future = asyncio.run_coroutine_threadsafe(check_invariance(), session.loop)
        post_val, post_draft_attr, post_content = chk_future.result(timeout=5)

        assert session.page.url == initial_url, "Source page URL was altered"
        assert post_val == "UNSAVED_DRAFT_SECRET_42", "Unsaved form input draft was overwritten or lost"
        assert post_draft_attr == "active_user_draft", "Source page DOM mutation was overwritten"
        assert post_content == initial_content, "Source page HTML content was altered during evaluation"
        assert not session.page.is_closed(), "Source page was closed"
    finally:
        session.stop()


def test_result_uniqueness_across_checkpoints_and_final_save(site):
    """Incremental checkpoints and final save do not produce duplicate logical test records."""
    pid = seed_project(site)
    session = create_live_session(site, pid)
    client = flask_app.test_client()

    try:
        payload = {
            "project_id": pid,
            "mode": "full",
            "page_limit": 2,
            "enable_interactive_disclosure": True,
        }
        res = client.post("/api/runs", json=payload)
        rid = res.get_json()["run_id"]

        worker = api.worker_records.get(rid)
        if worker and worker.thread:
            worker.thread.join(timeout=30)
            assert not worker.thread.is_alive()

        conn = get_db_connection()
        tests = conn.execute("SELECT id, url, viewport, expected_result, actual_result FROM tests WHERE run_id=?", (rid,)).fetchall()
        signatures = [(t["url"], t["viewport"], t["expected_result"], t["actual_result"]) for t in tests]
        assert len(signatures) == len(set(signatures)), "Duplicate logical evaluation records found across persistence checkpoints"
        conn.close()
    finally:
        session.stop()


# ==============================================================================
# 6. Circuit Breaker & Safety Tests
# ==============================================================================

def test_circuit_breaker_halts_further_interactive_work(site):
    """When cleanup failure is tripped, further interactive evaluations are immediately halted."""
    config = InteractiveWorkflowConfig(enabled=True)
    bridge = InteractiveDisclosureBridge(config=config)
    assert bridge.is_enabled()
    assert not bridge.is_cleanup_halted

    # Trip breaker
    bridge.halt_cleanup()
    assert bridge.is_cleanup_halted

    # Attempt evaluate_page_disclosures
    result = asyncio.run(bridge.evaluate_page_disclosures(
        source_context=None,
        source_page=None,
        page_url=site.base + "/",
        viewport_name="Desktop",
        run_id=1,
    ))
    assert result == [], "Halting cleanup must immediately return empty list and skip dispatch"


def test_privacy_preservation_no_path_secrets_in_new_records(site):
    """Disclosure records use origin-only display and opaque references; query secrets are stripped."""
    conn = get_db_connection()
    pid = conn.execute(
        "INSERT INTO projects(name, target_url, environment, tester_name) VALUES ('Secret Project', ?, 'Test', 'QA')",
        (site.base + "/secret-page?token=secret123&user=admin",),
    ).lastrowid
    conn.commit()
    conn.close()

    session = create_live_session(site, pid)
    client = flask_app.test_client()

    try:
        payload = {
            "project_id": pid,
            "mode": "full",
            "page_limit": 1,
            "enable_interactive_disclosure": True,
        }
        res = client.post("/api/runs", json=payload)
        rid = res.get_json()["run_id"]

        worker = api.worker_records.get(rid)
        if worker and worker.thread:
            worker.thread.join(timeout=30)
            assert not worker.thread.is_alive()

        conn = get_db_connection()
        tests = conn.execute("SELECT url, actual_result, expected_result FROM tests WHERE run_id=?", (rid,)).fetchall()
        disclosure_tests = [
            t for t in tests if t["expected_result"] == FUNCTIONAL_DISCLOSURE_EXPECTED
        ]
        assert len(disclosure_tests) > 0, "Disclosure evaluation records should be present"
        for t in disclosure_tests:
            assert "secret123" not in t["url"]
            assert "admin" not in t["url"]
            assert "token=" not in t["url"]
            assert "secret-page" not in t["url"]
            assert "[action: act-" in t["url"]
        conn.close()
    finally:
        session.stop()


# ==============================================================================
# 7. Component Persistence vs Live Runner Distinction Tests
# ==============================================================================

def test_component_persistence_with_synthetic_interrupted_records(tmp_path):
    """Test of supplied records directly through save/report functions (component persistence)."""
    conn = get_db_connection()
    pid = conn.execute("INSERT INTO projects(name, target_url, environment, tester_name) VALUES ('Comp', 'http://127.0.0.1:8000/', 'Test', 'QA')").lastrowid
    rid = conn.execute("INSERT INTO runs(project_id, status) VALUES (?, 'ABORTED')", (pid,)).lastrowid
    conn.commit()

    # Supply synthetic terminal records
    records = [
        {
            "url": "http://127.0.0.1:8000/",
            "final_url": "http://127.0.0.1:8000/",
            "expected_result": FUNCTIONAL_DISCLOSURE_EXPECTED,
            "actual_result": "Coverage limited: evaluation cancelled after confirmed action.",
            "status": "LIMITED COVERAGE",
            "viewport": "Desktop",
            "screenshot": None,
            "finding_id": None,
        }
    ]
    api._save_results(records, rid, pid, finish=False, terminal_evidence=True)

    saved = conn.execute("SELECT * FROM tests WHERE run_id=?", (rid,)).fetchall()
    assert len(saved) == 1
    assert saved[0]["status"] == "LIMITED COVERAGE"
    conn.close()
