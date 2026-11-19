"""Phase C3: Controlled Interactive Disclosure Workflow Integration.

Connects:
1. Phase A read-only candidate intent discovery (ReadOnlyBrowserSnapshotAdapter, SemanticNormalizer, CandidateActionIntentFactory)
2. Phase B2 guarded state reconstruction & execution under offline isolation barrier (GuardedActionExecutor, StateReconstructor)
3. Phase C1 functional oracle evaluation with bounded DOM observation (SafeDisclosureFunctionalOracle)
4. Phase C2 canonical reporting adapter and persistence path (adapt_functional_result_to_test_record)

Enforces:
- Explicit programmatic opt-in (enabled=False by default; default behavior untouched)
- Run-scoped sequential budget control (default max 2 per page, 10 per run, cross-viewport sharing)
- Pre-allocation slot consumption before reconstruction begins (failed attempts count; no retries)
- Bounded LIMITED COVERAGE skip records for budget exhaustion (at most one per page/viewport)
- Cancellation propagation across all lifecycle phases (discovery, reconstruction, action, observation, cleanup)
- Cleanup failure circuit breaker halting subsequent evaluations for the run
- Read-only source page preservation (actions occur strictly in disposable reconstruction contexts)
- Exactly-once persistence via runner's existing results and checkpoint path
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from app.qa.browser_snapshot_adapter import (
    CancellationToken,
    ReadOnlyBrowserSnapshotAdapter,
)
from app.qa.candidate_intent_factory import CandidateActionIntentFactory
from app.qa.candidate_intent_graph import CandidateActionIntent
from app.qa.crawl_policy import normalize_crawl_url
from app.qa.functional_oracle import (
    FunctionalEvidence,
    FunctionalEvaluationResult,
    FunctionalOutcomeVerdict,
    SafeDisclosureFunctionalOracle,
)
from app.qa.functional_reporting_adapter import (
    FUNCTIONAL_DISCLOSURE_EXPECTED,
    _validate_viewport,
    adapt_functional_result_to_test_record,
    compute_opaque_action_ref,
    sanitize_target_url,
)
from app.qa.guarded_execution import (
    GuardedActionExecutor,
    GuardedExecutionStatus,
)
from app.qa.interaction_policy import RiskClassification, InteractionSafetyPolicy
from app.qa.semantic_normalizer import SemanticNormalizer, SemanticState


@dataclass(frozen=True)
class InteractiveWorkflowConfig:
    """Explicit, finite, bounded configuration for Phase C3 interactive disclosure evaluation."""

    enabled: bool = False
    max_evaluations_per_page: int = 2
    max_evaluations_per_run: int = 10
    action_timeout: float = 1.0
    reconstruction_timeout: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a boolean.")

        # Reject booleans (isinstance(True, int) is True in Python) and non-integers
        for name, val in [
            ("max_evaluations_per_page", self.max_evaluations_per_page),
            ("max_evaluations_per_run", self.max_evaluations_per_run),
        ]:
            if isinstance(val, bool) or not isinstance(val, int):
                raise TypeError(f"{name} must be a finite integer, not {type(val).__name__}.")

        if not (1 <= self.max_evaluations_per_page <= 30):
            raise ValueError("max_evaluations_per_page must be between 1 and 30.")
        if not (1 <= self.max_evaluations_per_run <= 100):
            raise ValueError("max_evaluations_per_run must be between 1 and 100.")

        for name, val in [
            ("action_timeout", self.action_timeout),
            ("reconstruction_timeout", self.reconstruction_timeout),
        ]:
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise TypeError(f"{name} must be a finite number, not {type(val).__name__}.")

        if not (0.1 <= self.action_timeout <= 10.0):
            raise ValueError("action_timeout must be between 0.1 and 10.0 seconds.")
        if not (1.0 <= self.reconstruction_timeout <= 30.0):
            raise ValueError("reconstruction_timeout must be between 1.0 and 30.0 seconds.")

    @property
    def total_evaluation_budget(self) -> float:
        """Explicit finite sum of constituent phases + safety teardown margin."""
        return self.reconstruction_timeout + self.action_timeout + 5.0


class RunnerCancellationToken(CancellationToken):
    """Bridges runner stop state to the internal CancellationToken contract."""

    def __init__(self, is_stopped_fn: Optional[Callable[[], bool]] = None) -> None:
        super().__init__()
        self._is_stopped_fn = is_stopped_fn

    def is_cancelled(self) -> bool:
        if self._cancelled:
            return True
        if self._is_stopped_fn and self._is_stopped_fn():
            return True
        return False


class InteractiveDisclosureBridge:
    """Shared orchestration component for controlled interactive disclosure testing."""

    def __init__(
        self,
        config: Optional[InteractiveWorkflowConfig] = None,
        executor: Optional[GuardedActionExecutor] = None,
        oracle: Optional[SafeDisclosureFunctionalOracle] = None,
        snapshot_adapter: Optional[ReadOnlyBrowserSnapshotAdapter] = None,
        normalizer: Optional[SemanticNormalizer] = None,
        intent_factory: Optional[CandidateActionIntentFactory] = None,
    ) -> None:
        self.config = config or InteractiveWorkflowConfig()
        self._executor = executor or GuardedActionExecutor()
        self._snapshot_adapter = snapshot_adapter or ReadOnlyBrowserSnapshotAdapter()
        self._normalizer = normalizer or SemanticNormalizer()
        self._intent_factory = intent_factory or CandidateActionIntentFactory()
        self._oracle = oracle or SafeDisclosureFunctionalOracle(
            enable_defect_classification=True,
            snapshot_adapter=self._snapshot_adapter,
            normalizer=self._normalizer,
            intent_factory=self._intent_factory,
        )
        if getattr(self._oracle, "_snapshot_adapter", None) is None:
            self._oracle._snapshot_adapter = self._snapshot_adapter
        if getattr(self._oracle, "_normalizer", None) is None:
            self._oracle._normalizer = self._normalizer
        if getattr(self._oracle, "_intent_factory", None) is None:
            self._oracle._intent_factory = self._intent_factory

        # Run-scoped budget controller state (keyed by (canonical_url, viewport_name))
        self._per_page_consumed: Dict[Tuple[str, str], int] = {}
        self._total_consumed: int = 0
        self._emitted_skips: Set[Tuple[str, str, str]] = set()

        # Circuit breaker: true if disposable context teardown fails or is uncertain
        self._cleanup_halted: bool = False

    def is_enabled(self) -> bool:
        return self.config.enabled

    @property
    def is_cleanup_halted(self) -> bool:
        return self._cleanup_halted

    def halt_cleanup(self) -> None:
        """Trip circuit breaker to halt further interactive evaluations for this run."""
        self._cleanup_halted = True

    @property
    def total_evaluations_consumed(self) -> int:
        return self._total_consumed

    def get_page_evaluations_consumed(self, page_url: str, viewport: Optional[str] = None) -> int:
        canonical_url = normalize_crawl_url(page_url)
        if viewport:
            return self._per_page_consumed.get((canonical_url, viewport), 0)
        return sum(v for (u, _vp), v in self._per_page_consumed.items() if u == canonical_url)

    def _create_budget_skip_record(
        self,
        run_id: int,
        url: str,
        viewport: str,
        reason: str,
        control_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Produce a bounded, truthful LIMITED COVERAGE record when evaluations are skipped due to budget."""
        safe_viewport = _validate_viewport(viewport)
        ref = f"{control_id}:{reason}" if control_id else reason
        action_token = compute_opaque_action_ref(action_ref=ref, page_url=url, viewport=safe_viewport)
        safe_url = sanitize_target_url(url, action_ref=action_token)

        if reason == "per_page_budget":
            actual = (
                f"Coverage limited: per-page evaluation budget exhausted "
                f"(max {self.config.max_evaluations_per_page} per page)."
            )
        else:
            actual = (
                f"Coverage limited: per-run evaluation budget exhausted "
                f"(max {self.config.max_evaluations_per_run} per run)."
            )
        if control_id:
            actual += f" Skipped control: '{control_id}'."

        return {
            "run_id": int(run_id),
            "url": safe_url,
            "final_url": safe_url,
            "viewport": safe_viewport,
            "status": "LIMITED COVERAGE",
            "expected_result": FUNCTIONAL_DISCLOSURE_EXPECTED,
            "actual_result": actual,
            "screenshot_path": None,
            "screenshot": None,
            "stable_id": None,
            "finding_id": None,
        }

    def _create_discovery_diagnostic_record(
        self,
        run_id: int,
        url: str,
        viewport: str,
        status: str,
        actual_result: str,
    ) -> Dict[str, Any]:
        """Produce a bounded diagnostic or limited coverage record when discovery fails or is incomplete."""
        safe_viewport = _validate_viewport(viewport)
        action_token = compute_opaque_action_ref(action_ref="discovery_diagnostic", page_url=url, viewport=safe_viewport)
        safe_url = sanitize_target_url(url, action_ref=action_token)

        return {
            "run_id": int(run_id),
            "url": safe_url,
            "final_url": safe_url,
            "viewport": safe_viewport,
            "status": status,
            "expected_result": FUNCTIONAL_DISCLOSURE_EXPECTED,
            "actual_result": actual_result,
            "screenshot_path": None,
            "screenshot": None,
            "stable_id": None,
            "finding_id": None,
        }

    def create_infrastructure_diagnostic_record(
        self,
        run_id: int,
        url: str,
        viewport: str,
        reason: str = "Unexpected error during interactive evaluation.",
    ) -> Dict[str, Any]:
        """Produce a bounded diagnostic record for runner-level infrastructure exceptions."""
        safe_viewport = _validate_viewport(viewport)
        action_token = compute_opaque_action_ref(action_ref="infrastructure_error", page_url=url, viewport=safe_viewport)
        safe_url = sanitize_target_url(url, action_ref=action_token)

        return {
            "run_id": int(run_id),
            "url": safe_url,
            "final_url": safe_url,
            "viewport": safe_viewport,
            "status": "DIAGNOSTIC",
            "expected_result": FUNCTIONAL_DISCLOSURE_EXPECTED,
            "actual_result": f"Interactive disclosure evaluation halted: {reason}",
            "screenshot_path": None,
            "screenshot": None,
            "stable_id": None,
            "finding_id": None,
        }

    def _emit_record(
        self,
        record: Dict[str, Any],
        record_sink: Optional[Callable[[Dict[str, Any]], None]],
        results: List[Dict[str, Any]],
    ) -> None:
        """Append record to results and deliver incrementally to runner's sink if available."""
        results.append(record)
        if record_sink is not None:
            try:
                record_sink(record)
            except Exception:
                pass

    def _filter_safe_disclosure_candidates(
        self, intents: Sequence[CandidateActionIntent], semantic_state: SemanticState
    ) -> List[CandidateActionIntent]:
        """Select safe, read-only discovered candidates supported in Phase B2/C."""
        controls_by_id = {c.control_id: c for c in semantic_state.controls}
        safe_candidates: List[CandidateActionIntent] = []
        for intent in intents:
            if intent.expected_action_type != "click":
                continue
            if intent.risk_classification != RiskClassification.SAFE_CANDIDATE:
                continue
            control = controls_by_id.get(intent.control_fingerprint)
            if control is None:
                continue
            if control.accessible_role in ("button", "link", "summary") and control.state_expanded is not None:
                safe_candidates.append(intent)
            elif control.accessible_role == "tab" or control.state_selected is not None:
                safe_candidates.append(intent)
            elif control.accessible_role == "summary":
                safe_candidates.append(intent)
            elif control.accessible_role == "menuitem":
                name_lower = (control.accessible_name or "").lower()
                is_mutating_name = any(sig in name_lower for sig in ("save", "submit", "upload", "delete", "remove", "update", "purge", "revoke"))
                if not is_mutating_name and not control.form_ownership:
                    safe_candidates.append(intent)
            elif control.accessible_role == "button":
                name_lower = (control.accessible_name or "").lower()
                id_lower = (control.control_id or "").lower()
                is_mutating_name = any(sig in name_lower for sig in ("save", "submit", "upload", "delete", "remove", "update", "purge", "revoke"))
                if is_mutating_name or control.form_ownership:
                    continue
                is_disclosure_like = (
                    any(k in name_lower or k in id_lower for k in ("dialog", "history", "validate", "detail", "menu", "close", "dismiss", "modal"))
                )
                if is_disclosure_like:
                    safe_candidates.append(intent)
        return safe_candidates

    async def evaluate_page_disclosures(
        self,
        source_context: Any,
        source_page: Any,
        page_url: str,
        viewport_name: str,
        run_id: int,
        is_stopped_fn: Optional[Callable[[], bool]] = None,
        record_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
        project_id: Optional[int] = None,
        viewport: Optional[Dict[str, int]] = None,
    ) -> List[Dict[str, Any]]:
        """Evaluate safe disclosure controls discovered on a valid QA source page.

        Guarantees:
        - Source page is strictly read-only; no click/mutation occurs on source page.
        - Actions execute strictly inside disposable contexts under the offline barrier.
        - Budgets (per-page and per-run) are strictly honored sequentially.
        - Budget slot is pre-allocated before reconstruction begins.
        - Teardown failures immediately halt further interactive evaluations for the run.
        - Outcomes are adapted to canonical test records without customer findings.
        - Incremental delivery preserves completed records even if later evaluations fail.
        """
        if viewport is None or not isinstance(viewport, dict):
            if "mobile" in str(viewport_name).lower():
                target_viewport: Dict[str, int] = {"width": 390, "height": 844}
            else:
                target_viewport = {"width": 1366, "height": 768}
        else:
            target_viewport = {
                "width": int(viewport.get("width", 1366)),
                "height": int(viewport.get("height", 768)),
            }

        if not self.is_enabled():
            return []

        if self._cleanup_halted:
            return []

        if is_stopped_fn and is_stopped_fn():
            return []

        if source_page is None or source_page.is_closed():
            return []

        canonical_url = normalize_crawl_url(page_url)
        cancellation_token = RunnerCancellationToken(is_stopped_fn)
        results: List[Dict[str, Any]] = []

        if cancellation_token.is_cancelled():
            rec = self._create_discovery_diagnostic_record(
                run_id=run_id,
                url=canonical_url,
                viewport=viewport_name,
                status="LIMITED COVERAGE",
                actual_result="Interactive disclosure discovery cancelled before candidate evaluation.",
            )
            self._emit_record(rec, record_sink, results)
            return results

        # 1. Read-only candidate intent discovery on source_page
        expected_ctx = getattr(source_page, "context", source_context)
        eligibility = {
            "qa_owned": True,
            "manual_visible_page": False,
            "expected_context": expected_ctx,
        }

        try:
            snapshot = await self._snapshot_adapter.capture_snapshot(
                page=source_page,
                eligibility_contract=eligibility,
                cancellation_token=cancellation_token,
            )
        except Exception:
            # Discovery exception: could not evaluate page
            rec = self._create_discovery_diagnostic_record(
                run_id=run_id,
                url=canonical_url,
                viewport=viewport_name,
                status="DIAGNOSTIC",
                actual_result="Interactive disclosure discovery failed due to page snapshot error.",
            )
            self._emit_record(rec, record_sink, results)
            return results

        if cancellation_token.is_cancelled():
            rec = self._create_discovery_diagnostic_record(
                run_id=run_id,
                url=canonical_url,
                viewport=viewport_name,
                status="LIMITED COVERAGE",
                actual_result="Interactive disclosure discovery cancelled before candidate evaluation.",
            )
            self._emit_record(rec, record_sink, results)
            return results

        if snapshot.coverage.terminal_status != "COMPLETE":
            rec = self._create_discovery_diagnostic_record(
                run_id=run_id,
                url=canonical_url,
                viewport=viewport_name,
                status="LIMITED COVERAGE",
                actual_result="Coverage limited: page snapshot for interactive disclosure discovery was incomplete.",
            )
            self._emit_record(rec, record_sink, results)
            return results

        norm_res = self._normalizer.normalize_snapshot(snapshot)
        if norm_res.terminal_status != "COMPLETE" or norm_res.semantic_state is None:
            rec = self._create_discovery_diagnostic_record(
                run_id=run_id,
                url=canonical_url,
                viewport=viewport_name,
                status="LIMITED COVERAGE",
                actual_result="Coverage limited: semantic normalization for interactive disclosure discovery was incomplete.",
            )
            self._emit_record(rec, record_sink, results)
            return results

        derivation = self._intent_factory.derive_intents(norm_res)
        if derivation.terminal_status != "COMPLETE":
            rec = self._create_discovery_diagnostic_record(
                run_id=run_id,
                url=canonical_url,
                viewport=viewport_name,
                status="LIMITED COVERAGE",
                actual_result="Coverage limited: candidate intent derivation for interactive disclosure discovery was incomplete.",
            )
            self._emit_record(rec, record_sink, results)
            return results

        # Truthfully distinguished: page successfully evaluated with no supported candidates
        if not derivation.intents:
            return []

        # Report mutating controls from candidate intents
        controls_by_id = {c.control_id: c for c in norm_res.semantic_state.controls}
        for intent in derivation.intents:
            if intent.risk_classification == RiskClassification.POTENTIALLY_MUTATING:
                ctrl = controls_by_id.get(intent.control_fingerprint)
                ctrl_name = ctrl.accessible_name if ctrl else "Mutating control"
                skip_key = (canonical_url, viewport_name, f"mutation_{intent.control_fingerprint}")
                if skip_key not in self._emitted_skips:
                    self._emitted_skips.add(skip_key)
                    safe_viewport = _validate_viewport(viewport_name)
                    action_token = compute_opaque_action_ref(
                        action_ref=f"{intent.control_fingerprint}:{intent.expected_action_type}",
                        page_url=canonical_url,
                        viewport=safe_viewport,
                    )
                    safe_url = sanitize_target_url(canonical_url, action_ref=action_token)
                    act_msg = f"Action '{ctrl_name}' ({intent.expected_action_type}) is a persistent mutation or server modification outside authorized execution scope (Phase D permit required)."
                    mut_rec = {
                        "run_id": int(run_id),
                        "url": safe_url,
                        "final_url": safe_url,
                        "viewport": safe_viewport,
                        "status": "LIMITED COVERAGE",
                        "expected_result": "Persistent profile mutation requires explicit Phase D authorization and restoration workflow.",
                        "actual_result": act_msg,
                        "screenshot_path": None,
                        "screenshot": None,
                        "stable_id": None,
                        "finding_id": None,
                    }
                    self._emit_record(mut_rec, record_sink, results)

        # Report prohibited controls directly from semantic state for coverage accounting (never eligible for execution)
        for ctrl in norm_res.semantic_state.controls:
            if ctrl.visibility and ctrl.enabled:
                if InteractionSafetyPolicy.classify(ctrl) == RiskClassification.PROHIBITED:
                    skip_key = (canonical_url, viewport_name, f"mutation_{ctrl.control_id}")
                    if skip_key not in self._emitted_skips:
                        self._emitted_skips.add(skip_key)
                        action_type = CandidateActionIntentFactory._action_type_for_role(ctrl.accessible_role) or "click"
                        ctrl_name = ctrl.accessible_name or "Prohibited control"
                        safe_viewport = _validate_viewport(viewport_name)
                        action_token = compute_opaque_action_ref(
                            action_ref=f"{ctrl.control_id}:{action_type}",
                            page_url=canonical_url,
                            viewport=safe_viewport,
                        )
                        safe_url = sanitize_target_url(canonical_url, action_ref=action_token)
                        prohib_rec = {
                            "run_id": int(run_id),
                            "url": safe_url,
                            "final_url": safe_url,
                            "viewport": safe_viewport,
                            "status": "LIMITED COVERAGE",
                            "expected_result": "Persistent profile mutation requires explicit Phase D authorization and restoration workflow.",
                            "actual_result": f"Action '{ctrl_name}' ({action_type}) is a prohibited or destructive action outside authorized execution scope (Phase D permit required).",
                            "screenshot_path": None,
                            "screenshot": None,
                            "stable_id": None,
                            "finding_id": None,
                        }
                        self._emit_record(prohib_rec, record_sink, results)

        # Discovered initial unsupported controls
        intent_control_ids = {intent.control_fingerprint for intent in derivation.intents}
        for ctrl in norm_res.semantic_state.controls:
            if ctrl.visibility and ctrl.enabled and ctrl.control_id not in intent_control_ids:
                if InteractionSafetyPolicy.classify(ctrl) == RiskClassification.PROHIBITED:
                    continue
                skip_key = (canonical_url, viewport_name, f"unsupported_{ctrl.control_id}")
                if skip_key not in self._emitted_skips:
                    self._emitted_skips.add(skip_key)
                    safe_viewport = _validate_viewport(viewport_name)
                    action_token = compute_opaque_action_ref(
                        action_ref=f"{ctrl.control_id}:unsupported",
                        page_url=canonical_url,
                        viewport=safe_viewport,
                    )
                    safe_url = sanitize_target_url(canonical_url, action_ref=action_token)
                    unsupp_rec = {
                        "run_id": int(run_id),
                        "url": safe_url,
                        "final_url": safe_url,
                        "viewport": safe_viewport,
                        "status": "LIMITED COVERAGE",
                        "expected_result": "Control interaction requires supported interactive role pattern.",
                        "actual_result": f"Control '{ctrl.accessible_name or ctrl.control_id}' ({ctrl.accessible_role}) is unsupported for ephemeral disclosure evaluation.",
                        "screenshot_path": None,
                        "screenshot": None,
                        "stable_id": None,
                        "finding_id": None,
                    }
                    self._emit_record(unsupp_rec, record_sink, results)

        safe_candidates = self._filter_safe_disclosure_candidates(
            derivation.intents, norm_res.semantic_state
        )
        if not safe_candidates:
            return results

        # 2. Sequential execution under budget and cancellation control
        for candidate in safe_candidates:
            if is_stopped_fn and is_stopped_fn():
                break

            if self._cleanup_halted:
                break

            # Check per-page budget
            budget_key = (canonical_url, viewport_name)
            page_consumed = self._per_page_consumed.get(budget_key, 0)
            if page_consumed >= self.config.max_evaluations_per_page:
                skip_key = (canonical_url, viewport_name, "per_page_budget")
                if skip_key not in self._emitted_skips:
                    self._emitted_skips.add(skip_key)
                    skip_rec = self._create_budget_skip_record(
                        run_id=run_id,
                        url=canonical_url,
                        viewport=viewport_name,
                        reason="per_page_budget",
                        control_id=candidate.control_fingerprint,
                    )
                    self._emit_record(skip_rec, record_sink, results)
                break

            # Check per-run budget
            if self._total_consumed >= self.config.max_evaluations_per_run:
                skip_key = (canonical_url, viewport_name, "per_run_budget")
                if skip_key not in self._emitted_skips:
                    self._emitted_skips.add(skip_key)
                    skip_rec = self._create_budget_skip_record(
                        run_id=run_id,
                        url=canonical_url,
                        viewport=viewport_name,
                        reason="per_run_budget",
                        control_id=candidate.control_fingerprint,
                    )
                    self._emit_record(skip_rec, record_sink, results)
                break

            # Pre-consume slot before reconstruction begins (failed attempts count; no retry)
            self._per_page_consumed[budget_key] = page_consumed + 1
            self._total_consumed += 1

            # Execute bounded guarded disclosure oracle evaluation
            lifecycle_tracker = {
                "phase": "START",
                "action_attempted": False,
                "action_confirmed": False,
                "cleanup_completed": False,
                "cleanup_failed": False,
            }
            eval_res: Optional[FunctionalEvaluationResult] = None
            try:
                eval_res = await asyncio.wait_for(
                    self._oracle.evaluate_disclosure_action(
                        executor=self._executor,
                        source_context=source_context,
                        approved_target_url=canonical_url,
                        candidate_intent=candidate,
                        timeout=self.config.action_timeout,
                        reconstruction_timeout=self.config.reconstruction_timeout,
                        action_timeout=self.config.action_timeout,
                        cancellation_token=cancellation_token,
                        lifecycle_tracker=lifecycle_tracker,
                        project_id=project_id,
                        viewport=target_viewport,
                    ),
                    timeout=self.config.total_evaluation_budget,
                )
            except asyncio.TimeoutError:
                # Bounded total evaluation budget expired
                attempted = lifecycle_tracker["action_attempted"]
                confirmed = lifecycle_tracker["action_confirmed"]
                phase = lifecycle_tracker["phase"]
                self._cleanup_halted = True

                eval_res = FunctionalEvaluationResult(
                    verdict=FunctionalOutcomeVerdict.LIMITED_COVERAGE,
                    is_customer_defect=False,
                    defect_category=None,
                    target_control_id=candidate.control_fingerprint,
                    action_type=candidate.expected_action_type,
                    evidence=FunctionalEvidence(
                        action_attempted=attempted,
                        action_confirmed=confirmed,
                        expanded_before=None,
                        expanded_after=None,
                        state_toggled=False,
                        controls_count_delta=None,
                        fingerprint_changed=False,
                        pre_state_fingerprint=None,
                        post_state_fingerprint=None,
                    ),
                    execution_status=GuardedExecutionStatus.ACTION_FAILED if not confirmed else GuardedExecutionStatus.OBSERVATION_FAILED,
                    limitations=("total_evaluation_budget_exhausted", f"interrupted_during_{phase.lower()}"),
                    diagnostics={"evaluation_budget_timeout": True, "interrupted_phase": phase, "cleanup_uncertain": True},
                )
            except asyncio.CancelledError:
                attempted = lifecycle_tracker["action_attempted"]
                confirmed = lifecycle_tracker["action_confirmed"]
                phase = lifecycle_tracker["phase"]
                self._cleanup_halted = True

                eval_res = FunctionalEvaluationResult(
                    verdict=FunctionalOutcomeVerdict.LIMITED_COVERAGE,
                    is_customer_defect=False,
                    defect_category=None,
                    target_control_id=candidate.control_fingerprint,
                    action_type=candidate.expected_action_type,
                    evidence=FunctionalEvidence(
                        action_attempted=attempted,
                        action_confirmed=confirmed,
                        expanded_before=None,
                        expanded_after=None,
                        state_toggled=False,
                        controls_count_delta=None,
                        fingerprint_changed=False,
                        pre_state_fingerprint=None,
                        post_state_fingerprint=None,
                    ),
                    execution_status=GuardedExecutionStatus.CANCELLED,
                    limitations=("operation_cancelled", f"interrupted_during_{phase.lower()}"),
                    diagnostics={"cancelled": True, "interrupted_phase": phase, "cleanup_uncertain": not lifecycle_tracker["cleanup_completed"]},
                )
                test_rec = adapt_functional_result_to_test_record(
                    eval_res=eval_res,
                    run_id=run_id,
                    target_url=canonical_url,
                    viewport=viewport_name,
                )
                self._emit_record(test_rec, record_sink, results)
                raise
            except Exception as exc:
                # Unexpected exception during candidate evaluation
                attempted = lifecycle_tracker["action_attempted"]
                confirmed = lifecycle_tracker["action_confirmed"]
                phase = lifecycle_tracker["phase"]
                self._cleanup_halted = True

                eval_res = FunctionalEvaluationResult(
                    verdict=FunctionalOutcomeVerdict.DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE,
                    is_customer_defect=False,
                    defect_category=None,
                    target_control_id=candidate.control_fingerprint,
                    action_type=candidate.expected_action_type,
                    evidence=FunctionalEvidence(
                        action_attempted=attempted,
                        action_confirmed=confirmed,
                        expanded_before=None,
                        expanded_after=None,
                        state_toggled=False,
                        controls_count_delta=None,
                        fingerprint_changed=False,
                        pre_state_fingerprint=None,
                        post_state_fingerprint=None,
                    ),
                    execution_status=GuardedExecutionStatus.FAILED,
                    limitations=("unexpected_evaluation_exception",),
                    diagnostics={
                        "error_type": type(exc).__name__,
                        "interrupted_phase": phase,
                        "cleanup_uncertain": not lifecycle_tracker["cleanup_completed"],
                    },
                )

            # Teardown circuit breaker check: halt if cleanup failed or cannot be verified
            if (
                eval_res.verdict == FunctionalOutcomeVerdict.DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE
                or eval_res.execution_status == GuardedExecutionStatus.CLEANUP_FAILED
                or lifecycle_tracker["cleanup_failed"]
                or not lifecycle_tracker["cleanup_completed"]
            ):
                self._cleanup_halted = True

            # Adapt to canonical test record
            test_rec = adapt_functional_result_to_test_record(
                eval_res=eval_res,
                run_id=run_id,
                target_url=canonical_url,
                viewport=viewport_name,
                enable_customer_defects=True,
            )
            self._emit_record(test_rec, record_sink, results)

            # Bounded exploration of supported reachable controls revealed behind successful preceding action
            if (
                not self._cleanup_halted
                and eval_res
                and eval_res.verdict == FunctionalOutcomeVerdict.VERIFIED_SUCCESS
                and (not is_stopped_fn or not is_stopped_fn())
            ):
                rev_controls = eval_res.diagnostics.get("revealed_controls", [])
                rev_intents = eval_res.diagnostics.get("revealed_intents", [])
                rev_fp = eval_res.diagnostics.get("revealed_state_fingerprint")
                rev_by_id = {c.control_id: c for c in rev_controls}
                initial_visible_ctrl_ids = {c.control_id for c in norm_res.semantic_state.controls if c.visibility and c.enabled}

                # Account for revealed prohibited controls directly from revealed_controls (never eligible for execution)
                for rev_c in rev_controls:
                    if rev_c.visibility and rev_c.enabled and rev_c.control_id not in initial_visible_ctrl_ids:
                        if InteractionSafetyPolicy.classify(rev_c) == RiskClassification.PROHIBITED:
                            skip_key = (
                                canonical_url,
                                viewport_name,
                                f"revealed_mutation_{rev_c.control_id}",
                            )
                            if skip_key not in self._emitted_skips:
                                self._emitted_skips.add(skip_key)
                                c_name = rev_c.accessible_name or "Prohibited control"
                                action_type = CandidateActionIntentFactory._action_type_for_role(rev_c.accessible_role) or "click"
                                safe_viewport = _validate_viewport(viewport_name)
                                action_token = compute_opaque_action_ref(
                                    action_ref=f"{rev_c.control_id}:{action_type}",
                                    page_url=canonical_url,
                                    viewport=safe_viewport,
                                )
                                safe_url = sanitize_target_url(canonical_url, action_ref=action_token)
                                act_msg = (
                                    f"Action '{c_name}' ({action_type}) "
                                    f"is a prohibited or destructive action outside authorized execution scope (Phase D permit required)."
                                )
                                prohib_rec = {
                                    "run_id": int(run_id),
                                    "url": safe_url,
                                    "final_url": safe_url,
                                    "viewport": safe_viewport,
                                    "status": "LIMITED COVERAGE",
                                    "expected_result": "Persistent profile mutation requires explicit Phase D authorization and restoration workflow.",
                                    "actual_result": act_msg,
                                    "screenshot_path": None,
                                    "screenshot": None,
                                    "stable_id": None,
                                    "finding_id": None,
                                }
                                self._emit_record(prohib_rec, record_sink, results)

                for rev_intent in rev_intents:
                    if is_stopped_fn and is_stopped_fn():
                        break
                    if self._cleanup_halted:
                        break
                    if rev_intent.control_fingerprint in initial_visible_ctrl_ids:
                        continue

                    # Mutating revealed controls
                    if rev_intent.risk_classification == RiskClassification.POTENTIALLY_MUTATING:
                        skip_key = (
                            canonical_url,
                            viewport_name,
                            f"revealed_mutation_{rev_intent.control_fingerprint}",
                        )
                        if skip_key not in self._emitted_skips:
                            self._emitted_skips.add(skip_key)
                            rev_c = rev_by_id.get(rev_intent.control_fingerprint)
                            c_name = rev_c.accessible_name if rev_c else "Mutating control"
                            safe_viewport = _validate_viewport(viewport_name)
                            action_token = compute_opaque_action_ref(
                                action_ref=f"{rev_intent.control_fingerprint}:{rev_intent.expected_action_type}",
                                page_url=canonical_url,
                                viewport=safe_viewport,
                            )
                            safe_url = sanitize_target_url(canonical_url, action_ref=action_token)
                            act_msg = (
                                f"Action '{c_name}' ({rev_intent.expected_action_type}) "
                                f"is a persistent mutation or server modification outside authorized execution scope (Phase D permit required)."
                            )
                            mut_rec = {
                                "run_id": int(run_id),
                                "url": safe_url,
                                "final_url": safe_url,
                                "viewport": safe_viewport,
                                "status": "LIMITED COVERAGE",
                                "expected_result": "Persistent profile mutation requires explicit Phase D authorization and restoration workflow.",
                                "actual_result": act_msg,
                                "screenshot_path": None,
                                "screenshot": None,
                                "stable_id": None,
                                "finding_id": None,
                            }
                            self._emit_record(mut_rec, record_sink, results)
                        continue

                    # Safe candidate revealed behind preceding action
                    if rev_intent.risk_classification == RiskClassification.SAFE_CANDIDATE:
                        # Check budgets
                        budget_key = (canonical_url, viewport_name)
                        page_consumed = self._per_page_consumed.get(budget_key, 0)
                        if page_consumed >= self.config.max_evaluations_per_page:
                            skip_key = (canonical_url, viewport_name, "per_page_budget")
                            if skip_key not in self._emitted_skips:
                                self._emitted_skips.add(skip_key)
                                skip_rec = self._create_budget_skip_record(
                                    run_id=run_id,
                                    url=canonical_url,
                                    viewport=viewport_name,
                                    reason="per_page_budget",
                                    control_id=rev_intent.control_fingerprint,
                                )
                                self._emit_record(skip_rec, record_sink, results)
                            break

                        if self._total_consumed >= self.config.max_evaluations_per_run:
                            skip_key = (canonical_url, viewport_name, "per_run_budget")
                            if skip_key not in self._emitted_skips:
                                self._emitted_skips.add(skip_key)
                                skip_rec = self._create_budget_skip_record(
                                    run_id=run_id,
                                    url=canonical_url,
                                    viewport=viewport_name,
                                    reason="per_run_budget",
                                    control_id=rev_intent.control_fingerprint,
                                )
                                self._emit_record(skip_rec, record_sink, results)
                            break

                        # Pre-consume slot
                        self._per_page_consumed[budget_key] = page_consumed + 1
                        self._total_consumed += 1

                        prec_list = getattr(candidate, "preceding_intents", ()) or ()
                        chained_candidate = CandidateActionIntent(
                            parent_state_fingerprint=rev_fp,
                            control_fingerprint=rev_intent.control_fingerprint,
                            expected_action_type=rev_intent.expected_action_type,
                            risk_classification=rev_intent.risk_classification,
                            preceding_intents=prec_list + (candidate,),
                        )

                        sub_lifecycle = {
                            "phase": "START",
                            "action_attempted": False,
                            "action_confirmed": False,
                            "cleanup_completed": False,
                            "cleanup_failed": False,
                        }
                        try:
                            sub_res = await asyncio.wait_for(
                                self._oracle.evaluate_disclosure_action(
                                    executor=self._executor,
                                    source_context=source_context,
                                    approved_target_url=canonical_url,
                                    candidate_intent=chained_candidate,
                                    timeout=self.config.action_timeout,
                                    reconstruction_timeout=self.config.reconstruction_timeout,
                                    action_timeout=self.config.action_timeout,
                                    cancellation_token=cancellation_token,
                                    lifecycle_tracker=sub_lifecycle,
                                    project_id=project_id,
                                    viewport=target_viewport,
                                ),
                                timeout=self.config.total_evaluation_budget,
                            )
                        except Exception as sub_exc:
                            sub_res = FunctionalEvaluationResult(
                                verdict=FunctionalOutcomeVerdict.DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE,
                                is_customer_defect=False,
                                defect_category=None,
                                target_control_id=chained_candidate.control_fingerprint,
                                action_type=chained_candidate.expected_action_type,
                                evidence=FunctionalEvidence(
                                    action_attempted=sub_lifecycle["action_attempted"],
                                    action_confirmed=sub_lifecycle["action_confirmed"],
                                    expanded_before=None,
                                    expanded_after=None,
                                    state_toggled=False,
                                    controls_count_delta=None,
                                    fingerprint_changed=False,
                                    pre_state_fingerprint=None,
                                    post_state_fingerprint=None,
                                ),
                                execution_status=GuardedExecutionStatus.FAILED,
                                limitations=("unexpected_chained_evaluation_exception",),
                                diagnostics={"error_type": type(sub_exc).__name__},
                            )

                        if (
                            sub_res.verdict == FunctionalOutcomeVerdict.DIAGNOSTIC_OR_INFRASTRUCTURE_ISSUE
                            or sub_res.execution_status == GuardedExecutionStatus.CLEANUP_FAILED
                            or sub_lifecycle["cleanup_failed"]
                            or not sub_lifecycle["cleanup_completed"]
                        ):
                            self._cleanup_halted = True

                        sub_test_rec = adapt_functional_result_to_test_record(
                            eval_res=sub_res,
                            run_id=run_id,
                            target_url=canonical_url,
                            viewport=viewport_name,
                            enable_customer_defects=True,
                        )
                        self._emit_record(sub_test_rec, record_sink, results)

                # Discovered revealed controls without intents (unsupported roles/inputs)
                rev_intent_ctrl_ids = {i.control_fingerprint for i in rev_intents}
                for rev_ctrl in rev_controls:
                    if (
                        rev_ctrl.control_id not in initial_visible_ctrl_ids
                        and rev_ctrl.control_id not in rev_intent_ctrl_ids
                        and rev_ctrl.visibility
                        and rev_ctrl.enabled
                    ):
                        if InteractionSafetyPolicy.classify(rev_ctrl) == RiskClassification.PROHIBITED:
                            continue
                        skip_key = (
                            canonical_url,
                            viewport_name,
                            f"revealed_unsupported_{rev_ctrl.control_id}",
                        )
                        if skip_key not in self._emitted_skips:
                            self._emitted_skips.add(skip_key)
                            safe_viewport = _validate_viewport(viewport_name)
                            action_token = compute_opaque_action_ref(
                                action_ref=f"{rev_ctrl.control_id}:unsupported",
                                page_url=canonical_url,
                                viewport=safe_viewport,
                            )
                            safe_url = sanitize_target_url(canonical_url, action_ref=action_token)
                            unsupp_rec = {
                                "run_id": int(run_id),
                                "url": safe_url,
                                "final_url": safe_url,
                                "viewport": safe_viewport,
                                "status": "LIMITED COVERAGE",
                                "expected_result": "Control interaction requires supported interactive role pattern.",
                                "actual_result": f"Control '{rev_ctrl.accessible_name or rev_ctrl.control_id}' ({rev_ctrl.accessible_role}) is unsupported for ephemeral disclosure evaluation.",
                                "screenshot_path": None,
                                "screenshot": None,
                                "stable_id": None,
                                "finding_id": None,
                            }
                            self._emit_record(unsupp_rec, record_sink, results)

            if self._cleanup_halted:
                break

        return results
