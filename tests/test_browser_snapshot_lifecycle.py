import pytest
from app.qa.browser_snapshot_adapter import ReadOnlyBrowserSnapshotAdapter, CancellationToken
from tests.test_browser_snapshot_ownership import MockPage, MockFrame

@pytest.mark.asyncio
async def test_frame_detached_during_scan():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage()
    page.main_frame._is_detached = True

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})
    assert snapshot.coverage.terminal_status == "FRAME_DETACHED"

@pytest.mark.asyncio
async def test_navigation_race_during_evaluate():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage()

    class EvalExc(Exception):
        pass

    page.main_frame.eval_exc = EvalExc("execution context was destroyed")

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})
    assert snapshot.coverage.terminal_status == "NAVIGATION_RACE"

@pytest.mark.asyncio
async def test_cross_origin_frame_limited():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage()

    child = MockFrame(url="http://cross-origin.com", origin="http://cross-origin.com")
    page.main_frame.child_frames.append(child)

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})

    cf = snapshot.main_frame.child_frames[0]
    assert cf.coverage.terminal_status == "CROSS_ORIGIN_LIMITED"
    assert cf.is_same_origin is False
    assert not child.evaluate_called
    assert "CROSS_ORIGIN_LIMITED" in snapshot.coverage.limitations

@pytest.mark.asyncio
async def test_opaque_origin_rejected_without_evaluate():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage(url="about:blank") # opaque origin
    page.main_frame.url = "about:blank"

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})
    assert snapshot.coverage.terminal_status == "OPAQUE_ORIGIN"
    assert not page.main_frame.evaluate_called

@pytest.mark.asyncio
async def test_cancellation_before_scan():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage()

    token = CancellationToken()
    token.cancel() # Cancelled before passing in

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"}, cancellation_token=token)
    assert snapshot.coverage.terminal_status == "CANCELLED"
    assert not page.main_frame.evaluate_called

@pytest.mark.asyncio
async def test_cancellation_during_traversal():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage()

    child1 = MockFrame(url="http://test.com/child1")
    child2 = MockFrame(url="http://test.com/child2")
    page.main_frame.child_frames.extend([child1, child2])

    token = CancellationToken()

    # We want to cancel after child1 finishes its evaluate, but before child2.
    # We can mock child1's evaluate to trigger the token.
    original_eval = child1.evaluate
    async def mock_eval_and_cancel(*args, **kwargs):
        res = await original_eval(*args, **kwargs)
        token.cancel()
        return res

    child1.evaluate = mock_eval_and_cancel

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"}, cancellation_token=token)

    assert snapshot.coverage.terminal_status == "CANCELLED"
    assert child1.evaluate_called is True
    # child2 is never evaluated because loop breaks
    assert child2.evaluate_called is False
