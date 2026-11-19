import pytest
from app.qa.browser_snapshot_adapter import ReadOnlyBrowserSnapshotAdapter
from tests.test_browser_snapshot_ownership import MockPage

@pytest.mark.asyncio
async def test_privacy_sentinel_masks_private_fields():
    adapter = ReadOnlyBrowserSnapshotAdapter()
    page = MockPage()

    # We simulate what the JS extractor SHOULD return now (privacy applied BEFORE Python)
    page.main_frame._evaluate_data = {
        "status": "COMPLETE",
        "origin": "http://test.com",
        "controls": [
            {
                "control_id": "pw1",
                "raw_role": "input",
                "raw_name": "***", # Must be scrubbed by JS
                "href": "***",
                "is_private": True
            },
            {
                "control_id": "btn",
                "raw_role": "button",
                "raw_name": "PublicBtn",
                "is_private": False
            }
        ]
    }

    # Assert dom_data returned from evaluate does NOT contain the sentinel anywhere
    assert "PRIVATE-PHASE-A2-SENTINEL" not in str(page.main_frame._evaluate_data)

    snapshot = await adapter.capture_snapshot(page, {"qa_owned": True, "expected_context": "mock_ctx"})

    c1, c2 = snapshot.main_frame.controls

    assert c1.is_private is True
    assert c1.raw_name == "***"
    assert c1.href == "***"

    assert c2.is_private is False
    assert c2.raw_name == "PublicBtn"

    rep = repr(snapshot)
    assert "PRIVATE-PHASE-A2-SENTINEL" not in rep
