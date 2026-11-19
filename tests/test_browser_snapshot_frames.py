import pytest
from app.qa.browser_snapshot_adapter import ReadOnlyBrowserSnapshotAdapter
from tests.test_browser_snapshot_ownership import MockPage, MockFrame

@pytest.mark.asyncio
async def test_semantic_extraction():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage()

    page.main_frame._evaluate_data = {
        "status": "COMPLETE",
        "origin": "http://test.com",
        "controls": [
            {
                "control_id": "btn",
                "raw_role": "button",
                "raw_name": "Submit",
                "raw_visible": True,
                "raw_enabled": False,
                "checked": None,
                "selected": None,
                "expanded": True,
                "href": None,
                "target": None,
                "rel": None,
                "download": None,
                "form_ownership": "form1",
                "semantic_parent_context": None,
                "is_private": False,
                "is_volatile": False
            }
        ]
    }

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})
    c = snapshot.main_frame.controls[0]

    assert c.control_id == "btn"
    assert c.raw_role == "button"
    assert c.raw_name == "Submit"
    assert c.raw_visible is True
    assert c.raw_enabled is False
    assert c.expanded is True
    assert c.form_ownership == "form1"

@pytest.mark.asyncio
async def test_volatility_mapping():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage()

    page.main_frame._evaluate_data = {
        "status": "COMPLETE",
        "origin": "http://test.com",
        "controls": [
            {
                "control_id": "ember123",
                "raw_role": "input",
                "raw_name": "date: 2026-10-09",
                "is_volatile": True
            }
        ]
    }

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})
    c = snapshot.main_frame.controls[0]

    assert c.control_id == "ember123"
    assert c.is_volatile is True
    assert c.raw_name == "date: 2026-10-09"
