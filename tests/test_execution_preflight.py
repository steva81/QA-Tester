"""Tests for Phase B1 Safe Execution Preflight Pipeline.

Validates that ExecutionPreflight safely and deterministically revalidates
CandidateActionIntent instances against current QA-owned browser state
without invoking any UI action or mutating the browser.
"""

import asyncio
from dataclasses import FrozenInstanceError
import http.server
import threading
from typing import Any, List
import pytest
from playwright.async_api import async_playwright

from app.qa.browser_snapshot_adapter import (
    ReadOnlyBrowserSnapshotAdapter,
    CancellationToken,
)
from app.qa.candidate_intent_graph import CandidateActionIntent
from app.qa.candidate_intent_factory import CandidateActionIntentFactory
from app.qa.semantic_normalizer import SemanticNormalizer
from app.qa.state_fingerprint import CanonicalStateFingerprint
from app.qa.interaction_policy import RiskClassification
from app.qa.execution_preflight import (
    SafeExecutionPreflight,
    ExecutionPreflight,
    ExecutionPreflightResult,
    PreflightStatus,
)


# ==============================================================================
# Test Doubles & Behavioral Spies
# ==============================================================================

class SpyException(Exception):
    """Raised when a forbidden UI action or mutation method is invoked."""
    pass


FORBIDDEN_ACTION_METHODS = [
    "click", "dblclick", "tap", "hover", "focus", "blur", "fill",
    "type", "press", "press_sequentially", "select_option", "check",
    "uncheck", "set_checked", "set_input_files", "drag_to",
    "dispatch_event", "goto", "reload", "go_back", "go_forward"
]


class MockFrameDouble:
    def __init__(
        self,
        url: str = "http://test.local:8080/app",
        origin: str = "http://test.local:8080",
        name: str = "main",
        is_detached: bool = False,
        controls: List[dict] = None,
        status: str = "COMPLETE",
    ):
        self.url = url
        self.name = name
        self._origin = origin
        self._is_detached = is_detached
        self.child_frames = []
        self._evaluate_data = {
            "controls": controls if controls is not None else [],
            "status": status,
            "origin": origin,
        }
        self.evaluate_called = False
        self.eval_exc = None

    def is_detached(self) -> bool:
        return self._is_detached

    async def evaluate(self, script: str, *args: Any) -> Any:
        self.evaluate_called = True
        if self.eval_exc:
            raise self.eval_exc
        return self._evaluate_data

    def __getattr__(self, name: str) -> Any:
        if name in FORBIDDEN_ACTION_METHODS:
            def spy_method(*args: Any, **kwargs: Any) -> Any:
                raise SpyException(f"Forbidden method '{name}' was called on MockFrameDouble!")
            return spy_method
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")


class MockPageDouble:
    def __init__(
        self,
        url: str = "http://test.local:8080/app",
        context: Any = "mock_ctx",
        is_closed: bool = False,
        controls: List[dict] = None,
        status: str = "COMPLETE",
    ):
        self.url = url
        self.context = context
        self._is_closed = is_closed
        self.main_frame = MockFrameDouble(
            url=url,
            origin="http://test.local:8080",
            controls=controls,
            status=status,
        )

    def is_closed(self) -> bool:
        return self._is_closed

    def __getattr__(self, name: str) -> Any:
        if name in FORBIDDEN_ACTION_METHODS:
            def spy_method(*args: Any, **kwargs: Any) -> Any:
                raise SpyException(f"Forbidden method '{name}' was called on MockPageDouble!")
            return spy_method
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")


# ==============================================================================
# Helpers
# ==============================================================================

async def derive_intent_from_page(page: Any, expected_context: Any) -> CandidateActionIntent:
    adapter = ReadOnlyBrowserSnapshotAdapter()
    snap = await adapter.capture_snapshot(
        page,
        {
            "qa_owned": True,
            "manual_visible_page": False,
            "expected_context": expected_context,
        },
    )
    norm = SemanticNormalizer().normalize_snapshot(snap)
    res = CandidateActionIntentFactory.derive(norm)
    assert len(res.intents) > 0, "No intents derived for test setup!"
    return res.intents[0]


def standard_contract(context: Any = "mock_ctx") -> dict:
    return {
        "qa_owned": True,
        "manual_visible_page": False,
        "expected_context": context,
    }


# ==============================================================================
# Constructor Non-Overridability Tests
# ==============================================================================

def test_constructor_disallows_public_dependency_overrides():
    """SafeExecutionPreflight constructor accepts 0 arguments; overrides are forbidden."""
    with pytest.raises(TypeError):
        SafeExecutionPreflight(snapshot_adapter="custom")

    with pytest.raises(TypeError):
        SafeExecutionPreflight(normalizer="custom")

    with pytest.raises(TypeError):
        SafeExecutionPreflight(intent_factory="custom")


# ==============================================================================
# Explicit Eligibility Contract & Manual Protection Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_missing_eligibility_contract_fails_closed():
    """Missing or non-mapping eligibility contract fails closed as TARGET_NOT_ELIGIBLE."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")
    preflight = SafeExecutionPreflight()

    # None contract
    res1 = await preflight.preflight(intent, page, None)
    assert res1.status == PreflightStatus.TARGET_NOT_ELIGIBLE
    assert res1.intent_revalidated is False

    # String contract
    res2 = await preflight.preflight(intent, page, "invalid_contract")
    assert res2.status == PreflightStatus.TARGET_NOT_ELIGIBLE
    assert res2.intent_revalidated is False


@pytest.mark.asyncio
async def test_missing_or_non_true_qa_owned_fails_closed():
    """Missing or non-True qa_owned fails closed as TARGET_NOT_ELIGIBLE."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")
    preflight = SafeExecutionPreflight()

    # Missing qa_owned
    res1 = await preflight.preflight(
        intent, page, {"manual_visible_page": False, "expected_context": "mock_ctx"}
    )
    assert res1.status == PreflightStatus.TARGET_NOT_ELIGIBLE
    assert res1.intent_revalidated is False

    # qa_owned=False
    res2 = await preflight.preflight(
        intent, page, {"qa_owned": False, "manual_visible_page": False, "expected_context": "mock_ctx"}
    )
    assert res2.status == PreflightStatus.TARGET_NOT_ELIGIBLE
    assert res2.intent_revalidated is False

    # qa_owned truthy string instead of True
    res3 = await preflight.preflight(
        intent, page, {"qa_owned": "true", "manual_visible_page": False, "expected_context": "mock_ctx"}
    )
    assert res3.status == PreflightStatus.TARGET_NOT_ELIGIBLE
    assert res3.intent_revalidated is False


@pytest.mark.asyncio
async def test_missing_or_true_manual_visible_page_fails_closed():
    """Missing or True manual_visible_page fails closed as MANUAL_PAGE_EXCLUDED."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")
    preflight = SafeExecutionPreflight()

    # Missing manual_visible_page
    res1 = await preflight.preflight(
        intent, page, {"qa_owned": True, "expected_context": "mock_ctx"}
    )
    assert res1.status == PreflightStatus.MANUAL_PAGE_EXCLUDED
    assert res1.intent_revalidated is False

    # manual_visible_page=True
    res2 = await preflight.preflight(
        intent, page, {"qa_owned": True, "manual_visible_page": True, "expected_context": "mock_ctx"}
    )
    assert res2.status == PreflightStatus.MANUAL_PAGE_EXCLUDED
    assert res2.intent_revalidated is False


@pytest.mark.asyncio
async def test_missing_or_none_expected_context_fails_closed():
    """Missing or None expected_context fails closed as WRONG_CONTEXT."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")
    preflight = SafeExecutionPreflight()

    # Missing expected_context
    res1 = await preflight.preflight(
        intent, page, {"qa_owned": True, "manual_visible_page": False}
    )
    assert res1.status == PreflightStatus.WRONG_CONTEXT
    assert res1.intent_revalidated is False

    # expected_context=None
    res2 = await preflight.preflight(
        intent, page, {"qa_owned": True, "manual_visible_page": False, "expected_context": None}
    )
    assert res2.status == PreflightStatus.WRONG_CONTEXT
    assert res2.intent_revalidated is False


@pytest.mark.asyncio
async def test_manual_page_never_reaches_a2_evaluation():
    """Calling preflight without explicit manual-visible proof can never reach A2 evaluate."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")
    page.main_frame.evaluate_called = False

    preflight = SafeExecutionPreflight()
    # Contract missing manual_visible_page
    result = await preflight.preflight(
        intent, page, {"qa_owned": True, "expected_context": "mock_ctx"}
    )

    assert result.status == PreflightStatus.MANUAL_PAGE_EXCLUDED
    assert page.main_frame.evaluate_called is False, "A2 evaluate was reached despite missing manual proof!"


# ==============================================================================
# Pipeline & Revalidation Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_matching_current_fingerprint_revalidated():
    """Matching current fingerprint and intent produces REVALIDATED."""
    controls = [
        {
            "control_id": "btn1",
            "raw_role": "button",
            "raw_name": "Submit",
            "raw_visible": True,
            "raw_enabled": True,
        }
    ]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.REVALIDATED
    assert result.intent_revalidated is True
    assert result.current_parent_state_fingerprint == intent.parent_state_fingerprint
    assert result.risk_classification == intent.risk_classification
    assert result.limitations == ()
    assert result.candidate_intent == intent


@pytest.mark.asyncio
async def test_changed_current_state_stale_state():
    """Changed DOM state produces STALE_STATE with fresh fingerprint."""
    initial_controls = [
        {
            "control_id": "btn1",
            "raw_role": "button",
            "raw_name": "Submit",
            "raw_visible": True,
            "raw_enabled": True,
        }
    ]
    page = MockPageDouble(controls=initial_controls)
    old_intent = await derive_intent_from_page(page, "mock_ctx")

    # State changes: new button added
    page.main_frame._evaluate_data["controls"] = [
        {
            "control_id": "btn1",
            "raw_role": "button",
            "raw_name": "Submit",
            "raw_visible": True,
            "raw_enabled": True,
        },
        {
            "control_id": "btn2",
            "raw_role": "button",
            "raw_name": "Cancel",
            "raw_visible": True,
            "raw_enabled": True,
        },
    ]

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=old_intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.STALE_STATE
    assert result.intent_revalidated is False
    assert result.current_parent_state_fingerprint is not None
    assert result.current_parent_state_fingerprint != old_intent.parent_state_fingerprint
    assert result.candidate_intent is None


@pytest.mark.asyncio
async def test_intent_no_longer_derivable_intent_not_present():
    """Crafted intent with nonexistent control or wrong action type fails as INTENT_NOT_PRESENT."""
    controls = [
        {
            "control_id": "btn1",
            "raw_role": "button",
            "raw_name": "Click Me",
            "raw_visible": True,
            "raw_enabled": True,
        }
    ]
    page = MockPageDouble(controls=controls)
    valid_intent = await derive_intent_from_page(page, "mock_ctx")

    # Construct invalid intent with mismatched control_fingerprint
    invalid_intent = CandidateActionIntent(
        parent_state_fingerprint=valid_intent.parent_state_fingerprint,
        control_fingerprint="0:nonexistent",
        expected_action_type="click",
        risk_classification=RiskClassification.SAFE_CANDIDATE,
    )

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=invalid_intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.INTENT_NOT_PRESENT
    assert result.intent_revalidated is False
    assert result.candidate_intent is None


@pytest.mark.asyncio
async def test_action_type_mismatch_intent_not_present():
    """Intent with wrong action type fails as INTENT_NOT_PRESENT."""
    controls = [
        {
            "control_id": "btn1",
            "raw_role": "button",
            "raw_name": "Click Me",
            "raw_visible": True,
            "raw_enabled": True,
        }
    ]
    page = MockPageDouble(controls=controls)
    valid_intent = await derive_intent_from_page(page, "mock_ctx")

    # Mismatched action type ("select" instead of "click")
    mismatched_intent = CandidateActionIntent(
        parent_state_fingerprint=valid_intent.parent_state_fingerprint,
        control_fingerprint=valid_intent.control_fingerprint,
        expected_action_type="select",
        risk_classification=RiskClassification.SAFE_CANDIDATE,
    )

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=mismatched_intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.INTENT_NOT_PRESENT
    assert result.intent_revalidated is False


@pytest.mark.asyncio
async def test_resolved_target_state_rejected():
    """Intent with target_state != UNRESOLVED is rejected."""
    controls = [
        {
            "control_id": "btn1",
            "raw_role": "button",
            "raw_name": "Click Me",
            "raw_visible": True,
            "raw_enabled": True,
        }
    ]
    page = MockPageDouble(controls=controls)
    valid_intent = await derive_intent_from_page(page, "mock_ctx")

    # Force target_state to RESOLVED
    resolved_intent = CandidateActionIntent(
        parent_state_fingerprint=valid_intent.parent_state_fingerprint,
        control_fingerprint=valid_intent.control_fingerprint,
        expected_action_type=valid_intent.expected_action_type,
        risk_classification=valid_intent.risk_classification,
    )
    object.__setattr__(resolved_intent, "target_state", "RESOLVED")

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=resolved_intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.INTENT_NOT_PRESENT
    assert result.intent_revalidated is False


@pytest.mark.asyncio
async def test_current_policy_becomes_prohibited_rejected():
    """If control matches PROHIBITED signature, A4 does not derive it and intent is rejected."""
    controls = [
        {
            "control_id": "del_btn",
            "raw_role": "button",
            "raw_name": "Delete Account",
            "raw_visible": True,
            "raw_enabled": True,
        }
    ]
    page = MockPageDouble(controls=controls)
    adapter = ReadOnlyBrowserSnapshotAdapter()
    snap = await adapter.capture_snapshot(
        page,
        {
            "qa_owned": True,
            "manual_visible_page": False,
            "expected_context": "mock_ctx",
        },
    )
    norm = SemanticNormalizer().normalize_snapshot(snap)
    fp = CanonicalStateFingerprint.generate(norm.semantic_state)

    prohibited_intent = CandidateActionIntent(
        parent_state_fingerprint=fp,
        control_fingerprint="0:del_btn",
        expected_action_type="click",
        risk_classification=RiskClassification.PROHIBITED,
    )

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=prohibited_intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.INTENT_NOT_PRESENT
    assert result.intent_revalidated is False


@pytest.mark.asyncio
async def test_current_policy_becomes_unknown_rejected():
    """If control has unknown role, A4 does not derive it and intent is rejected."""
    controls = [
        {
            "control_id": "custom1",
            "raw_role": "unknown_widget",
            "raw_name": "Widget",
            "raw_visible": True,
            "raw_enabled": True,
        }
    ]
    page = MockPageDouble(controls=controls)
    adapter = ReadOnlyBrowserSnapshotAdapter()
    snap = await adapter.capture_snapshot(
        page,
        {
            "qa_owned": True,
            "manual_visible_page": False,
            "expected_context": "mock_ctx",
        },
    )
    norm = SemanticNormalizer().normalize_snapshot(snap)
    fp = CanonicalStateFingerprint.generate(norm.semantic_state)

    unknown_intent = CandidateActionIntent(
        parent_state_fingerprint=fp,
        control_fingerprint="0:custom1",
        expected_action_type="click",
        risk_classification=RiskClassification.UNKNOWN,
    )

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=unknown_intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.INTENT_NOT_PRESENT
    assert result.intent_revalidated is False


@pytest.mark.asyncio
async def test_safe_candidate_remains_truthful():
    """SAFE_CANDIDATE is preserved truthfully without execution authorization flags."""
    controls = [
        {
            "control_id": "nav_link",
            "raw_role": "a",
            "raw_name": "Documentation",
            "href": "http://test.local:8080/docs",
            "raw_visible": True,
            "raw_enabled": True,
        }
    ]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")
    assert intent.risk_classification == RiskClassification.SAFE_CANDIDATE

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.REVALIDATED
    assert result.intent_revalidated is True
    assert result.risk_classification == RiskClassification.SAFE_CANDIDATE
    # Verify absence of execution authorization flags
    assert not hasattr(result, "execute")
    assert not hasattr(result, "authorized")
    assert not hasattr(result, "safe_to_click")


@pytest.mark.asyncio
async def test_potentially_mutating_truthful_not_authorized():
    """POTENTIALLY_MUTATING intent is revalidated truthfully but strictly NOT authorized."""
    controls = [
        {
            "control_id": "save_btn",
            "raw_role": "button",
            "raw_name": "Submit",
            "form_ownership": "form1",
            "raw_visible": True,
            "raw_enabled": True,
        }
    ]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")
    assert intent.risk_classification == RiskClassification.POTENTIALLY_MUTATING

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.REVALIDATED
    assert result.intent_revalidated is True
    assert result.risk_classification == RiskClassification.POTENTIALLY_MUTATING
    # B1 MUST NOT produce execution authorization fields
    assert not hasattr(result, "execute")
    assert not hasattr(result, "authorized")
    assert not hasattr(result, "safe_to_click")


@pytest.mark.asyncio
async def test_terminal_cancelled_preflight():
    """Pre-cancelled or during-cancelled token produces CANCELLED."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")

    # 1. Pre-cancelled token
    token = CancellationToken(cancelled=True)
    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
        cancellation_token=token,
    )

    assert result.status == PreflightStatus.CANCELLED
    assert result.intent_revalidated is False
    assert result.candidate_intent is None

    # 2. Cancelled during evaluation
    token2 = CancellationToken()
    async def cancel_eval(script, *args):
        token2.cancel()
        return {"controls": controls, "status": "COMPLETE", "origin": "http://test.local:8080"}

    page.main_frame.evaluate = cancel_eval
    result2 = await preflight.preflight(
        intent=intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
        cancellation_token=token2,
    )
    assert result2.status == PreflightStatus.CANCELLED
    assert result2.intent_revalidated is False


@pytest.mark.asyncio
async def test_terminal_target_closed():
    """Closed page produces TARGET_CLOSED."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")

    # Mark page closed
    page._is_closed = True

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.TARGET_CLOSED
    assert result.intent_revalidated is False


@pytest.mark.asyncio
async def test_wrong_context_rejected():
    """Mismatch in BrowserContext produces WRONG_CONTEXT."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls, context="real_context")
    intent = await derive_intent_from_page(page, "real_context")

    preflight = SafeExecutionPreflight()

    # Mismatched expected_context
    result = await preflight.preflight(
        intent=intent,
        page=page,
        eligibility_contract=standard_contract("different_context"),
    )
    assert result.status == PreflightStatus.WRONG_CONTEXT
    assert result.intent_revalidated is False


@pytest.mark.asyncio
async def test_opaque_origin_rejected():
    """Opaque origin URL produces OPAQUE_ORIGIN."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls, url="data:text/html,<button>Btn</button>")
    intent = CandidateActionIntent(
        parent_state_fingerprint="dummy",
        control_fingerprint="0:btn1",
        expected_action_type="click",
        risk_classification=RiskClassification.SAFE_CANDIDATE,
    )

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.OPAQUE_ORIGIN
    assert result.intent_revalidated is False


@pytest.mark.asyncio
async def test_frame_detached_rejected():
    """Detached frame produces FRAME_DETACHED."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")

    page.main_frame._is_detached = True

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.FRAME_DETACHED
    assert result.intent_revalidated is False


@pytest.mark.asyncio
async def test_navigation_race_rejected():
    """Navigation race during evaluate produces NAVIGATION_RACE."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")

    page.main_frame.eval_exc = RuntimeError("Execution context was destroyed, most likely because of a navigation.")

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.NAVIGATION_RACE
    assert result.intent_revalidated is False


@pytest.mark.asyncio
async def test_limited_control_budget_preserved():
    """LIMITED_CONTROL_BUDGET in snapshot coverage produces REVALIDATED_WITH_LIMITATIONS."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls, status="LIMITED_CONTROL_BUDGET")
    intent = await derive_intent_from_page(page, "mock_ctx")

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.REVALIDATED_WITH_LIMITATIONS
    assert result.intent_revalidated is True
    assert "LIMITED_CONTROL_BUDGET" in result.limitations
    assert result.candidate_intent == intent


@pytest.mark.asyncio
async def test_limited_frame_budget_preserved():
    """Exceeded frame budget produces REVALIDATED_WITH_LIMITATIONS."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls)
    # Add child frames exceeding adapter budget
    for i in range(12):
        page.main_frame.child_frames.append(MockFrameDouble(url="http://test.local:8080/child", name=f"child_{i}"))

    intent = await derive_intent_from_page(page, "mock_ctx")

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.REVALIDATED_WITH_LIMITATIONS
    assert result.intent_revalidated is True
    assert "LIMITED_FRAME_BUDGET" in result.limitations


@pytest.mark.asyncio
async def test_cross_origin_limited_preserved():
    """Cross-origin child frame produces REVALIDATED_WITH_LIMITATIONS with CROSS_ORIGIN_LIMITED."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls)
    page.main_frame.child_frames.append(
        MockFrameDouble(url="http://other.local:9090/frame", origin="http://other.local:9090", name="cross")
    )

    intent = await derive_intent_from_page(page, "mock_ctx")

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.REVALIDATED_WITH_LIMITATIONS
    assert result.intent_revalidated is True
    assert "CROSS_ORIGIN_LIMITED" in result.limitations


@pytest.mark.asyncio
async def test_multiple_limitations_preserved():
    """Multiple limitations are aggregated and sorted in limitations tuple."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls, status="LIMITED_CONTROL_BUDGET")
    page.main_frame.child_frames.append(
        MockFrameDouble(url="http://other.local:9090/frame", origin="http://other.local:9090", name="cross")
    )

    intent = await derive_intent_from_page(page, "mock_ctx")

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    assert result.status == PreflightStatus.REVALIDATED_WITH_LIMITATIONS
    assert result.intent_revalidated is True
    assert result.limitations == ("CROSS_ORIGIN_LIMITED", "LIMITED_CONTROL_BUDGET")


@pytest.mark.asyncio
async def test_private_sentinel_absent():
    """Private input sentinel is never leaked into ExecutionPreflightResult representations."""
    sentinel = "PRIVATE-PHASE-B1-SENTINEL"
    controls = [
        {
            "control_id": "pwd1",
            "raw_role": "input password",
            "raw_name": "***",
            "is_private": True,
            "raw_visible": True,
            "raw_enabled": True,
        },
        {
            "control_id": "btn1",
            "raw_role": "button",
            "raw_name": "Submit",
            "raw_visible": True,
            "raw_enabled": True,
        },
    ]
    page = MockPageDouble(controls=controls)
    # Inject sentinel in raw private node
    page.main_frame._evaluate_data["controls"][0]["raw_value"] = sentinel

    intent = await derive_intent_from_page(page, "mock_ctx")

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(
        intent=intent,
        page=page,
        eligibility_contract=standard_contract("mock_ctx"),
    )

    # Prove sentinel is absent from every representation
    assert sentinel not in repr(result)
    assert sentinel not in str(result)
    assert sentinel not in (result.current_parent_state_fingerprint or "")
    if result.candidate_intent:
        assert sentinel not in repr(result.candidate_intent)
        assert sentinel not in str(result.candidate_intent)


@pytest.mark.asyncio
async def test_repeated_identical_input_deterministic():
    """Identical inputs produce strictly identical ExecutionPreflightResult."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")

    preflight = SafeExecutionPreflight()
    res1 = await preflight.preflight(intent=intent, page=page, eligibility_contract=standard_contract("mock_ctx"))
    res2 = await preflight.preflight(intent=intent, page=page, eligibility_contract=standard_contract("mock_ctx"))

    assert res1 == res2
    assert res1.status == res2.status
    assert res1.current_parent_state_fingerprint == res2.current_parent_state_fingerprint
    assert res1.risk_classification == res2.risk_classification
    assert res1.limitations == res2.limitations
    assert res1.candidate_intent == res2.candidate_intent


@pytest.mark.asyncio
async def test_result_immutable():
    """ExecutionPreflightResult is frozen and cannot be mutated."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(intent=intent, page=page, eligibility_contract=standard_contract("mock_ctx"))

    with pytest.raises(FrozenInstanceError):
        result.status = "MUTATED"

    with pytest.raises(FrozenInstanceError):
        result.intent_revalidated = False


@pytest.mark.asyncio
async def test_no_browser_runtime_object_retained():
    """ExecutionPreflightResult does not retain live Page, Frame, Context, or DOM objects."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")

    preflight = SafeExecutionPreflight()
    result = await preflight.preflight(intent=intent, page=page, eligibility_contract=standard_contract("mock_ctx"))

    for field_name in ["status", "intent_revalidated", "current_parent_state_fingerprint", "risk_classification", "limitations", "candidate_intent"]:
        val = getattr(result, field_name)
        assert not isinstance(val, (MockPageDouble, MockFrameDouble)), f"Field {field_name} retained browser object!"


@pytest.mark.asyncio
async def test_zero_action_behavioral_spy():
    """Spy confirms zero forbidden UI action methods are called during preflight."""
    controls = [{"control_id": "btn1", "raw_role": "button", "raw_name": "Btn", "raw_visible": True, "raw_enabled": True}]
    page = MockPageDouble(controls=controls)
    intent = await derive_intent_from_page(page, "mock_ctx")

    preflight = SafeExecutionPreflight()
    # Runs successfully without triggering spy
    result = await preflight.preflight(intent=intent, page=page, eligibility_contract=standard_contract("mock_ctx"))
    assert result.status == PreflightStatus.REVALIDATED

    # Verify that the spy actually intercepts forbidden actions
    with pytest.raises(SpyException, match="Forbidden method 'click' was called"):
        page.click()

    with pytest.raises(SpyException, match="Forbidden method 'fill' was called"):
        page.main_frame.fill()

    with pytest.raises(SpyException, match="Forbidden method 'goto' was called"):
        page.goto()


# ==============================================================================
# Real Chromium Integration Test
# ==============================================================================

REAL_HTML_INITIAL = """<!DOCTYPE html>
<html>
<head><title>B1 Preflight Test</title></head>
<body>
  <button id="target-btn">Click Me</button>
  <a href="/about" id="about-link">About Us</a>
  <form id="test-form">
    <input type="password" id="secret-pass" value="PRIVATE-PHASE-B1-SENTINEL" />
    <button type="submit" id="submit-btn">Submit</button>
  </form>
  <script>
    window.interactionCount = 0;
    ['click', 'dblclick', 'mousedown', 'mouseup', 'focus', 'blur', 'keydown', 'keypress', 'keyup', 'submit', 'change', 'input'].forEach(function(evt) {
        document.addEventListener(evt, function() {
            window.interactionCount++;
        }, true);
    });
  </script>
</body>
</html>
"""

class B1TrackingHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.server.request_counts['GET'] += 1
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(REAL_HTML_INITIAL.encode('utf-8'))

    def do_POST(self):
        self.server.request_counts['POST'] += 1
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # silent


@pytest.fixture(scope="module")
def b1_server():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), B1TrackingHandler)
    server.request_counts = {'GET': 0, 'POST': 0}
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_real_chromium_preflight(b1_server):
    """Real Chromium test proving zero UI events, zero storage writes, zero HTTP calls during preflight."""
    server_port = b1_server.server_port
    url = f"http://127.0.0.1:{server_port}/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()

            # Load page initially
            await page.goto(url)

            # Instrument storage writes in page
            await page.evaluate("""
                localStorage.setItem('baseline', 'val');
                window.storageWriteCount = 0;
                const origSet = Storage.prototype.setItem;
                Storage.prototype.setItem = function(k, v) {
                    if (this === window.localStorage || this === window.sessionStorage) {
                        window.storageWriteCount++;
                    }
                    try {
                        return origSet.call(this, k, v);
                    } catch(e) {}
                };
            """)

            # 1. Derive candidate intent from live page with explicit contract
            adapter = ReadOnlyBrowserSnapshotAdapter()
            snap = await adapter.capture_snapshot(
                page,
                {
                    "qa_owned": True,
                    "manual_visible_page": False,
                    "expected_context": context,
                },
            )
            norm = SemanticNormalizer().normalize_snapshot(snap)
            factory_res = CandidateActionIntentFactory.derive(norm)

            target_intent = next(i for i in factory_res.intents if i.control_fingerprint == "0:target-btn")
            assert target_intent.expected_action_type == "click"
            assert target_intent.risk_classification == RiskClassification.SAFE_CANDIDATE

            # Reset counters before preflight execution
            b1_server.request_counts['GET'] = 0
            b1_server.request_counts['POST'] = 0
            await page.evaluate("window.interactionCount = 0; window.storageWriteCount = 0;")

            # 2. Run SafeExecutionPreflight with explicit eligibility contract
            preflight = SafeExecutionPreflight()
            contract = {
                "qa_owned": True,
                "manual_visible_page": False,
                "expected_context": context,
            }
            result = await preflight.preflight(
                intent=target_intent,
                page=page,
                eligibility_contract=contract,
            )

            # Assert revalidated
            assert result.status == PreflightStatus.REVALIDATED
            assert result.intent_revalidated is True
            assert result.current_parent_state_fingerprint == target_intent.parent_state_fingerprint
            assert result.risk_classification == RiskClassification.SAFE_CANDIDATE
            assert result.candidate_intent == target_intent

            # ZERO UI events fired
            events_fired = await page.evaluate("window.interactionCount")
            assert events_fired == 0, f"Expected 0 UI events, got {events_fired}!"

            # ZERO storage writes
            storage_writes = await page.evaluate("window.storageWriteCount")
            assert storage_writes == 0, f"Expected 0 storage writes, got {storage_writes}!"

            # ZERO network requests caused by preflight
            assert b1_server.request_counts['GET'] == 0
            assert b1_server.request_counts['POST'] == 0

            # Sentinel absent from repr/str
            assert "PRIVATE-PHASE-B1-SENTINEL" not in repr(result)
            assert "PRIVATE-PHASE-B1-SENTINEL" not in str(result)

            # 3. Test Stale State by altering page in test setup
            # Reset counters
            await page.evaluate("""
                const newBtn = document.createElement('button');
                newBtn.id = 'extra-btn';
                newBtn.textContent = 'Extra Action';
                document.body.appendChild(newBtn);
                window.interactionCount = 0;
            """)

            stale_result = await preflight.preflight(
                intent=target_intent,
                page=page,
                eligibility_contract=contract,
            )

            assert stale_result.status == PreflightStatus.STALE_STATE
            assert stale_result.intent_revalidated is False
            assert stale_result.current_parent_state_fingerprint != target_intent.parent_state_fingerprint

            # Still zero UI events triggered
            events_fired_stale = await page.evaluate("window.interactionCount")
            assert events_fired_stale == 0

        finally:
            await browser.close()
