import pytest
import re
from tests.test_scoped_mutation_adversarial import isolated_adv_db  # noqa: F401


def resolve_upload_destination_from_attributes(data_qa_attr, form_action):
    """Mirror the upload destination discovery logic in scoped_mutation.py."""
    upload_destination = (data_qa_attr or "").strip()
    if not upload_destination:
        from app.qa.scoped_mutation import discover_attachment_form_destination
        upload_destination = discover_attachment_form_destination(form_action)
    if not upload_destination or not re.fullmatch(r"/[A-Za-z0-9._~/-]*", upload_destination) or "//" in upload_destination or "/.." in upload_destination:
        upload_destination = None
    return upload_destination


def test_explicit_data_qa_destination_precedence():
    # When data-qa-upload-destination is present, it takes precedence
    dest = resolve_upload_destination_from_attributes("/api/explicit/upload", "/api/form/upload")
    assert dest == "/api/explicit/upload"


def test_form_action_fallback_discovery():
    # When data-qa-upload-destination is absent, form action starting with / is discovered
    dest = resolve_upload_destination_from_attributes("", "/api/profile/attachments")
    assert dest == "/api/profile/attachments"


def test_invalid_and_external_form_actions_rejected():
    # External URL in form action is strictly rejected
    assert resolve_upload_destination_from_attributes("", "https://external-attacker.com/upload") is None
    # Javascript action is strictly rejected
    assert resolve_upload_destination_from_attributes("", "javascript:void(0)") is None
    # Path traversal is strictly rejected
    assert resolve_upload_destination_from_attributes("", "/api/v1/../../etc/passwd") is None
    # Double slash is strictly rejected
    assert resolve_upload_destination_from_attributes("", "//evil.com/upload") is None
    # Sensitive endpoints and arbitrary writes are strictly rejected
    assert resolve_upload_destination_from_attributes("", "/api/users/delete") is None
    assert resolve_upload_destination_from_attributes("", "/admin/purge") is None
    assert resolve_upload_destination_from_attributes("", "/auth/logout") is None
    assert resolve_upload_destination_from_attributes("", "/profile/avatar") is None
    assert resolve_upload_destination_from_attributes("", "/account/change_password") is None
    assert resolve_upload_destination_from_attributes("", "/api/billing/charge") is None
    assert resolve_upload_destination_from_attributes("", "/api/users/create") is None

    # Query, fragment, percent-encoding, trailing slash, resource IDs rejected by discover_attachment_form_destination
    assert resolve_upload_destination_from_attributes("", "/api/profile/attachments?query=1") is None
    assert resolve_upload_destination_from_attributes("", "/api/profile/attachments#sec") is None
    assert resolve_upload_destination_from_attributes("", "/api/profile/attachments%20upload") is None
    assert resolve_upload_destination_from_attributes("", "/api/profile/attachments/") is None
    assert resolve_upload_destination_from_attributes("", "/api/attachments/123") is None
    assert resolve_upload_destination_from_attributes("", "http://127.0.0.1/api/profile/attachments") is None


@pytest.mark.asyncio
async def test_workflow_stops_preflight_on_unapproved_form_action(isolated_adv_db, tmp_path):
    """Real workflow test against loopback server registering all writes proves zero writes occur."""
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from playwright.async_api import async_playwright
    from app.qa.scoped_mutation import AutonomousProfileWorkflowRunner, ScopedMutationNetworkBarrier, ScopedEntityOwnershipRegistry
    from tests.test_scoped_mutation_adversarial import isolated_adv_db  # noqa: F401

    writes = []
    class RecordingHandler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            html = """<!doctype html><html><body>
            <div id="attachment-section">
                <h4>Document Attachments</h4>
                <form id="upload-form" action="/api/billing/charge" method="POST">
                    <input type="file" id="file-input" name="file" />
                    <button type="submit" id="btn-upload-file">Upload</button>
                </form>
            </div></body></html>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def do_POST(self):
            writes.append({"path": self.path, "method": "POST"})
            self.send_response(200)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), RecordingHandler)
    server_url = f"http://127.0.0.1:{server.server_port}"
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(chromium_sandbox=True, )
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(f"{server_url}/")
            barrier = ScopedMutationNetworkBarrier(server_url)
            await barrier.install(page)
            runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path / "scratch"), barrier=barrier)
            res = await runner._exercise_upload_workflow(
                page=page,
                target_url=server_url,
                run_id=99,
                project_id=1,
                run_prefix="[QA-TEST-99]",
                viewport_name="Desktop",
                account_context=None,
                cancellation_token=None,
                screenshot_fn=None,
            )
            assert res.verdict == "LIMITED_COVERAGE"
            assert not res.is_customer_defect
            assert res.cleanup_status == "NOT_APPLICABLE"
            assert "Unsupported upload destination" in res.details
            # Zero writes occurred on server!
            assert len(writes) == 0
            rec = ScopedEntityOwnershipRegistry.get_records_for_run(99)[0]
            assert rec.status == "NOT_DISPATCHED" and rec.remote_status == "NOT_CREATED"
            assert rec.local_cleaned
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()

