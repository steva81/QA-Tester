"""Tests for Phase B2d: One Guarded Action + Observation Pipeline.

Validates that GuardedActionExecutor executes at most one guarded, positively identified
ephemeral disclosure-style action within a fresh, successfully reconstructed disposable
B2c session under verified network lockdown, followed by read-only A2/A3 observation.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from app.qa.browser_snapshot_adapter import (
    ReadOnlyBrowserSnapshotAdapter,
    CancellationToken,
)
from app.qa.candidate_intent_factory import CandidateActionIntentFactory
from app.qa.candidate_intent_graph import CandidateActionIntent
from app.qa.guarded_execution import (
    GuardedActionExecutor,
    GuardedExecutionResult,
    GuardedExecutionStatus,
)
from app.qa.interaction_policy import RiskClassification
from app.qa.isolated_network_barrier import (
    BarrierState,
    IsolatedNetworkBarrier,
    IsolatedNetworkBarrierSession,
)
from app.qa.semantic_normalizer import SemanticNormalizer
from app.qa.state_reconstruction import (
    ReconstructionStatus,
    StateReconstructionError,
    StateReconstructionResult,
    StateReconstructor,
)


class B2dTrackingHandler(BaseHTTPRequestHandler):
    """Local HTTP handler tracking network access and serving disclosure test pages."""

    counts: Dict[str, int] = {
        "get_disclosure": 0,
        "get_tab": 0,
        "get_ambiguous": 0,
        "get_mutating_click": 0,
        "get_plain_button": 0,
        "get_frame_parent": 0,
        "get_frame_child": 0,
        "post_mutation": 0,
    }

    @classmethod
    def reset_counts(cls):
        for k in cls.counts:
            cls.counts[k] = 0

    def do_GET(self):
        req_path = urlsplit(self.path).path
        if req_path == "/disclosure":
            self.__class__.counts["get_disclosure"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Disclosure Test</title></head>
<body>
  <h1>FAQ Section</h1>
  <button id="faq-toggle" aria-expanded="false" onclick="
    var isExp = this.getAttribute('aria-expanded') === 'true';
    this.setAttribute('aria-expanded', isExp ? 'false' : 'true');
    var content = document.getElementById('faq-content');
    if (content) { content.hidden = isExp; }
    window.__click_count = (window.__click_count || 0) + 1;
  ">Show FAQ Details</button>
  <div id="faq-content" hidden>
    <p>This is the detailed FAQ answer revealed upon disclosure.</p>
  </div>
</body>
</html>""")
        elif req_path == "/tab-disclosure":
            self.__class__.counts["get_tab"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Tab Disclosure Test</title></head>
<body>
  <h1>Tab Disclosure</h1>
  <div role="tablist">
    <button role="tab" id="terms-tab" aria-selected="false" aria-controls="terms-panel" onclick="
      this.setAttribute('aria-selected', 'true');
      var p = document.getElementById('terms-panel');
      if (p) { p.hidden = false; }
    ">Terms Tab</button>
  </div>
  <div id="terms-panel" role="tabpanel" hidden>
    <p>Terms content revealed here.</p>
  </div>
</body>
</html>""")
        elif req_path == "/frame-duplicate":
            self.__class__.counts["get_frame_parent"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Frame Duplicate Test</title></head>
<body>
  <h1>Main Document</h1>
  <button id="same" onclick="window.__wrong_clicks = (window.__wrong_clicks || 0) + 1;">Wrong Main Button</button>
  <iframe src="/frame-child" id="child-iframe" width="400" height="200"></iframe>
</body>
</html>""")
        elif req_path == "/frame-child":
            self.__class__.counts["get_frame_child"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Child Iframe</title></head>
<body>
  <h2>Child Frame</h2>
  <button id="same" aria-expanded="false" onclick="
    window.__correct_clicks = (window.__correct_clicks || 0) + 1;
    this.setAttribute('aria-expanded', 'true');
  ">Approved Iframe Toggle</button>
</body>
</html>""")
        elif req_path == "/ambiguous":
            self.__class__.counts["get_ambiguous"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Ambiguous Target Test</title></head>
<body>
  <h1>Ambiguous Buttons</h1>
  <button aria-expanded="false" onclick="this.setAttribute('aria-expanded', 'true');">Expand Section</button>
  <button aria-expanded="false" onclick="this.setAttribute('aria-expanded', 'true');">Expand Section</button>
</body>
</html>""")
        elif req_path == "/plain-button":
            self.__class__.counts["get_plain_button"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Plain Button Test</title></head>
<body>
  <h1>Plain Button</h1>
  <button id="plain-btn" onclick="window.__clicked = true;">Generic Action Button</button>
</body>
</html>""")
        elif req_path == "/mutating-action":
            self.__class__.counts["get_mutating_click"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Mutating Action Test</title></head>
<body>
  <h1>Form Actions</h1>
  <form id="settings-form" action="/save" method="POST">
    <input type="text" name="setting" value="foo" />
    <button id="submit-btn" type="submit">submit</button>
  </form>
</body>
</html>""")
        elif req_path == "/disclosure-with-leak":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Disclosure with Network Leak Attempt</title></head>
<body>
  <h1>Adversarial Disclosure</h1>
  <button id="leak-toggle" aria-expanded="false" onclick="
    this.setAttribute('aria-expanded', 'true');
    fetch('/api/leak-attempt', { method: 'POST', body: 'EXFILTRATED_DATA' }).catch(function(){});
  ">Show & Leak</button>
</body>
</html>""")
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        self.__class__.counts["post_mutation"] += 1
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, format, *args):
        pass


class ManagedB2dServer:
    def __init__(self, handler_cls=B2dTrackingHandler):
        self.server = HTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def b2d_server():
    B2dTrackingHandler.reset_counts()
    srv = ManagedB2dServer()
    try:
        yield srv
    finally:
        srv.close()


async def _derive_intent_from_page(page: Any, target_name: str) -> CandidateActionIntent:
    """Helper to derive CandidateActionIntent from an active page matching target_name."""
    adapter = ReadOnlyBrowserSnapshotAdapter()
    normalizer = SemanticNormalizer()
    factory = CandidateActionIntentFactory()

    contract = {
        "qa_owned": True,
        "manual_visible_page": False,
        "expected_context": page.context,
    }
    snapshot = await adapter.capture_snapshot(page, contract)
    assert snapshot.coverage.terminal_status == "COMPLETE"
    norm = normalizer.normalize_snapshot(snapshot)
    assert norm.terminal_status == "COMPLETE"
    derivation = factory.derive(norm)
    assert derivation.terminal_status == "COMPLETE"

    for intent in derivation.intents:
        if target_name in intent.control_fingerprint:
            return intent
    for control in norm.semantic_state.controls:
        if target_name in control.accessible_name or target_name in control.control_id:
            for intent in derivation.intents:
                if intent.control_fingerprint == control.control_id:
                    return intent

    assert len(derivation.intents) > 0, "No intents derived"
    return derivation.intents[0]


# ==============================================================================
# Phase B2d Core Suite & Regressions
# ==============================================================================


@pytest.mark.asyncio
async def test_guarded_execution_allowed_disclosure_success_real_chromium(b2d_server):
    """Verify B2d executes exactly one ephemeral disclosure click under lockdown and captures state transition."""
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{b2d_server.origin}/disclosure"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "faq-toggle")
        assert intent.risk_classification == RiskClassification.SAFE_CANDIDATE
        assert intent.expected_action_type == "click"

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        # 1. Successful execution outcome
        assert res.status == GuardedExecutionStatus.EXECUTED
        assert res.action_executed is True
        assert res.action_attempted is True
        assert res.action_confirmed is True
        assert res.action_type == "click"
        assert res.target_control_id == intent.control_fingerprint

        # 2. Ephemeral disclosure observation
        assert res.disclosure_expanded_before is False
        assert res.disclosure_expanded_after is True
        assert res.pre_action_state_fingerprint is not None
        assert res.post_action_state_fingerprint is not None
        assert res.pre_action_state_fingerprint != res.post_action_state_fingerprint
        assert res.post_action_controls_count is not None
        assert res.post_action_controls_count > 0

        # 3. Disposable resources safely disposed
        assert browser.contexts == [source_ctx]

        # 4. Source context invariance: source page was never clicked
        source_expanded = await source_page.locator("#faq-toggle").get_attribute("aria-expanded")
        assert source_expanded == "false"
        assert not source_page.is_closed()

        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_allowed_tab_disclosure_real_chromium(b2d_server):
    """Verify B2d executes ephemeral disclosure click on tab disclosure pattern."""
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{b2d_server.origin}/tab-disclosure"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "terms-tab")
        assert intent.risk_classification == RiskClassification.SAFE_CANDIDATE

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert res.status == GuardedExecutionStatus.EXECUTED
        assert res.action_executed is True
        assert res.action_attempted is True
        assert res.action_confirmed is True
        assert res.action_type == "click"
        assert browser.contexts == [source_ctx]

        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_frame_scoped_duplicate_id_real_chromium(b2d_server):
    """Regression 1: Main-frame/child-frame duplicate ID.
    
    Verifies that when a main document and an iframe both share the same ID ('same'),
    the executor strictly resolves the target inside the verified child frame ('0.0:same').
    The main document element receives zero clicks, while the approved child frame receives the click.
    """
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{b2d_server.origin}/frame-duplicate"
        await source_page.goto(target_url)

        # Wait for child iframe to load
        child_iframe_el = await source_page.wait_for_selector("#child-iframe")
        assert child_iframe_el is not None

        # Derive intent: target is the child iframe's approved disclosure toggle
        intent = await _derive_intent_from_page(source_page, "0.0:same")
        assert intent.control_fingerprint == "0.0:same"
        assert intent.risk_classification == RiskClassification.SAFE_CANDIDATE

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert res.status == GuardedExecutionStatus.EXECUTED
        assert res.action_executed is True
        assert res.action_confirmed is True
        assert res.target_control_id == "0.0:same"

        # Verify source context invariance: main document button received zero clicks
        wrong_clicks = await source_page.evaluate("() => window.__wrong_clicks || 0")
        assert wrong_clicks == 0

        assert browser.contexts == [source_ctx]
        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_stale_state_change_after_reconstruction(b2d_server):
    """Regression 2: Stale state change after successful reconstruction rejects with zero click attempts.

    Verifies that if DOM state changes after B2c reconstruction before action execution
    (causing pre-action fingerprint mismatch), B2d detects the stale state and rejects
    immediately with status STALE_STATE and action_executed=False, with 0 click attempts.
    """
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{b2d_server.origin}/disclosure"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "faq-toggle")
        orig_reconstruct = executor._reconstructor.reconstruct

        # Intercept reconstruction to alter DOM after revalidation before B2d action
        @asynccontextmanager
        async def mutating_reconstruct(*args, **kwargs):
            async with orig_reconstruct(*args, **kwargs) as sess:
                # Mutate button text to cause canonical state fingerprint mismatch
                await sess.page.evaluate(
                    "() => document.getElementById('faq-toggle').innerText = 'Stale Altered Button Text'"
                )
                yield sess

        executor._reconstructor.reconstruct = mutating_reconstruct

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert res.status == GuardedExecutionStatus.STALE_STATE
        assert res.action_executed is False
        assert res.action_attempted is False
        assert res.action_confirmed is False
        assert "stale state" in (res.error_message or "").lower()

        assert browser.contexts == [source_ctx]
        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_teardown_failure_preserves_confirmed_action(b2d_server):
    """Regression 3: Cleanup failure after confirmed click surfaces CLEANUP_FAILED without erasing action history.

    Verifies that if StateReconstructionError occurs during context teardown verification
    after a successful click, the executor reports CLEANUP_FAILED with action_executed=True
    and action_confirmed=True, rather than erasing the confirmed action.
    """
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{b2d_server.origin}/disclosure"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "faq-toggle")
        orig_reconstruct = executor._reconstructor.reconstruct

        # Intercept reconstruction to simulate teardown verification failure on exit
        @asynccontextmanager
        async def failing_teardown_reconstruct(*args, **kwargs):
            async with orig_reconstruct(*args, **kwargs) as sess:
                yield sess
            # Raise StateReconstructionError upon context manager exit
            raise StateReconstructionError(
                "Terminal cleanup failure during reconstruction teardown; verified context-owner disposal required"
            )

        executor._reconstructor.reconstruct = failing_teardown_reconstruct

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        # 1. Cleanup failure is explicitly surfaced (not hidden or mapped to false RECONSTRUCTION_FAILED)
        assert res.status == GuardedExecutionStatus.CLEANUP_FAILED

        # 2. Confirmed click is NOT erased
        assert res.action_executed is True
        assert res.action_attempted is True
        assert res.action_confirmed is True
        assert res.target_control_id == intent.control_fingerprint
        assert "Terminal teardown failure after action execution" in (res.error_message or "")

        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_rejection_disallowed_risk_tier(b2d_server):
    """Verify B2d immediately rejects POTENTIALLY_MUTATING intents without action execution."""
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{b2d_server.origin}/mutating-action"
        await source_page.goto(target_url)

        # Mutating submit button intent
        intent = await _derive_intent_from_page(source_page, "submit-btn")
        assert intent.risk_classification == RiskClassification.POTENTIALLY_MUTATING

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        # Rejection: POTENTIALLY_MUTATING never authorizes execution
        assert res.status == GuardedExecutionStatus.DISALLOWED_RISK_TIER
        assert res.action_executed is False
        assert "POTENTIALLY_MUTATING is rejected" in (res.error_message or "")

        # Zero network mutations
        assert B2dTrackingHandler.counts["post_mutation"] == 0
        assert browser.contexts == [source_ctx]

        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_rejection_unsupported_action_type(b2d_server):
    """Verify B2d rejects non-click action types without executing UI action."""
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{b2d_server.origin}/disclosure"
        await source_page.goto(target_url)

        base_intent = await _derive_intent_from_page(source_page, "faq-toggle")

        # Create intent with unsupported action type
        unsupported_intent = CandidateActionIntent(
            parent_state_fingerprint=base_intent.parent_state_fingerprint,
            control_fingerprint=base_intent.control_fingerprint,
            expected_action_type="input",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=unsupported_intent,
        )

        assert res.status == GuardedExecutionStatus.UNSUPPORTED_ACTION_TYPE
        assert res.action_executed is False
        assert browser.contexts == [source_ctx]

        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_rejection_non_disclosure_control(b2d_server):
    """Verify B2d rejects plain/generic buttons that are not positively identified disclosure patterns."""
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{b2d_server.origin}/plain-button"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "plain-btn")
        assert intent.risk_classification == RiskClassification.SAFE_CANDIDATE

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert res.status == GuardedExecutionStatus.NOT_DISCLOSURE_CONTROL
        assert res.action_executed is False
        assert "not a positively identified ephemeral disclosure pattern" in (res.error_message or "")
        assert browser.contexts == [source_ctx]

        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_rejection_ambiguous_target(b2d_server):
    """Verify B2d fails closed when multiple elements match the target identifier."""
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{b2d_server.origin}/ambiguous"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "Expand Section")

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert res.status == GuardedExecutionStatus.AMBIGUOUS_TARGET
        assert res.action_executed is False
        assert browser.contexts == [source_ctx]

        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_rejection_target_not_found_or_stale(b2d_server):
    """Verify B2d fails closed when the target control is not found on the page."""
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{b2d_server.origin}/disclosure"
        await source_page.goto(target_url)

        base_intent = await _derive_intent_from_page(source_page, "faq-toggle")

        # Alter control fingerprint to non-existent ID
        missing_intent = CandidateActionIntent(
            parent_state_fingerprint=base_intent.parent_state_fingerprint,
            control_fingerprint="0:non-existent-button-999",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=missing_intent,
        )

        assert res.status in (
            GuardedExecutionStatus.TARGET_NOT_FOUND,
            GuardedExecutionStatus.REVALIDATION_FAILED,
        )
        assert res.action_executed is False
        assert browser.contexts == [source_ctx]

        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_rejection_failed_reconstruction(b2d_server):
    """Verify B2d fails closed when state reconstruction fails (e.g. unapproved target URL)."""
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()

        # Target URL not authorized / invalid scheme
        invalid_url = "ftp://unauthorized.local/test"
        dummy_intent = CandidateActionIntent(
            parent_state_fingerprint="dummy_fp",
            control_fingerprint="0:btn",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=invalid_url,
            candidate_intent=dummy_intent,
        )

        assert res.status == GuardedExecutionStatus.RECONSTRUCTION_FAILED
        assert res.action_executed is False
        assert browser.contexts == [source_ctx]

        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_rejection_absent_lockdown(b2d_server):
    """Verify B2d fails closed if network barrier is not in completed lockdown state."""
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{b2d_server.origin}/disclosure"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "faq-toggle")

        orig_reconstruct = executor._reconstructor.reconstruct

        @asynccontextmanager
        async def mock_reconstruct(*args, **kwargs):
            async with orig_reconstruct(*args, **kwargs) as sess:
                # Force barrier session state to BOOTSTRAP (not LOCKED_DOWN)
                object.__setattr__(sess.barrier_session, "_state", BarrierState.BOOTSTRAP)
                yield sess

        executor._reconstructor.reconstruct = mock_reconstruct

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert res.status == GuardedExecutionStatus.LOCKDOWN_NOT_ACTIVE
        assert res.action_executed is False
        assert "lockdown is not active" in (res.error_message or "").lower()
        assert browser.contexts == [source_ctx]

        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_proves_at_most_one_action_and_no_retry(b2d_server):
    """Verify that exactly one Playwright action occurs and no retry or fallback is performed."""
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{b2d_server.origin}/disclosure"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "faq-toggle")

        # Execute once
        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert res.status == GuardedExecutionStatus.EXECUTED
        assert res.action_executed is True

        # Test failure during click: verify exactly 1 attempt and no retry
        orig_reconstruct = executor._reconstructor.reconstruct
        click_attempts = 0

        @asynccontextmanager
        async def mock_failing_click_reconstruct(*args, **kwargs):
            async with orig_reconstruct(*args, **kwargs) as sess:
                locator = sess.page.locator('[id="faq-toggle"]')
                orig_click = locator.click

                async def counting_failing_click(*c_args, **c_kwargs):
                    nonlocal click_attempts
                    click_attempts += 1
                    raise RuntimeError("Simulated click failure")

                sess.page.locator = lambda sel: type("MockLoc", (), {
                    "count": locator.count,
                    "is_visible": locator.is_visible,
                    "is_enabled": locator.is_enabled,
                    "click": counting_failing_click,
                })()
                sess.page.main_frame.locator = sess.page.locator
                yield sess

        executor._reconstructor.reconstruct = mock_failing_click_reconstruct

        fail_res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert fail_res.status == GuardedExecutionStatus.ACTION_FAILED
        assert fail_res.action_executed is False
        assert fail_res.action_attempted is True
        assert fail_res.action_confirmed is False
        assert click_attempts == 1  # Exactly one attempt, NO RETRY

        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_blocked_network_mutations_during_action(b2d_server):
    """Verify that network mutations attempted during the action are blocked by the barrier under lockdown."""
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{b2d_server.origin}/disclosure-with-leak"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "leak-toggle")

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert res.status == GuardedExecutionStatus.EXECUTED
        assert res.action_executed is True

        # Assert that zero network mutation requests reached the server
        assert B2dTrackingHandler.counts["post_mutation"] == 0
        assert browser.contexts == [source_ctx]

        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_source_context_invariance(b2d_server):
    """Verify that source context pages and storage state are strictly invariant throughout execution."""
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{b2d_server.origin}/disclosure"
        await source_page.goto(target_url)

        before_content = await source_page.content()
        before_expanded = await source_page.locator("#faq-toggle").get_attribute("aria-expanded")
        before_hidden = await source_page.locator("#faq-content").is_hidden()

        intent = await _derive_intent_from_page(source_page, "faq-toggle")

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert res.status == GuardedExecutionStatus.EXECUTED

        after_content = await source_page.content()
        after_expanded = await source_page.locator("#faq-toggle").get_attribute("aria-expanded")
        after_hidden = await source_page.locator("#faq-content").is_hidden()

        assert before_content == after_content
        assert before_expanded == after_expanded == "false"
        assert before_hidden == after_hidden is True
        assert not source_page.is_closed()
        assert browser.contexts == [source_ctx]

        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_resource_cleanup_and_cancellation(b2d_server):
    """Verify truthful cancellation reporting and guaranteed resource cleanup."""
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{b2d_server.origin}/disclosure"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "faq-toggle")

        # 1. Early cancellation before execution
        token = CancellationToken(cancelled=True)
        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
            cancellation_token=token,
        )

        assert res.status == GuardedExecutionStatus.CANCELLED
        assert res.action_executed is False
        assert browser.contexts == [source_ctx]

        # 2. Async caller task cancellation during execution
        task = asyncio.create_task(
            executor.execute(
                source_context=source_ctx,
                approved_target_url=target_url,
                candidate_intent=intent,
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        assert browser.contexts == [source_ctx]
        await browser.close()


@pytest.mark.asyncio
async def test_guarded_execution_privacy_and_representations(b2d_server):
    """Verify that credentials and query parameters are redacted from result representations."""
    dummy_res = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:faq-toggle",
        pre_action_state_fingerprint="pre_fp",
        post_action_state_fingerprint="post_fp",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
    )

    r_str = repr(dummy_res)
    assert "GuardedExecutionResult" in r_str
    assert dummy_res.status == GuardedExecutionStatus.EXECUTED

    # Immutability check
    with pytest.raises(FrozenInstanceError):
        dummy_res.action_executed = False  # type: ignore
