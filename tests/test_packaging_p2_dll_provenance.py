"""Positive and negative regression tests for R13-01: Strict DLL provenance classification."""

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from installer.build_release_1_0 import (
    assert_dll_provenance_trusted,
    classify_dll_provenance,
    sha256_file,
)
from playwright.sync_api import sync_playwright


def _create_junction(junction_path: str, target_path: str) -> None:
    env = os.environ.copy()
    env["QAT_JUNC"] = junction_path
    env["QAT_TARG"] = target_path
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "New-Item -ItemType Junction -Path $env:QAT_JUNC -Target $env:QAT_TARG | Out-Null",
        ],
        env=env,
        check=True,
    )


def test_classify_dll_provenance_positive_full_r13_candidate():
    """All 31 legitimate DLLs in a real built candidate must pass strict baseline + PEP 376 + browser checks."""
    r13_app = os.path.join(REPO_ROOT, "dist_candidate_20261106_v1_0_r13", "QA-Tester")
    assert os.path.isdir(r13_app), f"Expected preserved r13 app dir: {r13_app}"

    audit = classify_dll_provenance(r13_app, require_complete_set=True)
    assert_dll_provenance_trusted(audit)

    assert audit["total_dlls"] == 31
    assert audit["untrusted_count"] == 0
    assert audit["missing_expected_dlls"] == []
    assert audit["counts_by_origin"] == {
        "python_base": 7,
        "venv_packages": 10,
        "playwright_browsers": 14,
        "external_path": 0,
    }

    by_rel = {r["relative_path"]: r for r in audit["records"]}
    hs_egl = by_rel[
        "_internal/playwright_browsers/chromium_headless_shell-1248/chrome-headless-shell-win64/libEGL.dll"
    ]
    assert "chromium_headless_shell-1248" in hs_egl["verified_source_path"]
    assert hs_egl["owner_distribution"] == "playwright-chromium-headless-shell"

    wv_core = by_rel["_internal/webview/lib/Microsoft.Web.WebView2.Core.dll"]
    assert wv_core["owner_distribution"] == "pywebview"
    assert wv_core["owner_version"] == "4.4.1"


def test_classify_dll_provenance_positive_known_dlls():
    """Subset of legitimate DLLs from python_base, PEP 376 venv_packages, and both Playwright browsers passes."""
    with sync_playwright() as pw:
        chromium_exe = pw.chromium.executable_path

    ms_playwright_dir = os.path.dirname(os.path.dirname(os.path.dirname(chromium_exe)))
    c_egl = os.path.join(ms_playwright_dir, "chromium-1248", "chrome-win64", "libEGL.dll")
    hs_egl = os.path.join(
        ms_playwright_dir, "chromium_headless_shell-1248", "chrome-headless-shell-win64", "libEGL.dll"
    )
    venv_dir = os.path.abspath(os.path.join(os.path.dirname(sys.executable), ".."))
    clr_dll = os.path.join(
        venv_dir, "Lib", "site-packages", "clr_loader", "ffi", "dlls", "amd64", "ClrLoader.dll"
    )

    with tempfile.TemporaryDirectory(prefix="qat_dll_pos_test_") as tmp_dir:
        c_target = os.path.join(tmp_dir, "_internal", "playwright_browsers", "chromium-1248", "chrome-win64")
        hs_target = os.path.join(
            tmp_dir,
            "_internal",
            "playwright_browsers",
            "chromium_headless_shell-1248",
            "chrome-headless-shell-win64",
        )
        clr_target = os.path.join(tmp_dir, "_internal", "clr_loader", "ffi", "dlls", "amd64")
        os.makedirs(c_target, exist_ok=True)
        os.makedirs(hs_target, exist_ok=True)
        os.makedirs(clr_target, exist_ok=True)

        shutil.copy2(c_egl, os.path.join(c_target, "libEGL.dll"))
        shutil.copy2(hs_egl, os.path.join(hs_target, "libEGL.dll"))
        shutil.copy2(clr_dll, os.path.join(clr_target, "ClrLoader.dll"))

        audit = classify_dll_provenance(tmp_dir)
        assert_dll_provenance_trusted(audit)
        assert audit["counts_by_origin"] == {
            "python_base": 0,
            "venv_packages": 1,
            "playwright_browsers": 2,
            "external_path": 0,
        }


def test_codex_counterexample_untrusted_dll_in_venv_arbitrary_subdir_rejected():
    """Mandatory Codex R13-01 counterexample: unknown.dll in venv/arbitrary/unknown.dll MUST be rejected."""
    payload = b"MZFAKE_UNTRUSTED_DLL"
    expected_sha = hashlib.sha256(payload).hexdigest().upper()

    with tempfile.TemporaryDirectory(prefix="qat_r13_01_codex_") as tmp_dir:
        fake_app = os.path.join(tmp_dir, "app")
        fake_venv = os.path.join(tmp_dir, "venv")
        os.makedirs(os.path.join(fake_app, "_internal"), exist_ok=True)
        os.makedirs(os.path.join(fake_venv, "arbitrary"), exist_ok=True)

        with open(os.path.join(fake_app, "_internal", "unknown.dll"), "wb") as f:
            f.write(payload)
        with open(os.path.join(fake_venv, "arbitrary", "unknown.dll"), "wb") as f:
            f.write(payload)

        audit = classify_dll_provenance(fake_app, venv_dir=fake_venv)
        assert audit["counts_by_origin"]["venv_packages"] == 0
        assert audit["counts_by_origin"]["external_path"] == 1
        assert audit["untrusted_count"] == 1
        assert audit["records"][0]["trusted"] is False

        with pytest.raises(RuntimeError, match="Unverified DLL source found"):
            assert_dll_provenance_trusted(audit)


def test_unknown_dll_inside_allowed_site_packages_subdir_rejected():
    """An unknown DLL placed inside Lib/site-packages/webview/lib/ is still rejected."""
    payload = b"MZ_ROGUE_INSIDE_SITE_PACKAGES"
    with tempfile.TemporaryDirectory(prefix="qat_r13_01_sp_") as tmp_dir:
        fake_app = os.path.join(tmp_dir, "app")
        fake_venv = os.path.join(tmp_dir, "venv")
        sp_webview = os.path.join(fake_venv, "Lib", "site-packages", "webview", "lib")
        app_webview = os.path.join(fake_app, "_internal", "webview", "lib")
        os.makedirs(sp_webview, exist_ok=True)
        os.makedirs(app_webview, exist_ok=True)

        with open(os.path.join(sp_webview, "rogue_helper.dll"), "wb") as f:
            f.write(payload)
        with open(os.path.join(app_webview, "rogue_helper.dll"), "wb") as f:
            f.write(payload)

        audit = classify_dll_provenance(fake_app, venv_dir=fake_venv)
        assert audit["counts_by_origin"]["venv_packages"] == 0
        assert audit["counts_by_origin"]["external_path"] == 1
        with pytest.raises(RuntimeError, match="Unverified DLL source found"):
            assert_dll_provenance_trusted(audit)


def test_double_tampered_dll_matching_local_source_and_forged_record_still_rejected():
    """Even if packaged copy, local source copy, AND forged PEP 376 RECORD share tampered bytes, baseline rejects it."""
    tampered = b"MZ_TAMPERED_CLRLOADER_BOTH_ENDS"
    b64_hash = base64.urlsafe_b64encode(hashlib.sha256(tampered).digest()).decode("ascii").rstrip("=")

    with tempfile.TemporaryDirectory(prefix="qat_r13_01_tamper_") as tmp_dir:
        fake_app = os.path.join(tmp_dir, "app")
        fake_venv = os.path.join(tmp_dir, "venv")

        rel_pkg = os.path.join("clr_loader", "ffi", "dlls", "amd64", "ClrLoader.dll")
        sp_root = os.path.join(fake_venv, "Lib", "site-packages")
        dist_info = os.path.join(sp_root, "clr_loader-0.3.1.dist-info")
        os.makedirs(os.path.join(sp_root, os.path.dirname(rel_pkg)), exist_ok=True)
        os.makedirs(dist_info, exist_ok=True)
        os.makedirs(os.path.join(fake_app, "_internal", os.path.dirname(rel_pkg)), exist_ok=True)

        with open(os.path.join(sp_root, rel_pkg), "wb") as f:
            f.write(tampered)
        with open(os.path.join(fake_app, "_internal", rel_pkg), "wb") as f:
            f.write(tampered)
        with open(os.path.join(dist_info, "METADATA"), "w", encoding="utf-8") as mf:
            mf.write("Metadata-Version: 2.1\nName: clr-loader\nVersion: 0.3.1\n\n")
        with open(os.path.join(dist_info, "RECORD"), "w", encoding="utf-8") as rf:
            rf.write(f"clr_loader/ffi/dlls/amd64/ClrLoader.dll,sha256={b64_hash},{len(tampered)}\n")

        # Also test double-tampered browser DLL in ms-playwright + app
        fake_ms_pw = os.path.join(tmp_dir, "ms-playwright")
        os.makedirs(os.path.join(fake_ms_pw, "chromium-1248", "chrome-win64"), exist_ok=True)
        os.makedirs(
            os.path.join(fake_app, "_internal", "playwright_browsers", "chromium-1248", "chrome-win64"),
            exist_ok=True,
        )
        with open(os.path.join(fake_ms_pw, "chromium-1248", "chrome-win64", "libEGL.dll"), "wb") as f:
            f.write(tampered)
        with open(
            os.path.join(
                fake_app, "_internal", "playwright_browsers", "chromium-1248", "chrome-win64", "libEGL.dll"
            ),
            "wb",
        ) as f:
            f.write(tampered)

        audit = classify_dll_provenance(fake_app, venv_dir=fake_venv, ms_playwright_dir=fake_ms_pw)
        assert audit["counts_by_origin"]["venv_packages"] == 0
        assert audit["counts_by_origin"]["playwright_browsers"] == 0
        assert audit["counts_by_origin"]["external_path"] == 2
        assert all(r["rejection_reason"] == "baseline_sha256_mismatch" for r in audit["records"])
        with pytest.raises(RuntimeError, match="baseline_sha256_mismatch"):
            assert_dll_provenance_trusted(audit)


def test_legitimate_dll_at_wrong_packaged_relative_path_rejected():
    """A genuine Python/Chromium DLL placed at an unexpected packaged relative path is rejected."""
    with sync_playwright() as pw:
        chromium_exe = pw.chromium.executable_path
    ms_playwright_dir = os.path.dirname(os.path.dirname(os.path.dirname(chromium_exe)))
    c_egl = os.path.join(ms_playwright_dir, "chromium-1248", "chrome-win64", "libEGL.dll")

    with tempfile.TemporaryDirectory(prefix="qat_r13_01_wrongrel_") as tmp_dir:
        wrong_dir = os.path.join(tmp_dir, "_internal", "unexpected_location")
        os.makedirs(wrong_dir, exist_ok=True)
        shutil.copy2(c_egl, os.path.join(wrong_dir, "libEGL.dll"))

        audit = classify_dll_provenance(tmp_dir)
        assert audit["counts_by_origin"]["external_path"] == 1
        assert audit["records"][0]["rejection_reason"] == "not_in_trusted_dll_baseline"
        with pytest.raises(RuntimeError, match="not_in_trusted_dll_baseline"):
            assert_dll_provenance_trusted(audit)


def test_reparse_point_junction_in_app_or_source_rejected():
    """Reparse points (junctions) inside app_dir or source browser roots are rejected fail-closed."""
    with sync_playwright() as pw:
        chromium_exe = pw.chromium.executable_path
    real_ms_pw = os.path.dirname(os.path.dirname(os.path.dirname(chromium_exe)))
    real_c_dir = os.path.join(real_ms_pw, "chromium-1248", "chrome-win64")

    with tempfile.TemporaryDirectory(prefix="qat_r13_01_junc_") as tmp_dir:
        # Case A: Junction inside app_dir
        app_with_junc = os.path.join(tmp_dir, "app_junc")
        os.makedirs(os.path.join(app_with_junc, "_internal"), exist_ok=True)
        junc_in_app = os.path.join(app_with_junc, "_internal", "linked_dir")
        target_dir = os.path.join(tmp_dir, "outside_target")
        os.makedirs(target_dir, exist_ok=True)
        _create_junction(junc_in_app, target_dir)
        try:
            with pytest.raises(ValueError, match="Linked/reparse path rejected"):
                classify_dll_provenance(app_with_junc)
        finally:
            os.rmdir(junc_in_app)

        # Case B: Junction inside ms_playwright_dir pointing to real chromium-1248/chrome-win64
        app_clean = os.path.join(tmp_dir, "app_clean")
        c_target = os.path.join(app_clean, "_internal", "playwright_browsers", "chromium-1248", "chrome-win64")
        os.makedirs(c_target, exist_ok=True)
        shutil.copy2(os.path.join(real_c_dir, "libEGL.dll"), os.path.join(c_target, "libEGL.dll"))

        fake_ms_pw = os.path.join(tmp_dir, "ms_pw_junc")
        os.makedirs(os.path.join(fake_ms_pw, "chromium-1248"), exist_ok=True)
        junc_src = os.path.join(fake_ms_pw, "chromium-1248", "chrome-win64")
        _create_junction(junc_src, real_c_dir)
        try:
            audit = classify_dll_provenance(app_clean, ms_playwright_dir=fake_ms_pw)
            assert audit["counts_by_origin"]["playwright_browsers"] == 0
            assert audit["counts_by_origin"]["external_path"] == 1
            assert "Linked/reparse path rejected" in (audit["records"][0]["rejection_reason"] or "")
            with pytest.raises(RuntimeError, match="Unverified DLL source found"):
                assert_dll_provenance_trusted(audit)
        finally:
            os.rmdir(junc_src)


def test_missing_expected_dll_rejected_when_complete_set_required():
    """Full build gate (require_complete_set=True) rejects incomplete DLL set."""
    with tempfile.TemporaryDirectory(prefix="qat_r13_01_missing_") as tmp_dir:
        os.makedirs(os.path.join(tmp_dir, "_internal"), exist_ok=True)
        audit = classify_dll_provenance(tmp_dir, require_complete_set=True)
        assert len(audit["missing_expected_dlls"]) == 31
        with pytest.raises(RuntimeError, match="Missing required baseline DLLs"):
            assert_dll_provenance_trusted(audit)


def test_classify_dll_provenance_negative_unverified_external_dll():
    """Verify that an untrusted external DLL is flagged as external_path and rejected."""
    with tempfile.TemporaryDirectory(prefix="qat_dll_neg_test_") as tmp_dir:
        internal_dir = os.path.join(tmp_dir, "_internal")
        os.makedirs(internal_dir, exist_ok=True)

        fake_dll_path = os.path.join(internal_dir, "rogue_injected_module.dll")
        with open(fake_dll_path, "wb") as f:
            f.write(b"MZ\x90\x00FAKE_MALICIOUS_OR_UNVERIFIED_BINARY_PAYLOAD_TEST_DATA")

        audit = classify_dll_provenance(tmp_dir)
        assert audit["counts_by_origin"]["external_path"] == 1
        with pytest.raises(RuntimeError, match="Unverified DLL source found"):
            assert_dll_provenance_trusted(audit)
