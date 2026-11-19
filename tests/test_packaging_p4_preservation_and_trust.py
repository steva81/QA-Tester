"""Positive and negative regression tests for P4 (r10-r13 preservation & trust) and --reuse-app-from containment."""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from installer.build_release_1_0 import (
    PRESERVED_CANDIDATES,
    main as build_main,
    reuse_verified_candidate_app,
    sha256_file,
    to_long_path,
    validate_reuse_candidate,
    verify_preserved_candidates,
)


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest().upper()


def _seed_synthetic_reuse_repo(root_dir: str, cand_name: str = "dist_candidate_synth_ok"):
    cand_dir = os.path.join(root_dir, cand_name)
    app_dir = os.path.join(cand_dir, "QA-Tester")
    internal_dir = os.path.join(app_dir, "_internal")
    os.makedirs(internal_dir, exist_ok=True)

    exe_bytes = b"MZ_SYNTH_APP_EXE"
    uninst_bytes = b"MZ_SYNTH_UNINST_EXE"
    dll_bytes = b"MZ_SYNTH_INTERNAL_DLL"

    with open(os.path.join(app_dir, "QA-Tester.exe"), "wb") as f:
        f.write(exe_bytes)
    with open(os.path.join(app_dir, "QA-Tester-Uninstall.exe"), "wb") as f:
        f.write(uninst_bytes)
    with open(os.path.join(internal_dir, "python312.dll"), "wb") as f:
        f.write(dll_bytes)

    manifest_obj = {
        "candidate": cand_name,
        "version": "1.0.0",
        "exe_sha256": _sha256_bytes(exe_bytes),
        "files": [
            {"relative_path": "QA-Tester.exe", "sha256": _sha256_bytes(exe_bytes)},
            {"relative_path": "QA-Tester-Uninstall.exe", "sha256": _sha256_bytes(uninst_bytes)},
            {"relative_path": "_internal/python312.dll", "sha256": _sha256_bytes(dll_bytes)},
        ],
    }
    manifest_path = os.path.join(cand_dir, "package_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as mf:
        json.dump(manifest_obj, mf, indent=2)

    registry = {
        cand_name: {
            "exe": os.path.join(cand_name, "QA-Tester", "QA-Tester.exe"),
            "sha256": _sha256_bytes(exe_bytes),
            "manifest": os.path.join(cand_name, "package_manifest.json"),
            "manifest_sha256": sha256_file(manifest_path),
        }
    }
    return cand_dir, app_dir, manifest_path, manifest_obj, registry


def test_preserved_candidates_contains_r13_r12_r11_with_correct_hashes():
    for required_key in (
        "dist_installer_1.0.0_independent_r13",
        "dist_candidate_20261106_v1_0_r13",
        "dist_installer_1.0.0_independent_r12",
        "dist_candidate_20261103_v1_0_independent_r12",
        "dist_installer_1.0.0_independent_r11",
        "dist_candidate_20261102_v1_0_independent_r11",
    ):
        assert required_key in PRESERVED_CANDIDATES

    r13_setup = PRESERVED_CANDIDATES["dist_installer_1.0.0_independent_r13"]
    r13_app = PRESERVED_CANDIDATES["dist_candidate_20261106_v1_0_r13"]
    assert r13_setup["sha256"] == "4C78B3B86808CD8B0A31FCB618ECC3E8D928C7009177F8B865BC6127E88759A4"
    assert r13_app["sha256"] == "494D1A04CD544857328959AC837438329F735C4E15DB93C0348728BD598FEEC7"
    assert r13_app["manifest_sha256"] == "0CDE68C16400F6A64300C8B040B4E325D6988A2DB557BDABE0D62E68E76F4C9A"

    r12_setup = PRESERVED_CANDIDATES["dist_installer_1.0.0_independent_r12"]
    r12_app = PRESERVED_CANDIDATES["dist_candidate_20261103_v1_0_independent_r12"]
    assert r12_setup["sha256"] == "9D936EC6F70B0D6FA3970F38E5B556F15BEEB9EAA3D85E088983EAA056C72C27"
    assert r12_app["sha256"] == "812D43F18E34D69C0AC577658A5A065896C869B0924D38B22F8523E7D28BC532"
    assert r12_app["manifest_sha256"] == "2D1DA057F84727CE6E9B17A15A60DF1080927BD3A913BD349D3E2808B2CA6D23"


def test_verify_preserved_candidates_passes_all_checks():
    results = verify_preserved_candidates()
    assert "dist_installer_1.0.0_independent_r13" in results
    assert "dist_candidate_20261106_v1_0_r13" in results
    assert "dist_installer_1.0.0_independent_r12" in results
    assert "dist_candidate_20261103_v1_0_independent_r12" in results
    for cand_name, data in results.items():
        assert data["exe_sha256"] == PRESERVED_CANDIDATES[cand_name]["sha256"]


def test_build_script_source_contains_r11_r12_and_r13_in_trusted_releases():
    build_script_path = os.path.join(REPO_ROOT, "installer", "build_release_1_0.py")
    with open(build_script_path, "r", encoding="utf-8") as f:
        src = f.read()

    assert '"dist_candidate_20261102_v1_0_independent_r11"' in src
    assert '"dist_candidate_20261103_v1_0_independent_r12"' in src
    assert '"dist_candidate_20261106_v1_0_r13"' in src


def test_reuse_verified_candidate_app_positive_and_real_r13_preflight():
    short_root = tempfile.mkdtemp(prefix="q14ru_")
    try:
        # 1. Real sealed r13 candidate passes preflight validation without writing anything
        non_existent_dest = os.path.join(short_root, "never_written", "QA-Tester")
        plan = validate_reuse_candidate("dist_candidate_20261106_v1_0_r13", non_existent_dest)
        assert len(plan) == 866
        assert not os.path.exists(os.path.dirname(non_existent_dest))

        # 2. Synthetic sealed candidate copies allowed files and skips old uninstaller
        _cand_dir, _app_dir, _mpath, _mobj, registry = _seed_synthetic_reuse_repo(short_root)
        dest_app = os.path.join(short_root, "out_dist", "QA-Tester")
        res = reuse_verified_candidate_app(
            "dist_candidate_synth_ok",
            dest_app,
            repo_root=short_root,
            preserved_registry=registry,
        )
        assert res["copied_files"] == 2
        assert os.path.isfile(os.path.join(dest_app, "QA-Tester.exe"))
        assert os.path.isfile(os.path.join(dest_app, "_internal", "python312.dll"))
        assert not os.path.exists(os.path.join(dest_app, "QA-Tester-Uninstall.exe"))
    finally:
        shutil.rmtree(to_long_path(short_root), ignore_errors=True)


@pytest.mark.parametrize(
    "bad_reuse_arg",
    [
        "../dist_candidate_20261106_v1_0_r13",
        "dist_candidate_20261106_v1_0_r13/sub",
        r"C:\dist_candidate_20261106_v1_0_r13",
        "dist_candidate_unapproved_unknown",
        "dist_installer_1.0.0_independent_r13",  # installer entry has no app manifest
    ],
)
def test_reuse_rejects_invalid_or_unapproved_candidate_names_before_first_write(bad_reuse_arg):
    short_root = tempfile.mkdtemp(prefix="q14rn_")
    try:
        dest_parent = os.path.join(short_root, "untouched_parent")
        dest_app = os.path.join(dest_parent, "QA-Tester")
        with pytest.raises(ValueError):
            reuse_verified_candidate_app(bad_reuse_arg, dest_app)
        assert not os.path.exists(dest_parent)
    finally:
        shutil.rmtree(to_long_path(short_root), ignore_errors=True)


def test_reuse_rejects_tampered_manifest_and_tampered_source_file_before_first_write():
    short_root = tempfile.mkdtemp(prefix="q14rt_")
    try:
        _cand_dir, app_dir, manifest_path, manifest_obj, registry = _seed_synthetic_reuse_repo(short_root)
        dest_parent = os.path.join(short_root, "untouched_out")
        dest_app = os.path.join(dest_parent, "QA-Tester")

        # Tamper manifest on disk without updating sealed registry manifest_sha256
        tampered_manifest = dict(manifest_obj, version="9.9.9")
        with open(manifest_path, "w", encoding="utf-8") as mf:
            json.dump(tampered_manifest, mf)

        with pytest.raises(ValueError, match="manifest SHA-256 mismatch"):
            reuse_verified_candidate_app(
                "dist_candidate_synth_ok",
                dest_app,
                repo_root=short_root,
                preserved_registry=registry,
            )
        assert not os.path.exists(dest_parent)

        # Restore manifest, then tamper source DLL bytes on disk
        with open(manifest_path, "w", encoding="utf-8") as mf:
            json.dump(manifest_obj, mf, indent=2)
        with open(os.path.join(app_dir, "_internal", "python312.dll"), "wb") as df:
            df.write(b"MZ_CORRUPTED_DLL")

        with pytest.raises(ValueError, match="hash mismatch"):
            reuse_verified_candidate_app(
                "dist_candidate_synth_ok",
                dest_app,
                repo_root=short_root,
                preserved_registry=registry,
            )
        assert not os.path.exists(dest_parent)
    finally:
        shutil.rmtree(to_long_path(short_root), ignore_errors=True)


@pytest.mark.parametrize(
    "malicious_rel_path",
    [
        "../outside.dll",
        "_internal/../../outside.dll",
        r"_internal\..\..\outside.dll",
        "/outside.dll",
        "C:/outside.dll",
        r"\\server\share\outside.dll",
        "extra.txt",
        "install_receipt.json",
        "_internal/CON",
        "_internal/evil:stream",
    ],
)
def test_reuse_rejects_traversal_absolute_and_unknown_namespace_entries_before_first_write(malicious_rel_path):
    """Even if manifest_sha256 matched in a registry, traversal, absolute paths, and unknown files are rejected before any write."""
    short_root = tempfile.mkdtemp(prefix="q14rm_")
    try:
        _cand_dir, app_dir, manifest_path, manifest_obj, registry = _seed_synthetic_reuse_repo(short_root)
        bad_bytes = b"PAYLOAD"
        # Also place extra.txt if testing unknown top-level file so we verify namespace rejection specifically
        if malicious_rel_path in ("extra.txt", "install_receipt.json"):
            with open(os.path.join(app_dir, malicious_rel_path), "wb") as ef:
                ef.write(bad_bytes)

        manifest_obj["files"].append(
            {"relative_path": malicious_rel_path, "sha256": _sha256_bytes(bad_bytes)}
        )
        with open(manifest_path, "w", encoding="utf-8") as mf:
            json.dump(manifest_obj, mf, indent=2)
        registry["dist_candidate_synth_ok"]["manifest_sha256"] = sha256_file(manifest_path)

        dest_parent = os.path.join(short_root, "never_created_parent")
        dest_app = os.path.join(dest_parent, "QA-Tester")
        outside_probe = os.path.join(dest_parent, "outside.dll")

        with pytest.raises(ValueError):
            reuse_verified_candidate_app(
                "dist_candidate_synth_ok",
                dest_app,
                repo_root=short_root,
                preserved_registry=registry,
            )
        assert not os.path.exists(dest_parent)
        assert not os.path.exists(outside_probe)
    finally:
        shutil.rmtree(to_long_path(short_root), ignore_errors=True)


def test_reuse_rejects_unmanifested_extra_file_in_source_directory():
    short_root = tempfile.mkdtemp(prefix="q14rx_")
    try:
        _cand_dir, app_dir, _manifest_path, _manifest_obj, registry = _seed_synthetic_reuse_repo(short_root)
        with open(os.path.join(app_dir, "_internal", "sneaky_unlisted.dll"), "wb") as sf:
            sf.write(b"MZ_UNLISTED")
        dest_parent = os.path.join(short_root, "never_created")
        with pytest.raises(ValueError, match="Unmanifested extra file"):
            reuse_verified_candidate_app(
                "dist_candidate_synth_ok",
                os.path.join(dest_parent, "QA-Tester"),
                repo_root=short_root,
                preserved_registry=registry,
            )
        assert not os.path.exists(dest_parent)
    finally:
        shutil.rmtree(to_long_path(short_root), ignore_errors=True)


def test_build_main_rejects_unapproved_reuse_before_creating_candidate_dirs():
    cand_name = "dist_candidate_reuse_neg_probe_never_created"
    inst_name = "dist_installer_reuse_neg_probe_never_created"
    cand_path = os.path.join(REPO_ROOT, cand_name)
    inst_path = os.path.join(REPO_ROOT, inst_name)
    assert not os.path.exists(cand_path)
    assert not os.path.exists(inst_path)

    with pytest.raises(ValueError):
        build_main(
            [
                "--candidate-name",
                cand_name,
                "--installer-name",
                inst_name,
                "--reuse-app-from",
                "../outside_candidate",
            ]
        )
    assert not os.path.exists(cand_path)
    assert not os.path.exists(inst_path)
