"""Positive and negative regression tests for R13-03: Windows path length policy, staging/backup headroom, and UNC rejection."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from installer import setup_installer as setup, uninstaller as uninstall
from installer.build_release_1_0 import to_long_path
from installer.path_safety import (
    MAX_WIN32_PATH_LENGTH,
    checked_path,
    fs_path,
    member_path,
)
from installer.shortcut_helper import create_windows_shortcut

REAL_LONGEST_REL_MEMBER = (
    "_internal/playwright_browsers/chromium_headless_shell-1248/"
    "chrome-headless-shell-win64/resources/accessibility/"
    "reading_mode_gdocs_helper/gdocs_script.js"
)
assert len(REAL_LONGEST_REL_MEMBER) == 152


def _make_payload_zip(zip_path: str, exe_bytes: bytes, candidate_name: str = "r14_test_cand"):
    uninst_bytes = b"MZ_UNINSTALLER_" + candidate_name.encode("ascii")
    leaf_bytes = b"// gdocs_script leaf content " + candidate_name.encode("ascii")
    files = [
        {"relative_path": "QA-Tester.exe", "sha256": hashlib.sha256(exe_bytes).hexdigest().upper()},
        {"relative_path": "QA-Tester-Uninstall.exe", "sha256": hashlib.sha256(uninst_bytes).hexdigest().upper()},
        {"relative_path": REAL_LONGEST_REL_MEMBER, "sha256": hashlib.sha256(leaf_bytes).hexdigest().upper()},
    ]
    manifest = {
        "candidate": candidate_name,
        "version": "1.0.0",
        "exe_sha256": files[0]["sha256"],
        "files": files,
    }
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("QA-Tester.exe", exe_bytes)
        zf.writestr("QA-Tester-Uninstall.exe", uninst_bytes)
        zf.writestr(REAL_LONGEST_REL_MEMBER, leaf_bytes)
        zf.writestr("package_manifest.json", json.dumps(manifest))
    return manifest, files


@pytest.mark.parametrize(
    "unc_input",
    [
        r"\\server\share\file",
        "//server/share/file",
        r"\\?\UNC\server\share\file",
        r"\\?\\\server\share\file",
        r"\\.\PhysicalDrive0",
    ],
)
def test_codex_counterexample_to_long_path_rejects_unc_and_device_paths(unc_input):
    """Mandatory Codex R13-03 counterexample: to_long_path must reject UNC/device paths, never producing \\\\?\\\\\\server."""
    with pytest.raises(ValueError):
        to_long_path(unc_input)
    with pytest.raises(ValueError):
        checked_path(unc_input)
    with pytest.raises(ValueError):
        fs_path(unc_input)


def test_to_long_path_idempotent_on_valid_local_drive_path(tmp_path):
    p = str(tmp_path / "subdir" / "item.txt")
    lp1 = to_long_path(p)
    lp2 = to_long_path(lp1)
    assert lp1 == lp2
    assert lp1.startswith("\\\\?\\")
    assert not lp1.startswith("\\\\?\\\\")


def test_short_normal_destination_install_shortcut_and_uninstall(monkeypatch):
    """Short normal destination succeeds end-to-end including shortcuts, manifest verification, and uninstall."""
    monkeypatch.setattr(setup, "check_webview2_runtime", lambda **kw: (True, "test", None))
    short_root = tempfile.mkdtemp(prefix="q14s_")
    try:
        archive = os.path.join(short_root, "payload.zip")
        _manifest, files = _make_payload_zip(archive, b"MZ_SHORT_APP")
        monkeypatch.setattr(uninstall, "_owned_files", lambda: files)

        install_dir = os.path.join(short_root, "Programs", "QATester")
        menu_dir = os.path.join(short_root, "StartMenu", "QA Tester")
        data_dir = os.path.join(short_root, "LocalData", "QATester")

        res = setup.run_install(
            install_dir=install_dir,
            start_menu_dir=menu_dir,
            user_data_dir=data_dir,
            registry_key="",
            payload_zip=archive,
            create_shortcuts=True,
        )
        assert res["status"] == "success"
        assert not res["receipt"]["install_dir"].startswith("\\\\?\\")
        assert os.path.isfile(os.path.join(menu_dir, "QA Tester.lnk"))

        un_res = uninstall.run_uninstall(
            install_dir=install_dir,
            start_menu_dir=menu_dir,
            user_data_dir=data_dir,
            registry_key="",
        )
        assert un_res["status"] == "success"
        assert not os.path.exists(install_dir)
    finally:
        shutil.rmtree("\\\\?\\" + short_root, ignore_errors=True)


def test_exact_max_path_259_boundary_succeeds_across_staging_upgrade_rollback_and_uninstall(monkeypatch):
    """At exact 259-char installed leaf (where .qat-stage-* reaches ~275 chars and .qat-backup reaches 271 chars), all flows succeed."""
    monkeypatch.setattr(setup, "check_webview2_runtime", lambda **kw: (True, "test", None))

    # Build a short base in C:\Users\...\Temp so we can dial len(install_dir) to exact 106 chars (106 + 1 + 152 == 259)
    short_root = tempfile.mkdtemp(prefix="q14b_")
    try:
        target_install_len = MAX_WIN32_PATH_LENGTH - 1 - len(REAL_LONGEST_REL_MEMBER)  # 106 chars
        app_leaf = "app"
        prefix_needed = target_install_len - len(short_root) - 1 - len(app_leaf) - 1
        assert prefix_needed >= 4, f"Unexpectedly long TEMP root ({len(short_root)}): {short_root}"
        parent_dir = os.path.join(short_root, "p" * prefix_needed)
        install_dir = os.path.join(parent_dir, app_leaf)
        assert len(install_dir) + 1 + len(REAL_LONGEST_REL_MEMBER) == MAX_WIN32_PATH_LENGTH

        menu_dir = os.path.join(short_root, "m")
        data_dir = os.path.join(short_root, "d")
        archive_v1 = os.path.join(short_root, "v1.zip")
        archive_v2 = os.path.join(short_root, "v2.zip")
        rel_v1, files_v1 = _make_payload_zip(archive_v1, b"MZ_BOUNDARY_V1", "cand_v1")
        rel_v2, files_v2 = _make_payload_zip(archive_v2, b"MZ_BOUNDARY_V2", "cand_v2")

        monkeypatch.setattr(setup, "_trusted_releases", lambda: [rel_v1, rel_v2])
        monkeypatch.setattr(uninstall, "_owned_files", lambda: files_v2)

        # 1. Fresh install at exact 259-char leaf (staging leaf is > 270 chars!)
        res1 = setup.run_install(
            install_dir=install_dir,
            start_menu_dir=menu_dir,
            user_data_dir=data_dir,
            registry_key="",
            payload_zip=archive_v1,
            create_shortcuts=False,
        )
        assert res1["status"] == "success"
        assert os.path.isfile(fs_path(member_path(install_dir, REAL_LONGEST_REL_MEMBER)))

        # 2. Simulated failure mid-upgrade rolls back cleanly from .app.qat-backup (leaf > 270 chars!)
        orig_verify = setup._verify_installed_manifest
        calls = {"n": 0}

        def fail_on_second_verify(d):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("Simulated post-activation failure at boundary")
            return orig_verify(d)

        monkeypatch.setattr(setup, "_verify_installed_manifest", fail_on_second_verify)
        with pytest.raises(RuntimeError, match="Simulated post-activation failure"):
            setup.run_install(
                install_dir=install_dir,
                start_menu_dir=menu_dir,
                user_data_dir=data_dir,
                registry_key="",
                payload_zip=archive_v2,
                create_shortcuts=False,
            )
        monkeypatch.setattr(setup, "_verify_installed_manifest", orig_verify)
        assert open(fs_path(member_path(install_dir, "QA-Tester.exe")), "rb").read() == b"MZ_BOUNDARY_V1"

        # 3. Real upgrade v1 -> v2 at exact 259-char boundary succeeds
        res2 = setup.run_install(
            install_dir=install_dir,
            start_menu_dir=menu_dir,
            user_data_dir=data_dir,
            registry_key="",
            payload_zip=archive_v2,
            create_shortcuts=False,
        )
        assert res2["status"] == "success"
        assert res2["receipt"]["upgrade_mode"] == "upgrade"
        assert open(fs_path(member_path(install_dir, "QA-Tester.exe")), "rb").read() == b"MZ_BOUNDARY_V2"

        # 4. Uninstall at exact 259-char boundary removes app completely and preserves user data
        un_res = uninstall.run_uninstall(
            install_dir=install_dir,
            start_menu_dir=menu_dir,
            user_data_dir=data_dir,
            registry_key="",
        )
        assert un_res["status"] == "success"
        assert not os.path.exists(fs_path(install_dir))
        assert os.path.isdir(fs_path(data_dir))
    finally:
        shutil.rmtree("\\\\?\\" + short_root, ignore_errors=True)


@pytest.mark.parametrize("extra_chars", [1, 100])
def test_codex_counterexample_overlong_destination_260_and_359_rejected_before_any_mutation(
    monkeypatch, extra_chars
):
    """Mandatory Codex R13-03 counterexample: 260-char and 359-char destinations are rejected BEFORE any directory/file is created."""
    monkeypatch.setattr(setup, "check_webview2_runtime", lambda **kw: (True, "test", None))
    short_root = tempfile.mkdtemp(prefix="q14o_")
    try:
        archive = os.path.join(short_root, "payload.zip")
        _make_payload_zip(archive, b"MZ_OVERLONG_TEST")

        base_install_len = MAX_WIN32_PATH_LENGTH - 1 - len(REAL_LONGEST_REL_MEMBER) + extra_chars
        non_existent_parent = os.path.join(short_root, "sub_" + "d" * (base_install_len - len(short_root) - 9))
        install_dir = os.path.join(non_existent_parent, "app")
        leaf_len = len(install_dir) + 1 + len(REAL_LONGEST_REL_MEMBER)
        assert leaf_len == MAX_WIN32_PATH_LENGTH + extra_chars

        menu_dir = os.path.join(short_root, "menu")
        data_dir = os.path.join(short_root, "data")
        result_json = os.path.join(short_root, "res.json")

        with pytest.raises(ValueError, match="MAX_PATH"):
            setup.run_install(
                install_dir=install_dir,
                start_menu_dir=menu_dir,
                user_data_dir=data_dir,
                registry_key="",
                payload_zip=archive,
                create_shortcuts=False,
            )

        # Verify zero partial installation or parent/staging/user_data directory creation!
        assert not os.path.exists("\\\\?\\" + non_existent_parent)
        assert not os.path.exists("\\\\?\\" + menu_dir)
        assert not os.path.exists("\\\\?\\" + data_dir)

        # Also verify CLI entrypoint reports error status and exit code 1
        rc = setup.main(
            [
                "--silent",
                "--no-shortcuts",
                "--install-dir",
                install_dir,
                "--start-menu-dir",
                menu_dir,
                "--user-data-dir",
                data_dir,
                "--registry-key",
                "",
                "--payload-zip",
                archive,
                "--result-json",
                result_json,
            ]
        )
        assert rc == 1
        res_payload = json.loads(open(result_json, "r", encoding="utf-8").read())
        assert res_payload["status"] == "error"
        assert "MAX_PATH" in res_payload["message"]
        assert not os.path.exists("\\\\?\\" + non_existent_parent)
    finally:
        shutil.rmtree("\\\\?\\" + short_root, ignore_errors=True)


@pytest.mark.parametrize(
    "unsafe_member",
    [
        "../outside.dll",
        "/absolute.dll",
        "C:/outside.dll",
        "_internal/../../outside.dll",
        "_internal/CON",
        "_internal/x:stream",
    ],
)
def test_unsafe_zip_members_and_unc_install_dir_rejected_before_creating_parent(tmp_path, monkeypatch, unsafe_member):
    """Traversal, absolute, ADS, and device archive members as well as UNC destinations fail before creating parent/staging."""
    monkeypatch.setattr(setup, "check_webview2_runtime", lambda **kw: (True, "test", None))
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr(unsafe_member, b"bad")

    non_existent_parent = tmp_path / "never_created_parent"
    install_dir = non_existent_parent / "QATester"
    with pytest.raises(ValueError):
        setup.run_install(
            install_dir=str(install_dir),
            start_menu_dir=str(tmp_path / "menu"),
            user_data_dir=str(tmp_path / "data"),
            registry_key="",
            payload_zip=str(bad_zip),
            create_shortcuts=False,
        )
    assert not non_existent_parent.exists()

    with pytest.raises(ValueError):
        setup.run_install(
            install_dir=r"\\server\share\QATester",
            start_menu_dir=str(tmp_path / "menu"),
            user_data_dir=str(tmp_path / "data"),
            registry_key="",
            payload_zip=str(bad_zip),
            create_shortcuts=False,
        )


def test_shortcut_helper_rejects_overlong_target_before_wscript_truncation(tmp_path):
    """Shortcut helper must reject >=260-char targets fail-closed rather than allowing WScript.Shell truncation."""
    overlong_target = os.path.join(str(tmp_path), "x" * 240, "QA-Tester.exe")
    assert len(overlong_target) > 259
    with pytest.raises(ValueError, match="MAX_PATH"):
        create_windows_shortcut(str(tmp_path / "test.lnk"), overlong_target)
