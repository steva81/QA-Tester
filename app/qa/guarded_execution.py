"""Phase B2d: One Guarded Action + Observation Pipeline.

Executes at most one guarded, positively identified ephemeral disclosure-style action
within a fresh, successfully reconstructed disposable B2c session under verified network
lockdown, followed by read-only A2/A3 observation of state transition.
"""

import asyncio
from dataclasses import dataclass
from enum import Enum
import json
import math
import time
from typing import Any, Dict, Mapping, Optional, Tuple
import urllib.parse

from app.qa.browser_snapshot_adapter import (
    ReadOnlyBrowserSnapshotAdapter,
    CancellationToken,
)
from app.qa.candidate_intent_factory import CandidateActionIntentFactory
from app.qa.candidate_intent_graph import CandidateActionIntent
from app.qa.interaction_policy import InteractionSafetyPolicy, RiskClassification
from app.qa.isolated_network_barrier import BarrierState
from app.qa.semantic_model import SemanticControl, SemanticState
from app.qa.semantic_normalizer import SemanticNormalizer
from app.qa.state_fingerprint import CanonicalStateFingerprint
from app.qa.state_reconstruction import (
    ReconstructionStatus,
    StateReconstructionError,
    StateReconstructionResult,
    StateReconstructor,
)
from app.qa.validation_contract import (
    find_validation_contract_for_control,
    ValidationContract,
    ValidationContractResult,
    ValidationTestCaseResult,
    ValidationVerdict,
)


class GuardedExecutionStatus(str, Enum):
    """Authoritative outcome statuses for Phase B2d guarded action execution."""

    EXECUTED = "EXECUTED"
    RECONSTRUCTION_FAILED = "RECONSTRUCTION_FAILED"
    REVALIDATION_FAILED = "REVALIDATION_FAILED"
    STALE_STATE = "STALE_STATE"
    DISALLOWED_RISK_TIER = "DISALLOWED_RISK_TIER"
    UNSUPPORTED_ACTION_TYPE = "UNSUPPORTED_ACTION_TYPE"
    NOT_DISCLOSURE_CONTROL = "NOT_DISCLOSURE_CONTROL"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
    TARGET_NOT_VISIBLE_OR_DISABLED = "TARGET_NOT_VISIBLE_OR_DISABLED"
    LOCKDOWN_NOT_ACTIVE = "LOCKDOWN_NOT_ACTIVE"
    ACTION_FAILED = "ACTION_FAILED"
    OBSERVATION_FAILED = "OBSERVATION_FAILED"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


def _sanitize_for_repr(url_or_str: Optional[str]) -> Optional[str]:
    """Sanitize URL or metadata string to prevent credential or token leakage."""
    if not url_or_str:
        return url_or_str
    try:
        parsed = urllib.parse.urlsplit(url_or_str)
        if parsed.scheme and parsed.netloc:
            host_clean = parsed.hostname or ""
            if parsed.port:
                host_clean = f"{host_clean}:{parsed.port}"
            query_clean = "[REDACTED]" if parsed.query else ""
            return urllib.parse.urlunsplit(
                (parsed.scheme, host_clean, parsed.path, query_clean, "")
            )
    except Exception:
        pass
    return "<redacted>"


def _is_strictly_increasing_numeric_timestamp(
    pre_val: Optional[Any], curr_val: Optional[Any]
) -> bool:
    """Validate that curr_val is a valid numeric timestamp strictly greater than pre_val.
    
    Rejects missing, malformed, non-numeric, NaN, infinite, or non-increasing values.
    """
    if curr_val is None:
        return False
    curr_str = str(curr_val).strip()
    if not curr_str:
        return False
    try:
        curr_num = float(curr_str)
        if math.isnan(curr_num) or math.isinf(curr_num):
            return False
    except (ValueError, TypeError):
        return False

    if pre_val is None:
        return True
    
    pre_str = str(pre_val).strip()
    if not pre_str:
        return True
        
    try:
        pre_num = float(pre_str)
        if math.isnan(pre_num) or math.isinf(pre_num):
            return False
    except (ValueError, TypeError):
        return False
        
    return curr_num > pre_num


def _resolve_target_frame(page: Any, frame_path_str: str) -> Tuple[Optional[Any], Optional[str]]:
    """Resolve the exact Playwright Frame corresponding to the normalized frame_path_str.

    Returns (frame, error_reason).
    frame_path_str format: "0" for main frame, "0.0" for first child iframe, etc.
    """
    if not frame_path_str:
        return getattr(page, "main_frame", None), None

    parts = frame_path_str.split(".")
    try:
        int_parts = [int(p) for p in parts]
    except ValueError:
        return None, f"Invalid frame path format: {frame_path_str}"

    if not int_parts or int_parts[0] != 0:
        return None, f"Root frame index must be 0; received {frame_path_str}"

    current_frame = getattr(page, "main_frame", None)
    if current_frame is None:
        return None, "Page has no main_frame"

    for depth, idx in enumerate(int_parts[1:], start=1):
        if hasattr(current_frame, "is_detached") and current_frame.is_detached():
            return None, "Frame is detached"

        child_frames = getattr(current_frame, "child_frames", None)
        if child_frames is None or idx < 0 or idx >= len(child_frames):
            return None, f"Child frame index {idx} out of range at depth {depth}"

        current_frame = child_frames[idx]

    if hasattr(current_frame, "is_detached") and current_frame.is_detached():
        return None, "Frame is detached"

    return current_frame, None


@dataclass(frozen=True)
class GuardedExecutionResult:
    """Immutable result of a single guarded action execution and observation cycle.

    Strictly reports execution-control outcomes without functional defect classification.
    """

    status: GuardedExecutionStatus
    action_executed: bool
    action_type: Optional[str]
    target_control_id: Optional[str]
    pre_action_state_fingerprint: Optional[str]
    post_action_state_fingerprint: Optional[str]
    reconstruction_result: Optional[StateReconstructionResult]
    limitations: Tuple[str, ...]
    error_message: Optional[str] = None
    disclosure_expanded_before: Optional[bool] = None
    disclosure_expanded_after: Optional[bool] = None
    disclosure_selected_before: Optional[bool] = None
    disclosure_selected_after: Optional[bool] = None
    screenshot_path: Optional[str] = None
    post_action_controls_count: Optional[int] = None
    action_attempted: bool = False
    action_confirmed: bool = False
    dom_evidence: Optional[Dict[str, Any]] = None

    def __repr__(self) -> str:
        return (
            f"GuardedExecutionResult("
            f"status={self.status.value!r}, "
            f"action_executed={self.action_executed!r}, "
            f"action_attempted={self.action_attempted!r}, "
            f"action_confirmed={self.action_confirmed!r}, "
            f"action_type={self.action_type!r}, "
            f"target_control_id={self.target_control_id!r}, "
            f"pre_action_state_fingerprint={self.pre_action_state_fingerprint!r}, "
            f"post_action_state_fingerprint={self.post_action_state_fingerprint!r}, "
            f"disclosure_expanded_before={self.disclosure_expanded_before!r}, "
            f"disclosure_expanded_after={self.disclosure_expanded_after!r}, "
            f"limitations={self.limitations!r}, "
            f"error_message={self.error_message!r})"
        )

    def __str__(self) -> str:
        return (
            f"GuardedExecutionResult(status={self.status.value}, "
            f"action_executed={self.action_executed}, "
            f"target={self.target_control_id})"
        )


class GuardedActionExecutor:
    """Executes at most one guarded action within a fresh B2c disposable session.

    Enforces strict B2d invariants:
    1. Fresh disposable B2c state reconstruction with exact B1 revalidation.
    2. Network barrier lockdown MUST be verified active before execution.
    3. SAFE_CANDIDATE risk tier ONLY; POTENTIALLY_MUTATING never authorizes execution.
    4. Action type MUST be a click on a positively identified ephemeral disclosure pattern.
    5. Exact target identity and uniqueness required; fails closed on ambiguity or stale state.
    6. Target frame identity is strictly preserved; child-frame targets are executed in their frame.
    7. Fresh pre-action state fingerprint must match the approved candidate intent.
    8. Performs at most one normal Playwright click (no force, no JS click, no retry, no fallback).
    9. Captures post-action read-only A2/A3 observation and pre/post fingerprints.
    10. Execution stays strictly inside disposable context; never touches source context.
    11. Teardown failures remain explicit and do not erase evidence of confirmed actions.
    """

    def __init__(
        self,
        reconstructor: Optional[StateReconstructor] = None,
        snapshot_adapter: Optional[ReadOnlyBrowserSnapshotAdapter] = None,
        normalizer: Optional[SemanticNormalizer] = None,
    ) -> None:
        self._reconstructor = reconstructor or StateReconstructor()
        self._snapshot_adapter = snapshot_adapter or ReadOnlyBrowserSnapshotAdapter()
        self._normalizer = normalizer or SemanticNormalizer()

    def __repr__(self) -> str:
        return "<GuardedActionExecutor>"

    def __str__(self) -> str:
        return "GuardedActionExecutor"

    @staticmethod
    def _is_ephemeral_disclosure_control(
        control: SemanticControl,
        dom_meta: Optional[Dict[str, Any]] = None,
        target_url: Optional[str] = None,
        project_id: Optional[int] = None,
    ) -> bool:
        """Positively verify that a SemanticControl represents an authorized safe interaction pattern.

        Categories include:
        - Controls with an explicit expanded state (e.g. aria-expanded="false" or "true").
        - Elements with role "summary" or HTML <summary> tag.
        - Tabs with role "tab" (ephemeral view switching).
        - Controls with explicit aria-controls or data-toggle pointing to collapsible content.
        - Safe non-mutating dialog triggers (opening modal dialogs).
        - Safe non-mutating menu triggers (dropdown menus).
        - Safe local client-side validation triggers (e.g. date validation, constraint checking).

        Excludes mutating actions, form submits, and destructive actions.
        """
        # Form-mutating submits and raw inputs are NOT safe interactive control candidates
        if control.form_ownership and dom_meta and dom_meta.get("isFormSubmit"):
            return False
        if control.accessible_role in ("input", "textbox", "combobox", "checkbox", "radio"):
            return False

        # Positive evidence 1: Explicit expanded state tracked on semantic control
        if control.state_expanded is not None:
            return True

        # Positive evidence 2: Role summary or tag summary
        if control.accessible_role == "summary":
            return True

        # Positive evidence 3: Role tab or selectable state
        if control.accessible_role == "tab" or control.state_selected is not None:
            return True

        # Positive evidence 4: Role menuitem with verified supported action pattern
        if control.accessible_role == "menuitem":
            name_lower = (control.accessible_name or "").lower()
            is_mut = any(sig in name_lower for sig in ("save", "submit", "upload", "delete", "remove", "update", "purge", "revoke"))
            if is_mut or control.form_ownership:
                return False
            # Blocker 6: Require positive evidence of a supported action pattern
            # 1. Registered validation/interaction contract
            if find_validation_contract_for_control(
                control_id=control.control_id,
                accessible_name=control.accessible_name,
                dom_attributes=dom_meta,
                target_url=target_url,
                project_id=project_id,
            ) is not None:
                return True
            # 2. State toggle pattern (expanded, selected, or checked)
            if control.state_expanded is not None or control.state_selected is not None or control.state_checked is not None:
                return True
            # 3. Volatile metadata confirms supported pattern (has aria-controls, controls, or supported_pattern)
            if any("controls:" in m or "aria-controls:" in m or "supported_pattern" in m for m in (control.volatile_metadata or ())):
                return True
            # 4. DOM inspection confirms supported pattern (has aria-controls, dialog trigger, menu trigger, or accordion pattern)
            if dom_meta:
                if dom_meta.get("hasAriaControls") is True:
                    return True
                if dom_meta.get("isDialogTrigger") is True or dom_meta.get("hasAriaExpanded") is True:
                    return True
                if dom_meta.get("isAccordionBtn") is True:
                    return True
            # Unknown / unpatterned menu actions remain unsupported
            return False

        # Positive evidence 5: Explicit registered validation contract
        if find_validation_contract_for_control(
            control_id=control.control_id,
            accessible_name=control.accessible_name,
            dom_attributes=dom_meta,
            target_url=target_url,
            project_id=project_id,
        ) is not None:
            return True

        # Positive evidence 6: DOM inspection metadata (if available)
        if dom_meta:
            tag = (dom_meta.get("tagName") or "").lower()
            if tag == "summary":
                return True
            if dom_meta.get("hasAriaExpanded") is True:
                return True
            if dom_meta.get("hasAriaSelected") is True:
                return True
            if dom_meta.get("hasAriaControls") is True and control.accessible_role in ("button", "tab", "link"):
                # Form validation triggers require an explicit contract; uncontracted domain validation is unsupported
                ctrl_id = str(dom_meta.get("ariaControls") or dom_meta.get("controlsId") or "").lower()
                name_lower = (control.accessible_name or "").lower()
                is_validation = (
                    any(w in name_lower for w in ("validate", "check", "verify"))
                    or any(w in ctrl_id for w in ("feedback", "error", "validation"))
                )
                if is_validation:
                    return False
                return True
            if dom_meta.get("isDialogTrigger") is True:
                return True
            if dom_meta.get("isDialogClose") is True:
                return True
            if dom_meta.get("isMenuTrigger") is True:
                return True

        return False

    async def execute(
        self,
        source_context: Any,
        approved_target_url: str,
        candidate_intent: CandidateActionIntent,
        *,
        timeout: float = 15.0,
        wait_until: str = "load",
        cancellation_token: Optional[CancellationToken] = None,
        reconstruction_timeout: Optional[float] = None,
        action_timeout: Optional[float] = None,
        lifecycle_tracker: Optional[Dict[str, Any]] = None,
        project_id: Optional[int] = None,
        viewport: Optional[Dict[str, int]] = None,
    ) -> GuardedExecutionResult:
        """Safely execute at most one guarded action and capture post-action observation.

        Guarantees that:
        - All operations occur within an isolated disposable session.
        - Network lockdown is verified prior to execution.
        - Rejection occurs immediately for any disallowed risk tier, action type, or non-disclosure target.
        - Target frame identity is strictly preserved and resolved in its exact frame scope.
        - Pre-action state fingerprint is verified against the approved intent to prevent stale execution.
        - At most one normal Playwright click is attempted.
        - Pre/post fingerprints and observations are recorded.
        - All resources are fully cleaned up upon exit.
        - Teardown failures remain explicit and do not erase confirmed action history.
        """
        # 1. Type validation
        if not isinstance(candidate_intent, CandidateActionIntent):
            raise TypeError(
                f"candidate_intent must be a CandidateActionIntent; received {type(candidate_intent)!r}"
            )

        # 2. Risk classification gating: SAFE_CANDIDATE ONLY
        if candidate_intent.risk_classification != RiskClassification.SAFE_CANDIDATE:
            return GuardedExecutionResult(
                status=GuardedExecutionStatus.DISALLOWED_RISK_TIER,
                action_executed=False,
                action_type=candidate_intent.expected_action_type,
                target_control_id=candidate_intent.control_fingerprint,
                pre_action_state_fingerprint=candidate_intent.parent_state_fingerprint,
                post_action_state_fingerprint=None,
                reconstruction_result=None,
                limitations=(),
                error_message="Only SAFE_CANDIDATE risk tier authorizes execution; POTENTIALLY_MUTATING is rejected",
                action_attempted=False,
                action_confirmed=False,
            )

        # 3. Action type gating: Initially permit only click
        if candidate_intent.expected_action_type != "click":
            return GuardedExecutionResult(
                status=GuardedExecutionStatus.UNSUPPORTED_ACTION_TYPE,
                action_executed=False,
                action_type=candidate_intent.expected_action_type,
                target_control_id=candidate_intent.control_fingerprint,
                pre_action_state_fingerprint=candidate_intent.parent_state_fingerprint,
                post_action_state_fingerprint=None,
                reconstruction_result=None,
                limitations=(),
                error_message=f"Only 'click' actions on disclosure patterns are supported; received {candidate_intent.expected_action_type!r}",
                action_attempted=False,
                action_confirmed=False,
            )

        # 4. Early cancellation check
        if cancellation_token and cancellation_token.is_cancelled():
            return GuardedExecutionResult(
                status=GuardedExecutionStatus.CANCELLED,
                action_executed=False,
                action_type=candidate_intent.expected_action_type,
                target_control_id=candidate_intent.control_fingerprint,
                pre_action_state_fingerprint=candidate_intent.parent_state_fingerprint,
                post_action_state_fingerprint=None,
                reconstruction_result=None,
                limitations=(),
                error_message="Execution cancelled before state reconstruction",
                action_attempted=False,
                action_confirmed=False,
            )

        # Track execution progress across try block and context manager exit
        action_attempted: bool = False
        action_confirmed: bool = False
        locator: Any = None
        page: Any = None
        pre_state_fingerprint: Optional[str] = candidate_intent.parent_state_fingerprint
        post_state_fingerprint: Optional[str] = None
        reconstruction_res: Optional[StateReconstructionResult] = None
        target_control_id: Optional[str] = candidate_intent.control_fingerprint
        disclosure_expanded_before: Optional[bool] = None
        disclosure_expanded_after: Optional[bool] = None
        post_controls_count: Optional[int] = None
        all_limitations: Tuple[str, ...] = ()

        rec_timeout = float(reconstruction_timeout) if reconstruction_timeout is not None else float(timeout)
        act_timeout = float(action_timeout) if action_timeout is not None else float(timeout)

        # 5. Fresh disposable B2c reconstruction & exact B1 revalidation under lockdown
        try:
            if lifecycle_tracker is not None:
                lifecycle_tracker["phase"] = "RECONSTRUCTION"
            async with self._reconstructor.reconstruct(
                source_context=source_context,
                approved_target_url=approved_target_url,
                candidate_intent=candidate_intent,
                timeout=rec_timeout,
                wait_until=wait_until,
                lifecycle_tracker=lifecycle_tracker,
                cancellation_token=cancellation_token,
                viewport=viewport,
            ) as session:
                reconstruction_res = session.result

                # Check if reconstruction / revalidation failed
                if not reconstruction_res.is_revalidated:
                    status = (
                        GuardedExecutionStatus.REVALIDATION_FAILED
                        if reconstruction_res.status in (
                            ReconstructionStatus.STALE_STATE,
                            ReconstructionStatus.INTENT_NOT_PRESENT,
                        )
                        else GuardedExecutionStatus.RECONSTRUCTION_FAILED
                    )
                    return GuardedExecutionResult(
                        status=status,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=reconstruction_res.parent_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=reconstruction_res.limitations,
                        error_message=reconstruction_res.error_message
                        or f"Reconstruction did not revalidate: {reconstruction_res.status}",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                # 6. Verify completed network lockdown
                barrier = session.barrier_session
                if barrier is None or barrier.state != BarrierState.LOCKED_DOWN:
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.LOCKDOWN_NOT_ACTIVE,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=reconstruction_res.parent_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=reconstruction_res.limitations,
                        error_message="Network barrier lockdown is not active",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                page = session.page
                if page is None or page.is_closed():
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.FAILED,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=reconstruction_res.parent_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=reconstruction_res.limitations,
                        error_message="Disposable page is closed or not available",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                # 7. Pre-action observation & target identity validation
                eligibility_contract = {
                    "qa_owned": True,
                    "manual_visible_page": False,
                    "expected_context": session.disposable_context,
                }

                if lifecycle_tracker is not None:
                    lifecycle_tracker["phase"] = "PRE_ACTION_INSPECTION"

                pre_snapshot = await self._snapshot_adapter.capture_snapshot(
                    page, eligibility_contract, cancellation_token=cancellation_token
                )
                if pre_snapshot.coverage.terminal_status != "COMPLETE":
                    cancelled = (
                        pre_snapshot.coverage.terminal_status == "CANCELLED"
                        or (cancellation_token is not None and cancellation_token.is_cancelled())
                    )
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.CANCELLED if cancelled else GuardedExecutionStatus.FAILED,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=reconstruction_res.parent_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_snapshot.coverage.limitations,
                        error_message=f"Pre-action snapshot failed: {pre_snapshot.coverage.terminal_status}",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                pre_norm = self._normalizer.normalize_snapshot(pre_snapshot)
                if pre_norm.terminal_status != "COMPLETE" or pre_norm.semantic_state is None:
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.FAILED,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=reconstruction_res.parent_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations,
                        error_message=f"Pre-action semantic normalization failed: {pre_norm.terminal_status}",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                pre_state_fingerprint = CanonicalStateFingerprint.generate(pre_norm.semantic_state)

                # CONTRACT CHECK 2: Require fresh pre-action state fingerprint to match approved intent.
                # If the state changed (e.g. element text, layout, or controls modified), fail closed with STALE_STATE.
                if pre_state_fingerprint != candidate_intent.parent_state_fingerprint:
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.STALE_STATE,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations,
                        error_message="Pre-action state fingerprint does not match approved candidate intent (stale state)",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                # Locate target control in pre-action semantic state
                matching_controls = [
                    c for c in pre_norm.semantic_state.controls
                    if c.control_id == candidate_intent.control_fingerprint
                ]

                if not matching_controls:
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.TARGET_NOT_FOUND,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations,
                        error_message="Target control not found in current page state",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                if len(matching_controls) > 1:
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.AMBIGUOUS_TARGET,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations,
                        error_message="Target control ID matches multiple elements (ambiguous target)",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                target_control = matching_controls[0]

                # CONTRACT CHECK 2b: Recheck current policy/risk/action eligibility from fresh semantic state
                fresh_risk = InteractionSafetyPolicy.classify(target_control)
                if fresh_risk != RiskClassification.SAFE_CANDIDATE:
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.DISALLOWED_RISK_TIER,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations,
                        error_message=f"Fresh pre-action safety classification is {fresh_risk.value}; only SAFE_CANDIDATE is eligible",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                fresh_action = CandidateActionIntentFactory._action_type_for_role(target_control.accessible_role)
                if fresh_action != "click":
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.UNSUPPORTED_ACTION_TYPE,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations,
                        error_message=f"Fresh pre-action action type is {fresh_action!r}; only 'click' is eligible",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                if not target_control.visibility or not target_control.enabled:
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.TARGET_NOT_VISIBLE_OR_DISABLED,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations,
                        error_message="Target element is not visible or enabled in semantic model",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                # CONTRACT CHECK 1: Preserve and verify exact frame-qualified target identity.
                if ":" in target_control.control_id:
                    frame_path_str, raw_id = target_control.control_id.split(":", 1)
                else:
                    frame_path_str = "0"
                    raw_id = target_control.control_id

                target_frame, frame_err = _resolve_target_frame(page, frame_path_str)
                if target_frame is None:
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.TARGET_NOT_FOUND,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations,
                        error_message=f"Target frame '{frame_path_str}' could not be resolved: {frame_err}",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                # Inspect DOM metadata ON THE EXACT RESOLVED TARGET FRAME
                dom_meta = None
                try:
                    dom_meta = await target_frame.evaluate(
                        """(id) => {
                            let el = document.getElementById(id);
                            if (!el) {
                                return null;
                            }
                            const role = el.getAttribute('role');
                            const hasControls = el.hasAttribute('aria-controls');
                            const controlsId = el.getAttribute('aria-controls');
                            const hasPopup = el.getAttribute('aria-haspopup');
                            const typeAttr = (el.getAttribute('type') || '').toLowerCase();
                            const nameLower = (el.innerText || el.getAttribute('aria-label') || '').toLowerCase();

                            let isDialog = false;
                            if (hasPopup === 'dialog' || el.hasAttribute('data-modal-target') || (el.getAttribute('data-toggle') === 'modal')) {
                                isDialog = true;
                            } else if (hasControls && controlsId) {
                                const target = document.getElementById(controlsId);
                                if (target && (target.tagName.toLowerCase() === 'dialog' || target.getAttribute('role') === 'dialog')) {
                                    isDialog = true;
                                }
                            }

                            let isDialogClose = false;
                            if (el.closest('dialog') !== null || el.hasAttribute('data-close-dialog') || (el.getAttribute('data-dismiss') === 'modal')) {
                                isDialogClose = true;
                            }

                            let isMenu = (hasPopup === 'menu' || hasPopup === 'true' || el.getAttribute('data-toggle') === 'dropdown');

                            return {
                                tagName: el.tagName.toLowerCase(),
                                hasAriaExpanded: el.hasAttribute('aria-expanded'),
                                ariaExpanded: el.getAttribute('aria-expanded'),
                                hasAriaSelected: el.hasAttribute('aria-selected'),
                                ariaSelected: el.getAttribute('aria-selected'),
                                role: role,
                                hasAriaControls: hasControls,
                                controlsId: controlsId,
                                ariaControls: controlsId,
                                hasAriaHaspopup: hasPopup,
                                isDialogTrigger: isDialog,
                                isDialogClose: isDialogClose,
                                isMenuTrigger: isMenu,
                                isAccordionBtn: el.classList.contains('accordion-btn'),
                                isFormSubmit: typeAttr === 'submit',
                                isVisible: (el.offsetWidth > 0 && el.offsetHeight > 0),
                                isEnabled: !el.disabled && !el.hasAttribute('disabled'),
                            };
                        }""",
                        raw_id,
                    )
                except Exception:
                    pass

                # Positive ephemeral disclosure verification
                if not self._is_ephemeral_disclosure_control(target_control, dom_meta, target_url=approved_target_url, project_id=project_id):
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.NOT_DISCLOSURE_CONTROL,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations,
                        error_message="Target element is not a positively identified ephemeral disclosure pattern",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                # Determine Playwright locator SCOPED TO TARGET FRAME with safe escaping
                if raw_id and not raw_id.startswith("auto-"):
                    escaped_id = raw_id.replace("\\", "\\\\").replace('"', '\\"')
                    locator = target_frame.locator(f'[id="{escaped_id}"]')
                else:
                    # Auto-generated ID: locate via role and exact accessible name within target frame
                    locator = target_frame.get_by_role(
                        target_control.accessible_role,
                        name=target_control.accessible_name,
                        exact=True,
                    )

                # Exact uniqueness verification in Playwright DOM on target frame
                try:
                    target_count = await locator.count()
                except Exception:
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.TARGET_NOT_FOUND,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations,
                        error_message="Failed to count locator matches in disposable target frame",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                if target_count == 0:
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.TARGET_NOT_FOUND,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations,
                        error_message="Target element not found in disposable target frame DOM",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                if target_count > 1:
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.AMBIGUOUS_TARGET,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations,
                        error_message=f"Target locator matched {target_count} elements in target frame; unique target required",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                # Visibility and enabled gating on locator
                try:
                    is_visible = await locator.is_visible()
                    is_enabled = await locator.is_enabled()
                except Exception:
                    is_visible = False
                    is_enabled = False

                if not is_visible or not is_enabled:
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.TARGET_NOT_VISIBLE_OR_DISABLED,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations,
                        error_message="Target element is not visible or is disabled",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                # Check cancellation immediately before action execution
                if cancellation_token and cancellation_token.is_cancelled():
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.CANCELLED,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations,
                        error_message="Execution cancelled immediately prior to action",
                        action_attempted=False,
                        action_confirmed=False,
                    )

                # Observe pre-action dialog state
                dialog_state_before = None
                try:
                    dialog_state_before = await target_frame.evaluate("""() => {
                        const dialogs = Array.from(document.querySelectorAll('dialog'));
                        const anyModal = dialogs.some(d => {
                            try { return d.open && (typeof d.matches === 'function' ? d.matches(':modal') : true); }
                            catch(e) { return d.open; }
                        });
                        const anyOpen = dialogs.some(d => d.open || d.hasAttribute('open'));
                        return { anyModal, anyOpen, count: dialogs.length };
                    }""")
                except Exception:
                    pass

                # 9. Perform action (contract validation or normal Playwright click)
                if lifecycle_tracker is not None:
                    lifecycle_tracker["phase"] = "ACTION_DISPATCH"
                    lifecycle_tracker["action_attempted"] = True
                action_attempted = True
                action_error: Optional[str] = None
                validation_contract = find_validation_contract_for_control(
                    control_id=target_control.control_id,
                    accessible_name=target_control.accessible_name,
                    dom_attributes=dom_meta,
                    target_url=approved_target_url,
                    project_id=project_id,
                )
                validation_result = None

                try:
                    if validation_contract:
                        # 1. Pre-execution checks
                        if cancellation_token and cancellation_token.is_cancelled():
                            return GuardedExecutionResult(
                                status=GuardedExecutionStatus.CANCELLED,
                                action_executed=False,
                                action_type=candidate_intent.expected_action_type,
                                target_control_id=candidate_intent.control_fingerprint,
                                pre_action_state_fingerprint=pre_state_fingerprint,
                                post_action_state_fingerprint=None,
                                reconstruction_result=reconstruction_res,
                                limitations=pre_norm.limitations + ("operation_cancelled",),
                                error_message="Validation cancelled before execution",
                                action_attempted=True,
                                action_confirmed=False,
                            )

                        contract_deadline = time.monotonic() + (act_timeout * max(1, len(validation_contract.test_cases)))

                        # Verify contract completion configuration
                        if not validation_contract.has_completion_semantics:
                            _, cfg_err = validation_contract.validate_completion_configuration()
                            return GuardedExecutionResult(
                                status=GuardedExecutionStatus.ACTION_FAILED,
                                action_executed=False,
                                action_type=candidate_intent.expected_action_type,
                                target_control_id=candidate_intent.control_fingerprint,
                                pre_action_state_fingerprint=pre_state_fingerprint,
                                post_action_state_fingerprint=None,
                                reconstruction_result=reconstruction_res,
                                limitations=pre_norm.limitations + ("missing_validation_completion_configuration",),
                                error_message=f"Contract '{validation_contract.contract_id}' has invalid or missing completion semantics: {cfg_err}",
                                action_attempted=True,
                                action_confirmed=False,
                                dom_evidence={
                                    "validation_result": ValidationContractResult(
                                        contract_id=validation_contract.contract_id,
                                        rule_name=validation_contract.rule_name,
                                        verdict=ValidationVerdict.INCOMPLETE_COVERAGE,
                                        all_passed=False,
                                        is_customer_defect=False,
                                        defect_description=f"Invalid or missing completion configuration: {cfg_err}",
                                        case_results=(),
                                        tested_cases_count=0,
                                        contract=validation_contract,
                                    )
                                },
                            )

                        # 2. Verify prerequisites: unique, present, supported, editable fields
                        field_prereq_error = None
                        for field_name, f_sel in (validation_contract.field_selectors or {}).items():
                            in_loc = target_frame.locator(f_sel)
                            f_count = await in_loc.count()
                            if f_count == 0:
                                field_prereq_error = f"Missing required input field '{field_name}' ({f_sel})"
                                break
                            elif f_count > 1:
                                field_prereq_error = f"Ambiguous input field '{field_name}': multiple ({f_count}) elements matched '{f_sel}'"
                                break
                            try:
                                is_dis = await in_loc.is_disabled()
                                if is_dis:
                                    field_prereq_error = f"Input field '{field_name}' is disabled / not editable"
                                    break
                            except Exception as ex:
                                field_prereq_error = f"Input field '{field_name}' is not editable: {ex}"
                                break

                        if not field_prereq_error:
                            fb_loc = target_frame.locator(validation_contract.feedback_selector)
                            fb_cnt = await fb_loc.count()
                            if fb_cnt == 0:
                                field_prereq_error = f"Missing required feedback element ({validation_contract.feedback_selector})"
                            elif fb_cnt > 1:
                                field_prereq_error = f"Ambiguous feedback element: multiple ({fb_cnt}) elements matched '{validation_contract.feedback_selector}'"

                        if field_prereq_error:
                            return GuardedExecutionResult(
                                status=GuardedExecutionStatus.ACTION_FAILED,
                                action_executed=False,
                                action_type=candidate_intent.expected_action_type,
                                target_control_id=candidate_intent.control_fingerprint,
                                pre_action_state_fingerprint=pre_state_fingerprint,
                                post_action_state_fingerprint=None,
                                reconstruction_result=reconstruction_res,
                                limitations=pre_norm.limitations + ("validation_prerequisite_failed",),
                                error_message=f"Validation prerequisite failed: {field_prereq_error}",
                                action_attempted=True,
                                action_confirmed=False,
                                dom_evidence={
                                    "validation_result": ValidationContractResult(
                                        contract_id=validation_contract.contract_id,
                                        rule_name=validation_contract.rule_name,
                                        verdict=ValidationVerdict.PREREQUISITE_FAILED,
                                        all_passed=False,
                                        is_customer_defect=False,
                                        defect_description=f"Validation prerequisite failed: {field_prereq_error}",
                                        case_results=(),
                                        tested_cases_count=0,
                                        contract=validation_contract,
                                    )
                                },
                            )

                        # 3. Guarded multi-action loop across test cases
                        case_observations = []
                        execution_interrupted = False
                        interrupted_status = GuardedExecutionStatus.ACTION_FAILED
                        interrupted_reason = None

                        for tc in validation_contract.test_cases:
                            # Step cancellation check
                            if cancellation_token and cancellation_token.is_cancelled():
                                execution_interrupted = True
                                interrupted_status = GuardedExecutionStatus.CANCELLED
                                interrupted_reason = "Execution cancelled before validation case"
                                break

                            # Step contract deadline check
                            rem_contract_s = contract_deadline - time.monotonic()
                            if rem_contract_s <= 0:
                                execution_interrupted = True
                                interrupted_reason = "Action budget / deadline exceeded during validation"
                                break

                            # Per-test-case deadline: each test case has up to act_timeout, strictly bounded by overall contract deadline
                            case_deadline = min(contract_deadline, time.monotonic() + act_timeout)

                            # Record pre-case feedback text and visibility for freshness tracking
                            pre_fb_text = ""
                            pre_fb_vis = False
                            pre_marker_val = None
                            pre_timestamp_val = None
                            fb_loc = target_frame.locator(validation_contract.feedback_selector)
                            rem_ms = int((case_deadline - time.monotonic()) * 1000)
                            if rem_ms <= 0:
                                execution_interrupted = True
                                interrupted_status = GuardedExecutionStatus.ACTION_FAILED
                                interrupted_reason = "Deadline exceeded before baseline capture"
                                break
                            if await fb_loc.count() > 0:
                                marker_attr_arg = validation_contract.completion_marker_attr or ""
                                timestamp_attr_arg = validation_contract.completion_timestamp_attr or ""
                                try:
                                    eval_data = await fb_loc.evaluate(
                                        """(el, args) => {
                                            const rect = el.getBoundingClientRect();
                                            const style = window.getComputedStyle(el);
                                            const vis = !el.hidden && style.display !== 'none' && style.visibility !== 'hidden' && (rect.width > 0 || rect.height > 0 || (el.innerText || '').trim().length > 0);
                                            return {
                                                vis: vis,
                                                txt: (el.textContent || '').trim(),
                                                markerVal: args.mAttr ? el.getAttribute(args.mAttr) : null,
                                                timestampVal: args.tAttr ? el.getAttribute(args.tAttr) : null,
                                            };
                                        }""",
                                        {"mAttr": marker_attr_arg, "tAttr": timestamp_attr_arg},
                                    )
                                    pre_fb_vis = bool(eval_data.get("vis"))
                                    pre_fb_text = eval_data.get("txt", "")
                                    pre_marker_val = eval_data.get("markerVal")
                                    pre_timestamp_val = eval_data.get("timestampVal")
                                except Exception:
                                    pass

                            # Fill every input field with value verification
                            fill_error = None
                            for field_name, f_val in tc.input_values.items():
                                if cancellation_token and cancellation_token.is_cancelled():
                                    fill_error = "cancelled"
                                    break
                                rem_ms = int((case_deadline - time.monotonic()) * 1000)
                                if rem_ms <= 0:
                                    fill_error = "deadline_exceeded"
                                    break
                                f_sel = validation_contract.field_selectors.get(field_name, f"#{field_name}")
                                in_loc = target_frame.locator(f_sel)
                                fill_timeout = min(2000, max(50, rem_ms))
                                await in_loc.fill(f_val, timeout=fill_timeout)
                                # Verify entered value
                                entered_val = await in_loc.input_value(timeout=min(500, max(20, rem_ms)))
                                if entered_val != f_val:
                                    fill_error = f"Entered value verification failed for '{field_name}': expected '{f_val}', got '{entered_val}'"
                                    break

                            if fill_error:
                                execution_interrupted = True
                                if fill_error == "cancelled":
                                    interrupted_status = GuardedExecutionStatus.CANCELLED
                                    interrupted_reason = "Execution cancelled during field fill"
                                else:
                                    interrupted_status = GuardedExecutionStatus.ACTION_FAILED
                                    interrupted_reason = fill_error
                                break

                            # Pre-click cancellation check
                            if cancellation_token and cancellation_token.is_cancelled():
                                execution_interrupted = True
                                interrupted_status = GuardedExecutionStatus.CANCELLED
                                interrupted_reason = "Execution cancelled before trigger click"
                                break

                            # Pre-click deadline check
                            rem_ms = int((case_deadline - time.monotonic()) * 1000)
                            if rem_ms <= 0:
                                execution_interrupted = True
                                interrupted_status = GuardedExecutionStatus.ACTION_FAILED
                                interrupted_reason = "Deadline exceeded before trigger click"
                                break

                            # Bounded click
                            click_timeout = min(3000, max(20, rem_ms))
                            await locator.click(force=False, timeout=click_timeout)

                            # Wait for fresh case-specific feedback strictly bounded by remaining deadline
                            rem_s = case_deadline - time.monotonic()
                            if rem_s <= 0:
                                execution_interrupted = True
                                interrupted_status = GuardedExecutionStatus.ACTION_FAILED
                                interrupted_reason = "Deadline exceeded immediately after click"
                                case_observations.append((tc, pre_fb_vis, pre_fb_text, False, "TIMED_OUT"))
                                break

                            poll_duration = min(3.0, max(0.0, rem_s))
                            poll_deadline = time.monotonic() + poll_duration
                            fb_text = ""
                            fb_vis = False
                            fresh = False
                            obs_status = "COMPLETED"
                            observed_loading_state = False

                            if lifecycle_tracker is not None:
                                lifecycle_tracker["phase"] = "WAITING_FOR_FEEDBACK"

                            loading_classes = {"loading", "is-loading", "validating", "pending", "spinner", "busy"}
                            loading_texts = {
                                "validating", "validating...", "checking", "checking...",
                                "loading", "loading...", "please wait", "please wait...",
                                "evaluating", "evaluating...",
                            }

                            while time.monotonic() < poll_deadline:
                                # Check cancellation during observation polling
                                if cancellation_token and cancellation_token.is_cancelled():
                                    execution_interrupted = True
                                    interrupted_status = GuardedExecutionStatus.CANCELLED
                                    interrupted_reason = "Execution cancelled during feedback observation"
                                    obs_status = "CANCELLED"
                                    break

                                rem_overall_ms = int((case_deadline - time.monotonic()) * 1000)
                                if rem_overall_ms <= 0:
                                    execution_interrupted = True
                                    interrupted_status = GuardedExecutionStatus.ACTION_FAILED
                                    interrupted_reason = "Deadline exceeded during feedback observation"
                                    obs_status = "TIMED_OUT"
                                    break

                                curr_loc_timeout = min(200, max(20, rem_overall_ms))

                                if await fb_loc.count() > 0:
                                    marker_attr_arg = validation_contract.completion_marker_attr or ""
                                    timestamp_attr_arg = validation_contract.completion_timestamp_attr or ""
                                    try:
                                        eval_data = await fb_loc.evaluate(
                                            """(el, args) => {
                                                const rect = el.getBoundingClientRect();
                                                const style = window.getComputedStyle(el);
                                                const vis = !el.hidden && style.display !== 'none' && style.visibility !== 'hidden' && (rect.width > 0 || rect.height > 0 || (el.innerText || '').trim().length > 0);
                                                return {
                                                    vis: vis,
                                                    txt: (el.textContent || '').trim(),
                                                    ariaBusy: (el.getAttribute('aria-busy') || '').toLowerCase().trim(),
                                                    className: (el.getAttribute('class') || '').toLowerCase(),
                                                    markerVal: args.mAttr ? el.getAttribute(args.mAttr) : null,
                                                    timestampVal: args.tAttr ? el.getAttribute(args.tAttr) : null,
                                                };
                                            }""",
                                            {"mAttr": marker_attr_arg, "tAttr": timestamp_attr_arg},
                                        )
                                        curr_vis = bool(eval_data.get("vis"))
                                        curr_txt = eval_data.get("txt", "")
                                        aria_busy = eval_data.get("ariaBusy", "")
                                        class_attr = eval_data.get("className", "")
                                        marker_val = eval_data.get("markerVal")
                                        timestamp_val = eval_data.get("timestampVal")
                                    except Exception:
                                        curr_vis = False
                                        curr_txt = ""
                                        aria_busy = ""
                                        class_attr = ""
                                        marker_val = None
                                        timestamp_val = None

                                    classes_set = set(class_attr.split())
                                    curr_txt_lower = curr_txt.lower().strip()

                                    is_loading = (
                                        aria_busy == "true"
                                        or bool(classes_set & loading_classes)
                                        or (curr_txt_lower in loading_texts)
                                        or curr_txt_lower.startswith(("validating ", "checking ", "loading ", "please wait"))
                                    )

                                    if is_loading:
                                        observed_loading_state = True
                                        # Intermediate presentation / loading update: continue polling for real completed result
                                        sleep_time = min(0.04, max(0.005, (poll_deadline - time.monotonic())))
                                        if sleep_time > 0:
                                            await asyncio.sleep(sleep_time)
                                        continue

                                    # Distinguish observations from conclusions:
                                    # Cosmetic mutations or presentation loading cycles must NOT establish completion
                                    # for contracts expecting case markers or timestamps.
                                    # Completion must strictly establish that the current invocation evaluated the active case.
                                    case_completed = False
                                    eff_mode = validation_contract.effective_completion_mode

                                    if eff_mode == "case_marker":
                                        # Strict case marker mode: feedback element MUST identify the active test case
                                        if marker_val == tc.case_id:
                                            if pre_marker_val != tc.case_id:
                                                # Marker transitioned from non-active case to active case:
                                                # If timestamp attribute is also configured, require fresh numeric timestamp
                                                if validation_contract.completion_timestamp_attr:
                                                    if _is_strictly_increasing_numeric_timestamp(pre_timestamp_val, timestamp_val):
                                                        case_completed = True
                                                else:
                                                    case_completed = True
                                            else:
                                                # Marker was ALREADY tc.case_id before the action!
                                                # Pre-existing marker cannot prove current invocation.
                                                # Presentation or loading-only cycles CANNOT satisfy a case_marker contract.
                                                # Requires explicit strictly increasing numeric timestamp:
                                                if validation_contract.completion_timestamp_attr:
                                                    if _is_strictly_increasing_numeric_timestamp(pre_timestamp_val, timestamp_val):
                                                        case_completed = True

                                    elif eff_mode == "timestamp":
                                        # Strict numeric timestamp mode
                                        if _is_strictly_increasing_numeric_timestamp(pre_timestamp_val, timestamp_val):
                                            case_completed = True

                                    elif eff_mode == "token":
                                        # Strict token mode: non-empty invocation token distinct from baseline
                                        if marker_val and str(marker_val).strip() and marker_val != pre_marker_val:
                                            case_completed = True

                                    elif eff_mode == "loading_transition":
                                        # Explicit loading transition mode: genuine observed loading state followed by completion
                                        if observed_loading_state and not is_loading and curr_txt:
                                            case_completed = True

                                    if case_completed:
                                        fb_text = curr_txt
                                        fb_vis = curr_vis
                                        fresh = True
                                        obs_status = "COMPLETED"
                                        break

                                sleep_time = min(0.04, max(0.005, (poll_deadline - time.monotonic())))
                                if sleep_time > 0:
                                    await asyncio.sleep(sleep_time)

                            if not fresh:
                                if not execution_interrupted:
                                    execution_interrupted = True
                                    interrupted_status = GuardedExecutionStatus.ACTION_FAILED
                                    interrupted_reason = f"Fresh validation evidence could not be established for case '{tc.case_id}': feedback unchanged or timed out"
                                    obs_status = "STALE_FEEDBACK"

                                if await fb_loc.count() > 0:
                                    try:
                                        fb_text = (await fb_loc.text_content(timeout=min(200, max(20, int((case_deadline - time.monotonic()) * 1000)))) or "").strip()
                                        fb_vis = await fb_loc.is_visible()
                                    except Exception:
                                        pass

                            case_observations.append((tc, fb_vis, fb_text, fresh, obs_status))

                            if execution_interrupted:
                                # Stop subsequent actions immediately after cancellation or timeout
                                break

                        if execution_interrupted:
                            val_res = validation_contract.evaluate_observations(case_observations)
                            return GuardedExecutionResult(
                                status=interrupted_status,
                                action_executed=True,
                                action_type=candidate_intent.expected_action_type,
                                target_control_id=candidate_intent.control_fingerprint,
                                pre_action_state_fingerprint=pre_state_fingerprint,
                                post_action_state_fingerprint=None,
                                reconstruction_result=reconstruction_res,
                                limitations=pre_norm.limitations + (
                                    ("operation_cancelled",)
                                    if interrupted_status == GuardedExecutionStatus.CANCELLED
                                    else ("validation_execution_interrupted",)
                                ),
                                error_message=interrupted_reason or "Validation execution was interrupted",
                                action_attempted=True,
                                action_confirmed=True,
                                dom_evidence={"validation_result": val_res},
                            )

                        validation_result = validation_contract.evaluate_observations(case_observations)
                        action_confirmed = True
                        if lifecycle_tracker is not None:
                            lifecycle_tracker["action_confirmed"] = True
                    else:
                        # Exact single click invocation with bounded timeout
                        await locator.click(force=False, timeout=min(10000, max(100, int(act_timeout * 1000))))
                        action_confirmed = True
                        if lifecycle_tracker is not None:
                            lifecycle_tracker["action_confirmed"] = True
                except Exception as e:
                    action_error = f"Action click failed: {type(e).__name__}"
                    if (
                        (lifecycle_tracker and lifecycle_tracker.get("action_confirmed"))
                        or getattr(locator, "_action_confirmed", False)
                        or (page and getattr(page, "_action_confirmed", False))
                    ):
                        action_confirmed = True

                if not action_confirmed:
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.ACTION_FAILED,
                        action_executed=False,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations,
                        error_message=action_error or "Playwright click failed",
                        action_attempted=True,
                        action_confirmed=False,
                    )

                if action_error is not None:
                    # The underlying click succeeded and confirmed, but an observation or wrapper exception occurred
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.OBSERVATION_FAILED,
                        action_executed=True,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations + ("post_click_observation_exception",),
                        error_message=f"Observation failed after confirmed click: {action_error}",
                        action_attempted=True,
                        action_confirmed=True,
                    )

                # 10. Post-action read-only A2/A3 observation
                if lifecycle_tracker is not None:
                    lifecycle_tracker["phase"] = "POST_ACTION_OBSERVATION"
                try:
                    post_snapshot = await self._snapshot_adapter.capture_snapshot(
                        page, eligibility_contract, cancellation_token=cancellation_token
                    )
                except Exception as snap_exc:
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.OBSERVATION_FAILED,
                        action_executed=True,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=pre_norm.limitations + ("post_action_snapshot_exception",),
                        error_message=f"Post-action snapshot exception: {type(snap_exc).__name__}",
                        action_attempted=True,
                        action_confirmed=True,
                    )
                if post_snapshot.coverage.terminal_status != "COMPLETE":
                    is_cancelled = (
                        post_snapshot.coverage.terminal_status == "CANCELLED"
                        or (cancellation_token and cancellation_token.is_cancelled())
                    )
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.CANCELLED if is_cancelled else GuardedExecutionStatus.OBSERVATION_FAILED,
                        action_executed=True,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=post_snapshot.coverage.limitations + (("operation_cancelled",) if is_cancelled else ()),
                        error_message="Execution cancelled during post-action observation" if is_cancelled else f"Post-action snapshot failed: {post_snapshot.coverage.terminal_status}",
                        action_attempted=True,
                        action_confirmed=True,
                    )

                post_norm = self._normalizer.normalize_snapshot(post_snapshot)
                if post_norm.terminal_status != "COMPLETE" or post_norm.semantic_state is None:
                    is_cancelled = (
                        post_norm.terminal_status == "CANCELLED"
                        or (cancellation_token and cancellation_token.is_cancelled())
                    )
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.CANCELLED if is_cancelled else GuardedExecutionStatus.OBSERVATION_FAILED,
                        action_executed=True,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=post_norm.limitations + (("operation_cancelled",) if is_cancelled else ()),
                        error_message="Execution cancelled during post-action semantic normalization" if is_cancelled else f"Post-action semantic normalization failed: {post_norm.terminal_status}",
                        action_attempted=True,
                        action_confirmed=True,
                    )

                if cancellation_token and cancellation_token.is_cancelled():
                    return GuardedExecutionResult(
                        status=GuardedExecutionStatus.CANCELLED,
                        action_executed=True,
                        action_type=candidate_intent.expected_action_type,
                        target_control_id=candidate_intent.control_fingerprint,
                        pre_action_state_fingerprint=pre_state_fingerprint,
                        post_action_state_fingerprint=None,
                        reconstruction_result=reconstruction_res,
                        limitations=("operation_cancelled",),
                        error_message="Execution cancelled after post-action observation",
                        action_attempted=True,
                        action_confirmed=True,
                    )

                post_state_fingerprint = CanonicalStateFingerprint.generate(post_norm.semantic_state)

                # Determine disclosure and tab state change
                post_matching = [
                    c for c in post_norm.semantic_state.controls
                    if c.control_id == candidate_intent.control_fingerprint
                ]
                disclosure_expanded_after = post_matching[0].state_expanded if post_matching else None
                disclosure_expanded_before = target_control.state_expanded
                disclosure_selected_after = post_matching[0].state_selected if post_matching else None
                disclosure_selected_before = target_control.state_selected
                post_controls_count = len(post_norm.semantic_state.controls)

                # Capture post-action screenshot if page is available
                screenshot_path = None
                if page and hasattr(page, "screenshot") and not getattr(page, "is_closed", lambda: True)():
                    try:
                        import os
                        import uuid
                        from app.paths import get_screenshots_dir
                        safe_fid = candidate_intent.control_fingerprint.replace(":", "_").replace("/", "_").replace(".", "_")
                        filename = f"interactive_{safe_fid}_{uuid.uuid4().hex[:6]}.png"
                        target_path = os.path.join(get_screenshots_dir(), filename)
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        await page.screenshot(path=target_path)
                        screenshot_path = target_path
                    except Exception:
                        pass

                # Observe post-action dialog state
                dialog_state_after = None
                try:
                    dialog_state_after = await target_frame.evaluate("""() => {
                        const dialogs = Array.from(document.querySelectorAll('dialog'));
                        const anyModal = dialogs.some(d => {
                            try { return d.open && (typeof d.matches === 'function' ? d.matches(':modal') : true); }
                            catch(e) { return d.open; }
                        });
                        const anyOpen = dialogs.some(d => d.open || d.hasAttribute('open'));
                        return { anyModal, anyOpen, count: dialogs.length };
                    }""")
                except Exception:
                    pass

                all_limitations = tuple(
                    sorted(list(set(reconstruction_res.limitations + pre_norm.limitations + post_norm.limitations)))
                )

                dom_evidence_dict = {
                    "validation_result": validation_result,
                    "dialog_state_before": dialog_state_before,
                    "dialog_state_after": dialog_state_after,
                }

                executed_res = GuardedExecutionResult(
                    status=GuardedExecutionStatus.EXECUTED,
                    action_executed=True,
                    action_type="click",
                    target_control_id=candidate_intent.control_fingerprint,
                    pre_action_state_fingerprint=pre_state_fingerprint,
                    post_action_state_fingerprint=post_state_fingerprint,
                    reconstruction_result=reconstruction_res,
                    limitations=all_limitations,
                    error_message=None,
                    disclosure_expanded_before=disclosure_expanded_before,
                    disclosure_expanded_after=disclosure_expanded_after,
                    disclosure_selected_before=disclosure_selected_before,
                    disclosure_selected_after=disclosure_selected_after,
                    screenshot_path=screenshot_path,
                    post_action_controls_count=post_controls_count,
                    action_attempted=True,
                    action_confirmed=True,
                    dom_evidence=dom_evidence_dict,
                )
                return executed_res

        except StateReconstructionError as e:
            if lifecycle_tracker is not None:
                lifecycle_tracker["phase"] = "CLEANUP"
                lifecycle_tracker["cleanup_failed"] = True
                lifecycle_tracker["cleanup_completed"] = False
            # CONTRACT CHECK 3: Teardown failure must remain explicit and must NOT erase an already confirmed click.
            confirmed = bool(
                action_confirmed
                or (lifecycle_tracker and lifecycle_tracker.get("action_confirmed"))
                or getattr(locator, "_action_confirmed", False)
                or (page and getattr(page, "_action_confirmed", False))
            )
            attempted = bool(action_attempted or (lifecycle_tracker and lifecycle_tracker.get("action_attempted")))
            if confirmed:
                return GuardedExecutionResult(
                    status=GuardedExecutionStatus.CLEANUP_FAILED,
                    action_executed=True,
                    action_type="click",
                    target_control_id=target_control_id,
                    pre_action_state_fingerprint=pre_state_fingerprint,
                    post_action_state_fingerprint=post_state_fingerprint,
                    reconstruction_result=reconstruction_res,
                    limitations=all_limitations,
                    error_message=f"Terminal teardown failure after action execution; verified context-owner disposal required: {type(e).__name__}",
                    disclosure_expanded_before=disclosure_expanded_before,
                    disclosure_expanded_after=disclosure_expanded_after,
                    post_action_controls_count=post_controls_count,
                    action_attempted=True,
                    action_confirmed=True,
                )
            elif action_attempted:
                return GuardedExecutionResult(
                    status=GuardedExecutionStatus.CLEANUP_FAILED,
                    action_executed=True,
                    action_type="click",
                    target_control_id=target_control_id,
                    pre_action_state_fingerprint=pre_state_fingerprint,
                    post_action_state_fingerprint=None,
                    reconstruction_result=reconstruction_res,
                    limitations=all_limitations,
                    error_message=f"Terminal teardown failure after action attempt: {type(e).__name__}",
                    action_attempted=True,
                    action_confirmed=False,
                )
            else:
                return GuardedExecutionResult(
                    status=GuardedExecutionStatus.RECONSTRUCTION_FAILED,
                    action_executed=False,
                    action_type=candidate_intent.expected_action_type,
                    target_control_id=candidate_intent.control_fingerprint,
                    pre_action_state_fingerprint=candidate_intent.parent_state_fingerprint,
                    post_action_state_fingerprint=None,
                    reconstruction_result=None,
                    limitations=(),
                    error_message=f"State reconstruction error before action execution: {type(e).__name__}",
                    action_attempted=False,
                    action_confirmed=False,
                )
