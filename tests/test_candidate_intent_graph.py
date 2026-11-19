import pytest
from app.qa.semantic_model import SemanticControl, SemanticState
from app.qa.candidate_intent_graph import CandidateIntentGraph, CandidateActionIntent, StateNode
from app.qa.interaction_policy import RiskClassification

def create_state(url: str, name: str = "btn") -> SemanticState:
    c = SemanticControl(control_id="1", accessible_role="button", accessible_name=name, visibility=True, enabled=True, href=None, target=None, form_ownership=None, is_private=False)
    return SemanticState(url, (c,))

def test_intent_target_is_unresolved():
    intent = CandidateActionIntent("f1", "c1", "click", RiskClassification.SAFE_CANDIDATE)
    assert intent.target_state == "UNRESOLVED"
    assert not hasattr(intent, "execute")

    with pytest.raises(TypeError):
        CandidateActionIntent("f1", "c1", "click", RiskClassification.SAFE_CANDIDATE, target_state="RESOLVED")

def test_node_deduplication_and_budget():
    g = CandidateIntentGraph(max_nodes=2)
    s1 = create_state("http://a.com")
    s1_dup = create_state("http://a.com")
    s2 = create_state("http://b.com")
    s3 = create_state("http://c.com")

    assert g.add_state_node(s1) is True
    assert g.add_state_node(s1_dup) is True
    assert len(g.nodes) == 1

    assert g.add_state_node(s2) is True
    assert len(g.nodes) == 2
    assert g.exhausted_nodes is False

    assert g.add_state_node(s3) is False
    assert len(g.nodes) == 2
    assert g.exhausted_nodes is True

    # Confirm we cannot bypass limits by direct insertion
    with pytest.raises(TypeError):
        g.nodes["f4"] = StateNode("f4", s3)

def test_intent_deduplication_and_budget():
    g = CandidateIntentGraph(max_intents=2)
    i1 = CandidateActionIntent("f1", "c1", "click", RiskClassification.SAFE_CANDIDATE)
    i1_dup = CandidateActionIntent("f1", "c1", "click", RiskClassification.SAFE_CANDIDATE)
    i2 = CandidateActionIntent("f1", "c2", "click", RiskClassification.SAFE_CANDIDATE)
    i3 = CandidateActionIntent("f1", "c3", "click", RiskClassification.SAFE_CANDIDATE)

    assert g.add_intent(i1) is True
    assert g.add_intent(i1_dup) is True
    assert len(g.intents) == 1

    assert g.add_intent(i2) is True
    assert len(g.intents) == 2
    assert g.exhausted_intents is False

    assert g.add_intent(i3) is False
    assert len(g.intents) == 2
    assert g.exhausted_intents is True

    assert list(g.intents.keys()) == [i1, i2]
    assert len(g.get_all_intents()) == 2

    # Confirm we cannot bypass limits by direct insertion
    with pytest.raises(TypeError):
        g.intents[i3] = None

def test_privacy_sentinel_absent_from_graph():
    c = SemanticControl(
        control_id="1", accessible_role="button", accessible_name="PRIVATE-PHASE-A1-SENTINEL",
        visibility=True, enabled=True, href="PRIVATE-PHASE-A1-SENTINEL", target=None, form_ownership=None, is_private=True
    )
    s = SemanticState("http://a.com", (c,))
    g = CandidateIntentGraph()
    g.add_state_node(s)

    node = list(g.nodes.values())[0]
    assert "PRIVATE-PHASE-A1-SENTINEL" not in str(node.state)
    assert "PRIVATE-PHASE-A1-SENTINEL" not in repr(node)

    # 3. Privacy must remain intact across inspection views and intent representations
    assert "PRIVATE-PHASE-A1-SENTINEL" not in repr(g.nodes)
    assert "PRIVATE-PHASE-A1-SENTINEL" not in str(g)

    # Also verify intents don't leak it
    intent = CandidateActionIntent(g.nodes[node.fingerprint].fingerprint, "c1", "click", RiskClassification.SAFE_CANDIDATE)
    assert "PRIVATE-PHASE-A1-SENTINEL" not in repr(intent)
