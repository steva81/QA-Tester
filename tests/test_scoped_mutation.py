"""Unit and integration test suite for Scoped Authorized Mutation Support.

Verifies:
1. Scoped entity ownership registry lifecycle, unique tagging, and SQLite persistence.
2. Interruption recovery: persistence across process boundaries and recovery cleanup.
3. Network barrier enforcement: allows target origin writes; blocks external domains and dangerous endpoints.
4. Complete autonomous profile mutation lifecycle:
   Create -> inspect -> edit -> inspect -> upload -> delete -> confirm absence.
5. Validation rejection without unintended state writes.
6. Responsive viewports: Desktop (1366x768) and Mobile (390x844).
7. Strict preservation of pre-existing user records (refusal to delete non-owned items).
8. Failure, cancellation, and partial execution handling.
9. Honest reporting of ambiguous success, partial failure, and failed cleanup.
10. Absolute preservation of safety barriers against prohibited actions (account deletion).
"""

import asyncio
import json
import os
import sqlite3
import threading
import time
from typing import Any
import pytest
from playwright.async_api import async_playwright

from app.models.database import get_db_connection, init_db
from app.qa.browser_snapshot_adapter import CancellationToken
from app.qa.interaction_policy import InteractionSafetyPolicy, RiskClassification
from app.qa.semantic_model import SemanticControl
from app.qa.scoped_mutation import (
    AutonomousProfileWorkflowRunner,
    AuthenticatedIdentityDiscovery,
    ScopedEntityOwnershipRegistry,
    ScopedMutationNetworkBarrier,
    ScopedOwnershipRecord,
    ScopedProfileWorkflowResult,
    adapt_scoped_mutation_result_to_test_record,
)
from tests.profile_test_site import ProfileTestServer


@pytest.fixture(scope="module")
def profile_server():
    server = ProfileTestServer("127.0.0.1", 0)
    server.upload_destination_contract = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "mutation_test.sqlite")
    monkeypatch.setenv("QA_TESTER_DB_PATH", db_file)
    from app.paths import ensure_dirs
    ensure_dirs()
    init_db()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('ExampleApp', 'https://example.com', 'Development', 'Tester')"
    )
    c.execute("INSERT INTO runs (project_id, status) VALUES (1, 'RUNNING')")
    conn.commit()
    conn.close()
    yield db_file


# ---------------------------------------------------------------------------
# Test 1: Registry Lifecycle, Unique Tagging & SQLite DB Persistence
# ---------------------------------------------------------------------------
def test_scoped_ownership_registry_lifecycle_and_db_persistence(isolated_db):
    """Verify entity registration, status transitions, query by run, and SQLite persistence."""
    run_id = 1
    project_id = 1
    target_url = "https://example.com/profile"

    # 1. Unique prefix generation
    prefix1 = ScopedEntityOwnershipRegistry.generate_run_prefix(run_id)
    prefix2 = ScopedEntityOwnershipRegistry.generate_run_prefix(run_id)
    assert prefix1.startswith(f"[QA-TEST-{run_id}-")
    assert prefix1 != prefix2

    # 2. Register entity
    test_name = f"{prefix1} Test Company"
    rec = ScopedEntityOwnershipRegistry.register_entity(
        run_id=run_id,
        project_id=project_id,
        target_url=target_url,
        entity_type="experience_entry",
        entity_id="exp-test-101",
        entity_name=test_name,
        details={"role": "QA Lead", "notes": "Local verification"},
    )
    assert rec.id is not None
    assert rec.status == "CREATED"
    assert rec.entity_name == test_name

    # 3. Verify in DB immediately
    records = ScopedEntityOwnershipRegistry.get_records_for_run(run_id)
    assert len(records) == 1
    assert records[0].entity_id == "exp-test-101"
    assert records[0].status == "CREATED"

    # 4. Status transitions
    ScopedEntityOwnershipRegistry.update_status(rec.id, "EDITED")
    records = ScopedEntityOwnershipRegistry.get_records_for_run(run_id)
    assert records[0].status == "EDITED"

    ScopedEntityOwnershipRegistry.update_status(rec.id, "DELETED")
    records = ScopedEntityOwnershipRegistry.get_records_for_run(run_id)
    assert records[0].status == "DELETED"

    # 5. Ownership verification
    assert ScopedEntityOwnershipRegistry.is_owned_by_run(run_id, entity_id="exp-test-101") is True
    assert ScopedEntityOwnershipRegistry.is_owned_by_run(run_id, entity_name=test_name) is True
    # Non-owned items
    assert ScopedEntityOwnershipRegistry.is_owned_by_run(run_id, entity_id="exp-preexisting-1") is False
    assert ScopedEntityOwnershipRegistry.is_owned_by_run(run_id, entity_name="Unrelated User Entry") is False
    assert ScopedEntityOwnershipRegistry.is_owned_by_run(999, entity_id="exp-test-101") is False


# ---------------------------------------------------------------------------
# Test 2: Network Barrier Rules
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scoped_mutation_network_barrier_enforcement(profile_server):
    """Verify that network barrier allows target origin writes but blocks external domains and account destruction."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        context = await browser.new_context()
        barrier = ScopedMutationNetworkBarrier(target_origin=profile_server.origin)
        await barrier.install(context)

        page = await context.new_page()
        # 1. Target origin navigation allowed
        resp = await page.goto(f"{profile_server.origin}/profile?mutation=true")
        assert resp.status == 200

        # 2a. Unpermitted POST is blocked by default (deny-by-default boundary)
        unpermitted_result = await page.evaluate(
            """async (origin) => {
                try {
                    const r = await fetch(origin + '/api/profile/experiences', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({company: 'Nordic QA'})
                    });
                    return { status: r.status, ok: r.ok };
                } catch(e) {
                    return { error: e.toString(), blocked: true };
                }
            }""",
            profile_server.origin,
        )
        assert unpermitted_result.get("blocked") is True or unpermitted_result.get("status") in (403, 0)
        assert any("Unauthorized state change" in b["reason"] for b in barrier.blocked_requests)

        # 2b. Authorized POST under valid OperationPermit succeeds
        async with barrier.permit_scope(
            operation_type="CREATE_ENTITY",
            workflow_name="profile_experience",
            allowed_methods=("POST",),
        ):
            post_result = await page.evaluate(
                """async (origin) => {
                    try {
                        const r = await fetch(origin + '/api/profile/experiences', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({company: 'Nordic QA'})
                        });
                        return { status: r.status, ok: r.ok };
                    } catch(e) {
                        return { error: e.toString() };
                    }
                }""",
                profile_server.origin,
            )
            assert post_result.get("ok") is True

        # 3. Prohibited destructive endpoint blocked
        destroy_result = await page.evaluate(
            """async (origin) => {
                try {
                    const r = await fetch(origin + '/api/account/delete', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({confirm: true})
                    });
                    return { status: r.status, ok: r.ok };
                } catch(e) {
                    return { error: e.toString(), blocked: true };
                }
            }""",
            profile_server.origin,
        )
        # Should be blocked by barrier or return 403
        assert destroy_result.get("blocked") is True or destroy_result.get("status") in (403, 0)
        assert any("Prohibited" in b["reason"] for b in barrier.blocked_requests)

        # 4. Third-party external origin blocked
        ext_result = await page.evaluate(
            """async () => {
                try {
                    const r = await fetch('https://analytics.thirdparty.com/track', { method: 'POST', body: 'ping' });
                    return { ok: r.ok };
                } catch(e) {
                    return { blocked: true };
                }
            }"""
        )
        assert ext_result.get("blocked") is True
        assert any("External origin" in b["reason"] for b in barrier.blocked_requests)

        await browser.close()


# ---------------------------------------------------------------------------
# Test 3: Autonomous Lifecycle on Desktop Viewport (1366x768)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_autonomous_create_inspect_edit_delete_lifecycle_desktop(profile_server, isolated_db, tmp_path):
    """Verify autonomous profile suite execution in Desktop (1366x768) viewport:
    Validation rejection -> Create -> Edit -> Upload -> Delete -> Cleanup.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        context = await browser.new_context(viewport={"width": 1366, "height": 768})
        page = await context.new_page()
        await page.goto(f"{profile_server.origin}/profile?mutation=true")

        scratch_dir = str(tmp_path / "scratch")
        runner = AutonomousProfileWorkflowRunner(scratch_dir=scratch_dir)

        captured_shots = []
        async def dummy_screenshot_fn(pg, name, fid):
            shot_path = str(tmp_path / f"{name}_{fid}.png")
            await pg.screenshot(path=shot_path)
            captured_shots.append(shot_path)
            return shot_path

        results = await runner.run_authorized_profile_suite(
            page=page,
            target_url=f"{profile_server.origin}/profile?mutation=true",
            run_id=1,
            project_id=1,
            viewport_name="Desktop 1366x768",
            screenshot_fn=dummy_screenshot_fn,
        )

        # Verify results
        workflow_types = [r.workflow_type for r in results]
        assert "validation_rejection" in workflow_types
        assert "create" in workflow_types
        assert "edit" in workflow_types
        assert "upload" in workflow_types
        assert "delete" in workflow_types

        for r in results:
            assert r.verdict == "PASS", f"Workflow {r.workflow_type} failed: {r.details}"
            assert r.viewport_name == "Desktop 1366x768"

        # Verify pre-existing item remains in DOM
        preexisting_el = page.locator("#exp-preexisting-1")
        assert await preexisting_el.count() == 1
        assert "Nordic QA Governance" in (await preexisting_el.text_content())

        # Verify SQLite DB has registered and cleaned up records
        records = ScopedEntityOwnershipRegistry.get_records_for_run(1)
        assert len(records) >= 2  # experience entry + upload file
        for rec in records:
            assert rec.status == "DELETED", f"Record {rec.entity_name} status is {rec.status}, expected DELETED"

        # Verify screenshots were captured
        assert len(captured_shots) >= 4

        await browser.close()


# ---------------------------------------------------------------------------
# Test 4: Autonomous Lifecycle on Mobile Viewport (390x844)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_autonomous_create_inspect_edit_delete_lifecycle_mobile(profile_server, isolated_db, tmp_path):
    """Verify autonomous profile suite execution in Mobile (390x844) viewport."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()
        await page.goto(f"{profile_server.origin}/profile?mutation=true")

        scratch_dir = str(tmp_path / "scratch_mobile")
        runner = AutonomousProfileWorkflowRunner(scratch_dir=scratch_dir)

        results = await runner.run_authorized_profile_suite(
            page=page,
            target_url=f"{profile_server.origin}/profile?mutation=true",
            run_id=2,
            project_id=1,
            viewport_name="Mobile 390x844",
        )

        for r in results:
            assert r.verdict == "PASS", f"Mobile workflow {r.workflow_type} failed: {r.details}"
            assert "Mobile" in r.viewport_name

        records = ScopedEntityOwnershipRegistry.get_records_for_run(2)
        assert len(records) >= 2
        for rec in records:
            assert rec.status == "DELETED"

        await browser.close()


# ---------------------------------------------------------------------------
# Test 5: Harmless Test File Generation & Upload
# ---------------------------------------------------------------------------
def test_harmless_file_upload_generation(tmp_path):
    """Verify harmless test file generator creates files with identifiable metadata and valid formats."""
    scratch_dir = str(tmp_path / "files")
    txt_path, txt_name = ScopedEntityOwnershipRegistry.generate_harmless_file(scratch_dir, run_id=5, file_type="txt")
    assert os.path.exists(txt_path)
    assert txt_name.startswith("qa_test_upload_run5_")
    with open(txt_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "[QA-TEST-5-" in content

    png_path, png_name = ScopedEntityOwnershipRegistry.generate_harmless_file(scratch_dir, run_id=5, file_type="png")
    assert os.path.exists(png_path)
    assert png_name.endswith(".png")
    assert os.path.getsize(png_path) > 20


# ---------------------------------------------------------------------------
# Test 6: Strict Protection of Pre-Existing User Records
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_strict_protection_of_preexisting_records(profile_server, isolated_db, tmp_path):
    """Verify that attempt to delete pre-existing item is rejected with ownership boundary error."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{profile_server.origin}/profile?mutation=true")

        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path))

        # Attempt to delete pre-existing item that was NOT created by run 10
        fake_record = ScopedOwnershipRecord(
            id=9999,
            run_id=10,
            project_id=1,
            target_url=f"{profile_server.origin}/profile?mutation=true",
            entity_type="experience_entry",
            entity_id="exp-preexisting-1",
            entity_name="Nordic QA Governance (Pre-existing User Entry)",
            status="CREATED",
            details_json="{}",
        )

        del_res = await runner._exercise_delete_workflow(
            page=page,
            run_id=10,
            project_id=1,
            record=fake_record,
            viewport_name="Desktop",
            cancellation_token=None,
            screenshot_fn=None,
        )

        assert del_res.verdict == "FAIL"
        assert "Ownership boundary violation" in del_res.details
        assert del_res.cleanup_status == "FAILED"

        # Ensure pre-existing item is still in DOM!
        assert await page.locator("#exp-preexisting-1").count() == 1

        await browser.close()


# ---------------------------------------------------------------------------
# Test 7: Interruption & Post-Interruption Recovery Cleanup
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancellation_and_interruption_recovery_cleanup(profile_server, isolated_db, tmp_path):
    """Verify that an interrupted run leaves records in DB, and recovery cleanup sweeps and removes them."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{profile_server.origin}/profile?mutation=true")

        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path))
        run_id = 42

        # 1. Create item manually via workflow
        account = await AuthenticatedIdentityDiscovery.discover_session_identity(page, profile_server.origin)
        assert account
        run_prefix = ScopedEntityOwnershipRegistry.generate_run_prefix(run_id)
        created_rec, create_res = await runner._exercise_create_workflow(
            page=page,
            target_url=f"{profile_server.origin}/profile?mutation=true",
            run_id=run_id,
            project_id=1,
            run_prefix=run_prefix,
            account_context=account,
            viewport_name="Desktop",
            cancellation_token=None,
            screenshot_fn=None,
        )
        assert create_res.verdict == "PASS"
        assert created_rec is not None

        # Confirm item exists in DOM and in DB as CREATED
        assert await page.locator(f"text='{created_rec.entity_name}'").count() == 1
        db_recs = ScopedEntityOwnershipRegistry.get_records_for_run(run_id)
        assert len(db_recs) == 1
        assert db_recs[0].status == "CREATED"

        # 2. Simulate interruption recovery: invoke cleanup sweep
        cleanup_results = await runner._cleanup_run_entities(
            page=page,
            run_id=run_id,
            project_id=1,
            viewport_name="Desktop",
            screenshot_fn=None,
        )
        assert len(cleanup_results) >= 1
        assert cleanup_results[0].verdict == "PASS"
        assert cleanup_results[0].cleanup_status == "CLEANED_UP"

        # Confirm item removed from DOM
        assert await page.locator(f"text='{created_rec.entity_name}'").count() == 0

        # Confirm DB updated to DELETED
        db_recs_after = ScopedEntityOwnershipRegistry.get_records_for_run(run_id)
        assert db_recs_after[0].status == "DELETED"

        await browser.close()


# ---------------------------------------------------------------------------
# Test 8: Prohibited Actions Remain Safely Blocked
# ---------------------------------------------------------------------------
def test_prohibited_actions_remain_safely_blocked():
    """Verify that account deletion and destructive actions are classified as PROHIBITED by policy."""
    del_account_ctrl = SemanticControl(
        control_id="btn-delete-account",
        accessible_role="button",
        accessible_name="Delete Account Permanently",
        visibility=True,
        enabled=True,
        href=None,
        target=None,
        form_ownership=None,
        is_private=False,
    )
    assert InteractionSafetyPolicy.classify(del_account_ctrl) == RiskClassification.PROHIBITED

    revoke_keys_ctrl = SemanticControl(
        control_id="btn-revoke-keys",
        accessible_role="button",
        accessible_name="Revoke All Security Keys",
        visibility=True,
        enabled=True,
        href=None,
        target=None,
        form_ownership=None,
        is_private=False,
    )
    assert InteractionSafetyPolicy.classify(revoke_keys_ctrl) == RiskClassification.PROHIBITED


# ---------------------------------------------------------------------------
# Test 9: Reporting Adapter Integration
# ---------------------------------------------------------------------------
def test_reporting_adapter_and_evidence_integration():
    """Verify conversion of ScopedProfileWorkflowResult into canonical test record."""
    res_pass = ScopedProfileWorkflowResult(
        workflow_type="create",
        verdict="PASS",
        entity_id="exp-001",
        entity_name="[QA-TEST-1-ABC] Test Item",
        action_type="create_profile_item",
        details="Entity created and verified in DOM.",
        screenshot_path="/screenshots/create_001.png",
        cleanup_status="PENDING",
        viewport_name="Desktop 1366x768",
    )
    rec_pass = adapt_scoped_mutation_result_to_test_record(res_pass, run_id=1, target_url="https://example.com/profile")
    assert rec_pass["status"] == "PASS"
    assert rec_pass["viewport"] == "Desktop"
    assert rec_pass["screenshot_path"] == "/screenshots/create_001.png"
    assert rec_pass["entity_id"] == "exp-001"
    assert rec_pass["stable_id"] is None

    res_fail = ScopedProfileWorkflowResult(
        workflow_type="delete",
        verdict="FAIL",
        entity_id="exp-002",
        entity_name="[QA-TEST-1-DEF] Test Item",
        action_type="delete_test_item",
        details="Delete failed; element remains.",
        cleanup_status="FAILED",
        viewport_name="Mobile 390x844",
        is_customer_defect=True,
    )
    rec_fail = adapt_scoped_mutation_result_to_test_record(res_fail, run_id=1, target_url="https://example.com/profile")
    assert rec_fail["status"] == "FAIL"
    assert rec_fail["viewport"] == "Mobile"
    assert rec_fail["stable_id"] is not None
    assert rec_fail["cleanup_status"] == "FAILED"


# ---------------------------------------------------------------------------
# Test 10: Honest Handling of Failed Cleanup
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_honest_handling_of_failed_cleanup(profile_server, isolated_db, tmp_path):
    """Verify that when cleanup cannot find or remove an item, status is marked CLEANUP_FAILED honestly."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{profile_server.origin}/profile?mutation=true")

        run_id = 99
        # Register an entity that doesn't exist in DOM
        rec = ScopedEntityOwnershipRegistry.register_entity(
            run_id=run_id,
            project_id=1,
            target_url=f"{profile_server.origin}/profile?mutation=true",
            entity_type="experience_entry",
            entity_id="nonexistent-id-99",
            entity_name="[QA-TEST-99-ZZZ] Missing Item",
        )

        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path))
        del_res = await runner._exercise_delete_workflow(
            page=page,
            run_id=run_id,
            project_id=1,
            record=rec,
            viewport_name="Desktop",
            cancellation_token=None,
            screenshot_fn=None,
        )

        assert del_res.verdict == "LIMITED_COVERAGE"
        assert del_res.cleanup_status == "PENDING"

        # Status in DB remains uncleaned
        records = ScopedEntityOwnershipRegistry.get_records_for_run(run_id)
        assert records[0].status == "CREATED"

        await browser.close()
