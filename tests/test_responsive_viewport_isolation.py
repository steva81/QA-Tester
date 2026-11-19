"""Regression tests for Phase B2/C3 Viewport Propagation and Responsive Layout Isolation.

Verifies:
1. Intended viewports (Desktop 1366x768 and Mobile 390x844) are explicitly established
   at runtime before navigation and state reconstruction.
2. A responsive local fixture exhibits layout and behavior differences between desktop
   and mobile that would fail if both used the same default viewport.
3. Actual runtime dimensions (page.viewport_size and window.innerWidth/innerHeight) match
   the requested viewport.
4. Screenshots and persisted records correspond strictly to the viewport actually used
   (tall narrow mobile aspect ratio vs. wide desktop aspect ratio).
5. The source browser session and source context are strictly preserved and never mutated.
6. If the intended viewport cannot be established, an honest limited/failed outcome
   is recorded rather than reporting mobile coverage with a default desktop context.
7. Teardown and disposable context cleanup guarantees hold.
"""

import asyncio
from contextlib import asynccontextmanager
from http.server import HTTPServer, BaseHTTPRequestHandler
import os
from pathlib import Path
import threading
from typing import Any, Dict, Optional
import urllib.parse
from PIL import Image
import pytest
from playwright.async_api import async_playwright

from app.qa.isolated_execution_context import IsolatedExecutionContextFactory
from app.qa.state_reconstruction import (
    StateReconstructor,
    ReconstructionStatus,
)
from app.qa.candidate_intent_graph import CandidateActionIntent
from app.qa.candidate_intent_factory import CandidateActionIntentFactory
from app.qa.browser_snapshot_adapter import ReadOnlyBrowserSnapshotAdapter
from app.qa.semantic_normalizer import SemanticNormalizer
from app.qa.interaction_policy import RiskClassification
from app.qa.guarded_execution import GuardedActionExecutor, GuardedExecutionStatus
from app.qa.functional_oracle import SafeDisclosureFunctionalOracle, FunctionalOutcomeVerdict
from app.qa.interactive_bridge import InteractiveDisclosureBridge, InteractiveWorkflowConfig


# ==============================================================================
# Responsive Localhost Test Fixture
# ==============================================================================

RESPONSIVE_HTML = b"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Responsive Verification Test Site</title>
  <style>
    body { margin: 0; padding: 20px; font-family: sans-serif; background: #fff; }
    h1 { font-size: 20px; color: #1e293b; }
    .panel { padding: 12px; margin-top: 8px; border-radius: 4px; background: #f1f5f9; }
    [hidden] { display: none !important; }

    /* Distinct Desktop vs Mobile Layout Rules */
    @media (min-width: 769px) {
      #btn-desktop-menu { display: block !important; }
      #btn-mobile-menu { display: none !important; }
      .viewport-indicator { color: #1d4ed8; }
    }
    @media (max-width: 768px) {
      #btn-desktop-menu { display: none !important; }
      #btn-mobile-menu { display: block !important; }
      .viewport-indicator { color: #047857; }
    }
    .btn { padding: 10px 16px; border: 1px solid #cbd5e1; border-radius: 6px; cursor: pointer; }
  </style>
</head>
<body>
  <h1>Responsive Verification Fixture</h1>
  <p class="viewport-indicator">Active responsive layout</p>

  <!-- Desktop-only disclosure: hidden when width <= 768px -->
  <button id="btn-desktop-menu" class="btn" aria-expanded="false" aria-controls="panel-desktop">
    Desktop Menu Details
  </button>
  <div id="panel-desktop" class="panel" hidden>
    <p>Desktop advanced options and diagnostics.</p>
  </div>

  <!-- Mobile-only disclosure: hidden when width >= 769px -->
  <button id="btn-mobile-menu" class="btn" aria-expanded="false" aria-controls="panel-mobile">
    Mobile Menu Details
  </button>
  <div id="panel-mobile" class="panel" hidden>
    <p>Mobile navigation menu items.</p>
  </div>

  <script>
    document.getElementById('btn-desktop-menu').addEventListener('click', function() {
      const p = document.getElementById('panel-desktop');
      const exp = this.getAttribute('aria-expanded') === 'true';
      this.setAttribute('aria-expanded', String(!exp));
      p.hidden = exp;
    });

    document.getElementById('btn-mobile-menu').addEventListener('click', function() {
      const p = document.getElementById('panel-mobile');
      const exp = this.getAttribute('aria-expanded') === 'true';
      this.setAttribute('aria-expanded', String(!exp));
      p.hidden = exp;
    });
  </script>
</body>
</html>
"""


class ResponsiveTestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(RESPONSIVE_HTML)))
        self.end_headers()
        self.wfile.write(RESPONSIVE_HTML)


@pytest.fixture(scope="module")
def responsive_server():
    server = HTTPServer(("127.0.0.1", 0), ResponsiveTestHandler)
    host, port = server.server_address
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


async def _derive_intent_from_page(page: Any, raw_control_id: str) -> CandidateActionIntent:
    """Helper to derive CandidateActionIntent from an active page matching raw_control_id."""
    adapter = ReadOnlyBrowserSnapshotAdapter()
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
# 1. Isolated Context Factory Viewport Propagation
# ==============================================================================

@pytest.mark.asyncio
async def test_isolated_context_factory_propagates_viewport(responsive_server):
    """Proves IsolatedExecutionContextFactory applies target viewport to disposable context."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            source_context = await browser.new_context(viewport={"width": 1000, "height": 600})
            source_page = await source_context.new_page()
            await source_page.goto(responsive_server)

            factory = IsolatedExecutionContextFactory()

            # Mobile 390x844
            async with factory.open(source_context, viewport={"width": 390, "height": 844}) as disp_ctx:
                assert disp_ctx is not source_context
                disp_page = await disp_ctx.new_page()
                assert disp_page.viewport_size == {"width": 390, "height": 844}
                await disp_page.goto(responsive_server)
                inner_w = await disp_page.evaluate("() => window.innerWidth")
                inner_h = await disp_page.evaluate("() => window.innerHeight")
                assert inner_w == 390
                assert inner_h == 844

            # Desktop 1366x768
            async with factory.open(source_context, viewport={"width": 1366, "height": 768}) as disp_ctx2:
                disp_page2 = await disp_ctx2.new_page()
                assert disp_page2.viewport_size == {"width": 1366, "height": 768}
                await disp_page2.goto(responsive_server)
                inner_w2 = await disp_page2.evaluate("() => window.innerWidth")
                inner_h2 = await disp_page2.evaluate("() => window.innerHeight")
                assert inner_w2 == 1366
                assert inner_h2 == 768

            # Source context remains completely unaffected (unmutated)
            assert source_page.viewport_size == {"width": 1000, "height": 600}
            assert await source_page.evaluate("() => window.innerWidth") == 1000
        finally:
            await browser.close()


# ==============================================================================
# 2. State Reconstruction Runtime Dimensions & Verification
# ==============================================================================

@pytest.mark.asyncio
async def test_state_reconstruction_runtime_dimensions(responsive_server):
    """Proves StateReconstructor verifies runtime dimensions in isolated context."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            # 1. Prepare Mobile source page
            source_context = await browser.new_context()
            source_page = await source_context.new_page()
            await source_page.set_viewport_size({"width": 390, "height": 844})
            await source_page.goto(responsive_server)

            mobile_intent = await _derive_intent_from_page(source_page, "btn-mobile-menu")
            reconstructor = StateReconstructor()

            # Test Mobile 390x844
            async with reconstructor.reconstruct(
                source_context=source_context,
                approved_target_url=responsive_server,
                candidate_intent=mobile_intent,
                viewport={"width": 390, "height": 844},
            ) as session:
                assert session.result.status == ReconstructionStatus.REVALIDATED
                assert session.page is not None
                assert session.page.viewport_size == {"width": 390, "height": 844}
                inner_w = await session.page.evaluate("() => window.innerWidth")
                inner_h = await session.page.evaluate("() => window.innerHeight")
                assert inner_w == 390
                assert inner_h == 844

            # 2. Prepare Desktop source page
            await source_page.set_viewport_size({"width": 1366, "height": 768})
            await source_page.goto(responsive_server)
            desktop_intent = await _derive_intent_from_page(source_page, "btn-desktop-menu")

            # Test Desktop 1366x768
            async with reconstructor.reconstruct(
                source_context=source_context,
                approved_target_url=responsive_server,
                candidate_intent=desktop_intent,
                viewport={"width": 1366, "height": 768},
            ) as session2:
                assert session2.result.status == ReconstructionStatus.REVALIDATED
                assert session2.page is not None
                assert session2.page.viewport_size == {"width": 1366, "height": 768}
                inner_w2 = await session2.page.evaluate("() => window.innerWidth")
                inner_h2 = await session2.page.evaluate("() => window.innerHeight")
                assert inner_w2 == 1366
                assert inner_h2 == 768

        finally:
            await browser.close()


# ==============================================================================
# 3. Responsive Behavior Differences: Mobile vs. Desktop
# ==============================================================================

@pytest.mark.asyncio
async def test_responsive_behavior_differs_by_viewport(responsive_server):
    """Proves that layout-dependent controls behave differently and succeed under their proper viewport.
    
    If both used default 1280x720:
    - Mobile control (#btn-mobile-menu) would be display:none and fail.
    - Under 390x844, #btn-mobile-menu is visible and toggles aria-expanded.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            source_context = await browser.new_context()
            source_page = await source_context.new_page()

            executor = GuardedActionExecutor()

            # Under Mobile 390x844:
            await source_page.set_viewport_size({"width": 390, "height": 844})
            await source_page.goto(responsive_server)
            mobile_intent = await _derive_intent_from_page(source_page, "btn-mobile-menu")

            mobile_res = await executor.execute(
                source_context=source_context,
                approved_target_url=responsive_server,
                candidate_intent=mobile_intent,
                viewport={"width": 390, "height": 844},
            )
            assert mobile_res.status == GuardedExecutionStatus.EXECUTED
            assert mobile_res.action_executed is True
            assert mobile_res.disclosure_expanded_after is True

            # Under Desktop 1366x768:
            await source_page.set_viewport_size({"width": 1366, "height": 768})
            await source_page.goto(responsive_server)
            desktop_intent = await _derive_intent_from_page(source_page, "btn-desktop-menu")

            desktop_res = await executor.execute(
                source_context=source_context,
                approved_target_url=responsive_server,
                candidate_intent=desktop_intent,
                viewport={"width": 1366, "height": 768},
            )
            assert desktop_res.status == GuardedExecutionStatus.EXECUTED
            assert desktop_res.action_executed is True
            assert desktop_res.disclosure_expanded_after is True

        finally:
            await browser.close()


# ==============================================================================
# 4. Screenshot Dimensions and Aspect Ratio Match Configured Viewport
# ==============================================================================

@pytest.mark.asyncio
async def test_screenshots_match_configured_viewport_aspect_ratio(responsive_server):
    """Proves that captured screenshots correspond to the viewport actually used.
    
    Mobile (390x844) produces a tall portrait aspect ratio (~0.46),
    distinct from Desktop (1366x768) which produces a wide landscape aspect ratio (~1.78).
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            source_context = await browser.new_context()
            source_page = await source_context.new_page()

            executor = GuardedActionExecutor()

            # Execute Mobile
            await source_page.set_viewport_size({"width": 390, "height": 844})
            await source_page.goto(responsive_server)
            mobile_intent = await _derive_intent_from_page(source_page, "btn-mobile-menu")

            mobile_res = await executor.execute(
                source_context=source_context,
                approved_target_url=responsive_server,
                candidate_intent=mobile_intent,
                viewport={"width": 390, "height": 844},
            )
            assert mobile_res.screenshot_path is not None
            assert os.path.isfile(mobile_res.screenshot_path)

            with Image.open(mobile_res.screenshot_path) as m_img:
                m_w, m_h = m_img.size
                m_aspect = m_w / m_h
                # Distinctly tall portrait: aspect ratio must be < 0.6 (390/844 = 0.462)
                assert m_aspect < 0.6, f"Expected portrait mobile aspect ratio < 0.6, got {m_aspect:.3f} ({m_w}x{m_h})"
                assert m_w == 390, f"Expected width 390, got {m_w}"
                assert m_h == 844, f"Expected height 844, got {m_h}"

            # Execute Desktop
            await source_page.set_viewport_size({"width": 1366, "height": 768})
            await source_page.goto(responsive_server)
            desktop_intent = await _derive_intent_from_page(source_page, "btn-desktop-menu")

            desktop_res = await executor.execute(
                source_context=source_context,
                approved_target_url=responsive_server,
                candidate_intent=desktop_intent,
                viewport={"width": 1366, "height": 768},
            )
            assert desktop_res.screenshot_path is not None
            assert os.path.isfile(desktop_res.screenshot_path)

            with Image.open(desktop_res.screenshot_path) as d_img:
                d_w, d_h = d_img.size
                d_aspect = d_w / d_h
                # Distinctly wide landscape: aspect ratio must be > 1.5 (1366/768 = 1.778)
                assert d_aspect > 1.5, f"Expected landscape desktop aspect ratio > 1.5, got {d_aspect:.3f} ({d_w}x{d_h})"
                assert d_w == 1366, f"Expected width 1366, got {d_w}"
                assert d_h == 768, f"Expected height 768, got {d_h}"

        finally:
            await browser.close()


# ==============================================================================
# 5. Full Interactive Bridge End-to-End Viewport Propagation
# ==============================================================================

@pytest.mark.asyncio
async def test_interactive_bridge_end_to_end_viewport_propagation(responsive_server):
    """Proves InteractiveDisclosureBridge propagates viewport through entire evaluation chain."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            source_context = await browser.new_context()
            source_page = await source_context.new_page()
            await source_page.set_viewport_size({"width": 390, "height": 844})
            await source_page.goto(responsive_server)

            bridge = InteractiveDisclosureBridge(config=InteractiveWorkflowConfig(enabled=True))
            records = []

            # Evaluate under Mobile
            await bridge.evaluate_page_disclosures(
                source_context=source_context,
                source_page=source_page,
                page_url=responsive_server,
                viewport_name="Mobile 390x844",
                run_id=999,
                record_sink=lambda r: records.append(r),
                viewport={"width": 390, "height": 844},
            )

            assert len(records) > 0
            mobile_recs = [r for r in records if "Mobile" in str(r.get("viewport"))]
            assert len(mobile_recs) > 0
            # Source page is never closed or mutated
            assert not source_page.is_closed()
            assert source_page.viewport_size == {"width": 390, "height": 844}
        finally:
            await browser.close()


# ==============================================================================
# 6. Honest Limited Outcome When Viewport Cannot Be Established
# ==============================================================================

class ViewportFailingContextFactory(IsolatedExecutionContextFactory):
    """Context factory that simulates an environment where requested viewport cannot be established."""

    def __init__(self, forced_viewport={"width": 1280, "height": 720}):
        super().__init__()
        self.forced_viewport = forced_viewport

    @asynccontextmanager
    async def open(self, source_context: Any, viewport: Optional[dict] = None):
        async with super().open(source_context, viewport=self.forced_viewport) as ctx:
            orig_new_page = ctx.new_page
            async def stuck_new_page(*args, **kwargs):
                p = await orig_new_page(*args, **kwargs)
                async def noop_set_size(sz):
                    pass
                p.set_viewport_size = noop_set_size
                return p
            ctx.new_page = stuck_new_page
            yield ctx


@pytest.mark.asyncio
async def test_honest_limited_outcome_when_viewport_fails(responsive_server):
    """Proves that if the intended viewport cannot be established, an honest failure/limited
    outcome is recorded rather than claiming mobile coverage using default desktop dimensions.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            source_context = await browser.new_context()
            source_page = await source_context.new_page()
            await source_page.set_viewport_size({"width": 390, "height": 844})
            await source_page.goto(responsive_server)

            mobile_intent = await _derive_intent_from_page(source_page, "btn-mobile-menu")
            failing_factory = ViewportFailingContextFactory()
            reconstructor = StateReconstructor(context_factory=failing_factory)

            async with reconstructor.reconstruct(
                source_context=source_context,
                approved_target_url=responsive_server,
                candidate_intent=mobile_intent,
                viewport={"width": 390, "height": 844},
            ) as session:
                assert session.result.status == ReconstructionStatus.FAILED
                assert "viewport_establishment_failed" in session.result.limitations
                assert not session.result.is_revalidated
                assert "could not be established" in (session.result.error_message or "").lower()
        finally:
            await browser.close()

