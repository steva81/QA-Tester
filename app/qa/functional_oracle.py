"""Phase C: Functional Oracles for Ephemeral Disclosure Actions (First Bounded Increment).

Evaluates post-action state transitions following guarded execution of safe disclosure controls.
Distinguishes:
1. Verified Functional Success (target disclosure behaved correctly with positive DOM evidence)
2. Inconclusive Outcomes (ambiguous, missing, or insufficient state evidence; or deferred defect classification)
3. Non-Defect Events:
   - Execution-control failures (stale state, dispatch errors, timeouts)
   - Safety policy actions (risky/mutating control rejections)
   - Limited coverage (controls outside safe disclosure policy, network dependencies, budget exhausted)
   - Diagnostic/infrastructure issues (teardown verification failures)

Scope Limitation (First Phase C Increment):
Automatic customer-defect classification is intentionally DEFERRED in this increment.
Public evaluation paths never return SUPPORTED_FAILURE or is_customer_defect=True.
Potential defects (inert controls, dangling aria-controls, unrevealed/uncollapsed content,
and page script errors) evaluate to honest non-defect INCONCLUSIVE outcomes with explicit
rationale documenting that automatic defect classification is deferred.

Contract Invariant:
is_customer_defect is strictly True IF AND ONLY IF verdict is SUPPORTED_FAILURE.
Since SUPPORTED_FAILURE is not emitted in this increment, is_customer_defect is strictly False.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple
import urllib.parse

from app.qa.candidate_intent_graph import CandidateActionIntent
from app.qa.guarded_execution import (
    GuardedActionExecutor,
    GuardedExecutionResult,
    GuardedExecutionStatus,
    _resolve_target_frame,
)
from app.qa.interaction_policy import RiskClassification
from app.qa.redaction import redact_string
from app.qa.validation_contract import (
    ValidationContractResult,
    ValidationVerdict,
)


class FunctionalOutcomeVerdict(str, Enum):
    """Authoritative outcome verdicts for Phase C functional evaluation."""

    # 1. Action executed; target disclosure functioned correctly with positive evidence
    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"

    # 2. Action executed; target disclosure demonstrably failed (customer defect)
    # NOTE: Classification deferred in this first increment; retained for enum/API compatibility.
    SUPPORTED_FAILURE = "SUPPORTED_FAILURE"

    # 3. Action executed; evidence ambiguous, missing, or insufficient to verify; or deferred defect
    INCONCLUSIVE = "INCONCLUSIVE"

    # 4. Action prevented or failed by B2 execution control (stale state, target not found, dispatch error)
    EXECUTION_CONTROL_EVENT = "EXECUTION_CONTROL_EVENT"

    # 5. Action rejected by interaction safety policy (risky/mutating control)
    SAFETY_ABORT = "SAFETY_ABORT"

    # 6. Action outside supported safe disclosure scope (non-disclosure control, budget exhausted, network)
    LIMITED_COVERAGE = "LIMITED_COVERAGE"

    # 7. QA infrastructure or teardown verification issue (e.g. cleanup failure)
    DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE = "DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE"


class DefectCategory(str, Enum):
    """Specific categories of customer functional defects."""

    INERT_DISCLOSURE_CONTROL = "INERT_DISCLOSURE_CONTROL"
    DANGLING_ARIA_CONTROLS = "DANGLING_ARIA_CONTROLS"
    CONTENT_NOT_REVEALED = "CONTENT_NOT_REVEALED"
    CONTENT_NOT_COLLAPSED = "CONTENT_NOT_COLLAPSED"
    PAGE_SCRIPT_ERROR = "PAGE_SCRIPT_ERROR"
    VALIDATION_LOGIC_ERROR = "VALIDATION_LOGIC_ERROR"


class ObservationStatus(str, Enum):
    """Explicit status of the post-action observation."""

    COMPLETED = "COMPLETED"                 # Complete supported transition observed and positively verified
    TIMED_OUT = "TIMED_OUT"                 # Incomplete / observation budget exhausted
    FAILED = "FAILED"                       # Observation failed with an exception / evaluate error
    CANCELLED = "CANCELLED"                 # Observation aborted due to cancellation request
    MISSING = "MISSING"                     # No DOM observation evidence provided or unsupported
    SETTLED_UNVARYING = "SETTLED_UNVARYING" # Legacy alias for unvarying state


@dataclass(frozen=True)
class ObservationResult:
    """Explicit result of bounded asynchronous DOM observation."""

    status: ObservationStatus
    action_settled: bool
    duration_ms: float
    pending_async_count: int = 0
    provenance: str = "disposable_reconstruction"
    observed_transition: Optional[str] = None
    error_type: Optional[str] = None


DOM_OBSERVATION_KEYS = frozenset({
    "controlled_element_id",
    "controlled_element_found",
    "controlled_element_visible_before",
    "controlled_element_visible_after",
    "observation_status",
    "observation_result",
    "observation_provenance",
    "pre_observation_succeeded",
    "post_observation_succeeded",
})


LOCKDOWN_NETWORK_PATTERNS = (
    "failed to fetch",
    "net::err_",
    "networkerror",
    "aborterror",
    "network request failed",
    "load failed",
    "api disabled in isolated context",
    "blocked_by_client",
    "network_io_suspended",
)

BACKGROUND_ACTIVITY_PATTERNS = (
    "resizeobserver",
    "mutationobserver",
    "intersectionobserver",
    "analytics",
    "telemetry",
    "beacon",
    "unrelated background",
    "background timer",
    "worker",
    "heartbeat",
    "ping",
)


ALLOWLISTED_JS_ERROR_TYPES = frozenset({
    "Error",
    "TypeError",
    "ReferenceError",
    "SyntaxError",
    "RangeError",
    "URIError",
    "EvalError",
    "DOMException",
})


def safe_error_type(raw_type: Optional[str]) -> str:
    """Map any error type string strictly to an allowlisted JS error category."""
    if not raw_type:
        return "Error"
    clean = re.sub(r"[^A-Za-z0-9_]", "", str(raw_type))
    if clean in ALLOWLISTED_JS_ERROR_TYPES:
        return clean
    for allowed in ("TypeError", "ReferenceError", "SyntaxError", "RangeError", "URIError", "EvalError", "DOMException"):
        if clean.endswith(allowed):
            return allowed
    return "Error"


def extract_safe_error_type(err_obj: Any) -> str:
    """Extract an allowlisted JS error category from an exception, guaranteeing no arbitrary text."""
    name_attr = getattr(err_obj, "name", None)
    if name_attr:
        safe_name = safe_error_type(str(name_attr))
        if safe_name != "Error" or str(name_attr) == "Error":
            return safe_name

    raw = str(err_obj) if err_obj is not None else ""
    first_part = raw.split(":", 1)[0].strip() if ":" in raw else raw.strip()
    match = re.search(r'\b([A-Za-z0-9_]+)\b', first_part)
    if match:
        return safe_error_type(match.group(1))
    return "Error"


def sanitize_error_text(text: Optional[str]) -> str:
    """Sanitize exception text to strictly prevent credential or token leakage."""
    if not text:
        return ""
    cleaned = re.sub(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+:[^/@\s]+@', r'\1', str(text))
    cleaned = re.sub(
        r'([?&](?:token|password|secret|key|api_key|access_token|auth|session|session_id|sig|signature)=)[^&\s"\'\']+',
        r'\1[REDACTED]',
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = redact_string(cleaned)
    cleaned = re.sub(
        r'((?:password|secret|token|api_key|access_token|key|credential|private_key|auth)\s*[:=]\s*["\']?)[^"\'\s,;]+(["\']?)',
        r'\1[REDACTED]\2',
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r'\b(Bearer\s+)[A-Za-z0-9_\-\.]+\b', r'\1[REDACTED]', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b([A-Za-z0-9_\-\.]+):([A-Za-z0-9_\-\.]{6,})\b', r'\1:[REDACTED]', cleaned)
    return cleaned


def is_network_or_lockdown_artifact(err_text: str) -> bool:
    """Check if an error string is attributable to the QA network barrier lockdown."""
    low = err_text.lower()
    return any(p in low for p in LOCKDOWN_NETWORK_PATTERNS)


def is_unrelated_background_error(err_text: str) -> bool:
    """Check if an error string is attributable to unrelated background activity rather than the disclosure click."""
    low = err_text.lower()
    return any(p in low for p in BACKGROUND_ACTIVITY_PATTERNS)


@dataclass(frozen=True)
class StructuredPageError:
    """Privacy-safe structured page error representation."""

    error_type: str
    sanitized_message: Optional[str] = None
    is_network_or_lockdown_artifact: bool = False
    is_unrelated_background_activity: bool = False
    occurred_during_click: bool = False

    def __post_init__(self):
        safe_type = safe_error_type(self.error_type)
        object.__setattr__(self, "error_type", safe_type)

        if self.sanitized_message:
            msg_str = str(self.sanitized_message)
            if not self.is_network_or_lockdown_artifact and is_network_or_lockdown_artifact(msg_str):
                object.__setattr__(self, "is_network_or_lockdown_artifact", True)
            if not self.is_unrelated_background_activity and is_unrelated_background_error(msg_str):
                object.__setattr__(self, "is_unrelated_background_activity", True)

        # Strictly drop arbitrary message text - NEVER export or retain arbitrary exception text
        object.__setattr__(self, "sanitized_message", None)

    def __repr__(self) -> str:
        return f"StructuredPageError(type={self.error_type!r}, occurred_during_click={self.occurred_during_click})"


@dataclass(frozen=True)
class FunctionalEvidence:
    """Detailed evidence supporting the functional evaluation verdict."""

    action_attempted: bool
    action_confirmed: bool
    expanded_before: Optional[bool]
    expanded_after: Optional[bool]
    state_toggled: bool
    controls_count_delta: Optional[int]
    fingerprint_changed: bool
    pre_state_fingerprint: Optional[str]
    post_state_fingerprint: Optional[str]
    controlled_element_id: Optional[str] = None
    controlled_element_found: Optional[bool] = None
    controlled_element_visible_before: Optional[bool] = None
    controlled_element_visible_after: Optional[bool] = None
    selected_before: Optional[bool] = None
    selected_after: Optional[bool] = None
    screenshot_path: Optional[str] = None
    pre_observation_succeeded: bool = False
    post_observation_succeeded: bool = False
    page_script_errors: Tuple[str, ...] = field(default_factory=tuple)
    observation_sufficient: bool = False
    observation_status: Optional[str] = None
    rationale: str = ""


@dataclass(frozen=True)
class FunctionalEvaluationResult:
    """Immutable result of Phase C functional evaluation."""

    verdict: FunctionalOutcomeVerdict
    is_customer_defect: bool
    defect_category: Optional[DefectCategory]
    target_control_id: Optional[str]
    action_type: Optional[str]
    evidence: FunctionalEvidence
    execution_status: GuardedExecutionStatus
    limitations: Tuple[str, ...]
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Enforce contract invariant: is_customer_defect is True IF AND ONLY IF verdict is SUPPORTED_FAILURE
        expected_defect = (self.verdict == FunctionalOutcomeVerdict.SUPPORTED_FAILURE)
        if self.is_customer_defect != expected_defect:
            raise ValueError(
                f"Contract invariant violation: is_customer_defect must be {expected_defect} "
                f"for verdict {self.verdict}, got {self.is_customer_defect}"
            )


async def _wait_for_async_disclosure_transition(
    target_frame: Any,
    raw_id: str,
    pre_expanded: Optional[str],
    pre_vis: Optional[bool],
    cancellation_token: Optional[Any],
    max_wait_ms: int = 1000,
    poll_interval_ms: int = 25,
    pre_selected: Optional[str] = None,
    target_locator: Optional[Any] = None,
    pre_dialog_open: Optional[bool] = None,
) -> ObservationResult:
    """Bounded observation wait for supported asynchronous disclosure, dialog, and tab transitions.

    Polls target frame or locator until the bounded deadline.
    Distinguishes:
    - completed and positively verified (consistent semantic AND content visibility completion);
    - incomplete / budget exhausted;
    - failed observation;
    - cancelled.

    Does NOT use speculative timer monkeypatching or pending-async heuristics.
    Attribute-first and visibility-first partial transitions are allowed to finish within budget.
    """
    if (not target_frame and not target_locator) or not raw_id:
        return ObservationResult(
            status=ObservationStatus.FAILED,
            action_settled=False,
            duration_ms=0.0,
            error_type="TARGET_FRAME_OR_ID_MISSING",
        )

    if cancellation_token and cancellation_token.is_cancelled():
        return ObservationResult(
            status=ObservationStatus.CANCELLED,
            action_settled=False,
            duration_ms=0.0,
        )

    start_time = time.monotonic()
    deadline = start_time + (max_wait_ms / 1000.0)
    poll_s = poll_interval_ms / 1000.0

    while time.monotonic() < deadline:
        if cancellation_token and cancellation_token.is_cancelled():
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            return ObservationResult(
                status=ObservationStatus.CANCELLED,
                action_settled=False,
                duration_ms=elapsed_ms,
            )

        try:
            if target_locator is not None:
                curr_state = await target_locator.evaluate(r"""(btn) => {
                    if (!btn) return { btnFound: false };
                    const exp = btn.getAttribute('aria-expanded');
                    const sel = btn.getAttribute('aria-selected');
                    const ctrlAttr = btn.getAttribute('aria-controls');
                    let hasControls = false;
                    let ctrlId = null;
                    let multipleControls = false;
                    let ctrlFound = false;
                    let ctrlVis = false;

                    if (ctrlAttr) {
                        hasControls = true;
                        const tokens = ctrlAttr.trim().split(/\s+/);
                        if (tokens.length > 1) {
                            multipleControls = true;
                        } else if (tokens.length === 1 && tokens[0]) {
                            ctrlId = tokens[0];
                            const root = btn.getRootNode ? btn.getRootNode() : document;
                            const ctrlEl = (root && root.getElementById) ? root.getElementById(ctrlId) : document.getElementById(ctrlId);
                            if (ctrlEl) {
                                ctrlFound = true;
                                const rect = ctrlEl.getBoundingClientRect();
                                const style = window.getComputedStyle(ctrlEl);
                                const isDialog = ctrlEl.tagName.toLowerCase() === 'dialog';
                                const isOpen = ctrlEl.hasAttribute('open');
                                ctrlVis = (isDialog && isOpen) || (!ctrlEl.hidden && style.display !== 'none' && style.visibility !== 'hidden' && (rect.width > 0 || rect.height > 0));
                            }
                        }
                    }

                    const anyDialogOpen = Array.from(document.querySelectorAll('dialog')).some(d => d.open || d.hasAttribute('open'));

                    return {
                        btnFound: true,
                        exp: exp,
                        sel: sel,
                        hasControls: hasControls,
                        multipleControls: multipleControls,
                        ctrlId: ctrlId,
                        ctrlFound: ctrlFound,
                        ctrlVis: ctrlVis,
                        anyDialogOpen: anyDialogOpen,
                    };
                }""")
            else:
                curr_state = await target_frame.evaluate(r"""(id) => {
                    const btn = document.getElementById(id);
                    if (!btn) return { btnFound: false };
                    const exp = btn.getAttribute('aria-expanded');
                    const sel = btn.getAttribute('aria-selected');
                    const ctrlAttr = btn.getAttribute('aria-controls');
                    let hasControls = false;
                    let ctrlId = null;
                    let multipleControls = false;
                    let ctrlFound = false;
                    let ctrlVis = false;

                    if (ctrlAttr) {
                        hasControls = true;
                        const tokens = ctrlAttr.trim().split(/\s+/);
                        if (tokens.length > 1) {
                            multipleControls = true;
                        } else if (tokens.length === 1 && tokens[0]) {
                            ctrlId = tokens[0];
                            const ctrlEl = document.getElementById(ctrlId);
                            if (ctrlEl) {
                                ctrlFound = true;
                                const rect = ctrlEl.getBoundingClientRect();
                                const style = window.getComputedStyle(ctrlEl);
                                const isDialog = ctrlEl.tagName.toLowerCase() === 'dialog';
                                const isOpen = ctrlEl.hasAttribute('open');
                                ctrlVis = (isDialog && isOpen) || (!ctrlEl.hidden && style.display !== 'none' && style.visibility !== 'hidden' && (rect.width > 0 || rect.height > 0));
                            }
                        }
                    }

                    const anyDialogOpen = Array.from(document.querySelectorAll('dialog')).some(d => d.open || d.hasAttribute('open'));

                    return {
                        btnFound: true,
                        exp: exp,
                        sel: sel,
                        hasControls: hasControls,
                        multipleControls: multipleControls,
                        ctrlId: ctrlId,
                        ctrlFound: ctrlFound,
                        ctrlVis: ctrlVis,
                        anyDialogOpen: anyDialogOpen,
                    };
                }""", raw_id)
        except Exception:
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            return ObservationResult(
                status=ObservationStatus.FAILED,
                action_settled=False,
                duration_ms=elapsed_ms,
                error_type="DOM_OBSERVATION_EXCEPTION",
            )

        if not curr_state or not curr_state.get("btnFound"):
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            return ObservationResult(
                status=ObservationStatus.FAILED,
                action_settled=False,
                duration_ms=elapsed_ms,
                error_type="TARGET_ELEMENT_NOT_FOUND",
            )

        curr_exp = curr_state.get("exp")
        curr_sel = curr_state.get("sel")
        has_controls = curr_state.get("hasControls")
        multiple_controls = curr_state.get("multipleControls")
        ctrl_id = curr_state.get("ctrlId")
        ctrl_found = curr_state.get("ctrlFound")
        curr_vis = curr_state.get("ctrlVis")
        elapsed_s = time.monotonic() - start_time

        # Unsupported pattern: multiple aria-controls targets
        if multiple_controls:
            return ObservationResult(
                status=ObservationStatus.FAILED,
                action_settled=False,
                duration_ms=elapsed_s * 1000.0,
                error_type="MULTIPLE_ARIA_CONTROLS_UNSUPPORTED",
            )

        # 1. Consistent Expansion / Reveal Completion (Disclosure, Modal Dialog, or Local Validation):
        # Pre-state was collapsed/hidden -> toggled to (aria-expanded='true' OR curr_exp is None) AND controlled content visible
        if (curr_exp == "true" or (curr_exp is None and curr_vis is True)) and pre_vis is False and pre_expanded != "true":
            if has_controls and ctrl_id and ctrl_found:
                if curr_vis is True:
                    return ObservationResult(
                        status=ObservationStatus.COMPLETED,
                        action_settled=True,
                        duration_ms=elapsed_s * 1000.0,
                        observed_transition="EXPANDED",
                    )
                # If curr_vis is not yet True, continue polling (attribute-first transition)

        # 2. Consistent Collapse Completion:
        # Pre-state was expanded -> toggled to aria-expanded='false' AND controlled content hidden
        if curr_exp == "false" and pre_vis is True and pre_expanded == "true":
            if has_controls and ctrl_id and ctrl_found:
                if curr_vis is False:
                    return ObservationResult(
                        status=ObservationStatus.COMPLETED,
                        action_settled=True,
                        duration_ms=elapsed_s * 1000.0,
                        observed_transition="COLLAPSED",
                    )
                # If curr_vis is not yet False, continue polling (attribute-first transition)

        # 3. Consistent Tab / View Selection Completion:
        if curr_sel == "true" and pre_selected != "true":
            if has_controls and ctrl_id and ctrl_found:
                if curr_vis is True:
                    return ObservationResult(
                        status=ObservationStatus.COMPLETED,
                        action_settled=True,
                        duration_ms=elapsed_s * 1000.0,
                        observed_transition="SELECTED",
                    )
            else:
                return ObservationResult(
                    status=ObservationStatus.COMPLETED,
                    action_settled=True,
                    duration_ms=elapsed_s * 1000.0,
                    observed_transition="SELECTED",
                )

        # 4. Consistent Modal Dialog Dismissal Completion:
        if pre_dialog_open is True and curr_state.get("anyDialogOpen") is False:
            return ObservationResult(
                status=ObservationStatus.COMPLETED,
                action_settled=True,
                duration_ms=elapsed_s * 1000.0,
                observed_transition="DIALOG_DISMISSED",
            )

        # If visibility toggled first but attribute has not changed yet, continue polling (visibility-first transition)

        await asyncio.sleep(poll_s)

    # Observation budget exhausted without consistent completion
    elapsed_ms = (time.monotonic() - start_time) * 1000.0
    return ObservationResult(
        status=ObservationStatus.TIMED_OUT,
        action_settled=False,
        duration_ms=elapsed_ms,
        observed_transition="TIMEOUT_INCOMPLETE_TRANSITION",
    )


class SafeDisclosureFunctionalOracle:
    """Phase C functional oracle for evaluating ephemeral disclosure controls."""

    def __init__(
        self,
        enable_defect_classification: bool = False,
        snapshot_adapter: Optional[Any] = None,
        normalizer: Optional[Any] = None,
        intent_factory: Optional[Any] = None,
    ):
        self.enable_defect_classification = enable_defect_classification
        self._snapshot_adapter = snapshot_adapter
        self._normalizer = normalizer
        self._intent_factory = intent_factory

    def evaluate_result(
        self,
        execution_res: GuardedExecutionResult,
        dom_evidence: Optional[Dict[str, Any]] = None,
    ) -> FunctionalEvaluationResult:
        """Evaluate an existing GuardedExecutionResult against functional oracle criteria."""
        dom_ev = {**(getattr(execution_res, "dom_evidence", None) or {}), **(dom_evidence or {})}

        # 1. Non-executed: Safety policy rejections
        if execution_res.status in (
            GuardedExecutionStatus.DISALLOWED_RISK_TIER,
        ):
            evidence = FunctionalEvidence(
                action_attempted=execution_res.action_attempted,
                action_confirmed=execution_res.action_confirmed,
                expanded_before=execution_res.disclosure_expanded_before,
                expanded_after=execution_res.disclosure_expanded_after,
                state_toggled=False,
                controls_count_delta=None,
                fingerprint_changed=False,
                pre_state_fingerprint=execution_res.pre_action_state_fingerprint,
                post_state_fingerprint=execution_res.post_action_state_fingerprint,
                observation_sufficient=True,
                rationale="Action rejected by interaction safety policy: control exceeds safe disclosure risk tier",
            )
            return FunctionalEvaluationResult(
                verdict=FunctionalOutcomeVerdict.SAFETY_ABORT,
                is_customer_defect=False,
                defect_category=None,
                target_control_id=execution_res.target_control_id,
                action_type=execution_res.action_type,
                evidence=evidence,
                execution_status=execution_res.status,
                limitations=execution_res.limitations,
                diagnostics={"rejection_reason": sanitize_error_text(execution_res.error_message)},
            )

        # 2. Non-executed: Limited coverage (non-disclosure control)
        if execution_res.status == GuardedExecutionStatus.NOT_DISCLOSURE_CONTROL:
            evidence = FunctionalEvidence(
                action_attempted=execution_res.action_attempted,
                action_confirmed=execution_res.action_confirmed,
                expanded_before=execution_res.disclosure_expanded_before,
                expanded_after=execution_res.disclosure_expanded_after,
                state_toggled=False,
                controls_count_delta=None,
                fingerprint_changed=False,
                pre_state_fingerprint=execution_res.pre_action_state_fingerprint,
                post_state_fingerprint=execution_res.post_action_state_fingerprint,
                observation_sufficient=True,
                rationale="Control is outside Phase B2/C ephemeral disclosure coverage",
            )
            return FunctionalEvaluationResult(
                verdict=FunctionalOutcomeVerdict.LIMITED_COVERAGE,
                is_customer_defect=False,
                defect_category=None,
                target_control_id=execution_res.target_control_id,
                action_type=execution_res.action_type,
                evidence=evidence,
                execution_status=execution_res.status,
                limitations=execution_res.limitations,
                diagnostics={"coverage_reason": sanitize_error_text(execution_res.error_message)},
            )

        # 3. Teardown / infrastructure issue
        if execution_res.status == GuardedExecutionStatus.CLEANUP_FAILED:
            confirmed = bool(execution_res.action_confirmed or dom_ev.get("action_confirmed"))
            attempted = bool(execution_res.action_attempted or dom_ev.get("action_attempted") or confirmed)
            evidence = FunctionalEvidence(
                action_attempted=attempted,
                action_confirmed=confirmed,
                expanded_before=execution_res.disclosure_expanded_before,
                expanded_after=execution_res.disclosure_expanded_after,
                state_toggled=(
                    execution_res.disclosure_expanded_before is not None
                    and execution_res.disclosure_expanded_after is not None
                    and execution_res.disclosure_expanded_before != execution_res.disclosure_expanded_after
                ),
                controls_count_delta=None,
                fingerprint_changed=False,
                pre_state_fingerprint=execution_res.pre_action_state_fingerprint,
                post_state_fingerprint=execution_res.post_action_state_fingerprint,
                observation_sufficient=True,
                rationale="Teardown verification failed after action confirmation; diagnostic issue, not a customer defect",
            )
            return FunctionalEvaluationResult(
                verdict=FunctionalOutcomeVerdict.DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE,
                is_customer_defect=False,
                defect_category=None,
                target_control_id=execution_res.target_control_id,
                action_type=execution_res.action_type,
                evidence=evidence,
                execution_status=execution_res.status,
                limitations=execution_res.limitations,
                diagnostics={"cleanup_error": sanitize_error_text(execution_res.error_message)},
            )

        # 4. Cancellation event:
        if (
            execution_res.status == GuardedExecutionStatus.CANCELLED
            or dom_ev.get("observation_status") in (ObservationStatus.CANCELLED, "CANCELLED")
            or "operation_cancelled" in execution_res.limitations
        ):
            confirmed = bool(execution_res.action_confirmed or dom_ev.get("action_confirmed"))
            evidence = FunctionalEvidence(
                action_attempted=execution_res.action_attempted,
                action_confirmed=confirmed,
                expanded_before=execution_res.disclosure_expanded_before,
                expanded_after=execution_res.disclosure_expanded_after,
                state_toggled=False,
                controls_count_delta=None,
                fingerprint_changed=False,
                pre_state_fingerprint=execution_res.pre_action_state_fingerprint,
                post_state_fingerprint=execution_res.post_action_state_fingerprint,
                observation_sufficient=False,
                observation_status="CANCELLED",
                rationale="Evaluation was cancelled after confirmed action" if confirmed else "Evaluation was cancelled before action completion",
            )
            return FunctionalEvaluationResult(
                verdict=FunctionalOutcomeVerdict.LIMITED_COVERAGE,
                is_customer_defect=False,
                defect_category=None,
                target_control_id=execution_res.target_control_id,
                action_type=execution_res.action_type,
                evidence=evidence,
                execution_status=GuardedExecutionStatus.CANCELLED,
                limitations=execution_res.limitations + (("operation_cancelled",) if "operation_cancelled" not in execution_res.limitations else ()),
                diagnostics={"cancelled": True, "observation_status": "CANCELLED"},
            )

        # 5. Other execution-control events (reconstruction failed, stale state, target not found, action failed, etc.)
        if execution_res.status != GuardedExecutionStatus.EXECUTED and dom_ev.get("validation_result") is None:
            confirmed = bool(execution_res.action_confirmed or dom_ev.get("action_confirmed"))
            attempted = bool(execution_res.action_attempted or dom_ev.get("action_attempted") or confirmed)
            exec_status = execution_res.status
            if confirmed and exec_status == GuardedExecutionStatus.ACTION_FAILED:
                exec_status = GuardedExecutionStatus.OBSERVATION_FAILED

            evidence = FunctionalEvidence(
                action_attempted=attempted,
                action_confirmed=confirmed,
                expanded_before=execution_res.disclosure_expanded_before,
                expanded_after=execution_res.disclosure_expanded_after,
                state_toggled=False,
                controls_count_delta=None,
                fingerprint_changed=False,
                pre_state_fingerprint=execution_res.pre_action_state_fingerprint,
                post_state_fingerprint=execution_res.post_action_state_fingerprint,
                observation_sufficient=False if (not confirmed or exec_status == GuardedExecutionStatus.OBSERVATION_FAILED) else True,
                rationale=(
                    f"Action click confirmed, but observation failed: {execution_res.error_message}"
                    if confirmed
                    else f"Execution-control event prevented action completion: {execution_res.status.value}"
                ),
            )
            return FunctionalEvaluationResult(
                verdict=FunctionalOutcomeVerdict.EXECUTION_CONTROL_EVENT,
                is_customer_defect=False,
                defect_category=None,
                target_control_id=execution_res.target_control_id,
                action_type=execution_res.action_type,
                evidence=evidence,
                execution_status=exec_status,
                limitations=execution_res.limitations,
                diagnostics={"execution_error": sanitize_error_text(execution_res.error_message)},
            )

        # 5. Executed action: Functional Evaluation
        exp_before = execution_res.disclosure_expanded_before
        exp_after = execution_res.disclosure_expanded_after
        sel_before = getattr(execution_res, "disclosure_selected_before", None)
        sel_after = getattr(execution_res, "disclosure_selected_after", None)
        screenshot_path = getattr(execution_res, "screenshot_path", None)

        controlled_id = dom_ev.get("controlled_element_id")
        controlled_found = dom_ev.get("controlled_element_found")
        controlled_vis_before = dom_ev.get("controlled_element_visible_before")
        controlled_vis_after = dom_ev.get("controlled_element_visible_after")
        pre_obs_ok = bool(dom_ev.get("pre_observation_succeeded"))
        post_obs_ok = bool(dom_ev.get("post_observation_succeeded"))
        multiple_controls = bool(dom_ev.get("multiple_controlled_elements"))

        state_toggled = (
            (exp_before is not None and exp_after is not None and exp_before != exp_after)
            or (sel_before is not None and sel_after is not None and sel_before != sel_after)
            or (controlled_vis_before is False and controlled_vis_after is True)
        )

        pre_fp = execution_res.pre_action_state_fingerprint
        post_fp = execution_res.post_action_state_fingerprint
        fp_changed = (pre_fp is not None and post_fp is not None and pre_fp != post_fp)

        ctrl_delta = None
        if execution_res.post_action_controls_count is not None and dom_ev.get("pre_controls_count") is not None:
            ctrl_delta = execution_res.post_action_controls_count - dom_ev["pre_controls_count"]

        # Process and sanitize page errors; retain only allowlisted categories and bounded counts
        raw_page_errors: List[StructuredPageError] = dom_ev.get("structured_page_errors", [])
        click_errors: List[str] = []
        lockdown_errors: List[str] = []
        background_errors: List[str] = []

        for p_err in raw_page_errors:
            safe_type = safe_error_type(p_err.error_type)
            if p_err.is_network_or_lockdown_artifact:
                lockdown_errors.append(safe_type)
            elif p_err.is_unrelated_background_activity:
                background_errors.append(safe_type)
            elif p_err.occurred_during_click:
                click_errors.append(safe_type)

        if not click_errors and dom_ev.get("page_script_errors"):
            for err in dom_ev["page_script_errors"]:
                s_err = str(err)
                if is_network_or_lockdown_artifact(s_err):
                    lockdown_errors.append("Error")
                elif is_unrelated_background_error(s_err):
                    background_errors.append("Error")
                else:
                    click_errors.append(extract_safe_error_type(s_err))

        sanitized_click_errors = tuple(click_errors[:5])
        sanitized_lockdown_errors = tuple(lockdown_errors[:5])
        sanitized_background_errors = tuple(background_errors[:5])

        obs_status = dom_ev.get("observation_status")
        budget_exhausted = bool(dom_ev.get("observation_budget_exhausted"))
        is_timed_out = (
            obs_status in (ObservationStatus.TIMED_OUT, "TIMED_OUT", "ObservationStatus.TIMED_OUT")
            or str(obs_status).endswith("TIMED_OUT")
            or budget_exhausted
            or (execution_res.status == GuardedExecutionStatus.ACTION_FAILED and "deadline" in (execution_res.error_message or "").lower())
        )
        is_failed = (
            obs_status in (ObservationStatus.FAILED, "FAILED", "ObservationStatus.FAILED")
            or str(obs_status).endswith("FAILED")
            or bool(dom_ev.get("observation_error"))
            or execution_res.status in (GuardedExecutionStatus.FAILED, GuardedExecutionStatus.ACTION_FAILED)
        )
        is_cancelled = (
            obs_status in (ObservationStatus.CANCELLED, "CANCELLED", "ObservationStatus.CANCELLED")
            or str(obs_status).endswith("CANCELLED")
            or execution_res.status == GuardedExecutionStatus.CANCELLED
        )
        obs_completed = (
            obs_status in (ObservationStatus.COMPLETED, "COMPLETED", "ObservationStatus.COMPLETED")
            or str(obs_status).endswith("COMPLETED")
        )

        # Extract pre- and post-controlled element IDs to verify content identity consistency
        pre_ctrl_id = dom_ev.get("pre_controlled_element_id")
        post_ctrl_id = dom_ev.get("post_controlled_element_id")
        if pre_ctrl_id is not None or post_ctrl_id is not None:
            consistent_content_id = (
                bool(controlled_id)
                and pre_ctrl_id == controlled_id
                and post_ctrl_id == controlled_id
                and pre_ctrl_id == post_ctrl_id
            )
        else:
            consistent_content_id = bool(controlled_id)

        # Single success-eligibility check (Requirements 1 & 2):
        # Requires:
        # - confirmed guarded execution (action_confirmed is True and action_attempted is True)
        # - successful pre-action AND post-action observations (pre_obs_ok is True and post_obs_ok is True)
        # - supported and consistent target/content identity (same controlled content in pre and post)
        # - completed observation status (COMPLETED)
        # - no failed, cancelled, or timed-out observation
        # - no observation error
        # - not multiple controls
        # - controlled element found in DOM
        is_success_eligible = (
            execution_res.action_confirmed is True
            and execution_res.action_attempted is True
            and execution_res.status == GuardedExecutionStatus.EXECUTED
            and dom_ev.get("pre_observation_succeeded") is True
            and dom_ev.get("post_observation_succeeded") is True
            and obs_completed is True
            and not is_failed
            and not is_cancelled
            and not is_timed_out
            and not budget_exhausted
            and not bool(dom_ev.get("observation_error"))
            and not multiple_controls
            and consistent_content_id is True
            and controlled_found is True
        )

        # Domain Validation Check (Explicit Contract-Driven Evaluation)
        val_res = dom_ev.get("validation_result")
        if val_res is not None:
            # Check QA network barrier interference first
            qa_network_interference_contract = (
                bool(dom_ev.get("qa_network_interference"))
                or int(dom_ev.get("network_blocked_count", 0)) > 0
                or bool(dom_ev.get("network_activity_detected"))
                or bool(sanitized_lockdown_errors)
            )
            if qa_network_interference_contract:
                evidence = FunctionalEvidence(
                    action_attempted=execution_res.action_attempted,
                    action_confirmed=execution_res.action_confirmed,
                    expanded_before=exp_before,
                    expanded_after=exp_after,
                    state_toggled=False,
                    controls_count_delta=ctrl_delta,
                    fingerprint_changed=fp_changed,
                    pre_state_fingerprint=pre_fp,
                    post_state_fingerprint=post_fp,
                    controlled_element_id=controlled_id,
                    controlled_element_found=controlled_found,
                    controlled_element_visible_before=controlled_vis_before,
                    controlled_element_visible_after=controlled_vis_after,
                    pre_observation_succeeded=pre_obs_ok,
                    post_observation_succeeded=post_obs_ok,
                    page_script_errors=sanitized_click_errors,
                    observation_sufficient=False,
                    observation_status=str(obs_status or "INTERFERED"),
                    rationale="Validation action attempted network activity blocked by QA lockdown barrier; network-dependent validation is outside local offline contract coverage",
                )
                diag = {
                    "qa_network_interference": True,
                    "network_blocked_count": dom_ev.get("network_blocked_count", len(sanitized_lockdown_errors)),
                    "validation_contract_id": getattr(val_res.contract, "contract_id", None) if val_res else None,
                }
                return FunctionalEvaluationResult(
                    verdict=FunctionalOutcomeVerdict.LIMITED_COVERAGE,
                    is_customer_defect=False,
                    defect_category=None,
                    target_control_id=execution_res.target_control_id,
                    action_type=execution_res.action_type,
                    evidence=evidence,
                    execution_status=execution_res.status,
                    limitations=execution_res.limitations + ("network_barrier_blocked_request",),
                    diagnostics=diag,
                )

            # Contract satisfied case requires success eligibility
            if val_res.verdict == ValidationVerdict.SATISFIED:
                if is_success_eligible:
                    evidence = FunctionalEvidence(
                        action_attempted=execution_res.action_attempted,
                        action_confirmed=execution_res.action_confirmed,
                        expanded_before=exp_before,
                        expanded_after=exp_after,
                        state_toggled=True,
                        controls_count_delta=ctrl_delta,
                        fingerprint_changed=fp_changed,
                        pre_state_fingerprint=pre_fp,
                        post_state_fingerprint=post_fp,
                        controlled_element_id=controlled_id,
                        controlled_element_found=controlled_found,
                        controlled_element_visible_before=controlled_vis_before,
                        controlled_element_visible_after=controlled_vis_after,
                        pre_observation_succeeded=pre_obs_ok,
                        post_observation_succeeded=post_obs_ok,
                        page_script_errors=sanitized_click_errors,
                        observation_sufficient=True,
                        observation_status="COMPLETED",
                        rationale=f"Verified domain validation satisfied contract '{val_res.contract.rule_name}': {val_res.summary}",
                    )
                    diag = {
                        "validation_contract_id": val_res.contract.contract_id,
                        "rule_name": val_res.contract.rule_name,
                        "validation_summary": val_res.summary,
                        "cases_tested": len(val_res.test_case_results),
                    }
                    return FunctionalEvaluationResult(
                        verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
                        is_customer_defect=False,
                        defect_category=None,
                        target_control_id=execution_res.target_control_id,
                        action_type=execution_res.action_type,
                        evidence=evidence,
                        execution_status=execution_res.status,
                        limitations=execution_res.limitations,
                        diagnostics=diag,
                    )
                else:
                    evidence = FunctionalEvidence(
                        action_attempted=execution_res.action_attempted,
                        action_confirmed=execution_res.action_confirmed,
                        expanded_before=exp_before,
                        expanded_after=exp_after,
                        state_toggled=state_toggled,
                        controls_count_delta=ctrl_delta,
                        fingerprint_changed=fp_changed,
                        pre_state_fingerprint=pre_fp,
                        post_state_fingerprint=post_fp,
                        controlled_element_id=controlled_id,
                        controlled_element_found=controlled_found,
                        controlled_element_visible_before=controlled_vis_before,
                        controlled_element_visible_after=controlled_vis_after,
                        pre_observation_succeeded=pre_obs_ok,
                        post_observation_succeeded=post_obs_ok,
                        page_script_errors=sanitized_click_errors,
                        observation_sufficient=False,
                        observation_status=str(obs_status or "INCONCLUSIVE"),
                        rationale=f"Validation satisfied logic but execution/observation prerequisites were incomplete: {val_res.summary}",
                    )
                    return FunctionalEvaluationResult(
                        verdict=FunctionalOutcomeVerdict.LIMITED_COVERAGE,
                        is_customer_defect=False,
                        defect_category=None,
                        target_control_id=execution_res.target_control_id,
                        action_type=execution_res.action_type,
                        evidence=evidence,
                        execution_status=execution_res.status,
                        limitations=execution_res.limitations + ("incomplete_observation_prerequisites",),
                        diagnostics={"validation_contract_id": val_res.contract.contract_id},
                    )

            elif val_res.verdict in (ValidationVerdict.DEFECT_ACCEPTED_INVALID, ValidationVerdict.DEFECT_REJECTED_VALID):
                if is_cancelled or is_timed_out or execution_res.status == GuardedExecutionStatus.CANCELLED:
                    evidence = FunctionalEvidence(
                        action_attempted=execution_res.action_attempted,
                        action_confirmed=execution_res.action_confirmed,
                        expanded_before=exp_before,
                        expanded_after=exp_after,
                        state_toggled=False,
                        controls_count_delta=ctrl_delta,
                        fingerprint_changed=fp_changed,
                        pre_state_fingerprint=pre_fp,
                        post_state_fingerprint=post_fp,
                        controlled_element_id=controlled_id,
                        controlled_element_found=controlled_found,
                        controlled_element_visible_before=controlled_vis_before,
                        controlled_element_visible_after=controlled_vis_after,
                        pre_observation_succeeded=pre_obs_ok,
                        post_observation_succeeded=post_obs_ok,
                        page_script_errors=sanitized_click_errors,
                        observation_sufficient=False,
                        observation_status=str(obs_status or "INCONCLUSIVE"),
                        rationale=f"Validation interrupted by cancellation or timeout: {val_res.summary}",
                    )
                    return FunctionalEvaluationResult(
                        verdict=FunctionalOutcomeVerdict.LIMITED_COVERAGE,
                        is_customer_defect=False,
                        defect_category=None,
                        target_control_id=execution_res.target_control_id,
                        action_type=execution_res.action_type,
                        evidence=evidence,
                        execution_status=execution_res.status,
                        limitations=execution_res.limitations + ("execution_interrupted",),
                        diagnostics={"validation_contract_id": getattr(val_res.contract, "contract_id", None) if val_res else None},
                    )

                evidence = FunctionalEvidence(
                    action_attempted=execution_res.action_attempted,
                    action_confirmed=execution_res.action_confirmed,
                    expanded_before=exp_before,
                    expanded_after=exp_after,
                    state_toggled=True,
                    controls_count_delta=ctrl_delta,
                    fingerprint_changed=fp_changed,
                    pre_state_fingerprint=pre_fp,
                    post_state_fingerprint=post_fp,
                    controlled_element_id=controlled_id,
                    controlled_element_found=controlled_found,
                    controlled_element_visible_before=controlled_vis_before,
                    controlled_element_visible_after=controlled_vis_after,
                    pre_observation_succeeded=pre_obs_ok,
                    post_observation_succeeded=post_obs_ok,
                    page_script_errors=sanitized_click_errors,
                    observation_sufficient=True,
                    observation_status="COMPLETED",
                    rationale=f"Confirmed functional defect: {val_res.summary} (category: {DefectCategory.VALIDATION_LOGIC_ERROR.value})",
                )
                diag = {
                    "confirmed_defect_category": DefectCategory.VALIDATION_LOGIC_ERROR.value,
                    "defect_reason": val_res.summary,
                    "validation_contract_id": val_res.contract.contract_id,
                }
                # Respect enable_defect_classification flag
                if getattr(self, "enable_defect_classification", False):
                    return FunctionalEvaluationResult(
                        verdict=FunctionalOutcomeVerdict.SUPPORTED_FAILURE,
                        is_customer_defect=True,
                        defect_category=DefectCategory.VALIDATION_LOGIC_ERROR,
                        target_control_id=execution_res.target_control_id,
                        action_type=execution_res.action_type,
                        evidence=evidence,
                        execution_status=execution_res.status,
                        limitations=execution_res.limitations,
                        diagnostics=diag,
                    )
                else:
                    return FunctionalEvaluationResult(
                        verdict=FunctionalOutcomeVerdict.LIMITED_COVERAGE,
                        is_customer_defect=False,
                        defect_category=None,
                        target_control_id=execution_res.target_control_id,
                        action_type=execution_res.action_type,
                        evidence=evidence,
                        execution_status=execution_res.status,
                        limitations=execution_res.limitations + ("defect_classification_disabled",),
                        diagnostics=diag,
                    )

            elif val_res.verdict == ValidationVerdict.PREREQUISITE_FAILED:
                evidence = FunctionalEvidence(
                    action_attempted=execution_res.action_attempted,
                    action_confirmed=execution_res.action_confirmed,
                    expanded_before=exp_before,
                    expanded_after=exp_after,
                    state_toggled=False,
                    controls_count_delta=ctrl_delta,
                    fingerprint_changed=fp_changed,
                    pre_state_fingerprint=pre_fp,
                    post_state_fingerprint=post_fp,
                    controlled_element_id=controlled_id,
                    controlled_element_found=controlled_found,
                    controlled_element_visible_before=controlled_vis_before,
                    controlled_element_visible_after=controlled_vis_after,
                    pre_observation_succeeded=pre_obs_ok,
                    post_observation_succeeded=post_obs_ok,
                    page_script_errors=sanitized_click_errors,
                    observation_sufficient=False,
                    observation_status="INCONCLUSIVE",
                    rationale=f"Domain validation prerequisite failed: {val_res.summary}",
                )
                diag = {
                    "validation_contract_id": getattr(val_res.contract, "contract_id", None) if val_res else None,
                    "ambiguity_reason": val_res.summary,
                    "execution_error": sanitize_error_text(execution_res.error_message),
                }
                return FunctionalEvaluationResult(
                    verdict=FunctionalOutcomeVerdict.EXECUTION_CONTROL_EVENT,
                    is_customer_defect=False,
                    defect_category=None,
                    target_control_id=execution_res.target_control_id,
                    action_type=execution_res.action_type,
                    evidence=evidence,
                    execution_status=execution_res.status,
                    limitations=execution_res.limitations + ("validation_prerequisite_failed",),
                    diagnostics=diag,
                )

            elif val_res.verdict in (
                ValidationVerdict.AMBIGUOUS_FEEDBACK,
                ValidationVerdict.INCOMPLETE_COVERAGE,
            ):
                evidence = FunctionalEvidence(
                    action_attempted=execution_res.action_attempted,
                    action_confirmed=execution_res.action_confirmed,
                    expanded_before=exp_before,
                    expanded_after=exp_after,
                    state_toggled=False,
                    controls_count_delta=ctrl_delta,
                    fingerprint_changed=fp_changed,
                    pre_state_fingerprint=pre_fp,
                    post_state_fingerprint=post_fp,
                    controlled_element_id=controlled_id,
                    controlled_element_found=controlled_found,
                    controlled_element_visible_before=controlled_vis_before,
                    controlled_element_visible_after=controlled_vis_after,
                    pre_observation_succeeded=pre_obs_ok,
                    post_observation_succeeded=post_obs_ok,
                    page_script_errors=sanitized_click_errors,
                    observation_sufficient=False,
                    observation_status="INCONCLUSIVE",
                    rationale=f"Domain validation inconclusive: {val_res.summary}",
                )
                diag = {
                    "validation_contract_id": getattr(val_res.contract, "contract_id", None) if val_res else None,
                    "ambiguity_reason": val_res.summary,
                    "execution_error": sanitize_error_text(execution_res.error_message),
                }
                return FunctionalEvaluationResult(
                    verdict=FunctionalOutcomeVerdict.INCONCLUSIVE,
                    is_customer_defect=False,
                    defect_category=None,
                    target_control_id=execution_res.target_control_id,
                    action_type=execution_res.action_type,
                    evidence=evidence,
                    execution_status=execution_res.status,
                    limitations=execution_res.limitations + ("inconclusive_validation_contract",),
                    diagnostics=diag,
                )

        # Dialog Dismissal Check (modal dialog closed: open -> closed)
        dlg_before = dom_ev.get("dialog_state_before")
        dlg_after = dom_ev.get("dialog_state_after")
        is_dialog_dismissal = False
        if dlg_before and dlg_after:
            if dlg_before.get("anyOpen") is True and dlg_after.get("anyOpen") is False:
                is_dialog_dismissal = True

        if is_dialog_dismissal:
            evidence = FunctionalEvidence(
                action_attempted=execution_res.action_attempted,
                action_confirmed=execution_res.action_confirmed,
                expanded_before=True,
                expanded_after=False,
                state_toggled=True,
                controls_count_delta=ctrl_delta,
                fingerprint_changed=fp_changed,
                pre_state_fingerprint=pre_fp,
                post_state_fingerprint=post_fp,
                controlled_element_id=controlled_id,
                controlled_element_found=True,
                controlled_element_visible_before=True,
                controlled_element_visible_after=False,
                pre_observation_succeeded=True,
                post_observation_succeeded=True,
                page_script_errors=(),
                observation_sufficient=True,
                observation_status="COMPLETED",
                rationale="Modal dialog dismissed correctly: target dialog toggled open -> closed",
            )
            return FunctionalEvaluationResult(
                verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
                is_customer_defect=False,
                defect_category=None,
                target_control_id=execution_res.target_control_id,
                action_type=execution_res.action_type,
                evidence=evidence,
                execution_status=execution_res.status,
                limitations=execution_res.limitations,
                diagnostics={"transition": "dialog: open -> closed"},
            )

        # Rule 1: Verified Success on Tab / View Selection
        # Requires positive selection change AND confirmed associated content evidence
        has_positive_selection = (
            is_success_eligible
            and (sel_before is not None or sel_after is not None)
            and (sel_before is not True and sel_after is True)
            and (
                (controlled_id and controlled_found is True and controlled_vis_after is True)
                or (not controlled_id and (fp_changed or state_toggled or (ctrl_delta is not None and ctrl_delta != 0)))
            )
        )

        # Check if control is an uncontracted domain validation trigger
        ctrl_name = str(
            dom_ev.get("btnText")
            or execution_res.target_control_id
            or ""
        ).lower()
        ctrl_id_lower = str(controlled_id or "").lower()
        is_uncontracted_validation = (
            val_res is None
            and (
                any(w in ctrl_name for w in ("validate", "check", "verify"))
                or any(w in ctrl_id_lower for w in ("feedback", "error", "validation"))
            )
        )

        # Rule 2: Verified Success on Expansion / Modal Dialog / Menu
        # (exact False -> True boolean transitions, or unexpanded control revealing controlled target)
        has_positive_reveal = (
            is_success_eligible
            and not has_positive_selection
            and not is_uncontracted_validation
            and ((exp_before is False and exp_after is True) or (exp_before is None and controlled_vis_before is False))
            and controlled_vis_before is False
            and controlled_vis_after is True
        )

        # Rule 3: Verified Success on Collapse (Requirement 4: exact True -> False boolean transitions)
        has_positive_collapse = (
            is_success_eligible
            and not has_positive_selection
            and exp_before is True
            and exp_after is False
            and controlled_vis_before is True
            and controlled_vis_after is False
        )

        # Rule 1: Verified Success on Tab / View Selection
        if has_positive_selection:
            evidence = FunctionalEvidence(
                action_attempted=execution_res.action_attempted,
                action_confirmed=execution_res.action_confirmed,
                expanded_before=exp_before,
                expanded_after=exp_after,
                selected_before=sel_before,
                selected_after=sel_after,
                screenshot_path=screenshot_path,
                state_toggled=True,
                controls_count_delta=ctrl_delta,
                fingerprint_changed=fp_changed,
                pre_state_fingerprint=pre_fp,
                post_state_fingerprint=post_fp,
                controlled_element_id=controlled_id,
                controlled_element_found=controlled_found,
                controlled_element_visible_before=controlled_vis_before,
                controlled_element_visible_after=controlled_vis_after,
                pre_observation_succeeded=pre_obs_ok,
                post_observation_succeeded=post_obs_ok,
                page_script_errors=(),
                observation_sufficient=True,
                observation_status="COMPLETED",
                rationale="Tab/view selected correctly: selection state and target view updated",
            )
            diag = {"transition": f"selected: {sel_before} -> {sel_after}"}
            if dom_ev.get("revealed_controls"):
                diag["revealed_controls"] = dom_ev["revealed_controls"]
            if dom_ev.get("revealed_intents"):
                diag["revealed_intents"] = dom_ev["revealed_intents"]
            if dom_ev.get("revealed_state_fingerprint"):
                diag["revealed_state_fingerprint"] = dom_ev["revealed_state_fingerprint"]
            if sanitized_lockdown_errors:
                diag["unrelated_lockdown_artifacts_ignored"] = sanitized_lockdown_errors
            if sanitized_background_errors:
                diag["unrelated_background_errors_ignored"] = sanitized_background_errors
            if sanitized_click_errors:
                diag["observed_non_fatal_errors"] = sanitized_click_errors
            return FunctionalEvaluationResult(
                verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
                is_customer_defect=False,
                defect_category=None,
                target_control_id=execution_res.target_control_id,
                action_type=execution_res.action_type,
                evidence=evidence,
                execution_status=execution_res.status,
                limitations=execution_res.limitations,
                diagnostics=diag,
            )

        # Rule 2: Verified Success on Expansion / Modal Dialog / Menu
        if has_positive_reveal:
            is_dialog_target = bool(
                dom_ev.get("controlled_is_dialog")
                or (dom_ev.get("controlled_element_tag") == "dialog")
                or (dlg_before and dlg_after and not dlg_before.get("anyOpen") and dlg_after.get("anyOpen"))
                or (controlled_id and "dialog" in controlled_id.lower() and "close" not in controlled_id.lower())
            )
            is_menu_target = bool(
                dom_ev.get("controlled_is_menu")
                or (dom_ev.get("controlled_element_tag") == "menu")
                or (controlled_id and "menu" in controlled_id.lower())
            )
            if is_dialog_target:
                success_rationale = "Modal dialog opened correctly: target dialog became visible and active"
            elif is_menu_target:
                success_rationale = "Dropdown menu opened correctly: menu items became visible"
            else:
                success_rationale = "Disclosure expanded correctly: aria-expanded toggled False -> True and controlled content toggled hidden -> visible"

            evidence = FunctionalEvidence(
                action_attempted=execution_res.action_attempted,
                action_confirmed=execution_res.action_confirmed,
                expanded_before=exp_before,
                expanded_after=exp_after,
                state_toggled=True,
                controls_count_delta=ctrl_delta,
                fingerprint_changed=fp_changed,
                pre_state_fingerprint=pre_fp,
                post_state_fingerprint=post_fp,
                controlled_element_id=controlled_id,
                controlled_element_found=controlled_found,
                controlled_element_visible_before=controlled_vis_before,
                controlled_element_visible_after=controlled_vis_after,
                pre_observation_succeeded=pre_obs_ok,
                post_observation_succeeded=post_obs_ok,
                page_script_errors=(),
                observation_sufficient=True,
                observation_status="COMPLETED",
                rationale=success_rationale,
            )
            diag = {"transition": f"{exp_before} -> {exp_after}"}
            if dom_ev.get("revealed_controls"):
                diag["revealed_controls"] = dom_ev["revealed_controls"]
            if dom_ev.get("revealed_intents"):
                diag["revealed_intents"] = dom_ev["revealed_intents"]
            if dom_ev.get("revealed_state_fingerprint"):
                diag["revealed_state_fingerprint"] = dom_ev["revealed_state_fingerprint"]
            if sanitized_lockdown_errors:
                diag["unrelated_lockdown_artifacts_ignored"] = sanitized_lockdown_errors
            if sanitized_background_errors:
                diag["unrelated_background_errors_ignored"] = sanitized_background_errors
            if sanitized_click_errors:
                diag["observed_non_fatal_errors"] = sanitized_click_errors
            return FunctionalEvaluationResult(
                verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
                is_customer_defect=False,
                defect_category=None,
                target_control_id=execution_res.target_control_id,
                action_type=execution_res.action_type,
                evidence=evidence,
                execution_status=execution_res.status,
                limitations=execution_res.limitations,
                diagnostics=diag,
            )

        # Rule 3: Verified Success on Collapse
        if has_positive_collapse:
            evidence = FunctionalEvidence(
                action_attempted=execution_res.action_attempted,
                action_confirmed=execution_res.action_confirmed,
                expanded_before=exp_before,
                expanded_after=exp_after,
                state_toggled=True,
                controls_count_delta=ctrl_delta,
                fingerprint_changed=fp_changed,
                pre_state_fingerprint=pre_fp,
                post_state_fingerprint=post_fp,
                controlled_element_id=controlled_id,
                controlled_element_found=controlled_found,
                controlled_element_visible_before=controlled_vis_before,
                controlled_element_visible_after=controlled_vis_after,
                pre_observation_succeeded=pre_obs_ok,
                post_observation_succeeded=post_obs_ok,
                page_script_errors=(),
                observation_sufficient=True,
                observation_status="COMPLETED",
                rationale="Disclosure collapsed correctly: aria-expanded toggled True -> False and controlled content toggled visible -> hidden",
            )
            diag = {"transition": f"{exp_before} -> {exp_after}"}
            if dom_ev.get("revealed_controls"):
                diag["revealed_controls"] = dom_ev["revealed_controls"]
            if dom_ev.get("revealed_intents"):
                diag["revealed_intents"] = dom_ev["revealed_intents"]
            if dom_ev.get("revealed_state_fingerprint"):
                diag["revealed_state_fingerprint"] = dom_ev["revealed_state_fingerprint"]
            if sanitized_lockdown_errors:
                diag["unrelated_lockdown_artifacts_ignored"] = sanitized_lockdown_errors
            if sanitized_background_errors:
                diag["unrelated_background_errors_ignored"] = sanitized_background_errors
            if sanitized_click_errors:
                diag["observed_non_fatal_errors"] = sanitized_click_errors
            return FunctionalEvaluationResult(
                verdict=FunctionalOutcomeVerdict.VERIFIED_SUCCESS,
                is_customer_defect=False,
                defect_category=None,
                target_control_id=execution_res.target_control_id,
                action_type=execution_res.action_type,
                evidence=evidence,
                execution_status=execution_res.status,
                limitations=execution_res.limitations,
                diagnostics=diag,
            )

        # ==============================================================================
        # NON-DEFECT EVALUATION POLICIES
        # In this increment, any incomplete, timed out, ambiguous, interfered, or
        # failed observation evaluates to a non-defect outcome.
        # Defect classification is explicitly deferred.
        # ==============================================================================

        # Policy A: QA Network Barrier Interference (when disclosure did NOT succeed)
        qa_network_interference = (
            bool(dom_ev.get("qa_network_interference"))
            or int(dom_ev.get("network_blocked_count", 0)) > 0
            or bool(dom_ev.get("network_activity_detected"))
            or (bool(sanitized_lockdown_errors) and not has_positive_reveal and not has_positive_collapse)
        )
        if qa_network_interference:
            evidence = FunctionalEvidence(
                action_attempted=execution_res.action_attempted,
                action_confirmed=execution_res.action_confirmed,
                expanded_before=exp_before,
                expanded_after=exp_after,
                state_toggled=state_toggled,
                controls_count_delta=ctrl_delta,
                fingerprint_changed=fp_changed,
                pre_state_fingerprint=pre_fp,
                post_state_fingerprint=post_fp,
                controlled_element_id=controlled_id,
                controlled_element_found=controlled_found,
                controlled_element_visible_before=controlled_vis_before,
                controlled_element_visible_after=controlled_vis_after,
                pre_observation_succeeded=pre_obs_ok,
                post_observation_succeeded=post_obs_ok,
                page_script_errors=sanitized_click_errors,
                observation_sufficient=False,
                observation_status=str(obs_status or "INTERFERED"),
                rationale="Target disclosure attempted network activity blocked by QA lockdown barrier; network-dependent disclosures are outside offline ephemeral disclosure coverage",
            )
            diag = {
                "qa_network_interference": True,
                "network_blocked_count": dom_ev.get("network_blocked_count", len(sanitized_lockdown_errors)),
            }
            if sanitized_lockdown_errors:
                diag["unrelated_lockdown_artifacts_ignored"] = sanitized_lockdown_errors
            if sanitized_background_errors:
                diag["unrelated_background_errors_ignored"] = sanitized_background_errors
            if sanitized_click_errors:
                diag["observed_non_fatal_errors"] = sanitized_click_errors
            return FunctionalEvaluationResult(
                verdict=FunctionalOutcomeVerdict.LIMITED_COVERAGE,
                is_customer_defect=False,
                defect_category=None,
                target_control_id=execution_res.target_control_id,
                action_type=execution_res.action_type,
                evidence=evidence,
                execution_status=execution_res.status,
                limitations=execution_res.limitations + ("network_dependent_disclosure_outside_offline_scope",),
                diagnostics=diag,
            )

        # Policy B: Unsupported Disclosure Patterns (multiple controls or non-controls disclosure)
        if multiple_controls:
            evidence = FunctionalEvidence(
                action_attempted=execution_res.action_attempted,
                action_confirmed=execution_res.action_confirmed,
                expanded_before=exp_before,
                expanded_after=exp_after,
                state_toggled=state_toggled,
                controls_count_delta=ctrl_delta,
                fingerprint_changed=fp_changed,
                pre_state_fingerprint=pre_fp,
                post_state_fingerprint=post_fp,
                controlled_element_id=controlled_id,
                controlled_element_found=controlled_found,
                controlled_element_visible_before=controlled_vis_before,
                controlled_element_visible_after=controlled_vis_after,
                pre_observation_succeeded=pre_obs_ok,
                post_observation_succeeded=post_obs_ok,
                page_script_errors=sanitized_click_errors,
                observation_sufficient=False,
                observation_status="UNSUPPORTED",
                rationale="Disclosure references multiple aria-controls targets; multi-target disclosures are outside bounded oracle coverage in this increment",
            )
            return FunctionalEvaluationResult(
                verdict=FunctionalOutcomeVerdict.LIMITED_COVERAGE,
                is_customer_defect=False,
                defect_category=None,
                target_control_id=execution_res.target_control_id,
                action_type=execution_res.action_type,
                evidence=evidence,
                execution_status=execution_res.status,
                limitations=execution_res.limitations + ("multiple_controlled_elements_unsupported",),
                diagnostics={"unsupported_pattern": "multiple_aria_controls"},
            )

        # Policy C: Bounded Observation Budget Exhaustion
        if is_timed_out:
            if getattr(self, "enable_defect_classification", False):
                defect_cat = None
                defect_msg = ""
                if controlled_id and controlled_found is False:
                    defect_cat = DefectCategory.DANGLING_ARIA_CONTROLS
                    defect_msg = f"Disclosure specifies aria-controls='{controlled_id}', but element was not found in the DOM"
                elif state_toggled and exp_after is True and controlled_vis_after is False:
                    defect_cat = DefectCategory.CONTENT_NOT_REVEALED
                    defect_msg = f"aria-expanded toggled to true, but controlled element '{controlled_id}' remained hidden"
                elif state_toggled and exp_after is False and controlled_vis_after is True:
                    defect_cat = DefectCategory.CONTENT_NOT_COLLAPSED
                    defect_msg = f"aria-expanded toggled to false, but controlled element '{controlled_id}' remained visible"
                elif sanitized_click_errors:
                    defect_cat = DefectCategory.PAGE_SCRIPT_ERROR
                    defect_msg = f"Target page threw uncaught exception ({sanitized_click_errors[0]}) during click, leaving control inert"

                if defect_cat:
                    evidence = FunctionalEvidence(
                        action_attempted=execution_res.action_attempted,
                        action_confirmed=execution_res.action_confirmed,
                        expanded_before=exp_before,
                        expanded_after=exp_after,
                        selected_before=sel_before,
                        selected_after=sel_after,
                        screenshot_path=screenshot_path,
                        state_toggled=state_toggled,
                        controls_count_delta=ctrl_delta,
                        fingerprint_changed=fp_changed,
                        pre_state_fingerprint=pre_fp,
                        post_state_fingerprint=post_fp,
                        controlled_element_id=controlled_id,
                        controlled_element_found=controlled_found,
                        controlled_element_visible_before=controlled_vis_before,
                        controlled_element_visible_after=controlled_vis_after,
                        pre_observation_succeeded=pre_obs_ok,
                        post_observation_succeeded=post_obs_ok,
                        page_script_errors=sanitized_click_errors,
                        observation_sufficient=True,
                        observation_status="COMPLETED",
                        rationale=f"Confirmed functional defect: {defect_msg} (category: {defect_cat.value})",
                    )
                    diag = {
                        "confirmed_defect_category": defect_cat.value,
                        "defect_reason": defect_msg,
                    }
                    if sanitized_click_errors:
                        diag["script_errors"] = sanitized_click_errors
                    return FunctionalEvaluationResult(
                        verdict=FunctionalOutcomeVerdict.SUPPORTED_FAILURE,
                        is_customer_defect=True,
                        defect_category=defect_cat,
                        target_control_id=execution_res.target_control_id,
                        action_type=execution_res.action_type,
                        evidence=evidence,
                        execution_status=execution_res.status,
                        limitations=execution_res.limitations,
                        diagnostics=diag,
                    )

            evidence = FunctionalEvidence(
                action_attempted=execution_res.action_attempted,
                action_confirmed=execution_res.action_confirmed,
                expanded_before=exp_before,
                expanded_after=exp_after,
                state_toggled=state_toggled,
                controls_count_delta=ctrl_delta,
                fingerprint_changed=fp_changed,
                pre_state_fingerprint=pre_fp,
                post_state_fingerprint=post_fp,
                controlled_element_id=controlled_id,
                controlled_element_found=controlled_found,
                controlled_element_visible_before=controlled_vis_before,
                controlled_element_visible_after=controlled_vis_after,
                pre_observation_succeeded=pre_obs_ok,
                post_observation_succeeded=post_obs_ok,
                page_script_errors=sanitized_click_errors,
                observation_sufficient=False,
                observation_status="TIMED_OUT",
                rationale="Observation budget exhausted while transition was incomplete; transitions completing beyond budget are outside bounded observation coverage",
            )
            diag = {
                "observation_status": "TIMED_OUT",
                "observation_budget_exhausted": True,
            }
            if sanitized_click_errors:
                diag["script_errors"] = sanitized_click_errors
            if sanitized_lockdown_errors:
                diag["unrelated_lockdown_artifacts_ignored"] = sanitized_lockdown_errors
            if sanitized_background_errors:
                diag["unrelated_background_errors_ignored"] = sanitized_background_errors
            return FunctionalEvaluationResult(
                verdict=FunctionalOutcomeVerdict.LIMITED_COVERAGE,
                is_customer_defect=False,
                defect_category=None,
                target_control_id=execution_res.target_control_id,
                action_type=execution_res.action_type,
                evidence=evidence,
                execution_status=execution_res.status,
                limitations=execution_res.limitations + ("observation_budget_exhausted_beyond_bounded_coverage",),
                diagnostics=diag,
            )

        # Check for ambiguous state transition (neither aria-expanded nor standard disclosure indicators exist)
        if exp_before is None and exp_after is None:
            evidence = FunctionalEvidence(
                action_attempted=execution_res.action_attempted,
                action_confirmed=execution_res.action_confirmed,
                expanded_before=exp_before,
                expanded_after=exp_after,
                state_toggled=state_toggled,
                controls_count_delta=ctrl_delta,
                fingerprint_changed=fp_changed,
                pre_state_fingerprint=pre_fp,
                post_state_fingerprint=post_fp,
                controlled_element_id=controlled_id,
                controlled_element_found=controlled_found,
                controlled_element_visible_before=controlled_vis_before,
                controlled_element_visible_after=controlled_vis_after,
                pre_observation_succeeded=pre_obs_ok,
                post_observation_succeeded=post_obs_ok,
                page_script_errors=sanitized_click_errors,
                observation_sufficient=False,
                observation_status=str(obs_status or "MISSING"),
                rationale="Action confirmed, but state transition evidence was ambiguous, incomplete, or non-standard",
            )
            diag = {"ambiguity_reason": "Neither aria-expanded nor standard disclosure indicators exist"}
            if sanitized_click_errors:
                diag["script_errors"] = sanitized_click_errors
            return FunctionalEvaluationResult(
                verdict=FunctionalOutcomeVerdict.INCONCLUSIVE,
                is_customer_defect=False,
                defect_category=None,
                target_control_id=execution_res.target_control_id,
                action_type=execution_res.action_type,
                evidence=evidence,
                execution_status=execution_res.status,
                limitations=execution_res.limitations,
                diagnostics=diag,
            )

        # Policy D: Missing or Insufficient DOM Evidence Validation
        # Require genuine positive DOM observation evidence (both pre and post observation succeeded).
        # A provenance string, status label, element ID, nonempty dictionary,
        # or caller-provided sufficiency flag alone must not establish validity.
        has_dom_evidence = bool(pre_obs_ok and post_obs_ok)
        has_required_evidence = (
            has_dom_evidence
            and not is_failed
            and not is_cancelled
            and dom_ev.get("observation_sufficient") is not False
        )

        if not has_required_evidence:
            action_confirmed = bool(execution_res.action_confirmed or dom_ev.get("action_confirmed"))
            evidence = FunctionalEvidence(
                action_attempted=execution_res.action_attempted,
                action_confirmed=action_confirmed,
                expanded_before=exp_before,
                expanded_after=exp_after,
                state_toggled=state_toggled,
                controls_count_delta=ctrl_delta,
                fingerprint_changed=fp_changed,
                pre_state_fingerprint=pre_fp,
                post_state_fingerprint=post_fp,
                controlled_element_id=controlled_id,
                controlled_element_found=controlled_found,
                controlled_element_visible_before=controlled_vis_before,
                controlled_element_visible_after=controlled_vis_after,
                pre_observation_succeeded=pre_obs_ok,
                post_observation_succeeded=post_obs_ok,
                page_script_errors=sanitized_click_errors,
                observation_sufficient=False,
                observation_status=str(obs_status or ("CANCELLED" if is_cancelled else ("FAILED" if is_failed else "MISSING"))),
                rationale="DOM observation evidence was missing or insufficient (errored, cancelled, or incomplete) to establish functional outcome",
            )
            diag = {}
            if is_failed:
                diag["observation_error"] = "DOM_OBSERVATION_EXCEPTION"
            if is_cancelled:
                diag["observation_status"] = "CANCELLED"
                diag["cancelled"] = True
            if not has_dom_evidence:
                diag["missing_evidence"] = "Required pre/post DOM observations were missing or incomplete"
            if sanitized_click_errors:
                diag["script_errors"] = sanitized_click_errors
            if sanitized_lockdown_errors:
                diag["unrelated_lockdown_artifacts_ignored"] = sanitized_lockdown_errors
            if sanitized_background_errors:
                diag["unrelated_background_errors_ignored"] = sanitized_background_errors

            if is_cancelled:
                return FunctionalEvaluationResult(
                    verdict=FunctionalOutcomeVerdict.LIMITED_COVERAGE,
                    is_customer_defect=False,
                    defect_category=None,
                    target_control_id=execution_res.target_control_id,
                    action_type=execution_res.action_type,
                    evidence=evidence,
                    execution_status=GuardedExecutionStatus.CANCELLED,
                    limitations=execution_res.limitations + ("operation_cancelled",),
                    diagnostics=diag,
                )

            return FunctionalEvaluationResult(
                verdict=FunctionalOutcomeVerdict.INCONCLUSIVE,
                is_customer_defect=False,
                defect_category=None,
                target_control_id=execution_res.target_control_id,
                action_type=execution_res.action_type,
                evidence=evidence,
                execution_status=execution_res.status,
                limitations=execution_res.limitations,
                diagnostics=diag,
            )

        # ==============================================================================
        # INTENTIONALLY DEFERRED CUSTOMER-DEFECT CLASSIFICATION
        # In this bounded first Phase C increment, potential failure categories
        # (dangling aria-controls, unrevealed content, uncollapsed content, inert controls,
        # page script errors) are NOT classified as customer defects.
        # They evaluate to honest non-defect INCONCLUSIVE outcomes.
        # ==============================================================================

        deferred_category: Optional[str] = None
        deferred_reason: str = ""

        if controlled_id and controlled_found is False:
            deferred_category = DefectCategory.DANGLING_ARIA_CONTROLS.value
            deferred_reason = f"Disclosure specifies aria-controls='{controlled_id}', but element was not found in the DOM"

        elif state_toggled and exp_after is True and controlled_vis_after is False:
            deferred_category = DefectCategory.CONTENT_NOT_REVEALED.value
            deferred_reason = f"aria-expanded toggled to true, but controlled element '{controlled_id}' remained hidden"

        elif state_toggled and exp_after is False and controlled_vis_after is True:
            deferred_category = DefectCategory.CONTENT_NOT_COLLAPSED.value
            deferred_reason = f"aria-expanded toggled to false, but controlled element '{controlled_id}' remained visible"

        elif sanitized_click_errors:
            deferred_category = DefectCategory.PAGE_SCRIPT_ERROR.value
            deferred_reason = f"Target page threw uncaught exception ({sanitized_click_errors[0]}) during click, leaving control inert"

        elif not state_toggled and controlled_vis_before == controlled_vis_after:
            deferred_category = DefectCategory.INERT_DISCLOSURE_CONTROL.value
            deferred_reason = "Control received click, but neither disclosure state nor content visibility changed"

        if deferred_category:
            if getattr(self, "enable_defect_classification", False) and deferred_category != DefectCategory.INERT_DISCLOSURE_CONTROL.value:
                evidence = FunctionalEvidence(
                    action_attempted=execution_res.action_attempted,
                    action_confirmed=execution_res.action_confirmed,
                    expanded_before=exp_before,
                    expanded_after=exp_after,
                    selected_before=sel_before,
                    selected_after=sel_after,
                    screenshot_path=screenshot_path,
                    state_toggled=state_toggled,
                    controls_count_delta=ctrl_delta,
                    fingerprint_changed=fp_changed,
                    pre_state_fingerprint=pre_fp,
                    post_state_fingerprint=post_fp,
                    controlled_element_id=controlled_id,
                    controlled_element_found=controlled_found,
                    controlled_element_visible_before=controlled_vis_before,
                    controlled_element_visible_after=controlled_vis_after,
                    pre_observation_succeeded=pre_obs_ok,
                    post_observation_succeeded=post_obs_ok,
                    page_script_errors=sanitized_click_errors,
                    observation_sufficient=True,
                    observation_status="COMPLETED",
                    rationale=f"Confirmed functional defect: {deferred_reason} (category: {deferred_category})",
                )
                diag = {
                    "confirmed_defect_category": deferred_category,
                    "defect_reason": deferred_reason,
                }
                if sanitized_click_errors:
                    diag["script_errors"] = sanitized_click_errors
                return FunctionalEvaluationResult(
                    verdict=FunctionalOutcomeVerdict.SUPPORTED_FAILURE,
                    is_customer_defect=True,
                    defect_category=DefectCategory(deferred_category),
                    target_control_id=execution_res.target_control_id,
                    action_type=execution_res.action_type,
                    evidence=evidence,
                    execution_status=execution_res.status,
                    limitations=execution_res.limitations,
                    diagnostics=diag,
                )

            evidence = FunctionalEvidence(
                action_attempted=execution_res.action_attempted,
                action_confirmed=execution_res.action_confirmed,
                expanded_before=exp_before,
                expanded_after=exp_after,
                selected_before=sel_before,
                selected_after=sel_after,
                screenshot_path=screenshot_path,
                state_toggled=state_toggled,
                controls_count_delta=ctrl_delta,
                fingerprint_changed=fp_changed,
                pre_state_fingerprint=pre_fp,
                post_state_fingerprint=post_fp,
                controlled_element_id=controlled_id,
                controlled_element_found=controlled_found,
                controlled_element_visible_before=controlled_vis_before,
                controlled_element_visible_after=controlled_vis_after,
                pre_observation_succeeded=pre_obs_ok,
                post_observation_succeeded=post_obs_ok,
                page_script_errors=sanitized_click_errors,
                observation_sufficient=True,
                observation_status="COMPLETED",
                rationale=f"Automatic customer-defect classification is deferred in this increment: {deferred_reason} (deferred category: {deferred_category})",
            )
            diag = {
                "deferred_defect_category": deferred_category,
                "defect_classification_status": "DEFERRED_IN_FIRST_INCREMENT",
                "deferred_reason": deferred_reason,
            }
            if sanitized_click_errors:
                diag["script_errors"] = sanitized_click_errors
            return FunctionalEvaluationResult(
                verdict=FunctionalOutcomeVerdict.INCONCLUSIVE,
                is_customer_defect=False,
                defect_category=None,
                target_control_id=execution_res.target_control_id,
                action_type=execution_res.action_type,
                evidence=evidence,
                execution_status=execution_res.status,
                limitations=execution_res.limitations + (f"defect_classification_deferred_{deferred_category.lower()}",),
                diagnostics=diag,
            )

        # Fallthrough: Ambiguous / non-standard disclosure transition
        evidence = FunctionalEvidence(
            action_attempted=execution_res.action_attempted,
            action_confirmed=execution_res.action_confirmed,
            expanded_before=exp_before,
            expanded_after=exp_after,
            state_toggled=state_toggled,
            controls_count_delta=ctrl_delta,
            fingerprint_changed=fp_changed,
            pre_state_fingerprint=pre_fp,
            post_state_fingerprint=post_fp,
            controlled_element_id=controlled_id,
            controlled_element_found=controlled_found,
            controlled_element_visible_before=controlled_vis_before,
            controlled_element_visible_after=controlled_vis_after,
            pre_observation_succeeded=pre_obs_ok,
            post_observation_succeeded=post_obs_ok,
            page_script_errors=sanitized_click_errors,
            observation_sufficient=False,
            rationale="Action confirmed, but state transition evidence was ambiguous, incomplete, or non-standard",
        )
        diag = {"ambiguity_reason": "State transition did not match deterministic success pattern"}
        if sanitized_click_errors:
            diag["script_errors"] = sanitized_click_errors
        return FunctionalEvaluationResult(
            verdict=FunctionalOutcomeVerdict.INCONCLUSIVE,
            is_customer_defect=False,
            defect_category=None,
            target_control_id=execution_res.target_control_id,
            action_type=execution_res.action_type,
            evidence=evidence,
            execution_status=execution_res.status,
            limitations=execution_res.limitations,
            diagnostics=diag,
        )

    async def evaluate_disclosure_action(
        self,
        executor: GuardedActionExecutor,
        source_context: Any,
        approved_target_url: str,
        candidate_intent: CandidateActionIntent,
        timeout: float = 30.0,
        cancellation_token: Optional[Any] = None,
        reconstruction_timeout: Optional[float] = None,
        action_timeout: Optional[float] = None,
        lifecycle_tracker: Optional[Dict[str, Any]] = None,
        project_id: Optional[int] = None,
        viewport: Optional[Dict[str, int]] = None,
    ) -> FunctionalEvaluationResult:
        """Execute a guarded disclosure action with deep DOM observation and functional evaluation."""
        dom_evidence: Dict[str, Any] = {
            "structured_page_errors": [],
            "pre_controls_count": None,
            "controlled_element_id": None,
            "controlled_element_found": None,
            "controlled_element_visible_before": None,
            "controlled_element_visible_after": None,
            "pre_observation_succeeded": False,
            "post_observation_succeeded": False,
            "observation_error": None,
            "observation_status": ObservationStatus.MISSING,
            "observation_result": None,
            "observation_sufficient": False,
            "observation_budget_exhausted": False,
            "observation_provenance": "live_disposable_page",
            "qa_network_interference": False,
            "network_blocked_count": 0,
            "network_activity_detected": False,
            "multiple_controlled_elements": False,
        }

        orig_reconstruct = executor._reconstructor.reconstruct
        action_dispatch_active = False

        @asynccontextmanager
        async def observing_reconstruct(*args, **kwargs):
            nonlocal action_dispatch_active
            async with orig_reconstruct(*args, **kwargs) as sess:
                disp_page = sess.page

                barrier_sess = None
                try:
                    barrier_sess = sess.barrier_session
                except Exception:
                    barrier_sess = None

                pre_lockdown_blocked = barrier_sess.metrics.lockdown_blocked if barrier_sess else 0
                network_requests_attempted = 0
                network_requests_failed = 0

                def on_request(req):
                    nonlocal network_requests_attempted
                    network_requests_attempted += 1

                def on_request_failed(req):
                    nonlocal network_requests_failed
                    network_requests_failed += 1

                disp_page.on("request", on_request)
                disp_page.on("requestfailed", on_request_failed)

                # 1. Capture unhandled page script errors with privacy sanitization
                def on_page_error(err):
                    raw_str = str(err)
                    err_type = extract_safe_error_type(err)
                    is_lockdown = is_network_or_lockdown_artifact(raw_str)
                    is_bg = is_unrelated_background_error(raw_str)
                    dom_evidence["structured_page_errors"].append(
                        StructuredPageError(
                            error_type=err_type,
                            sanitized_message=None,
                            is_network_or_lockdown_artifact=is_lockdown,
                            is_unrelated_background_activity=is_bg,
                            occurred_during_click=action_dispatch_active,
                        )
                    )

                disp_page.on("pageerror", on_page_error)

                raw_control_id = candidate_intent.control_fingerprint
                frame_path = "0"
                raw_id = raw_control_id
                if ":" in raw_control_id:
                    frame_path, raw_id = raw_control_id.split(":", 1)

                target_frame = None
                try:
                    target_frame, _ = _resolve_target_frame(disp_page, frame_path)
                except Exception:
                    target_frame = None

                # Instrument locator and get_by_role clicks on target frame
                orig_locator = target_frame.locator if target_frame else None
                orig_get_by_role = target_frame.get_by_role if target_frame else None

                def wrap_locator(loc):
                    orig_click = loc.click

                    async def instrumented_click(*c_args, **c_kwargs):
                        nonlocal action_dispatch_active
                        action_dispatch_active = True
                        try:
                            # Pre-action observation immediately before click
                            pre_expanded = None
                            pre_vis = None
                            pre_selected = None
                            try:
                                if loc is not None:
                                    pre_state = await loc.evaluate(r"""(btn) => {
                                        if (!btn) return { btnFound: false };
                                        const exp = btn.getAttribute('aria-expanded');
                                        const sel = btn.getAttribute('aria-selected');
                                        const ctrlAttr = btn.getAttribute('aria-controls');
                                        let hasControls = false;
                                        let ctrlId = null;
                                        let multipleControls = false;
                                        let ctrlFound = false;
                                        let ctrlVis = false;
                                        if (ctrlAttr) {
                                            hasControls = true;
                                            const tokens = ctrlAttr.trim().split(/\s+/);
                                            if (tokens.length > 1) {
                                                multipleControls = true;
                                            } else if (tokens.length === 1 && tokens[0]) {
                                                ctrlId = tokens[0];
                                                const root = btn.getRootNode ? btn.getRootNode() : document;
                                                const ctrlEl = (root && root.getElementById) ? root.getElementById(ctrlId) : document.getElementById(ctrlId);
                                                if (ctrlEl) {
                                                    ctrlFound = true;
                                                    const rect = ctrlEl.getBoundingClientRect();
                                                    const style = window.getComputedStyle(ctrlEl);
                                                    const isDialog = ctrlEl.tagName.toLowerCase() === 'dialog' || ctrlEl.getAttribute('role') === 'dialog';
                                                    const isOpen = ctrlEl.hasAttribute('open');
                                                    ctrlVis = (isDialog && isOpen) || (!ctrlEl.hidden && style.display !== 'none' && style.visibility !== 'hidden' && (rect.width > 0 || rect.height > 0));
                                                }
                                            }
                                        }
                                        const anyDialogOpen = Array.from(document.querySelectorAll('dialog')).some(d => d.open || d.hasAttribute('open'));
                                        return {
                                            btnFound: true,
                                            exp: exp,
                                            sel: sel,
                                            hasControls: hasControls,
                                            multipleControls: multipleControls,
                                            ctrlId: ctrlId,
                                            ctrlFound: ctrlFound,
                                            ctrlVis: ctrlVis,
                                            anyDialogOpen: anyDialogOpen,
                                            btnText: (btn.textContent || btn.getAttribute('aria-label') || '').trim(),
                                            btnId: btn.id || null,
                                            ctrlAttr: ctrlAttr || null,
                                        };
                                    }""")

                                    if pre_state and pre_state.get("btnFound"):
                                        dom_evidence["pre_observation_succeeded"] = True
                                        dom_evidence["pre_controlled_element_id"] = pre_state.get("ctrlId")
                                        dom_evidence["controlled_element_id"] = pre_state.get("ctrlId")
                                        dom_evidence["controlled_element_found"] = pre_state.get("ctrlFound")
                                        dom_evidence["controlled_element_visible_before"] = pre_state.get("ctrlVis")
                                        dom_evidence["multiple_controlled_elements"] = pre_state.get("multipleControls")
                                        pre_expanded = pre_state.get("exp")
                                        pre_vis = pre_state.get("ctrlVis")
                                        pre_selected = pre_state.get("sel")
                                        pre_dialog_open = bool(pre_state.get("anyDialogOpen"))
                                        dom_evidence["selected_before"] = (pre_selected == "true")
                            except Exception:
                                dom_evidence["pre_observation_succeeded"] = False
                                dom_evidence["observation_error"] = "DOM_OBSERVATION_EXCEPTION"

                            res = await orig_click(*c_args, **c_kwargs)
                            if lifecycle_tracker is not None:
                                lifecycle_tracker["action_confirmed"] = True
                                lifecycle_tracker["phase"] = "POST_ACTION_OBSERVATION"
                            dom_evidence["action_confirmed"] = True
                            loc._action_confirmed = True
                            try:
                                disp_page._action_confirmed = True
                            except Exception:
                                pass

                            # Check if target control has a registered validation contract
                            from app.qa.validation_contract import find_validation_contract_for_control
                            btn_text = pre_state.get("btnText") if pre_state else None
                            if not btn_text and loc is not None:
                                try:
                                    btn_text = (await loc.text_content() or await loc.get_attribute("aria-label") or "").strip()
                                except Exception:
                                    pass

                            target_name = getattr(candidate_intent, "accessible_name", None) or btn_text
                            dom_attrs = {}
                            if pre_state:
                                if pre_state.get("btnId"):
                                    dom_attrs["id"] = pre_state.get("btnId")
                                if pre_state.get("ctrlId"):
                                    dom_attrs["controlsId"] = pre_state.get("ctrlId")
                                    dom_attrs["ariaControls"] = pre_state.get("ctrlId")

                            is_validation_control = (
                                find_validation_contract_for_control(
                                    control_id=candidate_intent.control_fingerprint,
                                    accessible_name=target_name,
                                    dom_attributes=dom_attrs or None,
                                    target_url=approved_target_url,
                                    project_id=project_id,
                                ) is not None
                            )

                            if is_validation_control:
                                dom_evidence["observation_status"] = ObservationStatus.COMPLETED
                                dom_evidence["observation_sufficient"] = True
                                dom_evidence["post_observation_succeeded"] = True
                            else:
                                # Bounded observation wait for supported asynchronous disclosure transitions
                                if action_timeout is not None:
                                    max_wait_ms = min(10000, max(100, int(action_timeout * 1000)))
                                else:
                                    max_wait_ms = min(1000, max(100, int(timeout * 1000)))
                                obs_res = await _wait_for_async_disclosure_transition(
                                    target_frame=target_frame,
                                    raw_id=raw_id,
                                    pre_expanded=pre_expanded,
                                    pre_vis=pre_vis,
                                    cancellation_token=cancellation_token,
                                    max_wait_ms=max_wait_ms,
                                    poll_interval_ms=25,
                                    pre_selected=pre_selected,
                                    target_locator=loc,
                                    pre_dialog_open=pre_dialog_open,
                                )
                                dom_evidence["observation_result"] = obs_res
                                dom_evidence["observation_status"] = obs_res.status
                                if obs_res.status == ObservationStatus.TIMED_OUT:
                                    dom_evidence["observation_budget_exhausted"] = True
                                    dom_evidence["observation_sufficient"] = False
                                elif obs_res.status in (ObservationStatus.FAILED, ObservationStatus.CANCELLED):
                                    dom_evidence["observation_sufficient"] = False
                                    if obs_res.error_type:
                                        dom_evidence["observation_error"] = obs_res.error_type
                                if obs_res.error_type == "MULTIPLE_ARIA_CONTROLS_UNSUPPORTED":
                                    dom_evidence["multiple_controlled_elements"] = True

                            # Post-action DOM state capture directly on locator
                            try:
                                if loc is not None:
                                    post_state = await loc.evaluate(r"""(btn) => {
                                        if (!btn) return { btnFound: false };
                                        const exp = btn.getAttribute('aria-expanded');
                                        const sel = btn.getAttribute('aria-selected');
                                        const ctrlAttr = btn.getAttribute('aria-controls');
                                        let ctrlFound = false;
                                        let ctrlVis = false;
                                        let ctrlId = null;
                                        let multipleControls = false;
                                        if (ctrlAttr) {
                                            const tokens = ctrlAttr.trim().split(/\s+/);
                                            if (tokens.length > 1) {
                                                multipleControls = true;
                                            } else if (tokens.length === 1 && tokens[0]) {
                                                ctrlId = tokens[0];
                                                const root = btn.getRootNode ? btn.getRootNode() : document;
                                                const ctrlEl = (root && root.getElementById) ? root.getElementById(ctrlId) : document.getElementById(ctrlId);
                                                if (ctrlEl) {
                                                    ctrlFound = true;
                                                    const rect = ctrlEl.getBoundingClientRect();
                                                    const style = window.getComputedStyle(ctrlEl);
                                                    const isDialog = ctrlEl.tagName.toLowerCase() === 'dialog' || ctrlEl.getAttribute('role') === 'dialog';
                                                    const isOpen = ctrlEl.hasAttribute('open');
                                                    ctrlVis = (isDialog && isOpen) || (!ctrlEl.hidden && style.display !== 'none' && style.visibility !== 'hidden' && (rect.width > 0 || rect.height > 0));
                                                }
                                            }
                                        }
                                        return {
                                            btnFound: true,
                                            exp: exp,
                                            sel: sel,
                                            ctrlId: ctrlId,
                                            ctrlFound: ctrlFound,
                                            ctrlVis: ctrlVis,
                                            multipleControls: multipleControls,
                                        };
                                    }""")

                                    if post_state and post_state.get("btnFound"):
                                        dom_evidence["post_observation_succeeded"] = True
                                        dom_evidence["post_controlled_element_id"] = post_state.get("ctrlId")
                                        dom_evidence["controlled_element_visible_after"] = post_state.get("ctrlVis")
                                        dom_evidence["selected_after"] = (post_state.get("sel") == "true")
                                        if dom_evidence.get("controlled_element_found") is None:
                                            dom_evidence["controlled_element_found"] = post_state.get("ctrlFound")
                                        if dom_evidence.get("controlled_element_id") is None:
                                            dom_evidence["controlled_element_id"] = post_state.get("ctrlId")
                                        if post_state.get("multipleControls"):
                                            dom_evidence["multiple_controlled_elements"] = True
                            except Exception:
                                dom_evidence["post_observation_succeeded"] = False
                                dom_evidence["observation_error"] = "DOM_OBSERVATION_EXCEPTION"

                            return res
                        finally:
                            action_dispatch_active = False

                    loc.click = instrumented_click
                    return loc

                if target_frame:
                    if orig_locator:
                        target_frame.locator = lambda *args, **kwargs: wrap_locator(orig_locator(*args, **kwargs))
                    if orig_get_by_role:
                        target_frame.get_by_role = lambda *args, **kwargs: wrap_locator(orig_get_by_role(*args, **kwargs))

                orig_close = disp_page.close

                async def observing_close(*c_args, **c_kwargs):
                    if barrier_sess:
                        try:
                            post_lockdown_blocked = barrier_sess.metrics.lockdown_blocked
                            delta = post_lockdown_blocked - pre_lockdown_blocked
                            has_lockdown_errs = any(
                                p.is_network_or_lockdown_artifact for p in dom_evidence.get("structured_page_errors", [])
                            )
                            if delta > 0 or network_requests_failed > 0 or (network_requests_attempted > 0 and has_lockdown_errs):
                                dom_evidence["qa_network_interference"] = True
                                dom_evidence["network_blocked_count"] = max(delta, network_requests_failed, network_requests_attempted)
                                dom_evidence["network_activity_detected"] = True
                        except Exception:
                            pass

                    # Settle final observation sufficiency flag
                    if (
                        dom_evidence.get("pre_observation_succeeded") is True
                        and dom_evidence.get("post_observation_succeeded") is True
                        and dom_evidence.get("observation_status") == ObservationStatus.COMPLETED
                        and not dom_evidence.get("observation_error")
                        and not dom_evidence.get("observation_budget_exhausted")
                        and not dom_evidence.get("multiple_controlled_elements")
                    ):
                        dom_evidence["observation_sufficient"] = True
                    else:
                        dom_evidence["observation_sufficient"] = False

                    # Discover controls/intents revealed on disposable page following action confirmation
                    if (
                        dom_evidence.get("action_confirmed")
                        and self._snapshot_adapter
                        and not disp_page.is_closed()
                    ):
                        try:
                            contract = {
                                "qa_owned": True,
                                "manual_visible_page": False,
                                "expected_context": disp_page.context,
                            }
                            post_snap = await self._snapshot_adapter.capture_snapshot(disp_page, contract)
                            if post_snap.coverage.terminal_status == "COMPLETE" and self._normalizer:
                                post_norm = self._normalizer.normalize_snapshot(post_snap)
                                if post_norm.terminal_status == "COMPLETE" and post_norm.semantic_state:
                                    dom_evidence["revealed_controls"] = list(post_norm.semantic_state.controls)
                                    from app.qa.state_fingerprint import CanonicalStateFingerprint
                                    dom_evidence["revealed_state_fingerprint"] = CanonicalStateFingerprint.generate(post_norm.semantic_state)
                                    if self._intent_factory:
                                        post_deriv = self._intent_factory.derive_intents(post_norm)
                                        dom_evidence["revealed_intents"] = list(post_deriv.intents)
                        except Exception:
                            pass

                    return await orig_close(*c_args, **c_kwargs)

                disp_page.close = observing_close
                yield sess

        try:
            executor._reconstructor.reconstruct = observing_reconstruct
            exec_res = await executor.execute(
                source_context=source_context,
                approved_target_url=approved_target_url,
                candidate_intent=candidate_intent,
                timeout=timeout,
                cancellation_token=cancellation_token,
                reconstruction_timeout=reconstruction_timeout,
                action_timeout=action_timeout,
                lifecycle_tracker=lifecycle_tracker,
                project_id=project_id,
                viewport=viewport,
            )
        finally:
            executor._reconstructor.reconstruct = orig_reconstruct

        return self.evaluate_result(exec_res, dom_evidence)
