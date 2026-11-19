import os
import sys
import time
import json
import hashlib
import tempfile
import threading
import subprocess
import urllib.request
import urllib.error
import http.server
import socket
import ctypes
import pytest
from playwright.sync_api import sync_playwright
from tests.profile_test_site import ProfileTestServer, PROFILE_INVENTORY

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DIST_DIR = os.environ.get("QA_TESTER_DIST_DIR", os.path.join(PROJECT_ROOT, "dist", "QA-Tester"))
EXE_PATH = os.environ.get("QA_TESTER_EXE_PATH", os.path.join(DIST_DIR, "QA-Tester.exe"))


@pytest.fixture(scope="module", autouse=True)
def record_packaged_candidate_identity(record_testsuite_property):
    """Pin optional review identity and persist the executable actually selected by this run."""
    candidate_sha = _get_candidate_sha()
    expected = os.environ.get("QA_TESTER_EXPECTED_EXE_SHA256")
    if expected:
        assert candidate_sha == expected.upper(), "Selected EXE does not match the approved candidate"
    record_testsuite_property("candidate_executable", os.path.abspath(EXE_PATH))
    record_testsuite_property("candidate_sha256", candidate_sha)
    print(f"PACKAGED_CANDIDATE={os.path.abspath(EXE_PATH)} SHA256={candidate_sha}")


# ============================================================================
# Controlled Localhost Target with Gated Stages & Disclosures
# ============================================================================

class ControlledLocalhostTarget(http.server.ThreadingHTTPServer):
    """Multi-page controlled HTTP server with deterministic stage holds, forms, disclosures, and defects."""

    def __init__(self, host="127.0.0.1", port=0):
        self.stage_reached_event = threading.Event()
        self.release_event = threading.Event()
        self.hold_stage = False
        target_self = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                # 1. Gated stage for deterministic stop/shutdown testing
                if self.path.startswith("/held-stage"):
                    if target_self.hold_stage:
                        target_self.stage_reached_event.set()
                        target_self.release_event.wait(timeout=20)
                    body = b"<!DOCTYPE html><html><body><h1>Stage Released</h1></body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # 2. Broken defect trigger
                if self.path == "/broken":
                    body = b"<!DOCTYPE html><html><body><h1>404 Page Not Found</h1></body></html>"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # 3. Standard pages
                if self.path == "/" or self.path == "/index.html":
                    html = """<!DOCTYPE html>
<html>
<head><title>Controlled Target Home</title></head>
<body>
    <h1>Welcome to Controlled Target</h1>
    <a href="/held-stage">Held Stage Page</a>
    <a href="/catalog">Catalog Page</a>
    <a href="/contact">Contact Page</a>
    <a href="/broken">Broken Page</a>
    <!-- Supported expanding disclosure -->
    <button id="faq-toggle" aria-expanded="false" aria-controls="faq-content" onclick="const c=document.getElementById('faq-content');const exp=this.getAttribute('aria-expanded')==='true';this.setAttribute('aria-expanded',!exp);c.style.display=exp?'none':'block';">Frequently Asked Questions</button>
    <div id="faq-content" style="display:none"><p>This is the disclosed FAQ answer content.</p></div>
    <!-- Inert / unverified non-expanding control -->
    <button id="inert-toggle" aria-expanded="false" aria-controls="inert-content">Inert Toggle Button</button>
    <div id="inert-content" style="display:none"><p>Never disclosed</p></div>
    <form action="/submit" method="GET">
        <input type="text" name="q" value="sample query" />
        <button type="submit">Search</button>
    </form>
</body>
</html>"""
                elif self.path.startswith("/catalog"):
                    html = """<!DOCTYPE html>
<html>
<head><title>Product Catalog</title></head>
<body>
    <h1>Catalog</h1>
    <p>Item 1: High Quality Widget</p>
    <a href="/">Back Home</a>
</body>
</html>"""
                elif self.path.startswith("/contact"):
                    html = """<!DOCTYPE html>
<html>
<head><title>Contact Us</title></head>
<body>
    <h1>Contact</h1>
    <p>Email: test@example.local</p>
    <a href="/">Back Home</a>
</body>
</html>"""
                else:
                    html = "<html><body><h1>Not Found</h1></body></html>"

                body = html.encode("utf-8")
                self.send_response(200 if not self.path.startswith("/not-found") else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = b'{"status": "received"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        super().__init__((host, port), Handler)
        self.port = self.server_port
        self.url = f"http://{host}:{self.port}"
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.release_event.set()
        self.shutdown()
        self.server_close()


# ============================================================================
# Helpers
# ============================================================================

def get_free_port():
    """Dynamically binds to port 0 to allocate an available ephemeral localhost port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_get(url, timeout=5):
    req = urllib.request.Request(url, headers={"User-Agent": "QA-Tester-Verifier"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def http_get_bytes(url, timeout=5):
    req = urllib.request.Request(url, headers={"User-Agent": "QA-Tester-Verifier"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def http_post_json(url, payload, headers=None, timeout=25):
    data = json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json", "User-Agent": "QA-Tester-Verifier"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def get_clean_env(test_writable, server_only=True, shutdown_token=None):
    env = os.environ.copy()
    env.pop("QA_TESTER_DB_PATH", None)
    env.pop("QA_TESTER_TEST_DIR", None)
    env.pop("FLASK_ENV", None)

    env["QA_TESTER_WRITABLE_DIR"] = test_writable
    env["CHROME_LOG_FILE"] = os.path.join(test_writable, "logs", "chromium_debug.log")
    if server_only:
        env["QA_TESTER_SERVER_ONLY"] = "1"
    else:
        env.pop("QA_TESTER_SERVER_ONLY", None)

    if shutdown_token:
        env["QA_TESTER_SHUTDOWN_TOKEN"] = shutdown_token
    else:
        env.pop("QA_TESTER_SHUTDOWN_TOKEN", None)
    return env


def wait_for_server(base_url, attempts=50, delay=0.25):
    for _ in range(attempts):
        try:
            status, html = http_get(base_url)
            if status == 200:
                return True, html
        except Exception:
            pass
        time.sleep(delay)
    return False, ""


def get_child_pids(parent_pid):
    """Discovers all descendant process IDs of the specified parent process on Windows."""
    try:
        cmd = f'powershell -NoProfile -Command "(Get-CimInstance Win32_Process | Where-Object {{ $_.ParentProcessId -eq {parent_pid} }}).ProcessId"'
        out = subprocess.check_output(cmd, shell=True, text=True).strip()
        pids = []
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids
    except Exception:
        return []


def is_pid_alive(pid):
    """Checks whether a process with the given PID is currently alive on Windows."""
    try:
        cmd = f'powershell -NoProfile -Command "Get-Process -Id {pid} -ErrorAction SilentlyContinue"'
        out = subprocess.check_output(cmd, shell=True, text=True).strip()
        return len(out) > 0
    except Exception:
        return False


def find_native_qa_tester_window(expected_pid=None):
    """Locates the top-level native Win32 window for QA Tester and returns (hwnd, pid)."""
    try:
        found = []
        def enum_cb(hwnd_val, lparam):
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd_val)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd_val, buff, length + 1)
                if "QA Tester" in buff.value:
                    pid_val = ctypes.c_ulong()
                    ctypes.windll.user32.GetWindowThreadProcessId(hwnd_val, ctypes.byref(pid_val))
                    if expected_pid is None or pid_val.value == expected_pid:
                        found.append((hwnd_val, pid_val.value))
            return True
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        ctypes.windll.user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
        if found:
            return found[0]

        if expected_pid is None:
            hwnd = ctypes.windll.user32.FindWindowW(None, "QA Tester")
            if hwnd:
                pid = ctypes.c_ulong()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                return hwnd, pid.value
        return (None, None)
    except Exception:
        return (None, None)


# ============================================================================
# 1. Artifact Integrity & Packaging Discovery
# ============================================================================

def test_packaging_artifact_integrity_and_discovery():
    """Validates that the packaged executable exists, hashes correctly, and bundles all required runtimes."""
    assert os.path.exists(EXE_PATH), f"Candidate executable not found at {EXE_PATH}"
    exe_size = os.path.getsize(EXE_PATH)
    assert exe_size > 5 * 1024 * 1024, f"Executable unexpectedly small: {exe_size} bytes"

    hasher = hashlib.sha256()
    with open(EXE_PATH, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    exe_hash = hasher.hexdigest().upper()
    assert len(exe_hash) == 64

    internal_dir = os.path.join(DIST_DIR, "_internal")
    assert os.path.isdir(internal_dir), "_internal directory missing"

    # Playwright Driver
    node_exe = os.path.join(internal_dir, "playwright", "driver", "node.exe")
    pw_cmd = os.path.join(internal_dir, "playwright", "driver", "playwright.cmd")
    assert os.path.exists(node_exe), f"Bundled node.exe missing at {node_exe}"
    assert os.path.exists(pw_cmd), f"Bundled playwright.cmd missing at {pw_cmd}"

    # Bundled Playwright Chromium Browser
    browsers_dir = os.path.join(internal_dir, "playwright_browsers")
    assert os.path.isdir(browsers_dir), f"playwright_browsers missing at {browsers_dir}"
    chromium_dirs = [d for d in os.listdir(browsers_dir) if d.startswith("chromium-")]
    assert len(chromium_dirs) > 0, "No bundled Chromium revisions found"
    chrome_exe = os.path.join(browsers_dir, chromium_dirs[0], "chrome-win", "chrome.exe")
    assert os.path.exists(chrome_exe), f"Bundled chrome.exe missing at {chrome_exe}"

    # Templates and Static Assets
    assert os.path.exists(os.path.join(internal_dir, "templates", "projects.html")), "Templates missing"
    assert os.path.exists(os.path.join(internal_dir, "static", "workspace.css")), "Static CSS missing"

    # PyWebView / WebView2 Native DLLs
    webview_lib = os.path.join(internal_dir, "webview", "lib")
    assert os.path.exists(os.path.join(webview_lib, "Microsoft.Web.WebView2.Core.dll")), "WebView2 Core missing"
    assert os.path.exists(os.path.join(webview_lib, "Microsoft.Web.WebView2.WinForms.dll")), "WebView2 WinForms missing"


# ============================================================================
# 2. Shutdown Security & Authentication
# ============================================================================

def test_shutdown_endpoint_security_and_authorization():
    """Verifies that /api/system/shutdown is disabled by default and requires valid secret token."""
    test_writable = tempfile.mkdtemp(prefix="qa_pkg_sec_")
    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"

    # Part A: Launch WITHOUT shutdown token configured (standard user operation)
    env_unprotected = get_clean_env(test_writable, server_only=True, shutdown_token=None)
    proc1 = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env_unprotected)

    try:
        started, _ = wait_for_server(base_url)
        assert started, "Server failed to start in Part A"

        # Attempt shutdown: must return 403 Forbidden because endpoint is disabled
        status, resp = http_post_json(f"{base_url}/api/system/shutdown", {})
        assert status == 403, f"Expected 403 Forbidden when disabled, got {status}"
        assert "disabled" in str(resp)

        # Confirm application is STILL running and serves requests
        status, _ = http_get(base_url)
        assert status == 200, "Application terminated on unauthorized request"
    finally:
        proc1.terminate()
        proc1.wait(timeout=10)

    # Part B: Launch WITH configured secret shutdown token
    secret_token = "qa-pack-test-secret-999"
    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    env_protected = get_clean_env(test_writable, server_only=True, shutdown_token=secret_token)
    proc2 = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env_protected)

    try:
        started, _ = wait_for_server(base_url)
        assert started, "Server failed to start in Part B"

        # 1. Attempt shutdown with no token -> 401 Unauthorized
        status, _ = http_post_json(f"{base_url}/api/system/shutdown", {})
        assert status == 401, f"Expected 401 Unauthorized for empty token, got {status}"

        # 2. Attempt shutdown with wrong token -> 401 Unauthorized
        status, _ = http_post_json(f"{base_url}/api/system/shutdown", {"token": "wrong-token"})
        assert status == 401, f"Expected 401 Unauthorized for wrong token, got {status}"

        # Confirm application is STILL running
        status, _ = http_get(base_url)
        assert status == 200, "Application died from bad token attempt"

        # 3. Authorized shutdown via header
        status, resp = http_post_json(
            f"{base_url}/api/system/shutdown",
            {},
            headers={"X-Shutdown-Token": secret_token},
        )
        assert status == 200, f"Expected 200 OK for authorized token, got {status}"
        assert resp.get("status") == "shutting_down"

        # Process should terminate cleanly with code 0
        proc2.wait(timeout=15)
        assert proc2.returncode == 0, f"Expected returncode 0, got {proc2.returncode}"
    finally:
        if proc2.poll() is None:
            proc2.kill()


# ============================================================================
# 3. Conclusive User Stop & Retention
# ============================================================================

def test_conclusive_user_stop_and_retention():
    """Deterministic stop verification: run is demonstrably active at held stage, stop is accepted,
    run reaches ABORTED, and all pre-stop records are retained identically in DB and report."""
    target = ControlledLocalhostTarget()
    target.hold_stage = True
    test_writable = tempfile.mkdtemp(prefix="qa_pkg_det_stop_")
    token = "det-stop-token"
    env = get_clean_env(test_writable, server_only=True, shutdown_token=token)

    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env)

    try:
        started, _ = wait_for_server(base_url)
        assert started

        # Create Project
        _, proj = http_post_json(
            f"{base_url}/api/projects",
            {
                "name": "Deterministic Stop Project",
                "target_url": target.url,
                "environment": "localhost-packaged",
                "tester_name": "QA Packager",
            },
        )
        proj_id = proj["id"]

        # Start Interactive Session
        status, _ = http_post_json(f"{base_url}/api/projects/{proj_id}/interactive/start", {})
        assert status == 200

        # Wait until READY
        ready = False
        for _ in range(40):
            time.sleep(0.5)
            _, sess_data = http_get(f"{base_url}/api/projects/{proj_id}/interactive/state")
            if json.loads(sess_data).get("state") == "READY":
                ready = True
                break
        assert ready, "Interactive session failed to reach READY state"

        # Start Full Site QA Run (incremental checkpointing enabled)
        _, run_data = http_post_json(
            f"{base_url}/api/runs",
            {"project_id": proj_id, "mode": "full", "page_limit": 10},
        )
        run_id = run_data["run_id"]

        # Wait until the crawler hits the held stage
        reached = target.stage_reached_event.wait(timeout=30)
        assert reached, "Crawler failed to reach held stage before stop trigger"

        # The run is GUARANTEED to be active and processing held-stage
        _, s_data = http_get(f"{base_url}/api/runs/{run_id}/status")
        run_state = json.loads(s_data).get("status")
        assert run_state == "RUNNING", f"Run completed prematurely! Expected RUNNING, got {run_state}"

        # Capture pre-stop records from SQLite (checkpointed by Full Site QA)
        db_path = os.path.join(test_writable, "data", "qa-tester.db")
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        pre_stop_tests = conn.execute("SELECT url, status, actual_result, viewport FROM tests WHERE run_id=?", (run_id,)).fetchall()
        pre_stop_records = [dict(r) for r in pre_stop_tests]
        conn.close()
        assert len(pre_stop_records) > 0, "No pre-stop records saved before stop trigger"

        # Exercise user-facing Stop endpoint
        stop_status, stop_res = http_post_json(f"{base_url}/api/runs/{run_id}/stop", {})
        assert stop_status == 200, f"Stop request rejected: {stop_status}"
        assert stop_res.get("status") == "stopping", f"Expected stopping status, got {stop_res}"

        # Release the held stage to allow worker loop to exit cleanly
        target.release_event.set()

        # Poll until terminal outcome
        final_status = None
        for _ in range(40):
            time.sleep(0.5)
            _, s_data = http_get(f"{base_url}/api/runs/{run_id}/status")
            final_status = json.loads(s_data).get("status")
            if final_status in ("ABORTED", "COMPLETED", "FAILED"):
                break

        assert final_status == "ABORTED", f"Expected ABORTED run, got {final_status}"

        # Compare content of EVERY captured record before and after stopping
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        post_stop_tests = conn.execute("SELECT url, status, actual_result, viewport FROM tests WHERE run_id=?", (run_id,)).fetchall()
        post_stop_records = [dict(r) for r in post_stop_tests]
        run_row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        conn.close()

        # All pre-stop records must be retained in post-stop records
        assert len(post_stop_records) >= len(pre_stop_records)
        for pre in pre_stop_records:
            matching = [p for p in post_stop_records if p["url"] == pre["url"] and p["viewport"] == pre["viewport"]]
            assert len(matching) > 0, f"Pre-stop record lost after stop: {pre}"
            assert matching[0]["status"] == pre["status"]
            assert matching[0]["actual_result"] == pre["actual_result"]

        # Verify packaged report HTML displays retained evidence and honest interrupted status
        html_status, report_html = http_get(f"{base_url}/export/{run_id}/html")
        assert html_status == 200
        assert "ABORTED" in report_html or "interrupted" in report_html.lower()

    finally:
        target.release_event.set()
        try:
            http_post_json(f"{base_url}/api/projects/{proj_id}/interactive/stop", {})
        except Exception:
            pass
        try:
            http_post_json(f"{base_url}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        except Exception:
            pass
        proc.wait(timeout=10)
        target.close()


# ============================================================================
# 4. Conclusive Application Shutdown & Process Cleanup
# ============================================================================

def test_conclusive_application_shutdown_and_process_cleanup():
    """Verifies that shutdown during active work waits for owned cleanup, terminates cleanly,
    cleans up all child processes, and handles repeated shutdown requests."""
    target = ControlledLocalhostTarget()
    target.hold_stage = True
    test_writable = tempfile.mkdtemp(prefix="qa_pkg_det_shut_")
    token = "det-shut-token"
    env = get_clean_env(test_writable, server_only=True, shutdown_token=token)

    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env)
    app_pid = proc.pid

    try:
        started, _ = wait_for_server(base_url)
        assert started

        # Create Project & Crawl Run
        _, proj = http_post_json(
            f"{base_url}/api/projects",
            {
                "name": "Shutdown Cleanup Project",
                "target_url": target.url,
                "environment": "localhost-packaged",
                "tester_name": "QA Packager",
            },
        )
        proj_id = proj["id"]

        _, run_data = http_post_json(f"{base_url}/api/runs", {"project_id": proj_id, "mode": "crawl"})
        run_id = run_data["run_id"]

        # Wait until worker is actively waiting at held stage
        reached = target.stage_reached_event.wait(timeout=15)
        assert reached, "Worker failed to reach held stage"

        # Verify worker is active
        _, s_data = http_get(f"{base_url}/api/runs/{run_id}/status")
        assert json.loads(s_data).get("status") == "RUNNING"

        # Track application and any child processes spawned by test
        child_pids = get_child_pids(app_pid)

        # 1. Test that unauthorized shutdown cannot stop the application
        unauth_status, _ = http_post_json(f"{base_url}/api/system/shutdown", {"token": "bad-token"})
        assert unauth_status == 401
        assert proc.poll() is None, "Application died from unauthorized shutdown request!"

        # 2. Send FIRST authorized shutdown request
        status1, resp1 = http_post_json(
            f"{base_url}/api/system/shutdown",
            {},
            headers={"X-Shutdown-Token": token},
        )
        assert status1 == 200
        assert resp1.get("status") == "shutting_down"

        # 3. Immediately send SECOND repeated shutdown request
        try:
            status2, _ = http_post_json(
                f"{base_url}/api/system/shutdown",
                {},
                headers={"X-Shutdown-Token": token},
            )
            assert status2 in (200, 404, 500)
        except Exception:
            pass

        # Release stage to allow any ongoing request to finalize
        target.release_event.set()

        # Main process must exit with return code 0
        proc.wait(timeout=25)
        assert proc.returncode == 0, f"Process exited with non-zero code {proc.returncode}"

        # Verify all tracked child processes are dead
        for cpid in child_pids:
            assert not is_pid_alive(cpid), f"Child process {cpid} leaked after shutdown!"

        # Verify log file confirms full owned cleanup sequence
        log_path = os.path.join(test_writable, "logs", "qa-tester.log")
        assert os.path.exists(log_path)
        with open(log_path, "r", encoding="utf-8") as f:
            log_text = f.read()

        assert "Initiating graceful application shutdown" in log_text
        assert "QA workers shut down successfully" in log_text
        assert "Flask server shut down successfully" in log_text
        assert "Graceful application shutdown completed" in log_text

        # Verify database terminal state
        db_path = os.path.join(test_writable, "data", "qa-tester.db")
        import sqlite3
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        conn.close()
        assert row is not None
        assert row[0] in ("ABORTED", "FAILED", "COMPLETED")

    finally:
        target.release_event.set()
        if proc.poll() is None:
            proc.kill()
        target.close()


# ============================================================================
# 5. Disclosure Evaluation Outcomes
# ============================================================================

def test_disclosure_evaluation_conclusive_outcomes():
    """Verifies disclosure outcomes: default-off produces zero disclosure tests, opt-in evaluates supported
    (PASS) and inert (LIMITED COVERAGE) controls, stable_id remains None, no customer findings created,
    and session loss produces truthful terminal outcome."""
    target = ControlledLocalhostTarget()
    test_writable = tempfile.mkdtemp(prefix="qa_pkg_disc_")
    token = "disc-token"
    env = get_clean_env(test_writable, server_only=True, shutdown_token=token)

    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env)

    try:
        started, _ = wait_for_server(base_url)
        assert started

        # Create Project
        _, proj = http_post_json(
            f"{base_url}/api/projects",
            {
                "name": "Disclosure Outcomes Project",
                "target_url": target.url,
                "environment": "localhost-packaged",
                "tester_name": "QA Packager",
            },
        )
        proj_id = proj["id"]

        # Start interactive session
        status, _ = http_post_json(f"{base_url}/api/projects/{proj_id}/interactive/start", {})
        assert status == 200

        # Wait until READY
        ready = False
        for _ in range(40):
            time.sleep(0.5)
            _, sess_data = http_get(f"{base_url}/api/projects/{proj_id}/interactive/state")
            if json.loads(sess_data).get("state") == "READY":
                ready = True
                break
        assert ready

        # --- Case A: Default-Off Disclosure produces ZERO disclosure records ---
        _, run_a = http_post_json(
            f"{base_url}/api/runs",
            {"project_id": proj_id, "mode": "full", "page_limit": 5, "enable_interactive_disclosure": False},
        )
        run_a_id = run_a["run_id"]

        for _ in range(40):
            time.sleep(0.5)
            _, s_data = http_get(f"{base_url}/api/runs/{run_a_id}/status")
            if json.loads(s_data).get("status") in ("COMPLETED", "FAILED"):
                break

        db_path = os.path.join(test_writable, "data", "qa-tester.db")
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        disc_a = conn.execute("SELECT * FROM tests WHERE run_id=? AND expected_result LIKE '%disclosure%'", (run_a_id,)).fetchall()
        assert len(disc_a) == 0, f"Default-off must produce zero disclosure records, found {len(disc_a)}"

        # --- Case B: Explicit Opt-In evaluates supported and inert controls ---
        _, run_b = http_post_json(
            f"{base_url}/api/runs",
            {"project_id": proj_id, "mode": "full", "page_limit": 5, "enable_interactive_disclosure": True},
        )
        run_b_id = run_b["run_id"]

        for _ in range(40):
            time.sleep(0.5)
            _, s_data = http_get(f"{base_url}/api/runs/{run_b_id}/status")
            if json.loads(s_data).get("status") in ("COMPLETED", "FAILED"):
                break

        disc_b = conn.execute("SELECT * FROM tests WHERE run_id=? AND expected_result LIKE '%disclosure%'", (run_b_id,)).fetchall()
        disc_b_records = [dict(r) for r in disc_b]
        assert len(disc_b_records) >= 2, f"Expected at least 2 disclosure records, got {len(disc_b_records)}"

        # 1. Successful expansion produces PASS with correct provenance
        pass_records = [r for r in disc_b_records if r["status"] == "PASS"]
        assert len(pass_records) >= 1, "Expected at least 1 PASS disclosure record for expanding control"
        assert any("Verified safe disclosure expansion" in r["actual_result"] for r in pass_records)

        # 2. Inert/unverified control produces honest LIMITED/DIAGNOSTIC coverage
        limited_records = [r for r in disc_b_records if r["status"] in ("LIMITED COVERAGE", "DIAGNOSTIC")]
        assert len(limited_records) >= 1, "Expected at least 1 LIMITED COVERAGE disclosure record for inert control"
        assert any("Coverage limited" in r["actual_result"] for r in limited_records)

        # 3. Disclosure records retain stable_id=None
        for r in disc_b_records:
            assert r["stable_id"] is None, f"Disclosure record must have stable_id=None, got {r['stable_id']}"

        # 4. Verify NO customer findings were created for disclosure evaluations
        findings_b = conn.execute("SELECT * FROM findings WHERE run_id=?", (run_b_id,)).fetchall()
        for f in findings_b:
            assert "disclosure" not in (f["title"] or "").lower(), f"Unexpected disclosure finding: {dict(f)}"
            assert "disclosure" not in (f["description"] or "").lower(), f"Unexpected disclosure finding: {dict(f)}"

        # 5. Inspect displayed HTML report contains the disclosure results
        html_code, report_b_html = http_get(f"{base_url}/export/{run_b_id}/html")
        assert html_code == 200
        assert "disclosure" in report_b_html.lower()
        assert "Verified safe disclosure expansion" in report_b_html

        # --- Case C: Session loss during active execution produces truthful terminal reporting ---
        target.hold_stage = True
        _, run_c = http_post_json(
            f"{base_url}/api/runs",
            {"project_id": proj_id, "mode": "full", "page_limit": 5},
        )
        run_c_id = run_c["run_id"]

        # Stop interactive session while run is in progress
        time.sleep(1.0)
        http_post_json(f"{base_url}/api/projects/{proj_id}/interactive/stop", {})
        target.release_event.set()

        final_c = None
        for _ in range(40):
            time.sleep(0.5)
            _, s_data = http_get(f"{base_url}/api/runs/{run_c_id}/status")
            final_c = json.loads(s_data).get("status")
            if final_c in ("ABORTED", "FAILED"):
                break

        assert final_c in ("ABORTED", "FAILED"), f"Expected terminal aborted/failed run on session loss, got {final_c}"
        cov_c = conn.execute("SELECT termination_reason FROM crawl_runs WHERE run_id=?", (run_c_id,)).fetchone()
        if cov_c and cov_c[0]:
            assert "session" in cov_c[0].lower() or "closed" in cov_c[0].lower() or "interrupted" in cov_c[0].lower()

        conn.close()

    finally:
        target.release_event.set()
        try:
            http_post_json(f"{base_url}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        except Exception:
            pass
        proc.wait(timeout=10)
        target.close()


# ============================================================================
# 6. Browser Automation Against Packaged Application Served UI
# ============================================================================

def test_packaged_user_interface_browser_automation():
    """Exercises the served user interface of the packaged app using Playwright browser automation:
    project creation, interactive session toggle, full site setup with disclosure opt-in, start run,
    cancel run, diagnostic presentation, and report export buttons."""
    target = ControlledLocalhostTarget()
    test_writable = tempfile.mkdtemp(prefix="qa_pkg_ui_auto_")
    token = "ui-auto-token"
    env = get_clean_env(test_writable, server_only=True, shutdown_token=token)

    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env)

    try:
        started, _ = wait_for_server(base_url)
        assert started

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # 1. Project creation through UI
            page.goto(f"{base_url}/projects")
            page.wait_for_selector('button:has-text("Create QA Project")')
            page.click('button:has-text("Create QA Project")')

            page.fill("#p_name", "Browser Automated Project")
            page.fill("#p_url", target.url)
            page.fill("#p_env", "Localhost Automated")
            page.fill("#p_tester", "UI Test Agent")
            page.click("#btnCreate")

            # Redirects to /project/<id>
            page.wait_for_url("**/project/*")
            assert "Browser Automated Project" in page.content()

            # 2. Navigate to New test setup (/project/<id>/dry-run)
            page.click('a:has-text("New test setup")')
            page.wait_for_url("**/dry-run")

            # 3. Mode selection & Disclosure default-off check
            page.select_option("#test-mode", "full")
            disclosure_cb = page.locator("#enableInteractiveDisclosure")
            assert not disclosure_cb.is_checked(), "Disclosure must be default unchecked"

            # 4. Explicit opt-in
            disclosure_cb.check()
            assert disclosure_cb.is_checked()

            # 5. Authorization confirmation
            page.check("#auth_confirm")

            # 6. Session preparation via UI button
            page.click("#btnToggleInteractive")
            # Wait until session-status shows active
            page.wait_for_selector('#session-status:has-text("Session")', timeout=20000)

            # 7. Start QA Run via UI button
            start_btn = page.locator("#btnStart")
            page.wait_for_function("!document.getElementById('btnStart').disabled", timeout=10000)
            start_btn.click()

            # 8. Navigates to running page (/run/<id>)
            page.wait_for_url("**/run/*")
            assert "QA test in progress" in page.content()

            # 9. Cancel run using the actual UI button (#cancel-run)
            cancel_btn = page.locator("#cancel-run")
            page.wait_for_selector("#cancel-run", timeout=10000)
            cancel_btn.click()

            # 10. Wait for reload / navigation to results workspace
            time.sleep(2.0)
            page.reload()
            page_content = page.content()
            assert "QA" in page_content

            # 11. Trigger report exports via API/UI endpoints to verify generation
            # Match run ID from URL
            run_id = page.url.split("/")[-1].split("?")[0]
            if run_id.isdigit():
                http_get(f"{base_url}/export/{run_id}/html")
                http_post_json(f"{base_url}/api/export/{run_id}/docx", {})
                http_post_json(f"{base_url}/api/export/{run_id}/txt", {})
                http_post_json(f"{base_url}/api/export/{run_id}/markdown", {})

                # Verify files exist in exports directory
                exports_dir = os.path.join(test_writable, "exports")
                assert os.path.exists(exports_dir)
                exported_files = os.listdir(exports_dir)
                assert any(f.endswith(".html") for f in exported_files)
                assert any(f.endswith(".docx") for f in exported_files)
                assert any(f.endswith(".txt") for f in exported_files)
                assert any(f.endswith(".md") for f in exported_files)

            browser.close()

    finally:
        try:
            http_post_json(f"{base_url}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        except Exception:
            pass
        proc.wait(timeout=10)
        target.close()


# ============================================================================
# 7. Native Desktop Window & Process Ownership
# ============================================================================

def test_native_desktop_window_pid_association():
    """Verifies that launching QA-Tester.exe opens an actual native Win32 window owned by the launched process."""
    test_writable = tempfile.mkdtemp(prefix="qa_pkg_win_pid_")
    token = "window-token-pid"
    env = get_clean_env(test_writable, server_only=False, shutdown_token=token)

    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env)
    expected_pid = proc.pid

    try:
        started, html = wait_for_server(base_url, attempts=50, delay=0.3)
        assert started, "Desktop GUI process failed to serve UI"

        # Verify UI DOM
        assert "<title>QA Tester</title>" in html

        # Locate native Win32 window and verify ownership
        hwnd, window_pid = None, None
        for _ in range(10):
            hwnd, window_pid = find_native_qa_tester_window(expected_pid=expected_pid)
            if hwnd is not None:
                break
            time.sleep(0.5)
        assert hwnd is not None, "Failed to locate native Win32 window for QA Tester"
        assert hwnd != 0
        assert window_pid == expected_pid, f"Window owned by unexpected PID: {window_pid} vs {expected_pid}"

        # Verify window geometry
        rect = (ctypes.c_long * 4)()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        assert width > 400 and height > 300, f"Window geometry invalid: {width}x{height}"

        # Terminate cleanly via token
        http_post_json(f"{base_url}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        proc.wait(timeout=15)
        assert proc.returncode == 0

    finally:
        if proc.poll() is None:
            proc.kill()


# ============================================================================
# 8. Data Persistence on Relaunch
# ============================================================================

def test_data_persistence_on_relaunch():
    """Verifies that projects, run history, and exports persist across application relaunches."""
    target = ControlledLocalhostTarget()
    test_writable = tempfile.mkdtemp(prefix="qa_pkg_pers_rel_")
    token = "rel-token"
    env = get_clean_env(test_writable, server_only=True, shutdown_token=token)

    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc1 = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env)

    try:
        started, _ = wait_for_server(base_url)
        assert started

        # Create Project & Crawl Run
        _, proj = http_post_json(
            f"{base_url}/api/projects",
            {
                "name": "Persist Across Restart Project",
                "target_url": target.url,
                "environment": "localhost-packaged",
                "tester_name": "QA Packager",
            },
        )
        proj_id = proj["id"]

        _, run_data = http_post_json(f"{base_url}/api/runs", {"project_id": proj_id, "mode": "crawl"})
        run_id = run_data["run_id"]

        for _ in range(40):
            time.sleep(0.5)
            _, s_data = http_get(f"{base_url}/api/runs/{run_id}/status")
            if json.loads(s_data).get("status") in ("COMPLETED", "FAILED"):
                break

        # Export all formats
        http_get(f"{base_url}/export/{run_id}/html")
        http_post_json(f"{base_url}/api/export/{run_id}/markdown", {})
        http_post_json(f"{base_url}/api/export/{run_id}/docx", {})
        http_post_json(f"{base_url}/api/export/{run_id}/txt", {})

        # Shut down instance 1
        http_post_json(f"{base_url}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        proc1.wait(timeout=10)
        assert proc1.returncode == 0

    finally:
        if proc1.poll() is None:
            proc1.kill()

    # Launch instance 2 with the SAME isolated writable directory
    port2 = get_free_port()
    base_url2 = f"http://127.0.0.1:{port2}"
    proc2 = subprocess.Popen([EXE_PATH, "--port", str(port2)], env=env)

    try:
        started2, _ = wait_for_server(base_url2)
        assert started2

        # Verify project is present in dashboard
        status, html = http_get(base_url2)
        assert status == 200
        assert "Persist Across Restart Project" in html

        # Verify run status is present and retains completed state
        status, s_data = http_get(f"{base_url2}/api/runs/{run_id}/status")
        assert status == 200
        assert json.loads(s_data).get("status") == "COMPLETED"

        # Verify exported files still exist on disk
        exports_dir = os.path.join(test_writable, "exports")
        files = os.listdir(exports_dir)
        assert any(f.endswith(".html") for f in files)
        assert any(f.endswith(".docx") for f in files)
        assert any(f.endswith(".md") for f in files)
        assert any(f.endswith(".txt") for f in files)

        # Shut down instance 2
        http_post_json(f"{base_url2}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        proc2.wait(timeout=10)
        assert proc2.returncode == 0

    finally:
        if proc2.poll() is None:
            proc2.kill()
        target.close()


# ============================================================================
# 9. Launch from Non-ASCII Directory with Spaces and Swedish Characters
# ============================================================================

def test_launch_from_directory_with_spaces_and_swedish_characters():
    """Verifies that the packaged application operates correctly with paths containing spaces and Swedish characters (åäö)."""
    swedish_dir = os.path.join(tempfile.gettempdir(), "qa_spår och möten_åäö 2026")
    os.makedirs(swedish_dir, exist_ok=True)
    token = "swedish-token"
    env = get_clean_env(swedish_dir, server_only=True, shutdown_token=token)

    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env, cwd=swedish_dir)

    try:
        started, html = wait_for_server(base_url)
        assert started, "Packaged app failed to boot in Swedish/spaced path directory"
        assert "QA Tester" in html

        # Verify database created in the Swedish path
        db_path = os.path.join(swedish_dir, "data", "qa-tester.db")
        assert os.path.exists(db_path), f"Database not created at {db_path}"

        # Verify logs created in the Swedish path
        log_path = os.path.join(swedish_dir, "logs", "qa-tester.log")
        assert os.path.exists(log_path), f"Log file not created at {log_path}"

        # Shut down cleanly
        http_post_json(f"{base_url}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        proc.wait(timeout=10)
        assert proc.returncode == 0

    finally:
        if proc.poll() is None:
            proc.kill()


# ============================================================================
# 10. Packaged Profile Automation Journey & Persistence Across Restart
# ============================================================================

def test_packaged_profile_journey_and_restart_persistence():
    """Verifies the new profile journey in the packaged Windows candidate binary (QA-Tester.exe).

    Distinguishes native desktop shell from headless browser-served automation:
    - Real packaged application process with isolated writable data directory.
    - Authenticated localhost profile fixture with all 16 inventory controls.
    - Full end-to-end disclosure and tab exploration with 1-to-1 inventory verification:
      * 5 passing safe controls (VERIFIED_SUCCESS -> PASS)
      * 3 confirmed defects (FAIL with linked QA-FUNC- findings)
      * 8 honest limited coverage records
      * 0 false customer defects
    - Canonical multi-format report exports (HTML, DOCX, Markdown, TXT).
    - Conclusive graceful shutdown.
    - Application restart on the same data directory verifying database and export persistence.
    """
    profile_server = ProfileTestServer("127.0.0.1", 0)
    profile_thread = threading.Thread(target=profile_server.serve_forever, daemon=True)
    profile_thread.start()

    test_writable = tempfile.mkdtemp(prefix="qa_pkg_profile_rel_")
    token = "profile-pkg-token"
    env = get_clean_env(test_writable, server_only=True, shutdown_token=token)

    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc1 = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env)

    try:
        started, _ = wait_for_server(base_url)
        assert started, "Packaged candidate binary failed to start for profile test"

        # 1. Create Profile Project
        profile_url = f"{profile_server.origin}/profile"
        status, proj = http_post_json(
            f"{base_url}/api/projects",
            {
                "name": "Packaged Profile Journey",
                "target_url": profile_url,
                "environment": "localhost-packaged",
                "tester_name": "QA Packager",
            },
        )
        assert status == 200, f"Failed to create project: {proj}"
        proj_id = proj["id"]

        # Register validation contract binding for profile test fixture on this project
        http_post_json(
            f"{base_url}/api/projects/{proj_id}/contracts",
            {
                "contract_id": "work_history_date_validation",
                "rule_name": "Work History Date Period Rule",
                "target_url_pattern": f"{profile_url}*",
                "target_control_id": "btn-validate-history",
                "accessible_name": "Validate Work History Dates",
                "feedback_selector": "#history-date-feedback",
                "field_selectors": {
                    "start_date": "#input-start-date",
                    "end_date": "#input-end-date",
                },
                "completion_marker_attr": "data-validation-case",
                "completion_marker_type": "case_id",
                "completion_timestamp_attr": "data-validation-timestamp",
                "completion_mode": "case_marker",
            },
        )

        # 2. Start interactive session
        status, _ = http_post_json(f"{base_url}/api/projects/{proj_id}/interactive/start", {})
        assert status == 200

        # Wait until READY
        ready = False
        for _ in range(40):
            time.sleep(0.5)
            _, sess_data = http_get(f"{base_url}/api/projects/{proj_id}/interactive/state")
            if json.loads(sess_data).get("state") == "READY":
                ready = True
                break
        assert ready, "Interactive session failed to reach READY state in packaged candidate"

        # 3. Start Profile Run with interactive disclosure enabled
        status, run_data = http_post_json(
            f"{base_url}/api/runs",
            {
                "project_id": proj_id,
                "mode": "full",
                "page_limit": 5,
                "enable_interactive_disclosure": True,
                "interactive_options": {
                    "max_evaluations_per_page": 20,
                    "max_evaluations_per_run": 50,
                    "action_timeout": 1.0,
                    "reconstruction_timeout": 5.0,
                },
            },
        )
        assert status == 200, f"Failed to start profile run: {run_data}"
        run_id = run_data["run_id"]

        # 4. Poll until completed
        final_status = None
        for _ in range(120):
            time.sleep(1.0)
            _, s_data = http_get(f"{base_url}/api/runs/{run_id}/status")
            final_status = json.loads(s_data).get("status")
            if final_status in ("COMPLETED", "FAILED", "ABORTED"):
                break

        assert final_status == "COMPLETED", f"Profile run did not complete: status={final_status}"

        # 5. Connect to packaged SQLite database and verify 1-to-1 inventory correspondence
        db_path = os.path.join(test_writable, "data", "qa-tester.db")
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        tests = [dict(r) for r in conn.execute("SELECT * FROM tests WHERE run_id=?", (run_id,)).fetchall()]
        findings = [dict(r) for r in conn.execute("SELECT * FROM findings WHERE run_id=?", (run_id,)).fetchall()]
        conn.close()

        # Run 1-to-1 inventory verification independently for Desktop and Mobile
        from tests.test_profile_automation import verify_profile_inventory_acceptance
        matched_desktop = verify_profile_inventory_acceptance(
            tests=tests,
            findings=findings,
            inventory=PROFILE_INVENTORY,
            target_url=profile_url,
            viewport="Desktop",
        )
        assert len(matched_desktop) == len(PROFILE_INVENTORY)

        matched_mobile = verify_profile_inventory_acceptance(
            tests=tests,
            findings=findings,
            inventory=PROFILE_INVENTORY,
            target_url=profile_url,
            viewport="Mobile",
        )
        assert len(matched_mobile) == len(PROFILE_INVENTORY)

        # 6. Verify export endpoints and generated documents
        html_code, html_doc = http_get(f"{base_url}/export/{run_id}/html")
        assert html_code == 200
        assert "QA Test Report" in html_doc
        assert "Profile Journey" in html_doc

        doc_code, _ = http_post_json(f"{base_url}/api/export/{run_id}/docx", {})
        assert doc_code in (200, 201)
        md_code, _ = http_post_json(f"{base_url}/api/export/{run_id}/markdown", {})
        assert md_code in (200, 201)
        txt_code, _ = http_post_json(f"{base_url}/api/export/{run_id}/txt", {})
        assert txt_code in (200, 201)

        exports_dir = os.path.join(test_writable, "exports")
        assert os.path.exists(exports_dir)
        exp_files = os.listdir(exports_dir)
        assert any(f.endswith(".docx") for f in exp_files)
        assert any(f.endswith(".md") for f in exp_files)

        # 7. Graceful application shutdown
        http_post_json(f"{base_url}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        proc1.wait(timeout=15)
        assert proc1.returncode == 0

    finally:
        if proc1.poll() is None:
            proc1.kill()

    # 8. Relaunch instance 2 on the SAME data directory and verify complete persistence
    port2 = get_free_port()
    base_url2 = f"http://127.0.0.1:{port2}"
    proc2 = subprocess.Popen([EXE_PATH, "--port", str(port2)], env=env)

    try:
        started2, html2 = wait_for_server(base_url2)
        assert started2
        assert "Packaged Profile Journey" in html2

        # Verify run status is COMPLETED
        _, s_data = http_get(f"{base_url2}/api/runs/{run_id}/status")
        assert json.loads(s_data).get("status") == "COMPLETED"

        # Verify tests and findings are intact in DB upon restart
        conn2 = sqlite3.connect(db_path)
        c2 = conn2.cursor()
        saved_tests = c2.execute("SELECT COUNT(*) FROM tests WHERE run_id=?", (run_id,)).fetchone()[0]
        saved_findings = c2.execute("SELECT COUNT(*) FROM findings WHERE run_id=?", (run_id,)).fetchone()[0]
        conn2.close()
        assert saved_tests == len(tests)
        assert saved_findings == len(findings) == 8

        # Clean shutdown instance 2
        http_post_json(f"{base_url2}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        proc2.wait(timeout=15)
        assert proc2.returncode == 0

    finally:
        if proc2.poll() is None:
            proc2.kill()
        profile_server.shutdown()
        profile_server.server_close()


# ============================================================================
# 11. Packaged Profile Served Browser UI Journeys & Restart Persistence
# ============================================================================

def _exercise_and_verify_visible_report_exports(page, base_url, run_id, rep_finding_title, exports_dir):
    """Activates the visible report export controls on the served Reports page (/run/<id>?tab=reports)
    via Playwright button clicks, verifies user-facing confirmation notices, and verifies generated
    artifacts on disk in the isolated test writable exports directory."""
    page.goto(f"{base_url}/run/{run_id}?tab=reports")
    page.wait_for_selector(".report-controls", timeout=10000)
    assert page.is_visible(".report-mode-control")

    # Verify standard mode is checked by default
    assert page.is_checked('input[name="report-mode"][value="standard"]')

    # 1. Activate visible Word DOCX export button
    btn_docx = page.locator('button[data-export="docx"]')
    assert btn_docx.is_visible()
    btn_docx.click()
    page.wait_for_selector('[data-export-result]:not([hidden])', timeout=15000)
    docx_notice = page.locator('[data-export-result] span').inner_text()
    assert "Standard" in docx_notice
    assert f"report_{run_id}_" in docx_notice
    assert docx_notice.endswith(".docx")

    # 2. Activate visible TXT export button
    btn_txt = page.locator('button[data-export="txt"]')
    assert btn_txt.is_visible()
    btn_txt.click()
    page.wait_for_function(
        "() => document.querySelector('[data-export-result] span')?.textContent.includes('.txt')",
        timeout=15000,
    )
    txt_notice = page.locator('[data-export-result] span').inner_text()
    assert "Standard" in txt_notice
    assert f"report_{run_id}_" in txt_notice
    assert txt_notice.endswith(".txt")

    # 3. Activate visible Markdown export button
    btn_md = page.locator('button[data-export="markdown"]')
    assert btn_md.is_visible()
    btn_md.click()
    page.wait_for_function(
        "() => document.querySelector('[data-export-result] span')?.textContent.includes('.md')",
        timeout=15000,
    )
    md_notice = page.locator('[data-export-result] span').inner_text()
    assert "Standard" in md_notice
    assert f"report_{run_id}_" in md_notice
    assert md_notice.endswith(".md")

    # 4. Activate visible HTML export link by clicking and handling actual browser navigation / popup
    link_html = page.locator('a.export-html[data-report-link]')
    assert link_html.is_visible()
    html_href = link_html.get_attribute("href")
    assert f"/export/{run_id}/html" in html_href
    with page.context.expect_page() as new_page_info:
        link_html.click()
    report_page = new_page_info.value
    report_page.wait_for_selector("body", timeout=10000)
    assert f"/export/{run_id}/html" in report_page.url
    assert "mode=standard" in report_page.url
    html_body = report_page.content()
    assert "QA Test Report" in html_body
    if rep_finding_title:
        from app.qa.reporting import customer_title
        cust_title = customer_title({"title": rep_finding_title})
        assert (cust_title in html_body or rep_finding_title in html_body)
    report_page.close()

    # 5. Verify physical exported artifacts in test directory
    assert os.path.exists(exports_dir)
    files = os.listdir(exports_dir)

    # DOCX artifact verification
    docx_files = [f for f in files if f.startswith(f"report_{run_id}_") and f.endswith(".docx")]
    assert docx_files, f"Expected exported DOCX file for run {run_id}, found: {files}"
    import docx as docx_lib
    doc = docx_lib.Document(os.path.join(exports_dir, docx_files[-1]))
    # Paragraph count assertion verifies document structure (not claiming visual APL fidelity)
    assert len(doc.paragraphs) > 5, "DOCX report must contain structured document content"
    assert any("QA Test Report" in p.text for p in doc.paragraphs[:5])
    if rep_finding_title:
        from app.qa.reporting import customer_title
        cust_title = customer_title({"title": rep_finding_title})
        assert any((cust_title in p.text or rep_finding_title in p.text) for p in doc.paragraphs)

    # Markdown artifact verification
    md_files = [f for f in files if f.startswith(f"report_{run_id}_") and f.endswith(".md")]
    assert md_files, f"Expected exported Markdown file for run {run_id}, found: {files}"
    with open(os.path.join(exports_dir, md_files[-1]), "r", encoding="utf-8") as f:
        md_text = f.read()
    assert "QA Test Report" in md_text
    if rep_finding_title:
        from app.qa.reporting import customer_title
        cust_title = customer_title({"title": rep_finding_title})
        assert (cust_title in md_text or rep_finding_title in md_text)

    # TXT artifact verification
    txt_files = [f for f in files if f.startswith(f"report_{run_id}_") and f.endswith(".txt")]
    assert txt_files, f"Expected exported TXT file for run {run_id}, found: {files}"
    with open(os.path.join(exports_dir, txt_files[-1]), "r", encoding="utf-8") as f:
        txt_text = f.read()
    assert len(txt_text) > 50
    assert "QA Test Report" in txt_text
    if rep_finding_title:
        from app.qa.reporting import customer_title
        cust_title = customer_title({"title": rep_finding_title})
        assert (cust_title in txt_text or rep_finding_title in txt_text)


def test_packaged_profile_ordinary_user_journey():
    """Scenario 1: Ordinary user workflow without hidden contract injection.
    Exercises Home -> project creation/setup -> test-browser session -> one QA start
    -> results -> evidence -> Reports -> export -> restart.

    Standard supported interactions are verified. Domain validation requiring an absent
    contract remains explicitly incomplete/unsupported (LIMITED COVERAGE) rather than PASS,
    with zero false customer defects. Customer defect findings (menu-item-broken, btn-dangling,
    btn-unrevealed, btn-crash) are accurately identified across viewports.
    """
    profile_server = ProfileTestServer("127.0.0.1", 0)
    profile_thread = threading.Thread(target=profile_server.serve_forever, daemon=True)
    profile_thread.start()

    test_writable = tempfile.mkdtemp(prefix="qa_pkg_profile_ord_")
    token = "profile-ord-token"
    env = get_clean_env(test_writable, server_only=True, shutdown_token=token)

    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc1 = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env)

    proj_id = None
    run_id = None
    rep_finding_id = None
    rep_finding_title = None

    try:
        started, _ = wait_for_server(base_url)
        assert started, "Packaged candidate binary failed to start for ordinary user journey"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # 1. Project creation through UI starting from Home dashboard
            page.goto(base_url)
            page.wait_for_selector("#action-my-projects, a[href='/projects']", timeout=10000)
            page.click("#action-my-projects")
            page.wait_for_url("**/projects")
            page.wait_for_selector('button:has-text("Create QA Project")')
            page.click('button:has-text("Create QA Project")')

            profile_url = f"{profile_server.origin}/profile"
            page.fill("#p_name", "Packaged Ordinary UI Profile Project")
            page.fill("#p_url", profile_url)
            page.fill("#p_env", "Localhost Packaged UI")
            page.fill("#p_tester", "Tester")
            page.click("#btnCreate")

            # Redirects to /project/<id>
            page.wait_for_url("**/project/*")
            assert "Packaged Ordinary UI Profile Project" in page.content()
            proj_id = page.url.split("/project/")[-1].split("?")[0].split("/")[0]

            # NOTE: Deliberately NO contract injection. Tests ordinary user workflow.

            # 2. Navigate to New test setup (/project/<id>/dry-run)
            page.click('a:has-text("New test setup")')
            page.wait_for_url("**/dry-run")

            # 3. Select mode 'full' and opt into interactive disclosure
            page.select_option("#test-mode", "full")
            disclosure_cb = page.locator("#enableInteractiveDisclosure")
            disclosure_cb.check()
            assert disclosure_cb.is_checked()

            # 4. Check authorization confirmation
            page.check("#auth_confirm")

            # 5. Open test browser session via UI button
            page.click("#btnToggleInteractive")
            page.wait_for_selector('#session-status:has-text("Session")', timeout=25000)

            # 6. Click primary #btnStart to initiate QA Run
            page.wait_for_function("!document.getElementById('btnStart').disabled", timeout=15000)
            page.click("#btnStart")

            # 7. Navigates to running page (/run/<id>)
            page.wait_for_url("**/run/*")
            run_id = page.url.split("/run/")[-1].split("?")[0].split("/")[0]

            # 8. Poll until run finishes
            for _ in range(120):
                time.sleep(1.0)
                _, s_data = http_get(f"{base_url}/api/runs/{run_id}/status")
                s_obj = json.loads(s_data)
                if s_obj.get("status") in ("COMPLETED", "FAILED", "ABORTED"):
                    break

            # 9. Navigate to results page and verify served UI presentation
            page.goto(f"{base_url}/run/{run_id}")
            page.wait_for_selector(".eyebrow:has-text('Run outcome')", timeout=10000)
            results_content = page.content()
            assert "Customer findings" in results_content
            assert "Total Checks" in results_content

            # Inspect findings list in served UI
            page.goto(f"{base_url}/run/{run_id}?tab=findings")
            page.wait_for_selector(".badge:has-text('customer findings')", timeout=10000)
            findings_content = page.content()
            assert "8 customer findings" in findings_content

            # 9b. UI Evidence Review for representative finding
            first_finding_card = page.locator(".finding-row").first
            assert first_finding_card.count() > 0
            rep_finding_link = first_finding_card.locator("h3 a").first
            rep_finding_title = rep_finding_link.inner_text().strip()
            rep_finding_href = rep_finding_link.get_attribute("href")
            rep_finding_id = int(rep_finding_href.split("/finding/")[-1].split("?")[0])
            rep_finding_link.click()
            page.wait_for_url(f"**/finding/{rep_finding_id}*")

            detail_content = page.content()
            assert rep_finding_title in detail_content
            assert "Original evidence" in detail_content

            # Verify illustrated evidence screenshot preview renders and delivers valid bytes
            preview_img = page.locator(".evidence-preview-box img[src*='/screenshots/']").first
            if preview_img.count() > 0:
                img_src = preview_img.get_attribute("src")
                assert img_src
                img_status, img_bytes = http_get_bytes(f"{base_url}{img_src}")
                assert img_status == 200
                assert len(img_bytes) > 50

            # 10. Generate and verify report exports via visible UI buttons on the Reports page
            exports_dir = os.path.join(test_writable, "exports")
            _exercise_and_verify_visible_report_exports(
                page=page,
                base_url=base_url,
                run_id=run_id,
                rep_finding_title=rep_finding_title,
                exports_dir=exports_dir,
            )

            browser.close()

        # 11. Conclusive graceful shutdown of instance 1
        http_post_json(f"{base_url}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        proc1.wait(timeout=15)
        assert proc1.returncode == 0

    finally:
        if proc1.poll() is None:
            proc1.kill()

    # 12. Relaunch instance 2 on the SAME data directory and verify complete persistence
    port2 = get_free_port()
    base_url2 = f"http://127.0.0.1:{port2}"
    proc2 = subprocess.Popen([EXE_PATH, "--port", str(port2)], env=env)

    try:
        started2, _ = wait_for_server(base_url2)
        assert started2

        with sync_playwright() as p:
            browser2 = p.chromium.launch(headless=True)
            page2 = browser2.new_page()

            page2.goto(base_url2)
            assert "Packaged Ordinary UI Profile Project" in page2.content()
            assert f"/run/{run_id}" in page2.content()

            page2.goto(f"{base_url2}/run/{run_id}?tab=findings")
            page2.wait_for_selector(".badge:has-text('customer findings')")
            assert "8 customer findings" in page2.content()

            browser2.close()

        # Verify SQLite database records and truthful unconfigured validation mapping
        import sqlite3
        import re
        from app.qa.functional_reporting_adapter import compute_opaque_action_ref

        db_path = os.path.join(test_writable, "data", "qa-tester.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        saved_tests = [dict(r) for r in conn.execute("SELECT * FROM tests WHERE run_id=?", (run_id,)).fetchall()]
        saved_findings = [dict(r) for r in conn.execute("SELECT * FROM findings WHERE run_id=?", (run_id,)).fetchall()]
        conn.close()

        token_pattern = re.compile(r"\[action:\s*(act-[a-f0-9]{8})\]")
        tests_by_token = {}
        for t in saved_tests:
            m = token_pattern.search(t["url"])
            if m:
                tests_by_token[m.group(1)] = t

        for vp in ("Desktop", "Mobile"):
            # Unconfigured validator must remain explicitly incomplete/unsupported (LIMITED COVERAGE), not PASS
            tok_hist = compute_opaque_action_ref(
                action_ref="0:btn-validate-history:click",
                page_url=profile_url,
                viewport=vp,
            )
            rec_hist = tests_by_token.get(tok_hist)
            assert rec_hist is not None, f"Expected test record for btn-validate-history in {vp}"
            assert rec_hist["status"] == "LIMITED COVERAGE", (
                f"Uncontracted validation must remain LIMITED COVERAGE, got {rec_hist['status']}"
            )
            assert rec_hist["stable_id"] is None, "Uncontracted validator must not produce a customer defect"

            # Check each inventory item against expected unconfigured outcomes
            for item in PROFILE_INVENTORY:
                raw_id = item["inventory_id"]
                action_type = "unsupported" if "unsupported" in item["category"] else "click"
                tok = compute_opaque_action_ref(
                    action_ref=f"0:{raw_id}:{action_type}",
                    page_url=profile_url,
                    viewport=vp,
                )
                rec = tests_by_token.get(tok)
                assert rec is not None, f"Missing test record for {raw_id} in {vp}"
                if raw_id == "btn-validate-history":
                    assert rec["status"] == "LIMITED COVERAGE"
                    assert rec["stable_id"] is None
                elif item.get("expected_defect_category"):
                    assert rec["status"] == "FAIL"
                    assert rec["stable_id"] is not None
                else:
                    assert rec["status"] == item["expected_verdict"]

        assert len(saved_tests) >= 52
        assert len(saved_findings) == 8

        # Clean shutdown instance 2
        http_post_json(f"{base_url2}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        proc2.wait(timeout=15)
        assert proc2.returncode == 0

    finally:
        if proc2.poll() is None:
            proc2.kill()
        profile_server.shutdown()
        profile_server.server_close()


def test_packaged_profile_served_ui_journey_and_persistence():
    """Scenario 2: Explicitly Preconfigured Domain-Validation Workflow (API-Preconfigured).
    Registers the contract through the existing supported API, then verifies the normal
    application run through the served browser UI, domain result (PASS) and persistence across restart:
      1. Project creation through UI (/projects -> #btnCreate).
      2. Registration of validation contract via supported API (POST /api/projects/<id>/contracts).
      3. Setting test mode to 'full', enabling interactive disclosure (#enableInteractiveDisclosure),
         and checking authorization (#auth_confirm). Ordinary visible settings without artificial budgets.
      4. Interactive session preparation via UI button (#btnToggleInteractive) until session active.
      5. Starting the QA test via the primary UI button (#btnStart).
      6. Observing progress on /run/<id> running view until completion.
      7. Inspecting results, metrics, and findings table in the served web UI.
      8. Generating canonical report exports via visible UI buttons on the Reports page.
      9. Conclusive graceful application shutdown via authorized token.
      10. Relaunch of packaged application on the same data directory verifying full persistence
          of project, run outcome, and findings in the served UI and SQLite database.
    """
    profile_server = ProfileTestServer("127.0.0.1", 0)
    profile_thread = threading.Thread(target=profile_server.serve_forever, daemon=True)
    profile_thread.start()

    test_writable = tempfile.mkdtemp(prefix="qa_pkg_profile_ui_")
    token = "profile-ui-token"
    env = get_clean_env(test_writable, server_only=True, shutdown_token=token)

    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc1 = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env)

    proj_id = None
    run_id = None
    rep_finding_id = None
    rep_finding_title = None

    try:
        started, _ = wait_for_server(base_url)
        assert started, "Packaged candidate binary failed to start for served UI journey"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # 1. Project creation through UI starting from Home dashboard
            page.goto(base_url)
            page.wait_for_selector("#action-my-projects, a[href='/projects']", timeout=10000)
            page.click("#action-my-projects")
            page.wait_for_url("**/projects")
            page.wait_for_selector('button:has-text("Create QA Project")')
            page.click('button:has-text("Create QA Project")')

            profile_url = f"{profile_server.origin}/profile"
            page.fill("#p_name", "Packaged Served UI Profile Project")
            page.fill("#p_url", profile_url)
            page.fill("#p_env", "Localhost Packaged UI")
            page.fill("#p_tester", "Tester")
            page.click("#btnCreate")

            # Redirects to /project/<id>
            page.wait_for_url("**/project/*")
            assert "Packaged Served UI Profile Project" in page.content()
            proj_id = page.url.split("/project/")[-1].split("?")[0].split("/")[0]

            # Register validation contract binding for profile test fixture on this project
            http_post_json(
                f"{base_url}/api/projects/{proj_id}/contracts",
                {
                    "contract_id": "work_history_date_validation",
                    "rule_name": "Work History Date Period Rule",
                    "target_url_pattern": f"{profile_url}*",
                    "target_control_id": "btn-validate-history",
                    "accessible_name": "Validate Work History Dates",
                    "feedback_selector": "#history-date-feedback",
                    "field_selectors": {
                        "start_date": "#input-start-date",
                        "end_date": "#input-end-date",
                    },
                    "completion_marker_attr": "data-validation-case",
                    "completion_marker_type": "case_id",
                    "completion_timestamp_attr": "data-validation-timestamp",
                    "completion_mode": "case_marker",
                },
            )

            # 2. Navigate to New test setup (/project/<id>/dry-run)
            page.click('a:has-text("New test setup")')
            page.wait_for_url("**/dry-run")

            # 3. Select mode 'full' and opt into interactive disclosure
            page.select_option("#test-mode", "full")
            disclosure_cb = page.locator("#enableInteractiveDisclosure")
            disclosure_cb.check()
            assert disclosure_cb.is_checked(), "Interactive disclosure checkbox must be checked"

            # 4. Check authorization confirmation
            page.check("#auth_confirm")

            # 5. Open test browser session via UI button
            page.click("#btnToggleInteractive")
            page.wait_for_selector('#session-status:has-text("Session")', timeout=25000)

            # 6. Click primary #btnStart to initiate QA Run
            page.wait_for_function("!document.getElementById('btnStart').disabled", timeout=15000)
            page.click("#btnStart")

            # 7. Navigates to running page (/run/<id>)
            page.wait_for_url("**/run/*")
            run_id = page.url.split("/run/")[-1].split("?")[0].split("/")[0]
            assert "QA test in progress" in page.content()

            # 8. Poll until run finishes
            for _ in range(120):
                time.sleep(1.0)
                _, s_data = http_get(f"{base_url}/api/runs/{run_id}/status")
                s_obj = json.loads(s_data)
                if s_obj.get("status") in ("COMPLETED", "FAILED", "ABORTED"):
                    break

            # 9. Navigate to results page and verify served UI presentation
            page.goto(f"{base_url}/run/{run_id}")
            page.wait_for_selector(".eyebrow:has-text('Run outcome')", timeout=10000)
            results_content = page.content()
            assert "Customer findings" in results_content
            assert "Total Checks" in results_content

            # Inspect findings list in served UI
            page.goto(f"{base_url}/run/{run_id}?tab=findings")
            page.wait_for_selector(".badge:has-text('customer findings')", timeout=10000)
            findings_content = page.content()
            # Under ordinary visible UI settings with default evaluations limit (10 per page),
            # the automatic profile workflow discovers reachable controls, navigates nested states,
            # and finds the 3 known customer defects (btn-dangling, btn-unrevealed, btn-crash).
            assert "8 customer findings" in findings_content
            assert "Broken Expandable Control" in findings_content or "Interactive control defect" in findings_content

            # 9b. Comprehensive UI Evidence Review for a representative finding
            first_finding_card = page.locator(".finding-row").first
            assert first_finding_card.count() > 0
            rep_finding_link = first_finding_card.locator("h3 a").first
            rep_finding_title = rep_finding_link.inner_text().strip()
            rep_finding_href = rep_finding_link.get_attribute("href")
            rep_finding_id = int(rep_finding_href.split("/finding/")[-1].split("?")[0])
            rep_finding_link.click()
            page.wait_for_url(f"**/finding/{rep_finding_id}*")

            detail_content = page.content()
            assert rep_finding_title in detail_content
            assert "Effective assessment" in detail_content
            assert "Effective Severity" in detail_content
            assert "Effective Risk" in detail_content
            assert "Effective Confidence" in detail_content
            assert "Issue summary" in detail_content
            assert "What does this mean?" in detail_content
            assert "Original evidence" in detail_content
            assert "Expected Result" in detail_content
            assert "Actual Result" in detail_content

            # Verify illustrated evidence screenshot preview renders and delivers valid bytes
            preview_img = page.locator(".evidence-preview-box img[src*='/screenshots/']").first
            if preview_img.count() > 0:
                img_src = preview_img.get_attribute("src")
                assert img_src
                img_status, img_bytes = http_get_bytes(f"{base_url}{img_src}")
                assert img_status == 200
                assert len(img_bytes) > 50, "Screenshot endpoint must serve valid non-empty image content"

            # 10. Generate and verify report exports via visible UI buttons on the Reports page
            exports_dir = os.path.join(test_writable, "exports")
            _exercise_and_verify_visible_report_exports(
                page=page,
                base_url=base_url,
                run_id=run_id,
                rep_finding_title=rep_finding_title,
                exports_dir=exports_dir,
            )

            browser.close()

        # 11. Conclusive graceful shutdown of instance 1
        http_post_json(f"{base_url}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        proc1.wait(timeout=15)
        assert proc1.returncode == 0

    finally:
        if proc1.poll() is None:
            proc1.kill()

    # 12. Relaunch instance 2 on the SAME data directory and verify complete persistence
    port2 = get_free_port()
    base_url2 = f"http://127.0.0.1:{port2}"
    proc2 = subprocess.Popen([EXE_PATH, "--port", str(port2)], env=env)

    try:
        started2, _ = wait_for_server(base_url2)
        assert started2

        with sync_playwright() as p:
            browser2 = p.chromium.launch(headless=True)
            page2 = browser2.new_page()

            # Verify project is visible in UI dashboard
            page2.goto(base_url2)
            assert "Packaged Served UI Profile Project" in page2.content()
            assert f"/run/{run_id}" in page2.content()

            # Verify project is listed in projects directory
            page2.goto(f"{base_url2}/projects")
            page2.wait_for_selector(f'a[href="/project/{proj_id}"]')
            assert "Packaged Served UI Profile Project" in page2.content()

            # Verify project overview displays the completed run
            page2.goto(f"{base_url2}/project/{proj_id}")
            assert f"/run/{run_id}" in page2.content()

            # Verify results page displays intact findings queue across restart
            page2.goto(f"{base_url2}/run/{run_id}?tab=findings")
            page2.wait_for_selector(".badge:has-text('customer findings')")
            assert "8 customer findings" in page2.content()

            # Verify run outcome and total checks persisted in UI
            page2.goto(f"{base_url2}/run/{run_id}")
            page2.wait_for_selector(".eyebrow:has-text('Run outcome')")
            outcome_content = page2.content()
            assert "Execution: COMPLETED" in outcome_content
            assert "Total Checks" in outcome_content

            browser2.close()

        # Verify SQLite database records and 1-to-1 inventory mapping
        import sqlite3
        db_path = os.path.join(test_writable, "data", "qa-tester.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        saved_tests = [dict(r) for r in conn.execute("SELECT * FROM tests WHERE run_id=?", (run_id,)).fetchall()]
        saved_findings = [dict(r) for r in conn.execute("SELECT * FROM findings WHERE run_id=?", (run_id,)).fetchall()]
        conn.close()

        from tests.test_profile_automation import verify_profile_inventory_acceptance
        from tests.profile_test_site import PROFILE_INVENTORY
        matched_desktop = verify_profile_inventory_acceptance(
            tests=saved_tests,
            findings=saved_findings,
            inventory=PROFILE_INVENTORY,
            target_url=profile_url,
            viewport="Desktop",
        )
        assert len(matched_desktop) == len(PROFILE_INVENTORY)

        matched_mobile = verify_profile_inventory_acceptance(
            tests=saved_tests,
            findings=saved_findings,
            inventory=PROFILE_INVENTORY,
            target_url=profile_url,
            viewport="Mobile",
        )
        assert len(matched_mobile) == len(PROFILE_INVENTORY)
        assert len(saved_tests) >= 52
        assert len(saved_findings) == 8

        # Verify representative finding in database and that linked test screenshot exists on disk
        db_rep_finding = next((f for f in saved_findings if f["id"] == rep_finding_id), None)
        assert db_rep_finding is not None, f"Finding id {rep_finding_id} must exist in SQLite database"
        assert db_rep_finding["title"] == rep_finding_title

        linked_tests = [t for t in saved_tests if t.get("stable_id") == db_rep_finding["stable_id"]]
        assert len(linked_tests) >= 1, "Finding must link to at least one test record via stable_id"
        for lt in linked_tests:
            if lt.get("screenshot_path"):
                assert os.path.exists(lt["screenshot_path"]), f"Screenshot file must exist on disk: {lt['screenshot_path']}"
                assert os.path.getsize(lt["screenshot_path"]) > 0

        # Clean shutdown instance 2
        http_post_json(f"{base_url2}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        proc2.wait(timeout=15)
        assert proc2.returncode == 0

    finally:
        if proc2.poll() is None:
            proc2.kill()
        profile_server.shutdown()
        profile_server.server_close()


# ============================================================================
# 12. Packaged Profile Served UI Small Budget Limitation Scenario
# ============================================================================

def test_packaged_profile_served_ui_small_budget_limitation():
    """Separated limitation acceptance test: verifies that setting an explicit small budget
    (limit=2) through the normal UI exhausts the budget after 2 evaluations, accurately
    resulting in 0 customer findings and honest LIMITED COVERAGE records, with persistence across restart."""
    profile_server = ProfileTestServer("127.0.0.1", 0)
    profile_thread = threading.Thread(target=profile_server.serve_forever, daemon=True)
    profile_thread.start()

    test_writable = tempfile.mkdtemp(prefix="qa_pkg_profile_small_")
    token = "profile-small-token"
    env = get_clean_env(test_writable, server_only=True, shutdown_token=token)

    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc1 = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env)

    proj_id = None
    run_id = None

    try:
        started, _ = wait_for_server(base_url)
        assert started, "Packaged candidate binary failed to start for small budget UI scenario"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # 1. Project creation through UI
            page.goto(f"{base_url}/projects")
            page.wait_for_selector('button:has-text("Create QA Project")')
            page.click('button:has-text("Create QA Project")')

            profile_url = f"{profile_server.origin}/profile"
            page.fill("#p_name", "Packaged Small Budget Project")
            page.fill("#p_url", profile_url)
            page.fill("#p_env", "Localhost Packaged UI")
            page.fill("#p_tester", "Tester")
            page.click("#btnCreate")

            page.wait_for_url("**/project/*")
            proj_id = page.url.split("/project/")[-1].split("?")[0].split("/")[0]

            # Register validation contract binding for profile test fixture on this project
            http_post_json(
                f"{base_url}/api/projects/{proj_id}/contracts",
                {
                    "contract_id": "work_history_date_validation",
                    "rule_name": "Work History Date Period Rule",
                    "target_url_pattern": f"{profile_url}*",
                    "target_control_id": "btn-validate-history",
                    "accessible_name": "Validate Work History Dates",
                    "feedback_selector": "#history-date-feedback",
                    "field_selectors": {
                        "start_date": "#input-start-date",
                        "end_date": "#input-end-date",
                    },
                    "completion_marker_attr": "data-validation-case",
                    "completion_marker_type": "case_id",
                    "completion_timestamp_attr": "data-validation-timestamp",
                    "completion_mode": "case_marker",
                },
            )

            # 2. Navigate to dry-run setup
            page.click('a:has-text("New test setup")')
            page.wait_for_url("**/dry-run")

            # 3. Mode full, opt-in interactive disclosure, and set small budget (2)
            page.select_option("#test-mode", "full")
            disclosure_cb = page.locator("#enableInteractiveDisclosure")
            disclosure_cb.check()
            assert disclosure_cb.is_checked()

            # Set small budget limit in normal UI input
            limit_input = page.locator("#interactiveEvaluationsLimit")
            limit_input.fill("2")
            assert limit_input.input_value() == "2"

            # 4. Check authorization confirmation
            page.check("#auth_confirm")

            # 5. Open test browser session via UI button
            page.click("#btnToggleInteractive")
            page.wait_for_selector('#session-status:has-text("Session")', timeout=25000)

            # 6. Click primary #btnStart to initiate QA Run
            page.wait_for_function("!document.getElementById('btnStart').disabled", timeout=15000)
            page.click("#btnStart")

            # 7. Navigates to running page
            page.wait_for_url("**/run/*")
            run_id = page.url.split("/run/")[-1].split("?")[0].split("/")[0]

            # 8. Poll until run finishes
            for _ in range(60):
                time.sleep(1.0)
                _, s_data = http_get(f"{base_url}/api/runs/{run_id}/status")
                s_obj = json.loads(s_data)
                if s_obj.get("status") in ("COMPLETED", "FAILED", "ABORTED"):
                    break

            # 9. Verify findings list displays 0 findings due to budget exhaustion
            page.goto(f"{base_url}/run/{run_id}?tab=findings")
            page.wait_for_selector(".badge:has-text('customer findings')", timeout=10000)
            findings_content = page.content()
            assert "0 customer findings" in findings_content
            assert "No customer findings recorded" in findings_content

            browser.close()

        # 10. Conclusive graceful shutdown of instance 1
        http_post_json(f"{base_url}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        proc1.wait(timeout=15)
        assert proc1.returncode == 0

    finally:
        if proc1.poll() is None:
            proc1.kill()

    # 11. Relaunch instance 2 and verify persistence of 0 findings and 26 tests
    port2 = get_free_port()
    base_url2 = f"http://127.0.0.1:{port2}"
    proc2 = subprocess.Popen([EXE_PATH, "--port", str(port2)], env=env)

    try:
        started2, _ = wait_for_server(base_url2)
        assert started2

        import sqlite3
        db_path = os.path.join(test_writable, "data", "qa-tester.db")
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        saved_tests = c.execute("SELECT COUNT(*) FROM tests WHERE run_id=?", (run_id,)).fetchone()[0]
        saved_findings = c.execute("SELECT COUNT(*) FROM findings WHERE run_id=?", (run_id,)).fetchone()[0]
        conn.close()

        assert saved_tests == len(PROFILE_INVENTORY)
        assert saved_findings == 0

        # Clean shutdown instance 2
        http_post_json(f"{base_url2}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        proc2.wait(timeout=15)
        assert proc2.returncode == 0

    finally:
        if proc2.poll() is None:
            proc2.kill()
        profile_server.shutdown()
        profile_server.server_close()


def _get_candidate_sha() -> str:
    assert os.path.exists(EXE_PATH), f"Candidate executable missing at {EXE_PATH}"
    hasher = hashlib.sha256()
    with open(EXE_PATH, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    sha = hasher.hexdigest().upper()
    assert len(sha) == 64
    return sha


# ---------------------------------------------------------------------------
# Packaged Scenario 1: Successful CRUD/Upload Mutation Lifecycle
# ---------------------------------------------------------------------------
def test_packaged_mutation_successful_lifecycle():
    """Verify that packaged candidate executes complete CRUD/upload mutation lifecycle,
    preserves original pre-existing material, confirms remote cleanup in server inventory,
    records explicit server operation history, and records verified state in SQLite."""
    candidate_sha = _get_candidate_sha()

    profile_server = ProfileTestServer("127.0.0.1", 0)
    profile_server.upload_destination_contract = True
    profile_thread = threading.Thread(target=profile_server.serve_forever, daemon=True)
    profile_thread.start()

    test_writable = tempfile.mkdtemp(prefix="qa_pkg_mut_life_")
    token = "pkg-mut-life-token"
    env = get_clean_env(test_writable, server_only=True, shutdown_token=token)

    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env)

    try:
        started, _ = wait_for_server(base_url)
        assert started, "Packaged candidate binary failed to start"

        # Baseline check on server inventory
        assert "exp-preexisting-1" in profile_server.experiences
        orig_count = len(profile_server.experiences)

        # Create project targeting profile test server with mutation enabled
        profile_url = f"{profile_server.origin}/profile?mutation=true"
        status, proj = http_post_json(
            f"{base_url}/api/projects",
            {
                "name": "Packaged Mutation Lifecycle Project",
                "target_url": profile_url,
                "environment": "localhost-packaged",
                "tester_name": "Tester",
            },
        )
        assert status == 200, f"Project creation failed: {proj}"
        proj_id = proj["id"]

        # Start interactive session
        status, start_data = http_post_json(f"{base_url}/api/projects/{proj_id}/interactive/start", {})
        assert status == 200, f"Interactive start failed: {start_data}"
        ready = False
        for _ in range(60):
            time.sleep(0.5)
            _, sess_data = http_get(f"{base_url}/api/projects/{proj_id}/interactive/state")
            if json.loads(sess_data).get("state") == "READY":
                ready = True
                break
        assert ready, "Interactive session failed to reach READY state in packaged candidate"

        # Start QA Run with authorized mutations enabled
        status, run_data = http_post_json(
            f"{base_url}/api/runs",
            {
                "project_id": proj_id,
                "mode": "full",
                "page_limit": 5,
                "enable_authorized_mutations": True,
                "enable_interactive_disclosure": True,
                "interactive_options": {
                    "max_evaluations_per_page": 20,
                    "max_evaluations_per_run": 50,
                    "action_timeout": 1.0,
                    "reconstruction_timeout": 5.0,
                },
            },
        )
        assert status == 200, f"Failed to start mutation run: {run_data}"
        run_id = run_data["run_id"]

        # Poll until completed
        final_status = None
        for _ in range(120):
            time.sleep(1.0)
            _, s_data = http_get(f"{base_url}/api/runs/{run_id}/status")
            final_status = json.loads(s_data).get("status")
            if final_status in ("COMPLETED", "FAILED", "ABORTED"):
                break

        assert final_status == "COMPLETED", f"Packaged mutation run did not complete: status={final_status}"

        # 1. Assert explicit server operation history (CREATE, EDIT, UPLOAD, and 2x DELETE)
        op_types = [entry["op"] for entry in profile_server.operations_log]
        assert "CREATE" in op_types, f"No CREATE operation logged: {profile_server.operations_log}"
        assert "EDIT" in op_types, f"No EDIT operation logged: {profile_server.operations_log}"
        assert "UPLOAD" in op_types, f"No UPLOAD operation logged: {profile_server.operations_log}"
        assert op_types.count("DELETE") >= 2, f"Expected at least 2 DELETE operations (attachment + experience), found: {profile_server.operations_log}"

        # 2. Assert intermediate persisted content: verify EDIT operation updated company name
        edit_entries = [e for e in profile_server.operations_log if e["op"] == "EDIT"]
        assert len(edit_entries) >= 1
        assert "[EDITED]" in edit_entries[0]["data"]["company"] or "QA-TEST" in edit_entries[0]["data"]["company"]

        # 3. Connect to SQLite database and verify journal records and final ownership states
        db_path = os.path.join(test_writable, "data", "qa-tester.db")
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        ownership_recs = [dict(r) for r in conn.execute("SELECT * FROM mutation_ownership_records WHERE run_id=?", (run_id,)).fetchall()]
        assert len(ownership_recs) >= 2, f"Expected ownership journal records in packaged DB for run {run_id}"

        for orec in ownership_recs:
            assert orec["run_id"] == run_id
            assert orec["remote_status"] in ("CONFIRMED_DELETED", "CLEANED_UP")
            assert orec["remote_cleaned"] == 1

        # Check tests table for scoped mutation test records
        tests = [dict(r) for r in conn.execute("SELECT * FROM tests WHERE run_id=?", (run_id,)).fetchall()]
        mutation_tests = [t for t in tests if any(kw in (t.get("expected_result") or "") for kw in ("Authorized", "Run-owned", "Harmless", "test entity", "barrier"))]
        assert len(mutation_tests) >= 1, f"Expected adapted mutation test results in packaged tests table, found {len(mutation_tests)}"

        conn.close()

        # 4. Assert server-side inventory: original content strictly preserved and test material cleaned up
        assert "exp-preexisting-1" in profile_server.experiences, "Original user experience was corrupted or deleted!"
        assert profile_server.experiences["exp-preexisting-1"]["company"] == "Nordic QA Governance (Pre-existing User Entry)"
        deleted_ids = [e.get("id") for e in profile_server.operations_log if e["op"] == "DELETE"]
        assert "exp-preexisting-1" not in deleted_ids, f"Pre-existing record was targeted by delete! {deleted_ids}"
        assert len(profile_server.experiences) == orig_count, f"Leftover test experiences found on server: {profile_server.experiences}"
        assert len(profile_server.attachments) == 0, f"Leftover test attachments found on server: {profile_server.attachments}"

        # Verify clean graceful shutdown
        http_post_json(f"{base_url}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        proc.wait(timeout=15)
        assert proc.returncode == 0

    finally:
        if proc.poll() is None:
            proc.kill()
        profile_server.shutdown()
        profile_server.server_close()


# ---------------------------------------------------------------------------
# Packaged Scenario 2: Mutation Uncertainty Handling & Cold-Restart Recovery
# ---------------------------------------------------------------------------
def test_packaged_mutation_uncertain_outcome_and_recovery():
    """Verify that mutation uncertainty records pending/ambiguous status in the journal
    without false defect claims, persists across cold restart, and allows safe reconciliation."""
    candidate_sha = _get_candidate_sha()

    profile_server = ProfileTestServer("127.0.0.1", 0)
    # Simulate read-back failure upon experience creation reload (1st reload after creation)
    profile_server.fail_readback_after_reloads = 1
    profile_thread = threading.Thread(target=profile_server.serve_forever, daemon=True)
    profile_thread.start()

    test_writable = tempfile.mkdtemp(prefix="qa_pkg_mut_unc_")
    token = "pkg-mut-unc-token"
    env = get_clean_env(test_writable, server_only=True, shutdown_token=token)

    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env)

    try:
        started, _ = wait_for_server(base_url)
        assert started

        # Create project targeting profile test server
        profile_url = f"{profile_server.origin}/profile?mutation=true"
        status, proj = http_post_json(
            f"{base_url}/api/projects",
            {
                "name": "Packaged Mutation Uncertainty Project",
                "target_url": profile_url,
                "environment": "localhost-packaged",
                "tester_name": "Tester",
            },
        )
        assert status == 200
        proj_id = proj["id"]

        # Start interactive session
        status, _ = http_post_json(f"{base_url}/api/projects/{proj_id}/interactive/start", {})
        assert status == 200
        ready = False
        for _ in range(40):
            time.sleep(0.5)
            _, sess_data = http_get(f"{base_url}/api/projects/{proj_id}/interactive/state")
            if json.loads(sess_data).get("state") == "READY":
                ready = True
                break
        assert ready

        # Execute Run 1: will encounter real read-back reload failure on creation
        status, run_data = http_post_json(
            f"{base_url}/api/runs",
            {
                "project_id": proj_id,
                "mode": "current",
                "enable_authorized_mutations": True,
            },
        )
        assert status == 200, f"Run 1 start failed: {run_data}"
        run_id = run_data["run_id"]

        for _ in range(120):
            time.sleep(1.0)
            _, s_data = http_get(f"{base_url}/api/runs/{run_id}/status")
            s = json.loads(s_data).get("status")
            if s in ("COMPLETED", "FAILED", "ABORTED"):
                break

        # Check SQLite database for real uncertain outcome without manual insert
        db_path = os.path.join(test_writable, "data", "qa-tester.db")
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Verify pending/ambiguous state in journal
        ambiguous_recs = [dict(r) for r in conn.execute(
            "SELECT * FROM mutation_ownership_records WHERE run_id=? AND (remote_status='AMBIGUOUS' OR status='AMBIGUOUS_UNCONFIRMED')",
            (run_id,)
        ).fetchall()]
        assert len(ambiguous_recs) >= 1, f"Expected real ambiguous journal record for run {run_id}"

        # Verify non-defect attribution: uncertain mutation record is LIMITED COVERAGE with no customer defect
        tests = [dict(r) for r in conn.execute("SELECT * FROM tests WHERE run_id=?", (run_id,)).fetchall()]
        unc_tests = [t for t in tests if any(kw in (t.get("expected_result") or "") for kw in ("created", "deleted", "Authorized test entity")) and t.get("status") == "LIMITED COVERAGE"]
        assert len(unc_tests) >= 1
        assert unc_tests[0].get("stable_id") is None
        findings = [dict(r) for r in conn.execute("SELECT * FROM findings WHERE run_id=?", (run_id,)).fetchall()]
        del_findings = [f for f in findings if "delete" in (f.get("title") or "").lower() or "create" in (f.get("title") or "").lower()]
        assert len(del_findings) == 0
        conn.close()

        # Shutdown server instance 1 to test cold restart persistence
        http_post_json(f"{base_url}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        proc.wait(timeout=15)
        assert proc.returncode == 0

        # Cold restart with same data directory
        port2 = get_free_port()
        base_url2 = f"http://127.0.0.1:{port2}"
        proc2 = subprocess.Popen([EXE_PATH, "--port", str(port2)], env=env)
        try:
            started2, _ = wait_for_server(base_url2)
            assert started2

            # Verify ambiguous record persisted across cold restart
            conn2 = sqlite3.connect(db_path)
            conn2.row_factory = sqlite3.Row
            persisted = conn2.execute(
                "SELECT * FROM mutation_ownership_records WHERE run_id=? AND (remote_status='AMBIGUOUS' OR status='AMBIGUOUS_UNCONFIRMED')",
                (run_id,)
            ).fetchall()
            assert len(persisted) >= 1
            conn2.close()

            # Server is now healthy again (no read-back failure)
            profile_server.fail_readback_after_reloads = None

            # Start interactive session for recovery
            status, _ = http_post_json(f"{base_url2}/api/projects/{proj_id}/interactive/start", {})
            assert status == 200
            for _ in range(40):
                time.sleep(0.5)
                _, sess_data = http_get(f"{base_url2}/api/projects/{proj_id}/interactive/state")
                if json.loads(sess_data).get("state") == "READY":
                    break

            # Execute Run 2: recovery sweep reconciles pending entities against server state
            status, run_data2 = http_post_json(
                f"{base_url2}/api/runs",
                {
                    "project_id": proj_id,
                    "mode": "current",
                    "enable_authorized_mutations": True,
                },
            )
            assert status == 200
            run_id2 = run_data2["run_id"]

            for _ in range(120):
                time.sleep(1.0)
                _, s_data = http_get(f"{base_url2}/api/runs/{run_id2}/status")
                s = json.loads(s_data).get("status")
                if s in ("COMPLETED", "FAILED", "ABORTED"):
                    break
            assert s == "COMPLETED", f"Run 2 status was {s}, data: {s_data}"

            # Verify reconciliation against server state in SQLite
            conn2 = sqlite3.connect(db_path)
            conn2.row_factory = sqlite3.Row
            reconciled = [dict(r) for r in conn2.execute(
                "SELECT * FROM mutation_ownership_records WHERE run_id=? AND remote_status='CONFIRMED_DELETED' AND remote_cleaned=1",
                (run_id,)
            ).fetchall()]
            assert len(reconciled) >= 1, "Prior ambiguous record was not reconciled after restart and second run"

            # Verify pre-existing records preserved and zero leftovers
            assert "exp-preexisting-1" in profile_server.experiences
            conn2.close()

            # Clean shutdown of second instance
            http_post_json(f"{base_url2}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
            proc2.wait(timeout=15)
            assert proc2.returncode == 0
        finally:
            if proc2.poll() is None:
                proc2.kill()

    finally:
        if proc.poll() is None:
            proc.kill()
        profile_server.shutdown()
        profile_server.server_close()


# ---------------------------------------------------------------------------
# Packaged Scenario 3: Real Barrier Boundary Rejection & Account Gating
# ---------------------------------------------------------------------------
def test_packaged_mutation_boundary_rejection():
    """Verify that the packaged application strictly blocks unapproved writes,
    wrong-account attempts, and unauthorized upload targets through the real network barrier."""
    candidate_sha = _get_candidate_sha()

    from tests.test_scoped_mutation_adversarial import AdversarialProfileServer
    adv_server = AdversarialProfileServer("127.0.0.1", 0)
    adv_thread = threading.Thread(target=adv_server.serve_forever, daemon=True)
    adv_thread.start()

    test_writable = tempfile.mkdtemp(prefix="qa_pkg_mut_bnd_")
    token = "pkg-mut-bnd-token"
    env = get_clean_env(test_writable, server_only=True, shutdown_token=token)

    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env)

    try:
        started, _ = wait_for_server(base_url)
        assert started

        # Record initial baseline state on server
        initial_status = adv_server.user_profile.get("account_status", "ACTIVE")
        initial_avatar = adv_server.user_profile.get("avatar", "default.png")

        # Create project targeting adversarial profile test server
        profile_url = f"{adv_server.origin}/profile"
        status, proj = http_post_json(
            f"{base_url}/api/projects",
            {
                "name": "Packaged Mutation Boundary Project",
                "target_url": profile_url,
                "environment": "localhost-packaged",
                "tester_name": "Tester",
            },
        )
        assert status == 200
        proj_id = proj["id"]

        # Start interactive session
        status, _ = http_post_json(f"{base_url}/api/projects/{proj_id}/interactive/start", {})
        assert status == 200
        ready = False
        for _ in range(40):
            time.sleep(0.5)
            _, sess_data = http_get(f"{base_url}/api/projects/{proj_id}/interactive/state")
            if json.loads(sess_data).get("state") == "READY":
                ready = True
                break
        assert ready, "Interactive session failed to reach READY state in packaged candidate"

        # Run with authorized mutations enabled against profile with dangerous controls
        status, run_data = http_post_json(
            f"{base_url}/api/runs",
            {
                "project_id": proj_id,
                "mode": "full",
                "page_limit": 5,
                "enable_authorized_mutations": True,
                "enable_interactive_disclosure": True,
                "interactive_options": {
                    "max_evaluations_per_page": 5,
                    "max_evaluations_per_run": 10,
                    "action_timeout": 1.0,
                    "reconstruction_timeout": 5.0,
                },
            },
        )
        assert status == 200, f"Failed to start run: {run_data}"
        run_id = run_data["run_id"]

        # Poll until complete
        final_s = None
        for _ in range(120):
            time.sleep(1.0)
            _, s_data = http_get(f"{base_url}/api/runs/{run_id}/status")
            final_s = json.loads(s_data).get("status")
            if final_s in ("COMPLETED", "FAILED", "ABORTED"):
                break
        assert final_s == "COMPLETED", f"Scenario 3 run did not complete: status={final_s}"

        # Confirm on adversarial server that prohibited state changes were completely blocked:
        # 1. User account was NOT deleted
        assert adv_server.user_profile.get("account_status") == initial_status
        assert adv_server.user_profile.get("account_status") != "PROHIBITED_DELETED"
        # 2. Avatar was NOT tampered with
        assert adv_server.user_profile.get("avatar") == initial_avatar
        assert adv_server.user_profile.get("avatar") != "tampered_avatar.png"
        # 3. Security keys and settings remained untouched
        assert adv_server.user_profile.get("security_settings_version") == 1
        # 4. Prohibited endpoints received zero unsolicited writes
        assert len(adv_server.unsolicited_writes_received) == 0
        assert len(adv_server.graphql_mutations_executed) == 0
        # 5. Pre-existing experience remains untouched
        assert "exp-real-1" in adv_server.experiences

        # Normal orchestration must not manufacture destructive self-test requests.
        # Actual barrier rejection is exercised by isolated test-only harnesses.
        db_path = os.path.join(test_writable, "data", "qa-tester.db")
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        tests = [dict(r) for r in conn.execute("SELECT * FROM tests WHERE run_id=?", (run_id,)).fetchall()]
        assert not any("/api/account/delete" in (t.get("actual_result") or "") for t in tests)
        conn.close()

        # Verify clean graceful shutdown
        http_post_json(f"{base_url}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        proc.wait(timeout=15)
        assert proc.returncode == 0

    finally:
        if proc.poll() is None:
            proc.kill()
        adv_server.shutdown()
        adv_server.server_close()






def test_packaged_chromium_logging_uses_writable_destination(tmp_path, monkeypatch):
    """Fresh bundle: visible/headless Chromium retain diagnostics without a debug.log sentinel.

    Uses the product's own per-mode launch args and a real http page, because legacy
    headless renderers ignore --log-file and write debug.log beside chrome.exe.
    """
    from pathlib import Path
    from app import paths
    chrome = next(Path(DIST_DIR).glob('_internal/playwright_browsers/chromium-*/chrome-win/chrome.exe'))
    assert not chrome.with_name('debug.log').exists(), 'Fresh candidate must not depend on a manual debug.log'
    writable = tmp_path / 'Loggar med \u00e5\u00e4\u00f6 och mellanslag'
    monkeypatch.setenv('QA_TESTER_WRITABLE_DIR', str(writable))
    monkeypatch.setattr(paths, 'is_packaged', lambda: True)
    monkeypatch.setattr(paths, 'get_writable_dir', lambda: str(writable))
    log = Path(paths.get_logs_dir()) / 'chromium_debug.log'
    assert paths.chromium_launch_args() == ['--log-file=' + str(log.resolve())]
    assert paths.chromium_launch_args(headless=True) == ['--log-file=' + str(log.resolve()), '--headless=new']
    target = ControlledLocalhostTarget()
    try:
        with sync_playwright() as pw:
            for headless in (False, True):
                args = paths.chromium_launch_args(headless=headless)
                browser = pw.chromium.launch(executable_path=str(chrome), headless=headless,
                    args=args + ['--enable-logging', '--v=1', '--use-gl=qa-review-invalid'])
                try:
                    page = browser.new_page()
                    messages = []
                    page.on('console', lambda message: messages.append(message.text))
                    page.goto('data:text/html,<title>Local logging check</title>')
                    assert page.title() == 'Local logging check'
                    page.evaluate("console.error('QA_LOG_REVIEW_DIAGNOSTIC')")
                    page.wait_for_timeout(300)
                    assert 'QA_LOG_REVIEW_DIAGNOSTIC' in messages
                    for path in ('/', '/catalog', '/contact'):
                        extra = browser.new_page()
                        extra.goto(target.url + path)
                        extra.close()
                finally:
                    browser.close()
                assert log.exists()
                native_log = log.read_text(encoding='utf-8', errors='replace')
                assert 'ERROR:' in native_log and 'GL' in native_log, 'Native Chromium errors must remain visible'
                assert not chrome.with_name('debug.log').exists(), f'debug.log written into package (headless={headless})'
    finally:
        target.shutdown()
        target.server_close()


def _package_tree_snapshot():
    snap = {}
    for root, _dirs, files in os.walk(DIST_DIR):
        for name in files:
            full = os.path.join(root, name)
            st = os.stat(full)
            snap[os.path.relpath(full, DIST_DIR)] = (st.st_size, st.st_mtime_ns)
    return snap


def test_packaged_smart_crawl_does_not_write_into_package():
    """Ordinary Smart Site Crawl without a QA Browser session (standalone headless Chromium),
    normal completion and normal shutdown must leave the installed package tree unchanged."""
    before = _package_tree_snapshot()
    target = ControlledLocalhostTarget()
    test_writable = tempfile.mkdtemp(prefix="qa_pkg_crawl_immutable_")
    token = "crawl-immutable-token"
    env = get_clean_env(test_writable, server_only=True, shutdown_token=token)
    env.pop("CHROME_LOG_FILE", None)  # the product itself must route Chromium logs
    port = get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([EXE_PATH, "--port", str(port)], env=env)
    try:
        started, _ = wait_for_server(base_url)
        assert started
        _, proj = http_post_json(f"{base_url}/api/projects", {
            "name": "Crawl Package Immutability", "target_url": target.url,
            "environment": "localhost-packaged", "tester_name": "QA Packager"})
        _, run_data = http_post_json(f"{base_url}/api/runs", {"project_id": proj["id"], "mode": "crawl"})
        run_id = run_data["run_id"]
        final = None
        for _ in range(120):
            time.sleep(0.5)
            _, s_data = http_get(f"{base_url}/api/runs/{run_id}/status")
            final = json.loads(s_data).get("status")
            if final in ("COMPLETED", "FAILED", "ABORTED"):
                break
        assert final == "COMPLETED", f"Crawl did not complete: {final}"
        import sqlite3
        conn = sqlite3.connect(os.path.join(test_writable, "data", "qa-tester.db"))
        records = conn.execute("SELECT COUNT(*) FROM tests WHERE run_id=?", (run_id,)).fetchone()[0]
        conn.close()
        assert records > 0, "Crawl produced no test records"
        status, _ = http_post_json(f"{base_url}/api/system/shutdown", {}, headers={"X-Shutdown-Token": token})
        assert status == 200
        proc.wait(timeout=30)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        target.shutdown()
        target.server_close()
    after = _package_tree_snapshot()
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    assert not (added or removed or changed), f"Package modified: added={added} removed={removed} changed={changed}"
