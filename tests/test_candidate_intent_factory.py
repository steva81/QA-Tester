from dataclasses import FrozenInstanceError
import pytest
import subprocess
import sys
import os
import ast

from app.qa.semantic_model import SemanticControl, SemanticState
from app.qa.semantic_normalizer import SemanticNormalizationResult
from app.qa.state_fingerprint import CanonicalStateFingerprint
from app.qa.candidate_intent_graph import CandidateActionIntent, CandidateIntentGraph
from app.qa.interaction_policy import InteractionSafetyPolicy, RiskClassification
from app.qa.candidate_intent_factory import (
    CandidateActionIntentFactory,
    CandidateIntentDerivationResult,
)


def _make_control(
    control_id: str = "0:c1",
    accessible_role: str = "button",
    accessible_name: str = "Save",
    visibility: bool = True,
    enabled: bool = True,
    href: str = None,
    target: str = None,
    form_ownership: str = None,
    is_private: bool = False,
    volatile_metadata: tuple = (),
    parent_semantic_context: str = "body",
    state_expanded: bool = None,
    state_selected: bool = None,
    state_checked: bool = None,
) -> SemanticControl:
    return SemanticControl(
        control_id=control_id,
        accessible_role=accessible_role,
        accessible_name=accessible_name,
        visibility=visibility,
        enabled=enabled,
        href=href,
        target=target,
        form_ownership=form_ownership,
        is_private=is_private,
        volatile_metadata=volatile_metadata,
        parent_semantic_context=parent_semantic_context,
        state_expanded=state_expanded,
        state_selected=state_selected,
        state_checked=state_checked,
    )


def _make_state(controls: tuple, url: str = "http://example.com") -> SemanticState:
    return SemanticState(url=url, controls=controls)


def _make_norm_result(
    state: SemanticState = None,
    terminal_status: str = "COMPLETE",
    limitations: tuple = (),
) -> SemanticNormalizationResult:
    return SemanticNormalizationResult(
        semantic_state=state,
        terminal_status=terminal_status,
        limitations=limitations,
    )


# 1. Complete semantic result -> deterministic candidate intents
def test_complete_result_to_deterministic_candidate_intents():
    factory = CandidateActionIntentFactory()
    c1 = _make_control(control_id="0:c1", accessible_role="button", accessible_name="View Application")
    state = _make_state((c1,))
    norm_res = _make_norm_result(state)

    res = factory.derive_intents(norm_res)
    assert isinstance(res, CandidateIntentDerivationResult)
    assert res.terminal_status == "COMPLETE"
    assert res.limitations == ()
    assert len(res.intents) == 1

    intent = res.intents[0]
    expected_fp = CanonicalStateFingerprint.generate(state)
    assert intent.parent_state_fingerprint == expected_fp
    assert intent.control_fingerprint == "0:c1"
    assert intent.expected_action_type == "click"
    assert intent.risk_classification == RiskClassification.SAFE_CANDIDATE
    assert intent.target_state == "UNRESOLVED"


# 2. Terminal semantic_state=None -> zero intents for non-complete statuses
@pytest.mark.parametrize(
    "status",
    [
        "CANCELLED",
        "TARGET_CLOSED",
        "FRAME_DETACHED",
        "NAVIGATION_RACE",
        "NOT_ELIGIBLE",
        "WRONG_CONTEXT",
        "MANUAL_PAGE_EXCLUDED",
        "OPAQUE_ORIGIN",
    ],
)
def test_terminal_non_complete_produces_zero_intents_and_preserves_status(status):
    factory = CandidateActionIntentFactory()
    norm_res = _make_norm_result(state=None, terminal_status=status, limitations=("SOME_LIMIT",))

    res = factory.derive_intents(norm_res)
    assert res.terminal_status == status
    assert res.limitations == ("SOME_LIMIT",)
    assert res.intents == ()
    assert len(res.intents) == 0


# 3. Limitations preserved (LIMITED_CONTROL_BUDGET, LIMITED_FRAME_BUDGET, CROSS_ORIGIN_LIMITED, multiple)
@pytest.mark.parametrize(
    "limits",
    [
        ("LIMITED_CONTROL_BUDGET",),
        ("LIMITED_FRAME_BUDGET",),
        ("CROSS_ORIGIN_LIMITED",),
        ("LIMITED_CONTROL_BUDGET", "LIMITED_FRAME_BUDGET", "CROSS_ORIGIN_LIMITED"),
    ],
)
def test_limitations_preserved_in_derivation(limits):
    factory = CandidateActionIntentFactory()
    c1 = _make_control(control_id="0:btn", accessible_role="button", accessible_name="Action")
    state = _make_state((c1,))
    norm_res = _make_norm_result(state=state, limitations=limits)

    res = factory.derive_intents(norm_res)
    assert res.limitations == limits
    assert len(res.intents) == 1


# 4. Visible enabled button
def test_visible_enabled_button():
    factory = CandidateActionIntentFactory()
    btn = _make_control(control_id="0:b1", accessible_role="button", accessible_name="Confirm")
    state = _make_state((btn,))

    res = factory.derive_intents(_make_norm_result(state))
    assert len(res.intents) == 1
    assert res.intents[0].expected_action_type == "click"
    assert res.intents[0].control_fingerprint == "0:b1"
    assert res.intents[0].risk_classification == RiskClassification.SAFE_CANDIDATE


# 5. Link
def test_visible_enabled_link():
    factory = CandidateActionIntentFactory()
    link = _make_control(control_id="0:l1", accessible_role="link", accessible_name="Documentation", href="/docs")
    state = _make_state((link,))

    res = factory.derive_intents(_make_norm_result(state))
    assert len(res.intents) == 1
    assert res.intents[0].expected_action_type == "click"
    assert res.intents[0].control_fingerprint == "0:l1"
    assert res.intents[0].risk_classification == RiskClassification.SAFE_CANDIDATE


# 6. Checkbox behavior under real policy
def test_checkbox_behavior_under_real_policy():
    # Under real InteractionSafetyPolicy, checkbox role is unhandled and fails closed as UNKNOWN -> 0 intents
    factory = CandidateActionIntentFactory()
    cb = _make_control(control_id="0:cb1", accessible_role="checkbox", accessible_name="I agree")
    state = _make_state((cb,))

    res = factory.derive_intents(_make_norm_result(state))
    assert len(res.intents) == 0


# 7. Radio behavior under real policy
def test_radio_behavior_under_real_policy():
    # Under real InteractionSafetyPolicy, radio role is unhandled and fails closed as UNKNOWN -> 0 intents
    factory = CandidateActionIntentFactory()
    r = _make_control(control_id="0:r1", accessible_role="radio", accessible_name="Option A")
    state = _make_state((r,))

    res = factory.derive_intents(_make_norm_result(state))
    assert len(res.intents) == 0


# 8. Combobox if supported by existing policy
def test_combobox_supported_by_existing_policy():
    factory = CandidateActionIntentFactory()
    combo = _make_control(control_id="0:c1", accessible_role="combobox", accessible_name="Select Country")
    state = _make_state((combo,))

    assert InteractionSafetyPolicy.classify(combo) == RiskClassification.SAFE_CANDIDATE
    res = factory.derive_intents(_make_norm_result(state))
    assert len(res.intents) == 1
    assert res.intents[0].expected_action_type == "select"
    assert res.intents[0].risk_classification == RiskClassification.SAFE_CANDIDATE


# 9. Textbox behavior according to existing safety policy
def test_textbox_behavior_according_to_existing_safety_policy():
    factory = CandidateActionIntentFactory()
    tb = _make_control(control_id="0:tb1", accessible_role="textbox", accessible_name="Username")
    state = _make_state((tb,))

    # Real policy fails closed on textbox -> 0 intents
    assert InteractionSafetyPolicy.classify(tb) == RiskClassification.UNKNOWN
    res = factory.derive_intents(_make_norm_result(state))
    assert len(res.intents) == 0


# 10. Hidden control gating
def test_hidden_control_gating():
    factory = CandidateActionIntentFactory()
    hidden_btn = _make_control(control_id="0:h1", accessible_role="button", accessible_name="Hidden", visibility=False)
    state = _make_state((hidden_btn,))

    res = factory.derive_intents(_make_norm_result(state))
    assert len(res.intents) == 0


# 11. Disabled control gating
def test_disabled_control_gating():
    factory = CandidateActionIntentFactory()
    disabled_btn = _make_control(control_id="0:d1", accessible_role="button", accessible_name="Disabled", enabled=False)
    state = _make_state((disabled_btn,))

    res = factory.derive_intents(_make_norm_result(state))
    assert len(res.intents) == 0


# 12. Private control and sentinel absence
def test_private_control_and_private_sentinel_absence():
    factory = CandidateActionIntentFactory()

    # A private button (no href)
    priv_btn = _make_control(
        control_id="0:priv_btn",
        accessible_role="button",
        accessible_name="PRIVATE-PHASE-A4-SENTINEL",
        is_private=True,
    )
    assert priv_btn.accessible_name == "***"

    # A private link
    priv_link = _make_control(
        control_id="0:priv_link",
        accessible_role="link",
        accessible_name="PRIVATE-PHASE-A4-SENTINEL",
        href="PRIVATE-PHASE-A4-SENTINEL",
        is_private=True,
    )
    assert priv_link.accessible_name == "***"
    assert priv_link.href == "***"

    state = _make_state((priv_btn, priv_link))
    res = factory.derive_intents(_make_norm_result(state))
    assert len(res.intents) == 2

    for intent in res.intents:
        # Verify no sentinel in intent repr, str, or fields
        assert "PRIVATE-PHASE-A4-SENTINEL" not in repr(intent)
        assert "PRIVATE-PHASE-A4-SENTINEL" not in str(intent)
        assert "PRIVATE-PHASE-A4-SENTINEL" not in intent.control_fingerprint
        assert "PRIVATE-PHASE-A4-SENTINEL" not in intent.parent_state_fingerprint

    # A private textbox (e.g. password field) excluded by policy
    priv_tb = _make_control(
        control_id="0:pwd",
        accessible_role="textbox",
        accessible_name="PRIVATE-PHASE-A4-SENTINEL",
        is_private=True,
    )
    state_tb = _make_state((priv_tb,))
    res_tb = factory.derive_intents(_make_norm_result(state_tb))
    # Real policy fails closed on textbox -> 0 intents, without customer defect
    assert len(res_tb.intents) == 0


# 13. Duplicate same-label controls remain distinct
def test_duplicate_same_label_controls_remain_distinct():
    factory = CandidateActionIntentFactory()
    c1 = _make_control(control_id="0:btn1", accessible_role="button", accessible_name="Submit")
    c2 = _make_control(control_id="0:btn2", accessible_role="button", accessible_name="Submit")
    state = _make_state((c1, c2))

    res = factory.derive_intents(_make_norm_result(state))
    assert len(res.intents) == 2
    assert res.intents[0].control_fingerprint == "0:btn1"
    assert res.intents[1].control_fingerprint == "0:btn2"
    assert res.intents[0].control_fingerprint != res.intents[1].control_fingerprint


# 14. Same role/name across frames remain distinct
def test_same_role_and_name_across_frames_remain_distinct():
    factory = CandidateActionIntentFactory()
    c_main = _make_control(control_id="0:btn", accessible_role="button", accessible_name="Click")
    c_frame = _make_control(control_id="0.0:btn", accessible_role="button", accessible_name="Click")
    state = _make_state((c_main, c_frame))

    res = factory.derive_intents(_make_norm_result(state))
    assert len(res.intents) == 2
    assert res.intents[0].control_fingerprint == "0:btn"
    assert res.intents[1].control_fingerprint == "0.0:btn"
    assert res.intents[0] != res.intents[1]


# 15. Volatile control preserved where eligible
def test_volatile_control_preserved_with_stable_fingerprint():
    factory = CandidateActionIntentFactory()
    vol_btn = _make_control(
        control_id="0:session_btn",
        accessible_role="button",
        accessible_name="Session 12345",
        volatile_metadata=("volatile",),
    )
    state = _make_state((vol_btn,))

    res = factory.derive_intents(_make_norm_result(state))
    assert len(res.intents) == 1
    assert res.intents[0].control_fingerprint == "0:session_btn"


# 16. Destructive/unsafe semantic controls do not become silently safe
def test_destructive_and_mutating_controls_never_become_silently_safe():
    factory = CandidateActionIntentFactory()

    # Ambiguous/potentially mutating button ("delete")
    mut_btn = _make_control(control_id="0:del_btn", accessible_role="button", accessible_name="delete")
    # Prohibited link ("/delete")
    prohib_link = _make_control(control_id="0:del_link", accessible_role="link", accessible_name="safe text", href="/delete")
    # Ordinary safe button
    safe_btn = _make_control(control_id="0:safe_btn", accessible_role="button", accessible_name="Continue")

    state = _make_state((mut_btn, prohib_link, safe_btn))
    res = factory.derive_intents(_make_norm_result(state))

    # 1. Prohibited control must produce ZERO candidate execution intents (safety block / excluded from execution)
    prohib_intents = [i for i in res.intents if i.control_fingerprint == "0:del_link"]
    assert len(prohib_intents) == 0

    # 2. Mutating control must retain POTENTIALLY_MUTATING risk classification, never SAFE_CANDIDATE
    mut_intents = [i for i in res.intents if i.control_fingerprint == "0:del_btn"]
    assert len(mut_intents) == 1
    assert mut_intents[0].risk_classification == RiskClassification.POTENTIALLY_MUTATING
    assert mut_intents[0].risk_classification != RiskClassification.SAFE_CANDIDATE

    # 3. Safe button retains SAFE_CANDIDATE
    safe_intents = [i for i in res.intents if i.control_fingerprint == "0:safe_btn"]
    assert len(safe_intents) == 1
    assert safe_intents[0].risk_classification == RiskClassification.SAFE_CANDIDATE


# 17. Form-owned mutating controls retain POTENTIALLY_MUTATING
def test_form_owned_mutating_control_risk():
    factory = CandidateActionIntentFactory()
    submit_btn = _make_control(control_id="0:sub", accessible_role="button", accessible_name="submit", form_ownership="form1")
    state = _make_state((submit_btn,))

    res = factory.derive_intents(_make_norm_result(state))
    assert len(res.intents) == 1
    assert res.intents[0].risk_classification == RiskClassification.POTENTIALLY_MUTATING


# 18. Target state always UNRESOLVED
def test_target_state_is_always_unresolved():
    factory = CandidateActionIntentFactory()
    c = _make_control(control_id="0:c1", accessible_role="button", accessible_name="Click")
    state = _make_state((c,))

    res = factory.derive_intents(_make_norm_result(state))
    for intent in res.intents:
        assert intent.target_state == "UNRESOLVED"
        assert not hasattr(intent, "execute")


# 19. Repeated input produces identical intents (determinism)
def test_determinism_repeated_input_identical_output():
    factory = CandidateActionIntentFactory()
    c1 = _make_control(control_id="0:c1", accessible_role="button", accessible_name="A")
    c2 = _make_control(control_id="0:c2", accessible_role="link", accessible_name="B", href="/b")
    state = _make_state((c1, c2))
    norm_res = _make_norm_result(state)

    res1 = factory.derive_intents(norm_res)
    res2 = factory.derive_intents(norm_res)

    assert res1.intents == res2.intents
    assert res1.terminal_status == res2.terminal_status
    assert res1.limitations == res2.limitations


# 20. Deterministic ordering preserves semantic control order
def test_deterministic_ordering_preserves_control_order():
    factory = CandidateActionIntentFactory()
    c_z = _make_control(control_id="0:z", accessible_role="button", accessible_name="Zebra")
    c_a = _make_control(control_id="0:a", accessible_role="button", accessible_name="Alpha")
    c_m = _make_control(control_id="0:m", accessible_role="button", accessible_name="Mango")
    state = _make_state((c_z, c_a, c_m))

    res = factory.derive_intents(_make_norm_result(state))
    assert [i.control_fingerprint for i in res.intents] == ["0:z", "0:a", "0:m"]


# 21. Boundedness and Immutability
def test_boundedness_and_immutability():
    factory = CandidateActionIntentFactory()
    c1 = _make_control(control_id="0:c1", accessible_role="button", accessible_name="Btn 1")
    c2 = _make_control(control_id="0:c2", accessible_role="button", accessible_name="Btn 2")
    state = _make_state((c1, c2))

    res = factory.derive_intents(_make_norm_result(state))
    assert isinstance(res.intents, tuple)
    assert isinstance(res.limitations, tuple)
    assert len(res.intents) <= len(state.controls)

    with pytest.raises((FrozenInstanceError, AttributeError)):
        res.intents = ()

    with pytest.raises((FrozenInstanceError, AttributeError)):
        res.terminal_status = "MUTATED"

    # Verify class-level role action map is immutable MappingProxyType
    with pytest.raises(TypeError):
        CandidateActionIntentFactory.ROLE_ACTION_MAP["button"] = "dblclick"


# 22. Compatibility with CandidateIntentGraph
def test_derived_intents_consumed_by_candidate_intent_graph():
    factory = CandidateActionIntentFactory()
    c1 = _make_control(control_id="0:c1", accessible_role="button", accessible_name="Btn 1")
    c2 = _make_control(control_id="0:c2", accessible_role="button", accessible_name="Btn 2")
    state = _make_state((c1, c2))

    res = factory.derive_intents(_make_norm_result(state))
    graph = CandidateIntentGraph(max_nodes=10, max_intents=10)

    assert graph.add_state_node(state) is True
    for intent in res.intents:
        assert graph.add_intent(intent) is True

    assert len(graph.get_all_intents()) == 2
    assert graph.get_all_intents() == list(res.intents)


# 23. Zero browser / runtime references
def test_zero_browser_or_runtime_references_in_intents():
    factory = CandidateActionIntentFactory()
    c = _make_control(control_id="0:c1", accessible_role="button", accessible_name="Btn")
    state = _make_state((c,))

    res = factory.derive_intents(_make_norm_result(state))
    intent = res.intents[0]

    forbidden_attributes = [
        "page", "frame", "browser", "context", "locator", "element",
        "session", "connection", "client", "request", "response",
        "click", "dblclick", "fill", "type", "press", "evaluate",
    ]
    for attr in forbidden_attributes:
        assert not hasattr(intent, attr), f"Intent must not have attribute {attr}"


# 24. Real InteractionSafetyPolicy integration
def test_real_safety_policy_integration():
    factory = CandidateActionIntentFactory()

    # Link with javascript:location -> PROHIBITED -> 0 intents
    c_js = _make_control(control_id="0:js", accessible_role="link", accessible_name="JS", href="javascript:location='foo'")
    assert InteractionSafetyPolicy.classify(c_js) == RiskClassification.PROHIBITED
    res_js = factory.derive_intents(_make_norm_result(_make_state((c_js,))))
    assert len(res_js.intents) == 0

    # Link with target=_blank -> UNKNOWN -> 0 intents
    c_blank = _make_control(control_id="0:blank", accessible_role="link", accessible_name="New Tab", href="/safe", target="_blank")
    assert InteractionSafetyPolicy.classify(c_blank) == RiskClassification.UNKNOWN
    res_blank = factory.derive_intents(_make_norm_result(_make_state((c_blank,))))
    assert len(res_blank.intents) == 0

    # Tab role -> SAFE_CANDIDATE -> click
    c_tab = _make_control(control_id="0:tab", accessible_role="tab", accessible_name="Overview")
    assert InteractionSafetyPolicy.classify(c_tab) == RiskClassification.SAFE_CANDIDATE
    res_tab = factory.derive_intents(_make_norm_result(_make_state((c_tab,))))
    assert len(res_tab.intents) == 1
    assert res_tab.intents[0].expected_action_type == "click"

    # Menuitem role without supported pattern -> UNKNOWN -> 0 intents (Blocker 6)
    c_menu_unpatterned = _make_control(control_id="0:menu_raw", accessible_role="menuitem", accessible_name="Profile")
    assert InteractionSafetyPolicy.classify(c_menu_unpatterned) == RiskClassification.UNKNOWN
    res_menu_unpatterned = factory.derive_intents(_make_norm_result(_make_state((c_menu_unpatterned,))))
    assert len(res_menu_unpatterned.intents) == 0

    # Menuitem role with positive supported pattern (state toggle) -> SAFE_CANDIDATE -> click
    c_menu = _make_control(control_id="0:menu", accessible_role="menuitem", accessible_name="Profile", state_expanded=False)
    assert InteractionSafetyPolicy.classify(c_menu) == RiskClassification.SAFE_CANDIDATE
    res_menu = factory.derive_intents(_make_norm_result(_make_state((c_menu,))))
    assert len(res_menu.intents) == 1
    assert res_menu.intents[0].expected_action_type == "click"


# 25. Static AST and fresh-subprocess isolation test
def test_dependency_isolation():
    script = """
import sys
import os
import ast

def inspect_ast():
    import os
    path = os.path.join(os.environ["PYTHONPATH"], "app", "qa", "candidate_intent_factory.py")
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    forbidden_imports = [
        'playwright', 'flask', 'sqlalchemy', 'requests', 'httpx',
        'aiohttp', 'socket', 'subprocess', 'app.qa.browser_snapshot_adapter',
        'app.routes', 'app.models', 'engine', 'full_site'
    ]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                if any(f in n.name for f in forbidden_imports):
                    print(f"AST VIOLATION: {n.name}")
                    sys.exit(1)
        elif isinstance(node, ast.ImportFrom):
            if any(f in (node.module or '') for f in forbidden_imports):
                print(f"AST VIOLATION: {node.module}")
                sys.exit(1)

inspect_ast()

import app.qa.candidate_intent_factory
forbidden = [
    'playwright', 'flask', 'sqlalchemy', 'requests', 'httpx',
    'aiohttp', 'socket', 'subprocess', 'app.qa.browser_snapshot_adapter',
    'app.routes', 'app.models', 'engine', 'full_site'
]
loaded = set(sys.modules.keys())
violations = [m for m in forbidden if m in loaded]
if violations:
    print(f"VIOLATIONS: {violations}")
    sys.exit(1)
print("ISOLATION CLEAN")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"Isolation test failed: {result.stderr}\n{result.stdout}"
    assert "ISOLATION CLEAN" in result.stdout


# 26. Pure action taxonomy mapping without safety policy override
def test_pure_action_taxonomy_mapping():
    # Verify exact mapping without bypassing safety
    assert CandidateActionIntentFactory._action_type_for_role("button") == "click"
    assert CandidateActionIntentFactory._action_type_for_role("link") == "click"
    assert CandidateActionIntentFactory._action_type_for_role("tab") == "click"
    assert CandidateActionIntentFactory._action_type_for_role("menuitem") == "click"
    assert CandidateActionIntentFactory._action_type_for_role("combobox") == "select"
    assert CandidateActionIntentFactory._action_type_for_role("checkbox") == "toggle"
    assert CandidateActionIntentFactory._action_type_for_role("radio") == "select"
    assert CandidateActionIntentFactory._action_type_for_role("textbox") == "input"

    # Distinctions preserved
    assert CandidateActionIntentFactory._action_type_for_role("checkbox") != "radio"
    assert CandidateActionIntentFactory._action_type_for_role("checkbox") != "select"
    assert CandidateActionIntentFactory._action_type_for_role("radio") != "toggle"
    assert CandidateActionIntentFactory._action_type_for_role("textbox") != "toggle"

    # Unmapped / unrecognized roles return None
    assert CandidateActionIntentFactory._action_type_for_role("dialog") is None
    assert CandidateActionIntentFactory._action_type_for_role("generic") is None
    assert CandidateActionIntentFactory._action_type_for_role("unknown") is None
    assert CandidateActionIntentFactory._action_type_for_role("") is None
    assert CandidateActionIntentFactory._action_type_for_role(None) is None


# 27. Fail-closed action test: unmapped semantic role cannot become "click"
def test_fail_closed_action_mapping_no_default_click():
    factory = CandidateActionIntentFactory()

    # Even if a control somehow passed safety, if its role is unmapped, it must NOT default to click
    assert CandidateActionIntentFactory._action_type_for_role("some_custom_unmapped_role") is None

    # Statically verify production factory source code does NOT contain .get(..., "click") or default fallback
    module_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "qa", "candidate_intent_factory.py"
    )
    with open(module_path, "r", encoding="utf-8") as f:
        src = f.read()

    assert '.get(control.accessible_role, "click")' not in src
    assert '.get(role, "click")' not in src
    assert ', "click")' not in src


# 28. Policy non-overridability test
def test_policy_and_role_map_cannot_be_overridden_via_constructor():
    # Constructor accepts no arguments
    with pytest.raises(TypeError):
        CandidateActionIntentFactory(policy_classifier=lambda _: RiskClassification.SAFE_CANDIDATE)

    with pytest.raises(TypeError):
        CandidateActionIntentFactory(allowed_risks=(RiskClassification.PROHIBITED,))

    with pytest.raises(TypeError):
        CandidateActionIntentFactory(role_action_map={"custom": "click"})


# 29. Coverage boundary test: public derivation requires SemanticNormalizationResult
def test_coverage_boundary_rejects_bare_semantic_state():
    factory = CandidateActionIntentFactory()
    c = _make_control()
    state = _make_state((c,))

    # Direct bare SemanticState must fail clearly with TypeError, not silently manufactured to COMPLETE
    with pytest.raises(TypeError) as excinfo:
        factory.derive_intents(state)
    assert "SemanticNormalizationResult" in str(excinfo.value)
    assert "Bare SemanticState is rejected" in str(excinfo.value)

    with pytest.raises(TypeError) as excinfo_cls:
        CandidateActionIntentFactory.derive(state)
    assert "SemanticNormalizationResult" in str(excinfo_cls.value)


# 30. Coverage boundary test: preserves limitations and terminal status faithfully
def test_coverage_boundary_preserves_limitations_and_terminal_status():
    factory = CandidateActionIntentFactory()
    c = _make_control()
    state = _make_state((c,))

    # Partial coverage limitations preserved
    res_partial = factory.derive_intents(
        SemanticNormalizationResult(
            semantic_state=state,
            terminal_status="COMPLETE",
            limitations=("LIMITED_CONTROL_BUDGET",),
        )
    )
    assert res_partial.terminal_status == "COMPLETE"
    assert res_partial.limitations == ("LIMITED_CONTROL_BUDGET",)
    assert len(res_partial.intents) == 1

    # Terminal status preserved, zero intents
    res_cancelled = factory.derive_intents(
        SemanticNormalizationResult(
            semantic_state=None,
            terminal_status="CANCELLED",
            limitations=("PARTIAL_BEFORE_CANCEL",),
        )
    )
    assert res_cancelled.terminal_status == "CANCELLED"
    assert res_cancelled.limitations == ("PARTIAL_BEFORE_CANCEL",)
    assert len(res_cancelled.intents) == 0
