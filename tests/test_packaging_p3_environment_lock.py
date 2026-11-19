"""Real positive and negative integration tests for R13-02: Build environment and version lock."""

import os
import subprocess
import sys
import tempfile
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from installer.build_release_1_0 import (
    ISOLATED_ENV_VARS_TO_STRIP,
    REQUIRED_BUILD_PACKAGES,
    REQUIRED_PYTHON_VERSION,
    get_controlled_env,
    main as build_main,
    resolve_target_python,
    verify_build_environment,
)


def test_requirements_file_matches_locked_build_dependencies():
    """requirements.txt must match REQUIRED_BUILD_PACKAGES with zero hidden version drift."""
    req_path = os.path.join(REPO_ROOT, "requirements.txt")
    assert os.path.isfile(req_path)
    pinned = {}
    with open(req_path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name, ver = line.split("==", 1)
            pinned[name.strip().lower()] = ver.strip()

    for pkg, expected_ver in REQUIRED_BUILD_PACKAGES.items():
        assert pinned.get(pkg.lower()) == expected_ver, (
            f"requirements.txt mismatch for {pkg}: expected {expected_ver}, got {pinned.get(pkg.lower())}"
        )


def test_verify_build_environment_positive_current_interpreter():
    """Real preflight probe succeeds on the active verified Python 3.12.10 + Playwright 1.64.1 venv."""
    audit = verify_build_environment()
    assert audit["python_version"] == REQUIRED_PYTHON_VERSION
    assert os.path.normcase(audit["python_executable"]) == os.path.normcase(os.path.abspath(sys.executable))
    assert audit["package_versions"]["playwright"] == "1.64.1"
    assert audit["package_versions"]["pyinstaller"] == "6.22.3"
    assert audit["package_versions"]["pytest"] == "9.1.1"
    assert audit["browser_revisions"]["chromium"]["revision"] == "1248"
    assert audit["browser_revisions"]["chromium-headless-shell"]["revision"] == "1248"
    for key in ("python_executable", "playwright_node_exe", "chromium_exe", "headless_shell_exe"):
        assert len(audit["binary_sha256"][key]) == 64


def test_codex_counterexample_legacy_venv_rejected_before_creating_directories():
    """Mandatory Codex R13-02 counterexample: calling from Playwright 1.64.1 with --venv-dir venv (1.50.0) MUST fail before directory creation."""
    legacy_venv = os.path.join(REPO_ROOT, "venv")
    assert os.path.isdir(legacy_venv), f"Expected preserved legacy venv at {legacy_venv}"

    cand_name = "dist_r13_02_must_not_be_created_app"
    inst_name = "dist_r13_02_must_not_be_created_inst"
    cand_path = os.path.join(REPO_ROOT, cand_name)
    inst_path = os.path.join(REPO_ROOT, inst_name)
    assert not os.path.exists(cand_path)
    assert not os.path.exists(inst_path)

    # 1. Direct integration call to build_release_1_0.main(...)
    with pytest.raises(RuntimeError, match=r"playwright: expected 1\.64\.1, got 1\.50\.0"):
        build_main(
            [
                "--candidate-name",
                cand_name,
                "--installer-name",
                inst_name,
                "--venv-dir",
                legacy_venv,
            ]
        )

    assert not os.path.exists(cand_path), "Candidate directory must NOT be created when environment check fails"
    assert not os.path.exists(inst_path), "Installer directory must NOT be created when environment check fails"

    # 2. Subprocess CLI invocation confirming non-zero exit code and zero output artifacts
    proc = subprocess.run(
        [
            sys.executable,
            os.path.join(REPO_ROOT, "installer", "build_release_1_0.py"),
            "--candidate-name",
            cand_name,
            "--installer-name",
            inst_name,
            "--venv-dir",
            legacy_venv,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode != 0
    combined = proc.stdout + "\n" + proc.stderr
    assert "playwright: expected 1.64.1, got 1.50.0" in combined
    assert not os.path.exists(cand_path)
    assert not os.path.exists(inst_path)


def test_poisoned_pythonpath_and_pythonhome_are_isolated(monkeypatch):
    """Ambient PYTHONPATH, PYTHONHOME, and PYTHONUSERBASE must be stripped so poisoned modules cannot hijack the build."""
    with tempfile.TemporaryDirectory(prefix="qat_r13_02_poison_") as poison_dir:
        fake_pw = os.path.join(poison_dir, "playwright")
        os.makedirs(fake_pw, exist_ok=True)
        with open(os.path.join(fake_pw, "__init__.py"), "w", encoding="utf-8") as f:
            f.write('raise RuntimeError("POISONED_PLAYWRIGHT_IMPORTED")\n')

        monkeypatch.setenv("PYTHONPATH", poison_dir)
        monkeypatch.setenv("PYTHONHOME", poison_dir)
        monkeypatch.setenv("PYTHONUSERBASE", poison_dir)
        monkeypatch.setenv("PYTHONSTARTUP", os.path.join(poison_dir, "startup.py"))

        env, controlled_paths = get_controlled_env()
        for var_name in ISOLATED_ENV_VARS_TO_STRIP:
            assert var_name not in env, f"{var_name} leaked into controlled env"
        assert env["PYTHONNOUSERSITE"] == "1"
        assert poison_dir not in env["PATH"]
        for p in controlled_paths:
            assert os.path.isdir(p)

        # Full subprocess probe must succeed cleanly despite poisoned caller environment
        audit = verify_build_environment()
        assert audit["package_versions"]["playwright"] == "1.64.1"


def test_venv_with_system_site_packages_enabled_rejected():
    """A venv configured with include-system-site-packages = true must be rejected fail-closed."""
    with tempfile.TemporaryDirectory(prefix="qat_r13_02_syssp_") as fake_venv:
        scripts_dir = os.path.join(fake_venv, "Scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        with open(os.path.join(scripts_dir, "python.exe"), "wb") as f:
            f.write(b"MZ")
        with open(os.path.join(fake_venv, "pyvenv.cfg"), "w", encoding="utf-8") as cfg:
            cfg.write("home = C:\\Python312\ninclude-system-site-packages = true\nversion = 3.12.10\n")

        with pytest.raises(RuntimeError, match="include-system-site-packages = false"):
            verify_build_environment(fake_venv)


def test_missing_venv_python_rejected_before_build():
    """Selecting a directory without Scripts/python.exe raises FileNotFoundError immediately."""
    with tempfile.TemporaryDirectory(prefix="qat_r13_02_nop_") as empty_dir:
        with pytest.raises(FileNotFoundError, match="Target Python interpreter not found"):
            resolve_target_python(empty_dir)


def test_build_script_uses_python_dash_m_pyinstaller_not_pyinstaller_exe():
    """Ensure build_release_1_0.py never invokes standalone pyinstaller.exe."""
    src_path = os.path.join(REPO_ROOT, "installer", "build_release_1_0.py")
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "pyinstaller.exe" not in src.lower()
    assert '"PyInstaller"' in src
