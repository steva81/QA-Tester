"""Phase C Increment 2: Functional Reporting Adapter Test Suite.

Validates:
1. Deterministic mapping of every FunctionalOutcomeVerdict to canonical status:
   - VERIFIED_SUCCESS -> PASS (scoped strictly to the tested disclosure action)
   - INCONCLUSIVE -> LIMITED COVERAGE (plain-language explanation of what remains unverified)
   - LIMITED_COVERAGE -> LIMITED COVERAGE (specific supported limitation)
   - SAFETY_ABORT -> SAFETY ACTION
   - EXECUTION_CONTROL_EVENT -> DIAGNOSTIC (preserves attempted/confirmed action history)
   - DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE -> DIAGNOSTIC (reports confirmed action and cleanup problem separately)
   - Unexpected SUPPORTED_FAILURE -> DIAGNOSTIC (never silently converted to PASS or finding)
2. No mapping creates customer findings, finding links, or customer triage entries.
3. Plain-language fixed templates only (no arbitrary exception dumps, rationale strings, or raw diagnostics).
4. Strict privacy sanitization: credentials and sensitive query tokens stripped from target URLs.
5. Mixed successful and unverified actions produce honest LIMITED COVERAGE outcome, never false complete coverage.
6. Repeated report rendering is read-only and idempotent.
7. Controlled localhost end-to-end integration: real Chromium -> executor -> oracle -> adapter -> reporting pipeline.
"""

import json
import sqlite3
import pytest
from playwright.async_api import async_playwright

from app.qa.functional_oracle import (
    DefectCategory,
    FunctionalEvidence,
    FunctionalEvaluationResult,
    FunctionalOutcomeVerdict,
    SafeDisclosureFunctionalOracle,
)
from app.qa.functional_reporting_adapter import (
    ALLOWLISTED_VIEWPORTS,
    adapt_functional_result_to_test_record,
    compute_opaque_action_ref,
    persist_functional_evaluation_record,
    sanitize_target_url,
)
from app.qa.guarded_execution import (
    GuardedActionExecutor,
    GuardedExecutionResult,
    GuardedExecutionStatus,
)
from app.qa.reporting import build_report, load_report, presentation
from app.qa.report_renderers import render_text, render_markdown, render_html
from tests.test_functional_oracle import FunctionalOracleServer, _derive_intent_from_page


@pytest.fixture(scope="module")
def oracle_server():
    server = FunctionalOracleServer()
    import threading
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def create_in_memory_db() -> sqlite3.Connection:
    """Create a fully initialized in-memory SQLite database matching the production schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            target_url TEXT NOT NULL,
            environment TEXT NOT NULL,
            tester_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT
        )
    """)

    c.execute("""
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_time TIMESTAMP,
            authorized_at TIMESTAMP,
            total_pages INTEGER DEFAULT 0,
            total_tests INTEGER DEFAULT 0,
            FOREIGN KEY (project_id) REFERENCES projects (id)
        )
    """)

    c.execute("""
        CREATE TABLE tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            expected_result TEXT,
            actual_result TEXT,
            status TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            viewport TEXT,
            screenshot_path TEXT,
            stable_id TEXT,
            final_url TEXT,
            FOREIGN KEY (run_id) REFERENCES runs (id)
        )
    """)

    c.execute("""
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
            confidence TEXT,
            description TEXT,
            impact TEXT,
            recommendation TEXT,
            plain_language_explanation TEXT,
            learning_explanation_sv TEXT,
            how_to_explain_sv TEXT,
            severity_origin TEXT NOT NULL DEFAULT 'baseline',
            risk_origin TEXT NOT NULL DEFAULT 'baseline',
            confidence_origin TEXT NOT NULL DEFAULT 'baseline',
            status TEXT DEFAULT 'OPEN',
            review_status TEXT DEFAULT 'PENDING',
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    from app.models.crawl import create_crawl_tables
    create_crawl_tables(conn)
    conn.commit()
    return conn


def _make_dummy_evidence(**kwargs) -> FunctionalEvidence:
    defaults = {
        "action_attempted": True,
        "action_confirmed": True,
        "expanded_before": False,
        "expanded_after": True,
        "state_toggled": True,
        "controls_count_delta": 0,
        "fingerprint_changed": True,
        "pre_state_fingerprint": "fp1",
        "post_state_fingerprint": "fp2",
        "controlled_element_id": "panel",
        "controlled_element_found": True,
        "controlled_element_visible_before": False,
        "controlled_element_visible_after": True,
        "pre_observation_succeeded": True,
        "post_observation_succeeded": True,
        "page_script_errors": (),
        "observation_sufficient": True,
        "observation_status": "COMPLETED",
        "rationale": "Disclosure expanded correctly",
    }
    defaults.update(kwargs)
    return FunctionalEvidence(**defaults)


# ==============================================================================
# 1. Deterministic Mapping Tests
# ==============================================================================

def test_mapping_verified_success_expansion_to_canonical_pass():
    """Verify VERIFIED_SUCCESS expansion maps to PASS with scoped transition evidence."""
    ev = _make_dummy_evidence(expanded_before=False, expanded_after=True, controlled_element_visible_before=False, controlled_element_visible_after=True)
    eval_res = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:faq-btn",
        action_type="click",
        evidence=ev,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )

    rec = adapt_functional_result_to_test_record(eval_res, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec["status"] == "PASS"
    assert rec["stable_id"] is None
    assert rec["finding_id"] is None
    assert "Verified safe disclosure expansion: aria-expanded toggled False -> True" in rec["actual_result"]
    assert "controlled content became visible" in rec["actual_result"]
    # Scope check: describes the specific transition, not whole-page correctness
    assert "whole-page" not in rec["actual_result"].lower()
    assert "entire page" not in rec["actual_result"].lower()


def test_mapping_verified_success_collapse_to_canonical_pass():
    """Verify VERIFIED_SUCCESS collapse maps to PASS with scoped transition evidence."""
    ev = _make_dummy_evidence(expanded_before=True, expanded_after=False, controlled_element_visible_before=True, controlled_element_visible_after=False)
    eval_res = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:collapse-btn",
        action_type="click",
        evidence=ev,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )

    rec = adapt_functional_result_to_test_record(eval_res, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec["status"] == "PASS"
    assert rec["stable_id"] is None
    assert "Verified safe disclosure collapse: aria-expanded toggled True -> False" in rec["actual_result"]
    assert "controlled content became hidden" in rec["actual_result"]


def test_contradictory_verified_success_rejected_by_positive_evidence_gate():
    """Verify Reproduction 1: Contradictory or missing evidence in VERIFIED_SUCCESS is rejected as DIAGNOSTIC.

    Reproduction from independent review:
    Construct a VERIFIED_SUCCESS result with evidence:
    action_confirmed=False,
    pre_observation_succeeded=False,
    controlled_element_visible_after=False.
    The adapter must emit DIAGNOSTIC (never PASS) with the fixed explanation:
    'Diagnostic: invalid success evidence; required pre/post observations, confirmed action, or state transitions were missing or contradictory.'
    Remove the generic 'verified transition' fallback for unsupported success evidence.
    """
    # 1. Exact independent review reproduction
    ev_repro = _make_dummy_evidence(
        action_confirmed=False,
        pre_observation_succeeded=False,
        controlled_element_visible_after=False,
    )
    res_repro = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:bad-btn",
        action_type="click",
        evidence=ev_repro,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )
    rec_repro = adapt_functional_result_to_test_record(res_repro, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec_repro["status"] == "DIAGNOSTIC"
    assert rec_repro["status"] != "PASS"
    assert "Diagnostic: invalid success evidence" in rec_repro["actual_result"]
    assert "required pre/post observations, confirmed action, or state transitions were missing or contradictory" in rec_repro["actual_result"]
    assert "verified safe disclosure transition" not in rec_repro["actual_result"].lower()

    # 2. Missing pre-observation alone
    ev_no_pre = _make_dummy_evidence(pre_observation_succeeded=False)
    res_no_pre = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:btn",
        action_type="click",
        evidence=ev_no_pre,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )
    rec_no_pre = adapt_functional_result_to_test_record(res_no_pre, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec_no_pre["status"] == "DIAGNOSTIC"
    assert "Diagnostic: invalid success evidence" in rec_no_pre["actual_result"]

    # 3. Missing post-observation alone
    ev_no_post = _make_dummy_evidence(post_observation_succeeded=False)
    res_no_post = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:btn",
        action_type="click",
        evidence=ev_no_post,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )
    rec_no_post = adapt_functional_result_to_test_record(res_no_post, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec_no_post["status"] == "DIAGNOSTIC"
    assert "Diagnostic: invalid success evidence" in rec_no_post["actual_result"]

    # 4. Observation timed out
    ev_timeout = _make_dummy_evidence(observation_status="TIMED_OUT")
    res_timeout = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:btn",
        action_type="click",
        evidence=ev_timeout,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )
    rec_timeout = adapt_functional_result_to_test_record(res_timeout, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec_timeout["status"] == "DIAGNOSTIC"
    assert "Diagnostic: invalid success evidence" in rec_timeout["actual_result"]

    # 5. Contradictory boolean transition (expanded toggled False -> True, but visibility toggled True -> False)
    ev_contradict = _make_dummy_evidence(
        expanded_before=False,
        expanded_after=True,
        controlled_element_visible_before=True,
        controlled_element_visible_after=False,
    )
    res_contradict = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:btn",
        action_type="click",
        evidence=ev_contradict,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )
    rec_contradict = adapt_functional_result_to_test_record(res_contradict, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec_contradict["status"] == "DIAGNOSTIC"
    assert "Diagnostic: invalid success evidence" in rec_contradict["actual_result"]


def test_mapping_inconclusive_deferred_defect_to_limited_coverage():
    """Verify INCONCLUSIVE with deferred defect category maps to LIMITED COVERAGE."""
    ev = _make_dummy_evidence(observation_sufficient=True)
    eval_res = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.INCONCLUSIVE,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:inert-btn",
        action_type="click",
        evidence=ev,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=("defect_classification_deferred_inert_disclosure_control",),
        diagnostics={"deferred_defect_category": "INERT_DISCLOSURE_CONTROL"},
    )

    rec = adapt_functional_result_to_test_record(eval_res, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec["status"] == "LIMITED COVERAGE"
    assert rec["stable_id"] is None
    assert "automated customer-defect classification is deferred" in rec["actual_result"]
    assert "state transition unverified" in rec["actual_result"]


def test_mapping_inconclusive_missing_evidence_to_limited_coverage():
    """Verify INCONCLUSIVE with missing pre/post observation maps to LIMITED COVERAGE."""
    ev = _make_dummy_evidence(pre_observation_succeeded=False, post_observation_succeeded=False)
    eval_res = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.INCONCLUSIVE,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:btn",
        action_type="click",
        evidence=ev,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )

    rec = adapt_functional_result_to_test_record(eval_res, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec["status"] == "LIMITED COVERAGE"
    assert rec["stable_id"] is None
    assert "required pre/post DOM observations were missing or incomplete" in rec["actual_result"]


def test_mapping_limited_coverage_budget_exhausted_to_limited_coverage():
    """Verify LIMITED_COVERAGE from timeout maps to LIMITED COVERAGE with budget explanation."""
    ev = _make_dummy_evidence(observation_status="TIMED_OUT")
    eval_res = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.LIMITED_COVERAGE,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:delayed-btn",
        action_type="click",
        evidence=ev,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=("observation_budget_exhausted_beyond_bounded_coverage",),
        diagnostics={"observation_status": "TIMED_OUT"},
    )

    rec = adapt_functional_result_to_test_record(eval_res, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec["status"] == "LIMITED COVERAGE"
    assert rec["stable_id"] is None
    assert "observation budget exhausted (1000ms)" in rec["actual_result"]


def test_mapping_limited_coverage_multiple_controls_to_limited_coverage():
    """Verify LIMITED_COVERAGE from multiple targets maps to LIMITED COVERAGE with multi-target reason."""
    ev = _make_dummy_evidence()
    eval_res = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.LIMITED_COVERAGE,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:multi-btn",
        action_type="click",
        evidence=ev,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=("multiple_controlled_elements_unsupported",),
    )

    rec = adapt_functional_result_to_test_record(eval_res, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec["status"] == "LIMITED COVERAGE"
    assert rec["stable_id"] is None
    assert "multiple controlled elements" in rec["actual_result"]


def test_mapping_limited_coverage_network_lockdown_to_limited_coverage():
    """Verify LIMITED_COVERAGE from network lockdown maps to LIMITED COVERAGE with offline barrier reason."""
    ev = _make_dummy_evidence()
    eval_res = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.LIMITED_COVERAGE,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:net-btn",
        action_type="click",
        evidence=ev,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=("network_dependent_disclosure_outside_offline_scope",),
        diagnostics={"qa_network_interference": True},
    )

    rec = adapt_functional_result_to_test_record(eval_res, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec["status"] == "LIMITED COVERAGE"
    assert rec["stable_id"] is None
    assert "offline isolation barrier" in rec["actual_result"]


def test_mapping_safety_abort_to_safety_action():
    """Verify SAFETY_ABORT maps to SAFETY ACTION."""
    ev = _make_dummy_evidence()
    eval_res = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.SAFETY_ABORT,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:delete-btn",
        action_type="click",
        evidence=ev,
        execution_status=GuardedExecutionStatus.DISALLOWED_RISK_TIER,
        limitations=(),
    )

    rec = adapt_functional_result_to_test_record(eval_res, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec["status"] == "SAFETY ACTION"
    assert rec["stable_id"] is None
    assert "Safety action: control was rejected by interaction safety policy" in rec["actual_result"]


def test_mapping_execution_control_event_to_diagnostic_with_action_history():
    """Verify EXECUTION_CONTROL_EVENT maps to DIAGNOSTIC preserving action history."""
    # 1. Action confirmed
    ev_conf = _make_dummy_evidence(action_confirmed=True)
    res_conf = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.EXECUTION_CONTROL_EVENT,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:btn",
        action_type="click",
        evidence=ev_conf,
        execution_status=GuardedExecutionStatus.RECONSTRUCTION_FAILED,
        limitations=(),
    )
    rec_conf = adapt_functional_result_to_test_record(res_conf, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec_conf["status"] == "DIAGNOSTIC"
    assert "click was confirmed executed" in rec_conf["actual_result"]

    # 2. Action prevented
    ev_prev = _make_dummy_evidence(action_attempted=False, action_confirmed=False)
    res_prev = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.EXECUTION_CONTROL_EVENT,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:btn",
        action_type="click",
        evidence=ev_prev,
        execution_status=GuardedExecutionStatus.TARGET_NOT_FOUND,
        limitations=(),
    )
    rec_prev = adapt_functional_result_to_test_record(res_prev, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec_prev["status"] == "DIAGNOSTIC"
    assert "action prevented by execution control" in rec_prev["actual_result"]


def test_mapping_cleanup_failure_after_confirmed_click_reports_separately():
    """Verify Requirement 6: Cleanup failure after confirmed click reports action & cleanup separately."""
    ev = _make_dummy_evidence(action_confirmed=True)
    eval_res = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:btn",
        action_type="click",
        evidence=ev,
        execution_status=GuardedExecutionStatus.CLEANUP_FAILED,
        limitations=(),
    )

    rec = adapt_functional_result_to_test_record(eval_res, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec["status"] == "DIAGNOSTIC"
    assert rec["stable_id"] is None
    # Must explicitly state confirmed click AND teardown verification failure separately
    assert "Cleanup failure after confirmed action" in rec["actual_result"]
    assert "click was confirmed executed" in rec["actual_result"]
    assert "teardown verification failed" in rec["actual_result"]
    assert "action never completed" not in rec["actual_result"].lower()


def test_mapping_unexpected_supported_failure_to_diagnostic_not_pass_or_finding():
    """Verify unexpected SUPPORTED_FAILURE input maps to DIAGNOSTIC and never PASS or customer finding."""
    ev = _make_dummy_evidence()
    eval_res = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.SUPPORTED_FAILURE,
        is_customer_defect=True,
        defect_category=DefectCategory.INERT_DISCLOSURE_CONTROL,
        target_control_id="0:btn",
        action_type="click",
        evidence=ev,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )

    rec = adapt_functional_result_to_test_record(eval_res, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec["status"] == "DIAGNOSTIC"
    assert rec["status"] != "PASS"
    assert rec["status"] != "FAIL"
    assert rec["stable_id"] is None
    assert rec["finding_id"] is None
    assert "unsupported outcome contract in this increment" in rec["actual_result"]


# ==============================================================================
# 2. Non-Creation of Customer Findings & Triage Invariants
# ==============================================================================

def test_no_customer_finding_or_triage_entry_created_for_any_verdict():
    """Verify that persisting results for ANY verdict never creates a customer finding or triage entry."""
    conn = create_in_memory_db()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('TestProj', 'http://127.0.0.1', 'Local', 'Tester')")
    c.execute("INSERT INTO runs (project_id, status) VALUES (1, 'COMPLETED')")
    run_id = 1

    verdicts = [
        FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
        FunctionalOutcomeVerdict.INCONCLUSIVE,
        FunctionalOutcomeVerdict.LIMITED_COVERAGE,
        FunctionalOutcomeVerdict.SAFETY_ABORT,
        FunctionalOutcomeVerdict.EXECUTION_CONTROL_EVENT,
        FunctionalOutcomeVerdict.DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE,
        FunctionalOutcomeVerdict.SUPPORTED_FAILURE,
    ]

    for v in verdicts:
        ev = _make_dummy_evidence()
        is_cust = (v == FunctionalOutcomeVerdict.SUPPORTED_FAILURE)
        cat = DefectCategory.INERT_DISCLOSURE_CONTROL if is_cust else None
        res = FunctionalEvaluationResult(
            verdict=v,
            is_customer_defect=is_cust,
            defect_category=cat,
            target_control_id="0:btn",
            action_type="click",
            evidence=ev,
            execution_status=GuardedExecutionStatus.EXECUTED,
            limitations=(),
        )
        test_id = persist_functional_evaluation_record(conn, res, run_id=run_id, target_url="http://127.0.0.1/faq")
        assert test_id is not None

    # Verify zero customer findings created in the database
    c.execute("SELECT count(*) FROM findings WHERE run_id=?", (run_id,))
    count = c.fetchone()[0]
    assert count == 0, f"Expected 0 findings, found {count}"

    # Load report using canonical reporting pipeline
    report = load_report(conn, run_id)
    assert report is not None
    assert report["customer_finding_count"] == 0
    assert len(report["findings"]) == 0
    assert len(report["triage"]) == 0
    assert len(report["review_queue"]) == 0
    assert len(report["unlinked_findings"]) == 0


# ==============================================================================
# 3. Privacy Sanitization & Redaction
# ==============================================================================

def test_privacy_sanitization_url_credentials_and_secrets_in_records():
    """Verify that credentials, passwords, sensitive query tokens, and path secrets are stripped from URLs."""
    raw_dirty_url = "http://admin:SECRET_PASSWORD_999@127.0.0.1:8080/dashboard?token=SYNTHETIC_QUERY_TOKEN_777&session=123"
    cleaned = sanitize_target_url(raw_dirty_url)

    assert "SECRET_PASSWORD_999" not in cleaned
    assert "admin:SECRET_PASSWORD_999@" not in cleaned
    assert "SYNTHETIC_QUERY_TOKEN_777" not in cleaned
    assert "dashboard" not in cleaned
    assert cleaned == "http://127.0.0.1:8080/"

    cleaned_with_ref = sanitize_target_url(raw_dirty_url, action_ref="act-12345678")
    assert cleaned_with_ref == "http://127.0.0.1:8080/ [action: act-12345678]"
    assert "dashboard" not in cleaned_with_ref

    ev = _make_dummy_evidence(
        expanded_before=False,
        expanded_after=True,
        controlled_element_visible_before=False,
        controlled_element_visible_after=True,
    )
    eval_res = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:faq-btn",
        action_type="click",
        evidence=ev,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )

    rec = adapt_functional_result_to_test_record(eval_res, run_id=1, target_url=raw_dirty_url)
    assert "SECRET_PASSWORD_999" not in rec["url"]
    assert "SYNTHETIC_QUERY_TOKEN_777" not in rec["url"]
    assert "dashboard" not in rec["url"]
    assert "SECRET_PASSWORD_999" not in rec["final_url"]
    assert "SYNTHETIC_QUERY_TOKEN_777" not in rec["final_url"]
    assert "dashboard" not in rec["final_url"]
    assert "SECRET_PASSWORD_999" not in rec["actual_result"]


def test_url_path_secrets_and_caller_labels_do_not_survive_persistence_or_reports():
    """Verify Reproduction 3: URL path secrets and arbitrary caller labels do not survive into database or reports.

    Reproduction from independent review:
    Persist a result using:
    http://127.0.0.1/reset/SYNTHETIC_PRIVATE_83XZ
    The sentinel must NOT survive in the tests table or load_report output.
    Defined privacy-safe URL representation: origin-only display plus opaque action reference.
    Validate viewport against an allowlist.
    Preserve useful action-level correlation without exposing raw control IDs, fingerprints, or URL secrets.
    """
    conn = create_in_memory_db()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Proj', 'http://127.0.0.1', 'Local', 'Tester')")
    c.execute("INSERT INTO runs (project_id, status) VALUES (1, 'COMPLETED')")
    run_id = 1

    path_secret = "SYNTHETIC_PRIVATE_83XZ"
    raw_secret_url = f"http://127.0.0.1/reset/{path_secret}"
    viewport_secret = "SYNTHETIC_SECRET_VIEWPORT_LABEL_99"
    control_id_secret = "SYNTHETIC_RAW_CTRL_ID_42"

    ev1 = _make_dummy_evidence(
        expanded_before=False,
        expanded_after=True,
        controlled_element_visible_before=False,
        controlled_element_visible_after=True,
        pre_state_fingerprint="SYNTHETIC_FINGERPRINT_BEFORE_11",
        post_state_fingerprint="SYNTHETIC_FINGERPRINT_AFTER_22",
    )
    res1 = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
        is_customer_defect=False,
        defect_category=None,
        target_control_id=control_id_secret,
        action_type="click",
        evidence=ev1,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )

    # Persist record with dirty inputs
    persist_functional_evaluation_record(
        conn,
        res1,
        run_id=run_id,
        target_url=raw_secret_url,
        viewport=viewport_secret,
    )

    # Persist second distinct action to verify correlation
    ev2 = _make_dummy_evidence(
        expanded_before=True,
        expanded_after=False,
        controlled_element_visible_before=True,
        controlled_element_visible_after=False,
    )
    res2 = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="second_action_control",
        action_type="click",
        evidence=ev2,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )
    persist_functional_evaluation_record(
        conn,
        res2,
        run_id=run_id,
        target_url=raw_secret_url,
        viewport="Desktop",
    )

    # 1. Inspect tests table in database
    c.execute("SELECT * FROM tests WHERE run_id=? ORDER BY id", (run_id,))
    db_rows = [dict(r) for r in c.fetchall()]
    assert len(db_rows) == 2

    for row in db_rows:
        # Crucial assertion: path secret NEVER enters database
        assert path_secret not in row["url"]
        assert path_secret not in (row.get("final_url") or "")
        assert path_secret not in row["actual_result"]
        assert "/reset" not in row["url"]

        # Caller viewport secret was clamped to allowlist
        assert viewport_secret not in row["viewport"]
        assert row["viewport"] in ALLOWLISTED_VIEWPORTS

        # Raw control ID and fingerprints not exposed
        assert control_id_secret not in row["url"]
        assert control_id_secret not in row["actual_result"]
        assert "SYNTHETIC_FINGERPRINT" not in row["actual_result"]

        # Origin display is preserved
        assert row["url"].startswith("http://127.0.0.1/")

    # Distinct action records have distinct correlation tokens
    assert db_rows[0]["url"] != db_rows[1]["url"]
    assert "[action: act-" in db_rows[0]["url"]
    assert "[action: act-" in db_rows[1]["url"]

    # 2. Inspect load_report output
    report = load_report(conn, run_id)
    report_dump = json.dumps(report)
    assert path_secret not in report_dump
    assert viewport_secret not in report_dump
    assert control_id_secret not in report_dump
    assert "SYNTHETIC_FINGERPRINT" not in report_dump

    # 3. Inspect text/markdown/html rendered exports
    text_out = render_text(report)
    md_out = render_markdown(report)
    html_out = render_html(report)

    for out in (text_out, md_out, html_out):
        assert path_secret not in out
        assert viewport_secret not in out
        assert control_id_secret not in out
        assert "SYNTHETIC_FINGERPRINT" not in out


# ==============================================================================
# 4. Mixed Actions & Truthful Run Outcome
# ==============================================================================

def test_incomplete_diagnostic_actions_propagate_to_limited_coverage_in_load_report():
    """Verify Reproduction 2: Incomplete C2 diagnostic actions propagate to LIMITED COVERAGE in load_report.

    Reproduction from independent review:
    Persist one valid PASS and one DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE / CLEANUP_FAILED result into a completed run.
    load_report previously returned outcome='COMPLETE', summary='No customer findings were recorded within completed coverage.'
    Now it must return:
    outcome='LIMITED COVERAGE'
    summary='No customer findings were confirmed, but evaluation was incomplete.'
    while preserving the required DIAGNOSTIC record and truthful confirmed-action history.

    Also tests mixed PASS plus:
    - execution-control failure (EXECUTION_CONTROL_EVENT)
    - unsupported outcome contract (SUPPORTED_FAILURE or invalid success evidence)
    - inconclusive/limited evaluation (INCONCLUSIVE or LIMITED_COVERAGE)
    """
    cases = [
        # (name, verdict, execution_status, evidence_kwargs, expected_status, expected_text_substring)
        (
            "cleanup_failure",
            FunctionalOutcomeVerdict.DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE,
            GuardedExecutionStatus.CLEANUP_FAILED,
            {"action_confirmed": True},
            "DIAGNOSTIC",
            "Cleanup failure after confirmed action",
        ),
        (
            "execution_control_failure",
            FunctionalOutcomeVerdict.EXECUTION_CONTROL_EVENT,
            GuardedExecutionStatus.RECONSTRUCTION_FAILED,
            {"action_confirmed": True},
            "DIAGNOSTIC",
            "Execution control diagnostic",
        ),
        (
            "unsupported_outcome_contract",
            FunctionalOutcomeVerdict.SUPPORTED_FAILURE,
            GuardedExecutionStatus.EXECUTED,
            {},
            "DIAGNOSTIC",
            "unsupported outcome contract in this increment",
        ),
        (
            "inconclusive_evaluation",
            FunctionalOutcomeVerdict.INCONCLUSIVE,
            GuardedExecutionStatus.EXECUTED,
            {"pre_observation_succeeded": False},
            "LIMITED COVERAGE",
            "Evaluation incomplete",
        ),
        (
            "limited_coverage_evaluation",
            FunctionalOutcomeVerdict.LIMITED_COVERAGE,
            GuardedExecutionStatus.EXECUTED,
            {"observation_status": "TIMED_OUT"},
            "LIMITED COVERAGE",
            "Coverage limited",
        ),
    ]

    for name, verdict, exec_status, ev_kwargs, expected_rec_status, expected_text in cases:
        conn = create_in_memory_db()
        c = conn.cursor()
        c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Proj', 'http://127.0.0.1', 'Local', 'Tester')")
        c.execute("INSERT INTO runs (project_id, status) VALUES (1, 'COMPLETED')")
        run_id = 1

        # 1. Action A: Valid PASS
        ev_pass = _make_dummy_evidence(expanded_before=False, expanded_after=True, controlled_element_visible_before=False, controlled_element_visible_after=True)
        res_pass = FunctionalEvaluationResult(
            verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
            is_customer_defect=False,
            defect_category=None,
            target_control_id="0:valid-btn",
            action_type="click",
            evidence=ev_pass,
            execution_status=GuardedExecutionStatus.EXECUTED,
            limitations=(),
        )
        persist_functional_evaluation_record(conn, res_pass, run_id=run_id, target_url="http://127.0.0.1/page")

        # 2. Action B: Incomplete/Diagnostic action
        is_cust = (verdict == FunctionalOutcomeVerdict.SUPPORTED_FAILURE)
        cat = DefectCategory.INERT_DISCLOSURE_CONTROL if is_cust else None
        ev_other = _make_dummy_evidence(**ev_kwargs)
        res_other = FunctionalEvaluationResult(
            verdict=verdict,
            is_customer_defect=is_cust,
            defect_category=cat,
            target_control_id="0:other-btn",
            action_type="click",
            evidence=ev_other,
            execution_status=exec_status,
            limitations=(),
        )
        persist_functional_evaluation_record(conn, res_other, run_id=run_id, target_url="http://127.0.0.1/page")

        # Verify database tests table
        c.execute("SELECT status, actual_result FROM tests WHERE run_id=? ORDER BY id", (run_id,))
        rows = c.fetchall()
        assert len(rows) == 2, f"Failed on case {name}"
        assert rows[0]["status"] == "PASS", f"Failed on case {name}"
        assert rows[1]["status"] == expected_rec_status, f"Failed on case {name}"
        assert expected_text in rows[1]["actual_result"], f"Failed on case {name}"

        # Verify load_report
        report = load_report(conn, run_id)
        assert report is not None
        assert report["customer_finding_count"] == 0
        assert report["outcome"] == "LIMITED COVERAGE", f"Failed on case {name}: got {report['outcome']}"
        assert report["summary"] == "No customer findings were confirmed, but evaluation was incomplete.", f"Failed on case {name}"
        assert "within completed coverage" not in report["summary"].lower()


def test_mixed_pass_and_limited_coverage_causes_honest_limited_run_outcome():
    """Verify Requirement 2 & 5: Mixed PASS and LIMITED COVERAGE actions produce honest LIMITED COVERAGE outcome."""
    conn = create_in_memory_db()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('TestProj', 'http://127.0.0.1', 'Local', 'Tester')")
    c.execute("INSERT INTO runs (project_id, status) VALUES (1, 'COMPLETED')")
    run_id = 1

    # 1. Action A: Successful expansion -> PASS
    ev_pass = _make_dummy_evidence(expanded_before=False, expanded_after=True)
    res_pass = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:working-btn",
        action_type="click",
        evidence=ev_pass,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )
    persist_functional_evaluation_record(conn, res_pass, run_id=run_id, target_url="http://127.0.0.1/faq")

    # 2. Action B: Inconclusive unverified disclosure -> LIMITED COVERAGE
    ev_inconc = _make_dummy_evidence()
    res_inconc = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.INCONCLUSIVE,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:unverified-btn",
        action_type="click",
        evidence=ev_inconc,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
        diagnostics={"deferred_defect_category": "CONTENT_NOT_REVEALED"},
    )
    persist_functional_evaluation_record(conn, res_inconc, run_id=run_id, target_url="http://127.0.0.1/faq")

    report = load_report(conn, run_id)
    assert report is not None
    assert report["stats"]["PASS"] == 1
    assert report["stats"]["LIMITED COVERAGE"] == 1
    assert report["customer_finding_count"] == 0

    # Critical contract assertion: The run outcome must be LIMITED COVERAGE, never COMPLETE
    assert report["outcome"] == "LIMITED COVERAGE"
    assert report["summary"] == "No customer findings were confirmed, but evaluation was incomplete."
    assert "within completed coverage" not in report["summary"].lower()


# ==============================================================================
# 5. Read-Only Idempotent Report Rendering
# ==============================================================================

def test_repeated_report_rendering_is_read_only_idempotent():
    """Verify that repeated report rendering creates zero duplicate records and is 100% read-only."""
    conn = create_in_memory_db()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('TestProj', 'http://127.0.0.1', 'Local', 'Tester')")
    c.execute("INSERT INTO runs (project_id, status) VALUES (1, 'COMPLETED')")
    run_id = 1

    ev = _make_dummy_evidence()
    res = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:faq-btn",
        action_type="click",
        evidence=ev,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )
    persist_functional_evaluation_record(conn, res, run_id=run_id, target_url="http://127.0.0.1/faq")

    c.execute("SELECT count(*) FROM tests WHERE run_id=?", (run_id,))
    init_test_count = c.fetchone()[0]

    # Render multiple times
    report1 = load_report(conn, run_id)
    txt1 = render_text(report1)
    md1 = render_markdown(report1)
    html1 = render_html(report1)

    report2 = load_report(conn, run_id)
    txt2 = render_text(report2)
    md2 = render_markdown(report2)
    html2 = render_html(report2)

    # Identical output
    assert txt1 == txt2
    assert md1 == md2
    assert html1 == html2

    # Database untouched
    c.execute("SELECT count(*) FROM tests WHERE run_id=?", (run_id,))
    after_test_count = c.fetchone()[0]
    assert init_test_count == after_test_count == 1


# ==============================================================================
# 6. Controlled Localhost End-to-End Test (Real Chromium)
# ==============================================================================

@pytest.mark.asyncio
async def test_controlled_localhost_end_to_end_oracle_to_adapter_to_reporting_real_chromium(oracle_server):
    """End-to-end integration: Real Chromium disclosure execution -> oracle -> adapter -> SQLite -> reporting.

    Validates:
    - Working FAQ evaluates to VERIFIED_SUCCESS and adapts to PASS.
    - Already-visible FAQ evaluates to non-success and adapts to LIMITED COVERAGE.
    - Persisted into real SQLite database; load_report produces honest LIMITED COVERAGE outcome.
    - Technical appendix includes both records cleanly without customer findings.
    """
    oracle = SafeDisclosureFunctionalOracle()
    executor = GuardedActionExecutor()

    conn = create_in_memory_db()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('E2EProj', 'http://127.0.0.1', 'Local', 'Tester')")
    c.execute("INSERT INTO runs (project_id, status) VALUES (1, 'COMPLETED')")
    run_id = 1

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        # 1. Action 1: Valid Working FAQ
        url_valid = f"{oracle_server.origin}/valid-faq"
        await source_page.goto(url_valid)
        intent_valid = await _derive_intent_from_page(source_page, "faq-btn")
        res_valid = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=url_valid,
            candidate_intent=intent_valid,
        )
        assert res_valid.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS

        test_id_1 = persist_functional_evaluation_record(conn, res_valid, run_id=run_id, target_url=url_valid)
        assert test_id_1 > 0

        # 2. Action 2: Already Visible FAQ (Reproduction 3)
        url_vis = f"{oracle_server.origin}/already-visible-disclosure"
        await source_page.goto(url_vis)
        intent_vis = await _derive_intent_from_page(source_page, "already-vis-btn")
        res_vis = await oracle.evaluate_disclosure_action(
            executor=executor,
            source_context=source_ctx,
            approved_target_url=url_vis,
            candidate_intent=intent_vis,
            timeout=1.0,
        )
        assert res_vis.verdict != FunctionalOutcomeVerdict.VERIFIED_SUCCESS

        test_id_2 = persist_functional_evaluation_record(conn, res_vis, run_id=run_id, target_url=url_vis)
        assert test_id_2 > test_id_1

        await browser.close()

    # Verify report assembly
    report = load_report(conn, run_id)
    assert report is not None
    assert report["total_checks"] == 2
    assert report["stats"]["PASS"] == 1
    assert report["stats"]["LIMITED COVERAGE"] == 1
    assert report["customer_finding_count"] == 0
    assert len(report["findings"]) == 0
    assert len(report["triage"]) == 0
    assert report["outcome"] == "LIMITED COVERAGE"
    assert report["summary"] == "No customer findings were confirmed, but evaluation was incomplete."

    # Verify rendering exports
    rendered_md = render_markdown(report, mode="full")
    assert "Record ID: 1" in rendered_md
    assert "Record ID: 2" in rendered_md
    assert "PASS" in rendered_md
    assert "LIMITED COVERAGE" in rendered_md
    assert "Customer findings: 0" in rendered_md


def test_pass_does_not_assert_whole_page_correctness():
    """Verify Requirement 1: A PASS describes only the specific supported disclosure transition, not whole-page correctness."""
    ev = _make_dummy_evidence(expanded_before=False, expanded_after=True)
    eval_res = FunctionalEvaluationResult(
        verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
        is_customer_defect=False,
        defect_category=None,
        target_control_id="0:faq-btn",
        action_type="click",
        evidence=ev,
        execution_status=GuardedExecutionStatus.EXECUTED,
        limitations=(),
    )

    rec = adapt_functional_result_to_test_record(eval_res, run_id=1, target_url="http://127.0.0.1/faq")
    assert rec["status"] == "PASS"
    # Scoped strictly to the disclosure transition
    assert "Verified safe disclosure expansion" in rec["actual_result"]
    assert "page fully evaluated" not in rec["actual_result"].lower()
    assert "whole page" not in rec["actual_result"].lower()
    assert "all controls" not in rec["actual_result"].lower()


def test_existing_reports_without_phase_c_records_remain_unchanged():
    """Verify Requirement 4: Existing report behavior for runs without Phase C records is preserved."""
    conn = create_in_memory_db()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('LegacyProj', 'http://127.0.0.1', 'Local', 'Tester')")
    c.execute("INSERT INTO runs (project_id, status) VALUES (1, 'COMPLETED')")
    run_id = 1

    # Standard crawl test record (Phase 2/3A style)
    c.execute("""
        INSERT INTO tests (run_id, url, expected_result, actual_result, status, viewport, stable_id)
        VALUES (1, 'http://127.0.0.1/home', 'Page loads', '200 OK', 'PASS', 'Desktop', NULL)
    """)
    conn.commit()

    report = load_report(conn, run_id)
    assert report is not None
    assert report["total_checks"] == 1
    assert report["stats"]["PASS"] == 1
    assert report["outcome"] == "COMPLETE"
    assert report["summary"] == "No customer findings were recorded within completed coverage."

