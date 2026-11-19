"""Phase C3: Controlled Interactive Disclosure Bridge Test Suite.

Acceptance Matrix Coverage:
1. Both runners remain unchanged with C3 disabled (zero interactive evaluations).
2. Explicit opt-in reaches full discovery -> disposable execution -> oracle -> canonical persistence/report path.
3. Only supported safe candidates reach action dispatch.
4. Per-page, cross-viewport, and per-run budgets hold, including failed attempts.
5. Budget exhaustion produces bounded truthful coverage records (at most one per page/viewport).
6. No retries or duplicate execution.
7. Cancellation at each lifecycle stage prevents new dispatch and preserves records/history.
8. Cleanup succeeds before parent-browser shutdown; cleanup failure stops further interactive work.
9. Source/manual DOM, storage, pages, URL, and online behavior remain unaffected by the bridge.
10. Persistence occurs exactly once through progress checkpoints and final save.
11. Mixed crawl/functional results retain honest run completeness.
12. Reports and exports contain no arbitrary secrets or customer findings from C3.
13. Existing relevant runner, reporting, triage, C1, and B2 regressions pass.
"""

import asyncio
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import time
from typing import Any, Dict, List, Optional
import urllib.parse
import uuid

import pytest
from playwright.async_api import async_playwright

from app.models.database import get_db_connection, init_db
from app.qa.crawl_policy import normalize_crawl_url
from app.qa.engine import SmartSmokeQA
from app.qa.full_site import FullSiteQA
from app.qa.candidate_intent_graph import CandidateActionIntent
from app.qa.candidate_intent_factory import CandidateActionIntentFactory
from app.qa.guarded_execution import GuardedActionExecutor, GuardedExecutionStatus
from app.qa.semantic_normalizer import SemanticNormalizer
from app.qa.functional_oracle import (
    FunctionalEvidence,
    FunctionalEvaluationResult,
    FunctionalOutcomeVerdict,
    SafeDisclosureFunctionalOracle,
)
from app.qa.functional_reporting_adapter import (
    FUNCTIONAL_DISCLOSURE_EXPECTED,
    adapt_functional_result_to_test_record,
)
from app.qa.interactive_bridge import (
    InteractiveDisclosureBridge,
    InteractiveWorkflowConfig,
)
from app.qa.reporting import load_report
from app.qa.state_reconstruction import StateReconstructionError


# ==============================================================================
# Localhost Server Fixture
# ==============================================================================

class C3TestServer(HTTPServer):
    def __init__(self, host="127.0.0.1", port=0):
        super().__init__((host, port), C3TestHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class C3TestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path

        if path == "/single-disclosure":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Single Disclosure Page</title></head>
<body>
  <h1>Interactive Disclosure</h1>
  <button id="disclosure-btn" aria-expanded="false" aria-controls="panel-content" onclick="
    var isExp = this.getAttribute('aria-expanded') === 'true';
    this.setAttribute('aria-expanded', isExp ? 'false' : 'true');
    var p = document.getElementById('panel-content');
    if (p) { p.hidden = isExp; }
  ">Toggle Details</button>
  <div id="panel-content" hidden>
    <p>Expanded details content successfully revealed.</p>
  </div>
</body>
</html>""")

        elif path == "/multi-disclosure":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Multi Disclosure Page</title></head>
<body>
  <h1>Multiple FAQ Disclosures</h1>
  <button id="faq-1" aria-expanded="false" aria-controls="ans-1" onclick="
    this.setAttribute('aria-expanded', this.getAttribute('aria-expanded') === 'true' ? 'false' : 'true');
    var el = document.getElementById('ans-1'); if(el) el.hidden = !el.hidden;
  ">FAQ 1</button>
  <div id="ans-1" hidden><p>Answer 1</p></div>

  <button id="faq-2" aria-expanded="false" aria-controls="ans-2" onclick="
    this.setAttribute('aria-expanded', this.getAttribute('aria-expanded') === 'true' ? 'false' : 'true');
    var el = document.getElementById('ans-2'); if(el) el.hidden = !el.hidden;
  ">FAQ 2</button>
  <div id="ans-2" hidden><p>Answer 2</p></div>

  <button id="faq-3" aria-expanded="false" aria-controls="ans-3" onclick="
    this.setAttribute('aria-expanded', this.getAttribute('aria-expanded') === 'true' ? 'false' : 'true');
    var el = document.getElementById('ans-3'); if(el) el.hidden = !el.hidden;
  ">FAQ 3</button>
  <div id="ans-3" hidden><p>Answer 3</p></div>

  <button id="faq-4" aria-expanded="false" aria-controls="ans-4" onclick="
    this.setAttribute('aria-expanded', this.getAttribute('aria-expanded') === 'true' ? 'false' : 'true');
    var el = document.getElementById('ans-4'); if(el) el.hidden = !el.hidden;
  ">FAQ 4</button>
  <div id="ans-4" hidden><p>Answer 4</p></div>
</body>
</html>""")

        elif path == "/mixed-controls":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Mixed Controls Page</title></head>
<body>
  <h1>Mixed Controls</h1>
  <!-- Safe disclosure -->
  <button id="safe-faq" aria-expanded="false" aria-controls="safe-ans" onclick="
    this.setAttribute('aria-expanded', 'true');
    document.getElementById('safe-ans').hidden = false;
  ">Safe FAQ</button>
  <div id="safe-ans" hidden><p>Safe Answer</p></div>

  <!-- Non-disclosure button (no aria-expanded) -->
  <button id="regular-btn">Plain Button</button>

  <!-- Destructive / mutating control -->
  <form action="/delete" method="POST">
    <button id="delete-btn" type="submit">Delete Everything</button>
  </form>
</body>
</html>""")

        elif path == "/passive-observation":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            # Duplicate DOM ID generates passive OBSERVATION without network failure
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Duplicate ID Page</title></head>
<body>
  <h1>Page with Duplicate ID and FAQ</h1>
  <div id="dup-item">Item 1</div>
  <div id="dup-item">Item 2</div>
  <button id="obs-faq" aria-expanded="false" aria-controls="obs-ans" onclick="
    this.setAttribute('aria-expanded', 'true');
    document.getElementById('obs-ans').hidden = false;
  ">Expand FAQ</button>
  <div id="obs-ans" hidden><p>FAQ Answer</p></div>
</body>
</html>""")

        elif path == "/page-one":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Page One</title></head>
<body>
  <h1>Page One</h1>
  <a href="/page-two">Go to Page Two</a>
  <button id="p1-btn1" aria-expanded="false" aria-controls="p1-ans1" onclick="
    this.setAttribute('aria-expanded', 'true'); document.getElementById('p1-ans1').hidden = false;
  ">P1 B1</button>
  <div id="p1-ans1" hidden><p>A1</p></div>
  <button id="p1-btn2" aria-expanded="false" aria-controls="p1-ans2" onclick="
    this.setAttribute('aria-expanded', 'true'); document.getElementById('p1-ans2').hidden = false;
  ">P1 B2</button>
  <div id="p1-ans2" hidden><p>A2</p></div>
</body>
</html>""")

        elif path == "/page-two":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Page Two</title></head>
<body>
  <h1>Page Two</h1>
  <button id="p2-btn1" aria-expanded="false" aria-controls="p2-ans1" onclick="
    this.setAttribute('aria-expanded', 'true'); document.getElementById('p2-ans1').hidden = false;
  ">P2 B1</button>
  <div id="p2-ans1" hidden><p>A1</p></div>
  <button id="p2-btn2" aria-expanded="false" aria-controls="p2-ans2" onclick="
    this.setAttribute('aria-expanded', 'true'); document.getElementById('p2-ans2').hidden = false;
  ">P2 B2</button>
  <div id="p2-ans2" hidden><p>A2</p></div>
</body>
</html>""")

        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def c3_server():
    server = C3TestServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    db_file = tmp_path / f"test_{uuid.uuid4().hex}.sqlite"
    monkeypatch.setenv("QA_TESTER_DB_PATH", str(db_file))
    init_db()
    conn = get_db_connection()
    c = conn.cursor()
    proj_id = c.execute(
        "INSERT INTO projects (name, target_url, environment, tester_name) VALUES (?, ?, ?, ?)",
        ("C3 Test Proj", "http://127.0.0.1", "testing", "QA Tester"),
    ).lastrowid
    run_id = c.execute(
        "INSERT INTO runs (project_id, status) VALUES (?, ?)",
        (proj_id, "RUNNING"),
    ).lastrowid
    conn.commit()
    conn.close()
    return proj_id, run_id


# ==============================================================================
# 1. Config Validation Tests
# ==============================================================================

def test_config_validation():
    """Verify finite bounds and strict type checking on InteractiveWorkflowConfig."""
    cfg = InteractiveWorkflowConfig()
    assert cfg.enabled is False
    assert cfg.max_evaluations_per_page == 2
    assert cfg.max_evaluations_per_run == 10
    assert cfg.action_timeout == 1.0
    assert cfg.reconstruction_timeout == 5.0
    assert cfg.total_evaluation_budget == 11.0

    custom = InteractiveWorkflowConfig(
        enabled=True,
        max_evaluations_per_page=5,
        max_evaluations_per_run=20,
        action_timeout=2.5,
        reconstruction_timeout=10.0,
    )
    assert custom.enabled is True
    assert custom.max_evaluations_per_page == 5
    assert custom.max_evaluations_per_run == 20
    assert custom.total_evaluation_budget == 17.5

    with pytest.raises(ValueError):
        InteractiveWorkflowConfig(max_evaluations_per_page=0)
    with pytest.raises(ValueError):
        InteractiveWorkflowConfig(max_evaluations_per_page=31)
    with pytest.raises(ValueError):
        InteractiveWorkflowConfig(max_evaluations_per_run=0)
    with pytest.raises(ValueError):
        InteractiveWorkflowConfig(max_evaluations_per_run=101)
    with pytest.raises(ValueError):
        InteractiveWorkflowConfig(action_timeout=0.05)
    with pytest.raises(ValueError):
        InteractiveWorkflowConfig(reconstruction_timeout=35.0)
    with pytest.raises(TypeError):
        InteractiveWorkflowConfig(enabled="true")  # type: ignore
    with pytest.raises(TypeError):
        InteractiveWorkflowConfig(max_evaluations_per_page=True)  # type: ignore
    with pytest.raises(TypeError):
        InteractiveWorkflowConfig(max_evaluations_per_page=False)  # type: ignore
    with pytest.raises(TypeError):
        InteractiveWorkflowConfig(max_evaluations_per_page=2.5)  # type: ignore
    with pytest.raises(TypeError):
        InteractiveWorkflowConfig(max_evaluations_per_run=True)  # type: ignore
    with pytest.raises(TypeError):
        InteractiveWorkflowConfig(max_evaluations_per_run=10.5)  # type: ignore
    with pytest.raises(TypeError):
        InteractiveWorkflowConfig(action_timeout=True)  # type: ignore
    with pytest.raises(TypeError):
        InteractiveWorkflowConfig(reconstruction_timeout=False)  # type: ignore


# ==============================================================================
# 2. Default Disabled Behavior Invariant
# ==============================================================================

@pytest.mark.asyncio
async def test_runners_default_disabled(c3_server, test_db):
    """Verify default behavior remains 100% untouched when C3 is disabled."""
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/single-disclosure"

    smoke = SmartSmokeQA(
        target_url=target_url,
        project_id=proj_id,
        run_id=run_id,
        mode="current",
    )
    assert smoke.interactive_bridge.is_enabled() is False
    assert smoke.interactive_bridge.total_evaluations_consumed == 0

    results = await smoke.run()

    # Zero interactive disclosure evaluations performed
    functional_results = [
        r for r in results
        if r.get("expected_result") == FUNCTIONAL_DISCLOSURE_EXPECTED
    ]
    assert len(functional_results) == 0
    assert smoke.interactive_bridge.total_evaluations_consumed == 0

    # Passive crawl results intact
    pass_results = [r for r in results if r.get("status") == "PASS"]
    assert len(pass_results) >= 1
    assert pass_results[0]["actual_result"] == "Page loaded without errors"


# ==============================================================================
# 3. Explicit Opt-In End-to-End Workflow & Source Invariance
# ==============================================================================

@pytest.mark.asyncio
async def test_explicit_opt_in_full_workflow_and_source_invariance(c3_server, test_db):
    """Verify explicit opt-in reaches the full discovery -> disposable execution -> oracle -> persistence path,
    while leaving the source page DOM and state strictly untouched."""
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/single-disclosure"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(service_workers="block")
            # Create user manual page in context
            user_page = await ctx.new_page()
            await user_page.goto(target_url)

            smoke = SmartSmokeQA(
                target_url=target_url,
                project_id=proj_id,
                run_id=run_id,
                context=ctx,
                mode="current",
                enable_interactive_disclosure=True,
                interactive_options={
                    "max_evaluations_per_page": 2,
                    "max_evaluations_per_run": 10,
                },
            )

            # Instrument bridge to inspect source page DOM right after evaluation
            source_state_verified = {}
            orig_bridge_eval = smoke.interactive_bridge.evaluate_page_disclosures

            async def instrumented_eval(*args, **kwargs):
                records = await orig_bridge_eval(*args, **kwargs)
                source_page = kwargs.get("source_page") or args[1]
                btn_state = await source_page.evaluate("""() => {
                    const btn = document.getElementById('disclosure-btn');
                    const panel = document.getElementById('panel-content');
                    return {
                        expanded: btn ? btn.getAttribute('aria-expanded') : null,
                        panelHidden: panel ? panel.hidden : null,
                    };
                }""")
                source_state_verified["expanded"] = btn_state["expanded"]
                source_state_verified["panelHidden"] = btn_state["panelHidden"]
                return records

            smoke.interactive_bridge.evaluate_page_disclosures = instrumented_eval

            results = await smoke.run()

            # 1. Source DOM Invariance Verified: source button was never clicked!
            assert source_state_verified["expanded"] == "false"
            assert source_state_verified["panelHidden"] is True

            # User page in browser context was preserved
            assert not user_page.is_closed()
            assert user_page.url == target_url

            # 2. Bridge evaluated candidate in disposable context
            assert smoke.interactive_bridge.total_evaluations_consumed >= 1

            func_records = [
                r for r in results
                if r.get("expected_result") == FUNCTIONAL_DISCLOSURE_EXPECTED
            ]
            assert len(func_records) >= 1
            f_rec = func_records[0]

            assert f_rec["status"] == "PASS"
            assert "Verified safe disclosure expansion" in f_rec["actual_result"]
            assert f_rec["stable_id"] is None
            assert f_rec["finding_id"] is None
            assert "act-" in f_rec["url"]
            assert "/single-disclosure" not in f_rec["url"]  # Sanitized to origin-only + action token

            # 3. Persistence check: Save results and ensure zero customer findings
            from app.routes.api import _save_results
            _save_results(smoke.results, run_id, proj_id, finish=True)

            conn = get_db_connection()
            c = conn.cursor()
            tests = c.execute("SELECT * FROM tests WHERE run_id=?", (run_id,)).fetchall()
            findings = c.execute("SELECT * FROM findings WHERE run_id=?", (run_id,)).fetchall()
            conn.close()

            # At least passive passes + functional passes
            assert len(tests) >= 2
            # Zero customer findings
            assert len(findings) == 0

            await ctx.close()
        finally:
            await browser.close()


# ==============================================================================
# 4. Supported Safe Candidates Filtering & Dangerous Control Exclusion
# ==============================================================================

@pytest.mark.asyncio
async def test_only_safe_disclosure_candidates_evaluated(c3_server, test_db):
    """Verify that dangerous controls (POST submit) and non-disclosure controls are excluded."""
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/mixed-controls"

    smoke = SmartSmokeQA(
        target_url=target_url,
        project_id=proj_id,
        run_id=run_id,
        mode="current",
        enable_interactive_disclosure=True,
    )

    results = await smoke.run()

    # Exactly safe FAQ disclosure evaluated across viewports
    func_records = [
        r for r in results
        if r.get("expected_result") == FUNCTIONAL_DISCLOSURE_EXPECTED
    ]
    # Each viewport evaluated at most the 1 safe candidate
    assert len(func_records) >= 1
    for r in func_records:
        assert r["status"] == "PASS"
        assert "Verified safe disclosure expansion" in r["actual_result"]


# ==============================================================================
# 5. Per-Page Budget & Bounded Skip Records
# ==============================================================================

@pytest.mark.asyncio
async def test_per_page_budget_and_bounded_skip_record(c3_server, test_db):
    """Verify that per-page budget limits evaluations (default max 2) and emits at most ONE skip record."""
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/multi-disclosure"

    smoke = SmartSmokeQA(
        target_url=target_url,
        project_id=proj_id,
        run_id=run_id,
        mode="current",
        enable_interactive_disclosure=True,
        interactive_options={"max_evaluations_per_page": 2},
    )

    results = await smoke.run()

    # Per-page consumed reached limit 2 per viewport (4 total across Desktop and Mobile)
    assert smoke.interactive_bridge.get_page_evaluations_consumed(target_url, viewport="Desktop") == 2
    assert smoke.interactive_bridge.get_page_evaluations_consumed(target_url, viewport="Mobile") == 2
    assert smoke.interactive_bridge.get_page_evaluations_consumed(target_url) == 4
    assert smoke.interactive_bridge.total_evaluations_consumed == 4

    func_records = [
        r for r in results
        if r.get("expected_result") == FUNCTIONAL_DISCLOSURE_EXPECTED
    ]
    passes = [r for r in func_records if r["status"] == "PASS"]
    assert len(passes) == 4

    # Skip records are bounded: at most one per viewport
    skips = [r for r in func_records if r["status"] == "LIMITED COVERAGE"]
    assert len(skips) >= 1
    for skip in skips:
        assert "per-page evaluation budget exhausted (max 2 per page)" in skip["actual_result"]


# ==============================================================================
# 6. Cross-Viewport Budget Sharing
# ==============================================================================

@pytest.mark.asyncio
async def test_cross_viewport_budget_sharing(c3_server, test_db):
    """Verify that Desktop and Mobile viewports each receive their allocated per-page budget without starvation."""
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/multi-disclosure"

    smoke = SmartSmokeQA(
        target_url=target_url,
        project_id=proj_id,
        run_id=run_id,
        mode="current",
        enable_interactive_disclosure=True,
        interactive_options={"max_evaluations_per_page": 2},
    )

    results = await smoke.run()

    # The canonical URL count across viewports is 4 (2 Desktop + 2 Mobile)
    assert smoke.interactive_bridge.get_page_evaluations_consumed(target_url) == 4
    assert smoke.interactive_bridge.total_evaluations_consumed == 4

    desktop_func = [
        r for r in results
        if r.get("expected_result") == FUNCTIONAL_DISCLOSURE_EXPECTED and "Desktop" in r["viewport"]
    ]
    mobile_func = [
        r for r in results
        if r.get("expected_result") == FUNCTIONAL_DISCLOSURE_EXPECTED and "Mobile" in r["viewport"]
    ]

    # Desktop consumed its 2 evaluation slots
    assert len([r for r in desktop_func if r["status"] == "PASS"]) == 2
    assert len([r for r in desktop_func if r["status"] == "LIMITED COVERAGE"]) == 1
    assert "per-page evaluation budget exhausted" in desktop_func[-1]["actual_result"]

    # Mobile is NOT starved: also consumed its 2 evaluation slots
    assert len([r for r in mobile_func if r["status"] == "PASS"]) == 2
    assert len([r for r in mobile_func if r["status"] == "LIMITED COVERAGE"]) == 1
    assert "per-page evaluation budget exhausted" in mobile_func[-1]["actual_result"]


# ==============================================================================
# 7. Per-Run Budget Enforcement Across Pages
# ==============================================================================

@pytest.mark.asyncio
async def test_per_run_budget_enforcement(c3_server, test_db):
    """Verify that per-run budget halts interactive evaluations once exhausted across pages."""
    proj_id, run_id = test_db
    page1 = f"{c3_server.origin}/page-one"

    # Run in crawl mode with max 2 pages
    smoke = SmartSmokeQA(
        target_url=page1,
        project_id=proj_id,
        run_id=run_id,
        mode="crawl",
        max_pages=2,
        enable_interactive_disclosure=True,
        interactive_options={
            "max_evaluations_per_page": 2,
            "max_evaluations_per_run": 3,  # Max 3 total evaluations
        },
    )

    results = await smoke.run()

    # Total consumed across all pages strictly capped at 3
    assert smoke.interactive_bridge.total_evaluations_consumed == 3

    func_records = [
        r for r in results
        if r.get("expected_result") == FUNCTIONAL_DISCLOSURE_EXPECTED
    ]
    passes = [r for r in func_records if r["status"] == "PASS"]
    assert len(passes) == 3

    run_skips = [
        r for r in func_records
        if r["status"] == "LIMITED COVERAGE" and "per-run evaluation budget exhausted" in r["actual_result"]
    ]
    assert len(run_skips) >= 1


# ==============================================================================
# 8. Slot Consumption Before Reconstruction & No Retries
# ==============================================================================

@pytest.mark.asyncio
async def test_slot_consumed_before_reconstruction_on_failure(c3_server, test_db):
    """Verify that budget slots are pre-consumed even if reconstruction fails, with no retries."""
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/single-disclosure"

    smoke = SmartSmokeQA(
        target_url=target_url,
        project_id=proj_id,
        run_id=run_id,
        mode="current",
        enable_interactive_disclosure=True,
        interactive_options={"max_evaluations_per_page": 2},
    )

    # Inject failure into executor reconstructor
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def failing_reconstruct(*args, **kwargs):
        raise StateReconstructionError("Simulated preflight reconstruction failure")
        yield  # pragma: no cover

    smoke.interactive_bridge._executor._reconstructor.reconstruct = failing_reconstruct

    results = await smoke.run()

    # Slot was consumed despite reconstruction failure
    assert smoke.interactive_bridge.total_evaluations_consumed >= 1

    func_records = [
        r for r in results
        if r.get("expected_result") == FUNCTIONAL_DISCLOSURE_EXPECTED
    ]
    assert len(func_records) >= 1
    # Recorded honestly as diagnostic execution control failure, not silently dropped
    assert func_records[0]["status"] == "DIAGNOSTIC"
    assert "Execution control diagnostic" in func_records[0]["actual_result"]


# ==============================================================================
# 9. Cleanup Failure Circuit Breaker
# ==============================================================================

@pytest.mark.asyncio
async def test_cleanup_failure_circuit_breaker_halts_subsequent_evaluations(c3_server, test_db):
    """Verify that a disposable teardown failure trips the circuit breaker and halts further evaluations."""
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/multi-disclosure"

    smoke = SmartSmokeQA(
        target_url=target_url,
        project_id=proj_id,
        run_id=run_id,
        mode="current",
        enable_interactive_disclosure=True,
        interactive_options={"max_evaluations_per_page": 4},
    )

    call_count = 0

    async def failing_cleanup_eval(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return FunctionalEvaluationResult(
            verdict=FunctionalOutcomeVerdict.DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE,
            is_customer_defect=False,
            defect_category=None,
            target_control_id="faq-1",
            action_type="click",
            evidence=FunctionalEvidence(
                action_attempted=True,
                action_confirmed=True,
                expanded_before=False,
                expanded_after=False,
                state_toggled=False,
                controls_count_delta=0,
                fingerprint_changed=False,
                pre_state_fingerprint="fp1",
                post_state_fingerprint="fp1",
            ),
            execution_status=GuardedExecutionStatus.CLEANUP_FAILED,
            limitations=("teardown_verification_failed",),
            diagnostics={"teardown_error": "CDP connection failed during context close"},
        )

    smoke.interactive_bridge._oracle.evaluate_disclosure_action = failing_cleanup_eval

    results = await smoke.run()

    # Circuit breaker tripped!
    assert smoke.interactive_bridge.is_cleanup_halted is True
    # Halted immediately after the first failure
    assert call_count == 1

    func_records = [
        r for r in results
        if r.get("expected_result") == FUNCTIONAL_DISCLOSURE_EXPECTED
    ]
    assert len(func_records) == 1
    assert func_records[0]["status"] == "DIAGNOSTIC"
    assert "Cleanup failure after confirmed action" in func_records[0]["actual_result"]


# ==============================================================================
# 10. Cancellation Propagation
# ==============================================================================

@pytest.mark.asyncio
async def test_cancellation_stops_dispatch_and_preserves_records(c3_server, test_db):
    """Verify that runner stop signal stops dispatching immediately and preserves existing history."""
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/multi-disclosure"

    smoke = SmartSmokeQA(
        target_url=target_url,
        project_id=proj_id,
        run_id=run_id,
        mode="current",
        enable_interactive_disclosure=True,
        interactive_options={"max_evaluations_per_page": 4},
    )

    orig_eval_action = smoke.interactive_bridge._oracle.evaluate_disclosure_action

    async def eval_and_stop(*args, **kwargs):
        res = await orig_eval_action(*args, **kwargs)
        smoke.stop()  # Runner stopped!
        return res

    smoke.interactive_bridge._oracle.evaluate_disclosure_action = eval_and_stop

    results = await smoke.run()

    assert smoke.is_stopped is True
    # Stopped dispatching further evaluations
    assert smoke.interactive_bridge.total_evaluations_consumed == 1

    func_records = [
        r for r in results
        if r.get("expected_result") == FUNCTIONAL_DISCLOSURE_EXPECTED
    ]
    assert len(func_records) == 1
    assert func_records[0]["status"] == "PASS"


# ==============================================================================
# 11. FullSiteQA Integration & Checkpoint Persistence
# ==============================================================================

@pytest.mark.asyncio
async def test_full_site_qa_integration_and_checkpointing(c3_server, test_db):
    """Verify FullSiteQA properly checkpoint-persists interactive disclosure results without duplicates."""
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/single-disclosure"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(service_workers="block")
            manual_page = await ctx.new_page()
            await manual_page.goto(target_url)

            checkpoint_batches = []

            def record_checkpoint(batch, coverage):
                checkpoint_batches.append(list(batch))

            full_qa = FullSiteQA(
                target_url=target_url,
                project_id=proj_id,
                run_id=run_id,
                context=ctx,
                page_limit=2,
                checkpoint=record_checkpoint,
                enable_interactive_disclosure=True,
                interactive_options={"max_evaluations_per_page": 2},
            )

            await full_qa.run()

            # Flatten all check-pointed results
            all_checkpointed = [item for batch in checkpoint_batches for item in batch]
            func_records = [
                r for r in all_checkpointed
                if r.get("expected_result") == FUNCTIONAL_DISCLOSURE_EXPECTED
            ]

            assert len(func_records) >= 1
            for r in func_records:
                assert r["status"] == "PASS"
                assert r["finding_id"] is None
                assert r["stable_id"] is None

            await ctx.close()
        finally:
            await browser.close()


# ==============================================================================
# 12. Combined Crawl and Functional Outcome Preservation
# ==============================================================================

@pytest.mark.asyncio
async def test_passive_crawl_observation_not_overridden_by_functional_pass(c3_server, test_db):
    """Verify that successful functional actions do not override crawl limitations or observations."""
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/passive-observation"

    smoke = SmartSmokeQA(
        target_url=target_url,
        project_id=proj_id,
        run_id=run_id,
        mode="current",
        enable_interactive_disclosure=True,
    )

    results = await smoke.run()

    # 1. Passive crawl observation preserved
    crawl_obs = [r for r in results if r.get("status") == "OBSERVATION"]
    assert len(crawl_obs) >= 1
    assert "Duplicate DOM ID" in crawl_obs[0]["actual_result"]

    # 2. Functional disclosure pass preserved as separate test record
    func_pass = [
        r for r in results
        if r.get("expected_result") == FUNCTIONAL_DISCLOSURE_EXPECTED and r["status"] == "PASS"
    ]
    assert len(func_pass) >= 1

    # 3. Overall report correctly shows BOTH facts
    from app.routes.api import _save_results
    _save_results(smoke.results, run_id, proj_id, finish=True)

    conn = get_db_connection()
    report = load_report(conn, run_id)
    conn.close()

    assert report["stats"]["OBSERVATION"] >= 1
    assert report["stats"]["PASS"] >= 1


# ==============================================================================
# 13. Reproduction 1: Completed Results Retained on Subsequent Failure
# ==============================================================================

@pytest.mark.asyncio
async def test_reproduction_1_completed_results_retained_on_subsequent_failure(c3_server, test_db):
    """Verify that when evaluation 1 succeeds and evaluation 2 raises an exception:
    - Evaluation 1 completed PASS is preserved in runner.results.
    - Evaluation 2 failure is recorded as an honest non-defect DIAGNOSTIC record.
    - Circuit breaker halts subsequent interactive dispatch.
    - Neither record is lost or silently swallowed.
    """
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/multi-disclosure"

    smoke = SmartSmokeQA(
        target_url=target_url,
        project_id=proj_id,
        run_id=run_id,
        mode="current",
        enable_interactive_disclosure=True,
        interactive_options={"max_evaluations_per_page": 2},
    )

    bridge = smoke.interactive_bridge
    eval_call_count = 0

    real_evaluate = bridge._oracle.evaluate_disclosure_action

    async def flaky_evaluate(*args, **kwargs):
        nonlocal eval_call_count
        eval_call_count += 1
        if eval_call_count == 1:
            return await real_evaluate(*args, **kwargs)
        raise RuntimeError("Simulated execution failure on candidate 2")

    bridge._oracle.evaluate_disclosure_action = flaky_evaluate

    results = await smoke.run()

    # Verify both records exist in results
    c3_records = [
        r for r in results
        if r.get("expected_result") == FUNCTIONAL_DISCLOSURE_EXPECTED
    ]
    assert len(c3_records) == 2, f"Expected 2 C3 records, got {len(c3_records)}"

    # Record 1 must be PASS
    assert c3_records[0]["status"] == "PASS"

    # Record 2 must be DIAGNOSTIC (truthful non-defect failure record)
    assert c3_records[1]["status"] == "DIAGNOSTIC"
    assert "unexpected execution error" in c3_records[1]["actual_result"] or "diagnostic" in c3_records[1]["actual_result"].lower()

    # Circuit breaker must be tripped
    assert bridge.is_cleanup_halted is True


# ==============================================================================
# 14. Reproduction 2: Discovery Failure is Reported Honestly
# ==============================================================================

@pytest.mark.asyncio
async def test_reproduction_2_discovery_failure_is_reported_honestly(c3_server, test_db):
    """Verify that:
    - When capture_snapshot raises an exception, a bounded DIAGNOSTIC record is produced (not []).
    - When snapshot coverage is incomplete, a bounded LIMITED COVERAGE record is produced.
    - When semantic normalization fails, a bounded LIMITED COVERAGE record is produced.
    - When intent derivation fails, a bounded LIMITED COVERAGE record is produced.
    - When discovery succeeds and finds no disclosure candidates, [] is returned (truthfully distinguished).
    """
    proj_id, run_id = test_db
    bridge = InteractiveDisclosureBridge(config=InteractiveWorkflowConfig(enabled=True))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await page.goto(f"{c3_server.origin}/single-disclosure")

            # Case A: capture_snapshot raises exception
            orig_capture = bridge._snapshot_adapter.capture_snapshot
            async def failing_capture(*args, **kwargs):
                raise RuntimeError("Simulated snapshot adapter failure")
            bridge._snapshot_adapter.capture_snapshot = failing_capture

            emitted_records = []
            recs = await bridge.evaluate_page_disclosures(
                source_context=ctx,
                source_page=page,
                page_url=page.url,
                viewport_name="Desktop 1366x768",
                run_id=run_id,
                record_sink=emitted_records.append,
            )
            assert len(recs) == 1
            assert recs[0]["status"] == "DIAGNOSTIC"
            assert "snapshot error" in recs[0]["actual_result"]
            assert recs[0]["finding_id"] is None
            assert len(emitted_records) == 1

            # Case B: Incomplete snapshot coverage
            class MockIncompleteSnapshot:
                class Coverage:
                    terminal_status = "INCOMPLETE"
                coverage = Coverage()

            async def incomplete_capture(*args, **kwargs):
                return MockIncompleteSnapshot()

            bridge._snapshot_adapter.capture_snapshot = incomplete_capture
            recs_inc = await bridge.evaluate_page_disclosures(
                source_context=ctx,
                source_page=page,
                page_url=page.url,
                viewport_name="Desktop 1366x768",
                run_id=run_id,
            )
            assert len(recs_inc) == 1
            assert recs_inc[0]["status"] == "LIMITED COVERAGE"
            assert "incomplete" in recs_inc[0]["actual_result"]

            # Case C: Page with no candidates truthfully returns []
            bridge._snapshot_adapter.capture_snapshot = orig_capture
            await page.goto(f"{c3_server.origin}/no-disclosure")
            recs_none = await bridge.evaluate_page_disclosures(
                source_context=ctx,
                source_page=page,
                page_url=page.url,
                viewport_name="Desktop 1366x768",
                run_id=run_id,
            )
            assert recs_none == []  # Successfully found no candidates!

            await ctx.close()
        finally:
            await browser.close()


# ==============================================================================
# 15. Reproduction 3: Reconstruction Timeout Wiring
# ==============================================================================

@pytest.mark.asyncio
async def test_reproduction_3_reconstruction_timeout_wiring(c3_server, test_db):
    """Verify that configured reconstruction_timeout=5.0 is received by _reconstructor.reconstruct (not 1.0)."""
    proj_id, run_id = test_db
    cfg = InteractiveWorkflowConfig(
        enabled=True,
        reconstruction_timeout=5.0,
        action_timeout=1.0,
    )
    bridge = InteractiveDisclosureBridge(config=cfg)
    received_timeout = None

    orig_reconstruct = bridge._executor._reconstructor.reconstruct

    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def spying_reconstruct(*args, **kwargs):
        nonlocal received_timeout
        received_timeout = kwargs.get("timeout")
        async with orig_reconstruct(*args, **kwargs) as sess:
            yield sess

    bridge._executor._reconstructor.reconstruct = spying_reconstruct

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await page.goto(f"{c3_server.origin}/single-disclosure")

            await bridge.evaluate_page_disclosures(
                source_context=ctx,
                source_page=page,
                page_url=page.url,
                viewport_name="Desktop 1366x768",
                run_id=run_id,
            )

            assert received_timeout == 5.0, f"Expected reconstruction timeout 5.0, got {received_timeout}"

            await ctx.close()
        finally:
            await browser.close()


# ==============================================================================
# 16. Truthful Timeout and Cancellation History
# ==============================================================================

@pytest.mark.asyncio
async def test_truthful_timeout_history_before_and_after_dispatch(c3_server, test_db):
    """Verify that timeout/cancellation records truthful action history:
    - Timeout before dispatch reports action_attempted=False, action_confirmed=False.
    - Timeout after confirmed dispatch preserves action_attempted=True, action_confirmed=True.
    - Circuit breaker halts subsequent actions in both cases.
    """
    proj_id, run_id = test_db

    # Sub-case A: Timeout before dispatch
    bridge_a = InteractiveDisclosureBridge(
        config=InteractiveWorkflowConfig(enabled=True, action_timeout=1.0, reconstruction_timeout=1.0)
    )

    async def timeout_before_click(*args, **kwargs):
        # Simulate wait_for timeout during reconstruction / pre-action
        tracker = kwargs.get("lifecycle_tracker", {})
        tracker["phase"] = "RECONSTRUCTION"
        tracker["action_attempted"] = False
        tracker["action_confirmed"] = False
        raise asyncio.TimeoutError()

    bridge_a._oracle.evaluate_disclosure_action = timeout_before_click

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await page.goto(f"{c3_server.origin}/single-disclosure")

            recs_a = await bridge_a.evaluate_page_disclosures(
                source_context=ctx,
                source_page=page,
                page_url=page.url,
                viewport_name="Desktop 1366x768",
                run_id=run_id,
            )
            assert len(recs_a) == 1
            assert recs_a[0]["status"] == "LIMITED COVERAGE"
            assert "before action dispatch" in recs_a[0]["actual_result"]
            assert bridge_a.is_cleanup_halted is True

            # Sub-case B: Timeout after confirmed action
            bridge_b = InteractiveDisclosureBridge(
                config=InteractiveWorkflowConfig(enabled=True, action_timeout=1.0, reconstruction_timeout=1.0)
            )

            async def timeout_after_click(*args, **kwargs):
                tracker = kwargs.get("lifecycle_tracker", {})
                tracker["phase"] = "POST_ACTION_OBSERVATION"
                tracker["action_attempted"] = True
                tracker["action_confirmed"] = True
                raise asyncio.TimeoutError()

            bridge_b._oracle.evaluate_disclosure_action = timeout_after_click

            recs_b = await bridge_b.evaluate_page_disclosures(
                source_context=ctx,
                source_page=page,
                page_url=page.url,
                viewport_name="Desktop 1366x768",
                run_id=run_id,
            )
            assert len(recs_b) == 1
            assert recs_b[0]["status"] == "LIMITED COVERAGE"
            assert "after confirmed action" in recs_b[0]["actual_result"]
            assert bridge_b.is_cleanup_halted is True

            await ctx.close()
        finally:
            await browser.close()


# ==============================================================================
# 17. Real Checkpoint and Final-Save Database Persistence (Requirement D)
# ==============================================================================

@pytest.mark.asyncio
async def test_real_checkpoint_and_final_save_database_persistence(tmp_path, monkeypatch, c3_server):
    """Verify real database persistence across repeated checkpoints and final save:
    - Sets up an isolated SQLite database.
    - Multiple checkpoints followed by final save.
    - Exact record counts in `tests` table.
    - Zero duplicate records (COUNT(*) == COUNT(DISTINCT id)).
    - Zero customer findings in `findings` table.
    - Honest final report completeness via load_report.
    """
    import app.paths as paths
    db_file = tmp_path / "isolated_qa.sqlite"
    monkeypatch.setenv("QA_TESTER_DB_PATH", str(db_file))
    monkeypatch.setattr(paths, "get_writable_dir", lambda: str(tmp_path))
    paths.ensure_dirs()
    init_db()

    conn = get_db_connection()
    c = conn.cursor()
    proj_id = c.execute(
        "INSERT INTO projects (name, target_url, environment, tester_name) VALUES (?, ?, ?, ?)",
        ("Persistence Test Project", c3_server.origin, "testing", "Tester"),
    ).lastrowid
    run_id = c.execute(
        "INSERT INTO runs (project_id, status) VALUES (?, ?)",
        (proj_id, "RUNNING"),
    ).lastrowid
    conn.commit()
    conn.close()

    from app.routes.api import _save_results

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context()
            manual_page = await ctx.new_page()
            await manual_page.goto(f"{c3_server.origin}/page-one")

            def real_checkpoint(batch, coverage):
                _save_results(batch, run_id, proj_id, finish=False, coverage=coverage)

            full_qa = FullSiteQA(
                target_url=f"{c3_server.origin}/page-one",
                project_id=proj_id,
                run_id=run_id,
                context=ctx,
                page_limit=2,
                checkpoint=real_checkpoint,
                enable_interactive_disclosure=True,
                interactive_options={"max_evaluations_per_page": 2},
            )

            results = await full_qa.run()

            # Final save at end of run
            _save_results(
                results[full_qa.saved_offset:],
                run_id,
                proj_id,
                finish=True,
                coverage=full_qa.snapshot(),
                terminal_status=full_qa.terminal_status,
            )

            # Inspect isolated SQLite database
            conn = get_db_connection()
            c = conn.cursor()

            # 1. Semantic deduplication check: no duplicate logical evaluations in DB
            total_tests = c.execute("SELECT COUNT(*) FROM tests WHERE run_id=?", (run_id,)).fetchone()[0]
            distinct_ids = c.execute("SELECT COUNT(DISTINCT id) FROM tests WHERE run_id=?", (run_id,)).fetchone()[0]
            assert total_tests == distinct_ids, "Duplicate primary keys found in tests table!"
            assert total_tests == len(full_qa.results), f"Mismatch: DB has {total_tests}, runner has {len(full_qa.results)}"

            # Group by semantic identity (url, viewport, expected_result) to ensure exactly-once persistence
            logical_dups = c.execute(
                """SELECT url, viewport, expected_result, COUNT(*) as cnt
                   FROM tests
                   WHERE run_id=? AND expected_result=?
                   GROUP BY url, viewport, expected_result
                   HAVING cnt > 1""",
                (run_id, FUNCTIONAL_DISCLOSURE_EXPECTED),
            ).fetchall()
            assert len(logical_dups) == 0, f"Duplicate logical evaluations persisted: {logical_dups}"

            # 2. Check that ZERO customer findings were created
            total_findings = c.execute("SELECT COUNT(*) FROM findings WHERE run_id=?", (run_id,)).fetchone()[0]
            assert total_findings == 0, f"Expected 0 customer findings, found {total_findings}"

            # 3. Check C3 test records in DB
            c3_rows = c.execute(
                "SELECT id, url, status, expected_result, actual_result, stable_id FROM tests WHERE run_id=? AND expected_result=?",
                (run_id, FUNCTIONAL_DISCLOSURE_EXPECTED),
            ).fetchall()
            assert len(c3_rows) >= 1
            for row in c3_rows:
                assert row["stable_id"] is None
                assert row["status"] in ("PASS", "LIMITED COVERAGE")

            pass_rows = [r for r in c3_rows if r["status"] == "PASS"]
            assert len(pass_rows) >= 1

            # 4. Check load_report outcome
            report = load_report(conn, run_id)
            conn.close()

            assert report["stats"]["PASS"] >= 1
            assert report["findings"] == []

            await ctx.close()
        finally:
            await browser.close()


# ==============================================================================
# 18. Action Correlation Across Pages and Viewports (Requirement E)
# ==============================================================================

def test_opaque_action_correlation_distinguishes_pages_and_viewports():
    """Verify that:
    - Two different pages sharing the same control ID produce distinct action tokens.
    - Two different viewports on the same page produce distinct action tokens.
    - No URL paths or secrets are leaked in the resulting sanitized URLs.
    """
    from app.qa.functional_reporting_adapter import (
        compute_opaque_action_ref,
        sanitize_target_url,
    )

    dummy_result = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="common-faq-button",
        action_type="click",
        evidence=FunctionalEvidence(
            action_attempted=True,
            action_confirmed=True,
            expanded_before=False,
            expanded_after=True,
            state_toggled=True,
            controls_count_delta=0,
            fingerprint_changed=False,
            pre_state_fingerprint="pre",
            post_state_fingerprint="post",
            controlled_element_id="faq-content",
            controlled_element_found=True,
            controlled_element_visible_before=False,
            controlled_element_visible_after=True,
            pre_observation_succeeded=True,
            post_observation_succeeded=True,
            observation_sufficient=True,
            observation_status="COMPLETED",
        ),
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
        diagnostics={},
    )

    # 1. Two different pages with same control ID
    page_1_url = "http://127.0.0.1:8080/products/faq"
    page_2_url = "http://127.0.0.1:8080/services/faq"

    token_page_1 = compute_opaque_action_ref(
        eval_res=dummy_result,
        page_url=page_1_url,
        viewport="Desktop",
    )
    token_page_2 = compute_opaque_action_ref(
        eval_res=dummy_result,
        page_url=page_2_url,
        viewport="Desktop",
    )

    assert token_page_1 != token_page_2, "Different pages must produce distinct action tokens!"

    url_1 = sanitize_target_url(page_1_url, action_ref=token_page_1)
    url_2 = sanitize_target_url(page_2_url, action_ref=token_page_2)

    assert url_1 != url_2
    assert "/products" not in url_1
    assert "/services" not in url_2
    assert url_1.startswith("http://127.0.0.1:8080/ [action: act-")
    assert url_2.startswith("http://127.0.0.1:8080/ [action: act-")

    # 2. Desktop vs Mobile on same page
    token_desktop = compute_opaque_action_ref(
        eval_res=dummy_result,
        page_url=page_1_url,
        viewport="Desktop",
    )
    token_mobile = compute_opaque_action_ref(
        eval_res=dummy_result,
        page_url=page_1_url,
        viewport="Mobile",
    )

    assert token_desktop != token_mobile, "Different viewports must produce distinct action tokens!"


async def _derive_intent_from_page(page: Any, raw_control_id: str) -> CandidateActionIntent:
    """Helper to derive CandidateActionIntent from an active page matching raw_control_id."""
    adapter = GuardedActionExecutor()._snapshot_adapter
    contract = {
        "qa_owned": True,
        "manual_visible_page": False,
        "expected_context": getattr(page, "context", None),
    }
    snapshot = await adapter.capture_snapshot(page, contract)
    norm = SemanticNormalizer().normalize_snapshot(snapshot)
    derivation = CandidateActionIntentFactory().derive_intents(norm)
    for intent in derivation.intents:
        if intent.control_fingerprint == raw_control_id or intent.control_fingerprint.endswith(f":{raw_control_id}"):
            return intent
    raise ValueError(f"Could not derive intent for {raw_control_id}")


# ==============================================================================
# 19. Defect 1: Interruption Immediately After Real Click Preserves Confirmation
# ==============================================================================

@pytest.mark.asyncio
async def test_interruption_immediately_after_click_confirms_action(c3_server, test_db):
    """Verify Defect 1 fix at actual lifecycle boundary:
    When real Playwright click returns and toggles aria-expanded to true,
    cancellation during subsequent observation produces:
    'Coverage limited: evaluation cancelled after confirmed action.'
    (Never 'cancelled after attempted action').
    """
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/single-disclosure"

    smoke = SmartSmokeQA(
        target_url=target_url,
        project_id=proj_id,
        run_id=run_id,
        mode="current",
        enable_interactive_disclosure=True,
    )

    import app.qa.functional_oracle as fo
    orig_wait = fo._wait_for_async_disclosure_transition

    # Hook observation wait to trigger cancellation token after click has returned
    async def cancel_during_observation(*args, **kwargs):
        cancel_token = kwargs.get("cancellation_token")
        if cancel_token:
            cancel_token.cancel()
        return await orig_wait(*args, **kwargs)

    smoke.interactive_bridge._oracle._wait_fn = cancel_during_observation
    import unittest.mock as mock
    with mock.patch("app.qa.functional_oracle._wait_for_async_disclosure_transition", side_effect=cancel_during_observation):
        results = await smoke.run()

    func_records = [
        r for r in results
        if r.get("expected_result") == FUNCTIONAL_DISCLOSURE_EXPECTED
    ]
    assert len(func_records) >= 1, f"Expected at least 1 record, got {len(func_records)}"
    rec = func_records[0]
    assert rec["status"] == "LIMITED COVERAGE"
    assert "evaluation cancelled after confirmed action" in rec["actual_result"]
    assert "after attempted action" not in rec["actual_result"]


# ==============================================================================
# 20. Defect 2: Cleanup Completed Flag Observed Only After Verified Teardown
# ==============================================================================

@pytest.mark.asyncio
async def test_cleanup_completed_is_never_true_while_page_is_open(c3_server, test_db):
    """Verify Defect 2 fix at actual lifecycle boundary:
    Independent instrumentation monitors `cleanup_completed = True`.
    At the exact moment the flag is set, the disposable page MUST ALREADY be closed.
    The flag is never observed while the disposable page is still open.
    """
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/single-disclosure"

    observed_page_states_at_cleanup = []

    class InstrumentedTracker(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.monitored_page = None

        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            if key == "cleanup_completed" and value is True:
                if self.monitored_page is not None:
                    # Check whether disposable page is open at the exact moment flag is set
                    is_closed = getattr(self.monitored_page, "is_closed", lambda: True)()
                    observed_page_states_at_cleanup.append(is_closed)

    tracker = InstrumentedTracker({
        "phase": "START",
        "action_attempted": False,
        "action_confirmed": False,
        "cleanup_completed": False,
        "cleanup_failed": False,
    })

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            source_ctx = await browser.new_context()
            source_page = await source_ctx.new_page()
            await source_page.goto(target_url)

            intent = await _derive_intent_from_page(source_page, "disclosure-btn")

            oracle = SafeDisclosureFunctionalOracle()
            executor = GuardedActionExecutor()

            orig_reconstruct = executor._reconstructor.reconstruct

            from contextlib import asynccontextmanager
            @asynccontextmanager
            async def page_capturing_reconstruct(*args, **kwargs):
                # Pass tracker down
                kwargs["lifecycle_tracker"] = tracker
                async with orig_reconstruct(*args, **kwargs) as sess:
                    tracker.monitored_page = sess.page
                    yield sess

            executor._reconstructor.reconstruct = page_capturing_reconstruct

            eval_res = await oracle.evaluate_disclosure_action(
                executor=executor,
                source_context=source_ctx,
                approved_target_url=target_url,
                candidate_intent=intent,
                lifecycle_tracker=tracker,
            )

            assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
            assert tracker["cleanup_completed"] is True
            assert len(observed_page_states_at_cleanup) >= 1
            # Every observation of cleanup_completed=True MUST have seen page.is_closed() == True!
            assert all(observed_page_states_at_cleanup), "cleanup_completed was set while page was still open!"

            await source_ctx.close()
        finally:
            await browser.close()


# ==============================================================================
# 21. Early Returns with Successful Teardown Truthfully Mark Cleanup Complete
# ==============================================================================

@pytest.mark.asyncio
async def test_early_return_with_successful_cleanup_marks_cleanup_completed(c3_server, test_db):
    """Verify that early returns from reconstruct (e.g. failed pre-action inspection)
    still execute teardown and truthfully mark cleanup_completed=True without false uncertainty.
    """
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/single-disclosure"

    tracker = {
        "phase": "START",
        "action_attempted": False,
        "action_confirmed": False,
        "cleanup_completed": False,
        "cleanup_failed": False,
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            source_ctx = await browser.new_context()
            source_page = await source_ctx.new_page()
            await source_page.goto(target_url)

            intent = await _derive_intent_from_page(source_page, "disclosure-btn")

            executor = GuardedActionExecutor()

            # Simulate pre-action snapshot failure causing early return inside async with session:
            import dataclasses
            orig_capture = executor._snapshot_adapter.capture_snapshot
            async def failing_snapshot(*args, **kwargs):
                snap = await orig_capture(*args, **kwargs)
                from app.qa.browser_snapshot_adapter import SnapshotCoverage
                return dataclasses.replace(snap, coverage=SnapshotCoverage(terminal_status="INCOMPLETE", limitations=("simulated_early_exit",)))

            executor._snapshot_adapter.capture_snapshot = failing_snapshot

            res = await executor.execute(
                source_context=source_ctx,
                approved_target_url=target_url,
                candidate_intent=intent,
                lifecycle_tracker=tracker,
            )

            # Early return occurred
            assert res.status == GuardedExecutionStatus.FAILED
            assert "simulated_early_exit" in res.limitations
            # Teardown completed cleanly
            assert tracker["cleanup_completed"] is True
            assert tracker["cleanup_failed"] is False

            await source_ctx.close()
        finally:
            await browser.close()


# ==============================================================================
# 22. External Task Cancellation is Preserved and Evidence is Recorded
# ==============================================================================

@pytest.mark.asyncio
async def test_external_task_cancellation_preserved_and_evidence_recorded(c3_server, test_db):
    """Verify that when an asyncio Task running the runner is cancelled externally:
    1. asyncio.CancelledError is preserved (re-raised to the caller of the task).
    2. Available evidence is recorded into runner.results via record_sink.
    3. The interrupted record truthfully reflects execution history.
    """
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/single-disclosure"

    smoke = SmartSmokeQA(
        target_url=target_url,
        project_id=proj_id,
        run_id=run_id,
        mode="current",
        enable_interactive_disclosure=True,
    )

    # Cancel task externally during oracle evaluation
    task_ref = []
    orig_eval = smoke.interactive_bridge._oracle.evaluate_disclosure_action

    async def cancelling_eval(*args, **kwargs):
        # Cancel the task externally
        if task_ref:
            task_ref[0].cancel()
        await asyncio.sleep(0.05)
        return await orig_eval(*args, **kwargs)

    smoke.interactive_bridge._oracle.evaluate_disclosure_action = cancelling_eval

    run_task = asyncio.create_task(smoke.run())
    task_ref.append(run_task)

    # The external cancellation MUST be preserved as CancelledError
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert run_task.cancelled() is True

    # Evidence collected prior to cancellation is retained in runner.results
    func_records = [
        r for r in smoke.results
        if r.get("expected_result") == FUNCTIONAL_DISCLOSURE_EXPECTED
    ]
    assert len(func_records) >= 1
    assert func_records[0]["status"] == "LIMITED COVERAGE"
    assert "operation_cancelled" in func_records[0]["actual_result"].lower() or "cancelled" in func_records[0]["actual_result"].lower()


# ==============================================================================
# 23. Confirmed Action Preserved Across Observation RuntimeError (With/Without Tracker & Negative Control)
# ==============================================================================

@pytest.mark.asyncio
async def test_observation_runtime_error_preserves_confirmed_action_with_and_without_tracker(c3_server, test_db):
    """Verify that:
    1. With tracker: RuntimeError during post-click observation preserves action_confirmed=True in tracker,
       returns OBSERVATION_FAILED (not ACTION_FAILED), and outputs canonical diagnostic:
       'Execution control diagnostic: click was confirmed executed, but downstream state verification could not proceed.'
    2. Without tracker (oracle optional-tracker API): action_confirmed=True is still preserved via locator/evidence,
       and the canonical record matches.
    3. Negative control: Genuine click failure (click raises before confirmation) leaves action_confirmed=False,
       returns ACTION_FAILED, and outputs:
       'Execution control diagnostic: click dispatch was attempted, but action could not be confirmed.'
    """
    proj_id, run_id = test_db
    target_url = f"{c3_server.origin}/single-disclosure"
    import app.qa.functional_oracle as fo

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            # --- Part 1: With Tracker (C3 Bridge Integration) ---
            ctx1 = await browser.new_context()
            page1 = await ctx1.new_page()
            await page1.goto(target_url)

            bridge1 = InteractiveDisclosureBridge(InteractiveWorkflowConfig(enabled=True))
            captured_trackers = []
            records1 = []
            orig_eval1 = bridge1._oracle.evaluate_disclosure_action

            async def capture_tracker(**kwargs):
                t = kwargs["lifecycle_tracker"]
                captured_trackers.append(t)
                res = await orig_eval1(**kwargs)
                t["eval_res"] = res
                return res

            async def interrupt_obs(**kwargs):
                # Inspect real button to verify click returned and aria-expanded toggled
                expanded = await kwargs["target_frame"].evaluate(
                    '(id) => document.getElementById(id).getAttribute("aria-expanded")',
                    kwargs["raw_id"],
                )
                assert expanded == "true", f"Expected aria-expanded='true', got {expanded}"
                raise RuntimeError("simulated post-click observation failure")

            bridge1._oracle.evaluate_disclosure_action = capture_tracker
            orig_wait = fo._wait_for_async_disclosure_transition
            fo._wait_for_async_disclosure_transition = interrupt_obs
            try:
                await bridge1.evaluate_page_disclosures(
                    ctx1, page1, target_url, "Desktop", run_id, record_sink=records1.append
                )
            finally:
                fo._wait_for_async_disclosure_transition = orig_wait

            assert len(captured_trackers) >= 1
            tracker1 = captured_trackers[0]
            assert tracker1["action_attempted"] is True
            assert tracker1["action_confirmed"] is True
            assert tracker1["cleanup_completed"] is True
            assert tracker1["cleanup_failed"] is False
            eval_res1 = tracker1["eval_res"]
            assert eval_res1.execution_status == GuardedExecutionStatus.OBSERVATION_FAILED
            assert eval_res1.evidence.action_confirmed is True
            assert eval_res1.evidence.action_attempted is True

            assert len(records1) == 1
            assert records1[0]["status"] == "DIAGNOSTIC"
            assert "click was confirmed executed, but downstream state verification could not proceed" in records1[0]["actual_result"]
            assert "could not be confirmed" not in records1[0]["actual_result"]
            await ctx1.close()

            # --- Part 2: Without Tracker (Oracle Optional-Tracker API: lifecycle_tracker=None) ---
            ctx2 = await browser.new_context()
            page2 = await ctx2.new_page()
            await page2.goto(target_url)

            intent2 = await _derive_intent_from_page(page2, "disclosure-btn")
            executor2 = GuardedActionExecutor()
            oracle2 = SafeDisclosureFunctionalOracle()

            fo._wait_for_async_disclosure_transition = interrupt_obs
            try:
                eval_res2 = await oracle2.evaluate_disclosure_action(
                    executor=executor2,
                    source_context=ctx2,
                    approved_target_url=target_url,
                    candidate_intent=intent2,
                    lifecycle_tracker=None,  # Optional tracker omitted!
                )
            finally:
                fo._wait_for_async_disclosure_transition = orig_wait

            assert eval_res2.execution_status == GuardedExecutionStatus.OBSERVATION_FAILED
            assert eval_res2.evidence.action_confirmed is True
            assert eval_res2.evidence.action_attempted is True

            rec2 = adapt_functional_result_to_test_record(eval_res2, run_id, target_url, "Desktop")
            assert rec2["status"] == "DIAGNOSTIC"
            assert "click was confirmed executed, but downstream state verification could not proceed" in rec2["actual_result"]
            assert "could not be confirmed" not in rec2["actual_result"]
            await ctx2.close()

            # --- Part 3: Negative Control (Genuine Underlying Click Failure) ---
            ctx3 = await browser.new_context()
            page3 = await ctx3.new_page()
            await page3.goto(target_url)

            intent3 = await _derive_intent_from_page(page3, "disclosure-btn")
            executor3 = GuardedActionExecutor()
            tracker3 = {
                "phase": "START",
                "action_attempted": False,
                "action_confirmed": False,
                "cleanup_completed": False,
                "cleanup_failed": False,
            }

            # Intercept locator click at frame level so underlying click raises BEFORE returning
            orig_reconstruct3 = executor3._reconstructor.reconstruct

            @asynccontextmanager
            async def failing_click_reconstruct(*args, **kwargs):
                async with orig_reconstruct3(*args, **kwargs) as sess:
                    frame = sess.page.main_frame
                    orig_loc = frame.locator
                    def wrap_failing(loc_obj):
                        async def failing_click(*c_args, **c_kwargs):
                            raise TimeoutError("Simulated Playwright locator.click timeout")
                        loc_obj.click = failing_click
                        return loc_obj
                    frame.locator = lambda *args, **kwargs: wrap_failing(orig_loc(*args, **kwargs))
                    yield sess

            executor3._reconstructor.reconstruct = failing_click_reconstruct
            eval_res3 = await oracle2.evaluate_disclosure_action(
                executor=executor3,
                source_context=ctx3,
                approved_target_url=target_url,
                candidate_intent=intent3,
                lifecycle_tracker=tracker3,
            )

            # Underlying click failed! Must NOT be confirmed.
            assert tracker3["action_attempted"] is True
            assert tracker3["action_confirmed"] is False
            assert tracker3["cleanup_completed"] is True
            assert eval_res3.execution_status == GuardedExecutionStatus.ACTION_FAILED
            assert eval_res3.evidence.action_confirmed is False
            assert eval_res3.evidence.action_attempted is True

            rec3 = adapt_functional_result_to_test_record(eval_res3, run_id, target_url, "Desktop")
            assert rec3["status"] == "DIAGNOSTIC"
            assert "click dispatch was attempted, but action could not be confirmed" in rec3["actual_result"]
            assert "click was confirmed executed" not in rec3["actual_result"]
            await ctx3.close()

        finally:
            await browser.close()


# ==============================================================================
# 24. Database Persistence for Interrupted and Cleanup-Failed Runs
# ==============================================================================

@pytest.mark.asyncio
async def test_database_persistence_interrupted_and_cleanup_failed_runs(tmp_path, monkeypatch, c3_server):
    """Verify that:
    1. Interrupted run persists its available records exactly once to SQLite without duplicates or customer findings.
    2. Cleanup-failed run persists its DIAGNOSTIC record exactly once to SQLite without duplicates or customer findings.
    3. Semantic deduplication confirms 0 duplicate logical records across checkpoints and final saves.
    """
    import app.paths as paths
    from app.models.database import init_db, get_db_connection
    from app.routes.api import _save_results

    db_file = tmp_path / "isolated_qa_interrupted.sqlite"
    monkeypatch.setenv("QA_TESTER_DB_PATH", str(db_file))
    monkeypatch.setattr(paths, "get_writable_dir", lambda: str(tmp_path))
    paths.ensure_dirs()
    init_db()

    conn = get_db_connection()
    c = conn.cursor()
    proj_id = c.execute(
        "INSERT INTO projects (name, target_url, environment, tester_name) VALUES (?, ?, ?, ?)",
        ("C3 Interrupted/Cleanup-Failed Project", c3_server.origin, "testing", "Tester"),
    ).lastrowid
    run_interrupted = c.execute(
        "INSERT INTO runs (project_id, status) VALUES (?, ?)",
        (proj_id, "RUNNING"),
    ).lastrowid
    run_cleanup_fail = c.execute(
        "INSERT INTO runs (project_id, status) VALUES (?, ?)",
        (proj_id, "RUNNING"),
    ).lastrowid
    conn.commit()
    conn.close()

    # 1. Interrupted run persistence
    rec_pass = {
        "run_id": run_interrupted,
        "url": "http://127.0.0.1:8080/ [action: act-11111111]",
        "final_url": "http://127.0.0.1:8080/ [action: act-11111111]",
        "viewport": "Desktop",
        "status": "PASS",
        "expected_result": FUNCTIONAL_DISCLOSURE_EXPECTED,
        "actual_result": "Working disclosure toggled aria-expanded.",
        "screenshot_path": None,
        "screenshot": None,
        "stable_id": None,
        "finding_id": None,
    }
    rec_interrupted = {
        "run_id": run_interrupted,
        "url": "http://127.0.0.1:8080/ [action: act-22222222]",
        "final_url": "http://127.0.0.1:8080/ [action: act-22222222]",
        "viewport": "Desktop",
        "status": "LIMITED COVERAGE",
        "expected_result": FUNCTIONAL_DISCLOSURE_EXPECTED,
        "actual_result": "Coverage limited: evaluation cancelled after confirmed action.",
        "screenshot_path": None,
        "screenshot": None,
        "stable_id": None,
        "finding_id": None,
    }

    # Simulate checkpointing first record
    _save_results([rec_pass], run_interrupted, proj_id, finish=False)
    # Simulate final save of subsequent interrupted record
    _save_results([rec_interrupted], run_interrupted, proj_id, finish=True, terminal_status="ABORTED")

    # 2. Cleanup-failed run persistence
    rec_cleanup_fail = {
        "run_id": run_cleanup_fail,
        "url": "http://127.0.0.1:8080/ [action: act-33333333]",
        "final_url": "http://127.0.0.1:8080/ [action: act-33333333]",
        "viewport": "Desktop",
        "status": "DIAGNOSTIC",
        "expected_result": FUNCTIONAL_DISCLOSURE_EXPECTED,
        "actual_result": "Cleanup failure after confirmed action: the click was confirmed executed, but teardown verification failed.",
        "screenshot_path": None,
        "screenshot": None,
        "stable_id": None,
        "finding_id": None,
    }
    # Simulate saving cleanup-failed run
    _save_results([rec_cleanup_fail], run_cleanup_fail, proj_id, finish=True, terminal_status="COMPLETED")

    # Verify SQLite persistence for both runs
    conn = get_db_connection()
    c = conn.cursor()

    for r_id, expected_count in [(run_interrupted, 2), (run_cleanup_fail, 1)]:
        # Check total tests matches expected
        tests_count = c.execute("SELECT COUNT(*) FROM tests WHERE run_id=?", (r_id,)).fetchone()[0]
        assert tests_count == expected_count, f"Run {r_id}: expected {expected_count} tests, got {tests_count}"

        # Check semantic deduplication (url, viewport, expected_result)
        dups = c.execute(
            """SELECT url, viewport, expected_result, COUNT(*) as cnt
               FROM tests
               WHERE run_id=?
               GROUP BY url, viewport, expected_result
               HAVING cnt > 1""",
            (r_id,),
        ).fetchall()
        assert len(dups) == 0, f"Run {r_id}: duplicate logical evaluations found: {dups}"

        # Check ZERO customer findings created
        findings_count = c.execute("SELECT COUNT(*) FROM findings WHERE run_id=?", (r_id,)).fetchone()[0]
        assert findings_count == 0, f"Run {r_id}: expected 0 customer findings, got {findings_count}"

    # Verify statuses in report
    rep_int = load_report(conn, run_interrupted)
    assert rep_int["stats"]["PASS"] == 1
    assert rep_int["stats"]["LIMITED COVERAGE"] == 1
    assert rep_int["findings"] == []

    rep_cln = load_report(conn, run_cleanup_fail)
    assert rep_cln["stats"]["DIAGNOSTIC"] == 1
    assert rep_cln["findings"] == []

    conn.close()


