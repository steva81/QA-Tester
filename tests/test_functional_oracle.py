"""Phase C: Functional Oracle Test Suite.

Validates:
1. Outcome Contract Invariants:
   - `is_customer_defect` is strictly True IF AND ONLY IF `verdict == SUPPORTED_FAILURE`.
   - Execution-control events, safety aborts, limited coverage, and diagnostic issues
     are NEVER customer defects.
2. Verified Functional Success on working disclosure controls (expansion & collapse).
3. Positive Evidence Requirement:
   - Missing or insufficient DOM evidence for expansion/collapse produces INCONCLUSIVE.
   - Observation exceptions never silently produce VERIFIED_SUCCESS.
4. Supported Functional Failures on defective target implementations:
   - Inert disclosure control (button clicked, but no state change)
   - Dangling aria-controls (button references non-existent container ID)
   - Content not revealed (aria-expanded toggled to true, but content stayed hidden)
   - Content not collapsed (aria-expanded toggled to false, but content stayed visible)
   - Page script errors (click threw uncaught JS exception)
5. Privacy-Safe Structured Error Capture:
   - Raw credentials, userinfo passwords in URLs, and tokens in page errors are strictly redacted.
   - Neither repr(result) nor asdict(result) contains raw secret tokens.
6. Error Causality & Network Lockdown Disambiguation:
   - Errors from QA network lockdown (Failed to fetch, net::ERR_) do NOT become customer defects.
   - Unrelated background timer errors do NOT turn a working disclosure into a customer defect.
7. Preservation of source-context invariance and clean resource lifecycle.
"""

import asyncio
from dataclasses import asdict
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import time
from typing import Any, Dict, List, Optional
import urllib.parse

import pytest
from playwright.async_api import async_playwright

from app.qa.browser_snapshot_adapter import CancellationToken

from app.qa.candidate_intent_factory import CandidateActionIntentFactory
from app.qa.candidate_intent_graph import CandidateActionIntent
from app.qa.functional_oracle import (
    DefectCategory,
    FunctionalEvidence,
    FunctionalEvaluationResult,
    FunctionalOutcomeVerdict,
    SafeDisclosureFunctionalOracle,
    StructuredPageError,
    sanitize_error_text,
)
from app.qa.guarded_execution import (
    GuardedActionExecutor,
    GuardedExecutionResult,
    GuardedExecutionStatus,
)
from app.qa.interaction_policy import RiskClassification
from app.qa.semantic_normalizer import SemanticNormalizer


# ==============================================================================
# Localhost HTTP Server Fixtures
# ==============================================================================

class FunctionalOracleServer(HTTPServer):
    def __init__(self, host="127.0.0.1", port=0):
        super().__init__((host, port), FunctionalOracleHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class FunctionalOracleHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path

        if path == "/valid-faq":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Working FAQ</title></head>
<body>
  <h1>FAQ Section</h1>
  <button id="faq-btn" aria-expanded="false" aria-controls="faq-ans" onclick="
    var isExp = this.getAttribute('aria-expanded') === 'true';
    this.setAttribute('aria-expanded', isExp ? 'false' : 'true');
    var ans = document.getElementById('faq-ans');
    if (ans) { ans.hidden = isExp; }
  ">Toggle FAQ</button>
  <div id="faq-ans" hidden>
    <p>This is the detailed FAQ answer content.</p>
  </div>
</body>
</html>""")

        elif path == "/valid-collapse":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Working Collapse</title></head>
<body>
  <h1>Collapsible Section</h1>
  <!-- Fixed working collapse: when isExp is true, clicking sets aria-expanded=false AND ans.hidden=true -->
  <button id="collapse-btn" aria-expanded="true" aria-controls="collapse-ans" onclick="
    var isExp = this.getAttribute('aria-expanded') === 'true';
    var willBeExp = !isExp;
    this.setAttribute('aria-expanded', willBeExp ? 'true' : 'false');
    var ans = document.getElementById('collapse-ans');
    if (ans) { ans.hidden = !willBeExp; }
  ">Collapse Section</button>
  <div id="collapse-ans">
    <p>This is initially visible content that collapses on click.</p>
  </div>
</body>
</html>""")

        elif path == "/broken-collapse":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Broken Collapse</title></head>
<body>
  <h1>Broken Collapse Section</h1>
  <!-- Defect: aria-expanded toggles to false, but content container remains visible -->
  <button id="broken-collapse-btn" aria-expanded="true" aria-controls="broken-collapse-ans" onclick="
    this.setAttribute('aria-expanded', 'false');
    var ans = document.getElementById('broken-collapse-ans');
    if (ans) { ans.hidden = false; /* Bug: developer left content unhidden */ }
  ">Collapse Section</button>
  <div id="broken-collapse-ans">
    <p>Content that fails to collapse and remains visible despite aria-expanded=false.</p>
  </div>
</body>
</html>""")

        elif path == "/inert-faq":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Inert FAQ</title></head>
<body>
  <h1>Inert FAQ Section</h1>
  <!-- Broken: Click handler is dead; aria-expanded never toggles -->
  <button id="inert-btn" aria-expanded="false" aria-controls="inert-ans" onclick="
    /* Dead/inert handler */
    window.__inert_click = true;
  ">Toggle FAQ</button>
  <div id="inert-ans" hidden>
    <p>Content that is never revealed because the button is inert.</p>
  </div>
</body>
</html>""")

        elif path == "/dangling-controls":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Dangling Controls</title></head>
<body>
  <h1>Dangling Controls Section</h1>
  <!-- Broken: aria-controls references an element ID that does not exist in the DOM -->
  <button id="dangling-btn" aria-expanded="false" aria-controls="non-existent-target-id" onclick="
    var isExp = this.getAttribute('aria-expanded') === 'true';
    this.setAttribute('aria-expanded', isExp ? 'false' : 'true');
  ">Toggle FAQ</button>
</body>
</html>""")

        elif path == "/hidden-content":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Content Not Revealed</title></head>
<body>
  <h1>Broken Reveal Section</h1>
  <!-- Broken: aria-expanded toggles to true, but content container remains hidden -->
  <button id="broken-reveal-btn" aria-expanded="false" aria-controls="broken-ans" onclick="
    this.setAttribute('aria-expanded', 'true');
    /* Bug: developer forgot to unhide broken-ans */
  ">Toggle FAQ</button>
  <div id="broken-ans" hidden>
    <p>Content that remains hidden despite aria-expanded=true.</p>
  </div>
</body>
</html>""")

        elif path == "/script-error":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Script Error Page</title></head>
<body>
  <h1>Crash Section</h1>
  <!-- Broken: Throws an unhandled runtime error upon click -->
  <button id="crash-btn" aria-expanded="false" aria-controls="crash-ans" onclick="
    throw new TypeError('Cannot read property null of undefined in disclosure handler');
  ">Toggle FAQ</button>
  <div id="crash-ans" hidden>
    <p>Answer content</p>
  </div>
</body>
</html>""")

        elif path == "/secret-error":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Secret Error Page</title></head>
<body>
  <h1>Secret Crash Section</h1>
  <!-- Error containing sensitive tokens, passwords, and userinfo URL -->
  <button id="secret-btn" aria-expanded="false" aria-controls="secret-ans" onclick="
    throw new Error('Disclosure failed with token=SYNTHETIC_API_TOKEN_9999 and password=SYNTHETIC_USER_PASS at http://admin:SYNTHETIC_USER_PASS@127.0.0.1/auth');
  ">Toggle FAQ</button>
  <div id="secret-ans" hidden><p>Secret answer</p></div>
</body>
</html>""")

        elif path == "/unlabelled-secret-error":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Unlabelled Secret Error Page</title></head>
<body>
  <h1>Unlabelled Secret Crash Section</h1>
  <!-- Real page error containing unlabelled synthetic secret that does not match credential regexes -->
  <button id="unlabelled-secret-btn" aria-expanded="false" aria-controls="unlabelled-ans" onclick="
    throw new Error('SYNTHETIC_PRIVATE_83XZ');
  ">Toggle FAQ</button>
  <div id="unlabelled-ans" hidden><p>Answer</p></div>
</body>
</html>""")

        elif path == "/generic-error-during-click":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Generic Error During Click Page</title></head>
<body>
  <h1>Generic Error During Click Section</h1>
  <!-- Working disclosure button that also triggers an unhandled generic error with no network or background keywords during click -->
  <button id="generic-err-btn" aria-expanded="false" aria-controls="generic-ans" onclick="
    this.setAttribute('aria-expanded', 'true');
    document.getElementById('generic-ans').hidden = false;
    setTimeout(() => { throw new TypeError('Cannot read properties of undefined'); }, 0);
  ">Toggle FAQ</button>
  <div id="generic-ans" hidden><p>Generic Answer Content</p></div>
</body>
</html>""")

        elif path == "/lockdown-fetch-background":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Lockdown Fetch Page</title></head>
<body>
  <h1>Lockdown Fetch Section</h1>
  <!-- Working disclosure button -->
  <button id="faq-btn" aria-expanded="false" aria-controls="faq-ans" onclick="
    this.setAttribute('aria-expanded', 'true');
    document.getElementById('faq-ans').hidden = false;
    /* Trigger a background fetch that will be blocked by network barrier lockdown */
    fetch('/blocked-api-endpoint').catch(e => {
      /* Re-throw as unhandled rejection to trigger pageerror */
      setTimeout(() => { throw new Error('Failed to fetch: net::ERR_FAILED'); }, 0);
    });
  ">Toggle FAQ</button>
  <div id="faq-ans" hidden><p>Answer</p></div>
</body>
</html>""")

        elif path == "/unrelated-background-error":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Unrelated Background Error</title></head>
<body>
  <h1>Unrelated Background Section</h1>
  <button id="faq-btn" aria-expanded="false" aria-controls="faq-ans" onclick="
    this.setAttribute('aria-expanded', 'true');
    document.getElementById('faq-ans').hidden = false;
  ">Toggle FAQ</button>
  <div id="faq-ans" hidden><p>Answer</p></div>
  <script>
    /* Background timer error unrelated to disclosure click */
    setTimeout(() => {
      throw new Error('Unrelated background timer failure in analytics script');
    }, 10);
  </script>
</body>
</html>""")

        elif path == "/lockdown-error-during-click":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Lockdown Error During Click</title></head>
<body>
  <h1>Lockdown Error During Click Section</h1>
  <button id="faq-btn" aria-expanded="false" aria-controls="faq-ans" onclick="
    this.setAttribute('aria-expanded', 'true');
    document.getElementById('faq-ans').hidden = false;
    setTimeout(() => { throw new Error('net::ERR_NETWORK_IO_SUSPENDED: Failed to fetch blocked resource'); }, 0);
  ">Toggle FAQ</button>
  <div id="faq-ans" hidden><p>Answer</p></div>
</body>
</html>""")

        elif path == "/background-error-during-click":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Background Error During Click</title></head>
<body>
  <h1>Background Error During Click Section</h1>
  <button id="faq-btn" aria-expanded="false" aria-controls="faq-ans" onclick="
    this.setAttribute('aria-expanded', 'true');
    document.getElementById('faq-ans').hidden = false;
    setTimeout(() => { throw new Error('ResizeObserver loop limit exceeded in background layout watcher'); }, 0);
  ">Toggle FAQ</button>
  <div id="faq-ans" hidden><p>Answer</p></div>
</body>
</html>""")

        elif path == "/ambiguous":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Ambiguous Page</title></head>
<body>
  <h1>Ambiguous Section</h1>
  <button id="ambiguous-btn" onclick="
    document.getElementById('counter').innerText = '1';
  ">Click Me</button>
  <span id="counter">0</span>
</body>
</html>""")

        elif path == "/occluded":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Occluded Page</title></head>
<body>
  <h1>Occluded Control</h1>
  <button id="faq-btn" aria-expanded="false" aria-controls="faq-ans">Toggle FAQ</button>
  <div id="faq-ans" hidden><p>Answer</p></div>
  <div style="position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.1); z-index:99999;"></div>
</body>
</html>""")

        elif path == "/delayed-faq":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Delayed FAQ</title></head>
<body>
  <h1>Delayed FAQ Section</h1>
  <!-- Asynchronous expansion using setTimeout(..., 300) -->
  <button id="delayed-faq-btn" aria-expanded="false" aria-controls="delayed-faq-ans" onclick="
    var btn = this;
    setTimeout(function() {
      btn.setAttribute('aria-expanded', 'true');
      var ans = document.getElementById('delayed-faq-ans');
      if (ans) { ans.hidden = false; }
    }, 300);
  ">Toggle FAQ</button>
  <div id="delayed-faq-ans" hidden>
    <p>This is delayed FAQ answer content.</p>
  </div>
</body>
</html>""")

        elif path == "/delayed-collapse":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Delayed Collapse</title></head>
<body>
  <h1>Delayed Collapse Section</h1>
  <!-- Asynchronous collapse using setTimeout(..., 300) -->
  <button id="delayed-collapse-btn" aria-expanded="true" aria-controls="delayed-collapse-ans" onclick="
    var btn = this;
    setTimeout(function() {
      btn.setAttribute('aria-expanded', 'false');
      var ans = document.getElementById('delayed-collapse-ans');
      if (ans) { ans.hidden = true; }
    }, 300);
  ">Collapse Section</button>
  <div id="delayed-collapse-ans">
    <p>This is initially visible content that collapses asynchronously.</p>
  </div>
</body>
</html>""")

        elif path == "/network-dependent-disclosure":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Network Dependent Disclosure</title></head>
<body>
  <h1>Network Disclosure Section</h1>
  <!-- Disclosure depends on external network fetch; blocked under QA lockdown -->
  <button id="network-btn" aria-expanded="false" aria-controls="network-ans" onclick="
    var btn = this;
    fetch('/api/remote-data').then(function(res) {
      return res.json();
    }).then(function(data) {
      btn.setAttribute('aria-expanded', 'true');
      var ans = document.getElementById('network-ans');
      if (ans) { ans.hidden = false; }
    }).catch(function(err) {
      /* Network blocked by QA barrier; disclosure does not expand */
    });
  ">Toggle FAQ</button>
  <div id="network-ans" hidden>
    <p>Data fetched from server</p>
  </div>
</body>
</html>""")

        elif path == "/long-delayed-faq":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Long Delayed FAQ</title></head>
<body>
  <h1>Long Delayed Section</h1>
  <button id="long-delayed-btn" aria-expanded="false" aria-controls="long-ans" onclick="
    var btn = this;
    setTimeout(function() {
      btn.setAttribute('aria-expanded', 'true');
      var ans = document.getElementById('long-ans');
      if (ans) { ans.hidden = false; }
    }, 10000);
  ">Toggle FAQ</button>
  <div id="long-ans" hidden><p>Answer</p></div>
</body>
</html>""")

        elif path == "/attribute-first-delayed-reveal":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Attribute First Delayed Reveal</title></head>
<body>
  <h1>Delayed Reveal Section</h1>
  <!-- Reproduction 1: Immediate aria-expanded=true, delayed content reveal after 500ms -->
  <button id="attr-first-btn" aria-expanded="false" aria-controls="attr-first-ans" onclick="
    var btn = this;
    btn.setAttribute('aria-expanded', 'true');
    setTimeout(function() {
      var ans = document.getElementById('attr-first-ans');
      if (ans) { ans.hidden = false; }
    }, 500);
  ">Toggle FAQ</button>
  <div id="attr-first-ans" hidden>
    <p>Controlled content revealed 500ms after attribute toggle.</p>
  </div>
</body>
</html>""")

        elif path == "/visibility-first-delayed-attribute":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Visibility First Delayed Attribute</title></head>
<body>
  <h1>Visibility First Section</h1>
  <!-- Visibility-first: content unhidden immediately, aria-expanded=true set after 300ms -->
  <button id="vis-first-btn" aria-expanded="false" aria-controls="vis-first-ans" onclick="
    var btn = this;
    var ans = document.getElementById('vis-first-ans');
    if (ans) { ans.hidden = false; }
    setTimeout(function() {
      btn.setAttribute('aria-expanded', 'true');
    }, 300);
  ">Toggle FAQ</button>
  <div id="vis-first-ans" hidden>
    <p>Content revealed immediately, attribute toggled after 300ms.</p>
  </div>
</body>
</html>""")

        elif path == "/delayed-1500-faq":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Delayed 1500 FAQ</title></head>
<body>
  <h1>Delayed 1500 Section</h1>
  <!-- Reproduction 2: Expansion scheduled after 1500ms, exceeding the 1000ms observation budget -->
  <button id="delayed-1500-btn" aria-expanded="false" aria-controls="delayed-1500-ans" onclick="
    var btn = this;
    setTimeout(function() {
      btn.setAttribute('aria-expanded', 'true');
      var ans = document.getElementById('delayed-1500-ans');
      if (ans) { ans.hidden = false; }
    }, 1500);
  ">Toggle FAQ</button>
  <div id="delayed-1500-ans" hidden>
    <p>Content revealed after 1500ms.</p>
  </div>
</body>
</html>""")

        elif path == "/native-timer-disclosure":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head>
  <title>Native Timer Disclosure</title>
  <script>
    // Native setTimeout saved during page load before any interaction
    const savedNativeSetTimeout = window.setTimeout;
  </script>
</head>
<body>
  <h1>Native Timer Section</h1>
  <button id="native-timer-btn" aria-expanded="false" aria-controls="native-timer-ans" onclick="
    savedNativeSetTimeout(function() {
      var btn = document.getElementById('native-timer-btn');
      btn.setAttribute('aria-expanded', 'true');
      var ans = document.getElementById('native-timer-ans');
      if (ans) { ans.hidden = false; }
    }, 200);
  ">Toggle with Native Timer</button>
  <div id="native-timer-ans" hidden>
    <p>Controlled content revealed using saved native timer.</p>
  </div>
</body>
</html>""")

        elif path == "/iframe-disclosure-page":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Iframe Disclosure Page</title></head>
<body>
  <h1>Iframe Parent Page</h1>
  <iframe id="subframe" src="/valid-faq" style="width:500px; height:300px;"></iframe>
</body>
</html>""")

        elif path == "/multiple-controls-disclosure":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Multiple Controls Disclosure</title></head>
<body>
  <h1>Multiple Controls Section</h1>
  <button id="multi-ctrl-btn" aria-expanded="false" aria-controls="multi-ans-1 multi-ans-2" onclick="
    this.setAttribute('aria-expanded', 'true');
    document.getElementById('multi-ans-1').hidden = false;
    document.getElementById('multi-ans-2').hidden = false;
  ">Toggle Multi</button>
  <div id="multi-ans-1" hidden><p>Answer 1</p></div>
  <div id="multi-ans-2" hidden><p>Answer 2</p></div>
</body>
</html>""")

        elif path == "/already-visible-disclosure":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Already Visible Disclosure</title></head>
<body>
  <h1>Already Visible Section</h1>
  <button id="already-vis-btn" aria-expanded="false" aria-controls="already-vis-panel" onclick="
    this.setAttribute('aria-expanded', 'true');
  ">Toggle FAQ</button>
  <div id="already-vis-panel">Already visible content that is not hidden initially</div>
</body>
</html>""")

        elif path == "/already-hidden-collapse":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Already Hidden Collapse</title></head>
<body>
  <h1>Already Hidden Section</h1>
  <button id="already-hidden-btn" aria-expanded="true" aria-controls="already-hidden-panel" onclick="
    this.setAttribute('aria-expanded', 'false');
  ">Collapse Section</button>
  <div id="already-hidden-panel" hidden>Already hidden content</div>
</body>
</html>""")

        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def oracle_server():
    server = FunctionalOracleServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


async def _derive_intent_from_page(page: Any, raw_control_id: str) -> CandidateActionIntent:
    """Helper to derive CandidateActionIntent from an active page matching raw_control_id."""
    adapter = GuardedActionExecutor()._snapshot_adapter
    contract = {
        "qa_owned": True,
        "manual_visible_page": False,
        "expected_context": page.context,
    }
    snapshot = await adapter.capture_snapshot(page, contract)
    assert snapshot.coverage.terminal_status == "COMPLETE"
    normalizer = SemanticNormalizer()
    norm = normalizer.normalize_snapshot(snapshot)
    assert norm.terminal_status == "COMPLETE"
    assert norm.semantic_state is not None

    factory = CandidateActionIntentFactory()
    derivation = factory.derive(norm)
    assert derivation.terminal_status == "COMPLETE"

    for intent in derivation.intents:
        if raw_control_id in intent.control_fingerprint:
            return intent
    for control in norm.semantic_state.controls:
        if raw_control_id in control.accessible_name or raw_control_id in control.control_id:
            for intent in derivation.intents:
                if intent.control_fingerprint == control.control_id:
                    return intent

    from app.qa.state_fingerprint import CanonicalStateFingerprint
    fp = CanonicalStateFingerprint.generate(norm.semantic_state)
    return CandidateActionIntent(
        parent_state_fingerprint=fp,
        control_fingerprint=raw_control_id,
        expected_action_type="click",
        risk_classification=RiskClassification.SAFE_CANDIDATE,
    )


# ==============================================================================
# 1. Contract & Invariant Tests
# ==============================================================================

def test_contract_invariant_customer_defect_strictly_matches_supported_failure():
    """Verify that is_customer_defect is strictly True for SUPPORTED_FAILURE and False for all others."""
    dummy_evidence = FunctionalEvidence(
        action_attempted=True,
        action_confirmed=True,
        expanded_before=False,
        expanded_after=True,
        state_toggled=True,
        controls_count_delta=1,
        fingerprint_changed=True,
        pre_state_fingerprint="fp1",
        post_state_fingerprint="fp2",
    )

    # 1. Valid SUPPORTED_FAILURE with is_customer_defect=True
    res_fail = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.SUPPORTED_FAILURE,
        is_customer_defect=True,
        defect_category=DefectCategory.INERT_DISCLOSURE_CONTROL,
        target_control_id="0:btn",
        action_type="click",
        evidence=dummy_evidence,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )
    assert res_fail.is_customer_defect is True

    # 2. Invariant violation: SUPPORTED_FAILURE with is_customer_defect=False must raise ValueError
    with pytest.raises(ValueError, match="Contract invariant violation"):
        FunctionalEvaluationResult(
            verdict=FunctionalOutcomeVerdict.SUPPORTED_FAILURE,
            is_customer_defect=False,
            defect_category=DefectCategory.INERT_DISCLOSURE_CONTROL,
            target_control_id="0:btn",
            action_type="click",
            evidence=dummy_evidence,
            execution_status=GuardedExecutionStatus.EXECUTED,
            limitations=(),
        )

    # 3. Invariant violation: VERIFIED_SUCCESS with is_customer_defect=True must raise ValueError
    with pytest.raises(ValueError, match="Contract invariant violation"):
        FunctionalEvaluationResult(
            verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
            is_customer_defect=True,
            defect_category=None,
            target_control_id="0:btn",
            action_type="click",
            evidence=dummy_evidence,
            execution_status=GuardedExecutionStatus.EXECUTED,
            limitations=(),
        )

    # 4. Invariant violation: EXECUTION_CONTROL_EVENT with is_customer_defect=True must raise ValueError
    with pytest.raises(ValueError, match="Contract invariant violation"):
        FunctionalEvaluationResult(
            verdict=FunctionalOutcomeVerdict.EXECUTION_CONTROL_EVENT,
            is_customer_defect=True,
            defect_category=None,
            target_control_id="0:btn",
            action_type="click",
            evidence=dummy_evidence,
            execution_status=GuardedExecutionStatus.ACTION_FAILED,
            limitations=(),
        )

    # 5. Invariant violation: SAFETY_ABORT with is_customer_defect=True must raise ValueError
    with pytest.raises(ValueError, match="Contract invariant violation"):
        FunctionalEvaluationResult(
            verdict=FunctionalOutcomeVerdict.SAFETY_ABORT,
            is_customer_defect=True,
            defect_category=None,
            target_control_id="0:btn",
            action_type="click",
            evidence=dummy_evidence,
            execution_status=GuardedExecutionStatus.DISALLOWED_RISK_TIER,
            limitations=(),
        )


def test_execution_control_events_never_become_customer_defects():
    """Verify that all non-executed execution statuses map to non-customer-defect verdicts."""
    oracle = SafeDisclosureFunctionalOracle()

    # Safety rejection
    res_safe = GuardedExecutionResult(
        status=GuardedExecutionStatus.DISALLOWED_RISK_TIER,
        action_executed=False,
        action_type="click",
        target_control_id="0:risky",
        pre_action_state_fingerprint="fp",
        post_action_state_fingerprint=None,
        reconstruction_result=None,
        limitations=(),
        error_message="High risk control",
        action_attempted=False,
        action_confirmed=False,
    )
    eval_safe = oracle.evaluate_result(res_safe)
    assert eval_safe.verdict == FunctionalOutcomeVerdict.SAFETY_ABORT
    assert eval_safe.is_customer_defect is False

    # Limited coverage (non-disclosure)
    res_cov = GuardedExecutionResult(
        status=GuardedExecutionStatus.NOT_DISCLOSURE_CONTROL,
        action_executed=False,
        action_type="click",
        target_control_id="0:plain",
        pre_action_state_fingerprint="fp",
        post_action_state_fingerprint=None,
        reconstruction_result=None,
        limitations=(),
        error_message="Not a disclosure pattern",
        action_attempted=False,
        action_confirmed=False,
    )
    eval_cov = oracle.evaluate_result(res_cov)
    assert eval_cov.verdict == FunctionalOutcomeVerdict.LIMITED_COVERAGE
    assert eval_cov.is_customer_defect is False

    # Stale state / target not found / action failed
    for st in (
        GuardedExecutionStatus.STALE_STATE,
        GuardedExecutionStatus.TARGET_NOT_FOUND,
        GuardedExecutionStatus.ACTION_FAILED,
        GuardedExecutionStatus.RECONSTRUCTION_FAILED,
    ):
        res_ctrl = GuardedExecutionResult(
            status=st,
            action_executed=False,
            action_type="click",
            target_control_id="0:btn",
            pre_action_state_fingerprint="fp",
            post_action_state_fingerprint=None,
            reconstruction_result=None,
            limitations=(),
            error_message=f"Failure: {st.value}",
            action_attempted=False,
            action_confirmed=False,
        )
        eval_ctrl = oracle.evaluate_result(res_ctrl)
        assert eval_ctrl.verdict == FunctionalOutcomeVerdict.EXECUTION_CONTROL_EVENT
        assert eval_ctrl.is_customer_defect is False

    # Cancellation evaluates to LIMITED_COVERAGE and is not a customer defect
    res_canc = GuardedExecutionResult(
        status=GuardedExecutionStatus.CANCELLED,
        action_executed=False,
        action_type="click",
        target_control_id="0:btn",
        pre_action_state_fingerprint="fp",
        post_action_state_fingerprint=None,
        reconstruction_result=None,
        limitations=(),
        error_message="Execution cancelled",
        action_attempted=False,
        action_confirmed=False,
    )
    eval_canc = oracle.evaluate_result(res_canc)
    assert eval_canc.verdict == FunctionalOutcomeVerdict.LIMITED_COVERAGE
    assert eval_canc.is_customer_defect is False

    # Cleanup failed (teardown issue)
    res_clean = GuardedExecutionResult(
        status=GuardedExecutionStatus.CLEANUP_FAILED,
        action_executed=True,
        action_type="click",
        target_control_id="0:btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp2",
        reconstruction_result=None,
        limitations=(),
        error_message="Teardown failed",
        action_attempted=True,
        action_confirmed=True,
    )
    eval_clean = oracle.evaluate_result(res_clean)
    assert eval_clean.verdict == FunctionalOutcomeVerdict.DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE
    assert eval_clean.is_customer_defect is False


def test_missing_evidence_treated_as_inconclusive():
    """Verify that an executed false -> true transition with missing DOM evidence produces INCONCLUSIVE."""
    oracle = SafeDisclosureFunctionalOracle()

    # Executed action with aria-expanded change, but dom_evidence is empty / missing
    exec_res = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp2",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=False,
        disclosure_expanded_after=True,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=True,
    )

    eval_res = oracle.evaluate_result(exec_res, dom_evidence={})
    # MUST produce INCONCLUSIVE, NOT VERIFIED_SUCCESS!
    assert eval_res.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert eval_res.is_customer_defect is False
    assert eval_res.evidence.observation_sufficient is False
    assert "missing or insufficient" in eval_res.evidence.rationale.lower()


def test_direct_probe_collapse_evaluations():
    """Verify collapse classifier behavior across visible content, missing evidence, and successful hiding."""
    oracle = SafeDisclosureFunctionalOracle()

    # Executed collapse action: aria-expanded changed true -> false
    exec_res = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp2",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=True,
        disclosure_expanded_after=False,
        post_action_controls_count=4,
        action_attempted=True,
        action_confirmed=True,
    )

    # 1. Defect: content remains visible after collapse -> INCONCLUSIVE (Defect classification deferred in this increment)
    eval_fail = oracle.evaluate_result(exec_res, dom_evidence={
        "controlled_element_id": "ans-id",
        "controlled_element_found": True,
        "controlled_element_visible_before": True,
        "controlled_element_visible_after": True,
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
    })
    assert eval_fail.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert eval_fail.is_customer_defect is False
    assert eval_fail.defect_category is None
    assert eval_fail.diagnostics.get("deferred_defect_category") == DefectCategory.CONTENT_NOT_COLLAPSED.value
    assert "deferred" in eval_fail.evidence.rationale.lower()
    assert eval_fail.evidence.controlled_element_visible_after is True

    # 2. Missing evidence: dom_evidence is empty -> INCONCLUSIVE
    eval_inconc = oracle.evaluate_result(exec_res, dom_evidence={})
    assert eval_inconc.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert eval_inconc.is_customer_defect is False
    assert eval_inconc.evidence.observation_sufficient is False

    # 3. Success: controlled element confirmed hidden (False) -> VERIFIED_SUCCESS
    eval_succ = oracle.evaluate_result(exec_res, dom_evidence={
        "controlled_element_id": "ans-id",
        "controlled_element_found": True,
        "controlled_element_visible_before": True,
        "controlled_element_visible_after": False,
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
        "observation_status": "COMPLETED",
    })
    assert eval_succ.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
    assert eval_succ.is_customer_defect is False
    assert eval_succ.defect_category == None
    assert eval_succ.evidence.controlled_element_visible_after is False

    # 4. Observation exception: must NOT produce silent VERIFIED_SUCCESS -> INCONCLUSIVE
    eval_exc = oracle.evaluate_result(exec_res, dom_evidence={
        "controlled_element_id": "ans-id",
        "observation_error": "Evaluation timed out during observation",
    })
    assert eval_exc.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert eval_exc.is_customer_defect is False
    assert "observation_error" in eval_exc.diagnostics


def test_direct_probe_sanitizer_redacts_synthetic_secrets_in_errors():
    """Verify that sanitize_error_text strictly redacts secrets from error strings across multiple formats."""
    # 1. Userinfo credentials in URL
    raw_userinfo = "Error: Failed to connect to http://admin:SUPER_SECRET_PASS_123@127.0.0.1:8080/api"
    cleaned_userinfo = sanitize_error_text(raw_userinfo)
    assert "SUPER_SECRET_PASS_123" not in cleaned_userinfo
    assert "admin:SUPER_SECRET_PASS_123@" not in cleaned_userinfo
    assert "http://127.0.0.1:8080/api" in cleaned_userinfo

    # 2. Sensitive query parameters
    raw_query = "Navigation to http://localhost/callback?token=SYNTHETIC_QUERY_TOKEN_777&password=PW999 failed"
    cleaned_query = sanitize_error_text(raw_query)
    assert "SYNTHETIC_QUERY_TOKEN_777" not in cleaned_query
    assert "PW999" not in cleaned_query
    assert "[REDACTED]" in cleaned_query

    # 3. Explicit token key assignments
    raw_token = "Unhandled exception: token=SYNTHETIC_API_KEY_444 and api_key=KEY_987 in auth handler"
    cleaned_token = sanitize_error_text(raw_token)
    assert "SYNTHETIC_API_KEY_444" not in cleaned_token
    assert "KEY_987" not in cleaned_token
    assert "[REDACTED]" in cleaned_token

    # 4. Bearer tokens
    raw_bearer = "Authorization header Bearer SECRET_BEARER_TOKEN_555 was rejected"
    cleaned_bearer = sanitize_error_text(raw_bearer)
    assert "SECRET_BEARER_TOKEN_555" not in cleaned_bearer
    assert "Bearer [REDACTED]" in cleaned_bearer


# ==============================================================================
# 2. Live Chromium Functional Evaluation Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_verified_functional_success_on_working_disclosure(oracle_server):
    """Verify that a working FAQ disclosure control evaluates to VERIFIED_SUCCESS."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/valid-faq"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "faq-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.defect_category is None
        assert eval_res.evidence.action_confirmed is True
        assert eval_res.evidence.expanded_before is False
        assert eval_res.evidence.expanded_after is True
        assert eval_res.evidence.state_toggled is True
        assert eval_res.evidence.controlled_element_found is True
        assert eval_res.evidence.controlled_element_visible_after is True

        # Invariance check
        source_exp = await source_page.evaluate("() => document.getElementById('faq-btn').getAttribute('aria-expanded')")
        assert source_exp == "false", "Source page was unexpectedly mutated!"

        await browser.close()


@pytest.mark.asyncio
async def test_verified_functional_success_on_collapse_action(oracle_server):
    """Verify that collapsing an expanded disclosure section evaluates to VERIFIED_SUCCESS."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/valid-collapse"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "collapse-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.evidence.action_confirmed is True
        assert eval_res.evidence.expanded_before is True
        assert eval_res.evidence.expanded_after is False
        assert eval_res.evidence.state_toggled is True
        assert eval_res.evidence.controlled_element_found is True
        assert eval_res.evidence.controlled_element_visible_after is False

        await browser.close()


@pytest.mark.asyncio
async def test_supported_failure_on_content_not_collapsed(oracle_server):
    """Regression 1: Verify that when aria-expanded toggles to false but content stays visible, evaluates to non-defect INCONCLUSIVE (deferred defect classification)."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/broken-collapse"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "broken-collapse-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        # In this first increment, defect classification is deferred -> honest non-defect INCONCLUSIVE
        assert eval_res.verdict in (FunctionalOutcomeVerdict.INCONCLUSIVE, FunctionalOutcomeVerdict.LIMITED_COVERAGE)
        assert eval_res.is_customer_defect is False
        assert eval_res.defect_category is None
        assert eval_res.evidence.expanded_before is True
        assert eval_res.evidence.expanded_after is False
        assert eval_res.evidence.controlled_element_visible_after is True

        await browser.close()


@pytest.mark.asyncio
async def test_supported_failure_on_inert_disclosure_control(oracle_server):
    """Verify that an inert disclosure control evaluates to non-defect INCONCLUSIVE (deferred defect classification)."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/inert-faq"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "inert-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert eval_res.verdict in (FunctionalOutcomeVerdict.INCONCLUSIVE, FunctionalOutcomeVerdict.LIMITED_COVERAGE)
        assert eval_res.is_customer_defect is False
        assert eval_res.defect_category is None
        assert eval_res.evidence.action_confirmed is True
        assert eval_res.evidence.expanded_before is False
        assert eval_res.evidence.expanded_after is False
        assert eval_res.evidence.state_toggled is False

        await browser.close()


@pytest.mark.asyncio
async def test_supported_failure_on_dangling_aria_controls(oracle_server):
    """Verify that a button referencing a non-existent aria-controls ID evaluates to non-defect INCONCLUSIVE (deferred defect classification)."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/dangling-controls"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "dangling-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert eval_res.verdict in (FunctionalOutcomeVerdict.INCONCLUSIVE, FunctionalOutcomeVerdict.LIMITED_COVERAGE)
        assert eval_res.is_customer_defect is False
        assert eval_res.defect_category is None
        assert eval_res.evidence.controlled_element_id == "non-existent-target-id"
        assert eval_res.evidence.controlled_element_found is False

        await browser.close()


@pytest.mark.asyncio
async def test_supported_failure_on_content_not_revealed(oracle_server):
    """Verify that when aria-expanded toggles but content stays hidden, evaluates to non-defect INCONCLUSIVE (deferred defect classification)."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/hidden-content"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "broken-reveal-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert eval_res.verdict in (FunctionalOutcomeVerdict.INCONCLUSIVE, FunctionalOutcomeVerdict.LIMITED_COVERAGE)
        assert eval_res.is_customer_defect is False
        assert eval_res.defect_category is None
        assert eval_res.evidence.controlled_element_id == "broken-ans"
        assert eval_res.evidence.controlled_element_found is True
        assert eval_res.evidence.controlled_element_visible_after is False

        await browser.close()


@pytest.mark.asyncio
async def test_supported_failure_on_page_script_error(oracle_server):
    """Verify that an uncaught exception in the page disclosure click handler evaluates to non-defect INCONCLUSIVE (deferred defect classification)."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/script-error"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "crash-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert eval_res.verdict in (FunctionalOutcomeVerdict.INCONCLUSIVE, FunctionalOutcomeVerdict.LIMITED_COVERAGE)
        assert eval_res.is_customer_defect is False
        assert eval_res.defect_category is None
        assert len(eval_res.evidence.page_script_errors) > 0
        assert eval_res.evidence.page_script_errors[0] == "TypeError"

        await browser.close()


# ==============================================================================
# 3. Privacy-Safe Structured Error Capture & Causality Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_privacy_safe_structured_page_error_redacts_credentials(oracle_server):
    """Regression 2: Verify raw credentials and tokens in page errors are strictly omitted from repr and asdict."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/secret-error"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "secret-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        # 1. Evaluates to honest non-defect INCONCLUSIVE (deferred defect classification)
        assert eval_res.verdict in (FunctionalOutcomeVerdict.INCONCLUSIVE, FunctionalOutcomeVerdict.LIMITED_COVERAGE)
        assert eval_res.defect_category is None
        assert eval_res.is_customer_defect is False

        # 2. Strict Privacy Verification: secrets must NEVER survive repr() or asdict()
        res_repr = repr(eval_res)
        res_dict_str = str(asdict(eval_res))

        assert "SYNTHETIC_API_TOKEN_9999" not in res_repr, "Raw API token leaked in repr()!"
        assert "SYNTHETIC_API_TOKEN_9999" not in res_dict_str, "Raw API token leaked in asdict()!"
        assert "SYNTHETIC_USER_PASS" not in res_repr, "Raw password leaked in repr()!"
        assert "SYNTHETIC_USER_PASS" not in res_dict_str, "Raw password leaked in asdict()!"

        # 3. Verify only allowlisted category is exported, zero arbitrary message text
        assert eval_res.evidence.page_script_errors == ("Error",)

        await browser.close()


@pytest.mark.asyncio
async def test_privacy_safe_structured_page_error_redacts_unlabelled_secret(oracle_server):
    """Regression: Verify unlabelled synthetic secret (e.g. SYNTHETIC_PRIVATE_83XZ) is not leaked in repr() or asdict()."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/unlabelled-secret-error"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "unlabelled-secret-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert eval_res.verdict in (FunctionalOutcomeVerdict.INCONCLUSIVE, FunctionalOutcomeVerdict.LIMITED_COVERAGE)
        assert eval_res.defect_category is None
        assert eval_res.is_customer_defect is False
        assert eval_res.evidence.page_script_errors == ("Error",)

        res_repr = repr(eval_res)
        res_dict_str = str(asdict(eval_res))

        assert "SYNTHETIC_PRIVATE_83XZ" not in res_repr, "Unlabelled secret leaked in repr()!"
        assert "SYNTHETIC_PRIVATE_83XZ" not in res_dict_str, "Unlabelled secret leaked in asdict()!"

        await browser.close()


@pytest.mark.asyncio
async def test_generic_error_during_click_window_evaluates_to_verified_success(oracle_server):
    """Regression: Verify an unrelated generic error during click window on a working disclosure evaluates to VERIFIED_SUCCESS."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/generic-error-during-click"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "generic-err-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.evidence.action_confirmed is True
        assert eval_res.evidence.expanded_after is True
        assert eval_res.evidence.controlled_element_visible_after is True
        assert eval_res.diagnostics.get("observed_non_fatal_errors") == ("TypeError",)

        res_repr = repr(eval_res)
        res_dict_str = str(asdict(eval_res))
        assert "Cannot read properties" not in res_repr
        assert "Cannot read properties" not in res_dict_str

        await browser.close()


@pytest.mark.asyncio
async def test_network_lockdown_error_does_not_become_customer_defect(oracle_server):
    """Regression 3: Verify that network barrier lockdown fetch failures do NOT become customer defects."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/lockdown-fetch-background"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "faq-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        # The working disclosure must be VERIFIED_SUCCESS, NOT a customer defect!
        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.evidence.action_confirmed is True
        assert eval_res.evidence.expanded_after is True
        assert eval_res.evidence.controlled_element_visible_after is True

        # Diagnostics note the ignored lockdown error
        assert "unrelated_lockdown_artifacts_ignored" in eval_res.diagnostics

        await browser.close()


@pytest.mark.asyncio
async def test_unrelated_background_error_not_attributed_to_disclosure(oracle_server):
    """Regression 4: Verify that an unrelated background timer error is not treated as a disclosure defect."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/unrelated-background-error"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "faq-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        # Working disclosure is verified success, not poisoned by unrelated background timer error
        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.evidence.action_confirmed is True

        await browser.close()


@pytest.mark.asyncio
async def test_synchronous_lockdown_error_during_click_not_customer_defect(oracle_server):
    """Regression 5: Verify synchronous network lockdown error dispatched during click window is not a customer defect."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/lockdown-error-during-click"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "faq-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.evidence.action_confirmed is True
        assert eval_res.evidence.expanded_after is True
        assert "unrelated_lockdown_artifacts_ignored" in eval_res.diagnostics

        await browser.close()


@pytest.mark.asyncio
async def test_unrelated_background_error_during_click_window_not_customer_defect(oracle_server):
    """Regression 6: Verify background activity error dispatched during click window is not a customer defect."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/background-error-during-click"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "faq-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.evidence.action_confirmed is True
        assert eval_res.evidence.expanded_after is True
        assert "unrelated_background_errors_ignored" in eval_res.diagnostics

        await browser.close()


@pytest.mark.asyncio
async def test_execution_control_failure_on_occluded_target_not_a_customer_defect(oracle_server):
    """Verify that actionability failure (occlusion) evaluates to EXECUTION_CONTROL_EVENT, NOT a customer defect."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/occluded"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "faq-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
            timeout=1.0,
        )

        # Must be EXECUTION_CONTROL_EVENT, NOT a customer defect!
        assert eval_res.verdict == FunctionalOutcomeVerdict.EXECUTION_CONTROL_EVENT
        assert eval_res.is_customer_defect is False
        assert eval_res.execution_status == GuardedExecutionStatus.ACTION_FAILED

        await browser.close()


@pytest.mark.asyncio
async def test_non_disclosure_control_evaluates_to_limited_coverage(oracle_server):
    """Verify that a non-disclosure control evaluates to LIMITED_COVERAGE, NOT a customer defect."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/ambiguous"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "ambiguous-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.LIMITED_COVERAGE
        assert eval_res.is_customer_defect is False
        assert eval_res.execution_status == GuardedExecutionStatus.NOT_DISCLOSURE_CONTROL

        await browser.close()


def test_inconclusive_verdict_on_ambiguous_state_transition():
    """Verify that an ambiguous state transition with missing indicators evaluates to INCONCLUSIVE."""
    oracle = SafeDisclosureFunctionalOracle()

    # Executed action where state changed, but neither aria-expanded nor standard disclosure indicators exist
    exec_res = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp2",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=None,
        disclosure_expanded_after=None,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=True,
    )

    eval_res = oracle.evaluate_result(exec_res, dom_evidence={})
    assert eval_res.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert eval_res.is_customer_defect is False
    assert eval_res.defect_category is None
    assert "ambiguous" in eval_res.evidence.rationale.lower()


def test_direct_probe_temporal_coincidence_does_not_become_claimed_causality():
    """Verify reviewer reproduction probe: executed false -> true with missing DOM evidence and StructuredPageError produces INCONCLUSIVE."""
    oracle = SafeDisclosureFunctionalOracle()
    exec_res = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:faq-btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp2",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=False,
        disclosure_expanded_after=True,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=True,
    )
    # Direct classifier probe with StructuredPageError during click window
    probe_error = StructuredPageError(
        error_type="TypeError",
        sanitized_message="Cannot read properties of undefined",
        occurred_during_click=True,
    )
    dom_ev_with_error = {
        "structured_page_errors": [probe_error],
    }
    eval_with_err = oracle.evaluate_result(exec_res, dom_evidence=dom_ev_with_error)

    # Must be INCONCLUSIVE and not a customer defect!
    assert eval_with_err.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert eval_with_err.is_customer_defect is False
    assert eval_with_err.defect_category is None
    assert eval_with_err.diagnostics.get("script_errors") == ("TypeError",)

    # Identical execution without error must also produce INCONCLUSIVE
    eval_without_err = oracle.evaluate_result(exec_res, dom_evidence={})
    assert eval_without_err.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert eval_without_err.is_customer_defect is False
    assert eval_without_err.defect_category is None


def test_direct_probe_observation_exception_diagnostics_privacy_safe():
    """Verify that observation exception diagnostics never export arbitrary exception messages."""
    oracle = SafeDisclosureFunctionalOracle()
    exec_res = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:faq-btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp2",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=False,
        disclosure_expanded_after=True,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=True,
    )
    dom_ev = {
        "observation_error": "SyntaxError: Unexpected token SYNTHETIC_PRIVATE_83XZ in JSON at position 42",
    }
    eval_res = oracle.evaluate_result(exec_res, dom_evidence=dom_ev)
    assert eval_res.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert eval_res.is_customer_defect is False
    assert eval_res.diagnostics.get("observation_error") == "DOM_OBSERVATION_EXCEPTION"
    assert "SYNTHETIC_PRIVATE_83XZ" not in repr(eval_res)
    assert "SYNTHETIC_PRIVATE_83XZ" not in str(asdict(eval_res))


@pytest.mark.asyncio
async def test_source_context_invariance_and_resource_cleanup_verified(oracle_server):
    """Verify source context URL and pages remain invariant and disposable contexts are closed."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/valid-faq"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "faq-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS

        # Source context invariance checks
        assert source_page.url == target_url
        assert len(source_ctx.pages) == 1
        source_btn_exp = await source_page.evaluate("() => document.getElementById('faq-btn').getAttribute('aria-expanded')")
        assert source_btn_exp == "false", "Source page state was modified!"

        # Resource cleanup verification: disposable context is closed before browser.close()
        assert browser.contexts == [source_ctx], "Disposable context leaked!"
        await browser.close()


# ==============================================================================
# 4. Phase C Evidence Contract Correction Regressions
# ==============================================================================

@pytest.mark.asyncio
async def test_verified_functional_success_on_delayed_expansion(oracle_server):
    """Verify Reproduction 1 fix: asynchronous expansion via setTimeout(..., 300) evaluates to VERIFIED_SUCCESS."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/delayed-faq"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "delayed-faq-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
            timeout=5.0,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.evidence.action_confirmed is True
        assert eval_res.evidence.expanded_before is False
        assert eval_res.evidence.expanded_after is True
        assert eval_res.evidence.state_toggled is True
        assert eval_res.evidence.controlled_element_found is True
        assert eval_res.evidence.controlled_element_visible_after is True

        await browser.close()


@pytest.mark.asyncio
async def test_verified_functional_success_on_delayed_collapse(oracle_server):
    """Verify asynchronous collapse via setTimeout(..., 300) evaluates to VERIFIED_SUCCESS."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/delayed-collapse"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "delayed-collapse-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
            timeout=5.0,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.evidence.action_confirmed is True
        assert eval_res.evidence.expanded_before is True
        assert eval_res.evidence.expanded_after is False
        assert eval_res.evidence.state_toggled is True
        assert eval_res.evidence.controlled_element_found is True
        assert eval_res.evidence.controlled_element_visible_after is False

        await browser.close()


@pytest.mark.asyncio
async def test_network_dependent_disclosure_blocked_by_qa_not_customer_defect(oracle_server):
    """Verify Reproduction 2 fix: network-dependent disclosure blocked by QA evaluates to LIMITED_COVERAGE, NOT customer defect."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/network-dependent-disclosure"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "network-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
            timeout=2.0,
        )

        # Must NOT be classified as a customer defect!
        assert eval_res.verdict == FunctionalOutcomeVerdict.LIMITED_COVERAGE
        assert eval_res.is_customer_defect is False
        assert eval_res.defect_category is None
        assert eval_res.diagnostics.get("qa_network_interference") is True
        assert "network_dependent_disclosure_outside_offline_scope" in eval_res.limitations

        await browser.close()


def test_failed_observation_on_inert_control_evaluates_to_inconclusive_not_defect():
    """Verify Reproduction 3 fix: unchanged state with observation_error="DOM_OBSERVATION_EXCEPTION" produces INCONCLUSIVE, NOT customer defect."""
    oracle = SafeDisclosureFunctionalOracle()
    exec_res = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:faq-btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp1",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=False,
        disclosure_expanded_after=False,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=True,
    )
    dom_ev = {
        "controlled_element_id": "faq-ans",
        "observation_error": "DOM_OBSERVATION_EXCEPTION",
    }
    eval_res = oracle.evaluate_result(exec_res, dom_evidence=dom_ev)

    assert eval_res.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert eval_res.is_customer_defect is False
    assert eval_res.defect_category is None
    assert eval_res.evidence.observation_sufficient is False
    assert eval_res.diagnostics.get("observation_error") == "DOM_OBSERVATION_EXCEPTION"


def test_missing_dom_evidence_on_inert_control_evaluates_to_inconclusive_not_defect():
    """Verify that unchanged expanded state with missing dom_evidence produces INCONCLUSIVE, NOT customer defect."""
    oracle = SafeDisclosureFunctionalOracle()
    exec_res = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:faq-btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp1",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=False,
        disclosure_expanded_after=False,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=True,
    )
    eval_res = oracle.evaluate_result(exec_res, dom_evidence={})

    assert eval_res.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert eval_res.is_customer_defect is False
    assert eval_res.defect_category is None
    assert eval_res.evidence.observation_sufficient is False
    assert "missing" in eval_res.diagnostics.get("missing_evidence", "").lower()


def test_failed_observation_on_revealed_control_evaluates_to_inconclusive():
    """Verify that observation exception during expansion produces INCONCLUSIVE, NOT customer defect."""
    oracle = SafeDisclosureFunctionalOracle()
    exec_res = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:faq-btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp2",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=False,
        disclosure_expanded_after=True,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=True,
    )
    dom_ev = {
        "controlled_element_id": "faq-ans",
        "observation_error": "DOM_OBSERVATION_EXCEPTION",
    }
    eval_res = oracle.evaluate_result(exec_res, dom_evidence=dom_ev)

    assert eval_res.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert eval_res.is_customer_defect is False
    assert eval_res.defect_category is None
    assert eval_res.evidence.observation_sufficient is False
    assert eval_res.diagnostics.get("observation_error") == "DOM_OBSERVATION_EXCEPTION"


def test_insufficient_observation_flag_evaluates_to_inconclusive():
    """Verify that observation_sufficient=False explicitly prevents customer defect classification."""
    oracle = SafeDisclosureFunctionalOracle()
    exec_res = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:faq-btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp1",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=False,
        disclosure_expanded_after=False,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=True,
    )
    dom_ev = {
        "controlled_element_id": "faq-ans",
        "observation_sufficient": False,
    }
    eval_res = oracle.evaluate_result(exec_res, dom_evidence=dom_ev)

    assert eval_res.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert eval_res.is_customer_defect is False
    assert eval_res.defect_category is None
    assert eval_res.evidence.observation_sufficient is False


@pytest.mark.asyncio
async def test_cancellation_during_observation_wait(oracle_server):
    """Verify that cancellation during the asynchronous observation wait aborts boundedly with clean disposal."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/long-delayed-faq"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "long-delayed-btn")
        cancel_token = CancellationToken()

        # Schedule cancellation after 150ms while observation wait is active
        async def cancel_later():
            await asyncio.sleep(0.15)
            cancel_token.cancel()

        cancel_task = asyncio.create_task(cancel_later())

        start_time = time.monotonic()
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
            timeout=10.0,
            cancellation_token=cancel_token,
        )
        elapsed = time.monotonic() - start_time
        await cancel_task

        # Observation wait must have broken boundedly well before the 10s timer
        assert elapsed < 3.0, f"Observation wait took too long to abort on cancellation: {elapsed}s"
        assert eval_res.is_customer_defect is False

        # Invariance and resource cleanup: only source context remains
        assert browser.contexts == [source_ctx]
        assert len(source_ctx.pages) == 1

        await browser.close()


@pytest.mark.asyncio
async def test_attribute_first_partial_transition_evaluates_to_verified_success(oracle_server):
    """Verify Reproduction 1 fix: immediate attribute toggle followed by 500ms reveal evaluates to VERIFIED_SUCCESS."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/attribute-first-delayed-reveal"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "attr-first-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
            timeout=5.0,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.defect_category is None
        assert eval_res.evidence.action_confirmed is True
        assert eval_res.evidence.expanded_before is False
        assert eval_res.evidence.expanded_after is True
        assert eval_res.evidence.state_toggled is True
        assert eval_res.evidence.controlled_element_found is True
        assert eval_res.evidence.controlled_element_visible_after is True
        assert eval_res.evidence.observation_sufficient is True

        await browser.close()


@pytest.mark.asyncio
async def test_visibility_first_partial_transition_evaluates_to_verified_success(oracle_server):
    """Verify visibility-first transition: immediate reveal followed by 300ms attribute toggle evaluates to VERIFIED_SUCCESS."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/visibility-first-delayed-attribute"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "vis-first-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
            timeout=5.0,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.defect_category is None
        assert eval_res.evidence.action_confirmed is True
        assert eval_res.evidence.expanded_before is False
        assert eval_res.evidence.expanded_after is True
        assert eval_res.evidence.state_toggled is True
        assert eval_res.evidence.controlled_element_found is True
        assert eval_res.evidence.controlled_element_visible_after is True
        assert eval_res.evidence.observation_sufficient is True

        await browser.close()


@pytest.mark.asyncio
async def test_transition_completing_beyond_budget_evaluates_to_non_defect(oracle_server):
    """Verify Reproduction 2 fix: 1500ms delayed transition exceeding 1000ms budget evaluates to non-defect outcome."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/delayed-1500-faq"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "delayed-1500-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
            timeout=5.0,
        )

        # Budget exhaustion must NEVER be classified as a customer defect
        assert eval_res.is_customer_defect is False
        assert eval_res.defect_category is None
        assert eval_res.verdict in (FunctionalOutcomeVerdict.LIMITED_COVERAGE, FunctionalOutcomeVerdict.INCONCLUSIVE)
        assert eval_res.evidence.observation_sufficient is False
        assert eval_res.diagnostics.get("observation_status") == "TIMED_OUT" or eval_res.diagnostics.get("observation_budget_exhausted") is True

        await browser.close()


def test_missing_evidence_bypass_with_diagnostics_only_evaluates_to_inconclusive():
    """Verify Reproduction 3 fix: diagnostics-only dictionary is not treated as evidence sufficiency."""
    oracle = SafeDisclosureFunctionalOracle()
    exec_res = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:faq-btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp1",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=False,
        disclosure_expanded_after=False,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=True,
    )

    # 1. Empty dictionary
    eval_empty = oracle.evaluate_result(exec_res, dom_evidence={})
    assert eval_empty.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert eval_empty.is_customer_defect is False
    assert eval_empty.defect_category is None
    assert eval_empty.evidence.observation_sufficient is False

    # 2. Diagnostics-only dictionary with empty structured_page_errors
    eval_diag_empty = oracle.evaluate_result(exec_res, dom_evidence={"structured_page_errors": []})
    assert eval_diag_empty.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert eval_diag_empty.is_customer_defect is False
    assert eval_diag_empty.defect_category is None
    assert eval_diag_empty.evidence.observation_sufficient is False

    # 3. Diagnostics-only dictionary with empty page_script_errors
    eval_script_empty = oracle.evaluate_result(exec_res, dom_evidence={"page_script_errors": ()})
    assert eval_script_empty.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert eval_script_empty.is_customer_defect is False
    assert eval_script_empty.defect_category is None
    assert eval_script_empty.evidence.observation_sufficient is False

    # 4. Invariant: all produce identical non-defect inconclusive verdict
    assert eval_empty.verdict == eval_diag_empty.verdict == eval_script_empty.verdict
    assert eval_empty.is_customer_defect == eval_diag_empty.is_customer_defect == eval_script_empty.is_customer_defect


# ==============================================================================
# 5. First Bounded Increment Mandatory Acceptance Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_native_timer_saved_during_page_load_evaluates_to_verified_success(oracle_server):
    """Acceptance B: Verify that a native timer saved during page load and used by click handler completes within budget -> VERIFIED_SUCCESS."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/native-timer-disclosure"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "native-timer-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
            timeout=5.0,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.defect_category is None
        assert eval_res.evidence.action_confirmed is True
        assert eval_res.evidence.expanded_before is False
        assert eval_res.evidence.expanded_after is True
        assert eval_res.evidence.controlled_element_found is True
        assert eval_res.evidence.controlled_element_visible_after is True

        await browser.close()


@pytest.mark.asyncio
async def test_exact_frame_target_observation_evaluates_to_verified_success(oracle_server):
    """Acceptance A: Verify exact frame/target observation for disclosure control inside an iframe."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/iframe-disclosure-page"
        await source_page.goto(target_url)

        # Wait for subframe and button to load
        iframe_el = await source_page.wait_for_selector("#subframe")
        frame = await iframe_el.content_frame()
        assert frame is not None
        await frame.wait_for_selector("#faq-btn")

        intent = await _derive_intent_from_page(source_page, "faq-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
            timeout=5.0,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.evidence.action_confirmed is True
        assert eval_res.evidence.expanded_after is True
        assert eval_res.evidence.controlled_element_visible_after is True

        await browser.close()


@pytest.mark.asyncio
async def test_multiple_controlled_elements_evaluates_to_limited_coverage(oracle_server):
    """Acceptance B: Verify that multiple aria-controls targets evaluate to LIMITED_COVERAGE (unsupported pattern)."""
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/multiple-controls-disclosure"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "multi-ctrl-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
            timeout=5.0,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.LIMITED_COVERAGE
        assert eval_res.is_customer_defect is False
        assert eval_res.defect_category is None
        assert "multiple_controlled_elements_unsupported" in eval_res.limitations

        await browser.close()


def test_empty_provenance_id_and_status_only_evidence_probes_evaluate_to_inconclusive():
    """Acceptance B: Verify empty, diagnostics-only, provenance-only, ID-only, and status-only evidence all evaluate to INCONCLUSIVE."""
    oracle = SafeDisclosureFunctionalOracle()
    exec_res = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:faq-btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp2",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=False,
        disclosure_expanded_after=True,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=True,
    )

    probe_cases = [
        {},
        {"structured_page_errors": []},
        {"observation_provenance": "live_disposable_page"},
        {"controlled_element_id": "faq-ans"},
        {"observation_status": "COMPLETED"},
        {"observation_sufficient": True},
        {"controlled_element_id": "faq-ans", "observation_sufficient": True},
        {"controlled_element_id": "faq-ans", "controlled_element_found": True},
    ]

    for probe in probe_cases:
        res = oracle.evaluate_result(exec_res, dom_evidence=probe)
        assert res.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE, f"Probe {probe} failed to produce INCONCLUSIVE"
        assert res.is_customer_defect is False
        assert res.defect_category is None
        assert res.evidence.observation_sufficient is False


def test_scope_invariant_deferred_customer_defect_classification():
    """Acceptance D (Scope Invariant): Across public oracle evaluation paths, SUPPORTED_FAILURE and is_customer_defect=True are never returned.
    
    Verifies all 5 former failure categories:
    - INERT_DISCLOSURE_CONTROL
    - DANGLING_ARIA_CONTROLS
    - CONTENT_NOT_REVEALED
    - CONTENT_NOT_COLLAPSED
    - PAGE_SCRIPT_ERROR
    """
    oracle = SafeDisclosureFunctionalOracle()

    # Base executed result
    base_exec = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp1",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=False,
        disclosure_expanded_after=False,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=True,
    )

    # 1. Inert control
    inert_ev = {
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
        "controlled_element_id": "ans",
        "controlled_element_found": True,
        "controlled_element_visible_before": False,
        "controlled_element_visible_after": False,
    }
    inert_res = oracle.evaluate_result(base_exec, dom_evidence=inert_ev)
    assert inert_res.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert inert_res.is_customer_defect is False
    assert inert_res.defect_category is None
    assert inert_res.diagnostics.get("deferred_defect_category") == "INERT_DISCLOSURE_CONTROL"
    assert "deferred" in inert_res.evidence.rationale.lower()

    # 2. Dangling aria-controls
    dangling_ev = {
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
        "controlled_element_id": "nonexistent",
        "controlled_element_found": False,
        "controlled_element_visible_before": False,
        "controlled_element_visible_after": False,
    }
    dangling_res = oracle.evaluate_result(base_exec, dom_evidence=dangling_ev)
    assert dangling_res.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert dangling_res.is_customer_defect is False
    assert dangling_res.defect_category is None
    assert dangling_res.diagnostics.get("deferred_defect_category") == "DANGLING_ARIA_CONTROLS"
    assert "deferred" in dangling_res.evidence.rationale.lower()

    # 3. Content not revealed
    reveal_exec = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp2",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=False,
        disclosure_expanded_after=True,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=True,
    )
    unrevealed_ev = {
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
        "controlled_element_id": "ans",
        "controlled_element_found": True,
        "controlled_element_visible_before": False,
        "controlled_element_visible_after": False,
    }
    unrevealed_res = oracle.evaluate_result(reveal_exec, dom_evidence=unrevealed_ev)
    assert unrevealed_res.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert unrevealed_res.is_customer_defect is False
    assert unrevealed_res.defect_category is None
    assert unrevealed_res.diagnostics.get("deferred_defect_category") == "CONTENT_NOT_REVEALED"
    assert "deferred" in unrevealed_res.evidence.rationale.lower()

    # 4. Content not collapsed
    collapse_exec = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp2",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=True,
        disclosure_expanded_after=False,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=True,
    )
    uncollapsed_ev = {
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
        "controlled_element_id": "ans",
        "controlled_element_found": True,
        "controlled_element_visible_before": True,
        "controlled_element_visible_after": True,
    }
    uncollapsed_res = oracle.evaluate_result(collapse_exec, dom_evidence=uncollapsed_ev)
    assert uncollapsed_res.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert uncollapsed_res.is_customer_defect is False
    assert uncollapsed_res.defect_category is None
    assert uncollapsed_res.diagnostics.get("deferred_defect_category") == "CONTENT_NOT_COLLAPSED"
    assert "deferred" in uncollapsed_res.evidence.rationale.lower()

    # 5. Page script error
    script_err_ev = {
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
        "controlled_element_id": "ans",
        "controlled_element_found": True,
        "controlled_element_visible_before": False,
        "controlled_element_visible_after": False,
        "structured_page_errors": [
            StructuredPageError(error_type="TypeError", occurred_during_click=True)
        ],
    }
    script_err_res = oracle.evaluate_result(base_exec, dom_evidence=script_err_ev)
    assert script_err_res.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert script_err_res.is_customer_defect is False
    assert script_err_res.defect_category is None
    assert script_err_res.diagnostics.get("deferred_defect_category") == "PAGE_SCRIPT_ERROR"
    assert "deferred" in script_err_res.evidence.rationale.lower()


@pytest.mark.asyncio
async def test_real_chromium_expansion_when_content_already_visible_cannot_be_verified_success(oracle_server):
    """Verify Reproduction 3: Real Chromium fixture where content is already visible before expansion.

    Must NOT return VERIFIED_SUCCESS because no hidden -> visible content transition occurred.
    Must return a non-defect outcome (INCONCLUSIVE or LIMITED_COVERAGE) with is_customer_defect=False.
    """
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/already-visible-disclosure"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "already-vis-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
            timeout=1.0,
        )

        assert eval_res.verdict != FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.verdict in (
            FunctionalOutcomeVerdict.INCONCLUSIVE,
            FunctionalOutcomeVerdict.LIMITED_COVERAGE,
        )
        assert eval_res.is_customer_defect is False
        assert eval_res.defect_category is None
        assert eval_res.evidence.controlled_element_visible_before is True
        assert eval_res.evidence.controlled_element_visible_after is True

        await browser.close()


@pytest.mark.asyncio
async def test_real_chromium_collapse_when_content_already_hidden_cannot_be_verified_success(oracle_server):
    """Verify that a collapse action where content is already hidden before click cannot return VERIFIED_SUCCESS.

    Must NOT return VERIFIED_SUCCESS because no visible -> hidden content transition occurred.
    Must return a non-defect outcome with is_customer_defect=False.
    """
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{oracle_server.origin}/already-hidden-collapse"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "already-hidden-btn")
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
            timeout=1.0,
        )

        assert eval_res.verdict != FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.verdict in (
            FunctionalOutcomeVerdict.INCONCLUSIVE,
            FunctionalOutcomeVerdict.LIMITED_COVERAGE,
        )
        assert eval_res.is_customer_defect is False
        assert eval_res.defect_category is None
        assert eval_res.evidence.controlled_element_visible_before is False
        assert eval_res.evidence.controlled_element_visible_after is False

        await browser.close()


def test_positive_success_eligibility_and_evidence_fabrication_rejection_probes():
    """Comprehensive acceptance matrix test for evaluate_result success eligibility and evidence preservation."""
    oracle = SafeDisclosureFunctionalOracle()

    # Base valid executed action result (expansion)
    valid_exec = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp2",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=False,
        disclosure_expanded_after=True,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=True,
    )

    # 1. Confirmed Reproduction 1: Executed False -> True transition with only partial evidence
    # {"controlled_element_id": "panel", "controlled_element_found": True, "controlled_element_visible_after": True}
    # MUST return INCONCLUSIVE and MUST NOT fabricate pre_observation_succeeded=True
    partial_ev_1 = {
        "controlled_element_id": "panel",
        "controlled_element_found": True,
        "controlled_element_visible_after": True,
    }
    res_1 = oracle.evaluate_result(valid_exec, dom_evidence=partial_ev_1)
    assert res_1.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert res_1.is_customer_defect is False
    assert res_1.defect_category is None
    assert res_1.evidence.pre_observation_succeeded is False
    assert res_1.evidence.post_observation_succeeded is False

    # 2. Confirmed Reproduction 2: Same with pre_observation_succeeded explicitly False
    partial_ev_2 = {
        "controlled_element_id": "panel",
        "controlled_element_found": True,
        "controlled_element_visible_after": True,
        "pre_observation_succeeded": False,
    }
    res_2 = oracle.evaluate_result(valid_exec, dom_evidence=partial_ev_2)
    assert res_2.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert res_2.is_customer_defect is False
    assert res_2.defect_category is None
    assert res_2.evidence.pre_observation_succeeded is False

    # 3. Missing post observation: pre_observation_succeeded=True, post_observation_succeeded=False
    missing_post_ev = {
        "controlled_element_id": "panel",
        "controlled_element_found": True,
        "controlled_element_visible_before": False,
        "controlled_element_visible_after": True,
        "pre_observation_succeeded": True,
        "post_observation_succeeded": False,
        "observation_status": "COMPLETED",
    }
    res_post_miss = oracle.evaluate_result(valid_exec, dom_evidence=missing_post_ev)
    assert res_post_miss.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert res_post_miss.is_customer_defect is False
    assert res_post_miss.evidence.post_observation_succeeded is False

    # 4. Explicit failed flag: observation_status="FAILED" or observation_error set
    failed_ev = {
        "controlled_element_id": "panel",
        "controlled_element_found": True,
        "controlled_element_visible_before": False,
        "controlled_element_visible_after": True,
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
        "observation_status": "FAILED",
        "observation_error": "DOM_OBSERVATION_EXCEPTION",
    }
    res_failed = oracle.evaluate_result(valid_exec, dom_evidence=failed_ev)
    assert res_failed.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert res_failed.is_customer_defect is False

    # 5. Missing completion status: observation_status=None or TIMED_OUT
    timed_out_ev = {
        "controlled_element_id": "panel",
        "controlled_element_found": True,
        "controlled_element_visible_before": False,
        "controlled_element_visible_after": True,
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
        "observation_status": "TIMED_OUT",
        "observation_budget_exhausted": True,
    }
    res_timeout = oracle.evaluate_result(valid_exec, dom_evidence=timed_out_ev)
    assert res_timeout.verdict == FunctionalOutcomeVerdict.LIMITED_COVERAGE
    assert res_timeout.is_customer_defect is False

    # 6. Unconfirmed action: action_confirmed=False or action_attempted=False
    unconfirmed_exec = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=False,
        action_type="click",
        target_control_id="0:btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp2",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=False,
        disclosure_expanded_after=True,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=False,
    )
    res_unconfirmed = oracle.evaluate_result(unconfirmed_exec, dom_evidence={
        "controlled_element_id": "panel",
        "controlled_element_found": True,
        "controlled_element_visible_before": False,
        "controlled_element_visible_after": True,
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
        "observation_status": "COMPLETED",
    })
    assert res_unconfirmed.verdict != FunctionalOutcomeVerdict.VERIFIED_SUCCESS
    assert res_unconfirmed.is_customer_defect is False
    assert res_unconfirmed.evidence.action_confirmed is False

    # 7. Content visible before and after expansion (True -> True)
    vis_before_and_after_ev = {
        "controlled_element_id": "panel",
        "controlled_element_found": True,
        "controlled_element_visible_before": True,
        "controlled_element_visible_after": True,
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
        "observation_status": "COMPLETED",
    }
    res_already_vis = oracle.evaluate_result(valid_exec, dom_evidence=vis_before_and_after_ev)
    assert res_already_vis.verdict != FunctionalOutcomeVerdict.VERIFIED_SUCCESS
    assert res_already_vis.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert res_already_vis.is_customer_defect is False

    # 8. Content hidden before and after collapse (False -> False)
    valid_collapse_exec = GuardedExecutionResult(
        status=GuardedExecutionStatus.EXECUTED,
        action_executed=True,
        action_type="click",
        target_control_id="0:btn",
        pre_action_state_fingerprint="fp1",
        post_action_state_fingerprint="fp2",
        reconstruction_result=None,
        limitations=(),
        error_message=None,
        disclosure_expanded_before=True,
        disclosure_expanded_after=False,
        post_action_controls_count=5,
        action_attempted=True,
        action_confirmed=True,
    )
    hidden_before_and_after_ev = {
        "controlled_element_id": "panel",
        "controlled_element_found": True,
        "controlled_element_visible_before": False,
        "controlled_element_visible_after": False,
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
        "observation_status": "COMPLETED",
    }
    res_already_hidden = oracle.evaluate_result(valid_collapse_exec, dom_evidence=hidden_before_and_after_ev)
    assert res_already_hidden.verdict != FunctionalOutcomeVerdict.VERIFIED_SUCCESS
    assert res_already_hidden.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert res_already_hidden.is_customer_defect is False

    # 9. Changed controlled-content identity: pre="panelA" != post="panelB"
    changed_id_ev = {
        "controlled_element_id": "panelA",
        "pre_controlled_element_id": "panelA",
        "post_controlled_element_id": "panelB",
        "controlled_element_found": True,
        "controlled_element_visible_before": False,
        "controlled_element_visible_after": True,
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
        "observation_status": "COMPLETED",
    }
    res_changed_id = oracle.evaluate_result(valid_exec, dom_evidence=changed_id_ev)
    assert res_changed_id.verdict != FunctionalOutcomeVerdict.VERIFIED_SUCCESS
    assert res_changed_id.verdict == FunctionalOutcomeVerdict.INCONCLUSIVE
    assert res_changed_id.is_customer_defect is False

    # 10. Valid expansion: exact boolean transitions -> VERIFIED_SUCCESS
    valid_reveal_ev = {
        "controlled_element_id": "panel",
        "pre_controlled_element_id": "panel",
        "post_controlled_element_id": "panel",
        "controlled_element_found": True,
        "controlled_element_visible_before": False,
        "controlled_element_visible_after": True,
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
        "observation_status": "COMPLETED",
    }
    res_reveal = oracle.evaluate_result(valid_exec, dom_evidence=valid_reveal_ev)
    assert res_reveal.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
    assert res_reveal.is_customer_defect is False
    assert res_reveal.evidence.action_confirmed is True
    assert res_reveal.evidence.action_attempted is True
    assert res_reveal.evidence.pre_observation_succeeded is True
    assert res_reveal.evidence.post_observation_succeeded is True
    assert res_reveal.evidence.expanded_before is False
    assert res_reveal.evidence.expanded_after is True
    assert res_reveal.evidence.controlled_element_visible_before is False
    assert res_reveal.evidence.controlled_element_visible_after is True

    # 11. Valid collapse: exact boolean transitions -> VERIFIED_SUCCESS
    valid_collapse_ev = {
        "controlled_element_id": "panel",
        "pre_controlled_element_id": "panel",
        "post_controlled_element_id": "panel",
        "controlled_element_found": True,
        "controlled_element_visible_before": True,
        "controlled_element_visible_after": False,
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
        "observation_status": "COMPLETED",
    }
    res_collapse = oracle.evaluate_result(valid_collapse_exec, dom_evidence=valid_collapse_ev)
    assert res_collapse.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
    assert res_collapse.is_customer_defect is False
    assert res_collapse.evidence.action_confirmed is True
    assert res_collapse.evidence.action_attempted is True
    assert res_collapse.evidence.pre_observation_succeeded is True
    assert res_collapse.evidence.post_observation_succeeded is True
    assert res_collapse.evidence.expanded_before is True
    assert res_collapse.evidence.expanded_after is False
    assert res_collapse.evidence.controlled_element_visible_before is True
    assert res_collapse.evidence.controlled_element_visible_after is False

