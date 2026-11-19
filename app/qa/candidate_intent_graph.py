from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple
from app.qa.semantic_model import SemanticState
from app.qa.state_fingerprint import CanonicalStateFingerprint
from app.qa.interaction_policy import RiskClassification

@dataclass(frozen=True)
class CandidateActionIntent:
    parent_state_fingerprint: str
    control_fingerprint: str
    expected_action_type: str
    risk_classification: RiskClassification
    preceding_intents: Tuple[Any, ...] = ()
    target_state: str = field(init=False, default="UNRESOLVED")

@dataclass(frozen=True)
class StateNode:
    fingerprint: str
    state: SemanticState

class CandidateIntentGraph:
    def __init__(self, max_nodes: int = 50, max_intents: int = 100):
        self.max_nodes = max_nodes
        self.max_intents = max_intents
        self._nodes: Dict[str, StateNode] = {}
        self._intents: Dict[CandidateActionIntent, None] = {}
        self.exhausted_nodes = False
        self.exhausted_intents = False

    @property
    def nodes(self):
        from types import MappingProxyType
        return MappingProxyType(self._nodes)

    @property
    def intents(self):
        from types import MappingProxyType
        return MappingProxyType(self._intents)

    def add_state_node(self, state: SemanticState) -> bool:
        fingerprint = CanonicalStateFingerprint.generate(state)

        if fingerprint in self._nodes:
            return True

        if len(self._nodes) >= self.max_nodes:
            self.exhausted_nodes = True
            return False

        self._nodes[fingerprint] = StateNode(fingerprint, state)
        return True

    def add_intent(self, intent: CandidateActionIntent) -> bool:
        if intent in self._intents:
            return True

        if len(self._intents) >= self.max_intents:
            self.exhausted_intents = True
            return False

        self._intents[intent] = None
        return True

    def get_all_intents(self) -> List[CandidateActionIntent]:
        return list(self._intents.keys())
