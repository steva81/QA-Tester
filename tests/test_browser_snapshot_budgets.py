import pytest
from app.qa.browser_snapshot_adapter import ReadOnlyBrowserSnapshotAdapter
from tests.test_browser_snapshot_ownership import MockPage, MockFrame

@pytest.mark.asyncio
async def test_control_budget_enforced_by_script_return():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage()

    page.main_frame._evaluate_data = {
        "status": "LIMITED_CONTROL_BUDGET",
        "origin": "http://test.com",
        "controls": [{"control_id": f"c{i}"} for i in range(500)]
    }

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})
    assert snapshot.coverage.terminal_status == "COMPLETE"
    assert "LIMITED_CONTROL_BUDGET" in snapshot.coverage.limitations
    assert len(snapshot.main_frame.controls) == 500

@pytest.mark.asyncio
async def test_frame_budget_enforced():
    adapter = ReadOnlyBrowserSnapshotAdapter(max_frames=1) # only main frame allowed globally
    page = MockPage()

    child1 = MockFrame()
    child2 = MockFrame()
    page.main_frame.child_frames.extend([child1, child2])

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})
    assert snapshot.coverage.terminal_status == "COMPLETE"
    assert "LIMITED_FRAME_BUDGET" in snapshot.coverage.limitations
    assert len(snapshot.main_frame.child_frames) == 0

@pytest.mark.asyncio
async def test_text_truncation_budget():
    adapter = ReadOnlyBrowserSnapshotAdapter(max_text_length=10)
    page = MockPage()

    long_string = "A" * 50
    page.main_frame._evaluate_data = {
        "status": "COMPLETE",
        "origin": "http://test.com",
        "controls": [
            {
                "raw_name": long_string,
                "href": long_string
            }
        ]
    }
    page.url = "http://test.com/" + long_string
    page.main_frame.url = page.url

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})
    assert snapshot.page_url == ("http://test.com/" + long_string)[:10]
    c = snapshot.main_frame.controls[0]
    assert c.raw_name == long_string[:10]
    assert c.href == long_string[:10]

@pytest.mark.asyncio
async def test_python_side_control_bound_defense():
    adapter = ReadOnlyBrowserSnapshotAdapter(max_controls_per_frame=10)
    page = MockPage()

    page.main_frame._evaluate_data = {
        "status": "COMPLETE",
        "origin": "http://test.com",
        "controls": [{"control_id": f"c{i}"} for i in range(50)] # Mock returns way more than asked
    }

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})
    assert snapshot.coverage.terminal_status == "COMPLETE"
    assert "LIMITED_CONTROL_BUDGET" in snapshot.coverage.limitations
    assert len(snapshot.main_frame.controls) == 10

@pytest.mark.asyncio
async def test_multi_limitation_coexistence_and_determinism():
    adapter = ReadOnlyBrowserSnapshotAdapter(max_controls_per_frame=1)
    page = MockPage()

    page.main_frame._evaluate_data = {
        "status": "LIMITED_CONTROL_BUDGET",
        "origin": "http://test.com",
        "controls": [{"control_id": "c1"}, {"control_id": "c2"}]
    }

    child = MockFrame(url="http://cross-origin.com", origin="http://cross-origin.com")
    page.main_frame.child_frames.append(child)

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})
    assert snapshot.coverage.terminal_status == "COMPLETE"

    assert "LIMITED_CONTROL_BUDGET" in snapshot.coverage.limitations
    assert "CROSS_ORIGIN_LIMITED" in snapshot.coverage.limitations

    # Assert tuple type and ordering
    assert isinstance(snapshot.coverage.limitations, tuple)
    assert snapshot.coverage.limitations == ("CROSS_ORIGIN_LIMITED", "LIMITED_CONTROL_BUDGET")

@pytest.mark.asyncio
async def test_frame_origin_is_explicitly_bounded():
    adapter = ReadOnlyBrowserSnapshotAdapter(max_text_length=15)
    page = MockPage()

    long_origin = "http://thisisaverylongoriginthatshouldbetruncated.com"
    page.main_frame.url = long_origin + "/path"
    page.main_frame._evaluate_data = {
        "status": "COMPLETE",
        "origin": long_origin,
        "controls": []
    }

    # Intentionally add a cross origin frame with long origin
    child_co = MockFrame(url=long_origin + "/child", origin=long_origin)
    page.main_frame.child_frames.append(child_co)

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})

    assert len(snapshot.main_frame.origin) <= 15
    assert len(snapshot.main_frame.child_frames[0].origin) <= 15

