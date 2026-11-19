"""End-to-end automated testing milestone suite for safe profile interactions.

Validates:
1. Candidate discovery discovers tabs, disclosures, and mutating controls.
2. Positive safety policy distinguishes safe tabs/disclosures from mutating actions (Save, Upload, Delete).
3. Safe tabs switch automatically and evaluate to VERIFIED_SUCCESS -> PASS.
4. Safe disclosures expand/collapse and evaluate to VERIFIED_SUCCESS -> PASS.
5. Deliberately broken controls produce confirmed customer defects with evidence:
   - Inert disclosure -> INERT_DISCLOSURE_CONTROL -> FAIL
   - Dangling target -> DANGLING_ARIA_CONTROLS_TARGET -> FAIL
   - Unrevealed content -> UNREVEALED_EXPANDED_CONTENT -> FAIL
6. Mutating buttons produce honest LIMITED COVERAGE records explaining Phase D requirement.
7. Interactive disclosure bridge evaluates supported profile sections automatically without manual tester clicks.
"""

import asyncio
import io
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any
import docx
import pytest
from playwright.async_api import async_playwright

import app.routes.api as api
from server import app as flask_app
from app.models.database import get_db_connection, init_db
from app.qa.interactive import InteractiveSession, sessions
from app.qa.reporting import load_report
from app.qa.report_renderers import render_html, render_markdown, render_docx
from app.qa.browser_snapshot_adapter import ReadOnlyBrowserSnapshotAdapter
from app.qa.browser_snapshot_model import BrowserSnapshot, FrameSnapshot, ControlSnapshot, SnapshotCoverage
from app.qa.candidate_intent_factory import CandidateActionIntentFactory
from app.qa.candidate_intent_graph import CandidateActionIntent
from app.qa.functional_oracle import (
    DefectCategory,
    FunctionalOutcomeVerdict,
    SafeDisclosureFunctionalOracle,
)
from app.qa.functional_reporting_adapter import (
    adapt_functional_result_to_test_record,
    compute_opaque_action_ref,
    persist_functional_evaluation_record,
)
from app.qa.guarded_execution import GuardedActionExecutor, GuardedExecutionStatus
from app.qa.interaction_policy import InteractionSafetyPolicy, RiskClassification
from app.qa.interactive_bridge import InteractiveDisclosureBridge, InteractiveWorkflowConfig
from app.qa.semantic_normalizer import SemanticNormalizer
from app.qa.state_fingerprint import CanonicalStateFingerprint
from app.qa.validation_contract import (
    ValidationVerdict,
    ValidationTestCase,
    ValidationTestCaseResult,
    ValidationContract,
    ValidationContractResult,
    ValidationContractBinding,
    WORK_HISTORY_DATE_CONTRACT,
    find_validation_contract_for_control,
    register_validation_contract,
    register_validation_contract_binding,
    register_profile_fixture_contracts,
    clear_validation_contracts,
)
from tests.profile_test_site import ProfileTestServer, PROFILE_INVENTORY


@pytest.fixture(scope="module")
def profile_server():
    server = ProfileTestServer("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    register_profile_fixture_contracts(server.origin)
    yield server
    clear_validation_contracts()
    server.shutdown()
    server.server_close()


@pytest.fixture
def isolated_profile_state(tmp_path, monkeypatch):
    import app.qa.interactive as interactive
    db_file = str(tmp_path / "profile_test.sqlite")
    monkeypatch.setenv("QA_TESTER_DB_PATH", db_file)
    from app.paths import ensure_dirs
    ensure_dirs()
    init_db()
    monkeypatch.setattr(api, "worker_records", {})
    monkeypatch.setattr(api, "running_engines", {})
    monkeypatch.setattr(api, "active_worker_threads", [])
    monkeypatch.setattr(api, "accepting_runs", True)
    interactive.sessions.clear()
    yield
    for engine in list(api.running_engines.values()):
        try:
            engine.stop()
        except Exception:
            pass
    for worker in list(api.worker_records.values()):
        try:
            worker.cancel.set()
        except Exception:
            pass
    for t in list(api.active_worker_threads):
        if t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
    api.worker_records.clear()
    api.running_engines.clear()
    api.active_worker_threads.clear()
    interactive.sessions.clear()


def _create_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            target_url TEXT NOT NULL,
            environment TEXT NOT NULL,
            tester_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    c.execute(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            status TEXT DEFAULT 'RUNNING',
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_time TIMESTAMP,
            total_tests INTEGER DEFAULT 0,
            FOREIGN KEY (project_id) REFERENCES projects (id)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            expected_result TEXT NOT NULL,
            actual_result TEXT NOT NULL,
            status TEXT NOT NULL,
            viewport TEXT NOT NULL,
            screenshot_path TEXT,
            stable_id TEXT,
            final_url TEXT,
            FOREIGN KEY (run_id) REFERENCES runs (id)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stable_id TEXT NOT NULL,
            project_id INTEGER NOT NULL,
            run_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            platform TEXT,
            area TEXT,
            severity TEXT,
            risk TEXT,
            status TEXT DEFAULT 'OPEN',
            description TEXT,
            impact TEXT,
            recommendation TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            review_status TEXT DEFAULT 'PENDING',
            plain_language_explanation TEXT,
            learning_explanation_sv TEXT,
            how_to_explain_sv TEXT,
            confidence TEXT DEFAULT 'Unreviewed',
            severity_origin TEXT NOT NULL DEFAULT 'legacy',
            risk_origin TEXT NOT NULL DEFAULT 'legacy',
            confidence_origin TEXT NOT NULL DEFAULT 'legacy',
            FOREIGN KEY (project_id) REFERENCES projects (id),
            FOREIGN KEY (run_id) REFERENCES runs (id)
        )
        """
    )
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('ProfileTest', 'http://127.0.0.1/profile', 'Localhost', 'Tester')")
    c.execute("INSERT INTO runs (project_id, status) VALUES (1, 'RUNNING')")
    conn.commit()
    return conn


async def _derive_intent_from_page(page: Any, raw_control_id: str) -> CandidateActionIntent:
    """Helper to derive CandidateActionIntent from active page matching raw_control_id."""
    adapter = ReadOnlyBrowserSnapshotAdapter()
    contract = {
        "qa_owned": True,
        "manual_visible_page": False,
        "expected_context": page.context,
    }
    snapshot = await adapter.capture_snapshot(page, contract)
    assert snapshot.coverage.terminal_status == "COMPLETE"
    normalizer = SemanticNormalizer()
    norm = normalizer.normalize_snapshot(snapshot)
    assert norm.terminal_status == "COMPLETE"
    assert norm.semantic_state is not None

    factory = CandidateActionIntentFactory()
    derivation = factory.derive(norm)

    for intent in derivation.intents:
        if raw_control_id in intent.control_fingerprint:
            return intent
    for control in norm.semantic_state.controls:
        name = getattr(control, "accessible_name", "") or ""
        cid = getattr(control, "control_id", "") or ""
        text = getattr(control, "text_content", "") or ""
        if raw_control_id in name or raw_control_id in cid or raw_control_id in text:
            for intent in derivation.intents:
                if intent.control_fingerprint == control.control_id:
                    return intent

    fp = CanonicalStateFingerprint.generate(norm.semantic_state)
    return CandidateActionIntent(
        parent_state_fingerprint=fp,
        control_fingerprint=raw_control_id,
        expected_action_type="click",
        risk_classification=RiskClassification.SAFE_CANDIDATE,
    )


@pytest.mark.asyncio
async def test_profile_candidate_discovery_and_policy(profile_server):
    """Verify candidate discovery discovers tabs and accordions, and policy blocks mutating actions."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(f"{profile_server.origin}/profile")

        snapshot_adapter = ReadOnlyBrowserSnapshotAdapter()
        normalizer = SemanticNormalizer()
        policy = InteractionSafetyPolicy()

        contract = {
            "qa_owned": True,
            "manual_visible_page": False,
            "expected_context": page.context,
        }
        snapshot = await snapshot_adapter.capture_snapshot(page=page, eligibility_contract=contract)
        norm_res = normalizer.normalize_snapshot(snapshot)

        # Check discovered controls
        controls = norm_res.semantic_state.controls
        control_names = [c.accessible_name.strip() for c in controls if c.accessible_name]

        assert any("Personal Info" in t for t in control_names), "Personal Info tab should be discovered"
        assert any("Security & Privacy" in t for t in control_names), "Security tab should be discovered"
        assert any("Employment Details" in t for t in control_names), "Employment accordion should be discovered"
        assert any("Save Profile Changes" in t for t in control_names), "Save button should be discovered"
        assert any("Delete Account" in t for t in control_names), "Delete button should be discovered"

        # Check policy classification
        save_ctrl = next((c for c in controls if "Save Profile Changes" in (c.accessible_name or "")), None)
        assert save_ctrl is not None
        save_classification = policy.classify(save_ctrl)
        assert save_classification == RiskClassification.POTENTIALLY_MUTATING, f"Save button should be POTENTIALLY_MUTATING, got {save_classification}"

        delete_ctrl = next((c for c in controls if "Delete Account" in (c.accessible_name or "")), None)
        assert delete_ctrl is not None
        delete_classification = policy.classify(delete_ctrl)
        assert delete_classification in (RiskClassification.POTENTIALLY_MUTATING, RiskClassification.PROHIBITED), f"Delete button should be POTENTIALLY_MUTATING or PROHIBITED, got {delete_classification}"

        tab_ctrl = next((c for c in controls if "Security & Privacy" in (c.accessible_name or "")), None)
        assert tab_ctrl is not None
        tab_classification = policy.classify(tab_ctrl)
        assert tab_classification == RiskClassification.SAFE_CANDIDATE, f"Security tab should be SAFE_CANDIDATE, got {tab_classification}"

        await browser.close()


@pytest.mark.asyncio
async def test_profile_safe_tab_switching_verified_success(profile_server):
    """Verify safe tab switching is executed, observed, and evaluated to VERIFIED_SUCCESS -> PASS."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{profile_server.origin}/profile")

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)

        candidate = await _derive_intent_from_page(page, "tab-security")

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=f"{profile_server.origin}/profile",
            candidate_intent=candidate,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.evidence.selected_after is True

        # Test adaptation to canonical record
        test_rec = adapt_functional_result_to_test_record(
            eval_res=eval_res,
            run_id=1,
            target_url=f"{profile_server.origin}/profile",
            enable_customer_defects=True,
        )

        assert test_rec["status"] == "PASS"
        assert "Verified safe tab selection" in test_rec["actual_result"]

        await browser.close()


@pytest.mark.asyncio
async def test_profile_safe_disclosure_expansion_success(profile_server):
    """Verify safe accordion expansion evaluates to VERIFIED_SUCCESS -> PASS."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{profile_server.origin}/profile")

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)

        candidate = await _derive_intent_from_page(page, "acc-employment")

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=f"{profile_server.origin}/profile",
            candidate_intent=candidate,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.evidence.expanded_before is False
        assert eval_res.evidence.expanded_after is True

        test_rec = adapt_functional_result_to_test_record(
            eval_res=eval_res,
            run_id=1,
            target_url=f"{profile_server.origin}/profile",
            enable_customer_defects=True,
        )

        assert test_rec["status"] == "PASS"
        assert "Verified safe disclosure expansion" in test_rec["actual_result"]

        await browser.close()


@pytest.mark.asyncio
async def test_dialog_journey_verified_success(profile_server):
    """Verify safe modal dialog journey evaluates to VERIFIED_SUCCESS -> PASS."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{profile_server.origin}/profile")

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)

        candidate = await _derive_intent_from_page(page, "btn-view-history-dialog")

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=f"{profile_server.origin}/profile",
            candidate_intent=candidate,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert "modal dialog" in (eval_res.evidence.rationale or "").lower()

        test_rec = adapt_functional_result_to_test_record(
            eval_res=eval_res,
            run_id=1,
            target_url=f"{profile_server.origin}/profile",
            enable_customer_defects=True,
        )

        assert test_rec["status"] == "PASS"
        assert "Verified modal dialog opened" in test_rec["actual_result"]

        await browser.close()


@pytest.mark.asyncio
async def test_work_history_date_validation_verified_success(profile_server):
    """Verify client-side local date constraint validation evaluates to VERIFIED_SUCCESS -> PASS."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{profile_server.origin}/profile")

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)

        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=f"{profile_server.origin}/profile",
            candidate_intent=candidate,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert "validation" in (eval_res.evidence.rationale or "").lower()

        test_rec = adapt_functional_result_to_test_record(
            eval_res=eval_res,
            run_id=1,
            target_url=f"{profile_server.origin}/profile",
            enable_customer_defects=True,
        )

        assert test_rec["status"] == "PASS"
        assert "Verified local input validation" in test_rec["actual_result"]

        await browser.close()


@pytest.mark.asyncio
async def test_dialog_dismissal_journey_verified_success(profile_server):
    """Verify modal dialog opening followed by nested close button dismissal evaluates to VERIFIED_SUCCESS -> PASS."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{profile_server.origin}/profile")

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)

        opener_intent = await _derive_intent_from_page(page, "btn-view-history-dialog")
        eval_opener = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=f"{profile_server.origin}/profile",
            candidate_intent=opener_intent,
        )
        assert eval_opener.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        opened_dialog_fp = eval_opener.diagnostics.get("revealed_state_fingerprint") or eval_opener.evidence.post_state_fingerprint

        close_intent = CandidateActionIntent(
            parent_state_fingerprint=opened_dialog_fp,
            control_fingerprint="0:btn-close-history-dialog",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(opener_intent,),
        )

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=f"{profile_server.origin}/profile",
            candidate_intent=close_intent,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert "dismissed" in (eval_res.evidence.rationale or "").lower()

        test_rec = adapt_functional_result_to_test_record(
            eval_res=eval_res,
            run_id=1,
            target_url=f"{profile_server.origin}/profile",
            enable_customer_defects=True,
        )
        assert test_rec["status"] == "PASS"
        assert "Verified modal dialog dismissed" in test_rec["actual_result"]

        await browser.close()


@pytest.mark.asyncio
async def test_menu_journey_verified_success_and_defect_attribution(profile_server):
    """Verify menu opener and nested menu items: working action (PASS), broken target (FAIL), inert item (LIMITED COVERAGE)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{profile_server.origin}/profile")

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)

        menu_opener = await _derive_intent_from_page(page, "btn-actions-menu")
        eval_opener = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=f"{profile_server.origin}/profile",
            candidate_intent=menu_opener,
        )
        assert eval_opener.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        opened_menu_fp = eval_opener.diagnostics.get("revealed_state_fingerprint") or eval_opener.evidence.post_state_fingerprint

        audit_intent = CandidateActionIntent(
            parent_state_fingerprint=opened_menu_fp,
            control_fingerprint="0:menu-item-audit",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(menu_opener,),
        )
        eval_audit = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=f"{profile_server.origin}/profile",
            candidate_intent=audit_intent,
        )
        assert eval_audit.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS

        broken_intent = CandidateActionIntent(
            parent_state_fingerprint=opened_menu_fp,
            control_fingerprint="0:menu-item-broken",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(menu_opener,),
        )
        eval_broken = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=f"{profile_server.origin}/profile",
            candidate_intent=broken_intent,
        )
        assert eval_broken.verdict == FunctionalOutcomeVerdict.SUPPORTED_FAILURE
        assert eval_broken.defect_category == DefectCategory.DANGLING_ARIA_CONTROLS

        inert_intent = CandidateActionIntent(
            parent_state_fingerprint=opened_menu_fp,
            control_fingerprint="0:menu-item-inert",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(menu_opener,),
        )
        eval_inert = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=f"{profile_server.origin}/profile",
            candidate_intent=inert_intent,
        )
        assert eval_inert.verdict == FunctionalOutcomeVerdict.LIMITED_COVERAGE

        await browser.close()


def test_domain_validation_contract_six_evaluation_cases():
    """Verify all 6 domain validation contract evaluation outcomes."""
    contract = WORK_HISTORY_DATE_CONTRACT
    assert len(contract.test_cases) == 2
    case_valid = contract.test_cases[0]
    case_invalid = contract.test_cases[1]

    # Case 1: Valid interval with expected feedback -> SATISFIED
    res1 = contract.evaluate_case(case_valid, feedback_visible=True, feedback_text="Dates are valid.")
    assert res1.passed is True

    # Case 2: Invalid interval with expected error constraint feedback -> SATISFIED
    res2 = contract.evaluate_case(case_invalid, feedback_visible=True, feedback_text="End date cannot be earlier than start date.")
    assert res2.passed is True

    # Overall contract result with satisfied cases
    all_res = contract.evaluate_observations([
        (case_valid, True, "Dates are valid."),
        (case_invalid, True, "End date cannot be earlier than start date."),
    ])
    assert all_res.verdict == ValidationVerdict.SATISFIED
    assert all_res.all_passed is True
    assert all_res.is_customer_defect is False

    # Case 3: Broken validator accepting invalid interval
    defect_acc = contract.evaluate_observations([
        (case_valid, True, "Dates are valid."),
        (case_invalid, True, "Dates are valid."),
    ])
    assert defect_acc.verdict == ValidationVerdict.DEFECT_ACCEPTED_INVALID
    assert defect_acc.all_passed is False
    assert defect_acc.is_customer_defect is True
    assert "accepted invalid input" in defect_acc.defect_description.lower()

    # Case 4: Broken validator rejecting valid interval
    defect_rej = contract.evaluate_observations([
        (case_valid, True, "End date cannot be earlier than start date."),
        (case_invalid, True, "End date cannot be earlier than start date."),
    ])
    assert defect_rej.verdict == ValidationVerdict.DEFECT_REJECTED_VALID
    assert defect_rej.all_passed is False
    assert defect_rej.is_customer_defect is True
    assert "incorrectly rejected valid input" in defect_rej.defect_description.lower()

    # Case 5: Ambiguous / irrelevant feedback
    amb_res = contract.evaluate_observations([
        (case_valid, True, "Server connection timeout error 504"),
        (case_invalid, True, "End date cannot be earlier than start date."),
    ])
    assert amb_res.verdict == ValidationVerdict.AMBIGUOUS_FEEDBACK
    assert amb_res.all_passed is False
    assert amb_res.is_customer_defect is False

    # Case 6: Missing contract lookup returns None
    missing = find_validation_contract_for_control("btn-uncontracted-button")
    assert missing is None


@pytest.mark.asyncio
async def test_browser_validation_variant_accept_invalid(profile_server):
    """Blocker 5: Browser execution of broken validator incorrectly accepting invalid input.
    Tests classification, persistence, finding creation, and defect-classification toggle."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        target_url = f"{profile_server.origin}/profile-validation-variant?variant=accept_invalid"
        await page.goto(target_url)

        executor = GuardedActionExecutor()
        oracle_enabled = SafeDisclosureFunctionalOracle(enable_defect_classification=True)

        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        # 1. Defect classification enabled -> SUPPORTED_FAILURE with customer defect
        eval_res = await oracle_enabled.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
        )
        assert eval_res.verdict == FunctionalOutcomeVerdict.SUPPORTED_FAILURE
        assert eval_res.is_customer_defect is True
        assert eval_res.defect_category == DefectCategory.VALIDATION_LOGIC_ERROR
        assert "accepted invalid input" in (eval_res.evidence.rationale or "").lower()

        # Test persistence
        conn = _create_test_db()
        test_id = persist_functional_evaluation_record(
            conn=conn,
            eval_res=eval_res,
            run_id=1,
            target_url=target_url,
            enable_customer_defects=True,
        )
        c = conn.cursor()
        c.execute("SELECT status, actual_result, stable_id FROM tests WHERE id = ?", (test_id,))
        row = c.fetchone()
        assert row["status"] == "FAIL"
        assert "validation_logic_error" in row["actual_result"].lower()

        c.execute("SELECT stable_id, severity, status FROM findings WHERE run_id = 1")
        findings = c.fetchall()
        assert len(findings) == 1
        assert findings[0]["stable_id"] == row["stable_id"]

        # 2. Defect classification disabled -> LIMITED_COVERAGE with zero customer defects (Blocker 3)
        oracle_disabled = SafeDisclosureFunctionalOracle(enable_defect_classification=False)
        eval_disabled = await oracle_disabled.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
        )
        assert eval_disabled.verdict == FunctionalOutcomeVerdict.LIMITED_COVERAGE
        assert eval_disabled.is_customer_defect is False

        # Persistence with defect classification disabled must create ZERO findings
        conn_disabled = _create_test_db()
        test_id_d = persist_functional_evaluation_record(
            conn=conn_disabled,
            eval_res=eval_disabled,
            run_id=1,
            target_url=target_url,
            enable_customer_defects=False,
        )
        c_d = conn_disabled.cursor()
        c_d.execute("SELECT status FROM tests WHERE id = ?", (test_id_d,))
        assert c_d.fetchone()["status"] == "LIMITED COVERAGE"
        c_d.execute("SELECT count(*) as cnt FROM findings WHERE run_id = 1")
        assert c_d.fetchone()["cnt"] == 0

        await browser.close()


@pytest.mark.asyncio
async def test_browser_validation_variant_reject_valid(profile_server):
    """Blocker 5: Browser execution of broken validator incorrectly rejecting valid input."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        target_url = f"{profile_server.origin}/profile-validation-variant?variant=reject_valid"
        await page.goto(target_url)

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)

        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
        )
        assert eval_res.verdict == FunctionalOutcomeVerdict.SUPPORTED_FAILURE
        assert eval_res.is_customer_defect is True
        assert eval_res.defect_category == DefectCategory.VALIDATION_LOGIC_ERROR
        assert "rejected valid input" in (eval_res.evidence.rationale or "").lower()

        await browser.close()


@pytest.mark.asyncio
async def test_browser_validation_variant_delayed_feedback(profile_server):
    """Blocker 5: Browser execution of validator with delayed feedback (polling freshness)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        target_url = f"{profile_server.origin}/profile-validation-variant?variant=delayed_feedback"
        await page.goto(target_url)

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)

        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
        )
        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert "Work History Date Interval" in (eval_res.evidence.rationale or "")

        await browser.close()


@pytest.mark.asyncio
async def test_browser_validation_variant_missing_and_ambiguous_fields(profile_server):
    """Blocker 2 & 5: Guarded execution halts on missing or ambiguous input fields (prerequisite failure)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)

        # 1. Missing field
        page_missing = await context.new_page()
        url_missing = f"{profile_server.origin}/profile-validation-variant?variant=missing_field"
        await page_missing.goto(url_missing)
        cand_missing = await _derive_intent_from_page(page_missing, "btn-validate-history")

        eval_missing = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=url_missing,
            candidate_intent=cand_missing,
        )
        assert eval_missing.execution_status == GuardedExecutionStatus.ACTION_FAILED
        assert "missing required input field" in (eval_missing.diagnostics.get("execution_error") or "").lower()
        assert eval_missing.is_customer_defect is False
        assert eval_missing.verdict in (FunctionalOutcomeVerdict.EXECUTION_CONTROL_EVENT, FunctionalOutcomeVerdict.LIMITED_COVERAGE)

        # 2. Ambiguous field
        page_ambig = await context.new_page()
        url_ambig = f"{profile_server.origin}/profile-validation-variant?variant=ambiguous_field"
        await page_ambig.goto(url_ambig)
        cand_ambig = await _derive_intent_from_page(page_ambig, "btn-validate-history")

        eval_ambig = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=url_ambig,
            candidate_intent=cand_ambig,
        )
        assert eval_ambig.execution_status == GuardedExecutionStatus.ACTION_FAILED
        assert "ambiguous input field" in (eval_ambig.diagnostics.get("execution_error") or "").lower()
        assert eval_ambig.is_customer_defect is False

        await browser.close()


@pytest.mark.asyncio
async def test_browser_validation_cancellation_and_blocked_network(profile_server):
    """Blocker 2, 3 & 5: Cancellation halts cleanly, and blocked network activity evaluates to LIMITED_COVERAGE."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)

        # 1. Cancellation check
        page_c = await context.new_page()
        url_c = f"{profile_server.origin}/profile"
        await page_c.goto(url_c)
        cand_c = await _derive_intent_from_page(page_c, "btn-validate-history")

        from app.qa.browser_snapshot_adapter import CancellationToken
        token = CancellationToken()
        token.cancel()

        eval_cancelled = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=url_c,
            candidate_intent=cand_c,
            cancellation_token=token,
        )
        assert eval_cancelled.execution_status == GuardedExecutionStatus.CANCELLED
        assert eval_cancelled.verdict == FunctionalOutcomeVerdict.LIMITED_COVERAGE
        assert eval_cancelled.is_customer_defect is False

        # 2. Blocked request during validation: must NOT evaluate to PASS or customer defect
        page_net = await context.new_page()
        url_net = f"{profile_server.origin}/profile-validation-variant?variant=blocked_network"
        await page_net.goto(url_net)
        cand_net = await _derive_intent_from_page(page_net, "btn-validate-history")

        eval_net = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=url_net,
            candidate_intent=cand_net,
        )
        assert eval_net.verdict == FunctionalOutcomeVerdict.LIMITED_COVERAGE
        assert eval_net.is_customer_defect is False
        assert any("network" in str(lim) for lim in eval_net.limitations) or eval_net.diagnostics.get("qa_network_interference")

        await browser.close()


@pytest.mark.asyncio
async def test_browser_validation_neutral_id_and_target_isolation(profile_server):
    """Blocker 1 & 5: Neutral-ID bindings match by selector/name, while unrelated targets remain isolated."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        # 1. Neutral-ID binding on /profile-varied
        page_varied = await context.new_page()
        url_varied = f"{profile_server.origin}/profile-varied"
        await page_varied.goto(url_varied)

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)

        cand_varied = await _derive_intent_from_page(page_varied, "Validate Period Dates")

        eval_varied = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=url_varied,
            candidate_intent=cand_varied,
        )
        assert eval_varied.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_varied.is_customer_defect is False

        # 2. Strict target isolation from unrelated target
        unrelated_url = f"{profile_server.origin}/profile-unrelated"
        contract_unrelated = find_validation_contract_for_control(
            control_id="btn-validate-history",
            accessible_name="Unrelated Button",
            target_url=unrelated_url,
        )
        assert contract_unrelated is None, "Contract hijacked unrelated target!"

        # External domain isolation
        external_url = "https://external-example.com/profile"
        contract_ext = find_validation_contract_for_control(
            control_id="btn-validate-history",
            accessible_name="Validate Work History Dates",
            target_url=external_url,
        )
        assert contract_ext is None, "Contract hijacked external domain!"

        await browser.close()


@pytest.mark.asyncio
async def test_profile_broken_control_and_inert_timeout_differentiation(profile_server):
    """Verify broken control (dangling aria-controls) is confirmed as a customer defect,
    while an inert unchanged control is honestly reported as LIMITED COVERAGE without false defect promotion."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{profile_server.origin}/profile")

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)

        # 1. Broken control with positive evidence of DOM failure: dangling target
        candidate_dangling = await _derive_intent_from_page(page, "btn-dangling")
        eval_dangling = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=f"{profile_server.origin}/profile",
            candidate_intent=candidate_dangling,
        )

        assert eval_dangling.verdict == FunctionalOutcomeVerdict.SUPPORTED_FAILURE
        assert eval_dangling.is_customer_defect is True
        assert eval_dangling.defect_category == DefectCategory.DANGLING_ARIA_CONTROLS

        # Test persistence into database for confirmed defect
        conn = _create_test_db()
        test_id = persist_functional_evaluation_record(
            conn=conn,
            eval_res=eval_dangling,
            run_id=1,
            target_url=f"{profile_server.origin}/profile",
            enable_customer_defects=True,
        )

        c = conn.cursor()
        c.execute("SELECT status, actual_result, stable_id FROM tests WHERE id = ?", (test_id,))
        row = c.fetchone()
        assert row["status"] == "FAIL"
        assert "functional defect confirmed (dangling_aria_controls)" in row["actual_result"].lower()
        assert row["stable_id"] is not None

        c.execute("SELECT stable_id, severity, risk, status, review_status FROM findings WHERE run_id = 1")
        findings = c.fetchall()
        assert len(findings) == 1
        assert findings[0]["stable_id"] == row["stable_id"]
        assert findings[0]["review_status"] == "PENDING"

        # 2. Inert unchanged control: Must NOT be falsely converted to a customer defect
        candidate_inert = await _derive_intent_from_page(page, "btn-inert")
        eval_inert = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=f"{profile_server.origin}/profile",
            candidate_intent=candidate_inert,
        )

        assert eval_inert.verdict == FunctionalOutcomeVerdict.LIMITED_COVERAGE
        assert eval_inert.is_customer_defect is False

        await browser.close()


@pytest.mark.asyncio
async def test_profile_end_to_end_interactive_bridge_evaluation(profile_server):
    """Verify bridge reaches supported profile sections and reports honest categories."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{profile_server.origin}/profile")

        config = InteractiveWorkflowConfig(
            enabled=True,
            max_evaluations_per_page=6,
            max_evaluations_per_run=10,
        )
        bridge = InteractiveDisclosureBridge(config=config)
        recs = await bridge.evaluate_page_disclosures(
            source_context=context,
            source_page=page,
            page_url=f"{profile_server.origin}/profile",
            viewport_name="Desktop",
            run_id=1,
        )

        assert len(recs) > 0, "Bridge should have evaluated interactive candidates"

        statuses = [r["status"] for r in recs]
        # Must have verified working PASS (e.g. tab-security, acc-employment)
        assert "PASS" in statuses, f"Expected PASS in statuses: {statuses}"
        # Must have LIMITED COVERAGE for mutating buttons (Save, Upload, Delete)
        assert "LIMITED COVERAGE" in statuses, f"Expected LIMITED COVERAGE in statuses: {statuses}"

        # Verify mutating button record explains Phase D
        mutating_recs = [r for r in recs if "Phase D permit" in r.get("actual_result", "")]
        assert len(mutating_recs) >= 1, "Expected honest limited coverage record explaining Phase D permit"
        assert "Phase D permit" in mutating_recs[0]["actual_result"]

        await browser.close()


def verify_profile_inventory_acceptance(
    tests: list[dict],
    findings: list[dict],
    inventory: list[dict],
    target_url: str,
    viewport: str = "Desktop",
) -> dict[str, dict]:
    """Verify exact 1-to-1 bijective correspondence between expected inventory controls and test records.

    Asserts:
    1. Exact count correspondence without omission or surplus.
    2. Privacy-safe opaque action tokens [action: act-xxxxxxxx] on every interactive record.
    3. Zero unexpected duplicate records per action token.
    4. Set equality between expected inventory action tokens and recorded test tokens.
    5. Exact 1-to-1 correspondence with expected verdict and honest defect attribution.
    6. Exact finding association for broken items with zero false customer defects.
    """
    desktop_interactive = [
        t for t in tests
        if t["viewport"] == viewport and t["expected_result"] != "Page loads successfully"
    ]
    expected_action_types = {
        "tab-personal": "click",
        "tab-security": "click",
        "tab-preferences": "click",
        "acc-employment": "click",
        "acc-2fa": "click",
        "acc-delayed": "click",
        "btn-view-history-dialog": "click",
        "btn-close-history-dialog": "click",
        "btn-validate-history": "click",
        "btn-actions-menu": "click",
        "menu-item-audit": "click",
        "menu-item-broken": "click",
        "menu-item-inert": "click",
        "btn-dangling": "click",
        "btn-unrevealed": "click",
        "btn-crash": "click",
        "btn-inert": "click",
        "btn-save": "click",
        "btn-upload": "click",
        "btn-delete": "click",
        "btn-delete-keys": "click",
        "pref-username": "unsupported",
        "pref-notes": "unsupported",
        "sec-phone": "unsupported",
        "input-start-date": "unsupported",
        "input-end-date": "unsupported",
    }

    expected_tokens = {}
    for item in inventory:
        raw_id = item["inventory_id"]
        action_type = expected_action_types.get(raw_id, "click")
        token = compute_opaque_action_ref(
            action_ref=f"0:{raw_id}:{action_type}",
            page_url=target_url,
            viewport=viewport,
        )
        expected_tokens[raw_id] = token

    record_by_token = {}
    token_pattern = re.compile(r"\[action:\s*(act-[a-f0-9]{8})\]")
    for rec in desktop_interactive:
        match = token_pattern.search(rec["url"])
        assert match is not None, f"Interactive record {rec['id']} missing privacy-safe action token: {rec['url']}"
        tok = match.group(1)
        assert tok not in record_by_token, f"Unexpected duplicate test record for action token {tok}"
        record_by_token[tok] = rec

    token_to_id = {v: k for k, v in expected_tokens.items()}
    missing_tokens = set(expected_tokens.values()) - set(record_by_token.keys())
    unexpected_tokens = set(record_by_token.keys()) - set(expected_tokens.values())
    missing_ids = [token_to_id[t] for t in missing_tokens if t in token_to_id]

    assert len(desktop_interactive) == len(inventory), (
        f"Expected {len(inventory)} {viewport} interactive test records, got {len(desktop_interactive)}. "
        f"Missing items: {missing_ids}, Unexpected: {unexpected_tokens}"
    )
    assert not missing_tokens and not unexpected_tokens, (
        f"Inventory set mismatch: missing={missing_tokens}, unexpected={unexpected_tokens}"
    )

    matched = {}
    for item in inventory:
        raw_id = item["inventory_id"]
        name = item["name"]
        expected_verdict = item["expected_verdict"]
        defect_cat = item.get("expected_defect_category")
        token = expected_tokens[raw_id]
        rec = record_by_token[token]
        matched[raw_id] = rec

        assert rec["status"] == expected_verdict, (
            f"Item '{name}' ({raw_id}) expected {expected_verdict}, got {rec['status']} "
            f"with actual_result: {rec['actual_result']}"
        )

        if defect_cat:
            assert rec["stable_id"] is not None, f"Expected stable_id for broken item {name}"
            matching_finding = next((f for f in findings if f["stable_id"] == rec["stable_id"]), None)
            assert matching_finding is not None, (
                f"Expected finding for broken item {name} with stable_id {rec['stable_id']}"
            )
            assert defect_cat.lower() in rec["actual_result"].lower()
            assert defect_cat.lower() in matching_finding["title"].lower() or defect_cat.lower() in matching_finding["description"].lower()
        else:
            assert rec["stable_id"] is None, f"Item '{name}' ({raw_id}) should not produce a defect finding"

    return matched


def test_integrated_profile_run_entrypoint_to_reporting(profile_server, isolated_profile_state):
    """Verify full end-to-end profile automation through real application entrypoint,
    worker, engine, persistence, findings, and canonical report export.

    Asserts:
    - All 16 declared inventory controls reached and accounted for.
    - 5 working safe controls evaluated to PASS.
    - 3 confirmed defects evaluated to FAIL with linked findings.
    - 8 non-mutating / unsupported / inert / initial / revealed controls evaluated to LIMITED COVERAGE.
    - Zero false customer defects.
    - Source session and server state remained completely unmutated.
    - Evidence survives persistence, export and reopening.
    - Multi-format canonical reports (HTML, MD, Word DOCX) render cleanly.
    """
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Profile Journey', ?, 'Localhost', 'Tester')",
        (f"{profile_server.origin}/profile",)
    )
    pid = c.lastrowid
    conn.commit()
    conn.close()

    class ManagedInteractiveSession(InteractiveSession):
        async def _init_playwright(self):
            self.loop_errors = []
            self.loop.set_exception_handler(lambda loop, error: self.loop_errors.append(error))
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True)
            self.context = await self.browser.new_context(service_workers="block")
            self.page = await self.context.new_page()
            await self.page.goto(f"{profile_server.origin}/profile")

        def stop(self, timeout=5):
            super().stop(timeout)
            if hasattr(self, "thread") and self.thread:
                self.thread.join(timeout=3)

    session = ManagedInteractiveSession(pid, f"{profile_server.origin}/profile")
    session.start()
    sessions[str(pid)] = session

    try:
        t0 = time.time()
        while not session.is_ready():
            if time.time() - t0 > 15:
                raise TimeoutError("Interactive session failed to become READY.")
            time.sleep(0.1)

        client = flask_app.test_client()
        res = client.post("/api/runs", json={
            "project_id": pid,
            "mode": "full",
            "page_limit": 5,
            "enable_interactive_disclosure": True,
            "interactive_options": {
                "max_evaluations_per_page": 20,
                "max_evaluations_per_run": 50,
                "action_timeout": 1.0,
                "reconstruction_timeout": 5.0,
            },
        })
        assert res.status_code == 200, f"Failed to start run: {res.get_json()}"
        rid = res.get_json()["run_id"]

        worker = api.worker_records.get(rid)
        if worker and worker.thread:
            worker.thread.join(timeout=60)

        conn = get_db_connection()
        c = conn.cursor()
        run = c.execute("SELECT status FROM runs WHERE id = ?", (rid,)).fetchone()
        assert run["status"] == "COMPLETED"

        tests = [dict(r) for r in c.execute("SELECT * FROM tests WHERE run_id = ?", (rid,)).fetchall()]
        findings = [dict(r) for r in c.execute("SELECT * FROM findings WHERE run_id = ?", (rid,)).fetchall()]
        conn.close()

        # 1. Assert exact 1-to-1 bijective correspondence between expected inventory controls and records
        matched_records = verify_profile_inventory_acceptance(
            tests=tests,
            findings=findings,
            inventory=PROFILE_INVENTORY,
            target_url=f"{profile_server.origin}/profile",
            viewport="Desktop",
        )
        assert len(matched_records) == len(PROFILE_INVENTORY)

        matched_mobile = verify_profile_inventory_acceptance(
            tests=tests,
            findings=findings,
            inventory=PROFILE_INVENTORY,
            target_url=f"{profile_server.origin}/profile",
            viewport="Mobile",
        )
        assert len(matched_mobile) == len(PROFILE_INVENTORY)

        # 2. Assert findings table has exactly the 4 confirmed defects per viewport (8 total, 4 distinct categories)
        assert len(findings) == 8, f"Expected exactly 8 confirmed customer defects (4 per viewport), got {len(findings)}"
        finding_titles = [f["title"] for f in findings]
        assert any("nonexistent-target-element" in t for t in finding_titles), "Dangling target defect missing"
        assert any("panel-unrevealed" in t for t in finding_titles), "Unrevealed content defect missing"
        assert any("TypeError" in t for t in finding_titles), "Script error defect missing"
        assert any("nonexistent-export-target" in t for t in finding_titles), "Broken export options defect missing"
        for f in findings:
            assert f["review_status"] == "PENDING"
            assert f["status"] == "OPEN"
            assert f["stable_id"].startswith("QA-FUNC-")

        # 3. Assert source session was not mutated
        assert session.is_ready()
        async def check_source_unmutated():
            is_personal_sel = await session.page.locator("#tab-personal").get_attribute("aria-selected")
            is_emp_hidden = await session.page.locator("#panel-employment").get_attribute("hidden")
            username_val = await session.page.locator("#pref-username").input_value()
            return is_personal_sel, is_emp_hidden, username_val

        is_sel, is_hidden, u_val = asyncio.run_coroutine_threadsafe(check_source_unmutated(), session.loop).result(timeout=5)
        assert is_sel == "true", f"Source session tab should remain 'Personal Info', got {is_sel}"
        assert is_hidden is not None, "Source session employment panel should remain hidden"
        assert u_val == "Tester", f"Source session username should remain 'Tester', got {u_val}"

        # 4. Canonical report generation and multi-format exports
        conn = get_db_connection()
        report = load_report(conn, rid)
        conn.close()

        html_out = render_html(report)
        assert "QA Test Report" in html_out
        assert "Profile Journey" in html_out

        md_out = render_markdown(report)
        assert "# QA Test Report" in md_out

        doc = render_docx(report)
        assert len(doc.paragraphs) > 0
        docx_path = Path("scratch/profile_test_report.docx")
        docx_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(docx_path))
        assert docx_path.exists()
        assert docx_path.stat().st_size > 1000

        # Read back saved docx to verify readability, headings, tables
        read_doc = docx.Document(str(docx_path))
        assert any("QA Test Report" in p.text for p in read_doc.paragraphs[:5])

        # 5. Persistence reopening verification: fresh DB connection reads all records
        fresh_conn = get_db_connection()
        c = fresh_conn.cursor()
        saved_count = c.execute("SELECT COUNT(*) FROM tests WHERE run_id = ?", (rid,)).fetchone()[0]
        saved_findings = c.execute("SELECT COUNT(*) FROM findings WHERE run_id = ?", (rid,)).fetchone()[0]
        fresh_conn.close()
        assert saved_count == len(tests)
        assert saved_findings == len(findings)

    finally:
        session.stop()


def test_integrated_profile_cancellation_preserves_evidence(profile_server, isolated_profile_state):
    """Verify cancellation cleanly stops worker, preserves completed records in DB,
    marks run ABORTED, and teardown finishes safely without corrupting session."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Cancel Profile', ?, 'Localhost', 'Tester')",
        (f"{profile_server.origin}/profile",)
    )
    pid = c.lastrowid
    conn.commit()
    conn.close()

    class ManagedInteractiveSession(InteractiveSession):
        async def _init_playwright(self):
            self.loop_errors = []
            self.loop.set_exception_handler(lambda loop, error: self.loop_errors.append(error))
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True)
            self.context = await self.browser.new_context(service_workers="block")
            self.page = await self.context.new_page()
            await self.page.goto(f"{profile_server.origin}/profile")

        def stop(self, timeout=5):
            super().stop(timeout)
            if hasattr(self, "thread") and self.thread:
                self.thread.join(timeout=3)

    session = ManagedInteractiveSession(pid, f"{profile_server.origin}/profile")
    session.start()
    sessions[str(pid)] = session
    try:
        t0 = time.time()
        while not session.is_ready():
            if time.time() - t0 > 15:
                raise TimeoutError("Interactive session failed to become READY.")
            time.sleep(0.1)

        client = flask_app.test_client()
        res = client.post("/api/runs", json={
            "project_id": pid,
            "mode": "full",
            "page_limit": 5,
            "enable_interactive_disclosure": True,
            "interactive_options": {
                "max_evaluations_per_page": 20,
                "max_evaluations_per_run": 50,
                "action_timeout": 1.0,
                "reconstruction_timeout": 5.0,
            },
        })
        assert res.status_code == 200
        rid = res.get_json()["run_id"]

        time.sleep(0.5)
        stop_res = client.post(f"/api/runs/{rid}/stop")
        assert stop_res.status_code == 200

        worker = api.worker_records.get(rid)
        if worker and worker.thread:
            worker.thread.join(timeout=30)

        conn = get_db_connection()
        c = conn.cursor()
        run = c.execute("SELECT status FROM runs WHERE id = ?", (rid,)).fetchone()
        assert run["status"] == "ABORTED"
        test_count = c.execute("SELECT COUNT(*) FROM tests WHERE run_id = ?", (rid,)).fetchone()[0]
        assert test_count >= 1, "Completed evidence prior to stop must be preserved"
        conn.close()
    finally:
        session.stop()


def test_profile_inventory_omitted_revealed_control_fails_acceptance(profile_server):
    """Negative control: Omitting a required revealed control (e.g. acc-2fa) causes 1-to-1 acceptance to fail."""
    target_url = f"{profile_server.origin}/profile"

    expected_action_types = {
        "tab-personal": "click",
        "tab-security": "click",
        "tab-preferences": "click",
        "acc-employment": "click",
        "acc-2fa": "click",
        "acc-delayed": "click",
        "btn-view-history-dialog": "click",
        "btn-close-history-dialog": "click",
        "btn-validate-history": "click",
        "btn-actions-menu": "click",
        "menu-item-audit": "click",
        "menu-item-broken": "click",
        "menu-item-inert": "click",
        "btn-dangling": "click",
        "btn-unrevealed": "click",
        "btn-crash": "click",
        "btn-inert": "click",
        "btn-save": "click",
        "btn-upload": "click",
        "btn-delete": "click",
        "btn-delete-keys": "click",
        "pref-username": "unsupported",
        "pref-notes": "unsupported",
        "sec-phone": "unsupported",
        "input-start-date": "unsupported",
        "input-end-date": "unsupported",
    }

    dummy_tests = []
    dummy_findings = []
    for item in PROFILE_INVENTORY:
        raw_id = item["inventory_id"]
        if raw_id == "acc-2fa":
            # Deliberately omit the required revealed control
            continue

        action_type = expected_action_types[raw_id]
        tok = compute_opaque_action_ref(action_ref=f"0:{raw_id}:{action_type}", page_url=target_url, viewport="Desktop")
        status = item["expected_verdict"]
        stable_id = None
        if item.get("expected_defect_category"):
            stable_id = f"QA-FUNC-{raw_id}"
            dummy_findings.append({
                "stable_id": stable_id,
                "title": f"Defect: {item['expected_defect_category']}",
                "description": f"Details for {raw_id}",
            })

        dummy_tests.append({
            "id": len(dummy_tests) + 1,
            "viewport": "Desktop",
            "url": f"http://127.0.0.1/ [action: {tok}]",
            "expected_result": "Interactive test",
            "actual_result": f"Result for {raw_id} ({item.get('expected_defect_category', '')})",
            "status": status,
            "stable_id": stable_id,
        })

    # Case 1: Count mismatch (25 of 26) fails immediately with missing item
    with pytest.raises(AssertionError, match=r"Expected 26 Desktop interactive test records, got 25"):
        verify_profile_inventory_acceptance(
            dummy_tests, dummy_findings, PROFILE_INVENTORY, target_url, "Desktop"
        )

    # Case 2: Count matches (16 records), but acc-2fa is missing and replaced by an unexpected token
    fake_tok = compute_opaque_action_ref(action_ref="0:extra-item:click", page_url=target_url, viewport="Desktop")
    dummy_tests.append({
        "id": 999,
        "viewport": "Desktop",
        "url": f"http://127.0.0.1/ [action: {fake_tok}]",
        "expected_result": "Interactive test",
        "actual_result": "Extraneous control",
        "status": "PASS",
        "stable_id": None,
    })
    with pytest.raises(AssertionError, match=r"Inventory set mismatch: missing="):
        verify_profile_inventory_acceptance(
            dummy_tests, dummy_findings, PROFILE_INVENTORY, target_url, "Desktop"
        )


def test_profile_inventory_variations_labels_ordering_structure():
    """Verify application discovery does NOT depend on fixture IDs, selectors, or URLs.

    Exercises SemanticNormalizer and CandidateActionIntentFactory on varied markup:
    - Localized/arbitrary labels ('Kontoinställningar', 'Sekretess & Inloggning', 'Visa organisationsdata')
    - Non-fixture IDs ('c-9182', 'dyn-uuid-391', 'custom-panel-a')
    - Varied DOM ordering (panels placed before tabs, disclosures nested in arbitrary containers)
    - Varied structural tags (div role='tab', div role='button' with aria-expanded)
    - Arbitrary URL ('https://internal.corp.local/account-center/v2')
    """
    raw_controls = (
        # 1. Structural variation: Panel preceded tabs in DOM, containing a custom disclosure button
        ControlSnapshot(
            control_id="dyn-uuid-mfa",
            raw_role="button",
            raw_name="Aktivera MFA",
            raw_visible=True,
            raw_enabled=True,
            checked=None,
            selected=None,
            expanded=False,
            href=None,
            target=None,
            rel=None,
            download_metadata=None,
            form_ownership=None,
            semantic_parent_context="section",
            is_private=False,
            is_volatile=False,
        ),
        # 2. Mutating button in another panel
        ControlSnapshot(
            control_id="dyn-uuid-save",
            raw_role="button",
            raw_name="Save Changes (Spara ändringar)",
            raw_visible=True,
            raw_enabled=True,
            checked=None,
            selected=None,
            expanded=None,
            href=None,
            target=None,
            rel=None,
            download_metadata=None,
            form_ownership=None,
            semantic_parent_context="section",
            is_private=False,
            is_volatile=False,
        ),
        # 3. Prohibited destructive button
        ControlSnapshot(
            control_id="dyn-uuid-delete",
            raw_role="button",
            raw_name="Delete Account Permanently (Radera konto)",
            raw_visible=True,
            raw_enabled=True,
            checked=None,
            selected=None,
            expanded=None,
            href=None,
            target=None,
            rel=None,
            download_metadata=None,
            form_ownership=None,
            semantic_parent_context="section",
            is_private=False,
            is_volatile=False,
        ),
        # 4. Another disclosure with Swedish label and non-standard ID
        ControlSnapshot(
            control_id="dyn-uuid-details",
            raw_role="button",
            raw_name="Visa organisationsdata",
            raw_visible=True,
            raw_enabled=True,
            checked=None,
            selected=None,
            expanded=False,
            href=None,
            target=None,
            rel=None,
            download_metadata=None,
            form_ownership=None,
            semantic_parent_context="section",
            is_private=False,
            is_volatile=False,
        ),
        # 5. Varied tabs at bottom of page with arbitrary IDs ('c-9182', 'c-9183')
        ControlSnapshot(
            control_id="c-9182",
            raw_role="tab",
            raw_name="Allmän Profil",
            raw_visible=True,
            raw_enabled=True,
            checked=None,
            selected=True,
            expanded=None,
            href=None,
            target=None,
            rel=None,
            download_metadata=None,
            form_ownership=None,
            semantic_parent_context="nav",
            is_private=False,
            is_volatile=False,
        ),
        ControlSnapshot(
            control_id="c-9183",
            raw_role="tab",
            raw_name="Sekretess & Inloggning",
            raw_visible=True,
            raw_enabled=True,
            checked=None,
            selected=False,
            expanded=None,
            href=None,
            target=None,
            rel=None,
            download_metadata=None,
            form_ownership=None,
            semantic_parent_context="nav",
            is_private=False,
            is_volatile=False,
        ),
    )

    frame = FrameSnapshot(
        frame_id="main",
        url="https://internal.corp.local/account-center/v2",
        origin="https://internal.corp.local",
        name="main",
        is_same_origin=True,
        controls=raw_controls,
        child_frames=(),
        coverage=SnapshotCoverage(terminal_status="COMPLETE", limitations=()),
    )
    snapshot = BrowserSnapshot(
        page_url="https://internal.corp.local/account-center/v2",
        main_frame=frame,
        coverage=SnapshotCoverage(terminal_status="COMPLETE", limitations=()),
    )

    normalizer = SemanticNormalizer()
    norm_res = normalizer.normalize_snapshot(snapshot)
    assert norm_res.terminal_status == "COMPLETE"
    assert len(norm_res.semantic_state.controls) == 6

    intent_factory = CandidateActionIntentFactory()
    derivation = intent_factory.derive_intents(norm_res)
    assert derivation.terminal_status == "COMPLETE"

    # Verify discovery does not depend on fixture IDs or fixture URLs
    fingerprints = {i.control_fingerprint for i in derivation.intents}
    assert "0:c-9182" in fingerprints
    assert "0:c-9183" in fingerprints
    assert "0:dyn-uuid-mfa" in fingerprints
    assert "0:dyn-uuid-details" in fingerprints
    assert "0:dyn-uuid-save" in fingerprints

    # Verify destructive button is excluded from candidate intents (PROHIBITED)
    assert not any("dyn-uuid-delete" in fp for fp in fingerprints)

    # Verify mutating button has POTENTIALLY_MUTATING classification
    mut = next(i for i in derivation.intents if "dyn-uuid-save" in i.control_fingerprint)
    assert mut.risk_classification == RiskClassification.POTENTIALLY_MUTATING

    # Verify tabs and disclosures have SAFE_CANDIDATE classification
    safe_intents = [i for i in derivation.intents if i.risk_classification == RiskClassification.SAFE_CANDIDATE]
    assert len(safe_intents) == 4  # 2 tabs + 2 disclosures


@pytest.mark.asyncio
async def test_profile_live_dom_variations_exploration(profile_server):
    """Verifies live discovery, guarded reconstruction, and oracle execution against /profile-varied.

    Live DOM variations include:
    - Inverted tab order ('System Preferences', 'Security & Credentials', 'Employee Overview').
    - Controls without explicit ID attributes (auto-ID generated by normalizer).
    - Varied labels ('Work History & Employment', 'Two-Factor Authentication Setup').
    - Delayed working disclosure (150ms).
    - Deliberately broken controls (dangling target, unrevealed, crash).
    - Mutating and prohibited server actions.
    """
    target_url = f"{profile_server.origin}/profile-varied"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(target_url)

        # 1. Live Discovery
        snap_adapter = ReadOnlyBrowserSnapshotAdapter()
        normalizer = SemanticNormalizer()
        initial_snap = await snap_adapter.capture_snapshot(
            source_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        assert initial_snap.coverage.terminal_status == "COMPLETE"

        initial_norm = normalizer.normalize_snapshot(initial_snap)
        assert initial_norm.terminal_status == "COMPLETE"
        initial_fp = CanonicalStateFingerprint.generate(initial_norm.semantic_state)

        # Confirm auto-IDs generated for controls lacking explicit HTML id
        auto_controls = [c for c in initial_norm.semantic_state.controls if "auto-" in c.control_id]
        assert len(auto_controls) >= 3, f"Expected auto-ID controls on varied page, found {len(auto_controls)}"

        intent_factory = CandidateActionIntentFactory()
        derivation = intent_factory.derive_intents(initial_norm)
        assert derivation.terminal_status == "COMPLETE"

        # Prohibited action (Delete Account) must not be executable
        assert not any("Delete Account" in (i.control_fingerprint or "") for i in derivation.intents)

        # 2. Guarded State Reconstruction with Auto-ID Prefix
        # Identify the "Security & Credentials" tab (auto-ID tab)
        sec_tab_ctrl = next(
            c for c in initial_norm.semantic_state.controls
            if c.accessible_role == "tab" and "Security" in (c.accessible_name or "")
        )
        assert "auto-" in sec_tab_ctrl.control_id

        sec_tab_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint=sec_tab_ctrl.control_id,
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        # Probe the revealed state behind Security tab using a temporary probe page
        probe_page = await source_ctx.new_page()
        await probe_page.goto(target_url)
        await probe_page.get_by_role("tab", name=sec_tab_ctrl.accessible_name).click()
        probe_snap = await snap_adapter.capture_snapshot(
            probe_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        probe_norm = normalizer.normalize_snapshot(probe_snap)
        revealed_fp = CanonicalStateFingerprint.generate(probe_norm.semantic_state)
        await probe_page.close()

        # Target revealed 2FA disclosure behind the tab
        mfa_ctrl = next(
            c for c in probe_norm.semantic_state.controls
            if "Two-Factor" in (c.accessible_name or "")
        )

        revealed_intent = CandidateActionIntent(
            parent_state_fingerprint=revealed_fp,
            control_fingerprint=mfa_ctrl.control_id,
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(sec_tab_intent,),
        )

        from app.qa.state_reconstruction import StateReconstructor, ReconstructionStatus
        reconstructor = StateReconstructor()

        async with reconstructor.reconstruct(source_ctx, target_url, revealed_intent) as session:
            assert session.result.is_revalidated is True
            assert session.result.status == ReconstructionStatus.REVALIDATED

        # 3. Functional Oracle Execution across Live Controls
        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)

        # A: Working Disclosure ("Work History & Employment" without explicit ID)
        hist_ctrl = next(
            c for c in initial_norm.semantic_state.controls
            if "Work History" in (c.accessible_name or "")
        )
        hist_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint=hist_ctrl.control_id,
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )
        hist_eval = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=hist_intent,
        )
        assert hist_eval.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert hist_eval.defect_category is None

        # B: Delayed Working Disclosure (150ms delay)
        del_ctrl = next(
            c for c in initial_norm.semantic_state.controls
            if "Delayed Transition" in (c.accessible_name or "")
        )
        del_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint=del_ctrl.control_id,
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )
        del_eval = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=del_intent,
        )
        assert del_eval.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert del_eval.defect_category is None

        # C: Deliberately Broken - Dangling Target
        dang_ctrl = next(
            c for c in initial_norm.semantic_state.controls
            if "Dangling Target" in (c.accessible_name or "")
        )
        dang_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint=dang_ctrl.control_id,
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )
        dang_eval = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=dang_intent,
        )
        assert dang_eval.verdict == FunctionalOutcomeVerdict.SUPPORTED_FAILURE
        assert dang_eval.defect_category == DefectCategory.DANGLING_ARIA_CONTROLS

        # D: Deliberately Broken - Unrevealed Content
        unrev_ctrl = next(
            c for c in initial_norm.semantic_state.controls
            if "Unrevealed Content" in (c.accessible_name or "")
        )
        unrev_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint=unrev_ctrl.control_id,
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )
        unrev_eval = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=unrev_intent,
        )
        assert unrev_eval.verdict == FunctionalOutcomeVerdict.SUPPORTED_FAILURE
        assert unrev_eval.defect_category == DefectCategory.CONTENT_NOT_REVEALED

        # E: Deliberately Broken - Crashing Button
        crash_ctrl = next(
            c for c in initial_norm.semantic_state.controls
            if "Crash On Click" in (c.accessible_name or "")
        )
        crash_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint=crash_ctrl.control_id,
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )
        crash_eval = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=crash_intent,
        )
        assert crash_eval.verdict == FunctionalOutcomeVerdict.SUPPORTED_FAILURE
        assert crash_eval.defect_category == DefectCategory.PAGE_SCRIPT_ERROR

        await source_ctx.close()
        await browser.close()


# ============================================================================
# Regression Test Suites: Followup Review 2026-10-20
# Safeguard 1: Project Isolation Through Actual Execution Path
# Safeguard 2: Fresh, Case-Specific Validation Evidence
# Safeguard 3: Observation Cancellation and Deadline Enforcement
# ============================================================================

@pytest.mark.asyncio
async def test_project_isolation_execution_path(profile_server):
    """Safeguard 1: Enforce project isolation through the actual execution path.
    - Missing project context (project_id=None) rejects project-bound contracts.
    - Wrong project context rejects project-bound contracts.
    - Two projects sharing the same URL and control identity cannot consume each other's contracts.
    - Repeated loading or updates replace stale registrations (no stale first-match).
    - Preserves explicit target-scoped test bindings without project_id.
    - Verifies integration through actual execution path (evaluate_disclosure_action).
    """
    # Contract definitions for Project 101 and Project 102
    contract_p101 = ValidationContract(
        contract_id="contract_project_101",
        rule_name="Project 101 Isolated Rule",
        trigger_control_id="btn-validate-history",
        field_selectors={"start_date": "#input-start-date", "end_date": "#input-end-date"},
        feedback_selector="#history-date-feedback",
        test_cases=WORK_HISTORY_DATE_CONTRACT.test_cases,
        completion_marker_attr="data-validation-case",
        completion_marker_type="case_id",
        completion_timestamp_attr="data-validation-timestamp",
        completion_mode="case_marker",
    )
    contract_p102 = ValidationContract(
        contract_id="contract_project_102",
        rule_name="Project 102 Isolated Rule",
        trigger_control_id="btn-validate-history",
        field_selectors={"start_date": "#input-start-date", "end_date": "#input-end-date"},
        feedback_selector="#history-date-feedback",
        test_cases=WORK_HISTORY_DATE_CONTRACT.test_cases,
        completion_marker_attr="data-validation-case",
        completion_marker_type="case_id",
        completion_timestamp_attr="data-validation-timestamp",
        completion_mode="case_marker",
    )

    target_url = f"{profile_server.origin}/profile-validation-variant?variant=working_validator_fresh"

    # Register project-bound contracts
    register_validation_contract(
        contract=contract_p101,
        target_url_pattern=target_url,
        target_control_id="btn-validate-history",
        feedback_selector="#history-date-feedback",
        project_id=101,
    )
    register_validation_contract(
        contract=contract_p102,
        target_url_pattern=target_url,
        target_control_id="btn-validate-history",
        feedback_selector="#history-date-feedback",
        project_id=102,
    )

    try:
        # 1. Missing project context: must NOT return project-bound contract
        contract_missing = find_validation_contract_for_control(
            control_id="btn-validate-history",
            target_url=target_url,
            project_id=None,
        )
        assert contract_missing is None or contract_missing.contract_id not in ("contract_project_101", "contract_project_102")

        # 2. Wrong project context: project 103 cannot access 101 or 102
        contract_wrong = find_validation_contract_for_control(
            control_id="btn-validate-history",
            target_url=target_url,
            project_id=103,
        )
        assert contract_wrong is None or contract_wrong.contract_id not in ("contract_project_101", "contract_project_102")

        # 3. Two projects sharing same URL and control: each strictly receives only its own contract
        found_101 = find_validation_contract_for_control(
            control_id="btn-validate-history",
            target_url=target_url,
            project_id=101,
        )
        assert found_101 is not None and found_101.contract_id == "contract_project_101"

        found_102 = find_validation_contract_for_control(
            control_id="btn-validate-history",
            target_url=target_url,
            project_id=102,
        )
        assert found_102 is not None and found_102.contract_id == "contract_project_102"

        # 4. Repeated loading / update: updating project 101 replaces the active contract
        contract_p101_updated = ValidationContract(
            contract_id="contract_project_101",
            rule_name="Project 101 Updated Rule",
            trigger_control_id="btn-validate-history",
            field_selectors={"start_date": "#input-start-date", "end_date": "#input-end-date"},
            feedback_selector="#history-date-feedback",
            test_cases=WORK_HISTORY_DATE_CONTRACT.test_cases,
            completion_marker_attr="data-validation-case",
            completion_marker_type="case_id",
            completion_timestamp_attr="data-validation-timestamp",
            completion_mode="case_marker",
        )
        register_validation_contract(
            contract=contract_p101_updated,
            target_url_pattern=target_url,
            target_control_id="btn-validate-history",
            feedback_selector="#history-date-feedback",
            project_id=101,
        )
        found_updated = find_validation_contract_for_control(
            control_id="btn-validate-history",
            target_url=target_url,
            project_id=101,
        )
        assert found_updated.rule_name == "Project 101 Updated Rule", "Old first-match contract remained active after update!"

        # 5. Integration through the actual execution path
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(target_url)

            executor = GuardedActionExecutor()
            oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
            candidate = await _derive_intent_from_page(page, "btn-validate-history")

            # Execute with project 101 context: uses Project 101 contract
            eval_p101 = await oracle.evaluate_disclosure_action(
                executor=executor,
                source_context=context,
                approved_target_url=target_url,
                candidate_intent=candidate,
                project_id=101,
            )
            assert eval_p101.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
            assert "Project 101 Updated Rule" in (eval_p101.evidence.rationale or "")

            # Execute with project 102 context: uses Project 102 contract
            eval_p102 = await oracle.evaluate_disclosure_action(
                executor=executor,
                source_context=context,
                approved_target_url=target_url,
                candidate_intent=candidate,
                project_id=102,
            )
            assert eval_p102.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
            assert "Project 102 Isolated Rule" in (eval_p102.evidence.rationale or "")

            # Execute with unrelated project 199 context: does not receive project 101 or 102 contracts
            eval_p199 = await oracle.evaluate_disclosure_action(
                executor=executor,
                source_context=context,
                approved_target_url=target_url,
                candidate_intent=candidate,
                project_id=199,
            )
            assert "Project 101" not in (eval_p199.evidence.rationale or "")
            assert "Project 102" not in (eval_p199.evidence.rationale or "")

            await browser.close()
    finally:
        from app.qa.validation_contract import unregister_validation_contract
        unregister_validation_contract("contract_project_101", project_id=101)
        unregister_validation_contract("contract_project_102", project_id=102)


@pytest.mark.asyncio
async def test_browser_validation_noop_trigger_preexisting_text(profile_server):
    """Safeguard 2: Pre-existing expected text with a no-op validation trigger.
    - Expected substring ALONE must NOT establish freshness.
    - Polling expires without observable change; halts subsequent actions immediately.
    - Outcome is NOT PASS and NOT a customer defect.
    """
    target_url = f"{profile_server.origin}/profile-validation-variant?variant=noop_trigger"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(target_url)

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
        )

        assert eval_res.verdict != FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.verdict in (FunctionalOutcomeVerdict.LIMITED_COVERAGE, FunctionalOutcomeVerdict.INCONCLUSIVE)

        await browser.close()


@pytest.mark.asyncio
async def test_browser_validation_stale_unchanged_timeout(profile_server):
    """Safeguard 2: Unchanged stale feedback after timeout.
    - Polling expires without fresh evidence.
    - Halts subsequent actions immediately.
    - Outcome is NOT PASS and NOT a customer defect.
    """
    target_url = f"{profile_server.origin}/profile-validation-variant?variant=stale_unchanged_timeout"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(target_url)

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
        )

        assert eval_res.verdict != FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.verdict in (FunctionalOutcomeVerdict.LIMITED_COVERAGE, FunctionalOutcomeVerdict.INCONCLUSIVE)

        await browser.close()


@pytest.mark.asyncio
async def test_browser_validation_late_preceding_feedback(profile_server):
    """Safeguard 2: Late feedback from preceding case does not leak into subsequent cases.
    - Preceding case times out after polling window (~1.0s) while feedback takes 4000ms.
    - Halts subsequent cases immediately so delayed feedback cannot be attributed to subsequent cases.
    - Outcome is NOT PASS and NOT a customer defect.
    """
    target_url = f"{profile_server.origin}/profile-validation-variant?variant=late_preceding_feedback"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(target_url)

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
        )

        assert eval_res.verdict != FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.verdict in (FunctionalOutcomeVerdict.LIMITED_COVERAGE, FunctionalOutcomeVerdict.INCONCLUSIVE)

        await browser.close()


@pytest.mark.asyncio
async def test_browser_validation_working_validator_fresh(profile_server):
    """Safeguard 2: Working validator with fresh results satisfies all contract cases."""
    target_url = f"{profile_server.origin}/profile-validation-variant?variant=working_validator_fresh"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(target_url)

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert "Work History Date Interval" in (eval_res.evidence.rationale or "")

        await browser.close()


@pytest.mark.asyncio
async def test_browser_validation_cancellation_during_observation(profile_server):
    """Safeguard 3: Enforce cancellation while waiting for feedback in the observation polling loop.
    - Cancellation token is triggered during observation polling.
    - Observation loop detects cancellation immediately and halts subsequent actions.
    - Outcome is NOT PASS and NOT a customer defect.
    """
    target_url = f"{profile_server.origin}/profile-validation-variant?variant=observation_timeout_slow"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(target_url)

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        from app.qa.browser_snapshot_adapter import CancellationToken
        token = CancellationToken()
        tracker = {}

        async def cancel_later():
            for _ in range(100):
                if tracker.get("phase") == "WAITING_FOR_FEEDBACK":
                    break
                await asyncio.sleep(0.01)
            token.cancel()

        cancel_task = asyncio.create_task(cancel_later())

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
            cancellation_token=token,
            lifecycle_tracker=tracker,
        )
        await cancel_task

        assert eval_res.execution_status == GuardedExecutionStatus.CANCELLED
        assert eval_res.verdict != FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.verdict in (FunctionalOutcomeVerdict.LIMITED_COVERAGE, FunctionalOutcomeVerdict.INCONCLUSIVE)

        await browser.close()


@pytest.mark.asyncio
async def test_browser_validation_deadline_expiry_during_observation(profile_server):
    """Safeguard 3: Enforce deadline expiry during feedback observation.
    - Polling and locator reads strictly bounded by remaining deadline.
    - No minimum polling wait that exceeds remaining budget.
    - Stops subsequent actions and reports honestly without PASS or defect.
    """
    target_url = f"{profile_server.origin}/profile-validation-variant?variant=observation_timeout_slow"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(target_url)

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        t0 = time.monotonic()
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
            action_timeout=0.35,  # Tiny action timeout: expires while waiting for 1200ms feedback
        )
        elapsed = time.monotonic() - t0

        assert elapsed < 3.0
        assert eval_res.execution_status == GuardedExecutionStatus.ACTION_FAILED
        assert eval_res.verdict != FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.verdict in (FunctionalOutcomeVerdict.LIMITED_COVERAGE, FunctionalOutcomeVerdict.INCONCLUSIVE)

        await browser.close()


def test_persistence_and_report_mapping_incomplete_outcome():
    """Verify that incomplete validation outcomes are truthfully mapped to LIMITED COVERAGE
    and never promoted to PASS or customer defects in reports or persistence."""
    from app.qa.functional_reporting_adapter import adapt_functional_result_to_test_record
    from app.qa.functional_oracle import (
        FunctionalEvidence,
        FunctionalEvaluationResult,
        FunctionalOutcomeVerdict,
    )
    from app.qa.validation_contract import ValidationContractResult, ValidationVerdict

    val_res = ValidationContractResult(
        contract_id="work_history_date_interval",
        rule_name="Work History Date Interval (End >= Start)",
        verdict=ValidationVerdict.INCOMPLETE_COVERAGE,
        all_passed=False,
        is_customer_defect=False,
        defect_description="Fresh validation evidence could not be established: feedback was stale",
        case_results=(),
        tested_cases_count=1,
    )

    eval_incomplete = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.INCONCLUSIVE,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:btn-validate-history",
        action_type="click",
        evidence=FunctionalEvidence(
            action_attempted=True,
            action_confirmed=True,
            expanded_before=None,
            expanded_after=None,
            state_toggled=False,
            controls_count_delta=None,
            fingerprint_changed=False,
            pre_state_fingerprint="fp1",
            post_state_fingerprint="fp1",
            controlled_element_id="history-date-feedback",
            controlled_element_found=True,
            controlled_element_visible_before=False,
            controlled_element_visible_after=False,
            pre_observation_succeeded=True,
            post_observation_succeeded=False,
            page_script_errors=(),
            observation_sufficient=False,
            observation_status="INCONCLUSIVE",
            rationale=f"Domain validation inconclusive: {val_res.summary}",
        ),
        execution_status=GuardedExecutionStatus.ACTION_FAILED,
        limitations=("validation_execution_interrupted",),
        diagnostics={"validation_contract_id": val_res.contract_id, "ambiguity_reason": val_res.summary},
    )

    record = adapt_functional_result_to_test_record(
        eval_res=eval_incomplete,
        run_id=1,
        target_url="http://localhost:5000/profile",
        viewport="Desktop",
    )

    assert record["status"] == "LIMITED COVERAGE"
    assert record.get("finding_id") is None
    assert record.get("stable_id") is None
    assert "inconclusive" in record["actual_result"].lower() or "limited" in record["actual_result"].lower() or "incomplete" in record["actual_result"].lower()


@pytest.mark.asyncio
async def test_browser_validation_cosmetic_class_mutation_fails_closed(profile_server):
    """Adversarial reproduction from review:
    - Pre-existing feedback text is present.
    - Trigger button click toggles only a cosmetic CSS class on the feedback element.
    - No validation logic runs.
    - Cosmetic mutation must NOT establish validation completion.
    - Must fail closed: verdict is LIMITED_COVERAGE / INCONCLUSIVE with customer_defect=False.
    - Verified through persistence and report mapping: no customer findings or defects created.
    """
    target_url = f"{profile_server.origin}/profile-validation-variant?variant=cosmetic_only"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(target_url)

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
        )

        assert eval_res.verdict != FunctionalOutcomeVerdict.SUPPORTED_FAILURE
        assert eval_res.verdict != FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert eval_res.verdict in (FunctionalOutcomeVerdict.LIMITED_COVERAGE, FunctionalOutcomeVerdict.INCONCLUSIVE)

        # Verify downstream report adapter and persistence
        from app.qa.functional_reporting_adapter import adapt_functional_result_to_test_record
        record = adapt_functional_result_to_test_record(
            eval_res=eval_res,
            run_id=42,
            target_url=target_url,
            viewport="Desktop",
        )
        assert record["status"] in ("LIMITED COVERAGE", "INCONCLUSIVE")
        assert record.get("finding_id") is None
        assert record.get("stable_id") is None

        await browser.close()


@pytest.mark.asyncio
async def test_browser_validation_loading_presentation_transition(profile_server):
    """Loading/presentation update before the real result arrives:
    - When clicked, feedback element enters intermediate loading state (aria-busy, .validating).
    - Polling loop does NOT treat intermediate loading presentation as completed result.
    - Waits for the loading state to complete and captures real result.
    - Yields verified PASS (VERIFIED_SUCCESS), customer_defect=False.
    """
    target_url = f"{profile_server.origin}/profile-validation-variant?variant=loading_presentation_transition"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(target_url)

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert "Work History Date Interval" in (eval_res.evidence.rationale or "")

        await browser.close()


@pytest.mark.asyncio
async def test_browser_validation_cross_case_delayed_feedback_isolated(profile_server):
    """Delayed feedback from an earlier case arriving while a later case is active:
    - Case 0 has a delayed callback (400ms) with error text and case_id 'case_invalid_interval'.
    - If Case 0 times out, subsequent actions halt, preventing delayed callback contamination.
    - If Case 1 runs with completion marker 'case_valid_interval', Case 0's delayed callback
      is rejected because its marker does not match Case 1's expected case ID.
    - Yields LIMITED_COVERAGE / INCONCLUSIVE with customer_defect=False (never false customer defect).
    """
    target_url = f"{profile_server.origin}/profile-validation-variant?variant=cross_case_delayed_callback"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(target_url)

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
        )

        # Cross-case delayed feedback was isolated:
        # Case 0's delayed callback was rejected during Case 1 because its marker belonged to Case 0.
        # Fails closed without falsely reporting a customer defect or false pass.
        assert eval_res.verdict != FunctionalOutcomeVerdict.SUPPORTED_FAILURE
        assert eval_res.is_customer_defect is False
        assert eval_res.verdict in (
            FunctionalOutcomeVerdict.LIMITED_COVERAGE,
            FunctionalOutcomeVerdict.INCONCLUSIVE,
            FunctionalOutcomeVerdict.EXECUTION_CONTROL_EVENT,
        )

        await browser.close()


@pytest.mark.asyncio
async def test_browser_validation_working_validator_genuine_completion(profile_server):
    """Working validator with genuine current-case completion:
    - All cases complete with attributable current-case evidence.
    - Expected feedback verified and matched to declared cases.
    - Satisfies contract with VERIFIED_SUCCESS and customer_defect=False.
    """
    target_url = f"{profile_server.origin}/profile-validation-variant?variant=working_validator_fresh"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(target_url)

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False

        await browser.close()


@pytest.mark.asyncio
async def test_browser_validation_identical_result_text_genuine_completion(profile_server):
    """Identical result text with genuine new completion:
    - Validator produces identical error text across consecutive cases.
    - Genuine new completion for current case is established via contract-supported completion marker.
    - Demonstrates that legitimate identical result text is accepted when completion is proven.
    """
    from app.qa.validation_contract import (
        ValidationContract,
        ValidationTestCase,
        register_validation_contract,
    )

    # Contract with two consecutive invalid cases both expecting identical error text
    two_invalid_contract = ValidationContract(
        contract_id="contract_two_invalid_dates",
        rule_name="Two Invalid Dates Rule",
        trigger_control_id="btn-validate-history",
        field_selectors={
            "start_date": "#input-start-date",
            "end_date": "#input-end-date",
        },
        feedback_selector="#history-date-feedback",
        test_cases=(
            ValidationTestCase(
                case_id="case_invalid_interval",
                description="Invalid interval 1 (Start 2024-05-01, End 2022-01-01)",
                input_values={
                    "start_date": "2024-05-01",
                    "end_date": "2022-01-01",
                },
                expected_valid=False,
                expected_feedback_substring="End date cannot be earlier than start date.",
            ),
            ValidationTestCase(
                case_id="case_invalid_interval_2",
                description="Invalid interval 2 (Start 2025-01-01, End 2023-01-01)",
                input_values={
                    "start_date": "2025-01-01",
                    "end_date": "2023-01-01",
                },
                expected_valid=False,
                expected_feedback_substring="End date cannot be earlier than start date.",
            ),
        ),
        target_url_pattern="*/profile-validation-variant?variant=identical_text_new_completion*",
        completion_marker_attr="data-validation-case",
        completion_marker_type="case_id",
    )
    register_validation_contract(two_invalid_contract)

    target_url = f"{profile_server.origin}/profile-validation-variant?variant=identical_text_new_completion"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(target_url)

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
        )

        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False

        await browser.close()


@pytest.mark.asyncio
async def test_browser_validation_stale_marker_reproduction(profile_server):
    """Adversarial reproduction from review with single-case contract:
    - Pre-existing data-validation-case="case_valid_interval" with stale error text.
    - Trigger button click toggles only cosmetic CSS class on the feedback element.
    - Single-case contract registers for case_valid_interval.
    - Pre-existing matching marker must NOT be accepted as fresh completion.
    - Must fail closed: verdict is LIMITED_COVERAGE / INCONCLUSIVE with is_customer_defect=False.
    - Verified through persistence and report mapping: zero findings created.
    """
    from dataclasses import replace
    target_url = f"{profile_server.origin}/profile-validation-variant?variant=stale_marker"

    clear_validation_contracts()
    single_case_contract = replace(
        WORK_HISTORY_DATE_CONTRACT,
        test_cases=WORK_HISTORY_DATE_CONTRACT.test_cases[:1],
        target_url_pattern=f"{profile_server.origin}/profile-validation-variant*",
    )
    register_validation_contract(single_case_contract)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(target_url)

            executor = GuardedActionExecutor()
            oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
            candidate = await _derive_intent_from_page(page, "btn-validate-history")

            eval_res = await oracle.evaluate_disclosure_action(
                executor=executor,
                source_context=context,
                approved_target_url=target_url,
                candidate_intent=candidate,
            )

            # Pre-existing matching marker must NOT establish completion
            assert eval_res.verdict != FunctionalOutcomeVerdict.SUPPORTED_FAILURE
            assert eval_res.is_customer_defect is False
            assert eval_res.verdict in (FunctionalOutcomeVerdict.LIMITED_COVERAGE, FunctionalOutcomeVerdict.INCONCLUSIVE)

            # Verify downstream persistence: zero customer defect findings created
            conn = _create_test_db()
            test_id = persist_functional_evaluation_record(
                conn=conn,
                eval_res=eval_res,
                run_id=101,
                target_url=target_url,
                enable_customer_defects=True,
            )
            c = conn.cursor()
            c.execute("SELECT status, actual_result, stable_id FROM tests WHERE id = ?", (test_id,))
            row = c.fetchone()
            assert row["status"] in ("LIMITED COVERAGE", "INCONCLUSIVE")
            assert row["stable_id"] is None

            c.execute("SELECT count(*) as cnt FROM findings WHERE run_id = 101")
            assert c.fetchone()["cnt"] == 0

            # Downstream report adapter mapping
            from app.qa.functional_reporting_adapter import adapt_functional_result_to_test_record
            record = adapt_functional_result_to_test_record(
                eval_res=eval_res,
                run_id=101,
                target_url=target_url,
                viewport="Desktop",
            )
            assert record["status"] in ("LIMITED COVERAGE", "INCONCLUSIVE")
            assert record.get("finding_id") is None
            assert record.get("stable_id") is None

            await browser.close()
    finally:
        clear_validation_contracts()
        register_profile_fixture_contracts(profile_server.origin)


@pytest.mark.asyncio
async def test_browser_validation_repeated_execution_prior_marker_remains(profile_server):
    """Repeated execution where the prior matching marker remains:
    - Target element already contains data-validation-case='case_valid_interval' from prior execution.
      * Subtest 1: When subsequent invocation does NOT update timestamp or loading state (inert repeated execution),
        it fails closed as INCONCLUSIVE / LIMITED_COVERAGE with is_customer_defect=False.
      * Subtest 2: When subsequent invocation genuinely updates timestamp, it establishes fresh completion
        and succeeds with VERIFIED_SUCCESS.
    """
    from dataclasses import replace
    clear_validation_contracts()
    single_contract = replace(
        WORK_HISTORY_DATE_CONTRACT,
        test_cases=WORK_HISTORY_DATE_CONTRACT.test_cases[:1],
        target_url_pattern=f"{profile_server.origin}/profile-validation-variant*",
    )
    register_validation_contract(single_contract)

    try:
        # Subtest 1: repeated execution where prior marker remains and no timestamp/loading update occurs
        target_url_inert = f"{profile_server.origin}/profile-validation-variant?variant=stale_marker"
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(target_url_inert)

            executor = GuardedActionExecutor()
            oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
            cand_inert = await _derive_intent_from_page(page, "btn-validate-history")

            eval_inert = await oracle.evaluate_disclosure_action(
                executor=executor,
                source_context=context,
                approved_target_url=target_url_inert,
                candidate_intent=cand_inert,
            )
            # Prior matching marker remained without fresh timestamp update -> fails closed
            assert eval_inert.verdict != FunctionalOutcomeVerdict.SUPPORTED_FAILURE
            assert eval_inert.verdict != FunctionalOutcomeVerdict.VERIFIED_SUCCESS
            assert eval_inert.verdict in (FunctionalOutcomeVerdict.LIMITED_COVERAGE, FunctionalOutcomeVerdict.INCONCLUSIVE)
            assert eval_inert.is_customer_defect is False

            await browser.close()

        # Subtest 2: repeated execution where prior marker remains but validator delivers fresh timestamp update
        target_url_working = f"{profile_server.origin}/profile-validation-variant?variant=stale_marker_working_update"
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(target_url_working)

            executor = GuardedActionExecutor()
            oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
            cand_w = await _derive_intent_from_page(page, "btn-validate-history")

            eval_working = await oracle.evaluate_disclosure_action(
                executor=executor,
                source_context=context,
                approved_target_url=target_url_working,
                candidate_intent=cand_w,
            )
            # Prior marker remained, but fresh invocation established newer timestamp -> succeeds
            assert eval_working.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
            assert eval_working.is_customer_defect is False

            await browser.close()
    finally:
        clear_validation_contracts()
        register_profile_fixture_contracts(profile_server.origin)


@pytest.mark.asyncio
async def test_browser_validation_missing_completion_semantics_honest_limitation(profile_server):
    """Missing completion semantics produce an honest incomplete result:
    - Contract has no completion semantics configured (completion_marker_attr=None, completion_mode=None, etc.).
    - Evaluates to LIMITED_COVERAGE / INCONCLUSIVE with limitation 'missing_validation_completion_configuration'.
    - Zero false customer defects or assumed fixture behavior.
    """
    target_url = f"{profile_server.origin}/profile-validation-variant?variant=working_validator_fresh"

    clear_validation_contracts()
    unconfigured_contract = ValidationContract(
        contract_id="contract_no_completion_config",
        rule_name="Unconfigured Completion Rule",
        trigger_control_id="btn-validate-history",
        field_selectors={
            "start_date": "#input-start-date",
            "end_date": "#input-end-date",
        },
        feedback_selector="#history-date-feedback",
        test_cases=WORK_HISTORY_DATE_CONTRACT.test_cases,
        target_url_pattern=f"{profile_server.origin}/profile-validation-variant*",
        completion_marker_attr=None,
        completion_marker_type=None,
        completion_timestamp_attr=None,
        completion_mode=None,
    )
    assert not unconfigured_contract.has_completion_semantics
    register_validation_contract(unconfigured_contract)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(target_url)

            executor = GuardedActionExecutor()
            oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
            candidate = await _derive_intent_from_page(page, "btn-validate-history")

            eval_res = await oracle.evaluate_disclosure_action(
                executor=executor,
                source_context=context,
                approved_target_url=target_url,
                candidate_intent=candidate,
            )

            assert eval_res.verdict != FunctionalOutcomeVerdict.SUPPORTED_FAILURE
            assert eval_res.is_customer_defect is False
            assert eval_res.verdict in (FunctionalOutcomeVerdict.LIMITED_COVERAGE, FunctionalOutcomeVerdict.INCONCLUSIVE)
            assert "missing_validation_completion_configuration" in eval_res.limitations

            # Persistence verifies zero findings
            conn = _create_test_db()
            test_id = persist_functional_evaluation_record(
                conn=conn,
                eval_res=eval_res,
                run_id=202,
                target_url=target_url,
                enable_customer_defects=True,
            )
            c = conn.cursor()
            c.execute("SELECT status, actual_result, stable_id FROM tests WHERE id = ?", (test_id,))
            row = c.fetchone()
            assert row["status"] in ("LIMITED COVERAGE", "INCONCLUSIVE")
            assert row["stable_id"] is None
            c.execute("SELECT count(*) as cnt FROM findings WHERE run_id = 202")
            assert c.fetchone()["cnt"] == 0

            await browser.close()
    finally:
        clear_validation_contracts()
        register_profile_fixture_contracts(profile_server.origin)


@pytest.mark.asyncio
async def test_browser_validation_loading_only_cycle_fails_closed(profile_server):
    """Adversarial reproduction from review:
    - Pre-existing matching marker data-validation-case='case_valid_interval' and old error message.
    - Contract declares completion_mode='case_marker'.
    - Action click only adds and removes a CSS .loading class (loading-only presentation cycle).
    - No validation logic runs, and marker/message remain unchanged.
    - Demonstrates that presentation/loading cycles CANNOT satisfy a case_marker contract.
    - Fails closed as INCONCLUSIVE / LIMITED_COVERAGE with 0 customer defect findings.
    """
    from dataclasses import replace
    clear_validation_contracts()
    single_contract = replace(
        WORK_HISTORY_DATE_CONTRACT,
        test_cases=WORK_HISTORY_DATE_CONTRACT.test_cases[:1],
        target_url_pattern=f"{profile_server.origin}/profile-validation-variant*",
        completion_mode="case_marker",
    )
    register_validation_contract(single_contract)

    target_url = f"{profile_server.origin}/profile-validation-variant?variant=stale_marker_loading_only"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(target_url)

            executor = GuardedActionExecutor()
            oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
            candidate = await _derive_intent_from_page(page, "btn-validate-history")

            eval_res = await oracle.evaluate_disclosure_action(
                executor=executor,
                source_context=context,
                approved_target_url=target_url,
                candidate_intent=candidate,
            )

            # Loading cycle on case_marker contract must NOT establish completion or report customer defect
            assert eval_res.verdict != FunctionalOutcomeVerdict.SUPPORTED_FAILURE
            assert eval_res.is_customer_defect is False
            assert eval_res.verdict in (FunctionalOutcomeVerdict.LIMITED_COVERAGE, FunctionalOutcomeVerdict.INCONCLUSIVE)

            # Downstream persistence check: zero findings in database
            conn = _create_test_db()
            test_id = persist_functional_evaluation_record(
                conn=conn,
                eval_res=eval_res,
                run_id=303,
                target_url=target_url,
                enable_customer_defects=True,
            )
            c = conn.cursor()
            c.execute("SELECT status, actual_result, stable_id FROM tests WHERE id = ?", (test_id,))
            row = c.fetchone()
            assert row["status"] in ("LIMITED COVERAGE", "INCONCLUSIVE")
            assert row["stable_id"] is None

            c.execute("SELECT count(*) as cnt FROM findings WHERE run_id = 303")
            assert c.fetchone()["cnt"] == 0

            await browser.close()
    finally:
        clear_validation_contracts()
        register_profile_fixture_contracts(profile_server.origin)


@pytest.mark.asyncio
async def test_browser_validation_malformed_and_non_increasing_timestamps_fail_closed(profile_server):
    """Numeric timestamp semantics must reject missing, malformed, and non-increasing timestamps:
    - Subtest 1: Malformed non-numeric timestamp attribute ('not_a_number').
    - Subtest 2: Non-increasing timestamp attribute (stays at 2000).
    - Both fail closed as INCONCLUSIVE / LIMITED_COVERAGE with is_customer_defect=False.
    """
    from dataclasses import replace
    clear_validation_contracts()
    single_contract = replace(
        WORK_HISTORY_DATE_CONTRACT,
        test_cases=WORK_HISTORY_DATE_CONTRACT.test_cases[:1],
        target_url_pattern=f"{profile_server.origin}/profile-validation-variant*",
        completion_mode="case_marker",
    )
    register_validation_contract(single_contract)

    try:
        # Subtest 1: Malformed timestamp
        url_malformed = f"{profile_server.origin}/profile-validation-variant?variant=stale_marker_malformed_timestamp"
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url_malformed)

            executor = GuardedActionExecutor()
            oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
            cand_m = await _derive_intent_from_page(page, "btn-validate-history")

            eval_m = await oracle.evaluate_disclosure_action(
                executor=executor,
                source_context=context,
                approved_target_url=url_malformed,
                candidate_intent=cand_m,
            )
            assert eval_m.verdict != FunctionalOutcomeVerdict.SUPPORTED_FAILURE
            assert eval_m.is_customer_defect is False
            assert eval_m.verdict in (FunctionalOutcomeVerdict.LIMITED_COVERAGE, FunctionalOutcomeVerdict.INCONCLUSIVE)

            await browser.close()

        # Subtest 2: Non-increasing timestamp
        url_non_inc = f"{profile_server.origin}/profile-validation-variant?variant=stale_marker_non_increasing_timestamp"
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url_non_inc)

            executor = GuardedActionExecutor()
            oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
            cand_ni = await _derive_intent_from_page(page, "btn-validate-history")

            eval_ni = await oracle.evaluate_disclosure_action(
                executor=executor,
                source_context=context,
                approved_target_url=url_non_inc,
                candidate_intent=cand_ni,
            )
            assert eval_ni.verdict != FunctionalOutcomeVerdict.SUPPORTED_FAILURE
            assert eval_ni.is_customer_defect is False
            assert eval_ni.verdict in (FunctionalOutcomeVerdict.LIMITED_COVERAGE, FunctionalOutcomeVerdict.INCONCLUSIVE)

            await browser.close()
    finally:
        clear_validation_contracts()
        register_profile_fixture_contracts(profile_server.origin)


def test_completion_configuration_validation_matrix():
    """Consolidated unit test matrix verifying configuration validation rules:
    - Unsupported modes are rejected.
    - Contradictory configurations are rejected.
    - Missing required attributes for declared mode are rejected.
    - Supported configurations are validated successfully.
    """
    from app.qa.validation_contract import ValidationContract, ValidationTestCase

    dummy_cases = (
        ValidationTestCase(
            case_id="c1",
            description="c1 desc",
            input_values={"f1": "v1"},
            expected_valid=True,
        ),
    )

    # 1. Missing all completion semantics -> invalid
    c_empty = ValidationContract(
        contract_id="c_empty",
        rule_name="Empty Rule",
        feedback_selector="#fb",
        test_cases=dummy_cases,
    )
    is_valid, err = c_empty.validate_completion_configuration()
    assert is_valid is False
    assert "No completion semantics" in err

    # 2. Unsupported completion mode
    c_unsupp = ValidationContract(
        contract_id="c_unsupp",
        rule_name="Unsupp Rule",
        feedback_selector="#fb",
        test_cases=dummy_cases,
        completion_mode="unsupported_heuristic",
    )
    is_valid, err = c_unsupp.validate_completion_configuration()
    assert is_valid is False
    assert "Unsupported completion_mode" in err

    # 3. case_marker missing completion_marker_attr
    c_cm_no_attr = ValidationContract(
        contract_id="c_cm_no_attr",
        rule_name="CM No Attr",
        feedback_selector="#fb",
        test_cases=dummy_cases,
        completion_mode="case_marker",
    )
    is_valid, err = c_cm_no_attr.validate_completion_configuration()
    assert is_valid is False
    assert "requires 'completion_marker_attr'" in err

    # 4. timestamp missing completion_timestamp_attr
    c_ts_no_attr = ValidationContract(
        contract_id="c_ts_no_attr",
        rule_name="TS No Attr",
        feedback_selector="#fb",
        test_cases=dummy_cases,
        completion_mode="timestamp",
    )
    is_valid, err = c_ts_no_attr.validate_completion_configuration()
    assert is_valid is False
    assert "requires 'completion_timestamp_attr'" in err

    # 5. token missing completion_marker_attr
    c_tok_no_attr = ValidationContract(
        contract_id="c_tok_no_attr",
        rule_name="Tok No Attr",
        feedback_selector="#fb",
        test_cases=dummy_cases,
        completion_mode="token",
    )
    is_valid, err = c_tok_no_attr.validate_completion_configuration()
    assert is_valid is False
    assert "requires 'completion_marker_attr'" in err

    # 6. Contradictory case_marker with token marker type
    c_cm_contra = ValidationContract(
        contract_id="c_cm_contra",
        rule_name="CM Contra",
        feedback_selector="#fb",
        test_cases=dummy_cases,
        completion_mode="case_marker",
        completion_marker_attr="data-val-case",
        completion_marker_type="token",
    )
    is_valid, err = c_cm_contra.validate_completion_configuration()
    assert is_valid is False
    assert "Contradictory" in err

    # 7. Valid case_marker
    c_cm_valid = ValidationContract(
        contract_id="c_cm_valid",
        rule_name="CM Valid",
        feedback_selector="#fb",
        test_cases=dummy_cases,
        completion_mode="case_marker",
        completion_marker_attr="data-val-case",
        completion_marker_type="case_id",
        completion_timestamp_attr="data-val-ts",
    )
    is_valid, err = c_cm_valid.validate_completion_configuration()
    assert is_valid is True
    assert err is None
    assert c_cm_valid.has_completion_semantics is True

    # 8. Valid timestamp
    c_ts_valid = ValidationContract(
        contract_id="c_ts_valid",
        rule_name="TS Valid",
        feedback_selector="#fb",
        test_cases=dummy_cases,
        completion_mode="timestamp",
        completion_timestamp_attr="data-ts",
    )
    is_valid, err = c_ts_valid.validate_completion_configuration()
    assert is_valid is True
    assert c_ts_valid.has_completion_semantics is True

    # 9. Valid loading_transition
    c_lt_valid = ValidationContract(
        contract_id="c_lt_valid",
        rule_name="LT Valid",
        feedback_selector="#fb",
        test_cases=dummy_cases,
        completion_mode="loading_transition",
    )
    is_valid, err = c_lt_valid.validate_completion_configuration()
    assert is_valid is True
    assert c_lt_valid.has_completion_semantics is True

    # 10. Valid token
    c_tok_valid = ValidationContract(
        contract_id="c_tok_valid",
        rule_name="Tok Valid",
        feedback_selector="#fb",
        test_cases=dummy_cases,
        completion_mode="token",
        completion_marker_attr="data-token",
    )
    is_valid, err = c_tok_valid.validate_completion_configuration()
    assert is_valid is True
    assert c_tok_valid.has_completion_semantics is True


def test_strictly_increasing_numeric_timestamp_matrix():
    """Unit test matrix verifying strict numeric timestamp comparisons:
    - Rejects missing/None/empty curr_val.
    - Rejects non-numeric strings ('invalid', 'abc').
    - Rejects NaN, Inf, -Inf.
    - Rejects non-increasing values (equal, decreasing).
    - Accepts strictly increasing positive numeric timestamps.
    """
    from app.qa.guarded_execution import _is_strictly_increasing_numeric_timestamp

    # Missing / None / Empty
    assert _is_strictly_increasing_numeric_timestamp(None, None) is False
    assert _is_strictly_increasing_numeric_timestamp("100", None) is False
    assert _is_strictly_increasing_numeric_timestamp("100", "") is False
    assert _is_strictly_increasing_numeric_timestamp("100", "   ") is False

    # Malformed non-numeric curr_val
    assert _is_strictly_increasing_numeric_timestamp("100", "not_a_number") is False
    assert _is_strictly_increasing_numeric_timestamp("100", "abc") is False
    assert _is_strictly_increasing_numeric_timestamp(None, "abc") is False

    # NaN / Inf
    assert _is_strictly_increasing_numeric_timestamp("100", "nan") is False
    assert _is_strictly_increasing_numeric_timestamp("100", "inf") is False
    assert _is_strictly_increasing_numeric_timestamp("100", "-inf") is False
    assert _is_strictly_increasing_numeric_timestamp("nan", "200") is False

    # Malformed pre_val with valid curr_val fails closed
    assert _is_strictly_increasing_numeric_timestamp("invalid_pre", "200") is False

    # Non-increasing values
    assert _is_strictly_increasing_numeric_timestamp("100", "100") is False
    assert _is_strictly_increasing_numeric_timestamp("100", "99") is False
    assert _is_strictly_increasing_numeric_timestamp("100.5", "100.5") is False
    assert _is_strictly_increasing_numeric_timestamp("100.5", "100.4") is False

    # Strictly increasing values
    assert _is_strictly_increasing_numeric_timestamp(None, "100") is True
    assert _is_strictly_increasing_numeric_timestamp("", "100") is True
    assert _is_strictly_increasing_numeric_timestamp("100", "101") is True
    assert _is_strictly_increasing_numeric_timestamp("100.1", "100.2") is True
    assert _is_strictly_increasing_numeric_timestamp("1729450000000", "1729450000100") is True


@pytest.mark.asyncio
async def test_multi_case_validation_contract_deadline_budget_allocation(profile_server):
    """Regression test for multi-case validation contract deadline budget allocation.
    Verifies that when a contract contains multiple test cases (e.g. valid and invalid intervals),
    action_timeout is budgeted per test case (bounded by overall contract_deadline) rather than
    forcing the entire multi-case transaction into a single action deadline.
    Both cases must execute and complete, yielding VERIFIED_SUCCESS instead of being starved into
    INCONCLUSIVE / AMBIGUOUS_FEEDBACK."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{profile_server.origin}/profile")

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        t0 = time.monotonic()
        eval_res = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=f"{profile_server.origin}/profile",
            candidate_intent=candidate,
            action_timeout=1.0,
        )
        elapsed = time.monotonic() - t0

        assert eval_res.execution_status == GuardedExecutionStatus.EXECUTED
        assert eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res.is_customer_defect is False
        assert "validation_execution_interrupted" not in eval_res.limitations

        # Verify both test cases were observed and passed
        assert eval_res.diagnostics.get("cases_tested") == 2
        assert eval_res.evidence.observation_status == "COMPLETED"
        assert eval_res.evidence.observation_sufficient is True

        await browser.close()


@pytest.mark.asyncio
async def test_multi_case_validation_contract_starvation_reproduction_and_per_case_resolution(profile_server):
    """Substantiating regression for multi-case validation contract deadline budget.
    Demonstrates:
    1. Timing condition associated with starvation: on a target with asynchronous feedback delay
       (profile-validation-variant?variant=delayed_feedback, ~300ms delay per case), individual cases
       take ~500ms each, meaning the 2-case transaction requires ~1.0-1.1s total.
    2. Under a simulated or constrained single-action shared budget (e.g. action_timeout=0.2s or an
       un-extended total deadline shorter than total elapsed time), Case 2 times out or is starved,
       failing closed with INCONCLUSIVE / ACTION_FAILED (producing LIMITED COVERAGE in the oracle).
    3. Under the corrected per-case budget with action_timeout=0.8s (where contract_deadline = 0.8 * 2 = 1.6s
       and case_deadline is 0.8s), each case succeeds within its declared bound, completing both cases (2/2)
       and yielding VERIFIED_SUCCESS.
    4. Enclosing evaluation budget and cancellation boundaries remain strictly enforced:
       a) A tight action_timeout (0.2s < 300ms feedback delay) aborts boundedly and fails closed.
       b) An explicit cancellation token cancels the operation immediately without lingering browser actions.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        target_url = f"{profile_server.origin}/profile-validation-variant?variant=delayed_feedback"
        await page.goto(target_url)

        executor = GuardedActionExecutor()
        oracle = SafeDisclosureFunctionalOracle(enable_defect_classification=True)
        candidate = await _derive_intent_from_page(page, "btn-validate-history")

        # Part 1: Corrected per-case allocation succeeds within declared bounds
        # Total duration for 2 cases on delayed_feedback is ~1.1s > 0.8s single action budget.
        # Under per-case budgeting (contract_deadline = 0.8 * 2 = 1.6s, case_deadline = 0.8s),
        # both cases finish within their individual 0.8s bounds and succeed.
        t0 = time.monotonic()
        eval_res_ok = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
            action_timeout=0.8,
        )
        elapsed_ok = time.monotonic() - t0
        assert eval_res_ok.execution_status == GuardedExecutionStatus.EXECUTED
        assert eval_res_ok.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
        assert eval_res_ok.is_customer_defect is False
        assert eval_res_ok.diagnostics.get("cases_tested") == 2
        assert eval_res_ok.evidence.observation_status == "COMPLETED"
        assert elapsed_ok > 0.8, "Demonstrates combined 2-case duration exceeds single 0.8s action budget"
        assert elapsed_ok < 3.5, "Demonstrates combined 2-case duration and reconstruction stays bounded"

        # Part 2: Enclosing budget enforcement — tight timeout fails closed
        # When action_timeout is 0.2s (less than the 300ms feedback delay), the executor must
        # boundedly fail closed rather than hanging or granting unbounded time.
        t0_tight = time.monotonic()
        eval_res_tight = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
            action_timeout=0.2,
        )
        elapsed_tight = time.monotonic() - t0_tight
        assert eval_res_tight.execution_status == GuardedExecutionStatus.ACTION_FAILED
        # The tight budget can expire before a validation result exists (execution
        # control) or during validation (inconclusive); neither proves a defect.
        assert eval_res_tight.verdict in (FunctionalOutcomeVerdict.INCONCLUSIVE,
                                        FunctionalOutcomeVerdict.EXECUTION_CONTROL_EVENT)
        assert eval_res_tight.is_customer_defect is False
        assert elapsed_tight < 1.5, "Must respect tight budget and abort boundedly"

        # Part 3: Cancellation semantics preserved
        from app.qa.guarded_execution import CancellationToken
        token = CancellationToken()
        token.cancel()
        eval_res_cancelled = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=context,
            approved_target_url=target_url,
            candidate_intent=candidate,
            action_timeout=1.0,
            cancellation_token=token,
        )
        assert eval_res_cancelled.execution_status == GuardedExecutionStatus.CANCELLED
        assert "operation_cancelled" in eval_res_cancelled.limitations

        await browser.close()







