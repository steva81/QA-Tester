"""Explicit domain validation contracts and evaluation engine for local form constraints.

Separates:
1. Reusable validation rules and test cases.
2. Explicit project/target-scoped contract bindings.
3. Truthful observation evaluation preventing false PASS and arbitrary visible text shortcuts.
"""

import fnmatch
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class ValidationVerdict(Enum):
    SATISFIED = "SATISFIED"
    DEFECT_ACCEPTED_INVALID = "DEFECT_ACCEPTED_INVALID"
    DEFECT_REJECTED_VALID = "DEFECT_REJECTED_VALID"
    AMBIGUOUS_FEEDBACK = "AMBIGUOUS_FEEDBACK"
    MISSING_CONTRACT = "MISSING_CONTRACT"
    INCOMPLETE_COVERAGE = "INCOMPLETE_COVERAGE"
    PREREQUISITE_FAILED = "PREREQUISITE_FAILED"


@dataclass(frozen=True)
class ValidationTestCase:
    case_id: str
    description: str
    input_values: Dict[str, str]  # field_name -> input value
    expected_valid: bool
    expected_feedback_substring: Optional[str] = None
    expected_feedback_absent: bool = False


@dataclass(frozen=True)
class ValidationTestCaseResult:
    case: ValidationTestCase
    actual_feedback_text: str
    actual_feedback_visible: bool
    passed: bool
    failure_reason: Optional[str] = None
    is_fresh: bool = True
    observation_status: str = "COMPLETED"


@dataclass(frozen=True)
class ValidationContractResult:
    contract_id: str
    rule_name: str
    verdict: ValidationVerdict
    all_passed: bool
    is_customer_defect: bool
    defect_description: Optional[str]
    case_results: Tuple[ValidationTestCaseResult, ...]
    tested_cases_count: int
    contract: Optional[Any] = None

    @property
    def summary(self) -> str:
        if self.defect_description:
            return self.defect_description
        if self.all_passed:
            return f"All {self.tested_cases_count} validation cases satisfied."
        return f"Validation verdict: {self.verdict.value}."

    @property
    def test_case_results(self) -> Tuple[ValidationTestCaseResult, ...]:
        return self.case_results


@dataclass(frozen=True)
class ValidationContract:
    contract_id: str
    rule_name: str
    trigger_control_id: Optional[str] = None  # canonical ID or raw ID of the trigger button
    field_selectors: Dict[str, str] = None  # field_name -> CSS selector
    feedback_selector: str = ""  # CSS selector for feedback element
    test_cases: Tuple[ValidationTestCase, ...] = ()
    target_url_pattern: Optional[str] = None
    trigger_name: Optional[str] = None
    trigger_selector: Optional[str] = None
    completion_marker_attr: Optional[str] = None
    completion_marker_type: Optional[str] = None
    completion_timestamp_attr: Optional[str] = None
    completion_mode: Optional[str] = None

    @property
    def effective_completion_mode(self) -> Optional[str]:
        if self.completion_mode:
            return self.completion_mode.strip().lower()
        if self.completion_marker_attr:
            if self.completion_marker_type == "token":
                return "token"
            return "case_marker"
        if self.completion_timestamp_attr:
            return "timestamp"
        return None

    def validate_completion_configuration(self) -> Tuple[bool, Optional[str]]:
        """Validate completion configuration.
        
        Returns (is_valid, error_reason).
        Rejects unsupported, contradictory, or missing configurations.
        """
        mode = self.effective_completion_mode
        if not mode or mode in ("none", "unspecified"):
            return False, "No completion semantics configured (completion_mode, completion_marker_attr, or completion_timestamp_attr required)"
        
        valid_modes = {"case_marker", "loading_transition", "timestamp", "token"}
        if mode not in valid_modes:
            return False, f"Unsupported completion_mode '{mode}'; must be one of {sorted(list(valid_modes))}"
        
        if mode == "case_marker":
            if not self.completion_marker_attr:
                return False, "completion_mode 'case_marker' requires 'completion_marker_attr' to be specified"
            if self.completion_marker_type and self.completion_marker_type not in ("case_id", "attribute_presence"):
                return False, f"Contradictory configuration: completion_mode 'case_marker' is incompatible with completion_marker_type '{self.completion_marker_type}'"
            return True, None
        
        if mode == "timestamp":
            if not self.completion_timestamp_attr:
                return False, "completion_mode 'timestamp' requires 'completion_timestamp_attr' to be specified"
            return True, None
        
        if mode == "token":
            if not self.completion_marker_attr:
                return False, "completion_mode 'token' requires 'completion_marker_attr' to be specified"
            return True, None
        
        if mode == "loading_transition":
            return True, None
        
        return False, f"Unsupported configuration for completion_mode '{mode}'"

    @property
    def has_completion_semantics(self) -> bool:
        """Indicate whether the contract declares an explicit, verifiable completion mechanism."""
        is_valid, _ = self.validate_completion_configuration()
        return is_valid

    def evaluate_case(
        self,
        case: ValidationTestCase,
        feedback_visible: bool,
        feedback_text: str,
        is_fresh: bool = True,
        observation_status: str = "COMPLETED",
    ) -> ValidationTestCaseResult:
        """Evaluate a single test case observation against expected validity.
        
        Requires positive expected evidence for each case. Arbitrary visible feedback
        or missing evidence cannot satisfy a case.
        """
        clean_text = (feedback_text or "").strip()
        text_lower = clean_text.lower()

        if not is_fresh or observation_status != "COMPLETED":
            return ValidationTestCaseResult(
                case=case,
                actual_feedback_text=clean_text,
                actual_feedback_visible=feedback_visible,
                passed=False,
                failure_reason=f"Fresh evidence could not be established for case '{case.description}' (status: {observation_status})",
                is_fresh=False,
                observation_status=observation_status,
            )

        error_keywords = (
            "earlier than",
            "cannot be earlier",
            "must be after",
            "must be greater",
            "end date before start",
            "invalid interval",
            "invalid date range",
            "invalid date",
        )
        valid_keywords = ("dates are valid", "valid period", "success", "input accepted")

        if case.expected_valid:
            # Case expects VALID input interval:
            # 1. Reject if feedback visible contains explicit constraint error
            if feedback_visible and any(kw in text_lower for kw in error_keywords):
                return ValidationTestCaseResult(
                    case=case,
                    actual_feedback_text=clean_text,
                    actual_feedback_visible=feedback_visible,
                    passed=False,
                    failure_reason="Validator incorrectly rejected valid input with an error constraint message",
                )

            # 2. If positive expected substring declared, require it
            if case.expected_feedback_substring:
                if feedback_visible and case.expected_feedback_substring.lower() in text_lower:
                    return ValidationTestCaseResult(
                        case=case,
                        actual_feedback_text=clean_text,
                        actual_feedback_visible=feedback_visible,
                        passed=True,
                    )
                else:
                    return ValidationTestCaseResult(
                        case=case,
                        actual_feedback_text=clean_text,
                        actual_feedback_visible=feedback_visible,
                        passed=False,
                        failure_reason=f"Ambiguous or unexpected feedback for valid input; expected '{case.expected_feedback_substring}', got: '{clean_text}'",
                    )

            # 3. If expected absent, require feedback to be hidden or empty
            if case.expected_feedback_absent:
                if not feedback_visible or not clean_text:
                    return ValidationTestCaseResult(
                        case=case,
                        actual_feedback_text=clean_text,
                        actual_feedback_visible=feedback_visible,
                        passed=True,
                    )
                else:
                    return ValidationTestCaseResult(
                        case=case,
                        actual_feedback_text=clean_text,
                        actual_feedback_visible=feedback_visible,
                        passed=False,
                        failure_reason=f"Expected feedback to be absent for valid input, but visible feedback was observed: '{clean_text}'",
                    )

            # 4. Default valid case requires absence of visible error feedback
            if not feedback_visible or not clean_text:
                return ValidationTestCaseResult(
                    case=case,
                    actual_feedback_text=clean_text,
                    actual_feedback_visible=feedback_visible,
                    passed=True,
                )

            # Arbitrary visible text with no expected substring is ambiguous
            return ValidationTestCaseResult(
                case=case,
                actual_feedback_text=clean_text,
                actual_feedback_visible=feedback_visible,
                passed=False,
                failure_reason=f"Ambiguous visible feedback for valid input without declared expected substring: '{clean_text}'",
            )

        else:
            # Case expects INVALID input interval:
            # 1. Reject if feedback is hidden OR explicitly claims valid
            if (not feedback_visible) or any(kw in text_lower for kw in valid_keywords):
                return ValidationTestCaseResult(
                    case=case,
                    actual_feedback_text=clean_text,
                    actual_feedback_visible=feedback_visible,
                    passed=False,
                    failure_reason="Validator accepted invalid input without displaying error constraint message",
                )

            # 2. If positive expected substring declared, require it
            if case.expected_feedback_substring:
                if case.expected_feedback_substring.lower() in text_lower:
                    return ValidationTestCaseResult(
                        case=case,
                        actual_feedback_text=clean_text,
                        actual_feedback_visible=feedback_visible,
                        passed=True,
                    )
                else:
                    return ValidationTestCaseResult(
                        case=case,
                        actual_feedback_text=clean_text,
                        actual_feedback_visible=feedback_visible,
                        passed=False,
                        failure_reason=f"Ambiguous or irrelevant feedback for invalid input; expected '{case.expected_feedback_substring}', got: '{clean_text}'",
                    )

            # 3. Require positive constraint error evidence if no expected substring declared
            if any(kw in text_lower for kw in error_keywords):
                return ValidationTestCaseResult(
                    case=case,
                    actual_feedback_text=clean_text,
                    actual_feedback_visible=feedback_visible,
                    passed=True,
                )

            # Arbitrary visible feedback is insufficient
            return ValidationTestCaseResult(
                case=case,
                actual_feedback_text=clean_text,
                actual_feedback_visible=feedback_visible,
                passed=False,
                failure_reason=f"Feedback does not provide positive constraint error evidence for invalid input: '{clean_text}'",
            )

    def evaluate_observations(
        self,
        case_observations: List[Any],
    ) -> ValidationContractResult:
        """Evaluate a sequence of test case observations against this contract.
        
        Enforces complete coverage:
        - Empty observations cannot satisfy contract (returns INCOMPLETE_COVERAGE).
        - Every declared case must have an observation.
        - Duplicate or unrelated observations are rejected.
        - Unconfirmed or stale observations fail closed as INCOMPLETE_COVERAGE.
        """
        if not case_observations:
            return ValidationContractResult(
                contract_id=self.contract_id,
                rule_name=self.rule_name,
                verdict=ValidationVerdict.INCOMPLETE_COVERAGE,
                all_passed=False,
                is_customer_defect=False,
                defect_description="No test case observations provided; empty observation cannot satisfy validation contract",
                case_results=(),
                tested_cases_count=0,
                contract=self,
            )

        observed_case_ids = [obs[0].case_id for obs in case_observations]
        declared_case_ids = [c.case_id for c in self.test_cases]

        # Duplicate check
        if len(observed_case_ids) != len(set(observed_case_ids)):
            return ValidationContractResult(
                contract_id=self.contract_id,
                rule_name=self.rule_name,
                verdict=ValidationVerdict.INCOMPLETE_COVERAGE,
                all_passed=False,
                is_customer_defect=False,
                defect_description="Duplicate observations received for validation test cases",
                case_results=(),
                tested_cases_count=len(case_observations),
                contract=self,
            )

        # Missing check
        missing_case_ids = set(declared_case_ids) - set(observed_case_ids)
        if missing_case_ids:
            return ValidationContractResult(
                contract_id=self.contract_id,
                rule_name=self.rule_name,
                verdict=ValidationVerdict.INCOMPLETE_COVERAGE,
                all_passed=False,
                is_customer_defect=False,
                defect_description=f"Incomplete contract case coverage: missing cases {sorted(list(missing_case_ids))}",
                case_results=(),
                tested_cases_count=len(case_observations),
                contract=self,
            )

        # Unrelated check
        unrelated_case_ids = set(observed_case_ids) - set(declared_case_ids)
        if unrelated_case_ids:
            return ValidationContractResult(
                contract_id=self.contract_id,
                rule_name=self.rule_name,
                verdict=ValidationVerdict.INCOMPLETE_COVERAGE,
                all_passed=False,
                is_customer_defect=False,
                defect_description=f"Unrelated observations received for undeclared cases {sorted(list(unrelated_case_ids))}",
                case_results=(),
                tested_cases_count=len(case_observations),
                contract=self,
            )

        results: List[ValidationTestCaseResult] = []
        for obs in case_observations:
            if len(obs) >= 5:
                case, vis, txt, is_fresh, obs_status = obs[0], obs[1], obs[2], obs[3], obs[4]
            elif len(obs) == 4:
                case, vis, txt, is_fresh = obs[0], obs[1], obs[2], obs[3]
                obs_status = "COMPLETED" if is_fresh else "STALE_FEEDBACK"
            else:
                case, vis, txt = obs[0], obs[1], obs[2]
                is_fresh = True
                obs_status = "COMPLETED"
            res = self.evaluate_case(case, vis, txt, is_fresh=is_fresh, observation_status=obs_status)
            results.append(res)

        # Check for unconfirmed/stale observations: never evaluate as functional PASS or customer defect
        if any(not r.is_fresh for r in results):
            unfresh_r = next(r for r in results if not r.is_fresh)
            return ValidationContractResult(
                contract_id=self.contract_id,
                rule_name=self.rule_name,
                verdict=ValidationVerdict.INCOMPLETE_COVERAGE,
                all_passed=False,
                is_customer_defect=False,
                defect_description=f"Fresh validation evidence could not be established for rule '{self.rule_name}': {unfresh_r.failure_reason}",
                case_results=tuple(results),
                tested_cases_count=len(results),
                contract=self,
            )

        all_passed = all(r.passed for r in results) and len(results) == len(self.test_cases)
        is_defect = False
        defect_desc = None
        verdict = ValidationVerdict.SATISFIED if all_passed else ValidationVerdict.AMBIGUOUS_FEEDBACK

        if not all_passed:
            # Check for customer defects
            for r in results:
                if not r.passed:
                    reason = r.failure_reason or ""
                    if "accepted invalid" in reason.lower():
                        verdict = ValidationVerdict.DEFECT_ACCEPTED_INVALID
                        is_defect = True
                        defect_desc = f"Validator defect in rule '{self.rule_name}': accepted invalid input ({r.case.description}) without error feedback"
                        break
                    elif "incorrectly rejected" in reason.lower():
                        verdict = ValidationVerdict.DEFECT_REJECTED_VALID
                        is_defect = True
                        defect_desc = f"Validator defect in rule '{self.rule_name}': incorrectly rejected valid input ({r.case.description}) with error message: '{r.actual_feedback_text}'"
                        break
                    elif "ambiguous" in reason.lower() or "irrelevant" in reason.lower() or "unexpected" in reason.lower():
                        verdict = ValidationVerdict.AMBIGUOUS_FEEDBACK
                        defect_desc = f"Validation outcome ambiguous for rule '{self.rule_name}': {reason}"
                        break
                    else:
                        verdict = ValidationVerdict.AMBIGUOUS_FEEDBACK
                        defect_desc = f"Validation outcome ambiguous: {reason}"
                        break

        return ValidationContractResult(
            contract_id=self.contract_id,
            rule_name=self.rule_name,
            verdict=verdict,
            all_passed=all_passed,
            is_customer_defect=is_defect,
            defect_description=defect_desc,
            case_results=tuple(results),
            tested_cases_count=len(results),
            contract=self,
        )


# ============================================================================
# Reusable Contract Definitions (Independent of Global Target Bindings)
# ============================================================================

WORK_HISTORY_DATE_CONTRACT = ValidationContract(
    contract_id="work_history_date_interval",
    rule_name="Work History Date Interval (End >= Start)",
    trigger_control_id="btn-validate-history",
    field_selectors={
        "start_date": "#input-start-date",
        "end_date": "#input-end-date",
    },
    feedback_selector="#history-date-feedback",
    completion_marker_attr="data-validation-case",
    completion_marker_type="case_id",
    completion_timestamp_attr="data-validation-timestamp",
    completion_mode="case_marker",
    test_cases=(
        ValidationTestCase(
            case_id="case_valid_interval",
            description="Valid interval (Start 2022-01-01, End 2024-05-01)",
            input_values={
                "start_date": "2022-01-01",
                "end_date": "2024-05-01",
            },
            expected_valid=True,
            expected_feedback_substring="Dates are valid.",
        ),
        ValidationTestCase(
            case_id="case_invalid_interval",
            description="Invalid interval (Start 2024-05-01, End 2022-01-01)",
            input_values={
                "start_date": "2024-05-01",
                "end_date": "2022-01-01",
            },
            expected_valid=False,
            expected_feedback_substring="End date cannot be earlier than start date.",
        ),
    ),
)

VARIED_WORK_HISTORY_DATE_CONTRACT = ValidationContract(
    contract_id="varied_work_history_date_interval",
    rule_name="Period Dates Interval (End >= Start)",
    trigger_control_id=None,  # Neutral/anonymous ID control
    trigger_name="Validate Period Dates",
    field_selectors={
        "start_date": "#v-start-date",
        "end_date": "#v-end-date",
    },
    feedback_selector="#v-date-feedback",
    completion_marker_attr="data-validation-case",
    completion_marker_type="case_id",
    completion_timestamp_attr="data-validation-timestamp",
    completion_mode="case_marker",
    test_cases=(
        ValidationTestCase(
            case_id="v_case_valid",
            description="Valid interval (Start 2022-01-01, End 2024-05-01)",
            input_values={
                "start_date": "2022-01-01",
                "end_date": "2024-05-01",
            },
            expected_valid=True,
            expected_feedback_substring="Dates are valid.",
        ),
        ValidationTestCase(
            case_id="v_case_invalid",
            description="Invalid interval (Start 2024-05-01, End 2022-01-01)",
            input_values={
                "start_date": "2024-05-01",
                "end_date": "2022-01-01",
            },
            expected_valid=False,
            expected_feedback_substring="End date cannot be earlier than start date.",
        ),
    ),
)


# ============================================================================
# Target-Scoped Explicit Contract Registration Registry
# ============================================================================

@dataclass
class ValidationContractBinding:
    contract: ValidationContract
    target_url_pattern: Optional[str] = None
    target_control_id: Optional[str] = None
    trigger_selector: Optional[str] = None
    accessible_name: Optional[str] = None
    feedback_selector: Optional[str] = None
    project_id: Optional[int] = None


# Explicit in-memory registry of target/project-scoped bindings.
# Starts empty in production; contracts are registered explicitly.
_REGISTERED_BINDINGS: List[ValidationContractBinding] = []


def _is_same_binding_target(b1: ValidationContractBinding, b2: ValidationContractBinding) -> bool:
    if b1.project_id != b2.project_id:
        return False
    if b1.target_url_pattern != b2.target_url_pattern:
        return False
    # Same project scope and same target URL pattern:
    if b1.contract.contract_id == b2.contract.contract_id:
        return True
    if b1.target_control_id and b2.target_control_id and b1.target_control_id == b2.target_control_id:
        return True
    if (
        b1.accessible_name
        and b2.accessible_name
        and b1.accessible_name.lower().strip() == b2.accessible_name.lower().strip()
    ):
        return True
    return False


def register_validation_contract_binding(binding: ValidationContractBinding) -> None:
    global _REGISTERED_BINDINGS
    _REGISTERED_BINDINGS = [
        b for b in _REGISTERED_BINDINGS
        if not _is_same_binding_target(b, binding)
    ]
    _REGISTERED_BINDINGS.insert(0, binding)


def register_validation_contract(
    contract: ValidationContract,
    target_url_pattern: Optional[str] = None,
    target_control_id: Optional[str] = None,
    trigger_selector: Optional[str] = None,
    accessible_name: Optional[str] = None,
    feedback_selector: Optional[str] = None,
    project_id: Optional[int] = None,
) -> ValidationContractBinding:
    binding = ValidationContractBinding(
        contract=contract,
        target_url_pattern=target_url_pattern or contract.target_url_pattern,
        target_control_id=target_control_id or contract.trigger_control_id,
        trigger_selector=trigger_selector or contract.trigger_selector,
        accessible_name=accessible_name or contract.trigger_name,
        feedback_selector=feedback_selector or contract.feedback_selector,
        project_id=project_id,
    )
    register_validation_contract_binding(binding)
    return binding


def clear_validation_contracts() -> None:
    """Clear all registered contract bindings (used for test isolation)."""
    _REGISTERED_BINDINGS.clear()


def prepare_bindings_after_project_delete(project_id: int) -> list:
    return [b for b in _REGISTERED_BINDINGS if b.project_id != project_id]


def publish_prepared_bindings(bindings: list) -> None:
    global _REGISTERED_BINDINGS
    _REGISTERED_BINDINGS = bindings


def unregister_validation_contract(contract_id: str, project_id: Optional[int] = None) -> None:
    global _REGISTERED_BINDINGS
    if project_id is not None:
        _REGISTERED_BINDINGS = [
            b for b in _REGISTERED_BINDINGS
            if not (b.contract.contract_id == contract_id and b.project_id == project_id)
        ]
    else:
        _REGISTERED_BINDINGS = [b for b in _REGISTERED_BINDINGS if b.contract.contract_id != contract_id]


def _matches_url(pattern: Optional[str], url: Optional[str]) -> bool:
    if not pattern or pattern == "*":
        return True
    if not url:
        return False
    if fnmatch.fnmatch(url, pattern):
        return True
    base_url = url.split("?")[0].split("#")[0]
    if fnmatch.fnmatch(base_url, pattern):
        return True
    return pattern == url or pattern == base_url


def _matches_binding(
    binding: ValidationContractBinding,
    control_id: str,
    raw_id: str,
    accessible_name: Optional[str],
    dom_attributes: Optional[Dict[str, Any]],
    target_url: Optional[str],
) -> bool:
    # Target URL scope isolation (unrelated targets with coincidentally matching IDs do NOT match)
    if binding.target_url_pattern is not None:
        if not target_url or not _matches_url(binding.target_url_pattern, target_url):
            return False

    # Match control_id
    if binding.target_control_id:
        raw_target_id = (
            binding.target_control_id.split(":", 1)[-1]
            if ":" in binding.target_control_id
            else binding.target_control_id
        )
        if control_id == binding.target_control_id or raw_id == raw_target_id:
            return True

    # Match accessible name (for neutral/auto-ID controls)
    if binding.accessible_name and accessible_name:
        if binding.accessible_name.lower().strip() == accessible_name.lower().strip():
            return True
        if binding.accessible_name.lower() in accessible_name.lower():
            return True

    # Match trigger_selector or DOM attributes
    if dom_attributes:
        controls_id = dom_attributes.get("controlsId") or dom_attributes.get("ariaControls")
        if controls_id and binding.feedback_selector:
            raw_controls_id = controls_id.split(":", 1)[-1]
            clean_fb_sel = binding.feedback_selector.lstrip("#")
            if raw_controls_id == clean_fb_sel:
                return True

        tag_id = dom_attributes.get("id")
        if tag_id and binding.target_control_id:
            raw_tag_id = tag_id.split(":", 1)[-1]
            raw_b_id = (
                binding.target_control_id.split(":", 1)[-1]
                if ":" in binding.target_control_id
                else binding.target_control_id
            )
            if raw_tag_id == raw_b_id:
                return True

    return False


def find_validation_contract_for_control(
    control_id: str,
    accessible_name: Optional[str] = None,
    dom_attributes: Optional[Dict[str, Any]] = None,
    target_url: Optional[str] = None,
    project_id: Optional[int] = None,
) -> Optional[ValidationContract]:
    """Look up an explicit validation contract by target-scoped binding and control identity.
    
    Ensures target isolation:
    - Rejects project-bound bindings when project context is missing or different.
    - Two projects using the same URL and control identity cannot consume each other's contracts.
    - Target-scoped test bindings (binding.project_id is None) are preserved as fallbacks.
    """
    raw_id = control_id.split(":", 1)[-1] if ":" in control_id else control_id

    # Pass 1: Project-bound bindings (highest priority, strictly isolated to project_id)
    if project_id is not None:
        for binding in _REGISTERED_BINDINGS:
            if binding.project_id == project_id or (binding.project_id is not None and str(binding.project_id) == str(project_id)):
                if _matches_binding(binding, control_id, raw_id, accessible_name, dom_attributes, target_url):
                    return binding.contract

    # Pass 2: Target-scoped fixture bindings (binding.project_id is None)
    # Project-bound bindings (binding.project_id is not None) are strictly excluded here
    for binding in _REGISTERED_BINDINGS:
        if binding.project_id is None:
            if _matches_binding(binding, control_id, raw_id, accessible_name, dom_attributes, target_url):
                return binding.contract

    return None


def register_profile_fixture_contracts(origin_or_pattern: Optional[str] = None) -> None:
    """Explicitly register validation contract bindings for localhost profile test fixtures.
    
    Keeps fixture bindings scoped to profile fixture URLs. Demonstrates explicit bindings
    with neutral/generated IDs and isolation from unrelated targets.
    """
    if origin_or_pattern and "://" in origin_or_pattern:
        prefix = origin_or_pattern.rstrip("/")
        register_validation_contract(
            contract=WORK_HISTORY_DATE_CONTRACT,
            target_url_pattern=f"{prefix}/profile",
            target_control_id="btn-validate-history",
            accessible_name="Validate Work History Dates",
            feedback_selector="#history-date-feedback",
        )
        register_validation_contract(
            contract=WORK_HISTORY_DATE_CONTRACT,
            target_url_pattern=f"{prefix}/profile-validation-variant*",
            target_control_id="btn-validate-history",
            accessible_name="Validate Work History Dates",
            feedback_selector="#history-date-feedback",
        )
        register_validation_contract(
            contract=VARIED_WORK_HISTORY_DATE_CONTRACT,
            target_url_pattern=f"{prefix}/profile-varied*",
            target_control_id=None,
            accessible_name="Validate Period Dates",
            feedback_selector="#v-date-feedback",
        )
    else:
        pattern = origin_or_pattern or "*/profile"
        register_validation_contract(
            contract=WORK_HISTORY_DATE_CONTRACT,
            target_url_pattern=pattern,
            target_control_id="btn-validate-history",
            accessible_name="Validate Work History Dates",
            feedback_selector="#history-date-feedback",
        )
        register_validation_contract(
            contract=WORK_HISTORY_DATE_CONTRACT,
            target_url_pattern="*/profile-validation-variant*",
            target_control_id="btn-validate-history",
            accessible_name="Validate Work History Dates",
            feedback_selector="#history-date-feedback",
        )
        register_validation_contract(
            contract=VARIED_WORK_HISTORY_DATE_CONTRACT,
            target_url_pattern="*/profile-varied*",
            target_control_id=None,
            accessible_name="Validate Period Dates",
            feedback_selector="#v-date-feedback",
        )
