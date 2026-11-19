from app.qa.semantic_model import SemanticControl, SemanticState
from app.qa.state_fingerprint import CanonicalStateFingerprint

def test_deterministic_identical_input():
    c = SemanticControl("c1", "button", "Click", True, True, None, None, None, False)
    s1 = SemanticState("http://a.com", (c,))
    s2 = SemanticState("http://a.com", (c,))
    assert CanonicalStateFingerprint.generate(s1) == CanonicalStateFingerprint.generate(s2)

def test_telemetry_metadata_change():
    c1 = SemanticControl("c1", "div", "Tracking", True, True, None, None, None, False, volatile_metadata=("react-id-1",))
    c2 = SemanticControl("c2", "div", "Tracking", True, True, None, None, None, False, volatile_metadata=("react-id-2",))
    s1 = SemanticState("http://a.com", (c1,))
    s2 = SemanticState("http://a.com", (c2,))
    assert CanonicalStateFingerprint.generate(s1) == CanonicalStateFingerprint.generate(s2)
    norm = CanonicalStateFingerprint._normalize_state(s1)
    assert len(norm['controls']) == 1
    assert norm['controls'][0]['name'] == 'Tracking'

def test_removing_control_alters_fingerprint():
    c1 = SemanticControl("c1", "div", "Tracking", True, True, None, None, None, False)
    s1 = SemanticState("http://a.com", (c1,))
    s2 = SemanticState("http://a.com", ())
    assert CanonicalStateFingerprint.generate(s1) != CanonicalStateFingerprint.generate(s2)

def test_meaningful_visible_date_change():
    s1 = SemanticState("http://a.com", (), meaningful_dates=("2023-01-01",))
    s2 = SemanticState("http://a.com", (), meaningful_dates=("2023-01-02",))
    assert CanonicalStateFingerprint.generate(s1) != CanonicalStateFingerprint.generate(s2)

def test_meaningful_visible_identifier_change():
    c1 = SemanticControl("c1", "div", "User-1234", True, True, None, None, None, False)
    c2 = SemanticControl("c1", "div", "User-5678", True, True, None, None, None, False)
    s1 = SemanticState("http://a.com", (c1,))
    s2 = SemanticState("http://a.com", (c2,))
    assert CanonicalStateFingerprint.generate(s1) != CanonicalStateFingerprint.generate(s2)

def test_validation_state_change():
    s1 = SemanticState("http://a.com", (), validation_messages=("Error 1",))
    s2 = SemanticState("http://a.com", (), validation_messages=("Error 2",))
    assert CanonicalStateFingerprint.generate(s1) != CanonicalStateFingerprint.generate(s2)

def test_selected_state_change():
    c1 = SemanticControl("c1", "tab", "Tab", True, True, None, None, None, False, state_selected=True)
    c2 = SemanticControl("c1", "tab", "Tab", True, True, None, None, None, False, state_selected=False)
    s1 = SemanticState("http://a.com", (c1,))
    s2 = SemanticState("http://a.com", (c2,))
    assert CanonicalStateFingerprint.generate(s1) != CanonicalStateFingerprint.generate(s2)

def test_privacy_sentinel_absent():
    c1 = SemanticControl("c1", "input", "PRIVATE-PHASE-A1-SENTINEL", True, True, "PRIVATE-PHASE-A1-SENTINEL", None, None, True)
    s1 = SemanticState("http://a.com", (c1,))
    norm = CanonicalStateFingerprint._normalize_state(s1)
    import json
    assert "PRIVATE-PHASE-A1-SENTINEL" not in json.dumps(norm)
    assert norm['controls'][0]['name'] == "***"
    assert norm['controls'][0]['href'] == "***"
