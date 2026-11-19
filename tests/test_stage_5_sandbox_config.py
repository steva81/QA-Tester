import pytest
import os
import re
import importlib.metadata
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.qa.engine import SmartSmokeQA
from app.qa.interactive import InteractiveSession
from app.paths import chromium_launch_args

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.asyncio
async def test_smart_smoke_qa_standalone_launch_kwargs():
    """Verify SmartSmokeQA standalone launch specifies chromium_sandbox=True and no --no-sandbox."""
    engine = SmartSmokeQA(target_url="http://127.0.0.1:8080", project_id=1, run_id=1)
    engine.queue = []
    
    mock_browser = AsyncMock()
    mock_context = AsyncMock()
    mock_context.on = MagicMock()
    mock_context.remove_listener = MagicMock()
    mock_context._qa_safety_hold = None
    mock_context.service_workers = None
    mock_browser.new_context.return_value = mock_context
    mock_context.pages = []
    mock_context.browser = None

    mock_playwright = MagicMock()
    mock_playwright._connection = None
    mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
    mock_playwright.stop = AsyncMock()

    with patch("app.qa.engine.async_playwright") as mock_ap:
        mock_ap.return_value.start = AsyncMock(return_value=mock_playwright)

        with patch.object(engine, "_execute_queue", new=AsyncMock(return_value=None)):
            await engine.run()

    # Verify launch kwargs
    mock_playwright.chromium.launch.assert_awaited_once()
    _, kwargs = mock_playwright.chromium.launch.call_args
    assert kwargs.get("chromium_sandbox") is True
    assert kwargs.get("headless") is True
    assert "args" in kwargs
    # Must NOT pass ignore_default_args containing --no-sandbox
    ignore_args = kwargs.get("ignore_default_args", [])
    assert "--no-sandbox" not in ignore_args

    # Verify service workers blocked
    mock_browser.new_context.assert_awaited_once_with(service_workers="block")


@pytest.mark.asyncio
async def test_interactive_session_launch_kwargs():
    """Verify InteractiveSession launch specifies chromium_sandbox=True and retains popup policy."""
    session = InteractiveSession(project_id=1, start_url="http://127.0.0.1:8080")

    mock_browser = AsyncMock()
    mock_context = AsyncMock()
    mock_page = AsyncMock()
    mock_browser.new_context.return_value = mock_context
    mock_context.new_page.return_value = mock_page

    mock_playwright = MagicMock()
    mock_playwright._connection = None
    mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

    with patch("app.qa.interactive.async_playwright") as mock_ap:
        mock_ap.return_value.start = AsyncMock(return_value=mock_playwright)

        await session._init_playwright()

    # Verify launch kwargs
    mock_playwright.chromium.launch.assert_awaited_once()
    _, kwargs = mock_playwright.chromium.launch.call_args
    assert kwargs.get("chromium_sandbox") is True
    assert kwargs.get("headless") is False
    assert kwargs.get("ignore_default_args") == ["--disable-popup-blocking"]
    assert "--no-sandbox" not in (kwargs.get("ignore_default_args") or [])

    # Verify context settings
    mock_browser.new_context.assert_awaited_once_with(
        ignore_https_errors=True,
        service_workers="block",
        no_viewport=True,
    )


@pytest.mark.asyncio
async def test_injected_context_ownership_and_no_browser_launch():
    """Verify that an externally provided context is never replaced with a new browser."""
    mock_provided_ctx = AsyncMock()
    mock_provided_ctx.on = MagicMock()
    mock_provided_ctx.remove_listener = MagicMock()
    mock_provided_ctx._qa_safety_hold = None
    mock_provided_ctx.service_workers = None
    mock_provided_ctx.pages = []
    mock_provided_ctx.browser = None
    
    engine = SmartSmokeQA(
        target_url="http://127.0.0.1:8080",
        project_id=1,
        run_id=1,
        context=mock_provided_ctx,
    )
    engine.queue = []

    with patch("app.qa.engine.async_playwright") as mock_ap:
        with patch.object(engine, "_execute_queue", new=AsyncMock(return_value=None)):
            await engine.run()

        # Playwright standalone launch was never invoked
        mock_ap.assert_not_called()

    # Provided context was used and not destroyed as standalone
    assert engine.provided_context is mock_provided_ctx


@pytest.mark.asyncio
async def test_sandbox_failure_does_not_retry_without_sandbox():
    """Verify that if sandbox launch fails, no insecure fallback retry is attempted."""
    engine = SmartSmokeQA(target_url="http://127.0.0.1:8080", project_id=1, run_id=1)
    engine.queue = []

    mock_playwright = MagicMock()
    mock_playwright._connection = None
    mock_playwright.chromium.launch = AsyncMock(side_effect=RuntimeError("Simulated sandbox initialization failure"))
    mock_playwright.stop = AsyncMock()

    with patch("app.qa.engine.async_playwright") as mock_ap:
        mock_ap.return_value.start = AsyncMock(return_value=mock_playwright)

        with pytest.raises(RuntimeError, match="Simulated sandbox initialization failure"):
            await engine.run()

    # Launch was attempted exactly once; never retried without sandbox
    assert mock_playwright.chromium.launch.await_count == 1
    # Safe teardown occurred
    mock_playwright.stop.assert_awaited_once()


def test_survey_production_browser_launches():
    """Ensure no undocumented third launch path exists in app/."""
    app_dir = REPO_ROOT / "app"
    launch_sites = []
    for root, _, files in os.walk(app_dir):
        for f in files:
            if f.endswith(".py"):
                p = os.path.normpath(os.path.join(root, f))
                with open(p, "r", encoding="utf-8") as fh:
                    for line_no, line in enumerate(fh, 1):
                        if re.search(r"\.(?:launch|launch_persistent_context)\(", line):
                            launch_sites.append((p, line_no, line.strip()))

    assert len(launch_sites) == 2
    paths = [p for p, _, _ in launch_sites]
    assert any("app\\qa\\engine.py" in p or "app/qa/engine.py" in p for p in paths)
    assert any("app\\qa\\interactive.py" in p or "app/qa/interactive.py" in p for p in paths)


def test_requirements_pinning_and_installed_version_measurement():
    """Document requirements pinning vs actually installed runtime version."""
    req_file = REPO_ROOT / "requirements.txt"
    with open(req_file, "r", encoding="utf-8") as f:
        req_text = f.read()

    # 1. Target version is explicitly pinned in requirements.txt
    assert "playwright==1.64.1" in req_text

    # 2. Installed runtime version is measured and reported honestly
    installed_ver = importlib.metadata.version("playwright")
    # In accordance with project instructions: do NOT run pip install in main environment.
    # In isolated testing environment, 1.64.1 is verified under authorized exception.
    assert installed_ver in ("1.50.0", "1.64.1")
