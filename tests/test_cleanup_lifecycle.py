"""Durable Regression Coverage for Resource and Cleanup Lifecycle.

Validates the complete cleanup lifecycle contract across:
1. Normal completion: clean teardown of browser, driver (returncode 0), and event loop.
2. User stop: cooperative cancellation, ABORTED terminal status, retained prior evidence, clean driver shutdown.
3. Browser-close failure: failure is not swallowed, driver stop is still executed, status is FAILED, not COMPLETED.
4. Driver-stop failure: failure is not swallowed, run() raises error, status is FAILED, not COMPLETED.
5. Cancellation during teardown: task cancellation does not abort driver stop, cleanup runs to completion before loop closes.
6. Multiple teardown failures: both failures are collected into ExceptionGroup without swallowing either.
"""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import threading
import time
from unittest.mock import patch
import pytest

from app.models.database import get_db_connection, init_db
from app.qa.engine import SmartSmokeQA
from app.routes.api import run_qa_async_in_thread, worker_records
import app.paths as paths
from server import app as flask_app


class SimpleTestServer:
    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><head><title>Test Page</title></head><body><h1>Hello</h1></body></html>")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(scope="module")
def local_site():
    site = SimpleTestServer()
    yield site
    site.close()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "qa_test.db")
    monkeypatch.setenv("QA_TESTER_DB_PATH", db_file)
    init_db()
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO projects (name, target_url, environment, tester_name) VALUES (?, ?, ?, ?)",
        ("Test Project", "http://127.0.0.1:5000", "Testing", "Tester"),
    )
    conn.commit()
    conn.close()
    return db_file


@pytest.mark.asyncio
async def test_normal_completion_lifecycle(local_site):
    """Verifies normal run cleans up browser and driver subprocess cleanly (returncode 0)."""
    engine = SmartSmokeQA(local_site.url, project_id=1, run_id=1, max_pages=1, mode="crawl")

    unclosed_transports = []
    unraisable_exceptions = []

    def unraisable_hook(unraisable):
        unraisable_exceptions.append(unraisable)

    old_unraisable_hook = sys.unraisablehook
    sys.unraisablehook = unraisable_hook
    try:
        results = await engine.run()
        assert len(results) >= 1
        assert engine.cleanup_error is None

        # Verify browser is closed
        assert engine.browser is not None
        assert not engine.browser.is_connected()

        # Verify Playwright driver subprocess exited with returncode 0
        assert engine.playwright is not None
        impl = getattr(engine.playwright, "_impl_obj", None)
        assert impl is not None
        proc = impl._connection._transport._proc
        assert proc.returncode == 0, f"Driver process returncode should be 0, got {proc.returncode}"
    finally:
        sys.unraisablehook = old_unraisable_hook

    assert len(unraisable_exceptions) == 0, f"Unexpected unraisable exceptions: {unraisable_exceptions}"


@pytest.mark.asyncio
async def test_browser_close_failure_attempts_driver_stop_and_propagates(local_site):
    """Verifies that if browser.close() fails, driver p.stop() is STILL attempted, and error is propagated."""
    from playwright.async_api import async_playwright

    engine = SmartSmokeQA(local_site.url, project_id=1, run_id=2, max_pages=1, mode="crawl")

    orig_start = async_playwright().start
    captured_p = None

    async def custom_start():
        nonlocal captured_p
        p = await orig_start()
        captured_p = p
        orig_launch = p.chromium.launch

        async def hooked_launch(*args, **kwargs):
            browser = await orig_launch(*args, **kwargs)

            async def failing_close():
                raise RuntimeError("Simulated browser.close() failure")

            browser.close = failing_close
            return browser

        p.chromium.launch = hooked_launch
        return p

    with patch("app.qa.engine.async_playwright") as mock_ap:
        mock_ap.return_value.start = custom_start

        with pytest.raises(RuntimeError) as exc_info:
            await engine.run()

        assert "Simulated browser.close() failure" in str(exc_info.value)
        assert engine.cleanup_error is not None
        assert "Simulated browser.close() failure" in str(engine.cleanup_error)

        # Verify driver was still stopped despite browser.close failure
        assert captured_p is not None
        impl = captured_p._impl_obj
        proc = impl._connection._transport._proc
        assert proc.returncode == 0, "Driver subprocess should have been stopped with returncode 0"


@pytest.mark.asyncio
async def test_driver_stop_failure_propagates_and_marks_cleanup_error(local_site):
    """Verifies that if p.stop() fails, engine.run() does not return success [], but raises and sets cleanup_error."""
    from playwright.async_api import async_playwright

    engine = SmartSmokeQA(local_site.url, project_id=1, run_id=3, max_pages=1, mode="collected", collected_urls=[])

    orig_start = async_playwright().start
    captured_p = None
    real_stop = None

    async def custom_start():
        nonlocal captured_p, real_stop
        p = await orig_start()
        captured_p = p
        real_stop = p.stop

        async def failing_stop():
            raise RuntimeError("Reviewer simulated driver stop failure")

        p.stop = failing_stop
        return p

    with patch("app.qa.engine.async_playwright") as mock_ap:
        mock_ap.return_value.start = custom_start

        try:
            with pytest.raises(RuntimeError) as exc_info:
                await engine.run()

            assert "Reviewer simulated driver stop failure" in str(exc_info.value)
            assert engine.cleanup_error is not None
            assert "Reviewer simulated driver stop failure" in str(engine.cleanup_error)

            # Subprocess returncode is None because stop failed
            impl = captured_p._impl_obj
            proc = impl._connection._transport._proc
            assert proc.returncode is None
        finally:
            if real_stop:
                await real_stop()
                assert proc.returncode == 0


@pytest.mark.asyncio
async def test_cancellation_during_teardown_completes_all_steps(local_site):
    """Verifies that cancellation arriving during browser.close() does not prevent p.stop() from running."""
    from playwright.async_api import async_playwright

    engine = SmartSmokeQA(local_site.url, project_id=1, run_id=4, max_pages=1, mode="crawl")

    orig_start = async_playwright().start
    captured_p = None
    engine_task = None
    close_started = asyncio.Event()

    async def custom_start():
        nonlocal captured_p
        p = await orig_start()
        captured_p = p
        orig_launch = p.chromium.launch

        async def hooked_launch(*args, **kwargs):
            browser = await orig_launch(*args, **kwargs)
            orig_close = browser.close

            async def cancelling_close():
                close_started.set()
                if engine_task:
                    engine_task.cancel()
                await asyncio.sleep(0.05)
                return await orig_close()

            browser.close = cancelling_close
            return browser

        p.chromium.launch = hooked_launch
        return p

    with patch("app.qa.engine.async_playwright") as mock_ap:
        mock_ap.return_value.start = custom_start

        async def run_coro():
            return await engine.run()

        engine_task = asyncio.create_task(run_coro())

        with pytest.raises(asyncio.CancelledError):
            await engine_task

        assert close_started.is_set(), "browser.close was not invoked"
        assert captured_p is not None
        impl = captured_p._impl_obj
        proc = impl._connection._transport._proc
        assert proc.returncode == 0, "Driver subprocess must be 0, confirming p.stop finished despite cancellation"


@pytest.mark.asyncio
async def test_multiple_teardown_failures_grouped_without_swallowing(local_site):
    """Verifies that when both browser.close and p.stop fail, ExceptionGroup is raised with both errors."""
    from playwright.async_api import async_playwright

    engine = SmartSmokeQA(local_site.url, project_id=1, run_id=5, max_pages=1, mode="collected", collected_urls=[])

    orig_start = async_playwright().start
    captured_p = None
    real_stop = None

    async def custom_start():
        nonlocal captured_p, real_stop
        p = await orig_start()
        captured_p = p
        real_stop = p.stop
        orig_launch = p.chromium.launch

        async def hooked_launch(*args, **kwargs):
            browser = await orig_launch(*args, **kwargs)

            async def failing_b_close():
                raise RuntimeError("Browser close error E1")

            browser.close = failing_b_close
            return browser

        p.chromium.launch = hooked_launch

        async def failing_p_stop():
            raise RuntimeError("Driver stop error E2")

        p.stop = failing_p_stop
        return p

    with patch("app.qa.engine.async_playwright") as mock_ap:
        mock_ap.return_value.start = custom_start

        try:
            with pytest.raises(ExceptionGroup) as exc_info:
                await engine.run()

            eg = exc_info.value
            sub_messages = [str(e) for e in eg.exceptions]
            assert any("Browser close error E1" in m for m in sub_messages)
            assert any("Driver stop error E2" in m for m in sub_messages)
            assert engine.cleanup_error is eg
        finally:
            if real_stop:
                await real_stop()


def test_worker_records_failure_when_cleanup_fails(isolated_db, local_site):
    """Verifies that in run_qa_async_in_thread, a cleanup failure marks the run FAILED, not COMPLETED."""
    from playwright.async_api import async_playwright

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO runs (project_id, status, start_time) VALUES (?, 'RUNNING', datetime('now'))",
        (1,),
    )
    run_id = c.lastrowid
    conn.commit()
    conn.close()

    orig_start = async_playwright().start
    real_stop = None

    async def custom_start():
        nonlocal real_stop
        p = await orig_start()
        real_stop = p.stop

        async def failing_stop():
            try:
                await real_stop()
            finally:
                raise RuntimeError("Injected worker driver stop failure")

        p.stop = failing_stop
        return p

    with patch("app.qa.engine.async_playwright") as mock_ap:
        mock_ap.return_value.start = custom_start

        run_qa_async_in_thread(local_site.url, project_id=1, run_id=run_id, mode="crawl")

    conn = get_db_connection()
    run_row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    terminal_test = conn.execute(
        "SELECT expected_result, actual_result, status FROM tests WHERE run_id=? ORDER BY id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    conn.close()

    assert run_row["status"] == "FAILED", f"Expected run status FAILED, got {run_row['status']}"
    assert terminal_test is not None
    assert terminal_test["status"] == "FAIL"
    assert "Injected worker driver stop failure" in terminal_test["actual_result"]


def test_worker_user_stop_preserves_aborted_and_retains_evidence(isolated_db, local_site):
    """Verifies that when a worker is cancelled via cooperative stop, it marks ABORTED and retains evidence."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO runs (project_id, status, start_time) VALUES (?, 'RUNNING', datetime('now'))",
        (1,),
    )
    run_id = c.lastrowid
    conn.commit()
    conn.close()

    worker_thread = threading.Thread(
        target=run_qa_async_in_thread,
        args=(local_site.url, 1, run_id),
        kwargs={"mode": "crawl"},
        daemon=True,
    )
    worker_thread.start()

    # Wait for worker record to be registered and then signal stop
    for _ in range(50):
        if run_id in worker_records and worker_records[run_id].engine:
            worker_records[run_id].cancel.set()
            worker_records[run_id].engine.stop()
            break
        time.sleep(0.05)

    worker_thread.join(timeout=10)
    assert not worker_thread.is_alive(), "Worker thread should have stopped within timeout"

    conn = get_db_connection()
    run_row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    conn.close()

    assert run_row["status"] == "ABORTED", f"Expected ABORTED, got {run_row['status']}"


@pytest.mark.asyncio
async def test_run_shielded_step_repeated_cancellation_preserves_completion():
    """Verifies that _run_shielded_step maintains task shielding across repeated cancellations."""
    cleanup_completed = False
    cleanup_cancelled = False
    wait_event = asyncio.Event()

    async def cleanup_coro():
        nonlocal cleanup_completed, cleanup_cancelled
        try:
            await wait_event.wait()
            cleanup_completed = True
        except asyncio.CancelledError:
            cleanup_cancelled = True
            raise

    async def owner_coro():
        return await SmartSmokeQA._run_shielded_step(cleanup_coro(), timeout=5)

    owner_task = asyncio.create_task(owner_coro())
    await asyncio.sleep(0.01)

    # 1st cancellation
    owner_task.cancel()
    await asyncio.sleep(0.01)

    # 2nd cancellation while owner is in cancellation loop
    owner_task.cancel()
    await asyncio.sleep(0.01)

    # Allow cleanup to complete
    wait_event.set()

    helper_result = await owner_task
    assert helper_result == (None, True), f"Expected (None, True), got {helper_result}"
    assert cleanup_cancelled is False, "Cleanup task should NOT have been cancelled by owner cancellation"
    assert cleanup_completed is True, "Cleanup task must run to completion"


@pytest.mark.asyncio
async def test_run_shielded_step_timeout_truthfully_reported_and_supervisor_consumed():
    """Verifies that _run_shielded_step truthfully reports timeout and cleanly consumes supervisory task."""
    cleanup_started = False
    async def hung_cleanup():
        nonlocal cleanup_started
        cleanup_started = True
        await asyncio.sleep(10)

    exc, was_cancelled = await SmartSmokeQA._run_shielded_step(hung_cleanup(), timeout=0.05)
    assert cleanup_started is True
    assert isinstance(exc, TimeoutError), f"Expected TimeoutError, got {type(exc)}"
    assert "timed out after 0.05 seconds" in str(exc)
    assert was_cancelled is False


@pytest.mark.asyncio
async def test_repeated_cancellation_during_browser_teardown(local_site):
    """Verifies that repeated cancellation during browser.close() does not abort driver stop."""
    from playwright.async_api import async_playwright

    engine = SmartSmokeQA(local_site.url, project_id=1, run_id=6, max_pages=1, mode="crawl")

    orig_start = async_playwright().start
    captured_p = None
    engine_task = None
    close_event = asyncio.Event()

    async def custom_start():
        nonlocal captured_p
        p = await orig_start()
        captured_p = p
        orig_launch = p.chromium.launch

        async def hooked_launch(*args, **kwargs):
            browser = await orig_launch(*args, **kwargs)
            orig_close = browser.close

            async def cancelling_close():
                close_event.set()
                if engine_task:
                    engine_task.cancel()
                    await asyncio.sleep(0.01)
                    engine_task.cancel()
                await asyncio.sleep(0.05)
                return await orig_close()

            browser.close = cancelling_close
            return browser

        p.chromium.launch = hooked_launch
        return p

    with patch("app.qa.engine.async_playwright") as mock_ap:
        mock_ap.return_value.start = custom_start

        engine_task = asyncio.create_task(engine.run())

        with pytest.raises(asyncio.CancelledError):
            await engine_task

        assert close_event.is_set(), "browser.close was not called"
        assert captured_p is not None
        impl = captured_p._impl_obj
        proc = impl._connection._transport._proc
        assert proc.returncode == 0, f"Driver subprocess returncode should be 0, got {proc.returncode}"


@pytest.mark.asyncio
async def test_repeated_cancellation_during_driver_teardown(local_site):
    """Verifies that repeated cancellation during p.stop() completes driver teardown with returncode 0."""
    from playwright.async_api import async_playwright

    engine = SmartSmokeQA(local_site.url, project_id=1, run_id=7, max_pages=1, mode="crawl")

    orig_start = async_playwright().start
    captured_p = None
    engine_task = None
    stop_event = asyncio.Event()

    async def custom_start():
        nonlocal captured_p
        p = await orig_start()
        captured_p = p
        orig_stop = p.stop

        async def cancelling_stop():
            stop_event.set()
            if engine_task:
                engine_task.cancel()
                await asyncio.sleep(0.01)
                engine_task.cancel()
            await asyncio.sleep(0.05)
            return await orig_stop()

        p.stop = cancelling_stop
        return p

    with patch("app.qa.engine.async_playwright") as mock_ap:
        mock_ap.return_value.start = custom_start

        engine_task = asyncio.create_task(engine.run())

        with pytest.raises(asyncio.CancelledError):
            await engine_task

        assert stop_event.is_set(), "p.stop was not called"
        assert captured_p is not None
        impl = captured_p._impl_obj
        proc = impl._connection._transport._proc
        assert proc.returncode == 0, f"Driver subprocess returncode should be 0, got {proc.returncode}"


@pytest.mark.asyncio
async def test_combined_run_failure_and_teardown_failure(local_site):
    """Verifies that if run fails AND teardown fails, both are truthfully reported and not swallowed."""
    from playwright.async_api import async_playwright

    engine = SmartSmokeQA(local_site.url, project_id=1, run_id=8, max_pages=1, mode="crawl")

    orig_start = async_playwright().start
    real_stop = None

    async def custom_start():
        nonlocal real_stop
        p = await orig_start()
        real_stop = p.stop

        async def failing_stop():
            try:
                await real_stop()
            finally:
                raise RuntimeError("Teardown stop error E2")

        p.stop = failing_stop

        orig_launch = p.chromium.launch
        async def failing_launch(*args, **kwargs):
            b = await orig_launch(*args, **kwargs)
            async def failing_new_context(*c_args, **c_kwargs):
                raise RuntimeError("Primary run error E1")
            b.new_context = failing_new_context
            return b

        p.chromium.launch = failing_launch
        return p

    with patch("app.qa.engine.async_playwright") as mock_ap:
        mock_ap.return_value.start = custom_start

        with pytest.raises(RuntimeError) as exc_info:
            await engine.run()

        assert "Primary run error E1" in str(exc_info.value)
        assert engine.cleanup_error is not None
        assert "Teardown stop error E2" in str(engine.cleanup_error)


def test_mark_terminal_escalates_aborted_to_failed_and_preserves_evidence(isolated_db):
    """Verifies _mark_terminal escalates ABORTED to FAILED and preserves all diagnostic test records."""
    from app.routes.api import _mark_terminal

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO runs (project_id, status, start_time) VALUES (?, 'RUNNING', datetime('now'))",
        (1,),
    )
    run_id = c.lastrowid
    # Pre-existing check evidence
    c.execute(
        "INSERT INTO tests (run_id, url, expected_result, actual_result, status, viewport) VALUES (?, 'http://example.com', 'Page loads', '200 OK', 'PASS', 'Desktop')",
        (run_id,),
    )
    conn.commit()
    conn.close()

    # Step 1: Initial shutdown marks ABORTED
    _mark_terminal(run_id, "Shutdown requested", status="ABORTED")

    conn = get_db_connection()
    r1 = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    t1 = [row["actual_result"] for row in conn.execute("SELECT actual_result FROM tests WHERE run_id=?", (run_id,)).fetchall()]
    conn.close()
    assert r1 == "ABORTED"
    assert "200 OK" in t1
    assert "Shutdown requested" in t1

    # Step 2: Subsequent cleanup failure escalates to FAILED
    _mark_terminal(run_id, "Driver cleanup failed", status="FAILED")

    conn = get_db_connection()
    r2 = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    t2 = [row["actual_result"] for row in conn.execute("SELECT actual_result FROM tests WHERE run_id=?", (run_id,)).fetchall()]
    conn.close()
    assert r2 == "FAILED", f"Status should escalate to FAILED, got {r2}"
    assert "200 OK" in t2, "Prior evidence must be preserved"
    assert "Shutdown requested" in t2, "Original shutdown diagnostic must be preserved"
    assert "Driver cleanup failed" in t2, "Subsequent cleanup failure must be recorded"


def test_actual_shutdown_followed_by_cleanup_failure(isolated_db, local_site):
    """Verifies that an actual worker thread interrupted by shutdown_qa_workers records FAILED on cleanup failure."""
    from playwright.async_api import async_playwright
    from app.routes.api import shutdown_qa_workers, active_worker_threads

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO runs (project_id, status, start_time) VALUES (?, 'RUNNING', datetime('now'))",
        (1,),
    )
    run_id = c.lastrowid
    conn.commit()
    conn.close()

    orig_start = async_playwright().start
    real_stop = None
    started_event = threading.Event()

    async def custom_start():
        nonlocal real_stop
        p = await orig_start()
        real_stop = p.stop

        async def failing_stop():
            try:
                await real_stop()
            finally:
                raise RuntimeError("Driver cleanup failure during shutdown")

        p.stop = failing_stop
        started_event.set()
        return p

    with patch("app.qa.engine.async_playwright") as mock_ap:
        mock_ap.return_value.start = custom_start

        worker_thread = threading.Thread(
            target=run_qa_async_in_thread,
            args=(local_site.url, 1, run_id),
            kwargs={"mode": "crawl"},
            daemon=True,
        )
        active_worker_threads.append(worker_thread)
        worker_thread.start()

        # Wait until worker has started playwright
        assert started_event.wait(timeout=5), "Worker should have started playwright"

        try:
            # Trigger application shutdown
            import app.routes.api as api
            previous_accepting_runs = api.accepting_runs
            shutdown_qa_workers()

            worker_thread.join(timeout=10)
            assert not worker_thread.is_alive(), "Worker should have joined"
        finally:
            import app.routes.api as api
            with api.worker_lock:
                assert not worker_thread.is_alive(), 'Do not hide a live worker by clearing its registry'
                assert not any(t.is_alive() for t in api.active_worker_threads)
                api.accepting_runs = previous_accepting_runs
                api.worker_records.clear()
                api.running_engines.clear()
                api.active_worker_threads.clear()

    conn = get_db_connection()
    run_row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    tests = [row["actual_result"] for row in conn.execute("SELECT actual_result FROM tests WHERE run_id=?", (run_id,)).fetchall()]
    conn.close()

    assert run_row["status"] == "FAILED", f"Expected FAILED, got {run_row['status']}"
    assert any("Driver cleanup failure during shutdown" in t for t in tests), f"Cleanup failure must be recorded in tests, got {tests}"

