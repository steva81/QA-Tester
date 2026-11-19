from app.qa.semantic_model import SemanticControl
from app.qa.interaction_policy import InteractionSafetyPolicy, RiskClassification

def create_control(role, name, visibility=True, enabled=True, href=None, form=None, target=None):
    return SemanticControl(
        control_id="c1", accessible_role=role, accessible_name=name,
        visibility=visibility, enabled=enabled, href=href, target=target, form_ownership=form,
        is_private=False
    )

def test_unknown_fails_closed():
    c = create_control("unknown", "unknown")
    assert InteractionSafetyPolicy.classify(c) == RiskClassification.UNKNOWN

def test_destructive_prohibited():
    c = create_control("link", "Delete Account")
    assert InteractionSafetyPolicy.classify(c) == RiskClassification.PROHIBITED

def test_ambiguous_not_safe():
    c = create_control("button", "delete")
    assert InteractionSafetyPolicy.classify(c) == RiskClassification.POTENTIALLY_MUTATING

def test_form_owned_submit():
    c = create_control("button", "Submit", form="f1")
    assert InteractionSafetyPolicy.classify(c) == RiskClassification.POTENTIALLY_MUTATING

def test_observational_state():
    c = create_control("button", "Submit", enabled=False)
    assert InteractionSafetyPolicy.classify(c) == RiskClassification.OBSERVE_ONLY

def test_no_site_specific_hardcoding():
    c = create_control("button", "example.com magic button")
    assert InteractionSafetyPolicy.classify(c) == RiskClassification.SAFE_CANDIDATE

def test_href_adversarial_cases():
    cases = [
        ("javascript:void(0)", RiskClassification.UNKNOWN),
        ("javascript:location='foo'", RiskClassification.PROHIBITED),
        ("mailto:test@example.invalid", RiskClassification.UNKNOWN),
        ("tel:+123456", RiskClassification.UNKNOWN),
        ("data:text/html,<html>", RiskClassification.UNKNOWN),
        ("/delete", RiskClassification.PROHIBITED),
        ("/DELETE", RiskClassification.PROHIBITED),
        ("/%64elete", RiskClassification.PROHIBITED),
        ("/logout?next=/", RiskClassification.PROHIBITED),
        ("/profile#delete", RiskClassification.PROHIBITED),
        ("/update", RiskClassification.POTENTIALLY_MUTATING),
        ("http://example.com/ordinary", RiskClassification.SAFE_CANDIDATE),
        ("/ordinary", RiskClassification.SAFE_CANDIDATE),
        (None, RiskClassification.SAFE_CANDIDATE)
    ]
    for href, expected in cases:
        c = create_control("link", "Settings" if "delete" not in str(href) else "safe text", href=href)
        assert InteractionSafetyPolicy.classify(c) == expected, f"Failed on href: {href}"

def test_href_target_blank():
    c = create_control("link", "Settings", href="/ordinary", target="_blank")
    assert InteractionSafetyPolicy.classify(c) == RiskClassification.UNKNOWN
