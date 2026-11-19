import hashlib
import json
from typing import Any, Dict
from app.qa.semantic_model import SemanticState

class CanonicalStateFingerprint:
    @staticmethod
    def _normalize_state(state: SemanticState) -> Dict[str, Any]:
        controls = []
        for c in state.controls:
            c_dict = {
                "role": c.accessible_role,
                "name": c.accessible_name,
                "visibility": c.visibility,
                "enabled": c.enabled,
                "href": c.href,
                "expanded": c.state_expanded,
                "selected": c.state_selected,
                "checked": c.state_checked,
                "parent_context": c.parent_semantic_context
            }
            controls.append(c_dict)

        controls.sort(key=lambda x: json.dumps(x, sort_keys=True))

        return {
            "url": state.url,
            "controls": controls,
            "validation_messages": sorted(list(state.validation_messages)),
            "meaningful_dates": sorted(list(state.meaningful_dates))
        }

    @staticmethod
    def generate(state: SemanticState) -> str:
        normalized = CanonicalStateFingerprint._normalize_state(state)
        encoded = json.dumps(normalized, sort_keys=True).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()
