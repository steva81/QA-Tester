"""Controlled, reproducible Windows build pipeline for QA Tester 1.0 and Setup Installer.

Builds:
1. dist_candidate_20261028_v1_0/QA-Tester/QA-Tester.exe (+ QA-Tester-Uninstall.exe & package_manifest.json)
2. dist_installer_1.0.0/QA-Tester-Setup-1.0.0.exe (+ installer_manifest.json)

Guarantees:
- Preserves dist_candidate_20261027_fix, dist_candidate_20261028_v1, and dist_candidate_20261028_v2 untouched.
- Sanitizes PATH so PyInstaller bindepend never pulls CRT/API-set DLLs from Eclipse Adoptium JDK or other arbitrary PATH folders.
- Audits all packaged DLLs and their provenance.
"""

from datetime import datetime, timezone
import argparse
import base64
import csv
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple
import zipfile

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TRUSTED_DLL_BASELINE_PATH = os.path.join(os.path.dirname(__file__), "trusted_dll_baseline.json")
TRUSTED_DLL_BASELINE_SHA256 = "9AEA7121DF6413B4A14B39FBDE8D1B64720920B7C75EB9DEBC4A73CB6C8690EF"

PRESERVED_CANDIDATES = {
    "dist_installer_1.0.0_independent_r13": {
        "exe": r"dist_installer_1.0.0_independent_r13\QA-Tester-Setup-1.0.0.exe",
        "sha256": "4C78B3B86808CD8B0A31FCB618ECC3E8D928C7009177F8B865BC6127E88759A4",
    },
    "dist_candidate_20261106_v1_0_r13": {
        "exe": r"dist_candidate_20261106_v1_0_r13\QA-Tester\QA-Tester.exe",
        "sha256": "494D1A04CD544857328959AC837438329F735C4E15DB93C0348728BD598FEEC7",
        "manifest": r"dist_candidate_20261106_v1_0_r13\package_manifest.json",
        "manifest_sha256": "0CDE68C16400F6A64300C8B040B4E325D6988A2DB557BDABE0D62E68E76F4C9A",
    },
    "dist_installer_1.0.0_independent_r12": {
        "exe": r"dist_installer_1.0.0_independent_r12\QA-Tester-Setup-1.0.0.exe",
        "sha256": "9D936EC6F70B0D6FA3970F38E5B556F15BEEB9EAA3D85E088983EAA056C72C27",
    },
    "dist_candidate_20261103_v1_0_independent_r12": {
        "exe": r"dist_candidate_20261103_v1_0_independent_r12\QA-Tester\QA-Tester.exe",
        "sha256": "812D43F18E34D69C0AC577658A5A065896C869B0924D38B22F8523E7D28BC532",
        "manifest": r"dist_candidate_20261103_v1_0_independent_r12\package_manifest.json",
        "manifest_sha256": "2D1DA057F84727CE6E9B17A15A60DF1080927BD3A913BD349D3E2808B2CA6D23",
    },
    "dist_installer_1.0.0_independent_r11": {
        "exe": r"dist_installer_1.0.0_independent_r11\QA-Tester-Setup-1.0.0.exe",
        "sha256": "451F8FDD35B4035B1ADC070AD5F58AD383B1819BA1F56B97A0D7F486A74DD7D1",
    },
    "dist_candidate_20261102_v1_0_independent_r11": {
        "exe": r"dist_candidate_20261102_v1_0_independent_r11\QA-Tester\QA-Tester.exe",
        "sha256": "6DF1C27BBA283FE0B66D35DDA482E179A3C209AED43BE8DEC4C508304A22121F",
        "manifest": r"dist_candidate_20261102_v1_0_independent_r11\package_manifest.json",
        "manifest_sha256": "E0131F789081B93B611B51E449477C684EE0DCA94C10501861A8FA7D266AE516",
    },
    "dist_installer_1.0.0_independent_r10": {
        "exe": r"dist_installer_1.0.0_independent_r10\QA-Tester-Setup-1.0.0.exe",
        "sha256": "5410DCED7F212BF26F6CE1C42C9E8F40FCBE31B2A2B76341D00DC5BB91BEE447",
    },
    "dist_candidate_20261028_v1_0_independent_r10": {
        "exe": r"dist_candidate_20261028_v1_0_independent_r10\QA-Tester\QA-Tester.exe",
        "sha256": "FDD6ACFA332B0B0837E300D16EC4CF39A55DD74F1D78CC21D00BC0F6153F5C92",
        "manifest": r"dist_candidate_20261028_v1_0_independent_r10\package_manifest.json",
        "manifest_sha256": "B5FE5F1526484041B3B5B2F481268ED8A33CE8794FC77E62303A1BFB1ACFD0B1",
    },
    "dist_installer_1.0.0_independent_r9": {
        "exe": r"dist_installer_1.0.0_independent_r9\QA-Tester-Setup-1.0.0.exe",
        "sha256": "B596D8F47BE822C230184748C8A1AF57156B82133C1B1D63C5A7A013DB150EFF",
    },
    "dist_candidate_20261028_v1_0_independent_r9": {
        "exe": r"dist_candidate_20261028_v1_0_independent_r9\QA-Tester\QA-Tester.exe",
        "sha256": "FDD6ACFA332B0B0837E300D16EC4CF39A55DD74F1D78CC21D00BC0F6153F5C92",
        "manifest": r"dist_candidate_20261028_v1_0_independent_r9\package_manifest.json",
        "manifest_sha256": "1EB5BB3A08D49033BEB347AF169A1A7038AC9D34F3E139BA556AD2B18466F300",
    },
    "dist_candidate_20261027_fix": {
        "exe": r"dist_candidate_20261027_fix\QA-Tester\QA-Tester.exe",
        "sha256": "2A8B0B5B943C68E624654FCC3375BBA36385F3F8EF114D2D5C40560141B2BA10",
    },
    "dist_candidate_20261028_v1": {
        "exe": r"dist_candidate_20261028_v1\QA-Tester\QA-Tester.exe",
        "sha256": "A2849B903091DB58797963AA6F0D3DA3E1563A1D381F78D79AE4E78628D70CD0",
    },
    "dist_candidate_20261028_v2": {
        "exe": r"dist_candidate_20261028_v2\QA-Tester\QA-Tester.exe",
        "sha256": "21A78D60A19D013B690FD9A81317CB674D0F6EFA42EE79B3A8C9772BCA60431F",
        "manifest": r"dist_candidate_20261028_v2\package_manifest.json",
        "manifest_sha256": "3B817D344EF9EE2AC9E0F63A8AFD7D095B226A381F65A80106FDF8CC375E9D92",
    },
    "dist_candidate_20261028_v1_0": {
        "exe": r"dist_candidate_20261028_v1_0\QA-Tester\QA-Tester.exe",
        "sha256": "7C7BE658C8E54E51FC0A03DA6B44037756D90DB0AA417BEF4C8602E68EA21A6E",
        "manifest": r"dist_candidate_20261028_v1_0\package_manifest.json",
        "manifest_sha256": "1247F946E1B8751F5D9EE9004E76A437675C3EAA43375077D9B25CA22DDC0CA8",
    },
    "dist_installer_1.0.0": {
        "exe": r"dist_installer_1.0.0\QA-Tester-Setup-1.0.0.exe",
        "sha256": "6D2240C09F4BCDA753F598AFD6915AF91EB859068AE4379ABE15998DBA05E233",
    },
    "dist_candidate_20261028_v1_0_independent_r6": {
        "exe": r"dist_candidate_20261028_v1_0_independent_r6\QA-Tester\QA-Tester.exe",
        "sha256": "FDD6ACFA332B0B0837E300D16EC4CF39A55DD74F1D78CC21D00BC0F6153F5C92",
        "manifest": r"dist_candidate_20261028_v1_0_independent_r6\package_manifest.json",
        "manifest_sha256": "53008CAB765574CAA5C1E2460E64CD8DFB9C1D8B1682D3B9320C4E5C44FD7428",
    },
    "dist_installer_1.0.0_independent_r6": {
        "exe": r"dist_installer_1.0.0_independent_r6\QA-Tester-Setup-1.0.0.exe",
        "sha256": "1DDD3303ED84D325DF1003B9062A432E1143F3C5B75975D1DD2A6824E14B82DC",
    },
}


def to_long_path(p: str) -> str:
    """Convert a validated local Windows drive path to extended-length (\\\\?\\X:\\...), rejecting UNC/device paths."""
    raw = os.fspath(p)
    if not raw:
        raise ValueError("Empty path is not supported")
    if sys.platform != "win32":
        return os.path.abspath(raw)
    if raw.startswith("\\\\?\\"):
        rest = raw[4:]
        if rest.upper().startswith("UNC\\") or rest.startswith(("\\", "/")):
            raise ValueError(f"UNC paths are not supported: {raw}")
        if len(rest) < 3 or not rest[0].isalpha() or rest[1] != ":" or rest[2] not in ("\\", "/"):
            raise ValueError(f"Invalid extended-length local drive path: {raw}")
        return "\\\\?\\" + os.path.abspath(rest)
    if raw.startswith(("\\\\", "//")):
        raise ValueError(f"UNC and device paths are not supported: {raw}")
    abs_p = os.path.abspath(raw)
    if (
        abs_p.startswith(("\\\\", "//"))
        or len(abs_p) < 3
        or not abs_p[0].isalpha()
        or abs_p[1] != ":"
        or abs_p[2] != "\\"
    ):
        raise ValueError(f"Local Windows drive path required, got: {raw}")
    return "\\\\?\\" + abs_p


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(to_long_path(path), "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def verify_preserved_candidates() -> Dict[str, Any]:
    results = {}
    for name, expected in PRESERVED_CANDIDATES.items():
        exe_path = os.path.join(REPO_ROOT, expected["exe"])
        if not os.path.isfile(exe_path):
            raise RuntimeError(f"Preserved candidate missing: {exe_path}")
        actual_sha = sha256_file(exe_path)
        if actual_sha != expected["sha256"]:
            raise RuntimeError(
                f"Preserved candidate {name} hash mismatch! Expected {expected['sha256']}, got {actual_sha}"
            )
        item = {"exe": expected["exe"], "exe_sha256": actual_sha, "verified": True}
        if "manifest" in expected:
            m_path = os.path.join(REPO_ROOT, expected["manifest"])
            actual_m_sha = sha256_file(m_path)
            if actual_m_sha != expected["manifest_sha256"]:
                raise RuntimeError(
                    f"Preserved manifest {name} hash mismatch! Expected {expected['manifest_sha256']}, got {actual_m_sha}"
                )
            item["manifest_sha256"] = actual_m_sha
        results[name] = item
    return results


ISOLATED_ENV_VARS_TO_STRIP = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONEXECUTABLE",
    "__PYVENV_LAUNCHER__",
    "VIRTUAL_ENV",
)

REQUIRED_PYTHON_VERSION = "3.12.10"
REQUIRED_BUILD_PACKAGES: Dict[str, str] = {
    "pyinstaller": "6.22.3",
    "playwright": "1.64.1",
    "flask": "3.0.3",
    "pywebview": "4.4.1",
    "python-dotenv": "1.0.1",
    "graphql-core": "3.2.8",
    "python-docx": "1.2.0",
    "pillow": "12.3.0",
    "pythonnet": "3.2.1",
    "clr-loader": "0.3.1",
    "pytest": "9.1.1",
}


def resolve_target_python(venv_dir: Optional[str] = None) -> Tuple[str, str]:
    """Resolve and boundary-check (target_python_exe, resolved_venv_dir) as a single source of truth."""
    if venv_dir:
        resolved_venv = _assert_clean_local_path(os.path.abspath(venv_dir), check_hardlinks=False)
        target_python = os.path.join(resolved_venv, "Scripts", "python.exe")
    else:
        target_python = _assert_clean_local_path(os.path.abspath(sys.executable), check_hardlinks=False)
        venv_scripts = os.path.dirname(target_python)
        resolved_venv = _assert_clean_local_path(os.path.abspath(os.path.join(venv_scripts, "..")), check_hardlinks=False)

    if not os.path.isfile(to_long_path(target_python)):
        raise FileNotFoundError(f"Target Python interpreter not found: {target_python}")
    target_python = _assert_contained_within(resolved_venv, target_python, check_hardlinks=False)
    return target_python, resolved_venv


def get_controlled_env(venv_dir: Optional[str] = None) -> Tuple[Dict[str, str], List[str]]:
    if not venv_dir:
        venv_scripts = os.path.abspath(os.path.dirname(sys.executable))
        resolved_venv = os.path.abspath(os.path.join(venv_scripts, ".."))
    else:
        resolved_venv = os.path.abspath(venv_dir)
        venv_scripts = os.path.join(resolved_venv, "Scripts")

    pyvenv_cfg = {}
    cfg_file = os.path.join(resolved_venv, "pyvenv.cfg")
    if os.path.isfile(cfg_file):
        with open(cfg_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "=" in line:
                    k, v = line.split("=", 1)
                    pyvenv_cfg[k.strip().lower()] = v.strip()

    base_py = (
        os.path.abspath(pyvenv_cfg["home"])
        if pyvenv_cfg.get("home")
        else os.path.abspath(getattr(sys, "base_prefix", sys.prefix))
    )
    sys_root = os.environ.get("SystemRoot", r"C:\Windows")
    controlled_paths = [
        venv_scripts,
        base_py,
        os.path.join(base_py, "DLLs"),
        os.path.join(base_py, "Library", "bin"),
        os.path.join(sys_root, "System32"),
        sys_root,
        os.path.join(sys_root, "System32", "Wbem"),
        os.path.join(sys_root, "System32", "WindowsPowerShell", "v1.0"),
    ]
    env = os.environ.copy()
    for var_name in ISOLATED_ENV_VARS_TO_STRIP:
        env.pop(var_name, None)
    active_paths = [p for p in controlled_paths if os.path.isdir(p)]
    env["PATH"] = os.pathsep.join(active_paths)
    env["PYTHONUTF8"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    return env, active_paths


_ENV_PROBE_SCRIPT = r"""
import importlib
import importlib.metadata as md
import json
import os
import sys

expected_exe = os.environ["QAT_EXPECTED_PYTHON_EXE"]
expected_venv = os.environ["QAT_EXPECTED_VENV_DIR"]
required_py = os.environ["QAT_REQUIRED_PYTHON_VERSION"]
required_pkgs = json.loads(os.environ["QAT_REQUIRED_PACKAGES_JSON"])

actual_exe = os.path.abspath(sys.executable)
if os.path.normcase(actual_exe) != os.path.normcase(expected_exe):
    raise RuntimeError(f"Interpreter mismatch: expected {expected_exe}, got {actual_exe}")

actual_py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
if actual_py_ver != required_py:
    raise RuntimeError(f"Unsupported Python version: {actual_py_ver}. Expected {required_py}.")

allowed_roots = [os.path.normcase(os.path.abspath(expected_venv)), os.path.normcase(os.path.abspath(sys.base_prefix))]
leaked_sys_paths = []
for entry in sys.path:
    if not entry:
        continue
    abs_e = os.path.normcase(os.path.abspath(entry))
    if not any(abs_e == r or abs_e.startswith(r + os.sep) for r in allowed_roots):
        leaked_sys_paths.append(entry)
if leaked_sys_paths:
    raise RuntimeError(f"Unisolated sys.path entries detected in target interpreter: {leaked_sys_paths}")

measured_versions = {}
mismatches = []
for pkg, req_ver in required_pkgs.items():
    try:
        inst_ver = md.version(pkg)
    except md.PackageNotFoundError:
        inst_ver = None
    measured_versions[pkg] = inst_ver
    if inst_ver != req_ver:
        mismatches.append(f"{pkg}: expected {req_ver}, got {inst_ver}")

if mismatches:
    raise RuntimeError("Build environment dependency mismatch: " + "; ".join(mismatches))

sp_norm = os.path.normcase(os.path.join(os.path.abspath(expected_venv), "Lib", "site-packages"))
module_origins = {}
for mod_name in ("PyInstaller", "playwright", "flask", "webview", "docx", "PIL", "graphql", "dotenv", "clr_loader", "pythonnet"):
    mod = importlib.import_module(mod_name)
    mod_file = os.path.abspath(getattr(mod, "__file__", ""))
    if not os.path.normcase(mod_file).startswith(sp_norm + os.sep):
        raise RuntimeError(f"Module {mod_name} resolved outside target venv site-packages: {mod_file}")
    module_origins[mod_name] = mod_file

from playwright.sync_api import sync_playwright
import playwright

pw_pkg_dir = os.path.dirname(os.path.abspath(playwright.__file__))
node_exe = os.path.join(pw_pkg_dir, "driver", "node.exe")
browsers_json = os.path.join(pw_pkg_dir, "driver", "package", "browsers.json")
with open(browsers_json, "r", encoding="utf-8") as bf:
    b_spec = json.load(bf)

with sync_playwright() as pw:
    chromium_exe = os.path.abspath(pw.chromium.executable_path)

ms_pw_dir = os.path.dirname(os.path.dirname(os.path.dirname(chromium_exe)))
headless_exe = None
browser_revs = {}
for b in b_spec.get("browsers", []):
    if b.get("name") in ("chromium", "chromium-headless-shell"):
        browser_revs[b["name"]] = {"revision": b.get("revision"), "browserVersion": b.get("browserVersion")}
        if b["name"] == "chromium-headless-shell":
            headless_exe = os.path.join(
                ms_pw_dir,
                f"chromium_headless_shell-{b['revision']}",
                "chrome-headless-shell-win64",
                "chrome-headless-shell.exe",
            )

if browser_revs.get("chromium", {}).get("revision") != "1248":
    raise RuntimeError(f"Unexpected Chromium revision: {browser_revs}")
if browser_revs.get("chromium-headless-shell", {}).get("revision") != "1248":
    raise RuntimeError(f"Unexpected Headless Shell revision: {browser_revs}")
for req_bin in (node_exe, chromium_exe, headless_exe):
    if not req_bin or not os.path.isfile(req_bin):
        raise RuntimeError(f"Required Playwright binary missing in target environment: {req_bin}")

print(json.dumps({
    "python_executable": actual_exe,
    "python_version": actual_py_ver,
    "python_full_version": sys.version,
    "base_prefix": os.path.abspath(sys.base_prefix),
    "venv_dir": os.path.abspath(expected_venv),
    "package_versions": measured_versions,
    "module_origins": module_origins,
    "browser_revisions": browser_revs,
    "ms_playwright_dir": ms_pw_dir,
    "node_exe": node_exe,
    "chromium_exe": chromium_exe,
    "headless_shell_exe": headless_exe,
}))
"""


def verify_build_environment(venv_dir: Optional[str] = None) -> Dict[str, Any]:
    """Validate target Python interpreter, isolated environment, locked package versions, and browsers BEFORE build."""
    target_python, resolved_venv = resolve_target_python(venv_dir)
    pyvenv_cfg = _parse_pyvenv_cfg(resolved_venv)
    if not pyvenv_cfg:
        raise RuntimeError(f"Missing pyvenv.cfg in build virtual environment: {resolved_venv}")
    if pyvenv_cfg.get("include-system-site-packages", "").lower() != "false":
        raise RuntimeError(
            f"Build venv must set include-system-site-packages = false (got {pyvenv_cfg.get('include-system-site-packages')!r})"
        )

    env, controlled_paths = get_controlled_env(resolved_venv)
    probe_env = env.copy()
    probe_env["QAT_EXPECTED_PYTHON_EXE"] = target_python
    probe_env["QAT_EXPECTED_VENV_DIR"] = resolved_venv
    probe_env["QAT_REQUIRED_PYTHON_VERSION"] = REQUIRED_PYTHON_VERSION
    probe_env["QAT_REQUIRED_PACKAGES_JSON"] = json.dumps(REQUIRED_BUILD_PACKAGES)

    proc = subprocess.run(
        [target_python, "-s", "-c", _ENV_PROBE_SCRIPT],
        cwd=REPO_ROOT,
        env=probe_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        err_detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"Target build environment verification failed ({target_python}): {err_detail}")

    probe_data = json.loads(proc.stdout.strip().splitlines()[-1])
    probe_data["sanitized_path_entries"] = controlled_paths
    probe_data["stripped_environment_variables"] = list(ISOLATED_ENV_VARS_TO_STRIP)
    probe_data["binary_sha256"] = {
        "python_executable": sha256_file(probe_data["python_executable"]),
        "playwright_node_exe": sha256_file(probe_data["node_exe"]),
        "chromium_exe": sha256_file(probe_data["chromium_exe"]),
        "headless_shell_exe": sha256_file(probe_data["headless_shell_exe"]),
    }
    return probe_data


def generate_package_manifest(app_dir: str, candidate_name: str) -> Dict[str, Any]:
    files_list: List[Dict[str, Any]] = []
    total_bytes = 0
    app_dir_lp = to_long_path(app_dir)
    for root, _, files in os.walk(app_dir_lp):
        for fname in sorted(files):
            if fname == "package_manifest.json":
                continue
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, app_dir_lp).replace("\\", "/")
            fsize = os.path.getsize(fpath)
            fhash = sha256_file(fpath)
            total_bytes += fsize
            files_list.append(
                {
                    "relative_path": rel_path,
                    "size_bytes": fsize,
                    "sha256": fhash,
                }
            )
    files_list.sort(key=lambda x: x["relative_path"])
    exe_path = os.path.join(app_dir, "QA-Tester.exe")
    uninst_path = os.path.join(app_dir, "QA-Tester-Uninstall.exe")
    return {
        "candidate": candidate_name,
        "version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "exe_path": f"{candidate_name}/QA-Tester/QA-Tester.exe",
        "exe_sha256": sha256_file(exe_path),
        "uninstaller_sha256": sha256_file(uninst_path) if os.path.isfile(to_long_path(uninst_path)) else None,
        "total_files": len(files_list),
        "total_size_bytes": total_bytes,
        "files": files_list,
    }


def _norm_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name.strip()).lower()


def _known_local_appdata() -> str:
    if os.name != "nt":
        return os.path.expanduser("~/.cache")
    buf = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 28, None, 0, buf):
        raise RuntimeError("Cannot resolve Windows LocalAppData folder")
    return buf.value


def _assert_clean_local_path(path_value: str, check_hardlinks: bool = True) -> str:
    raw = os.fspath(path_value)
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    if not os.path.isabs(raw) or raw.startswith(("\\\\", "//")):
        raise ValueError(f"Absolute local path required: {path_value}")
    p = Path(os.path.abspath(raw))
    if p == p.parent:
        raise ValueError(f"Filesystem root rejected: {path_value}")
    for part in p.parts[1:]:
        if ":" in part or part.endswith((" ", ".")):
            raise ValueError(f"Ambiguous Windows path component rejected: {path_value}")
    for item in (*reversed(p.parents), p):
        item_lp = to_long_path(str(item))
        if os.path.lexists(item_lp):
            st = os.lstat(item_lp)
            if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"Linked/reparse path rejected: {item}")
            if check_hardlinks and stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                raise ValueError(f"Hard-linked file rejected: {item}")
    return str(p)


def _assert_contained_within(root_dir: str, target_path: str, check_hardlinks: bool = True) -> str:
    clean_root = _assert_clean_local_path(root_dir, check_hardlinks=check_hardlinks)
    clean_target = _assert_clean_local_path(target_path, check_hardlinks=check_hardlinks)
    norm_root = os.path.normcase(clean_root)
    norm_target = os.path.normcase(clean_target)
    try:
        common = os.path.commonpath([norm_root, norm_target])
    except ValueError as exc:
        raise ValueError(f"Path crosses drive boundary: {clean_target}") from exc
    if common != norm_root:
        raise ValueError(f"Path escapes root {clean_root}: {clean_target}")
    return clean_target


def _check_clean_app_tree(app_dir: str) -> str:
    clean_app = _assert_clean_local_path(app_dir, check_hardlinks=True)
    app_lp = to_long_path(clean_app)
    if os.path.isdir(app_lp):
        for base_lp, dirs, files in os.walk(app_lp, followlinks=False):
            rel = os.path.relpath(base_lp, app_lp)
            base_clean = clean_app if rel == "." else os.path.join(clean_app, rel)
            for entry_name in dirs + files:
                _assert_contained_within(clean_app, os.path.join(base_clean, entry_name), check_hardlinks=True)
    return clean_app


def _load_trusted_dll_baseline() -> Dict[str, Any]:
    clean_baseline = _assert_clean_local_path(TRUSTED_DLL_BASELINE_PATH, check_hardlinks=False)
    actual_sha = sha256_file(clean_baseline)
    if actual_sha != TRUSTED_DLL_BASELINE_SHA256:
        raise RuntimeError(
            f"Trusted DLL baseline integrity failure: expected {TRUSTED_DLL_BASELINE_SHA256}, got {actual_sha}"
        )
    with open(to_long_path(clean_baseline), "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("schema_version") != 1 or not isinstance(data.get("dll_entries"), dict):
        raise RuntimeError("Invalid trusted DLL baseline schema")
    return data


def _parse_pyvenv_cfg(venv_dir: str) -> Dict[str, str]:
    cfg_path = os.path.join(venv_dir, "pyvenv.cfg")
    cfg_lp = to_long_path(cfg_path)
    if not os.path.isfile(cfg_lp):
        return {}
    _assert_contained_within(venv_dir, cfg_path, check_hardlinks=False)
    parsed: Dict[str, str] = {}
    with open(cfg_lp, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "=" in line:
                k, v = line.split("=", 1)
                parsed[k.strip().lower()] = v.strip()
    return parsed


def _load_pep376_site_packages_index(site_packages_dir: str) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
    """Return (installed_versions_by_dist, dll_ownership_by_norm_relpath) from .dist-info metadata and RECORD."""
    dist_versions: Dict[str, str] = {}
    dll_ownership: Dict[str, Dict[str, str]] = {}
    sp_lp = to_long_path(site_packages_dir)
    if not os.path.isdir(sp_lp):
        return dist_versions, dll_ownership

    _assert_clean_local_path(site_packages_dir, check_hardlinks=False)
    for entry in sorted(os.listdir(sp_lp)):
        if not entry.endswith(".dist-info"):
            continue
        dist_info_dir = os.path.join(site_packages_dir, entry)
        try:
            _assert_contained_within(site_packages_dir, dist_info_dir, check_hardlinks=False)
        except ValueError:
            continue
        meta_path = os.path.join(dist_info_dir, "METADATA")
        record_path = os.path.join(dist_info_dir, "RECORD")
        if not (os.path.isfile(to_long_path(meta_path)) and os.path.isfile(to_long_path(record_path))):
            continue
        try:
            _assert_contained_within(site_packages_dir, meta_path, check_hardlinks=False)
            _assert_contained_within(site_packages_dir, record_path, check_hardlinks=False)
        except ValueError:
            continue

        dist_name = None
        dist_ver = None
        with open(to_long_path(meta_path), "r", encoding="utf-8", errors="replace") as mf:
            for line in mf:
                if line.strip() == "":
                    break
                if line.lower().startswith("name:") and dist_name is None:
                    dist_name = _norm_dist_name(line.split(":", 1)[1])
                elif line.lower().startswith("version:") and dist_ver is None:
                    dist_ver = line.split(":", 1)[1].strip()
        if not dist_name or not dist_ver:
            continue
        dist_versions[dist_name] = dist_ver

        with open(to_long_path(record_path), "r", encoding="utf-8", errors="replace", newline="") as rf:
            reader = csv.reader(rf)
            for row in reader:
                if not row or len(row) < 2:
                    continue
                rec_rel = row[0].replace("\\", "/").strip()
                hash_spec = row[1].strip()
                if not rec_rel.lower().endswith(".dll"):
                    continue
                parts = rec_rel.split("/")
                if any(p in ("", ".", "..") or ":" in p for p in parts):
                    continue
                if not hash_spec.startswith("sha256="):
                    continue
                b64_val = hash_spec[len("sha256=") :]
                pad = "=" * ((4 - len(b64_val) % 4) % 4)
                try:
                    rec_sha = base64.urlsafe_b64decode(b64_val + pad).hex().upper()
                except Exception:
                    continue
                dll_ownership[rec_rel.lower()] = {
                    "distribution": dist_name,
                    "version": dist_ver,
                    "relpath": rec_rel,
                    "record_sha256": rec_sha,
                }
    return dist_versions, dll_ownership


def classify_dll_provenance(
    app_dir: str,
    venv_dir: Optional[str] = None,
    ms_playwright_dir: Optional[str] = None,
    require_complete_set: bool = False,
) -> Dict[str, Any]:
    clean_app_dir = _check_clean_app_tree(app_dir)
    baseline = _load_trusted_dll_baseline()
    locked_versions = baseline["locked_versions"]
    baseline_entries = baseline["dll_entries"]

    if not venv_dir:
        venv_scripts = os.path.abspath(os.path.dirname(sys.executable))
        resolved_venv_dir = os.path.abspath(os.path.join(venv_scripts, ".."))
    else:
        resolved_venv_dir = os.path.abspath(venv_dir)

    venv_clean_ok = False
    try:
        resolved_venv_dir = _assert_clean_local_path(resolved_venv_dir, check_hardlinks=False)
        venv_clean_ok = True
    except ValueError:
        venv_clean_ok = False

    pyvenv_cfg = _parse_pyvenv_cfg(resolved_venv_dir) if venv_clean_ok else {}
    base_py: Optional[str] = None
    base_py_version: Optional[str] = None
    if pyvenv_cfg.get("home"):
        candidate_base = os.path.abspath(pyvenv_cfg["home"])
        try:
            base_py = _assert_clean_local_path(candidate_base, check_hardlinks=False)
            base_py_version = pyvenv_cfg.get("version")
        except ValueError:
            base_py = None
    elif venv_clean_ok and os.path.isfile(to_long_path(os.path.join(resolved_venv_dir, "python312.dll"))):
        base_py = resolved_venv_dir

    site_packages_dir = os.path.join(resolved_venv_dir, "Lib", "site-packages")
    dist_versions, venv_dll_ownership = (
        _load_pep376_site_packages_index(site_packages_dir) if venv_clean_ok else ({}, {})
    )

    if ms_playwright_dir:
        resolved_ms_pw = os.path.abspath(ms_playwright_dir)
    else:
        pw_env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
        if pw_env == "0":
            resolved_ms_pw = os.path.join(site_packages_dir, "playwright", "driver", "package", ".local-browsers")
        elif pw_env:
            resolved_ms_pw = os.path.abspath(pw_env)
        else:
            resolved_ms_pw = os.path.join(_known_local_appdata(), "ms-playwright")

    ms_pw_clean_ok = False
    try:
        if os.path.isdir(to_long_path(resolved_ms_pw)):
            resolved_ms_pw = _assert_clean_local_path(resolved_ms_pw, check_hardlinks=False)
            ms_pw_clean_ok = True
    except ValueError:
        ms_pw_clean_ok = False

    browsers_spec_map: Dict[str, Dict[str, str]] = {}
    browsers_json_path = os.path.join(site_packages_dir, "playwright", "driver", "package", "browsers.json")
    if venv_clean_ok and os.path.isfile(to_long_path(browsers_json_path)):
        try:
            _assert_contained_within(site_packages_dir, browsers_json_path, check_hardlinks=False)
            with open(to_long_path(browsers_json_path), "r", encoding="utf-8") as bf:
                b_spec = json.load(bf)
            for b in b_spec.get("browsers", []):
                if isinstance(b, dict) and b.get("name"):
                    browsers_spec_map[str(b["name"])] = {
                        "revision": str(b.get("revision", "")),
                        "browserVersion": str(b.get("browserVersion", "")),
                    }
        except Exception:
            browsers_spec_map = {}

    dll_records: List[Dict[str, Any]] = []
    rejected_dlls: List[Dict[str, Any]] = []
    by_origin = {"python_base": 0, "venv_packages": 0, "playwright_browsers": 0, "external_path": 0}
    seen_rel_paths = set()

    app_dir_lp = to_long_path(clean_app_dir)
    for root, _, files in os.walk(app_dir_lp, followlinks=False):
        for fname in sorted(files):
            if not fname.lower().endswith(".dll"):
                continue
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, app_dir_lp).replace("\\", "/")
            seen_rel_paths.add(rel_path)
            fhash = sha256_file(fpath)

            expected = baseline_entries.get(rel_path)
            origin = "external_path"
            verified_source_path: Optional[str] = None
            trusted = False
            owner_dist: Optional[str] = None
            owner_ver: Optional[str] = None
            rejection_reason: Optional[str] = None

            if expected is None:
                rejection_reason = "not_in_trusted_dll_baseline"
            elif fhash != expected["expected_sha256"].upper():
                rejection_reason = "baseline_sha256_mismatch"
            else:
                exp_cat = expected["origin_category"]
                exp_owner = expected["owner_distribution"]
                exp_ver = expected["owner_version"]
                exp_src_rel = expected["source_relpath"]
                exp_sha = expected["expected_sha256"].upper()

                if exp_cat == "python_base":
                    if not base_py:
                        rejection_reason = "missing_or_untrusted_python_base"
                    elif base_py_version is not None and base_py_version != locked_versions["cpython"]:
                        rejection_reason = f"python_base_version_mismatch:{base_py_version}"
                    else:
                        cand_src = os.path.join(base_py, *exp_src_rel.split("/"))
                        try:
                            cand_src = _assert_contained_within(base_py, cand_src, check_hardlinks=True)
                            if not os.path.isfile(to_long_path(cand_src)):
                                rejection_reason = "python_base_source_missing"
                            elif sha256_file(cand_src) != exp_sha:
                                rejection_reason = "python_base_source_sha256_mismatch"
                            else:
                                origin = "python_base"
                                verified_source_path = cand_src
                                trusted = True
                                owner_dist = exp_owner
                                owner_ver = exp_ver
                        except ValueError as exc:
                            rejection_reason = f"python_base_boundary_error:{exc}"

                elif exp_cat == "venv_packages":
                    norm_owner = _norm_dist_name(exp_owner)
                    rec_info = venv_dll_ownership.get(exp_src_rel.lower())
                    if not venv_clean_ok or not os.path.isdir(to_long_path(site_packages_dir)):
                        rejection_reason = "missing_or_untrusted_venv_site_packages"
                    elif rec_info is None:
                        rejection_reason = "not_recorded_in_pep376_dist_info_record"
                    elif rec_info["distribution"] != norm_owner or rec_info["version"] != exp_ver:
                        rejection_reason = (
                            f"pep376_owner_mismatch:{rec_info['distribution']}=={rec_info['version']}"
                        )
                    elif rec_info["record_sha256"] != exp_sha:
                        rejection_reason = "pep376_record_sha256_mismatch"
                    else:
                        cand_src = os.path.join(site_packages_dir, *exp_src_rel.split("/"))
                        try:
                            cand_src = _assert_contained_within(site_packages_dir, cand_src, check_hardlinks=True)
                            if not os.path.isfile(to_long_path(cand_src)):
                                rejection_reason = "venv_package_source_missing"
                            elif sha256_file(cand_src) != exp_sha:
                                rejection_reason = "venv_package_source_sha256_mismatch"
                            else:
                                origin = "venv_packages"
                                verified_source_path = cand_src
                                trusted = True
                                owner_dist = exp_owner
                                owner_ver = exp_ver
                        except ValueError as exc:
                            rejection_reason = f"venv_package_boundary_error:{exc}"

                elif exp_cat == "playwright_browsers":
                    pw_installed_ver = dist_versions.get("playwright")
                    b_key = "chromium-headless-shell" if "headless" in exp_owner else "chromium"
                    b_meta = browsers_spec_map.get(b_key)
                    expected_bver = (
                        locked_versions["playwright_headless_shell_browser_version"]
                        if b_key == "chromium-headless-shell"
                        else locked_versions["playwright_chromium_browser_version"]
                    )
                    if not ms_pw_clean_ok:
                        rejection_reason = "missing_or_untrusted_ms_playwright_dir"
                    elif pw_installed_ver != locked_versions["playwright"]:
                        rejection_reason = f"playwright_version_mismatch:{pw_installed_ver}"
                    elif not b_meta or b_meta["revision"] != exp_ver or b_meta["browserVersion"] != expected_bver:
                        rejection_reason = f"browsers_json_spec_mismatch:{b_meta}"
                    else:
                        cand_src = os.path.join(resolved_ms_pw, *exp_src_rel.split("/"))
                        try:
                            cand_src = _assert_contained_within(resolved_ms_pw, cand_src, check_hardlinks=True)
                            if not os.path.isfile(to_long_path(cand_src)):
                                rejection_reason = "playwright_browser_source_missing"
                            elif sha256_file(cand_src) != exp_sha:
                                rejection_reason = "playwright_browser_source_sha256_mismatch"
                            else:
                                origin = "playwright_browsers"
                                verified_source_path = cand_src
                                trusted = True
                                owner_dist = exp_owner
                                owner_ver = exp_ver
                        except ValueError as exc:
                            rejection_reason = f"playwright_browser_boundary_error:{exc}"
                else:
                    rejection_reason = f"unknown_baseline_category:{exp_cat}"

            by_origin[origin] = by_origin.get(origin, 0) + 1
            rec = {
                "relative_path": rel_path,
                "sha256": fhash,
                "origin_category": origin,
                "trusted": trusted,
                "owner_distribution": owner_dist,
                "owner_version": owner_ver,
                "verified_source_path": verified_source_path,
                "rejection_reason": rejection_reason,
            }
            dll_records.append(rec)
            if not trusted:
                rejected_dlls.append(rec)

    missing_expected_dlls = (
        sorted(set(baseline_entries.keys()) - seen_rel_paths) if require_complete_set else []
    )

    # Compare top-level _internal DLLs against dist_candidate_20261028_v2
    v2_internal = os.path.join(REPO_ROOT, "dist_candidate_20261028_v2", "QA-Tester", "_internal")
    v1_0_internal = os.path.join(clean_app_dir, "_internal")
    v2_top_dlls = (
        {f for f in os.listdir(to_long_path(v2_internal)) if f.lower().endswith(".dll")}
        if os.path.isdir(to_long_path(v2_internal))
        else set()
    )
    v1_0_top_dlls = (
        {f for f in os.listdir(to_long_path(v1_0_internal)) if f.lower().endswith(".dll")}
        if os.path.isdir(to_long_path(v1_0_internal))
        else set()
    )

    excluded_ambient_dlls = sorted(list(v2_top_dlls - v1_0_top_dlls))
    retained_top_dlls = sorted(list(v1_0_top_dlls))

    return {
        "baseline_sha256": TRUSTED_DLL_BASELINE_SHA256,
        "records": dll_records,
        "total_dlls": len(dll_records),
        "counts_by_origin": by_origin,
        "untrusted_count": len(rejected_dlls),
        "rejected_dlls": rejected_dlls,
        "missing_expected_dlls": missing_expected_dlls,
        "retained_top_level_internal_dlls": retained_top_dlls,
        "excluded_ambient_path_dlls_count": len(excluded_ambient_dlls),
        "excluded_ambient_path_dlls": excluded_ambient_dlls,
    }


def assert_dll_provenance_trusted(dll_audit: Dict[str, Any]) -> None:
    if (
        dll_audit.get("counts_by_origin", {}).get("external_path", 0) > 0
        or dll_audit.get("untrusted_count", 0) > 0
        or any(not r.get("trusted") for r in dll_audit.get("records", []))
    ):
        reasons = [
            f"{r.get('relative_path')}: {r.get('rejection_reason')}"
            for r in dll_audit.get("rejected_dlls", [])
        ]
        raise RuntimeError(f"Unverified DLL source found ({'; '.join(reasons)})")
    if dll_audit.get("missing_expected_dlls"):
        raise RuntimeError(
            f"Missing required baseline DLLs: {dll_audit['missing_expected_dlls']}"
        )


def _is_allowed_reuse_relative_path(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/")
    if norm in ("QA-Tester.exe", "QA-Tester-Uninstall.exe"):
        return True
    parts = [p for p in norm.split("/") if p]
    return len(parts) >= 2 and parts[0] == "_internal"


def validate_reuse_candidate(
    reuse_name: str,
    destination_app_dir: str,
    repo_root: str = REPO_ROOT,
    preserved_registry: Optional[Dict[str, Any]] = None,
) -> List[Tuple[str, str, str]]:
    """Validate sealed manifest identity, member containment, namespace, and file hashes BEFORE any write."""
    from installer.path_safety import checked_path, member_path

    if (
        not isinstance(reuse_name, str)
        or not reuse_name.startswith("dist_")
        or os.path.basename(reuse_name) != reuse_name
        or "/" in reuse_name
        or "\\" in reuse_name
        or ":" in reuse_name
        or ".." in reuse_name
    ):
        raise ValueError(f"Invalid --reuse-app-from directory name: {reuse_name!r}")

    registry = preserved_registry if preserved_registry is not None else PRESERVED_CANDIDATES
    expected = registry.get(reuse_name)
    if (
        not isinstance(expected, dict)
        or "manifest" not in expected
        or "manifest_sha256" not in expected
        or "sha256" not in expected
    ):
        raise ValueError(f"Candidate {reuse_name!r} is not an approved sealed reuse source")

    clean_repo = _assert_clean_local_path(os.path.abspath(repo_root), check_hardlinks=False)
    source_root = _assert_contained_within(
        clean_repo, os.path.join(clean_repo, reuse_name), check_hardlinks=False
    )
    source_app = _assert_contained_within(
        source_root, os.path.join(source_root, "QA-Tester"), check_hardlinks=False
    )
    if not os.path.isdir(to_long_path(source_app)):
        raise ValueError(f"Reuse candidate application folder missing: {source_app}")

    manifest_path = _assert_contained_within(
        clean_repo, os.path.join(clean_repo, expected["manifest"]), check_hardlinks=False
    )
    if not os.path.isfile(to_long_path(manifest_path)):
        raise ValueError(f"Reuse candidate manifest missing: {manifest_path}")

    actual_manifest_sha = sha256_file(manifest_path)
    expected_manifest_sha = str(expected["manifest_sha256"]).upper()
    if actual_manifest_sha != expected_manifest_sha:
        raise ValueError(
            f"Reuse candidate manifest SHA-256 mismatch for {reuse_name}: "
            f"expected {expected_manifest_sha}, got {actual_manifest_sha}"
        )

    with open(to_long_path(manifest_path), "r", encoding="utf-8") as mf:
        source_manifest = json.load(mf)

    if not isinstance(source_manifest, dict):
        raise ValueError("Reuse manifest must be a JSON object")
    expected_exe_sha = str(expected["sha256"]).upper()
    if str(source_manifest.get("exe_sha256", "")).upper() != expected_exe_sha:
        raise ValueError("Reuse manifest exe_sha256 does not match sealed candidate identity")

    files = source_manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("Reuse manifest files list is missing or empty")

    dest_clean = checked_path(destination_app_dir)
    seen_dest_keys = set()
    expected_src_keys = set()
    copy_plan: List[Tuple[str, str, str]] = []
    saw_app_exe = False

    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("Invalid reuse manifest file entry")
        rel = entry.get("relative_path")
        exp_sha = entry.get("sha256")
        if not isinstance(rel, str) or not rel:
            raise ValueError("Missing relative_path in reuse manifest entry")
        if not isinstance(exp_sha, str) or not re.fullmatch(r"[0-9A-Fa-f]{64}", exp_sha):
            raise ValueError(f"Invalid SHA-256 in reuse manifest for {rel!r}")

        src_member = member_path(source_app, rel)
        dest_member = member_path(dest_clean, rel)
        if not _is_allowed_reuse_relative_path(rel):
            raise ValueError(f"Disallowed file outside application namespace in reuse manifest: {rel}")

        dest_key = os.path.normcase(dest_member)
        if dest_key in seen_dest_keys:
            raise ValueError(f"Duplicate reuse manifest entry: {rel}")
        seen_dest_keys.add(dest_key)

        src_verified = _assert_contained_within(source_app, src_member, check_hardlinks=False)
        if not os.path.isfile(to_long_path(src_verified)):
            raise ValueError(f"Reuse source file missing on disk: {rel}")
        actual_sha = sha256_file(src_verified)
        if actual_sha != exp_sha.upper():
            raise ValueError(f"Reuse source file hash mismatch for {rel}: expected {exp_sha.upper()}, got {actual_sha}")

        expected_src_keys.add(os.path.normcase(src_verified))
        if rel.replace("\\", "/") == "QA-Tester.exe":
            if exp_sha.upper() != expected_exe_sha:
                raise ValueError("Reuse manifest QA-Tester.exe hash mismatches sealed candidate EXE hash")
            saw_app_exe = True

        copy_plan.append((rel.replace("\\", "/"), src_verified, dest_member))

    if not saw_app_exe:
        raise ValueError("Reuse manifest does not contain QA-Tester.exe")

    # Verify source_app has no symlinks/reparse points and no unmanifested extra files
    optional_inner_manifest = os.path.normcase(member_path(source_app, "package_manifest.json"))
    source_app_lp = to_long_path(source_app)
    for walk_root, walk_dirs, walk_files in os.walk(source_app_lp, followlinks=False):
        clean_walk_root = walk_root[4:] if walk_root.startswith("\\\\?\\") else walk_root
        _assert_contained_within(source_app, clean_walk_root, check_hardlinks=False)
        for d in walk_dirs:
            dp = os.path.join(clean_walk_root, d)
            _assert_contained_within(source_app, dp, check_hardlinks=False)
        for fn in walk_files:
            fp = os.path.join(clean_walk_root, fn)
            fp_verified = _assert_contained_within(source_app, fp, check_hardlinks=False)
            norm_fp = os.path.normcase(fp_verified)
            if norm_fp != optional_inner_manifest and norm_fp not in expected_src_keys:
                rel_extra = os.path.relpath(fp_verified, source_app).replace("\\", "/")
                raise ValueError(f"Unmanifested extra file present in reuse source directory: {rel_extra}")

    return copy_plan


def reuse_verified_candidate_app(
    reuse_name: str,
    destination_app_dir: str,
    repo_root: str = REPO_ROOT,
    preserved_registry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Copy a sealed, fully validated candidate app into destination_app_dir with zero writes on validation failure."""
    from installer.path_safety import checked_path

    copy_plan = validate_reuse_candidate(
        reuse_name=reuse_name,
        destination_app_dir=destination_app_dir,
        repo_root=repo_root,
        preserved_registry=preserved_registry,
    )
    dest_clean = checked_path(destination_app_dir)
    if os.path.exists(to_long_path(dest_clean)):
        raise FileExistsError(f"Destination application directory already exists: {dest_clean}")

    os.makedirs(to_long_path(dest_clean), exist_ok=False)
    copied_count = 0
    for rel, src_verified, dest_member in copy_plan:
        if rel == "QA-Tester-Uninstall.exe":
            continue
        dest_lp = to_long_path(dest_member)
        os.makedirs(os.path.dirname(dest_lp), exist_ok=True)
        with open(to_long_path(src_verified), "rb") as sf, open(dest_lp, "xb") as df:
            shutil.copyfileobj(sf, df)
        shutil.copystat(to_long_path(src_verified), dest_lp)
        copied_count += 1

    return {
        "reused_candidate": reuse_name,
        "destination_app_dir": dest_clean,
        "copied_files": copied_count,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate-name', required=True)
    parser.add_argument('--installer-name', required=True)
    parser.add_argument('--reuse-app-from', help='Verified existing candidate directory for installer-only changes')
    parser.add_argument('--venv-dir', help='Custom virtual environment directory (defaults to current python environment)')
    args = parser.parse_args(argv)
    for name in (args.candidate_name, args.installer_name):
        if not name.startswith('dist_') or os.path.basename(name) != name or '/' in name or '\\' in name:
            raise ValueError('Expected a new dist_ directory name')
        if os.path.exists(os.path.join(REPO_ROOT, name)):
            raise FileExistsError('Refusing to overwrite an existing candidate: ' + name)

    print("0. Verifying target Python interpreter, isolated environment, and locked dependencies...")
    env_audit = verify_build_environment(args.venv_dir)
    target_python = env_audit["python_executable"]
    venv_dir = env_audit["venv_dir"]
    ms_playwright_dir = env_audit["ms_playwright_dir"]

    print("1. Verifying preserved candidates BEFORE build...")
    pre_preserved = verify_preserved_candidates()
    if args.reuse_app_from:
        validate_reuse_candidate(
            args.reuse_app_from,
            os.path.join(REPO_ROOT, args.candidate_name, "QA-Tester"),
        )

    env, controlled_paths = get_controlled_env(venv_dir)

    build_work_root = tempfile.mkdtemp(prefix="qa_tester_v1_0_build_")
    env["PYINSTALLER_CONFIG_DIR"] = os.path.join(build_work_root, "pyinstaller_config")
    print(f"2. Using isolated build work directory: {build_work_root}")
    print(f"   Verified target Python: {target_python}")
    print(f"   Sanitized PATH entries: {controlled_paths}")

    candidate_name = args.candidate_name
    candidate_root = os.path.join(REPO_ROOT, candidate_name)
    installer_root = os.path.join(REPO_ROOT, args.installer_name)

    # Step B: Build QA-Tester in temp dist_app first (avoids OneDrive rmdir WinError 5 and prevents partial dist_ dirs)
    print(f"4. Building {candidate_name}/QA-Tester from QA-Tester.spec...")
    app_dist_temp = os.path.join(build_work_root, "dist_app")
    app_work = os.path.join(build_work_root, "work_app")
    cmd_app = [
        target_python,
        "-s",
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        app_dist_temp,
        "--workpath",
        app_work,
        os.path.join(REPO_ROOT, "QA-Tester.spec"),
    ]
    if args.reuse_app_from:
        reuse_verified_candidate_app(
            args.reuse_app_from,
            os.path.join(app_dist_temp, "QA-Tester"),
        )
        print("Reused verified unchanged app: " + args.reuse_app_from)
    else:
        subprocess.run(cmd_app, cwd=REPO_ROOT, env=env, check=True)

    built_app_dir = os.path.join(app_dist_temp, "QA-Tester")
    built_app_exe = os.path.join(built_app_dir, "QA-Tester.exe")
    if not os.path.isfile(built_app_exe):
        raise RuntimeError(f"Failed to build {built_app_exe}")

    # Step A: Build QA-Tester-Uninstall.exe
    print("3. Building QA-Tester-Uninstall.exe...")
    uninst_dist = os.path.join(build_work_root, "dist_uninst")
    uninst_work = os.path.join(build_work_root, "work_uninst")
    uninst_spec = os.path.join(build_work_root, "spec_uninst")
    owned_path = os.path.join(build_work_root, 'owned_files.json')
    owned = generate_package_manifest(built_app_dir, candidate_name)['files']
    with open(owned_path, 'w', encoding='utf-8') as f:
        json.dump(owned, f, indent=2)
    shutil.copy2(owned_path, os.path.join(REPO_ROOT, "installer", "owned_files.json"))
    cmd_uninst = [
        # Immutable deletion allowlist bundled in the uninstaller.
        target_python,
        "-s",
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--add-data",
        f"{owned_path};.",
        "--windowed",
        "--name",
        "QA-Tester-Uninstall",
        "--version-file",
        os.path.join(REPO_ROOT, "installer", "version_info_uninstaller.txt"),
        "--distpath",
        uninst_dist,
        "--workpath",
        uninst_work,
        "--specpath",
        uninst_spec,
        "--paths",
        REPO_ROOT,
        os.path.join(REPO_ROOT, "installer", "uninstaller.py"),
    ]
    subprocess.run(cmd_uninst, cwd=REPO_ROOT, env=env, check=True)
    built_uninst_exe = os.path.join(uninst_dist, "QA-Tester-Uninstall.exe")
    if not os.path.isfile(built_uninst_exe):
        raise RuntimeError("Failed to build QA-Tester-Uninstall.exe")

    # Copy QA-Tester-Uninstall.exe into built_app_dir
    shutil.copy2(built_uninst_exe, os.path.join(built_app_dir, "QA-Tester-Uninstall.exe"))

    # Step C: Audit DLL provenance BEFORE creating candidate_root / installer_root directories
    print("5. Auditing DLL provenance and generating package_manifest.json...")
    dll_audit = classify_dll_provenance(
        built_app_dir,
        venv_dir=venv_dir,
        ms_playwright_dir=ms_playwright_dir,
        require_complete_set=True,
    )
    assert_dll_provenance_trusted(dll_audit)

    os.makedirs(candidate_root, exist_ok=True)
    os.makedirs(installer_root, exist_ok=True)

    manifest_data = generate_package_manifest(built_app_dir, candidate_name)
    manifest_outer = os.path.join(candidate_root, "package_manifest.json")
    manifest_inner_temp = os.path.join(built_app_dir, "package_manifest.json")
    for mp in (manifest_outer, manifest_inner_temp):
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)

    # Copy clean built_app_dir to candidate_root/QA-Tester
    app_dir = os.path.join(candidate_root, "QA-Tester")
    shutil.copytree(to_long_path(built_app_dir), to_long_path(app_dir), dirs_exist_ok=True)

    # Step D: Create payload zip for installer
    print("6. Creating compressed payload archive for Setup installer...")
    payload_zip_temp = os.path.join(build_work_root, "QA-Tester-1.0.0-payload.zip")
    built_app_lp = to_long_path(built_app_dir)
    with zipfile.ZipFile(payload_zip_temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, _, files in os.walk(built_app_lp):
            for fname in sorted(files):
                fpath = os.path.join(root, fname)
                rel_path = os.path.relpath(fpath, built_app_lp).replace("\\", "/")
                zf.write(fpath, rel_path)
    payload_zip_path = os.path.join(installer_root, "QA-Tester-1.0.0-payload.zip")
    shutil.copy2(payload_zip_temp, payload_zip_path)

    # Step E: Build standalone Windows Setup executable QA-Tester-Setup-1.0.0.exe
    print("7. Building standalone Windows Setup installer QA-Tester-Setup-1.0.0.exe...")
    trusted_releases = []
    for rel_name in (
        "dist_candidate_20261028_v2",
        "dist_candidate_20261028_v1_0_independent_r6",
        "dist_candidate_20261028_v1_0_independent_r9",
        "dist_candidate_20261028_v1_0_independent_r10",
        "dist_candidate_20261102_v1_0_independent_r11",
        "dist_candidate_20261103_v1_0_independent_r12",
        "dist_candidate_20261106_v1_0_r13",
    ):
        rel_manifest_path = os.path.join(REPO_ROOT, PRESERVED_CANDIDATES[rel_name]["manifest"])
        with open(rel_manifest_path, "r", encoding="utf-8") as rf:
            rel_manifest = json.load(rf)
        trusted_releases.append(
            {
                "candidate": rel_name,
                "version": rel_manifest.get("version", "2.0" if "v2" in rel_name else "1.0.0"),
                "exe_sha256": rel_manifest["exe_sha256"],
                "files": rel_manifest["files"],
            }
        )
    trusted_releases.append(
        {
            "candidate": candidate_name,
            "version": manifest_data.get("version", "1.0.0"),
            "exe_sha256": manifest_data["exe_sha256"],
            "files": manifest_data["files"],
        }
    )
    trusted_releases_temp = os.path.join(build_work_root, "trusted_releases.json")
    with open(trusted_releases_temp, "w", encoding="utf-8") as tf:
        json.dump({"releases": trusted_releases}, tf, indent=2)
    shutil.copy2(trusted_releases_temp, os.path.join(REPO_ROOT, "installer", "trusted_releases.json"))

    setup_dist_temp = os.path.join(build_work_root, "dist_setup")
    setup_work = os.path.join(build_work_root, "work_setup")
    setup_spec = os.path.join(build_work_root, "spec_setup")
    cmd_setup = [
        target_python,
        "-s",
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        "QA-Tester-Setup-1.0.0",
        "--version-file",
        os.path.join(REPO_ROOT, "installer", "version_info_setup.txt"),
        "--add-data",
        f"{payload_zip_temp};payload",
        "--add-data",
        f"{trusted_releases_temp};.",
        "--distpath",
        setup_dist_temp,
        "--workpath",
        setup_work,
        "--specpath",
        setup_spec,
        "--paths",
        REPO_ROOT,
        os.path.join(REPO_ROOT, "installer", "setup_installer.py"),
    ]
    subprocess.run(cmd_setup, cwd=REPO_ROOT, env=env, check=True)

    setup_exe_path = os.path.join(installer_root, "QA-Tester-Setup-1.0.0.exe")
    shutil.copy2(os.path.join(setup_dist_temp, "QA-Tester-Setup-1.0.0.exe"), setup_exe_path)
    if not os.path.isfile(setup_exe_path):
        raise RuntimeError("Failed to build QA-Tester-Setup-1.0.0.exe")

    print("8. Verifying preserved candidates AFTER build...")
    post_preserved = verify_preserved_candidates()

    installer_manifest = {
        "product_name": "QA Tester",
        "version": "1.0.0",
        "win32_file_version": "1.0.0.0",
        "reused_verified_application": args.reuse_app_from,
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "controlled_build_environment": env_audit,
        "installer": {
            "path": f"{args.installer_name}/QA-Tester-Setup-1.0.0.exe",
            "size_bytes": os.path.getsize(setup_exe_path),
            "sha256": sha256_file(setup_exe_path),
        },
        "payload_archive": {
            "path": f"{args.installer_name}/QA-Tester-1.0.0-payload.zip",
            "size_bytes": os.path.getsize(payload_zip_path),
            "sha256": sha256_file(payload_zip_path),
        },
        "application_candidate": {
            "candidate_dir": candidate_name,
            "exe_path": f"{candidate_name}/QA-Tester/QA-Tester.exe",
            "exe_sha256": manifest_data["exe_sha256"],
            "uninstaller_path": f"{candidate_name}/QA-Tester/QA-Tester-Uninstall.exe",
            "uninstaller_sha256": manifest_data["uninstaller_sha256"],
            "manifest_path": f"{candidate_name}/package_manifest.json",
            "manifest_sha256": sha256_file(manifest_outer),
            "total_files": manifest_data["total_files"],
            "total_size_bytes": manifest_data["total_size_bytes"],
        },
        "dll_provenance_audit": dll_audit,
        "preserved_candidates_verified": post_preserved,
    }

    installer_manifest_path = os.path.join(installer_root, "installer_manifest.json")
    with open(installer_manifest_path, "w", encoding="utf-8") as f:
        json.dump(installer_manifest, f, indent=2)

    print(f"Build work retained for audit: {build_work_root}")
    print("Build completed successfully!")
    print(f"  App EXE SHA-256:       {installer_manifest['application_candidate']['exe_sha256']}")
    print(f"  Uninstaller SHA-256:   {installer_manifest['application_candidate']['uninstaller_sha256']}")
    print(f"  Setup EXE SHA-256:     {installer_manifest['installer']['sha256']}")
    print(f"  Excluded ambient DLLs: {dll_audit['excluded_ambient_path_dlls_count']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

