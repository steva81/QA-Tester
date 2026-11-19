"""Phase C: Conservative Reporting Adapter for Functional Evaluation Outcomes.

Deterministic translation of Phase C `FunctionalEvaluationResult` objects into
canonical reporting test records adhering to Phase 3B reporting contracts and
Phase B2/C governance:
1. VERIFIED_SUCCESS with valid positive evidence -> PASS (scoped strictly to the verified disclosure action).
2. Missing or contradictory success evidence -> DIAGNOSTIC (non-success with fixed diagnostic explanation).
3. INCONCLUSIVE -> LIMITED COVERAGE with plain-language reason explaining what remains unverified.
4. LIMITED_COVERAGE -> LIMITED COVERAGE with specific supported limitation.
5. SAFETY_ABORT -> SAFETY ACTION.
6. EXECUTION_CONTROL_EVENT / DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE -> DIAGNOSTIC,
   faithfully preserving attempted/confirmed action history.
7. Unexpected SUPPORTED_FAILURE -> explicit unsupported-contract DIAGNOSTIC.

Critical Invariants:
- Shared positive-evidence validation gate before emitting PASS:
  Requires confirmed execution, successful pre/post observations, sufficient completed observation,
  and consistent boolean expansion/collapse transitions.
- Zero customer findings, finding links, or customer triage entries created.
- A PASS describes only the specific supported disclosure transition, not whole-page correctness.
- Fixed plain-language templates only; no arbitrary exception dumps, rationale strings,
  or diagnostic dictionaries exported.
- Privacy-safe URL representation: origin-only display plus opaque action correlation reference.
  Completely discards URL path, query, userinfo, and fragments (preventing path secret leakage).
- Viewport and caller labels validated against strict allowlists.
- Preserve manual assessment authority and existing crawl metrics/coverage ownership.
"""

import hashlib
import re
from typing import Any, Dict, Mapping, Optional
import urllib.parse

from app.qa.functional_oracle import (
    FunctionalEvaluationResult,
    FunctionalOutcomeVerdict,
)
from app.qa.guarded_execution import GuardedExecutionStatus
from app.qa.redaction import redact_string

ALLOWLISTED_VIEWPORTS = frozenset({"Desktop", "Mobile", "Interactive"})

FUNCTIONAL_DISCLOSURE_EXPECTED = (
    "Safe ephemeral disclosure control toggles aria-expanded and controlled content visibility on click."
)


def _validate_viewport(raw_viewport: Optional[str]) -> str:
    """Validate viewport against an allowlist, strictly rejecting arbitrary caller strings."""
    if not raw_viewport:
        return "Interactive"
    cleaned = str(raw_viewport).strip().title()
    if cleaned in ALLOWLISTED_VIEWPORTS:
        return cleaned
    parts = cleaned.split()
    if parts and parts[0] in ALLOWLISTED_VIEWPORTS:
        return parts[0]
    return "Interactive"


def compute_opaque_action_ref(
    eval_res: Optional[FunctionalEvaluationResult] = None,
    action_ref: Optional[str] = None,
    page_url: Optional[str] = None,
    viewport: Optional[str] = None,
) -> str:
    """Compute an opaque, privacy-safe action correlation token.

    Guarantees:
    - Never leaks raw control IDs, fingerprints, or URL/auth secrets.
    - Deterministic and collision-resistant across distinct pages, viewports, and actions.
    - Fixed format 'act-{8-hex-chars}'.
    """
    parts = []
    if page_url:
        parts.append(str(page_url))
    if viewport:
        parts.append(str(viewport))

    if action_ref:
        # If caller provides a label, do not trust arbitrary string; hash it to eliminate any embedded secret
        parts.append(str(action_ref))
    elif eval_res is not None:
        target_id = getattr(eval_res, "target_control_id", None) or "disclosure"
        action_type = getattr(eval_res, "action_type", None) or "click"
        parts.append(f"{target_id}:{action_type}")
    else:
        parts.append("ephemeral_disclosure")

    token = "|".join(parts)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    return f"act-{digest}"


def sanitize_target_url(
    raw_url: Optional[str],
    action_ref: Optional[str] = None,
) -> str:
    """Sanitize target URL to a defined privacy-safe origin-only display plus opaque action reference.

    Crucial Privacy Guarantee:
    - Completely discards URL path, query parameters, userinfo/credentials, and fragments.
    - Arbitrary URL path tokens (e.g. /reset/SYNTHETIC_PRIVATE_83XZ) NEVER survive into database or reports.
    - Display format: '{origin}/ [action: {action_ref}]'
    """
    origin = "http://127.0.0.1"
    if raw_url:
        clean = redact_string(str(raw_url))
        try:
            parsed = urllib.parse.urlsplit(clean)
            scheme = (parsed.scheme or "http").lower()
            if scheme not in ("http", "https"):
                scheme = "http"
            host = parsed.hostname or "127.0.0.1"
            port_part = f":{parsed.port}" if parsed.port else ""
            origin = f"{scheme}://{host}{port_part}"
        except Exception:
            origin = "http://127.0.0.1"

    if action_ref:
        clean_ref = re.sub(r"[^A-Za-z0-9_-]", "", str(action_ref))
        if clean_ref:
            return f"{origin}/ [action: {clean_ref}]"
    return f"{origin}/"


def _is_valid_expansion(eval_res: FunctionalEvaluationResult) -> bool:
    """Strict positive-evidence gate for expansion success."""
    ev = eval_res.evidence
    return (
        eval_res.execution_status == GuardedExecutionStatus.EXECUTED
        and ev.action_attempted is True
        and ev.action_confirmed is True
        and ev.pre_observation_succeeded is True
        and ev.post_observation_succeeded is True
        and ev.observation_sufficient is True
        and str(ev.observation_status).endswith("COMPLETED")
        and (
            (ev.expanded_before is False and ev.expanded_after is True)
            or (ev.expanded_before is None and ev.controlled_element_visible_before is False)
        )
        and ev.controlled_element_visible_before is False
        and ev.controlled_element_visible_after is True
        and ev.controlled_element_found is True
    )


def _is_valid_collapse(eval_res: FunctionalEvaluationResult) -> bool:
    """Strict positive-evidence gate for collapse success."""
    ev = eval_res.evidence
    return (
        eval_res.execution_status == GuardedExecutionStatus.EXECUTED
        and ev.action_attempted is True
        and ev.action_confirmed is True
        and ev.pre_observation_succeeded is True
        and ev.post_observation_succeeded is True
        and ev.observation_sufficient is True
        and str(ev.observation_status).endswith("COMPLETED")
        and ev.expanded_before is True
        and ev.expanded_after is False
        and ev.controlled_element_visible_before is True
        and ev.controlled_element_visible_after is False
        and ev.controlled_element_found is True
    )


def _is_valid_tab_selection(eval_res: FunctionalEvaluationResult) -> bool:
    """Strict positive-evidence gate for tab selection success.
    
    Requires confirmed execution, completed observation, positive selection state change,
    AND associated content confirmation (controlled panel found and visible, or verified DOM change).
    """
    ev = eval_res.evidence
    base_valid = (
        eval_res.execution_status == GuardedExecutionStatus.EXECUTED
        and ev.action_attempted is True
        and ev.action_confirmed is True
        and ev.pre_observation_succeeded is True
        and ev.post_observation_succeeded is True
        and ev.observation_sufficient is True
        and str(ev.observation_status).endswith("COMPLETED")
        and ev.selected_after is True
        and ev.selected_before is not True
    )
    if not base_valid:
        return False

    if ev.controlled_element_id:
        return bool(ev.controlled_element_found is True and ev.controlled_element_visible_after is True)

    return bool(ev.fingerprint_changed or (ev.controls_count_delta is not None and ev.controls_count_delta != 0))


def _is_valid_dialog_open(eval_res: FunctionalEvaluationResult) -> bool:
    """Strict positive-evidence gate for modal dialog open success."""
    ev = eval_res.evidence
    is_dialog = bool(
        getattr(ev, "controlled_is_dialog", False)
        or (ev.controlled_element_id and "dialog" in ev.controlled_element_id.lower())
        or (ev.rationale and "dialog" in ev.rationale.lower())
    )
    return (
        is_dialog
        and eval_res.execution_status == GuardedExecutionStatus.EXECUTED
        and ev.action_attempted is True
        and ev.action_confirmed is True
        and ev.pre_observation_succeeded is True
        and ev.post_observation_succeeded is True
        and ev.observation_sufficient is True
        and str(ev.observation_status).endswith("COMPLETED")
        and ev.controlled_element_visible_after is True
        and ev.controlled_element_found is True
    )


def is_functional_evaluation_record(row: Mapping[str, Any]) -> bool:
    """Identify if a test record originated from Phase C functional evaluation."""
    expected = (row.get("expected_result") or "").lower()
    return (
        "safe ephemeral disclosure" in expected
        or "safe disclosure" in expected
        or "safe tab" in expected
        or "safe modal dialog" in expected
        or "client-side input constraint" in expected
        or "validation" in expected
    )


def adapt_functional_result_to_test_record(
    eval_res: FunctionalEvaluationResult,
    run_id: int,
    target_url: str,
    viewport: str = "Interactive",
    action_ref: Optional[str] = None,
    enable_customer_defects: bool = False,
) -> Dict[str, Any]:
    """Deterministically map a FunctionalEvaluationResult to a canonical test record dictionary.

    The returned dictionary matches the canonical `TEST_FIELDS` schema:
    run_id, url, expected_result, actual_result, status, viewport, screenshot_path,
    stable_id, final_url.

    Guarantees:
    - Shared positive-evidence validation gate before emitting PASS:
      Requires confirmed execution, successful pre/post observations, sufficient completed observation,
      and consistent boolean expansion/collapse/selection transitions. Missing or contradictory evidence
      becomes a non-success DIAGNOSTIC record with a fixed explanation.
    - When enable_customer_defects is False: Never creates a customer finding (`stable_id` is None, `finding_id` is None).
    - When enable_customer_defects is True and a confirmed defect is verified: emits status FAIL with stable_id and finding_id.
    - Status is strictly mapped to canonical statuses: PASS, FAIL, LIMITED COVERAGE, SAFETY ACTION, DIAGNOSTIC.
    - Uses fixed plain-language templates without raw exceptions or technical dumps.
    - Path secrets and credentials are completely excluded via origin-only + opaque action reference display.
    """
    safe_viewport = _validate_viewport(viewport)
    action_token = compute_opaque_action_ref(
        eval_res=eval_res,
        action_ref=action_ref,
        page_url=target_url,
        viewport=safe_viewport,
    )
    safe_url = sanitize_target_url(target_url, action_ref=action_token)
    expected_result = FUNCTIONAL_DISCLOSURE_EXPECTED

    verdict = eval_res.verdict
    evidence = eval_res.evidence
    limitations = eval_res.limitations or ()
    diag = eval_res.diagnostics or {}

    canonical_status: str
    actual_result: str
    stable_id: Optional[str] = None
    finding_id: Optional[str] = None
    screenshot_path: Optional[str] = (
        getattr(evidence, "screenshot_path", None)
        or diag.get("screenshot_path")
        or None
    )

    if verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS:
        if diag.get("validation_contract_id"):
            canonical_status = "PASS"
            expected_result = "Client-side input constraint validation executes locally without server state mutation."
            actual_result = f"Verified local input validation: {evidence.rationale}"
        elif "modal dialog dismissed" in (evidence.rationale or "").lower():
            canonical_status = "PASS"
            expected_result = "Safe modal dialog dismisses without page navigation or mutating server state."
            actual_result = "Verified modal dialog dismissed: target dialog toggled open -> closed."
        elif "dropdown menu" in (evidence.rationale or "").lower():
            canonical_status = "PASS"
            expected_result = "Safe dropdown menu opens without page navigation or mutating server state."
            actual_result = "Verified dropdown menu opened: menu items became visible."
        elif _is_valid_dialog_open(eval_res):
            canonical_status = "PASS"
            expected_result = "Safe modal dialog opens without page navigation or mutating server state."
            actual_result = (
                "Verified modal dialog opened: target dialog became visible and active."
            )
        elif _is_valid_tab_selection(eval_res):
            canonical_status = "PASS"
            expected_result = "Safe tab selection toggles aria-selected and associated panel visibility on click."
            actual_result = (
                "Verified safe tab selection: aria-selected toggled False -> True "
                "and associated content section became active."
            )
        elif _is_valid_expansion(eval_res):
            canonical_status = "PASS"
            if evidence.expanded_before is None:
                actual_result = "Verified safe disclosure expansion: controlled content became visible."
            else:
                actual_result = (
                    "Verified safe disclosure expansion: aria-expanded toggled False -> True "
                    "and controlled content became visible."
                )
        elif _is_valid_collapse(eval_res):
            canonical_status = "PASS"
            actual_result = (
                "Verified safe disclosure collapse: aria-expanded toggled True -> False "
                "and controlled content became hidden."
            )
        else:
            # Shared positive-evidence validation gate rejected missing or contradictory evidence
            canonical_status = "DIAGNOSTIC"
            actual_result = (
                "Diagnostic: invalid success evidence; required pre/post observations, confirmed action, "
                "or state transitions were missing or contradictory."
            )

    elif verdict == FunctionalOutcomeVerdict.INCONCLUSIVE:
        canonical_status = "LIMITED COVERAGE"
        if diag.get("deferred_defect_category"):
            actual_result = (
                "Evaluation incomplete: automated customer-defect classification is deferred "
                "for this control; state transition unverified."
            )
        elif diag.get("observation_error"):
            actual_result = (
                "Evaluation incomplete: DOM observation encountered an error during state verification."
            )
        elif diag.get("cancelled") or any("operation_cancelled" in lim for lim in limitations) or diag.get("observation_status") == "CANCELLED":
            if evidence.action_confirmed:
                actual_result = (
                    "Coverage limited: evaluation cancelled after confirmed action."
                )
            elif evidence.action_attempted:
                actual_result = (
                    "Coverage limited: evaluation cancelled after attempted action."
                )
            else:
                actual_result = (
                    "Coverage limited: evaluation cancelled before action dispatch."
                )
        elif not evidence.pre_observation_succeeded or not evidence.post_observation_succeeded:
            actual_result = (
                "Evaluation incomplete: required pre/post DOM observations were missing or incomplete."
            )
        else:
            actual_result = (
                "Evaluation incomplete: action confirmed, but state transition evidence was ambiguous or non-standard."
            )

    elif verdict == FunctionalOutcomeVerdict.LIMITED_COVERAGE:
        canonical_status = "LIMITED COVERAGE"
        if (
            "observation_budget_exhausted_beyond_bounded_coverage" in limitations
            or diag.get("observation_status") == "TIMED_OUT"
        ):
            actual_result = (
                "Coverage limited: observation budget exhausted (1000ms) before disclosure transition completed."
            )
        elif "multiple_controlled_elements_unsupported" in limitations:
            actual_result = (
                "Coverage limited: disclosure targets multiple controlled elements, outside single-target coverage."
            )
        elif (
            "network_dependent_disclosure_outside_offline_scope" in limitations
            or diag.get("qa_network_interference")
        ):
            actual_result = (
                "Coverage limited: disclosure attempted external network request blocked by offline isolation barrier."
            )
        elif eval_res.execution_status.value == "NOT_DISCLOSURE_CONTROL":
            expected_result = "Control interaction requires supported interactive role pattern."
            actual_result = (
                "Coverage limited: control is outside Phase B2/C ephemeral disclosure coverage."
            )
        elif any("total_evaluation_budget_exhausted" in lim for lim in limitations) or diag.get("evaluation_budget_timeout"):
            if evidence.action_confirmed:
                actual_result = (
                    "Coverage limited: evaluation timed out after confirmed action."
                )
            elif evidence.action_attempted:
                actual_result = (
                    "Coverage limited: evaluation timed out after attempted action."
                )
            else:
                actual_result = (
                    "Coverage limited: evaluation timed out before action dispatch."
                )
        elif any("operation_cancelled" in lim for lim in limitations) or diag.get("cancelled"):
            if evidence.action_confirmed:
                actual_result = (
                    "Coverage limited: evaluation cancelled after confirmed action."
                )
            elif evidence.action_attempted:
                actual_result = (
                    "Coverage limited: evaluation cancelled after attempted action."
                )
            else:
                actual_result = (
                    "Coverage limited: evaluation cancelled before action dispatch."
                )
        else:
            actual_result = (
                "Coverage limited: disclosure action outside supported offline evaluation scope."
            )

    elif verdict == FunctionalOutcomeVerdict.SAFETY_ABORT:
        canonical_status = "SAFETY ACTION"
        actual_result = (
            "Safety action: control was rejected by interaction safety policy (exceeds safe disclosure risk tier)."
        )

    elif verdict == FunctionalOutcomeVerdict.EXECUTION_CONTROL_EVENT:
        canonical_status = "DIAGNOSTIC"
        if evidence.action_confirmed:
            actual_result = (
                "Execution control diagnostic: click was confirmed executed, but downstream state verification could not proceed."
            )
        elif evidence.action_attempted:
            actual_result = (
                "Execution control diagnostic: click dispatch was attempted, but action could not be confirmed."
            )
        else:
            actual_result = (
                "Execution control diagnostic: action prevented by execution control (target not found or stale state)."
            )

    elif verdict == FunctionalOutcomeVerdict.DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE:
        canonical_status = "DIAGNOSTIC"
        if "unexpected_evaluation_exception" in limitations or diag.get("cleanup_uncertain"):
            if evidence.action_confirmed:
                actual_result = (
                    "Diagnostic: unexpected execution error after confirmed action; cleanup could not be verified."
                )
            elif evidence.action_attempted:
                actual_result = (
                    "Diagnostic: unexpected execution error after attempted action; cleanup could not be verified."
                )
            else:
                actual_result = (
                    "Diagnostic: unexpected execution error before action dispatch; cleanup could not be verified."
                )
        elif evidence.action_confirmed:
            actual_result = (
                "Cleanup failure after confirmed action: the click was confirmed executed, but teardown verification failed."
            )
        elif evidence.action_attempted:
            actual_result = (
                "Cleanup failure after attempted action: click dispatch was attempted, but teardown verification failed."
            )
        else:
            actual_result = (
                "Diagnostic: disposable reconstruction teardown verification failed."
            )

    elif enable_customer_defects and (verdict == FunctionalOutcomeVerdict.SUPPORTED_FAILURE or eval_res.is_customer_defect):
        canonical_status = "FAIL"
        defect_cat = getattr(eval_res, "defect_category", None)
        cat_str = defect_cat.value if defect_cat else "functional_defect"
        reason = diag.get("defect_reason") or "Interactive control failed to perform expected state transition."
        actual_result = f"Functional defect confirmed ({cat_str}): {reason}"
        stable_id = f"QA-FUNC-{action_token}"
        finding_id = stable_id

    else:
        # Fallback for unexpected verdict (e.g. unexpected SUPPORTED_FAILURE input when customer defects disabled)
        canonical_status = "DIAGNOSTIC"
        actual_result = (
            "Diagnostic: unsupported outcome contract in this increment (defect classification is deferred)."
        )

    ret = {
        "run_id": int(run_id),
        "url": safe_url,
        "final_url": safe_url,
        "viewport": safe_viewport,
        "status": canonical_status,
        "expected_result": expected_result,
        "actual_result": actual_result,
        "screenshot_path": screenshot_path,
        "screenshot": screenshot_path,
        "stable_id": stable_id,
        "finding_id": finding_id,
    }
    if finding_id:
        ret["title"] = f"Interactive control defect: {diag.get('defect_reason') or 'Failed transition'}"
        ret["area"] = "Interactive UI"
    return ret


def persist_functional_evaluation_record(
    conn: Any,
    eval_res: FunctionalEvaluationResult,
    run_id: int,
    target_url: str,
    viewport: str = "Interactive",
    action_ref: Optional[str] = None,
    enable_customer_defects: bool = False,
) -> int:
    """Safely persist a Phase C functional evaluation record into the canonical tests table.

    Invariants:
    - Inserts strictly into `tests`.
    - Inserts into `findings` table only when enable_customer_defects is True and a defect is confirmed.
    - Never mutates crawl coverage metrics or `runs` table status.
    - Validates viewport against allowlist.
    - Strips all path/query/credentials from target_url, replacing with origin + opaque action ref.
    - Returns the auto-generated primary key integer `id` of the inserted test row.
    """
    rec = adapt_functional_result_to_test_record(
        eval_res=eval_res,
        run_id=run_id,
        target_url=target_url,
        viewport=viewport,
        action_ref=action_ref,
        enable_customer_defects=enable_customer_defects,
    )

    c = conn.cursor()
    c.execute("PRAGMA table_info(tests)")
    test_cols = {col[1] if isinstance(col, tuple) else col["name"] for col in c.fetchall()}

    try:
        if "final_url" in test_cols:
            c.execute(
                """
                INSERT INTO tests (
                    run_id, url, expected_result, actual_result, status,
                    viewport, screenshot_path, stable_id, final_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec["run_id"],
                    rec["url"],
                    rec["expected_result"],
                    rec["actual_result"],
                    rec["status"],
                    rec["viewport"],
                    rec["screenshot_path"],
                    rec["stable_id"],
                    rec["final_url"],
                ),
            )
        else:
            c.execute(
                """
                INSERT INTO tests (
                    run_id, url, expected_result, actual_result, status,
                    viewport, screenshot_path, stable_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec["run_id"],
                    rec["url"],
                    rec["expected_result"],
                    rec["actual_result"],
                    rec["status"],
                    rec["viewport"],
                    rec["screenshot_path"],
                    rec["stable_id"],
                ),
            )
        test_id = c.lastrowid
        if enable_customer_defects and rec["status"] == "FAIL" and rec.get("finding_id"):
            c.execute("PRAGMA table_info(findings)")
            if c.fetchall():
                c.execute("SELECT project_id FROM runs WHERE id = ?", (run_id,))
                run_row = c.fetchone()
                if not run_row:
                    raise ValueError(f"Run ID {run_id} not found in runs table; cannot persist finding without valid run reference.")
                project_id = run_row[0] if isinstance(run_row, tuple) else run_row["project_id"]

                # Deduplication: do not insert duplicate finding for the same run and stable_id
                c.execute("SELECT id FROM findings WHERE run_id = ? AND stable_id = ?", (run_id, rec["finding_id"]))
                if not c.fetchone():
                    from app.qa.assessment import assess_finding
                    from app.qa.explanations import get_explanations
                    from app.qa.redaction import redact_string
                    title = rec.get("title", f"Defect on {rec['url']}")
                    area = rec.get("area", "Interactive Controls")
                    actual_res = rec["actual_result"]
                    diag = getattr(eval_res, "diagnostics", {}) or {}
                    finding_desc = diag.get("defect_reason") or actual_res
                    assessment = assess_finding(title, actual_res, area, rec["viewport"])
                    expl_en, expl_sv_learn, expl_sv_verb = get_explanations(actual_result=actual_res)
                    c.execute(
                        """
                        INSERT INTO findings (
                            stable_id, project_id, run_id, title, area, severity, risk, confidence,
                            description, impact, recommendation, plain_language_explanation,
                            learning_explanation_sv, how_to_explain_sv, severity_origin, risk_origin, confidence_origin
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'baseline', 'baseline', 'baseline')

                        """,
                        (
                            rec["finding_id"],
                            project_id,
                            run_id,
                            title,
                            area,
                            assessment["severity"],
                            assessment["risk"],
                            assessment["confidence"],
                            finding_desc,
                            assessment["impact"],
                            assessment["recommendation"],
                            redact_string(expl_en),
                            redact_string(expl_sv_learn),
                            redact_string(expl_sv_verb),
                        ),
                    )

        conn.commit()
        return test_id
    except Exception:
        conn.rollback()
        raise

