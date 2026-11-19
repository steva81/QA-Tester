"""CDP-verified upload through the site's own file input; isolated localhost fixture only.

Covers the bounded supported case (small in-memory text file, declared exact destination)
and the safety boundaries that must keep blocking: disk-backed files whose bytes Chromium
omits, missing destination contract, substituted content and unrelated writes while an
upload permit is active.
"""
import base64
import hashlib
import json
import threading
from contextlib import asynccontextmanager
from email import policy as email_policy
from email.parser import BytesParser

import pytest
from playwright.async_api import async_playwright

from app.paths import chromium_launch_args
from app.qa.browser_guard import BrowserMutationGuard
from app.qa.browser_snapshot_adapter import CancellationToken
from app.qa.scoped_mutation import (
    AuthenticatedIdentityDiscovery, AutonomousProfileWorkflowRunner, OperationType,
    ScopedEntityOwnershipRegistry, ScopedMutationNetworkBarrier,
    adapt_scoped_mutation_result_to_test_record,
)
from tests.profile_test_site import ProfileTestServer
from tests.test_scoped_mutation_adversarial import isolated_adv_db  # noqa: F401

DEST = "/api/profile/attachments/upload"


@pytest.fixture
def profile_server():
    server = ProfileTestServer("127.0.0.1", 0)
    server.upload_destination_contract = True
    server.attachments["att-preexisting"] = {"id": "att-preexisting", "filename": "existing-resume.txt", "content": b"keep"}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@asynccontextmanager
async def guarded_page(server):
    """Production wiring: page-level barrier route plus browser-owned Fetch guard."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=chromium_launch_args(headless=True),
            chromium_sandbox=True,
        )
        try:
            context = await browser.new_context()
            barrier = ScopedMutationNetworkBarrier(server.origin)
            guard = BrowserMutationGuard(context, [], server.origin, mutation_barrier=barrier)
            await guard.start()
            page = await context.new_page()
            await barrier.install(page)
            await page.goto(server.origin + "/profile?mutation=true")
            yield page, barrier, guard
            await page.close()
            await guard.stop()
        finally:
            await browser.close()


def uploads(server):
    return [op for op in server.operations_log if op["op"] == "UPLOAD"]


def file_part(op):
    raw = base64.b64decode(op["body_b64"])
    message = BytesParser(policy=email_policy.default).parsebytes(
        b"Content-Type: " + op["content_type"].encode() + b"\r\n\r\n" + raw)
    files = [part for part in message.iter_parts() if part.get_filename()]
    assert len(files) == 1
    return files[0].get_filename(), files[0].get_payload(decode=True)


def acquire(barrier, name, content, destination=DEST):
    return barrier.acquire_permit(
        operation_type=OperationType.UPLOAD_ATTACHMENT, workflow_name="profile_attachment",
        allowed_methods=("POST",), endpoint_pattern=r"^/api/(?:profile/)?attachments(?:/upload)?/?$",
        upload_destination=destination, approved_file_name=name,
        approved_file_hash=hashlib.sha256(content).hexdigest(), approved_file_size=len(content),
    )


@pytest.mark.asyncio
async def test_positive_upload_exact_bytes_readback_and_verified_delete(profile_server, isolated_adv_db, tmp_path):
    scratch = tmp_path / "scratch"
    async with guarded_page(profile_server) as (page, barrier, guard):
        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(scratch), barrier=barrier)
        result = await runner._exercise_upload_workflow(
            page=page, target_url=profile_server.origin + "/profile?mutation=true", run_id=71,
            project_id=1, run_prefix="[QA-TEST-71-ATT]", viewport_name="Desktop",
            account_context=None, cancellation_token=None, screenshot_fn=None)
    evidence = {"result": result.__dict__, "operations": profile_server.operations_log,
                "blocked": barrier.blocked_requests}
    assert result.verdict == "PASS", json.dumps(evidence, indent=2, default=str)
    assert result.cleanup_status == "CLEANED_UP"

    sent = uploads(profile_server)
    assert len(sent) == 1 and sent[0]["path"] == DEST
    name, content = file_part(sent[0])
    digest = hashlib.sha256(content).hexdigest()
    # Server-received bytes are exactly the approved file reported in the result.
    assert name == result.entity_name and content
    assert f"({len(content)} bytes, sha256 {digest})" in result.details
    assert f"to {DEST};" in result.details
    assert b"QA-Tester harmless verification upload" in content

    # Same exact ID created, read back and deleted; pre-existing data untouched.
    deletes = [op for op in profile_server.operations_log if op["op"] == "DELETE"]
    assert [d["id"] for d in deletes] == [sent[0]["id"]] == [result.entity_id]
    assert list(profile_server.attachments) == ["att-preexisting"]

    records = ScopedEntityOwnershipRegistry.get_records_for_run(71)
    assert len(records) == 1
    rec = records[0]
    assert rec.remote_entity_id == sent[0]["id"]
    assert rec.remote_status == "CONFIRMED_DELETED" and rec.remote_cleaned and rec.local_cleaned
    assert not list(scratch.iterdir())


@pytest.mark.asyncio
async def test_missing_destination_contract_stays_blocked(profile_server, isolated_adv_db, tmp_path, preflight_calls):
    profile_server.upload_destination_contract = False
    async with guarded_page(profile_server) as (page, barrier, guard):
        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path / "s"), barrier=barrier)
        result = await runner._exercise_upload_workflow(
            page=page, target_url=profile_server.origin + "/profile?mutation=true", run_id=72,
            project_id=1, run_prefix="[QA-TEST-72-ATT]", viewport_name="Desktop",
            account_context=None, cancellation_token=None, screenshot_fn=None)
    assert result.verdict == "LIMITED_COVERAGE" and not result.is_customer_defect
    assert result.cleanup_status == "NOT_APPLICABLE"
    assert "Unsupported upload destination" in result.details
    assert not uploads(profile_server)
    assert barrier.active_permit is None
    assert len(profile_server.operations_log) == 0
    assert preflight_calls == dict(file_selection=0, clicks=0, permits=0)
    assert barrier.blocked_requests == []
    rec = ScopedEntityOwnershipRegistry.get_records_for_run(72)[0]
    assert rec.status == "NOT_DISPATCHED" and rec.remote_status == "NOT_CREATED"
    assert rec.local_cleaned


@pytest.mark.asyncio
async def test_disk_backed_file_bytes_omitted_by_cdp_are_blocked(profile_server, tmp_path):
    content = b"disk-backed approved content\n"
    path = tmp_path / "disk.txt"
    path.write_bytes(content)
    async with guarded_page(profile_server) as (page, barrier, guard):
        permit = acquire(barrier, "disk.txt", content)
        await page.locator("#input-experience-file").set_input_files(str(path))
        await page.locator("#btn-upload-file").click()
        await page.wait_for_timeout(800)
        assert permit.use_count == 0 and not permit.consumed
    assert not uploads(profile_server)
    assert any("omitted" in b["reason"] or "unsupported" in b["reason"].lower() for b in barrier.blocked_requests)


@pytest.mark.asyncio
async def test_substituted_memory_content_is_blocked(profile_server, tmp_path):
    async with guarded_page(profile_server) as (page, barrier, guard):
        permit = acquire(barrier, "approved.txt", b"approved content\n")
        await page.locator("#input-experience-file").set_input_files(
            {"name": "approved.txt", "mimeType": "text/plain", "buffer": b"substituted content\n"})
        await page.locator("#btn-upload-file").click()
        await page.wait_for_timeout(800)
        assert permit.use_count == 0
    assert not uploads(profile_server)
    assert any("digest" in b["reason"] or "size" in b["reason"] for b in barrier.blocked_requests)


@pytest.mark.asyncio
async def test_unrelated_write_during_upload_permit_is_blocked(profile_server, tmp_path):
    async with guarded_page(profile_server) as (page, barrier, guard):
        permit = acquire(barrier, "approved.txt", b"approved content\n")
        status = await page.evaluate("""async () => {
            try { const r = await fetch('/api/profile/experience', {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify({company: 'x'})});
                return r.status; } catch (e) { return 'blocked'; }
        }""")
        assert status == "blocked"
        assert permit.use_count == 0
    assert not [op for op in profile_server.operations_log if op["op"] in ("CREATE", "UPLOAD")]


@pytest.mark.asyncio
async def test_upload_cancellation_before_selection(profile_server, isolated_adv_db, tmp_path):
    token = CancellationToken(cancelled=True)
    async with guarded_page(profile_server) as (page, barrier, guard):
        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path / "s"), barrier=barrier)
        result = await runner._exercise_upload_workflow(
            page=page, target_url=profile_server.origin + "/profile?mutation=true", run_id=73,
            project_id=1, run_prefix="[QA-TEST-73-ATT]", viewport_name="Desktop",
            account_context=None, cancellation_token=token, screenshot_fn=None)
    assert result.verdict == "LIMITED_COVERAGE" and not result.is_customer_defect
    assert result.cleanup_status == "NOT_APPLICABLE"
    assert not uploads(profile_server)
    rec = ScopedEntityOwnershipRegistry.get_records_for_run(73)[0]
    assert rec.status == "NOT_DISPATCHED" and rec.remote_status == "NOT_CREATED"
    assert not list((tmp_path / "s").iterdir())


@pytest.mark.asyncio
async def test_upload_cancellation_after_dispatch_preserves_recovery(profile_server, isolated_adv_db, tmp_path):
    class PostDispatchToken:
        def is_cancelled(self):
            return len(uploads(profile_server)) > 0

    async with guarded_page(profile_server) as (page, barrier, guard):
        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path / "s"), barrier=barrier)
        result = await runner._exercise_upload_workflow(
            page=page, target_url=profile_server.origin + "/profile?mutation=true", run_id=74,
            project_id=1, run_prefix="[QA-TEST-74-ATT]", viewport_name="Desktop",
            account_context=None, cancellation_token=PostDispatchToken(), screenshot_fn=None)
    assert result.verdict == "LIMITED_COVERAGE" and not result.is_customer_defect
    assert result.cleanup_status == "PENDING"
    assert result.entity_id.startswith("att-")
    # Creation was sent and read back, but deletion was cancelled
    assert len(uploads(profile_server)) == 1
    assert not [op for op in profile_server.operations_log if op["op"] == "DELETE"]
    rec = ScopedEntityOwnershipRegistry.get_records_for_run(74)[0]
    assert rec.status == "CREATED"
    assert rec.remote_status == "CONFIRMED_EXISTS"
    assert not rec.remote_cleaned
    assert rec.local_cleaned
    assert not list((tmp_path / "s").iterdir())
    assert result.entity_id in profile_server.attachments


@pytest.mark.asyncio
async def test_server_500_during_upload_marks_ambiguous(profile_server, isolated_adv_db, tmp_path):
    profile_server.fail_upload_server = True
    async with guarded_page(profile_server) as (page, barrier, guard):
        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path / "s"), barrier=barrier)
        result = await runner._exercise_upload_workflow(
            page=page, target_url=profile_server.origin + "/profile?mutation=true", run_id=75,
            project_id=1, run_prefix="[QA-TEST-75-ATT]", viewport_name="Desktop",
            account_context=None, cancellation_token=None, screenshot_fn=None)
    assert result.verdict == "FAIL"
    assert result.is_customer_defect is True
    assert result.defect_category == "UPLOAD_ASSOCIATION_FAILED"
    assert result.cleanup_status == "PENDING"
    assert "HTTP 500" in result.details
    rec = ScopedEntityOwnershipRegistry.get_records_for_run(75)[0]
    assert rec.status == "AMBIGUOUS_UNCONFIRMED"
    assert not rec.remote_cleaned
    assert rec.local_cleaned
    assert not list((tmp_path / "s").iterdir())


@pytest.mark.asyncio
async def test_server_500_during_delete_marks_cleanup_pending(profile_server, isolated_adv_db, tmp_path):
    profile_server.fail_delete_attachment_server = True
    async with guarded_page(profile_server) as (page, barrier, guard):
        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path / "s"), barrier=barrier)
        result = await runner._exercise_upload_workflow(
            page=page, target_url=profile_server.origin + "/profile?mutation=true", run_id=76,
            project_id=1, run_prefix="[QA-TEST-76-ATT]", viewport_name="Desktop",
            account_context=None, cancellation_token=None, screenshot_fn=None)
    assert result.verdict == "FAIL"
    assert result.is_customer_defect is True
    assert result.defect_category == "DELETION_EXECUTION_FAILED"
    assert result.cleanup_status == "PENDING"
    assert "HTTP 500" in result.details
    rec = ScopedEntityOwnershipRegistry.get_records_for_run(76)[0]
    assert rec.status == "CLEANUP_FAILED"
    assert not rec.remote_cleaned
    assert rec.local_cleaned
    assert any(k.startswith("att-") and k != "att-preexisting" for k in profile_server.attachments)


@pytest.mark.asyncio
async def test_cold_recovery_reconciles_and_deletes_pending_attachment(profile_server, isolated_adv_db, tmp_path):
    att_id = "att-999"
    att_name = "qa_test_upload_run77_XYZ123.txt"
    profile_server.attachments[att_id] = {"id": att_id, "filename": att_name, "content": b"pending payload"}
    profile_server.fail_delete_attachment_server = False

    async with guarded_page(profile_server) as (page, barrier, guard):
        account = await AuthenticatedIdentityDiscovery.discover_session_identity(page, profile_server.origin)
        assert account

        ScopedEntityOwnershipRegistry.register_entity(
            run_id=77,
            project_id=1,
            target_url=profile_server.origin + "/profile?mutation=true",
            entity_type="profile_attachment",
            entity_id=att_id,
            entity_name=att_name,
            account_context=account,
        )

        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path / "s"), barrier=barrier)
        recovery_results = await runner.reconcile_and_recover_pending_entities(
            page=page,
            project_id=1,
            target_url=profile_server.origin + "/profile?mutation=true",
            account_context=account,
            run_id=77,
        )

    assert len(recovery_results) == 1
    res = recovery_results[0]
    assert res.verdict == "PASS"
    assert res.cleanup_status == "CLEANED_UP"
    assert res.entity_id == att_id

    # Server received DELETE and attachment is removed
    assert att_id not in profile_server.attachments
    assert list(profile_server.attachments) == ["att-preexisting"]

    updated_recs = ScopedEntityOwnershipRegistry.get_records_for_run(77)
    assert updated_recs[0].status == "DELETED"
    assert updated_recs[0].remote_status == "CONFIRMED_DELETED"
    assert updated_recs[0].remote_cleaned is True


@pytest.mark.asyncio
async def test_cold_recovery_confirms_absent_attachment_without_duplicate_delete(profile_server, isolated_adv_db, tmp_path):
    att_name = "qa_test_upload_run78_NEVERCREATED.txt"
    async with guarded_page(profile_server) as (page, barrier, guard):
        account = await AuthenticatedIdentityDiscovery.discover_session_identity(page, profile_server.origin)
        assert account

        att_id = "att-run78-absent"
        ScopedEntityOwnershipRegistry.register_entity(
            run_id=78,
            project_id=1,
            target_url=profile_server.origin + "/profile?mutation=true",
            entity_type="profile_attachment",
            entity_id=att_id,
            entity_name=att_name,
            account_context=account,
        )

        initial_ops = list(profile_server.operations_log)

        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path / "s"), barrier=barrier)
        recovery_results = await runner.reconcile_and_recover_pending_entities(
            page=page,
            project_id=1,
            target_url=profile_server.origin + "/profile?mutation=true",
            account_context=account,
            run_id=78,
        )

    assert len(recovery_results) == 1
    res = recovery_results[0]
    assert res.verdict == "PASS"
    assert res.cleanup_status == "CLEANED_UP"
    assert "confirmed absent" in res.details

    # Zero delete calls dispatched to server because the item was already absent
    new_ops = [op for op in profile_server.operations_log if op not in initial_ops]
    assert not any(op["op"] == "DELETE" for op in new_ops)

    updated_recs = ScopedEntityOwnershipRegistry.get_records_for_run(78)
    assert updated_recs[0].status == "DELETED"
    assert updated_recs[0].remote_status == "CONFIRMED_DELETED"
    assert updated_recs[0].remote_cleaned is True


@pytest.mark.asyncio
async def test_upload_timeout_during_readback_is_limited_coverage(profile_server, isolated_adv_db, tmp_path, monkeypatch):
    async with guarded_page(profile_server) as (page, barrier, guard):
        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path / "s"), barrier=barrier)
        async def fake_readback(*args, **kwargs):
            raise TimeoutError("Simulated readback timeout while reloading page")
        monkeypatch.setattr(runner, "_verify_authenticated_scope_readback", fake_readback)

        result = await runner._exercise_upload_workflow(
            page=page, target_url=profile_server.origin + "/profile?mutation=true", run_id=79,
            project_id=1, run_prefix="[QA-TEST-79-ATT]", viewport_name="Desktop",
            account_context=None, cancellation_token=None, screenshot_fn=None)

    assert result.verdict == "LIMITED_COVERAGE"
    assert result.is_customer_defect is False
    assert result.cleanup_status == "PENDING"
    assert "timeout" in result.details.lower()
    rec = ScopedEntityOwnershipRegistry.get_records_for_run(79)[0]
    assert rec.status == "AMBIGUOUS_UNCONFIRMED"
    assert not rec.remote_cleaned
    assert rec.local_cleaned is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [502, 503, 504, 429])
async def test_server_uncertain_status_codes_during_upload_are_not_customer_defects(profile_server, isolated_adv_db, tmp_path, status_code):
    profile_server.fail_upload_server = True
    profile_server.fail_upload_status_code = status_code
    run_id = 80 + (status_code % 10)
    async with guarded_page(profile_server) as (page, barrier, guard):
        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path / "s"), barrier=barrier)
        result = await runner._exercise_upload_workflow(
            page=page, target_url=profile_server.origin + "/profile?mutation=true", run_id=run_id,
            project_id=1, run_prefix=f"[QA-TEST-{run_id}-ATT]", viewport_name="Desktop",
            account_context=None, cancellation_token=None, screenshot_fn=None)

    assert result.verdict == "LIMITED_COVERAGE"
    assert result.is_customer_defect is False
    assert result.defect_category is None
    assert result.cleanup_status == "PENDING"
    assert f"HTTP {status_code}" in result.details

    # Verify no customer defect / finding record is generated
    record = adapt_scoped_mutation_result_to_test_record(result, run_id, profile_server.origin)
    assert record["status"] == "LIMITED COVERAGE"
    assert record["stable_id"] is None

    rec = ScopedEntityOwnershipRegistry.get_records_for_run(run_id)[0]
    assert rec.status == "AMBIGUOUS_UNCONFIRMED"
    assert not rec.remote_cleaned
    assert rec.local_cleaned is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [502, 503, 504, 429])
async def test_server_uncertain_status_codes_during_delete_are_not_customer_defects(profile_server, isolated_adv_db, tmp_path, status_code):
    profile_server.fail_delete_attachment_server = True
    profile_server.fail_delete_status_code = status_code
    run_id = 90 + (status_code % 10)
    async with guarded_page(profile_server) as (page, barrier, guard):
        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path / "s"), barrier=barrier)
        result = await runner._exercise_upload_workflow(
            page=page, target_url=profile_server.origin + "/profile?mutation=true", run_id=run_id,
            project_id=1, run_prefix=f"[QA-TEST-{run_id}-ATT]", viewport_name="Desktop",
            account_context=None, cancellation_token=None, screenshot_fn=None)

    assert result.verdict == "LIMITED_COVERAGE"
    assert result.is_customer_defect is False
    assert result.defect_category is None
    assert result.cleanup_status == "PENDING"
    assert f"HTTP {status_code}" in result.details

    # Verify no customer defect / finding record is generated
    record = adapt_scoped_mutation_result_to_test_record(result, run_id, profile_server.origin)
    assert record["status"] == "LIMITED COVERAGE"
    assert record["stable_id"] is None


