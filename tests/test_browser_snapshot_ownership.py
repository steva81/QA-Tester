import pytest
from app.qa.browser_snapshot_adapter import ReadOnlyBrowserSnapshotAdapter, CancellationToken

class MockFrame:
    def __init__(self, is_detached=False, url="http://test.com", origin="http://test.com", name="main"):
        self._is_detached = is_detached
        self.url = url
        self.name = name
        self.child_frames = []
        self._evaluate_data = {"controls": [], "status": "COMPLETE", "origin": origin}
        self.evaluate_called = False

    def is_detached(self):
        return self._is_detached

    async def evaluate(self, script, *args):
        self.evaluate_called = True
        if hasattr(self, 'eval_exc'):
            raise self.eval_exc
        return self._evaluate_data

    def __getattr__(self, name):
        forbidden = [
            "click", "dblclick", "tap", "hover", "focus", "blur", "fill",
            "type", "press", "press_sequentially", "select_option", "check",
            "uncheck", "set_checked", "set_input_files", "drag_to",
            "dispatch_event", "goto", "reload", "go_back", "go_forward"
        ]
        if name in forbidden:
            def spy_method(*args, **kwargs):
                raise SpyException(f"Forbidden method {name} was called!")
            return spy_method
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

class SpyException(Exception):
    pass

class MockPage:
    def __init__(self, is_closed=False, context="mock_ctx", url="http://test.com"):
        self._is_closed = is_closed
        self.context = context
        self.url = url
        self.main_frame = MockFrame(url=url)

    def is_closed(self):
        return self._is_closed

    def __getattr__(self, name):
        forbidden = [
            "click", "dblclick", "tap", "hover", "focus", "blur", "fill",
            "type", "press", "press_sequentially", "select_option", "check",
            "uncheck", "set_checked", "set_input_files", "drag_to",
            "dispatch_event", "goto", "reload", "go_back", "go_forward"
        ]
        if name in forbidden:
            def spy_method(*args, **kwargs):
                raise SpyException(f"Forbidden method {name} was called!")
            return spy_method
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

@pytest.mark.asyncio
async def test_missing_eligibility_rejected():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage()

    snapshot = await adapter.capture_snapshot(page, {})
    assert snapshot.coverage.terminal_status == "NOT_ELIGIBLE"

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": False})
    assert snapshot.coverage.terminal_status == "NOT_ELIGIBLE"

@pytest.mark.asyncio
async def test_manual_visible_rejected():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage()
    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "manual_visible_page": True})
    assert snapshot.coverage.terminal_status == "MANUAL_PAGE_EXCLUDED"

@pytest.mark.asyncio
async def test_wrong_context_rejected():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage(context="actual_ctx")
    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "expected_ctx"})
    assert snapshot.coverage.terminal_status == "WRONG_CONTEXT"

@pytest.mark.asyncio
async def test_missing_context_rejected():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage()
    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True}) # expected_context is omitted
    assert snapshot.coverage.terminal_status == "WRONG_CONTEXT"

@pytest.mark.asyncio
async def test_closed_target_rejected():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage(is_closed=True)
    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})
    assert snapshot.coverage.terminal_status == "TARGET_CLOSED"

@pytest.mark.asyncio
async def test_eligible_scanned():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage()
    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})
    assert snapshot.coverage.terminal_status == "COMPLETE"
    assert page.main_frame.evaluate_called

@pytest.mark.asyncio
async def test_forbidden_action_spy():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage()

    # We will successfully capture snapshot.
    # The MockPage __getattr__ spy ensures NO forbidden methods are accessed during execution.
    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})
    assert snapshot.coverage.terminal_status == "COMPLETE"

    # Assert spy is working by manually invoking it in test.
    with pytest.raises(SpyException, match="Forbidden method click was called!"):
        page.click()

    with pytest.raises(SpyException, match="Forbidden method fill was called!"):
        page.main_frame.fill()
