import pytest
import subprocess
import sys
import os

from app.qa.semantic_normalizer import SemanticNormalizer, SemanticNormalizationResult
from app.qa.browser_snapshot_model import BrowserSnapshot, FrameSnapshot, ControlSnapshot, SnapshotCoverage
from app.qa.semantic_model import SemanticState, SemanticControl
from app.qa.state_fingerprint import CanonicalStateFingerprint

def _base_control(**kwargs):
    d = dict(
        control_id="c1",
        raw_role="button",
        raw_name="Save",
        raw_visible=True,
        raw_enabled=True,
        checked=None,
        selected=None,
        expanded=None,
        href=None,
        target=None,
        rel=None,
        download_metadata=None,
        form_ownership=None,
        semantic_parent_context="body",
        is_private=False,
        is_volatile=False
    )
    d.update(kwargs)
    return ControlSnapshot(**d)

def _base_frame(frame_id="f1", controls=(), child_frames=(), is_same=True, limitations=()):
    return FrameSnapshot(
        frame_id=frame_id,
        url="http://example.com",
        origin="http://example.com",
        name="test_frame",
        is_same_origin=is_same,
        controls=tuple(controls),
        child_frames=tuple(child_frames),
        coverage=SnapshotCoverage("COMPLETE", tuple(limitations))
    )

def test_complete_snapshot_to_semantic_state():
    normalizer = SemanticNormalizer()
    c1 = _base_control()
    main = _base_frame(controls=[c1])
    snap = BrowserSnapshot("http://example.com", main, SnapshotCoverage("COMPLETE", ()))

    res = normalizer.normalize_snapshot(snap)
    assert res.terminal_status == "COMPLETE"
    assert res.limitations == ()
    assert isinstance(res.semantic_state, SemanticState)
    assert res.semantic_state.url == "http://example.com"
    assert len(res.semantic_state.controls) == 1

    ctrl = res.semantic_state.controls[0]
    assert ctrl.control_id == "0:c1"
    assert ctrl.accessible_role == "button"
    assert ctrl.accessible_name == "Save"

def test_repeated_identical_input_produces_identical_output_and_fingerprint():
    normalizer = SemanticNormalizer()
    c1 = _base_control()
    main = _base_frame(controls=[c1])
    snap = BrowserSnapshot("http://example.com", main, SnapshotCoverage("COMPLETE", ()))

    res1 = normalizer.normalize_snapshot(snap)
    res2 = normalizer.normalize_snapshot(snap)

    assert res1.semantic_state == res2.semantic_state
    fp1 = CanonicalStateFingerprint.generate(res1.semantic_state)
    fp2 = CanonicalStateFingerprint.generate(res2.semantic_state)
    assert fp1 == fp2

def test_button_and_link_normalization():
    normalizer = SemanticNormalizer()
    c_btn = _base_control(raw_role="button")
    c_link = _base_control(raw_role="a", href="/test")
    c_gen = _base_control(raw_role="a", href=None)
    main = _base_frame(controls=[c_btn, c_link, c_gen])
    snap = BrowserSnapshot("http://example.com", main, SnapshotCoverage("COMPLETE", ()))

    res = normalizer.normalize_snapshot(snap)
    assert res.semantic_state.controls[0].accessible_role == "button"
    assert res.semantic_state.controls[1].accessible_role == "link"
    assert res.semantic_state.controls[1].href == "/test"
    assert res.semantic_state.controls[2].accessible_role == "generic"

def test_checkbox_radio_text_select():
    normalizer = SemanticNormalizer()
    main = _base_frame(controls=[
        _base_control(control_id="1", raw_role="input checkbox", checked=True),
        _base_control(control_id="2", raw_role="input radio", checked=False),
        _base_control(control_id="3", raw_role="input text"),
        _base_control(control_id="4", raw_role="select"),
        _base_control(control_id="5", raw_role="input") # ambiguous fallback to generic
    ])
    res = normalizer.normalize_snapshot(BrowserSnapshot("http://example.com", main, SnapshotCoverage("COMPLETE", ())))
    ctrls = res.semantic_state.controls
    assert ctrls[0].accessible_role == "checkbox"
    assert ctrls[0].state_checked is True
    assert ctrls[1].accessible_role == "radio"
    assert ctrls[1].state_checked is False
    assert ctrls[2].accessible_role == "textbox"
    assert ctrls[3].accessible_role == "combobox"
    assert ctrls[4].accessible_role == "generic"

def test_state_attributes():
    normalizer = SemanticNormalizer()
    c = _base_control(
        expanded=True, selected=False, checked=True, raw_visible=False, raw_enabled=False,
        target="_blank", form_ownership="form1"
    )
    res = normalizer.normalize_snapshot(BrowserSnapshot("http://example.com", _base_frame(controls=[c]), SnapshotCoverage("COMPLETE", ())))
    ctrl = res.semantic_state.controls[0]
    assert ctrl.state_expanded is True
    assert ctrl.state_selected is False
    assert ctrl.state_checked is True
    assert ctrl.visibility is False
    assert ctrl.enabled is False
    assert ctrl.target == "_blank"
    assert ctrl.form_ownership == "form1"

def test_explicit_and_unknown_roles():
    normalizer = SemanticNormalizer()
    c1 = _base_control(raw_role="dialog")
    c2 = _base_control(raw_role="unknown_garbage")
    res = normalizer.normalize_snapshot(BrowserSnapshot("http://example.com", _base_frame(controls=[c1, c2]), SnapshotCoverage("COMPLETE", ())))
    assert res.semantic_state.controls[0].accessible_role == "dialog"
    assert res.semantic_state.controls[1].accessible_role == "generic"

def test_empty_raw_name():
    normalizer = SemanticNormalizer()
    c = _base_control(raw_name="")
    res = normalizer.normalize_snapshot(BrowserSnapshot("http://example.com", _base_frame(controls=[c]), SnapshotCoverage("COMPLETE", ())))
    assert res.semantic_state.controls[0].accessible_name == ""

def test_privacy_and_sentinel():
    normalizer = SemanticNormalizer()
    c = _base_control(raw_name="PRIVATE-PHASE-A3-SENTINEL", is_private=True, href="PRIVATE-PHASE-A3-SENTINEL")
    res = normalizer.normalize_snapshot(BrowserSnapshot("http://example.com", _base_frame(controls=[c]), SnapshotCoverage("COMPLETE", ())))
    ctrl = res.semantic_state.controls[0]
    assert "PRIVATE-PHASE-A3-SENTINEL" not in ctrl.accessible_name
    assert "PRIVATE-PHASE-A3-SENTINEL" not in repr(ctrl)
    assert ctrl.accessible_name == "***"
    assert ctrl.href == "***"
    assert ctrl.is_private is True

def test_volatile_metadata():
    normalizer = SemanticNormalizer()
    c1 = _base_control(control_id="c1", raw_name="123e4567-e89b-12d3-a456-426614174000", is_volatile=True)
    c2 = _base_control(control_id="c2", raw_name="2026-10-09", is_volatile=True)
    c3 = _base_control(control_id="c3", raw_name="12345", is_volatile=True)
    res = normalizer.normalize_snapshot(BrowserSnapshot("http://example.com", _base_frame(controls=[c1, c2, c3]), SnapshotCoverage("COMPLETE", ())))
    
    assert res.semantic_state.controls[0].volatile_metadata == ("volatile",)
    assert res.semantic_state.controls[0].accessible_name == "123e4567-e89b-12d3-a456-426614174000"
    
    assert res.semantic_state.controls[1].volatile_metadata == ("volatile",)
    assert res.semantic_state.controls[1].accessible_name == "2026-10-09"
    
    assert res.semantic_state.controls[2].volatile_metadata == ("volatile",)
    assert res.semantic_state.controls[2].accessible_name == "12345"

def test_duplicate_same_label_controls():
    normalizer = SemanticNormalizer()
    c1 = _base_control(control_id="c1", raw_name="Save")
    c2 = _base_control(control_id="c2", raw_name="Save")
    res = normalizer.normalize_snapshot(BrowserSnapshot("http://example.com", _base_frame(controls=[c1, c2]), SnapshotCoverage("COMPLETE", ())))
    assert len(res.semantic_state.controls) == 2
    assert res.semantic_state.controls[0].control_id == "0:c1"
    assert res.semantic_state.controls[1].control_id == "0:c2"

def test_frame_distinguishability():
    normalizer = SemanticNormalizer()
    c_main = _base_control(control_id="c1", semantic_parent_context="div")
    c_child = _base_control(control_id="c1", semantic_parent_context="div")
    child = _base_frame(frame_id="f2", controls=[c_child])
    main = _base_frame(frame_id="f1", controls=[c_main], child_frames=[child])
    res = normalizer.normalize_snapshot(BrowserSnapshot("http://example.com", main, SnapshotCoverage("COMPLETE", ())))
    ctrls = res.semantic_state.controls
    assert len(ctrls) == 2
    assert ctrls[0].control_id == "0:c1"
    assert ctrls[0].parent_semantic_context == "[0] div"
    assert ctrls[1].control_id == "0.0:c1"
    assert ctrls[1].parent_semantic_context == "[0.0] div"

def test_frame_collision_same_name():
    normalizer = SemanticNormalizer()
    c_child1 = _base_control(control_id="auto-button-0")
    c_child2 = _base_control(control_id="auto-button-0")
    child1 = _base_frame(frame_id="widget", controls=[c_child1])
    child2 = _base_frame(frame_id="widget", controls=[c_child2])
    main = _base_frame(frame_id="f1", controls=[], child_frames=[child1, child2])
    res = normalizer.normalize_snapshot(BrowserSnapshot("http://example.com", main, SnapshotCoverage("COMPLETE", ())))
    ctrls = res.semantic_state.controls
    assert len(ctrls) == 2
    assert ctrls[0].control_id != ctrls[1].control_id
    assert ctrls[0].control_id == "0.0:auto-button-0"
    assert ctrls[1].control_id == "0.1:auto-button-0"

def test_frame_collision_nested():
    normalizer = SemanticNormalizer()
    c_child = _base_control(control_id="auto-button-0")
    nested = _base_frame(frame_id="widget", controls=[c_child])
    child = _base_frame(frame_id="widget", controls=[], child_frames=[nested])
    main = _base_frame(controls=[], child_frames=[child])
    res = normalizer.normalize_snapshot(BrowserSnapshot("http://example.com", main, SnapshotCoverage("COMPLETE", ())))
    ctrl = res.semantic_state.controls[0]
    assert ctrl.control_id == "0.0.0:auto-button-0"

def test_frame_collision_main_child():
    normalizer = SemanticNormalizer()
    c_main = _base_control(control_id="auto-button-0")
    c_child = _base_control(control_id="auto-button-0")
    child = _base_frame(frame_id="widget", controls=[c_child])
    main = _base_frame(frame_id="widget", controls=[c_main], child_frames=[child])
    res = normalizer.normalize_snapshot(BrowserSnapshot("http://example.com", main, SnapshotCoverage("COMPLETE", ())))
    ctrls = res.semantic_state.controls
    assert len(ctrls) == 2
    assert ctrls[0].control_id != ctrls[1].control_id
    assert ctrls[0].control_id == "0:auto-button-0"
    assert ctrls[1].control_id == "0.0:auto-button-0"

def test_cross_origin_limited_frame():
    normalizer = SemanticNormalizer()
    child = _base_frame(frame_id="f2", controls=[], is_same=False, limitations=("CROSS_ORIGIN_LIMITED",))
    main = _base_frame(controls=[], child_frames=[child])
    snap = BrowserSnapshot("http://example.com", main, SnapshotCoverage("COMPLETE", ()))
    res = normalizer.normalize_snapshot(snap)
    assert res.terminal_status == "COMPLETE"
    assert "CROSS_ORIGIN_LIMITED" in res.limitations
    assert len(res.semantic_state.controls) == 0

def test_limitations_preserved():
    normalizer = SemanticNormalizer()
    child = _base_frame(limitations=("LIMITED_CONTROL_BUDGET",))
    main = _base_frame(limitations=("LIMITED_FRAME_BUDGET",), child_frames=[child])
    snap = BrowserSnapshot("http://example.com", main, SnapshotCoverage("COMPLETE", ("SOME_OTHER_LIMIT",)))
    res = normalizer.normalize_snapshot(snap)
    assert set(res.limitations) == {"SOME_OTHER_LIMIT", "LIMITED_FRAME_BUDGET", "LIMITED_CONTROL_BUDGET"}

def test_terminal_states():
    normalizer = SemanticNormalizer()
    for status in ("CANCELLED", "TARGET_CLOSED", "FRAME_DETACHED", "NAVIGATION_RACE", "NOT_ELIGIBLE", "WRONG_CONTEXT", "MANUAL_PAGE_EXCLUDED", "OPAQUE_ORIGIN"):
        snap = BrowserSnapshot("http://example.com", _base_frame(), SnapshotCoverage(status, ()))
        res = normalizer.normalize_snapshot(snap)
        assert res.terminal_status == status
        assert res.semantic_state is None

def test_dependency_isolation():
    script = """
import sys
import os
import ast

def inspect_ast():
    import os
    path = os.path.join(os.environ["PYTHONPATH"], "app", "qa", "semantic_normalizer.py")
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    forbidden_imports = ['playwright', 'flask', 'sqlalchemy', 'requests', 'httpx', 'aiohttp', 'socket', 'subprocess', 'app.qa.browser_snapshot_adapter', 'app.routes', 'app.models']
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

import app.qa.semantic_normalizer
forbidden = ['playwright', 'flask', 'sqlalchemy', 'requests', 'httpx', 'aiohttp', 'socket', 'subprocess', 'app.qa.browser_snapshot_adapter', 'app.routes', 'app.models']
loaded = set(sys.modules.keys())
violations = [m for m in forbidden if m in loaded]
if violations:
    print(f"VIOLATIONS: {violations}")
    sys.exit(1)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"Isolation test failed: {result.stderr}\\n{result.stdout}"
