"""Adversarial requests originate only in this isolated local test harness."""
import hashlib
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright
from tests.test_scoped_mutation_adversarial import adv_server, isolated_adv_db
from app.qa.scoped_mutation import (
    AutonomousProfileWorkflowRunner, AuthenticatedIdentityDiscovery,
    ScopedEntityOwnershipRegistry, ScopedMutationNetworkBarrier, OperationType,
    ScopedProfileWorkflowResult, ScopedOwnershipRecord, adapt_scoped_mutation_result_to_test_record,
)


@pytest.mark.parametrize("customer_defect", [False, True])
def test_mutation_tool_failure_does_not_become_customer_finding(customer_defect):
    result = ScopedProfileWorkflowResult(workflow_type="upload", verdict="FAIL",
        entity_id=None, entity_name="controlled-test-upload",
        action_type="upload_harmless_file", details="Controlled failure",
        is_customer_defect=customer_defect)
    record = adapt_scoped_mutation_result_to_test_record(result, 1, "http://127.0.0.1:8080")
    assert record['status'] == ('FAIL' if customer_defect else 'LIMITED COVERAGE')
    assert bool(record['stable_id']) == customer_defect


def multipart(content=b"approved\r\n--not-a-boundary\x00bytes", extra=b""):
    return (b'--BoundaryCase\r\nContent-Disposition: form-data; name="file"; filename="approved.txt"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\n' + content +
            b'\r\n--BoundaryCase' + (b'\r\n' + extra + b'\r\n--BoundaryCase' if extra else b'') + b'--\r\n')


@pytest.mark.asyncio
async def test_real_browser_upload_boundary_preserves_headers_and_permit(adv_server):
    content = b"approved\r\n--not-a-boundary\x00bytes"
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page(extra_http_headers={"X-Unrelated": "preserved"})
            await page.goto(adv_server.origin + "/profile")
            barrier = ScopedMutationNetworkBarrier(adv_server.origin)
            await barrier.install(page)
            permit = barrier.acquire_permit(
                operation_type=OperationType.UPLOAD_ATTACHMENT, workflow_name="profile_attachment",
                allowed_methods=("POST",), endpoint_pattern="/api/attachments",
                verified_account="tester@example.com", account_context="tester@example.com",
                parent_resource_id="exp-owned", upload_destination="/api/attachments",
                approved_file_name="approved.txt", approved_file_hash=hashlib.sha256(content).hexdigest(),
                approved_file_size=len(content), allowed_parts=("file",),
            )
            async def send(body, suffix="?parent_id=exp-owned", parent="exp-owned"):
                return await page.evaluate("""async ({body,suffix,parent}) => {
                    try { const r = await fetch('/api/attachments'+suffix, {method:'POST',
                        headers:{'Content-Type':'multipart/form-data; boundary=BoundaryCase', 'X-Parent-Id':parent},
                        body:new Uint8Array(body)}); return r.status; }
                    catch(e) { return 'blocked'; }
                }""", {"body": list(body), "suffix": suffix, "parent": parent})
            bad = [
                (b"", "?parent_id=exp-owned", "exp-owned"),
                (multipart(b"substituted"), "?parent_id=exp-owned", "exp-owned"),
                (multipart(extra=b'Content-Disposition: form-data; name="extra"; filename="extra.txt"\r\n\r\nx'), "?parent_id=exp-owned", "exp-owned"),
                (multipart(), "?parent_id=exp-other", "exp-owned"),
                (multipart(), "?parent_id=exp-owned", "exp-other"),
                (multipart(), "?parent_id=exp-owned&account_id=other", "exp-owned"),
                (multipart(extra=b'Content-Disposition: form-data; name="account_id"\r\n\r\nother'), "?parent_id=exp-owned", "exp-owned"),
                (multipart()[:-10], "?parent_id=exp-owned", "exp-owned"),
            ]
            for body, suffix, parent in bad:
                before = len(barrier.blocked_requests)
                assert await send(body, suffix, parent) == "blocked"
                assert len(barrier.blocked_requests) == before + 1
                assert permit.use_count == 0 and not permit.consumed
            seen = []
            page.on("request", lambda r: seen.append(r))
            assert isinstance(await send(multipart()), int)
            assert permit.use_count == 1 and permit.consumed
            assert seen[-1].headers["x-unrelated"] == "preserved"
            assert "x-dispatch-token" not in seen[-1].headers
            # Cancellation is checked at the production barrier, even with a permit.
            barrier.is_cancelled = lambda: True
            before = len(barrier.blocked_requests)
            assert await send(multipart()) == "blocked"
            assert len(barrier.blocked_requests) == before + 1
            assert "cancelled" in barrier.blocked_requests[-1]["reason"]
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("readback", ["UNCERTAIN_READBACK", "CONFIRMED_SUCCESS"])
@pytest.mark.parametrize("confirmed", [False, True])
async def test_recovery_requires_authoritative_readback(isolated_adv_db, tmp_path, monkeypatch, readback, confirmed):
    actor = "actor"
    origin = "http://127.0.0.1:8080"
    registry = ScopedEntityOwnershipRegistry()
    rec = registry.record_creation_intent(run_id=1, project_id=1, target_url=origin,
        entity_type="profile_experience", run_prefix="[QA-TEST-1-ABC]", proposed_name="[QA-TEST-1-ABC] Item", account_context=actor)
    rec2 = registry.record_creation_intent(run_id=1, project_id=1, target_url=origin,
        entity_type="profile_experience", run_prefix="[QA-TEST-1-ABC]", proposed_name="[QA-TEST-1-ABC] Second", account_context=actor)
    if confirmed:
        for index, record in enumerate((rec, rec2)):
            registry.confirm_creation(record.id, f"exp-{index}", record.entity_name)
    runner = AutonomousProfileWorkflowRunner(registry=registry, scratch_dir=str(tmp_path))
    monkeypatch.setattr(AuthenticatedIdentityDiscovery, "discover_session_identity", AsyncMock(return_value=actor))
    runner._verify_authenticated_scope_readback = AsyncMock(return_value=(readback, "controlled incomplete readback", {}))
    runner._exercise_validation_rejection = AsyncMock(return_value=None)
    runner._exercise_create_workflow = AsyncMock(return_value=(None, None))
    runner._exercise_upload_workflow = AsyncMock(return_value=None)
    runner._cleanup_run_entities = AsyncMock(return_value=[])
    results = await runner.run_authorized_profile_suite(SimpleNamespace(url=origin), origin, 2, 1)
    stored = registry.get_records_for_run(1)
    assert len(stored) == 2
    assert all(r.remote_cleaned == (confirmed and readback == "CONFIRMED_SUCCESS") for r in stored)
    assert runner._verify_authenticated_scope_readback.await_count == (2 if confirmed else 0)
    if not confirmed or readback == "UNCERTAIN_READBACK":
        runner._exercise_create_workflow.assert_not_awaited()
        assert any(r.action_type == "block_pending_recovery" for r in results)
    else:
        runner._exercise_create_workflow.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_does_not_reconcile_same_actor_other_origin(isolated_adv_db, tmp_path, monkeypatch):
    registry = ScopedEntityOwnershipRegistry()
    rec = registry.record_creation_intent(run_id=1, project_id=1, target_url="http://127.0.0.1:9001",
        entity_type="profile_experience", run_prefix="[QA-TEST-1-ABC]", proposed_name="[QA-TEST-1-ABC] Item", account_context="actor")
    runner = AutonomousProfileWorkflowRunner(registry=registry, scratch_dir=str(tmp_path))
    monkeypatch.setattr(AuthenticatedIdentityDiscovery, "discover_session_identity", AsyncMock(return_value="actor"))
    runner._verify_authenticated_scope_readback = AsyncMock()
    await runner.reconcile_and_recover_pending_entities(SimpleNamespace(url="http://127.0.0.1:9002"), 1, "http://127.0.0.1:9002", "actor")
    runner._verify_authenticated_scope_readback.assert_not_awaited()
    assert not registry.get_records_for_run(1)[0].remote_cleaned


@pytest.mark.asyncio
@pytest.mark.parametrize("complete,content,expected", [
    (False, '<div class="exp-item" data-id="preexisting">Existing</div>', "UNCERTAIN_READBACK"),
    (False, '<p>No records</p>', "UNCERTAIN_READBACK"),
    (True, '<h2>Experience</h2>', "UNCERTAIN_READBACK"),
    (True, '<div class="pagination">Page 1 of 2</div><p>No records</p>', "UNCERTAIN_READBACK"),
    (True, '<div class="exp-item" data-id="preexisting">Existing</div>', "CONFIRMED_SUCCESS"),
])
async def test_live_readback_completeness_contract(adv_server, tmp_path, complete, content, expected):
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page()
            marker = 'data-qa-inventory-complete="true"' if complete else ''
            html = ('<meta name="authenticated-user" content="actor">'
                    f'<section id="inventory" {marker}>{content}</section>')
            await page.route(adv_server.origin + '/contract', lambda route: route.fulfill(body=html, content_type="text/html"))
            await page.goto(adv_server.origin + '/contract')
            runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path))
            status, _, _ = await runner._verify_authenticated_scope_readback(page, "#inventory",
                forbidden_element_text="[QA-TEST-1-ABC] Item", target_entity_id="owned",
                expected_account="actor", intended_origin=adv_server.origin)
            assert status == expected
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_cancellation_during_identity_check_denies_dispatch(monkeypatch):
    cancelled = False
    barrier = ScopedMutationNetworkBarrier("http://127.0.0.1:8080", is_cancelled=lambda: cancelled)
    permit = barrier.acquire_permit(operation_type=OperationType.CREATE_ENTITY,
        workflow_name="profile_experience", allowed_methods=("POST",),
        endpoint_pattern="/api/experience", verified_account="actor")
    async def identity(*args):
        nonlocal cancelled
        cancelled = True
        return "actor"
    monkeypatch.setattr(AuthenticatedIdentityDiscovery, "discover_session_identity", identity)
    req = SimpleNamespace(url="http://127.0.0.1:8080/api/experience", method="POST",
        headers={"content-type": "application/json"}, post_data_buffer=b'{}', post_data=None,
        frame=SimpleNamespace(page=SimpleNamespace(evaluate=AsyncMock())))
    route = SimpleNamespace(request=req, abort=AsyncMock(), continue_=AsyncMock())
    await barrier._handle_route(route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()
    assert permit.use_count == 0
    assert "cancelled during identity" in barrier.blocked_requests[-1]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("account,origin,project", [
    ("other", "http://127.0.0.1:8080", 1),
    ("actor", "http://127.0.0.1:8081", 1),
    ("actor", "http://127.0.0.1:8080", 2),
])
async def test_attachment_sweep_cannot_cross_journal_scope(isolated_adv_db, tmp_path, monkeypatch, account, origin, project):
    registry = ScopedEntityOwnershipRegistry()
    local = tmp_path / "other-scope.txt"
    local.write_text("preserve")
    registry.record_creation_intent(run_id=1, project_id=project, target_url=origin,
        entity_type="profile_attachment", run_prefix="[QA-TEST-1-ABC]", proposed_name="other-scope.txt",
        account_context=account, details={"local_path": str(local)})
    monkeypatch.setattr(AuthenticatedIdentityDiscovery, "discover_session_identity", AsyncMock(return_value="actor"))
    runner = AutonomousProfileWorkflowRunner(registry=registry, scratch_dir=str(tmp_path))
    page = SimpleNamespace(url="http://127.0.0.1:8080")
    assert await runner._cleanup_run_entities(page, 1, 1, target_url=page.url, account_context="actor") == []
    assert local.read_text() == "preserve"
    record = registry.get_records_for_run(1)[0]
    assert not record.local_cleaned and not record.remote_cleaned


@pytest.mark.asyncio
async def test_creation_with_present_dom_but_missing_id_attribute_is_not_customer_defect(adv_server, isolated_adv_db, tmp_path):
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page()
            # DOM contains the company text inside a container that has NO data-id or id attribute
            html = """
            <!DOCTYPE html>
            <html>
            <head><meta name="authenticated-user" content="tester@example.com"></head>
            <body>
                <div id="section-experience-management" data-qa-inventory-complete="true">
                    <button id="btn-add-experience">Add Experience</button>
                    <dialog id="dialog-add-experience">
                        <input id="exp-company" name="company" />
                        <input id="exp-role" name="role" />
                        <textarea id="exp-desc" name="desc"></textarea>
                        <button id="btn-submit-experience">Save</button>
                    </dialog>
                    <div class="uncontracted-list">
                        <!-- Item will appear here without data-id -->
                    </div>
                </div>
                <script>
                    document.getElementById('btn-add-experience').addEventListener('click', () => {
                        document.getElementById('dialog-add-experience').showModal();
                    });
                    document.getElementById('btn-submit-experience').addEventListener('click', (e) => {
                        e.preventDefault();
                        const val = document.getElementById('exp-company').value;
                        const div = document.createElement('div');
                        div.className = 'item-entry';
                        div.textContent = val;
                        document.querySelector('.uncontracted-list').appendChild(div);
                        document.getElementById('dialog-add-experience').close();
                    });
                </script>
            </body>
            </html>
            """
            await page.route(adv_server.origin + "/uncontracted_profile", lambda route: route.fulfill(body=html, content_type="text/html"))
            await page.goto(adv_server.origin + "/uncontracted_profile")

            barrier = ScopedMutationNetworkBarrier(adv_server.origin)
            runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path), barrier=barrier)
            _, result = await runner._exercise_create_workflow(
                page=page, target_url=adv_server.origin + "/uncontracted_profile",
                run_id=88, project_id=1, run_prefix="[QA-TEST-88-EXP]", viewport_name="Desktop",
                account_context="tester@example.com", cancellation_token=None, screenshot_fn=None,
            )

            assert result.verdict == "LIMITED_COVERAGE"
            assert result.is_customer_defect is False
            assert result.defect_category is None
            assert "contract attribute" in result.details

            # Verify no customer defect / finding is created
            record = adapt_scoped_mutation_result_to_test_record(result, 88, adv_server.origin)
            assert record["status"] == "LIMITED COVERAGE"
            assert record["stable_id"] is None
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_delete_runner_exception_does_not_become_customer_defect(adv_server, isolated_adv_db, tmp_path, monkeypatch):
    registry = ScopedEntityOwnershipRegistry()
    runner = AutonomousProfileWorkflowRunner(registry=registry, scratch_dir=str(tmp_path))
    record = registry.record_creation_intent(
        run_id=99, project_id=1, target_url=adv_server.origin,
        entity_type="profile_experience", run_prefix="[QA-TEST-99-EXP]",
        proposed_name="Test Company", account_context="tester@example.com",
    )
    record.remote_entity_id = "exp-99"
    record.status = "CONFIRMED_CREATED"
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page()
            # Simulate unexpected internal Python exception in delete permit/execution
            async def failing_permit(*args, **kwargs):
                raise RuntimeError("Internal runner environment error")
            monkeypatch.setattr(runner, "_permit", failing_permit)

            result = await runner._exercise_delete_workflow(
                page=page, run_id=99, project_id=1, record=record, viewport_name="Desktop"
            )

            assert result.is_customer_defect is False
            # Verify record adaptation yields no customer finding
            test_rec = adapt_scoped_mutation_result_to_test_record(result, 99, adv_server.origin)
            assert test_rec["status"] == "LIMITED COVERAGE"
            assert test_rec["stable_id"] is None
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_server_500_delete_with_actual_backend_removal_is_cleaned_up(adv_server, isolated_adv_db, tmp_path, monkeypatch):
    registry = ScopedEntityOwnershipRegistry()
    runner = AutonomousProfileWorkflowRunner(registry=registry, scratch_dir=str(tmp_path))
    record = registry.record_creation_intent(
        run_id=101, project_id=1, target_url=adv_server.origin,
        entity_type="profile_experience", run_prefix="[QA-TEST-101-EXP]",
        proposed_name="[QA-TEST-101-EXP] Real Removal", account_context="tester@example.com",
    )
    record.remote_entity_id = "exp-101"
    record.status = "CONFIRMED_CREATED"
    registry.confirm_creation(record.id, "exp-101", record.entity_name)

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page()
            # Server returns HTTP 500 on the delete permit request
            class FailingDeletePermit:
                recorded_responses = [{"status": 500, "url": adv_server.origin + "/api/experience/exp-101"}]
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    pass

            monkeypatch.setattr(runner, "_permit", lambda **kwargs: FailingDeletePermit())

            # Read-back verification proves actual server absence: the item was indeed deleted from backend!
            async def fake_readback(*args, **kwargs):
                return "CONFIRMED_SUCCESS", "Confirmed absent from DOM upon reload", {}
            monkeypatch.setattr(runner, "_verify_authenticated_scope_readback", fake_readback)

            # Set up page with container and delete button
            html = f"""
            <!DOCTYPE html>
            <html><head><meta name="authenticated-user" content="tester@example.com"></head>
            <body>
                <div id="section-experience-management" data-qa-inventory-complete="true">
                    <div id="exp-101" class="exp-item">
                        <span>{record.entity_name}</span>
                        <button id="btn-delete-exp-101">Delete</button>
                    </div>
                </div>
            </body></html>
            """
            await page.route(adv_server.origin + "/profile_del500", lambda route: route.fulfill(body=html, content_type="text/html"))
            await page.goto(adv_server.origin + "/profile_del500")

            result = await runner._exercise_delete_workflow(
                page=page, run_id=101, project_id=1, record=record,
                account_context="tester@example.com", viewport_name="Desktop",
            )

            # Factual evidence confirms the item was removed from server, so cleanup is CLEANED_UP!
            assert result.cleanup_status == "CLEANED_UP"
            assert result.defect_category == "SERVER_ERROR_ON_DELETE"
            assert result.is_customer_defect is True
            assert "confirmed deleted from server storage" in result.details

            # Journal is updated to DELETED / CONFIRMED_DELETED
            updated = registry.get_records_for_run(101)[0]
            assert updated.status == "DELETED"
            assert updated.remote_status == "CONFIRMED_DELETED"
            assert updated.remote_cleaned is True
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_recovery_discovers_remote_id_for_orphaned_intent(adv_server, isolated_adv_db, tmp_path):
    registry = ScopedEntityOwnershipRegistry()
    runner = AutonomousProfileWorkflowRunner(registry=registry, scratch_dir=str(tmp_path))
    # Orphan record from a crashed creation whose server returned 500: remote_entity_id was None!
    record = registry.record_creation_intent(
        run_id=102, project_id=1, target_url=adv_server.origin + "/profile",
        entity_type="profile_experience", run_prefix="[QA-TEST-102-EXP]",
        proposed_name="[QA-TEST-102-EXP] Crash Corp", account_context="tester@example.com",
    )
    assert record.remote_entity_id is None

    # The item is actually present in backend store (simulating committed DB write before 500)
    adv_server.experiences["exp-discovered-102"] = {
        "id": "exp-discovered-102",
        "company": record.entity_name,
        "role": "Lead Architect",
        "desc": "Committed before crash",
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page()
            await page.goto(adv_server.origin + "/profile")

            barrier = ScopedMutationNetworkBarrier(adv_server.origin)
            await barrier.install(page)
            runner.barrier = barrier

            # Under B2/B3 safety rules, recovery MUST NOT speculate by DOM/text matching without authoritative server ID.
            # Automated deletion is strictly blocked and entity remains PENDING with LIMITED_COVERAGE.
            results = await runner.reconcile_and_recover_pending_entities(
                page=page, run_id=102, project_id=1, target_url=adv_server.origin + "/profile",
                account_context="tester@example.com",
            )

            assert len(results) == 1
            assert results[0].verdict == "LIMITED_COVERAGE"
            assert results[0].cleanup_status == "PENDING"
            assert results[0].entity_id is None
            assert results[0].is_customer_defect is False

            # Journal record is preserved, NOT deleted, remote ID NOT guessed or adopted
            updated = registry.get_records_for_run(102)[0]
            assert updated.remote_entity_id is None
            assert updated.status != "DELETED"
            assert updated.remote_cleaned is False
            assert "exp-discovered-102" in adv_server.experiences
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_server_500_edit_with_actual_backend_update_is_recorded(adv_server, isolated_adv_db, tmp_path, monkeypatch):
    registry = ScopedEntityOwnershipRegistry()
    runner = AutonomousProfileWorkflowRunner(registry=registry, scratch_dir=str(tmp_path))
    record = registry.record_creation_intent(
        run_id=103, project_id=1, target_url=adv_server.origin,
        entity_type="profile_experience", run_prefix="[QA-TEST-103-EXP]",
        proposed_name="[QA-TEST-103-EXP] Original Name", account_context="tester@example.com",
    )
    record.remote_entity_id = "exp-103"
    record.status = "CONFIRMED_CREATED"
    registry.confirm_creation(record.id, "exp-103", record.entity_name)

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page()
            # Server returns HTTP 500 on the edit permit request (crash-after-commit)
            class FailingEditPermit:
                recorded_responses = [{"status": 500, "url": adv_server.origin + "/api/experience/exp-103"}]
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    pass

            monkeypatch.setattr(runner, "_permit", lambda **kwargs: FailingEditPermit())

            # Read-back verification proves actual server persistence of the updated name!
            async def fake_readback(*args, **kwargs):
                return "CONFIRMED_SUCCESS", "Confirmed updated name in DOM upon reload", {}
            monkeypatch.setattr(runner, "_verify_authenticated_scope_readback", fake_readback)

            # Set up page with container, edit button and dialog
            html = f"""
            <!DOCTYPE html>
            <html><head><meta name="authenticated-user" content="tester@example.com"></head>
            <body>
                <div id="section-experience-management" data-qa-inventory-complete="true">
                    <div id="exp-103" class="exp-item">
                        <span>{record.entity_name}</span>
                        <button id="btn-edit-experience">Edit</button>
                    </div>
                </div>
                <dialog id="dialog-edit-experience" open>
                    <input id="edit-exp-company" value="{record.entity_name}">
                    <button id="btn-save-experience">Save Changes</button>
                </dialog>
            </body></html>
            """
            await page.route(adv_server.origin + "/profile_edit500", lambda route: route.fulfill(body=html, content_type="text/html"))
            await page.goto(adv_server.origin + "/profile_edit500")

            result = await runner._exercise_edit_workflow(
                page=page, target_url=adv_server.origin, run_id=103, project_id=1,
                record=record, viewport_name="Desktop", account_context="tester@example.com",
            )

            # Factual evidence confirms server persisted the updated name despite 500 error!
            assert result.verdict == "FAIL"
            assert result.defect_category == "SERVER_ERROR_ON_EDIT"
            assert result.is_customer_defect is True
            assert "confirmed updated" in result.details

            # Journal is updated to reflect the new name so subsequent delete/recovery can find it!
            updated = registry.get_records_for_run(103)[0]
            assert "(Updated)" in updated.entity_name
            assert updated.entity_name == f"[QA-TEST-103-EXP] Original Name (Updated)"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_server_500_attachment_delete_with_actual_backend_removal_is_cleaned_up(adv_server, isolated_adv_db, tmp_path, monkeypatch):
    registry = ScopedEntityOwnershipRegistry()
    runner = AutonomousProfileWorkflowRunner(registry=registry, scratch_dir=str(tmp_path))

    # Test upload with simulated deletion HTTP 500 where readback confirms absence
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page()

            # Mock upload permit (success) and delete permit (HTTP 500)
            class UploadPermit:
                use_count = 1
                recorded_responses = [{"status": 200}]
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    pass

            class FailingDeletePermit:
                recorded_responses = [{"status": 500}]
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    pass

            def fake_permit(**kwargs):
                op = kwargs.get("operation_type")
                if op == OperationType.DELETE_ATTACHMENT:
                    return FailingDeletePermit()
                return UploadPermit()

            monkeypatch.setattr(runner, "_permit", fake_permit)

            # Mock authenticated readback: upload creation succeeds, attachment deletion confirms absence!
            call_count = 0
            async def fake_readback(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                return "CONFIRMED_SUCCESS", "Confirmed", {}

            monkeypatch.setattr(runner, "_verify_authenticated_scope_readback", fake_readback)

            file_name = "qa_test_upload_run104_fixed.txt"
            file_path = os.path.join(runner.scratch_dir, file_name)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("QA-Tester harmless verification upload test content")
            monkeypatch.setattr(runner.registry, "generate_harmless_file", lambda sdir, rid, ext: (file_path, file_name))

            html = f"""
            <!DOCTYPE html>
            <html><head><meta name="authenticated-user" content="tester@example.com"></head>
            <body>
                <div id="attachment-section" data-section="attachments" data-qa-inventory-complete="true">
                    <h4>Attachment</h4>
                    <input type="file" id="input-attachment" data-qa-upload-destination="/api/profile/attachments/upload">
                    <button id="btn-submit-attachment">Upload</button>
                    <div data-attachment-id="att-500">
                        <span>{file_name}</span>
                        <button id="btn-delete-att-500">Delete</button>
                    </div>
                </div>
            </body></html>
            """
            await page.route(adv_server.origin + "/profile_att500", lambda route: route.fulfill(body=html, content_type="text/html"))
            await page.goto(adv_server.origin + "/profile_att500")

            result = await runner._exercise_upload_workflow(
                page=page, target_url=adv_server.origin + "/profile_att500", run_id=104, project_id=1,
                run_prefix="[QA-TEST-104-ATT]", cancellation_token=None, screenshot_fn=None,
                account_context="tester@example.com", viewport_name="Desktop",
            )

            # Server returned HTTP 500 on delete, but attachment was confirmed removed from server storage!
            assert result.verdict == "FAIL", result.details
            assert result.defect_category == "SERVER_ERROR_ON_DELETE"
            assert result.is_customer_defect is True
            assert result.cleanup_status == "CLEANED_UP"

            # Journal record is marked DELETED
            updated = registry.get_records_for_run(104)[0]
            assert updated.status == "DELETED"
            assert updated.remote_status == "CONFIRMED_DELETED"
            assert updated.remote_cleaned is True
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_server_500_attachment_delete_with_uncertain_readback_is_limited_coverage(adv_server, isolated_adv_db, tmp_path, monkeypatch):
    registry = ScopedEntityOwnershipRegistry()
    runner = AutonomousProfileWorkflowRunner(registry=registry, scratch_dir=str(tmp_path))

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page()

            class FailingDeletePermit:
                use_count = 1
                recorded_responses = [{"status": 500, "url": adv_server.origin + "/api/profile/attachments/att-501"}]
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    pass

            def permit_factory(**kwargs):
                if kwargs.get("operation_type") == OperationType.DELETE_ATTACHMENT:
                    return FailingDeletePermit()
                class MockPermit:
                    use_count = 1
                    recorded_responses = [{"status": 200, "url": adv_server.origin + "/api/profile/attachments/upload"}]
                    async def __aenter__(self):
                        return self
                    async def __aexit__(self, *args):
                        pass
                return MockPermit()

            monkeypatch.setattr(runner, "_permit", permit_factory)

            # Read-back verification: confirmed for creation, UNCERTAIN for deletion
            async def fake_readback(*args, **kwargs):
                if kwargs.get("forbidden_element_text"):
                    return "UNCERTAIN_READBACK", "Container busy with transient reload state", {}
                return "CONFIRMED_SUCCESS", "Attachment confirmed present", {}
            monkeypatch.setattr(runner, "_verify_authenticated_scope_readback", fake_readback)

            file_path = os.path.join(str(tmp_path), "test_doc_501.txt")
            with open(file_path, "w") as f:
                f.write("harmless test content")
            file_name = "test_doc_501.txt"
            monkeypatch.setattr(runner.registry, "generate_harmless_file", lambda sdir, rid, ext: (file_path, file_name))

            html = f"""
            <!DOCTYPE html>
            <html><head><meta name="authenticated-user" content="tester@example.com"></head>
            <body>
                <div id="attachment-section" data-section="attachments" data-qa-inventory-complete="true">
                    <h4>Attachment</h4>
                    <input type="file" id="input-attachment" data-qa-upload-destination="/api/profile/attachments/upload">
                    <button id="btn-submit-attachment">Upload</button>
                    <div data-attachment-id="att-501">
                        <span>{file_name}</span>
                        <button id="btn-delete-att-501">Delete</button>
                    </div>
                </div>
            </body></html>
            """
            await page.route(adv_server.origin + "/profile_att_uncertain", lambda route: route.fulfill(body=html, content_type="text/html"))
            await page.goto(adv_server.origin + "/profile_att_uncertain")

            result = await runner._exercise_upload_workflow(
                page=page, target_url=adv_server.origin + "/profile_att_uncertain", run_id=105, project_id=1,
                run_prefix="[QA-TEST-105-ATT]", cancellation_token=None, screenshot_fn=None,
                account_context="tester@example.com", viewport_name="Desktop",
            )

            # Uncertain readback MUST NEVER be reported as confirmed customer defect!
            assert result.verdict == "LIMITED_COVERAGE", result.details
            assert result.is_customer_defect is False
            assert result.cleanup_status == "PENDING"

            # Journal record remains ambiguous / PENDING for recovery cleanup
            updated = registry.get_records_for_run(105)[0]
            assert updated.status in ("AMBIGUOUS_UNCONFIRMED", "CONFIRMED_CREATED")
            assert updated.remote_cleaned is False
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_server_503_attachment_delete_journal_consistent_with_result(adv_server, isolated_adv_db, tmp_path, monkeypatch):
    registry = ScopedEntityOwnershipRegistry()
    runner = AutonomousProfileWorkflowRunner(registry=registry, scratch_dir=str(tmp_path))

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page()

            class FailingDeletePermit:
                use_count = 1
                recorded_responses = [{"status": 503, "url": adv_server.origin + "/api/profile/attachments/att-503"}]
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    pass

            def permit_factory(**kwargs):
                if kwargs.get("operation_type") == OperationType.DELETE_ATTACHMENT:
                    return FailingDeletePermit()
                class MockPermit:
                    use_count = 1
                    recorded_responses = [{"status": 200, "url": adv_server.origin + "/api/profile/attachments/upload"}]
                    async def __aenter__(self):
                        return self
                    async def __aexit__(self, *args):
                        pass
                return MockPermit()

            monkeypatch.setattr(runner, "_permit", permit_factory)

            # Readback indicates created on step 4, still present on step 5
            async def fake_readback(*args, **kwargs):
                if kwargs.get("forbidden_element_text"):
                    return "CONFIRMED_FAILURE", "Attachment remains present", {}
                return "CONFIRMED_SUCCESS", "Attachment confirmed present", {}
            monkeypatch.setattr(runner, "_verify_authenticated_scope_readback", fake_readback)

            file_path = os.path.join(str(tmp_path), "test_doc_503.txt")
            with open(file_path, "w") as f:
                f.write("harmless test content")
            file_name = "test_doc_503.txt"
            monkeypatch.setattr(runner.registry, "generate_harmless_file", lambda sdir, rid, ext: (file_path, file_name))

            html = f"""
            <!DOCTYPE html>
            <html><head><meta name="authenticated-user" content="tester@example.com"></head>
            <body>
                <div id="attachment-section" data-section="attachments" data-qa-inventory-complete="true">
                    <h4>Attachment</h4>
                    <input type="file" id="input-attachment" data-qa-upload-destination="/api/profile/attachments/upload">
                    <button id="btn-submit-attachment">Upload</button>
                    <div data-attachment-id="att-503">
                        <span>{file_name}</span>
                        <button id="btn-delete-att-503">Delete</button>
                    </div>
                </div>
            </body></html>
            """
            await page.route(adv_server.origin + "/profile_att_503", lambda route: route.fulfill(body=html, content_type="text/html"))
            await page.goto(adv_server.origin + "/profile_att_503")

            result = await runner._exercise_upload_workflow(
                page=page, target_url=adv_server.origin + "/profile_att_503", run_id=106, project_id=1,
                run_prefix="[QA-TEST-106-ATT]", cancellation_token=None, screenshot_fn=None,
                account_context="tester@example.com", viewport_name="Desktop",
            )

            # Transient 503 is LIMITED_COVERAGE and NOT a customer defect
            assert result.verdict == "LIMITED_COVERAGE"
            assert result.is_customer_defect is False
            assert result.cleanup_status == "PENDING"

            # Journal status MUST BE ambiguous / PENDING, NOT CLEANUP_FAILED
            updated = registry.get_records_for_run(106)[0]
            assert updated.status == "AMBIGUOUS_UNCONFIRMED"
            assert "HTTP 503" in (updated.details_json or "")
            assert updated.remote_cleaned is False
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_numeric_resource_id_selectors_valid_in_css(adv_server, isolated_adv_db, tmp_path, monkeypatch):
    registry = ScopedEntityOwnershipRegistry()
    runner = AutonomousProfileWorkflowRunner(registry=registry, scratch_dir=str(tmp_path))

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page()
            # Numeric ID "12" would fail under raw "#12" selector
            numeric_id = "12"
            html = f"""
            <!DOCTYPE html>
            <html><head><meta name="authenticated-user" content="tester@example.com"></head>
            <body>
                <div id="section-experience-management" data-qa-inventory-complete="true">
                    <div id="{numeric_id}" data-id="{numeric_id}" class="exp-item">
                        <span>[QA-TEST-107-NUM] Numeric Corp</span>
                        <button id="btn-delete-{numeric_id}" data-delete="{numeric_id}">Delete</button>
                    </div>
                </div>
            </body></html>
            """
            await page.route(adv_server.origin + "/profile_numeric", lambda route: route.fulfill(body=html, content_type="text/html"))
            await page.goto(adv_server.origin + "/profile_numeric")

            # Absence check with numeric target_entity_id must NOT throw CSS DOMException
            status, reason, _ = await runner._verify_authenticated_scope_readback(
                page=page,
                expected_scope_selector="#section-experience-management",
                forbidden_element_text="NonExistent Corp",
                target_entity_id=numeric_id,
                expected_account="tester@example.com",
            )
            # Item with target_entity_id="12" is present, so absence check correctly returns CONFIRMED_FAILURE
            assert status == "CONFIRMED_FAILURE"
            assert "still present" in reason
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_orphan_intent_safety_and_no_silent_drop(adv_server, isolated_adv_db, tmp_path):
    registry = ScopedEntityOwnershipRegistry()
    runner = AutonomousProfileWorkflowRunner(registry=registry, scratch_dir=str(tmp_path))

    # Add experience to real adv_server endpoint
    adv_server.experiences["exp-generic-1"] = {
        "id": "exp-generic-1",
        "company": "Generic Company",
        "role": "Consultant",
        "desc": "Unprefixed entity",
    }

    # Record intent with generic name lacking [QA-TEST- run prefix!
    record = registry.record_creation_intent(
        run_id=108, project_id=1, target_url=adv_server.origin + "/profile",
        entity_type="profile_experience", run_prefix="",
        proposed_name="Generic Company", account_context="tester@example.com",
    )
    assert record.remote_entity_id is None

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page()
            await page.goto(adv_server.origin + "/profile")

            barrier = ScopedMutationNetworkBarrier(adv_server.origin)
            await barrier.install(page)
            runner.barrier = barrier

            # Name matching alone without verified run prefix MUST NOT delete!
            results = await runner.reconcile_and_recover_pending_entities(
                page=page, run_id=108, project_id=1, target_url=adv_server.origin + "/profile",
                account_context="tester@example.com",
            )

            # Entity must NOT silently disappear from recovery results!
            assert len(results) == 1
            assert results[0].verdict == "LIMITED_COVERAGE"
            assert results[0].cleanup_status == "PENDING"
            assert "lacks confirmed remote ID" in results[0].details

            # Entity in DB was NOT deleted
            updated = registry.get_records_for_run(108)[0]
            assert updated.status != "DELETED"
            assert updated.remote_cleaned is False
            assert "exp-generic-1" in adv_server.experiences
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_cdp_fetch_barrier_enforces_endpoint_and_resource_id_defense_in_depth(adv_server, isolated_adv_db, tmp_path):
    from app.qa.browser_guard import BrowserMutationGuard
    from app.qa.scoped_mutation import OperationPermit, OperationType

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            context = await browser.new_context()
            barrier = ScopedMutationNetworkBarrier(adv_server.origin)
            # Intentionally DO NOT install Playwright context route barrier on page!
            # We want to test that the CDP Fetch barrier independently catches and aborts mismatches!

            permit = OperationPermit(
                permit_id="permit-test-1",
                operation_type=OperationType.DELETE_ENTITY,
                workflow_name="profile_experience",
                allowed_origin=adv_server.origin,
                allowed_methods=("DELETE", "POST"),
                endpoint_pattern=r"^/api/(?:profile/)?experience/exp-target-1/?$",
                resource_id="exp-target-1",
            )
            barrier.active_permit = permit

            guard = BrowserMutationGuard(
                context=context,
                original_pages=[],
                origin=adv_server.origin,
                mutation_barrier=barrier,
            )
            await guard.start()

            page = await context.new_page()
            await page.goto(adv_server.origin + "/profile")

            # Attempt 1: Wrong endpoint pattern /api/other/exp-target-1
            resp1 = await page.evaluate(f"""async () => {{
                try {{
                    const res = await fetch('{adv_server.origin}/api/other/exp-target-1', {{ method: 'DELETE' }});
                    return res.status;
                }} catch (e) {{
                    return 'fetch_failed';
                }}
            }}""")
            # CDP Fetch barrier aborts the request
            assert resp1 == 'fetch_failed'

            # Attempt 2: Wrong resource ID /api/experience/exp-wrong-99
            resp2 = await page.evaluate(f"""async () => {{
                try {{
                    const res = await fetch('{adv_server.origin}/api/experience/exp-wrong-99', {{ method: 'DELETE' }});
                    return res.status;
                }} catch (e) {{
                    return 'fetch_failed';
                }}
            }}""")
            assert resp2 == 'fetch_failed'

            # Barrier records blocked requests from CDP Fetch barrier!
            assert len(barrier.blocked_requests) == 2, barrier.blocked_requests
            for blocked, path in zip(barrier.blocked_requests, ("/api/other/exp-target-1", "/api/experience/exp-wrong-99")):
                assert blocked["url"] == adv_server.origin + path
                assert blocked["method"] == "DELETE"
                assert "rejected mismatched request: Endpoint" in blocked["reason"], blocked
            assert permit.use_count == 0
            assert not permit.consumed

            # Match the endpoint, but conflict with its bound resource in the query.
            # This reaches resource-ID validation rather than the endpoint check.
            resp3 = await page.evaluate("""async () => {
                try {
                    return (await fetch('/api/experience/exp-target-1?id=exp-wrong-99', {method: 'DELETE'})).status;
                } catch (e) { return 'fetch_failed'; }
            }""")
            assert resp3 == 'fetch_failed'
            assert len(barrier.blocked_requests) == 3
            blocked = barrier.blocked_requests[-1]
            assert blocked['url'] == adv_server.origin + '/api/experience/exp-target-1?id=exp-wrong-99'
            assert "Conflicting target resource ID in query parameter" in blocked['reason']
            assert permit.use_count == 0 and not permit.consumed

            await page.close()
            await guard.stop()
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_b1_upload_without_delete_button_reports_limited_coverage_pending(adv_server, isolated_adv_db, tmp_path, monkeypatch):
    """B1 Regression: Upload succeeds, but page DOM has no delete button.

    Must NOT crash with UnboundLocalError!
    Must return verdict="LIMITED_COVERAGE", cleanup_status="PENDING", is_customer_defect=False.
    Journal record must retain remote_cleaned=False and NOT be marked CLEANUP_FAILED.
    """
    registry = ScopedEntityOwnershipRegistry()
    runner = AutonomousProfileWorkflowRunner(registry=registry, scratch_dir=str(tmp_path))

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page()

            class MockPermit:
                use_count = 1
                recorded_responses = [{"status": 200, "url": adv_server.origin + "/api/profile/attachments/upload"}]
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    pass

            def permit_factory(**kwargs):
                return MockPermit()

            monkeypatch.setattr(runner, "_permit", permit_factory)

            # Read-back verification: confirms attachment present upon creation
            async def fake_readback(*args, **kwargs):
                return "CONFIRMED_SUCCESS", "Attachment confirmed present", {}
            monkeypatch.setattr(runner, "_verify_authenticated_scope_readback", fake_readback)

            file_path = os.path.join(str(tmp_path), "test_doc_nodelete.txt")
            with open(file_path, "w") as f:
                f.write("harmless test content")
            file_name = "test_doc_nodelete.txt"
            monkeypatch.setattr(runner.registry, "generate_harmless_file", lambda sdir, rid, ext: (file_path, file_name))

            # HTML with upload form, but NO delete button anywhere in the DOM!
            html = f"""
            <!DOCTYPE html>
            <html><head><meta name="authenticated-user" content="tester@example.com"></head>
            <body>
                <div id="attachment-section" data-section="attachments" data-qa-inventory-complete="true">
                    <h4>Attachment</h4>
                    <input type="file" id="input-attachment" data-qa-upload-destination="/api/profile/attachments/upload">
                    <button id="btn-submit-attachment">Upload</button>
                    <div data-attachment-id="att-readonly-1">
                        <span class="attachment-name">{file_name}</span>
                        <!-- Intentionally NO delete button here! -->
                    </div>
                </div>
            </body></html>
            """
            await page.route(adv_server.origin + "/profile_att_nodelete", lambda route: route.fulfill(body=html, content_type="text/html"))
            await page.goto(adv_server.origin + "/profile_att_nodelete")

            result = await runner._exercise_upload_workflow(
                page=page, target_url=adv_server.origin + "/profile_att_nodelete", run_id=111, project_id=1,
                run_prefix="[QA-TEST-111-ATT]", cancellation_token=None, screenshot_fn=None,
                account_context="tester@example.com", viewport_name="Desktop",
            )

            # Assert clean LIMITED_COVERAGE, no UnboundLocalError, no false defect
            assert result.verdict == "LIMITED_COVERAGE"
            assert result.is_customer_defect is False
            assert result.cleanup_status == "PENDING"
            assert "Remote UI does not provide an actionable attachment delete control" in result.details

            # Journal record in database must NOT be marked CLEANUP_FAILED
            rec = registry.get_records_for_run(111)[0]
            assert rec.remote_cleaned is False
            assert rec.status in ("CREATED", "CONFIRMED_CREATED", "AMBIGUOUS_UNCONFIRMED")
            assert rec.status != "CLEANUP_FAILED"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_b2_b3_orphan_intent_never_discovers_or_deletes_without_server_id(adv_server, isolated_adv_db, tmp_path):
    """B2 & B3 Regression: Orphan creation intent without confirmed remote ID.

    Must NOT speculate by matching DOM text or run prefixes!
    Must NOT adopt or save an unverified remote ID into SQLite!
    Must block automated deletion and leave entity as PENDING.
    """
    registry = ScopedEntityOwnershipRegistry()
    runner = AutonomousProfileWorkflowRunner(registry=registry, scratch_dir=str(tmp_path))

    # Existing resource on server that has a run-prefixed name
    prefixed_name = "[QA-TEST-112] Accidental Pre-existing"
    adv_server.experiences["exp-existing-99"] = {
        "id": "exp-existing-99",
        "company": prefixed_name,
        "role": "Engineer",
        "desc": "Existing item",
    }

    # Record orphan creation intent in registry with NO remote_entity_id (remote_entity_id=None)
    record = registry.record_creation_intent(
        run_id=112, project_id=1, target_url=adv_server.origin + "/profile",
        entity_type="profile_experience", run_prefix="[QA-TEST-112]",
        proposed_name=prefixed_name, account_context="tester@example.com",
    )
    assert record.remote_entity_id is None

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page()
            await page.goto(adv_server.origin + "/profile")

            barrier = ScopedMutationNetworkBarrier(adv_server.origin)
            await barrier.install(page)
            runner.barrier = barrier

            # Recovery sweep should refuse to guess or delete the unconfirmed intent
            results = await runner.reconcile_and_recover_pending_entities(
                page=page, run_id=112, project_id=1, target_url=adv_server.origin + "/profile",
                account_context="tester@example.com",
            )

            assert len(results) == 1
            assert results[0].verdict == "LIMITED_COVERAGE"
            assert results[0].cleanup_status == "PENDING"
            assert results[0].is_customer_defect is False
            assert "lacks confirmed remote ID" in results[0].details

            # B2: Verify NO remote_entity_id was adopted or stored in the database!
            updated = registry.get_records_for_run(112)[0]
            assert updated.remote_entity_id is None
            assert updated.status != "DELETED"
            assert updated.remote_cleaned is False

            # B3: Verify server entity was NOT deleted!
            assert "exp-existing-99" in adv_server.experiences
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_b4_scoped_delete_selector_and_resilient_recovery_sweep(adv_server, isolated_adv_db, tmp_path):
    """B4 Regression: Special characters in name/ID do not break selectors or abort sweep."""
    registry = ScopedEntityOwnershipRegistry()
    runner = AutonomousProfileWorkflowRunner(registry=registry, scratch_dir=str(tmp_path))

    # Record 1 with quotes in name
    name_with_quotes = '[QA-TEST-113] Acme "Gadgets" & \'Widgets\''
    adv_server.experiences["exp-quote-1"] = {
        "id": "exp-quote-1",
        "company": name_with_quotes,
        "role": "Tester",
        "desc": "Quote testing",
    }
    rec1 = registry.record_creation_intent(
        run_id=113, project_id=1, target_url=adv_server.origin + "/profile",
        entity_type="profile_experience", run_prefix="[QA-TEST-113]",
        proposed_name=name_with_quotes, account_context="tester@example.com",
    )
    registry.confirm_creation(rec1.id, "exp-quote-1", name_with_quotes)

    # Record 2: normal record
    name_normal = "[QA-TEST-113] Normal Company"
    adv_server.experiences["exp-norm-2"] = {
        "id": "exp-norm-2",
        "company": name_normal,
        "role": "Tester",
        "desc": "Normal testing",
    }
    rec2 = registry.record_creation_intent(
        run_id=113, project_id=1, target_url=adv_server.origin + "/profile",
        entity_type="profile_experience", run_prefix="[QA-TEST-113]",
        proposed_name=name_normal, account_context="tester@example.com",
    )
    registry.confirm_creation(rec2.id, "exp-norm-2", name_normal)

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page()
            await page.goto(adv_server.origin + "/profile")

            barrier = ScopedMutationNetworkBarrier(adv_server.origin)
            await barrier.install(page)
            runner.barrier = barrier

            # Sweep should process both records without crashing
            results = await runner.reconcile_and_recover_pending_entities(
                page=page, run_id=113, project_id=1, target_url=adv_server.origin + "/profile",
                account_context="tester@example.com",
            )

            # Both records were processed and recovered
            assert len(results) == 2
            assert all(r.verdict == "PASS" for r in results)
            assert all(r.cleanup_status == "CLEANED_UP" for r in results)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_b5_cdp_fetch_blocks_substring_id_unconstrained_endpoint_and_state_reuse(adv_server, isolated_adv_db):
    """B5 Regression: CDP Fetch barrier defense-in-depth enforcement.

    1. Blocks substring resource_id match in body.
    2. Blocks permit lacking endpoint_pattern.
    3. Blocks state reuse (second request under single-use permit).
    """
    from app.qa.browser_guard import BrowserMutationGuard
    from app.qa.scoped_mutation import OperationPermit, OperationType

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            context = await browser.new_context()
            barrier = ScopedMutationNetworkBarrier(adv_server.origin)
            # Intentionally DO NOT install context route barrier - testing CDP Fetch directly!

            guard = BrowserMutationGuard(
                context=context,
                original_pages=[],
                origin=adv_server.origin,
                mutation_barrier=barrier,
            )
            await guard.start()

            page = await context.new_page()
            await page.goto(adv_server.origin + "/profile")

            # Check 1: Substring ID match in body is strictly rejected
            permit1 = OperationPermit(
                permit_id="permit-sub-1",
                operation_type=OperationType.EDIT_ENTITY,
                workflow_name="profile_experience",
                allowed_origin=adv_server.origin,
                allowed_methods=("POST", "PUT"),
                endpoint_pattern=r"^/api/(?:profile/)?experience/12/?$",
                resource_id="12",
                max_uses=1,
            )
            barrier.active_permit = permit1

            # Request sends resource_id="123" in body; "12" is only a substring of "123"!
            resp_sub = await page.evaluate(f"""async () => {{
                try {{
                    const res = await fetch('{adv_server.origin}/api/experience/12', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ id: '123', company: '123 Main St' }})
                    }});
                    return res.status;
                }} catch (e) {{
                    return 'fetch_failed';
                }}
            }}""")
            assert resp_sub == 'fetch_failed'

            # Check 2: Permit lacking endpoint_pattern is rejected
            permit2 = OperationPermit(
                permit_id="permit-noend-2",
                operation_type=OperationType.DELETE_ENTITY,
                workflow_name="profile_experience",
                allowed_origin=adv_server.origin,
                allowed_methods=("DELETE",),
                endpoint_pattern=None,  # Intentionally missing!
                resource_id="exp-1",
                max_uses=1,
            )
            barrier.active_permit = permit2

            resp_noend = await page.evaluate(f"""async () => {{
                try {{
                    const res = await fetch('{adv_server.origin}/api/experience/exp-1', {{ method: 'DELETE' }});
                    return res.status;
                }} catch (e) {{
                    return 'fetch_failed';
                }}
            }}""")
            assert resp_noend == 'fetch_failed'

            # Check 3: State reuse is blocked: second request under single-use permit is aborted
            permit3 = OperationPermit(
                permit_id="permit-reuse-3",
                operation_type=OperationType.DELETE_ENTITY,
                workflow_name="profile_experience",
                allowed_origin=adv_server.origin,
                allowed_methods=("DELETE",),
                endpoint_pattern=r"^/api/(?:profile/)?experience/exp-reuse-1/?$",
                resource_id="exp-reuse-1",
                max_uses=1,
            )
            barrier.active_permit = permit3

            adv_server.experiences["exp-reuse-1"] = {"id": "exp-reuse-1", "company": "Reuse Test"}

            # First request: matches and consumes permit
            resp_first = await page.evaluate(f"""async () => {{
                try {{
                    const res = await fetch('{adv_server.origin}/api/experience/exp-reuse-1', {{ method: 'DELETE' }});
                    return res.status;
                }} catch (e) {{
                    return 'fetch_failed';
                }}
            }}""")
            assert resp_first == 200
            assert permit3.consumed is True

            # Second request: attempting reuse under same permit -> MUST BE ABORTED!
            resp_second = await page.evaluate(f"""async () => {{
                try {{
                    const res = await fetch('{adv_server.origin}/api/experience/exp-reuse-1', {{ method: 'DELETE' }});
                    return res.status;
                }} catch (e) {{
                    return 'fetch_failed';
                }}
            }}""")
            assert resp_second == 'fetch_failed'

            await page.close()
            await guard.stop()
        finally:
            await browser.close()





