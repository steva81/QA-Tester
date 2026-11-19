import pytest
from app.qa.semantic_model import SemanticControl, SemanticState

def test_semantic_control_immutable():
    control = SemanticControl(
        control_id="c1", accessible_role="button", accessible_name="Submit",
        visibility=True, enabled=True, href=None, target=None, form_ownership=None,
        is_private=False
    )
    with pytest.raises(Exception):
        control.accessible_name = "Hack"

def test_hidden_and_disabled_control_representation():
    control = SemanticControl(
        control_id="c1", accessible_role="button", accessible_name="Submit",
        visibility=False, enabled=False, href=None, target=None, form_ownership=None,
        is_private=False
    )
    assert not control.visibility
    assert not control.enabled

def test_semantic_parent_context():
    control = SemanticControl(
        control_id="c1", accessible_role="button", accessible_name="Submit",
        visibility=True, enabled=True, href=None, target=None, form_ownership=None,
        is_private=False, parent_semantic_context="dialog"
    )
    assert control.parent_semantic_context == "dialog"

def test_privacy_sentinel_absent_from_repr_and_state():
    control = SemanticControl(
        control_id="c1", accessible_role="input", accessible_name="PRIVATE-PHASE-A1-SENTINEL",
        visibility=True, enabled=True, href="PRIVATE-PHASE-A1-SENTINEL", target=None, form_ownership=None,
        is_private=True
    )
    state = SemanticState("a", (control,))

    assert "PRIVATE-PHASE-A1-SENTINEL" not in repr(control)
    assert "PRIVATE-PHASE-A1-SENTINEL" not in repr(state)
    assert "PRIVATE-PHASE-A1-SENTINEL" not in str(control)
    assert "PRIVATE-PHASE-A1-SENTINEL" not in str(state)
    assert control.accessible_name == "***"
    assert control.href == "***"
