"""Phase B1: Safe Execution Preflight Pipeline.

Revalidates a CandidateActionIntent against current QA-owned browser state
strictly without executing any UI action or mutating the browser.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional, Tuple, Any

from app.qa.browser_snapshot_adapter import (
    ReadOnlyBrowserSnapshotAdapter,
    CancellationToken,
)
from app.qa.browser_snapshot_model import BrowserSnapshot
from app.qa.semantic_normalizer import (
    SemanticNormalizer,
    SemanticNormalizationResult,
)
from app.qa.candidate_intent_factory import (
    CandidateActionIntentFactory,
    CandidateIntentDerivationResult,
)
from app.qa.candidate_intent_graph import CandidateActionIntent
from app.qa.state_fingerprint import CanonicalStateFingerprint
from app.qa.interaction_policy import RiskClassification


class PreflightStatus:
    REVALIDATED = "REVALIDATED"
    REVALIDATED_WITH_LIMITATIONS = "REVALIDATED_WITH_LIMITATIONS"
    STALE_STATE = "STALE_STATE"
    INTENT_NOT_PRESENT = "INTENT_NOT_PRESENT"
    TARGET_NOT_ELIGIBLE = "TARGET_NOT_ELIGIBLE"
    CANCELLED = "CANCELLED"
    TARGET_CLOSED = "TARGET_CLOSED"
    WRONG_CONTEXT = "WRONG_CONTEXT"
    MANUAL_PAGE_EXCLUDED = "MANUAL_PAGE_EXCLUDED"
    OPAQUE_ORIGIN = "OPAQUE_ORIGIN"
    FRAME_DETACHED = "FRAME_DETACHED"
    NAVIGATION_RACE = "NAVIGATION_RACE"


@dataclass(frozen=True)
class ExecutionPreflightResult:
    status: str
    intent_revalidated: bool
    current_parent_state_fingerprint: Optional[str]
    risk_classification: Optional[RiskClassification]
    limitations: Tuple[str, ...]
    candidate_intent: Optional[CandidateActionIntent] = None


class SafeExecutionPreflight:
    """Preflight pipeline that revalidates candidate intents without action execution."""

    def __init__(self) -> None:
        self._snapshot_adapter = ReadOnlyBrowserSnapshotAdapter()
        self._normalizer = SemanticNormalizer()
        self._intent_factory = CandidateActionIntentFactory()

    async def preflight(
        self,
        intent: CandidateActionIntent,
        page: Any,
        eligibility_contract: Any,
        *,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> ExecutionPreflightResult:
        """Perform safe, read-only execution preflight.

        Answers strictly: Is this exact candidate intent still valid and eligible
        to be considered for later execution on this exact QA-owned background state?
        """
        # Type validation
        if not isinstance(intent, CandidateActionIntent):
            raise TypeError(
                f"SafeExecutionPreflight requires a CandidateActionIntent; "
                f"received {type(intent)!r}."
            )

        # Target state requirement: must be UNRESOLVED
        if intent.target_state != "UNRESOLVED":
            return ExecutionPreflightResult(
                status=PreflightStatus.INTENT_NOT_PRESENT,
                intent_revalidated=False,
                current_parent_state_fingerprint=None,
                risk_classification=None,
                limitations=(),
                candidate_intent=None,
            )

        # Early cancellation check
        if cancellation_token and cancellation_token.is_cancelled():
            return ExecutionPreflightResult(
                status=PreflightStatus.CANCELLED,
                intent_revalidated=False,
                current_parent_state_fingerprint=None,
                risk_classification=None,
                limitations=(),
                candidate_intent=None,
            )

        # Fail closed on missing, invalid, or incomplete eligibility contract
        if not isinstance(eligibility_contract, Mapping):
            return ExecutionPreflightResult(
                status=PreflightStatus.TARGET_NOT_ELIGIBLE,
                intent_revalidated=False,
                current_parent_state_fingerprint=None,
                risk_classification=None,
                limitations=(),
                candidate_intent=None,
            )

        if "qa_owned" not in eligibility_contract or eligibility_contract["qa_owned"] is not True:
            return ExecutionPreflightResult(
                status=PreflightStatus.TARGET_NOT_ELIGIBLE,
                intent_revalidated=False,
                current_parent_state_fingerprint=None,
                risk_classification=None,
                limitations=(),
                candidate_intent=None,
            )

        if (
            "manual_visible_page" not in eligibility_contract
            or eligibility_contract["manual_visible_page"] is not False
        ):
            return ExecutionPreflightResult(
                status=PreflightStatus.MANUAL_PAGE_EXCLUDED,
                intent_revalidated=False,
                current_parent_state_fingerprint=None,
                risk_classification=None,
                limitations=(),
                candidate_intent=None,
            )

        if (
            "expected_context" not in eligibility_contract
            or eligibility_contract["expected_context"] is None
        ):
            return ExecutionPreflightResult(
                status=PreflightStatus.WRONG_CONTEXT,
                intent_revalidated=False,
                current_parent_state_fingerprint=None,
                risk_classification=None,
                limitations=(),
                candidate_intent=None,
            )

        expected_context = eligibility_contract["expected_context"]

        # Fail closed on missing page
        if page is None:
            return ExecutionPreflightResult(
                status=PreflightStatus.TARGET_NOT_ELIGIBLE,
                intent_revalidated=False,
                current_parent_state_fingerprint=None,
                risk_classification=None,
                limitations=(),
                candidate_intent=None,
            )

        # Verify page context matches expected context
        try:
            page_context = getattr(page, "context", None)
            if page_context != expected_context:
                return ExecutionPreflightResult(
                    status=PreflightStatus.WRONG_CONTEXT,
                    intent_revalidated=False,
                    current_parent_state_fingerprint=None,
                    risk_classification=None,
                    limitations=(),
                    candidate_intent=None,
                )
        except Exception:
            return ExecutionPreflightResult(
                status=PreflightStatus.WRONG_CONTEXT,
                intent_revalidated=False,
                current_parent_state_fingerprint=None,
                risk_classification=None,
                limitations=(),
                candidate_intent=None,
            )

        # Check if page is closed
        try:
            is_closed_attr = getattr(page, "is_closed", None)
            if callable(is_closed_attr):
                if is_closed_attr():
                    return ExecutionPreflightResult(
                        status=PreflightStatus.TARGET_CLOSED,
                        intent_revalidated=False,
                        current_parent_state_fingerprint=None,
                        risk_classification=None,
                        limitations=(),
                        candidate_intent=None,
                    )
        except Exception:
            return ExecutionPreflightResult(
                status=PreflightStatus.TARGET_CLOSED,
                intent_revalidated=False,
                current_parent_state_fingerprint=None,
                risk_classification=None,
                limitations=(),
                candidate_intent=None,
            )

        # Forward truthful validated contract values directly to A2 snapshot adapter
        forwarded_contract = {
            "qa_owned": eligibility_contract["qa_owned"],
            "manual_visible_page": eligibility_contract["manual_visible_page"],
            "expected_context": eligibility_contract["expected_context"],
        }

        # Fresh A2 snapshot capture (purely read-only)
        try:
            snapshot: BrowserSnapshot = await self._snapshot_adapter.capture_snapshot(
                page=page,
                eligibility_contract=forwarded_contract,
                cancellation_token=cancellation_token,
            )
        except Exception:
            return ExecutionPreflightResult(
                status=PreflightStatus.TARGET_CLOSED,
                intent_revalidated=False,
                current_parent_state_fingerprint=None,
                risk_classification=None,
                limitations=(),
                candidate_intent=None,
            )

        # Handle terminal conditions from A2
        terminal_status = snapshot.coverage.terminal_status
        if terminal_status != "COMPLETE":
            status_out = terminal_status
            if status_out == "NOT_ELIGIBLE":
                status_out = PreflightStatus.TARGET_NOT_ELIGIBLE
            return ExecutionPreflightResult(
                status=status_out,
                intent_revalidated=False,
                current_parent_state_fingerprint=None,
                risk_classification=None,
                limitations=snapshot.coverage.limitations,
                candidate_intent=None,
            )

        # Fresh A3 semantic normalization
        norm_result: SemanticNormalizationResult = self._normalizer.normalize_snapshot(snapshot)
        if norm_result.terminal_status != "COMPLETE" or norm_result.semantic_state is None:
            status_out = norm_result.terminal_status
            if status_out == "NOT_ELIGIBLE":
                status_out = PreflightStatus.TARGET_NOT_ELIGIBLE
            return ExecutionPreflightResult(
                status=status_out,
                intent_revalidated=False,
                current_parent_state_fingerprint=None,
                risk_classification=None,
                limitations=norm_result.limitations,
                candidate_intent=None,
            )

        # Compute current state fingerprint and validate against intent's parent fingerprint
        current_fingerprint = CanonicalStateFingerprint.generate(norm_result.semantic_state)
        if current_fingerprint != intent.parent_state_fingerprint:
            return ExecutionPreflightResult(
                status=PreflightStatus.STALE_STATE,
                intent_revalidated=False,
                current_parent_state_fingerprint=current_fingerprint,
                risk_classification=None,
                limitations=norm_result.limitations,
                candidate_intent=None,
            )

        # Fresh A4 candidate intent derivation from current semantic state
        derivation_result: CandidateIntentDerivationResult = self._intent_factory.derive(norm_result)
        if derivation_result.terminal_status != "COMPLETE":
            status_out = derivation_result.terminal_status
            if status_out == "NOT_ELIGIBLE":
                status_out = PreflightStatus.TARGET_NOT_ELIGIBLE
            return ExecutionPreflightResult(
                status=status_out,
                intent_revalidated=False,
                current_parent_state_fingerprint=current_fingerprint,
                risk_classification=None,
                limitations=derivation_result.limitations,
                candidate_intent=None,
            )

        # Match exact incoming candidate intent in freshly derived intents
        # Exact equality verifies parent_state_fingerprint, control_fingerprint,
        # expected_action_type, risk_classification, and target_state ("UNRESOLVED")
        matching_intent: Optional[CandidateActionIntent] = None
        for cand in derivation_result.intents:
            if (
                cand == intent
                or (
                    cand.parent_state_fingerprint == intent.parent_state_fingerprint
                    and cand.control_fingerprint == intent.control_fingerprint
                    and cand.expected_action_type == intent.expected_action_type
                    and cand.risk_classification == intent.risk_classification
                )
            ):
                matching_intent = intent
                break

        if matching_intent is None:
            return ExecutionPreflightResult(
                status=PreflightStatus.INTENT_NOT_PRESENT,
                intent_revalidated=False,
                current_parent_state_fingerprint=current_fingerprint,
                risk_classification=None,
                limitations=derivation_result.limitations,
                candidate_intent=None,
            )

        # Determine limitations and final status
        all_limitations = tuple(sorted(list(set(norm_result.limitations + derivation_result.limitations))))
        if all_limitations:
            final_status = PreflightStatus.REVALIDATED_WITH_LIMITATIONS
        else:
            final_status = PreflightStatus.REVALIDATED

        return ExecutionPreflightResult(
            status=final_status,
            intent_revalidated=True,
            current_parent_state_fingerprint=current_fingerprint,
            risk_classification=matching_intent.risk_classification,
            limitations=all_limitations,
            candidate_intent=matching_intent,
        )


# Alias for convenience and backward/interface flexibility
ExecutionPreflight = SafeExecutionPreflight
