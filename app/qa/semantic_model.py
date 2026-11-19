from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class SemanticControl:
    control_id: str
    accessible_role: str
    accessible_name: str
    visibility: bool
    enabled: bool
    href: Optional[str]
    target: Optional[str]
    form_ownership: Optional[str]
    is_private: bool
    volatile_metadata: tuple[str, ...] = ()
    parent_semantic_context: str = "body"
    state_expanded: Optional[bool] = None
    state_selected: Optional[bool] = None
    state_checked: Optional[bool] = None

    def __post_init__(self):
        if self.is_private:
            object.__setattr__(self, 'accessible_name', '***')
            if self.href:
                object.__setattr__(self, 'href', '***')

@dataclass(frozen=True)
class SemanticState:
    url: str
    controls: tuple[SemanticControl, ...]
    validation_messages: tuple[str, ...] = ()
    meaningful_dates: tuple[str, ...] = ()
