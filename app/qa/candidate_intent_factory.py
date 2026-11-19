from dataclasses import dataclass
from types import MappingProxyType
from typing import Tuple, Optional, Mapping, List

from app.qa.semantic_model import SemanticControl, SemanticState
from app.qa.semantic_normalizer import SemanticNormalizationResult
from app.qa.state_fingerprint import CanonicalStateFingerprint
from app.qa.candidate_intent_graph import CandidateActionIntent
from app.qa.interaction_policy import InteractionSafetyPolicy, RiskClassification


@dataclass(frozen=True)
class CandidateIntentDerivationResult:
    intents: Tuple[CandidateActionIntent, ...]
    terminal_status: str
    limitations: Tuple[str, ...]


class CandidateActionIntentFactory:
    ROLE_ACTION_MAP: Mapping[str, str] = MappingProxyType({
        "button": "click",
        "link": "click",
        "tab": "click",
        "menuitem": "click",
        "combobox": "select",
        "checkbox": "toggle",
        "radio": "select",
        "textbox": "input",
    })

    _ALLOWED_RISKS = frozenset({
        RiskClassification.SAFE_CANDIDATE,
        RiskClassification.POTENTIALLY_MUTATING,
    })

    def __init__(self) -> None:
        pass

    @classmethod
    def derive(
        cls,
        normalization_result: SemanticNormalizationResult,
    ) -> CandidateIntentDerivationResult:
        return cls().derive_intents(normalization_result)

    def derive_intents(
        self,
        normalization_result: SemanticNormalizationResult,
    ) -> CandidateIntentDerivationResult:
        if not isinstance(normalization_result, SemanticNormalizationResult):
            raise TypeError(
                f"CandidateActionIntentFactory.derive_intents requires a SemanticNormalizationResult; "
                f"received {type(normalization_result)!r}. Bare SemanticState is rejected to prevent coverage erasure."
            )

        if normalization_result.semantic_state is None or normalization_result.terminal_status != "COMPLETE":
            return CandidateIntentDerivationResult(
                intents=(),
                terminal_status=normalization_result.terminal_status,
                limitations=normalization_result.limitations,
            )

        intents = self._derive_control_intents(normalization_result.semantic_state)
        return CandidateIntentDerivationResult(
            intents=tuple(intents),
            terminal_status=normalization_result.terminal_status,
            limitations=normalization_result.limitations,
        )

    @classmethod
    def _action_type_for_role(cls, role: Optional[str]) -> Optional[str]:
        if not role:
            return None
        return cls.ROLE_ACTION_MAP.get(role)

    def _derive_control_intents(self, state: SemanticState) -> Tuple[CandidateActionIntent, ...]:
        parent_fingerprint = CanonicalStateFingerprint.generate(state)
        intents: List[CandidateActionIntent] = []

        for control in state.controls:
            # 1. Visibility and enabled gating
            if not control.visibility or not control.enabled:
                continue

            # 2. Authoritative safety policy classification (never overridable)
            risk = InteractionSafetyPolicy.classify(control)
            if risk not in self._ALLOWED_RISKS:
                continue

            # 3. Deterministic action mapping based on semantic evidence (fail-closed, no default fallback)
            action_type = self._action_type_for_role(control.accessible_role)
            if action_type is None:
                continue

            # 4. Construct pure unexecuted CandidateActionIntent
            intent = CandidateActionIntent(
                parent_state_fingerprint=parent_fingerprint,
                control_fingerprint=control.control_id,
                expected_action_type=action_type,
                risk_classification=risk,
            )
            intents.append(intent)

        return tuple(intents)
