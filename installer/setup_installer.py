"""QA Tester 1.0 Windows Installer (GUI + Silent CLI).

Installs QA Tester 1.0 into a per-user Windows application directory without
requiring Administrator privileges, verifies Microsoft Edge WebView2 Runtime,
verifies SHA-256 integrity of all installed files against package_manifest.json,
creates Start Menu shortcuts, registers per-user Windows Uninstall metadata, and
strictly preserves existing user projects, test results, and settings on upgrade.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import stat
import sys
import tempfile
import shutil
from typing import Any, Callable, Dict, List, Optional
import zipfile

from installer.shortcut_helper import create_windows_shortcut, read_windows_shortcut
from installer.webview2_check import check_webview2_runtime, WEBVIEW2_DOWNLOAD_URL
from installer.path_safety import (
    MAX_WIN32_PATH_LENGTH,
    known_folder,
    checked_path,
    check_tree,
    disjoint,
    fs_path,
    member_path,
    strip_extended_prefix,
    validate_win32_max_path,
)

APP_NAME = "QA Tester"
APP_VERSION = "1.0.0"
APP_PUBLISHER = "QA Tester Project"
DEFAULT_UNINSTALL_REG_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\QATester"


def default_install_dir() -> str:
    local_app_data = known_folder()
    return os.path.join(local_app_data, "Programs", "QATester")


def default_start_menu_dir() -> str:
    appdata = known_folder(False)
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "QA Tester")


def default_user_data_dir() -> str:
    local_app_data = known_folder()
    return os.path.join(local_app_data, "QATester")


def _discover_payload_zip(explicit_zip: Optional[str] = None) -> str:
    if getattr(sys, "frozen", False):
        if explicit_zip or os.environ.get("QA_TESTER_PAYLOAD_ZIP"):
            raise ValueError('Packaged Setup only accepts its embedded payload')
        return os.path.join(sys._MEIPASS, 'payload', 'QA-Tester-1.0.0-payload.zip')
    if explicit_zip and os.path.isfile(explicit_zip):
        return os.path.abspath(explicit_zip)
    env_zip = os.environ.get("QA_TESTER_PAYLOAD_ZIP")
    if env_zip and os.path.isfile(env_zip):
        return os.path.abspath(env_zip)
    if getattr(sys, "frozen", False):
        bundled = os.path.join(sys._MEIPASS, "payload", "QA-Tester-1.0.0-payload.zip")
        if os.path.isfile(bundled):
            return bundled
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    candidate = os.path.join(repo_root, "dist_installer_1.0.0", "QA-Tester-1.0.0-payload.zip")
    if os.path.isfile(candidate):
        return candidate
    raise FileNotFoundError("Installer payload archive (QA-Tester-1.0.0-payload.zip) was not found.")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(fs_path(path), "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _trusted_releases() -> List[Dict[str, Any]]:
    """Load build-time embedded allowlist of verified QA Tester releases."""
    base = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(__file__)
    path = os.path.join(base, "trusted_releases.json")
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("releases", [])


def _is_allowed_app_relative_path(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/")
    if norm in ("QA-Tester.exe", "QA-Tester-Uninstall.exe"):
        return True
    return norm.startswith("_internal/") and len(norm) > len("_internal/")


def _backup_dir_for(install_dir: str) -> str:
    resolved = checked_path(os.path.abspath(install_dir))
    parent = os.path.dirname(resolved)
    base = os.path.basename(resolved)
    return os.path.join(parent, f".{base}.qat-backup")


def _journal_path_for(install_dir: str) -> str:
    resolved = checked_path(os.path.abspath(install_dir))
    parent = os.path.dirname(resolved)
    base = os.path.basename(resolved)
    return os.path.join(parent, f".{base}.qat-upgrade.json")


def _dir_fs_path(dir_path: str) -> str:
    """Validated extended-length directory path that also permits drive roots as parent directories."""
    raw = os.fspath(dir_path)
    if not os.path.isabs(raw) or raw.startswith(("\\\\", "//")):
        raise ValueError("An absolute local path is required")
    abs_d = os.path.abspath(raw)
    from pathlib import Path as _Path

    p = _Path(abs_d)
    if p == p.parent:
        return ("\\\\?\\" + str(p)) if os.name == "nt" else str(p)
    return fs_path(abs_d)


def _write_upgrade_journal(path: str, payload: Dict[str, Any], temporary_dir: Optional[str] = None) -> None:
    """Publish a complete journal before any irreversible cleanup begins."""
    checked_path(path)
    tmp_dir_fs = _dir_fs_path(temporary_dir or os.path.dirname(path))
    fd, temporary = tempfile.mkstemp(prefix='.qat-journal-', dir=tmp_dir_fs)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, fs_path(path))
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _verify_directory_matches_release(
    install_dir: str,
    release: Dict[str, Any],
    strict_no_unknown: bool = True,
    require_manifest_file: bool = False,
    allow_missing: bool = False,
) -> Dict[str, Any]:
    """Verify that install_dir cryptographically matches a release file list."""
    clean_install_dir = checked_path(install_dir)
    check_tree(clean_install_dir)
    entries = release.get("files", [])
    if not entries:
        raise ValueError("Release manifest contains no file entries.")

    expected_files_map: Dict[str, str] = {}
    has_main_exe = False
    for entry in entries:
        rel = entry["relative_path"].replace("\\", "/")
        if not _is_allowed_app_relative_path(rel):
            raise ValueError(f"Release manifest entry outside application namespace: {rel}")
        if rel == "QA-Tester.exe":
            has_main_exe = True
        full_path = member_path(clean_install_dir, rel)
        norm_full = os.path.normcase(full_path)
        if norm_full in expected_files_map:
            raise ValueError(f"Duplicate release manifest entry: {rel}")
        expected_sha = entry["sha256"].upper()
        expected_files_map[norm_full] = expected_sha
        if not os.path.isfile(fs_path(full_path)):
            if allow_missing and not os.path.lexists(fs_path(full_path)):
                continue
            raise ValueError(f"Missing expected application file: {rel}")
        if _sha256_file(full_path) != expected_sha:
            raise ValueError(f"SHA-256 mismatch for application file: {rel}")

    if not has_main_exe:
        raise ValueError("Release manifest is missing QA-Tester.exe.")

    manifest_path = member_path(clean_install_dir, "package_manifest.json")
    if require_manifest_file and not os.path.isfile(fs_path(manifest_path)):
        raise ValueError("package_manifest.json is missing from installation directory.")
    if os.path.lexists(fs_path(manifest_path)):
        if not os.path.isfile(fs_path(manifest_path)):
            raise ValueError("Invalid package_manifest.json in installation directory.")
        try:
            with open(fs_path(manifest_path), "r", encoding="utf-8") as f:
                disk_manifest = json.load(f)
        except Exception as exc:
            raise ValueError(f"Corrupt package_manifest.json in installation directory: {exc}") from exc
        disk_entries = disk_manifest.get("files", [])
        disk_map: Dict[str, str] = {}
        for de in disk_entries:
            d_rel = de["relative_path"].replace("\\", "/")
            d_full = os.path.normcase(member_path(clean_install_dir, d_rel))
            disk_map[d_full] = de["sha256"].upper()
        if disk_map != expected_files_map:
            raise ValueError("Installed package_manifest.json does not match verified release files.")

    receipt_path = member_path(clean_install_dir, "install_receipt.json")
    if os.path.lexists(fs_path(receipt_path)):
        if not os.path.isfile(fs_path(receipt_path)):
            raise ValueError("Invalid install_receipt.json in installation directory.")
        try:
            with open(fs_path(receipt_path), "r", encoding="utf-8") as f:
                disk_receipt = json.load(f)
        except Exception as exc:
            raise ValueError(f"Corrupt install_receipt.json in installation directory: {exc}") from exc
        if disk_receipt.get("app_name") != APP_NAME:
            raise ValueError("Installed receipt does not belong to QA Tester.")
        rec_dir = disk_receipt.get("install_dir")
        base_name = os.path.basename(clean_install_dir)
        expected_rec_dir = (
            os.path.join(os.path.dirname(clean_install_dir), base_name[1 : -len(".qat-backup")])
            if base_name.startswith(".") and base_name.endswith(".qat-backup") and len(base_name) > len(".qat-backup") + 1
            else clean_install_dir
        )
        if rec_dir and os.path.normcase(os.path.abspath(str(rec_dir))) != os.path.normcase(expected_rec_dir):
            raise ValueError("Installed receipt path does not match installation directory.")

    if strict_no_unknown:
        allowed_files = set(expected_files_map.keys())
        if os.path.isfile(fs_path(manifest_path)):
            allowed_files.add(os.path.normcase(manifest_path))
        if os.path.isfile(fs_path(receipt_path)):
            allowed_files.add(os.path.normcase(receipt_path))

        allowed_dirs = {os.path.normcase(clean_install_dir)}
        norm_root = os.path.normcase(clean_install_dir)
        for fp in expected_files_map.keys():
            cur = os.path.dirname(fp)
            while cur and len(cur) >= len(norm_root):
                allowed_dirs.add(os.path.normcase(cur))
                if os.path.normcase(cur) == norm_root:
                    break
                parent_cur = os.path.dirname(cur)
                if parent_cur == cur:
                    break
                cur = parent_cur

        root_fs = fs_path(clean_install_dir)
        for base_fs, dirs, files in os.walk(root_fs, followlinks=False):
            rel_b = os.path.relpath(base_fs, root_fs)
            base_clean = clean_install_dir if rel_b == "." else os.path.join(clean_install_dir, rel_b)
            if os.path.normcase(base_clean) not in allowed_dirs:
                raise ValueError(
                    "Existing installation is preserved: unknown directory present in installation folder."
                )
            for d in dirs:
                d_full = os.path.normcase(checked_path(os.path.join(base_clean, d)))
                if d_full not in allowed_dirs:
                    raise ValueError(
                        "Existing installation is preserved: unknown directory present in installation folder."
                    )
            for fname in files:
                f_full = os.path.normcase(checked_path(os.path.join(base_clean, fname)))
                if f_full not in allowed_files:
                    raise ValueError(
                        "Existing installation is preserved: unknown or foreign file present in installation folder."
                    )

    exe_norm = os.path.normcase(member_path(clean_install_dir, "QA-Tester.exe"))
    return {
        "candidate": release.get("candidate", "verified"),
        "version": release.get("version", APP_VERSION),
        "exe_sha256": expected_files_map[exe_norm],
        "verified_files": len(expected_files_map),
        "files": entries,
    }


def _match_trusted_existing_installation(install_dir: str) -> Dict[str, Any]:
    """Verify that install_dir is a known, untampered QA Tester release eligible for upgrade."""
    check_tree(install_dir)
    exe_path = member_path(install_dir, "QA-Tester.exe")
    if not os.path.isfile(fs_path(exe_path)):
        raise ValueError(
            "Existing installation is preserved. Directory does not contain a verified QA Tester installation."
        )
    actual_exe_sha = _sha256_file(exe_path)
    candidates = []
    for rel in _trusted_releases():
        rel_exe_sha = (rel.get("exe_sha256") or "").upper()
        if not rel_exe_sha:
            for e in rel.get("files", []):
                if e.get("relative_path", "").replace("\\", "/") == "QA-Tester.exe":
                    rel_exe_sha = e.get("sha256", "").upper()
                    break
        if rel_exe_sha == actual_exe_sha:
            candidates.append(rel)

    if not candidates:
        raise ValueError(
            "Existing installation is preserved. Select a new empty directory for this version."
        )

    last_err = None
    for rel in candidates:
        try:
            return _verify_directory_matches_release(install_dir, rel, strict_no_unknown=True)
        except ValueError as exc:
            last_err = exc
    raise ValueError(
        f"Existing installation is preserved. {last_err or 'Select a new empty directory for this version.'}"
    )


def _remove_verified_release_dir(target_dir: str, release_files: List[Dict[str, Any]], allow_missing: bool = False) -> None:
    """Remove only cryptographically verified release files and empty directories."""
    clean_target = checked_path(target_dir)
    target_fs = fs_path(clean_target)
    if not os.path.isdir(target_fs):
        return
    _verify_directory_matches_release(clean_target, {"files": release_files}, strict_no_unknown=True, allow_missing=allow_missing)
    for entry in release_files:
        path = member_path(clean_target, entry["relative_path"])
        p_fs = fs_path(path)
        if os.path.isfile(p_fs) and _sha256_file(path) == entry["sha256"].upper():
            checked_path(path)
            try:
                os.chmod(p_fs, stat.S_IWRITE)
            except OSError:
                pass
            os.remove(p_fs)
    for meta_name in ("install_receipt.json", "package_manifest.json"):
        meta_path = member_path(clean_target, meta_name)
        m_fs = fs_path(meta_path)
        if os.path.isfile(m_fs):
            checked_path(meta_path)
            try:
                os.chmod(m_fs, stat.S_IWRITE)
            except OSError:
                pass
            os.remove(m_fs)
    for base_fs, dirs, _files in os.walk(target_fs, topdown=False, followlinks=False):
        rel_b = os.path.relpath(base_fs, target_fs)
        base_clean = clean_target if rel_b == "." else os.path.join(clean_target, rel_b)
        for name in dirs:
            try:
                dpath = checked_path(os.path.join(base_clean, name))
                d_fs = fs_path(dpath)
                if not os.listdir(d_fs):
                    os.chmod(d_fs, stat.S_IWRITE)
                    os.rmdir(d_fs)
            except OSError:
                pass
    try:
        checked_target = checked_path(clean_target)
        ct_fs = fs_path(checked_target)
        if not os.listdir(ct_fs):
            os.chmod(ct_fs, stat.S_IWRITE)
            os.rmdir(ct_fs)
    except OSError:
        pass


def _snapshot_and_verify_shortcuts(
    start_menu_dir: str, install_dir: str, create_shortcuts: bool
) -> Dict[str, Optional[bytes]]:
    snap: Dict[str, Optional[bytes]] = {
        "QA Tester.lnk": None,
        "Uninstall QA Tester.lnk": None,
    }
    for lnk_name, exe_name in (
        ("QA Tester.lnk", "QA-Tester.exe"),
        ("Uninstall QA Tester.lnk", "QA-Tester-Uninstall.exe"),
    ):
        lnk_path = os.path.join(start_menu_dir, lnk_name)
        if os.path.lexists(lnk_path):
            checked_path(lnk_path)
            if not os.path.isfile(lnk_path):
                raise ValueError(f"Shortcut path is not a regular file: {lnk_path}")
            if create_shortcuts:
                info = read_windows_shortcut(lnk_path)
                expected_target = os.path.join(install_dir, exe_name)
                if os.path.normcase(info.get("target_path", "")) != os.path.normcase(expected_target):
                    raise ValueError(
                        f"Existing Start Menu shortcut '{lnk_name}' points to a different installation; refusing to overwrite."
                    )
            with open(lnk_path, "rb") as f:
                snap[lnk_name] = f.read()
    return snap


def _restore_shortcuts(
    start_menu_dir: str,
    install_dir: str,
    snap: Dict[str, Optional[bytes]],
    menu_existed_before: bool,
) -> None:
    expected_targets = {
        "QA Tester.lnk": os.path.join(install_dir, "QA-Tester.exe"),
        "Uninstall QA Tester.lnk": os.path.join(install_dir, "QA-Tester-Uninstall.exe"),
    }
    for lnk_name, raw_bytes in snap.items():
        if lnk_name not in expected_targets:
            continue
        lnk_path = os.path.join(start_menu_dir, lnk_name)
        try:
            if raw_bytes is None:
                if os.path.isfile(lnk_path):
                    checked_path(lnk_path)
                    os.remove(lnk_path)
            else:
                os.makedirs(start_menu_dir, exist_ok=True)
                if os.path.lexists(lnk_path):
                    checked_path(lnk_path)
                with open(lnk_path, "wb") as f:
                    f.write(raw_bytes)
                info = read_windows_shortcut(lnk_path)
                if os.path.normcase(info.get("target_path", "")) != os.path.normcase(expected_targets[lnk_name]):
                    os.remove(lnk_path)
        except Exception:
            pass
    if not menu_existed_before:
        try:
            os.rmdir(start_menu_dir)
        except OSError:
            pass


def _snapshot_and_verify_registry(reg_subkey: str, install_dir: str) -> Dict[str, Any]:
    if os.name != "nt" or not reg_subkey:
        return {"existed": False, "values": []}
    import winreg

    try:
        values = []
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_subkey, 0, winreg.KEY_READ) as key:
            idx = 0
            while True:
                try:
                    name, val, vtype = winreg.EnumValue(key, idx)
                    values.append({"name": name, "value": val, "type": int(vtype)})
                    if name == "InstallLocation" and val:
                        if os.path.normcase(os.path.abspath(str(val))) != os.path.normcase(install_dir):
                            raise ValueError(
                                "Existing Uninstall registry key points to a different installation location; refusing to overwrite."
                            )
                    idx += 1
                except OSError:
                    break
        return {"existed": True, "values": values}
    except FileNotFoundError:
        return {"existed": False, "values": []}


def _restore_registry(reg_subkey: str, install_dir: str, reg_snap: Dict[str, Any]) -> None:
    if os.name != "nt" or not reg_subkey:
        return
    import winreg

    if not reg_snap.get("existed"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, reg_subkey)
        except OSError:
            pass
        return

    values = reg_snap.get("values", [])
    for item in values:
        if item.get("name") == "InstallLocation" and item.get("value"):
            if os.path.normcase(os.path.abspath(str(item["value"]))) != os.path.normcase(install_dir):
                return
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, reg_subkey, 0, winreg.KEY_WRITE | winreg.KEY_READ
        ) as key:
            existing_names = []
            idx = 0
            while True:
                try:
                    n, _v, _t = winreg.EnumValue(key, idx)
                    existing_names.append(n)
                    idx += 1
                except OSError:
                    break
            snap_names = {item["name"] for item in values}
            for n in existing_names:
                if n not in snap_names:
                    try:
                        winreg.DeleteValue(key, n)
                    except OSError:
                        pass
            for item in values:
                winreg.SetValueEx(key, item["name"], 0, int(item["type"]), item["value"])
    except OSError:
        pass


def _load_payload_manifest_only(payload_zip: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        archive_path = _discover_payload_zip(payload_zip)
        with zipfile.ZipFile(archive_path, "r") as zf:
            return json.loads(zf.read("package_manifest.json").decode("utf-8"))
    except Exception:
        return None


def recover_interrupted_upgrade(
    install_dir: Optional[str] = None,
    start_menu_dir: Optional[str] = None,
    user_data_dir: Optional[str] = None,
    registry_key: Optional[str] = None,
    payload_zip: Optional[str] = None,
) -> Dict[str, Any]:
    """Recover a verified QA Tester installation if a prior upgrade was interrupted mid-activation."""
    resolved_install_dir = os.path.abspath(install_dir or default_install_dir())
    resolved_start_menu_dir = os.path.abspath(start_menu_dir or default_start_menu_dir())
    resolved_user_data_dir = os.path.abspath(user_data_dir or default_user_data_dir())
    resolved_reg_key = (
        registry_key if registry_key is not None else DEFAULT_UNINSTALL_REG_SUBKEY
    )

    for path in (resolved_install_dir, resolved_start_menu_dir, resolved_user_data_dir):
        check_tree(path)
    disjoint(resolved_install_dir, resolved_user_data_dir)
    disjoint(resolved_install_dir, resolved_start_menu_dir)
    disjoint(resolved_start_menu_dir, resolved_user_data_dir)
    if (
        resolved_reg_key
        and resolved_reg_key != DEFAULT_UNINSTALL_REG_SUBKEY
        and not resolved_reg_key.startswith("Software\\QATester_Isolated_")
    ):
        raise ValueError("Uninstall registry key is outside the application namespace")

    backup_dir = _backup_dir_for(resolved_install_dir)
    journal_path = _journal_path_for(resolved_install_dir)

    if not os.path.lexists(fs_path(backup_dir)):
        if os.path.lexists(fs_path(journal_path)):
            checked_path(journal_path)
            if os.path.isfile(fs_path(journal_path)):
                os.remove(fs_path(journal_path))
        return {
            "status": "noop",
            "recovered": False,
            "install_dir": resolved_install_dir,
        }

    check_tree(backup_dir)
    journal_data: Dict[str, Any] = {}
    if os.path.lexists(fs_path(journal_path)):
        checked_path(journal_path)
        if os.path.isfile(fs_path(journal_path)):
            try:
                with open(fs_path(journal_path), "r", encoding="utf-8") as f:
                    journal_data = json.load(f)
            except Exception:
                journal_data = {}

    if journal_data.get('phase') == 'committed':
        # After commit, the live version must never be removed to restore an
        # old backup that may already be partially deleted. Validate both ends
        # against the embedded whitelist before resuming cleanup.
        current = _match_trusted_existing_installation(resolved_install_dir)
        if current['candidate'] != journal_data.get('current_candidate'):
            raise ValueError('Committed installation does not match upgrade journal')
        previous = next((r for r in _trusted_releases()
                         if r.get('candidate') == journal_data.get('previous_candidate')
                         and r.get('exe_sha256') == journal_data.get('previous_exe_sha256')), None)
        if previous is None:
            raise ValueError('Untrusted previous release in committed upgrade journal')
        _remove_verified_release_dir(backup_dir, previous['files'], allow_missing=True)
        if os.path.exists(fs_path(backup_dir)):
            raise OSError('Committed upgrade backup cleanup is still pending')
        checked_path(journal_path)
        os.remove(fs_path(journal_path))
        return {'status': 'recovered', 'recovered': True, 'install_dir': resolved_install_dir,
                'recovery_action': 'completed_committed_cleanup',
                'restored_candidate': current['candidate'], 'restored_exe_sha256': current['exe_sha256']}

    backup_release = _match_trusted_existing_installation(backup_dir)

    if os.path.lexists(fs_path(resolved_install_dir)):
        check_tree(resolved_install_dir)
        if os.path.isdir(fs_path(resolved_install_dir)) and os.listdir(fs_path(resolved_install_dir)):
            matched_current = None
            payload_manifest = _load_payload_manifest_only(payload_zip)
            candidate_manifests = list(_trusted_releases())
            if payload_manifest and payload_manifest.get("files"):
                candidate_manifests.insert(0, payload_manifest)
            for cand in candidate_manifests:
                try:
                    matched_current = _verify_directory_matches_release(
                        resolved_install_dir, cand, strict_no_unknown=True, allow_missing=True
                    )
                    break
                except ValueError:
                    continue
            if matched_current is None:
                raise ValueError(
                    "Cannot recover interrupted upgrade: target directory contains unverified or foreign files."
                )
            _remove_verified_release_dir(resolved_install_dir, matched_current["files"], allow_missing=True)
        elif os.path.isdir(fs_path(resolved_install_dir)):
            os.rmdir(fs_path(resolved_install_dir))

    checked_path(resolved_install_dir)
    os.rename(fs_path(backup_dir), fs_path(resolved_install_dir))

    if journal_data.get("pre_shortcuts_hex"):
        snap_bytes = {
            k: (bytes.fromhex(v) if isinstance(v, str) else None)
            for k, v in journal_data["pre_shortcuts_hex"].items()
        }
        _restore_shortcuts(
            resolved_start_menu_dir,
            resolved_install_dir,
            snap_bytes,
            bool(journal_data.get("menu_existed_before", True)),
        )

    if "pre_registry" in journal_data and isinstance(journal_data["pre_registry"], dict):
        _restore_registry(resolved_reg_key, resolved_install_dir, journal_data["pre_registry"])

    if os.path.isfile(fs_path(journal_path)):
        checked_path(journal_path)
        os.remove(fs_path(journal_path))

    restored_check = _match_trusted_existing_installation(resolved_install_dir)
    return {
        "status": "recovered",
        "recovered": True,
        "install_dir": resolved_install_dir,
        "restored_candidate": restored_check["candidate"],
        "restored_exe_sha256": restored_check["exe_sha256"],
        "restored_files": restored_check["verified_files"],
    }


def _verify_installed_manifest(install_dir: str) -> Dict[str, Any]:
    clean_install_dir = checked_path(install_dir)
    manifest_path = member_path(clean_install_dir, "package_manifest.json")
    if not os.path.isfile(fs_path(manifest_path)):
        raise RuntimeError("package_manifest.json is missing from the extracted installation.")
    with open(fs_path(manifest_path), "r", encoding="utf-8") as f:
        manifest = json.load(f)
    entries = manifest.get("files", [])
    if not entries:
        raise RuntimeError("package_manifest.json contains no file entries.")
    verified_count = 0
    for entry in entries:
        rel_path = entry["relative_path"].replace("/", os.sep)
        full_path = member_path(clean_install_dir, rel_path)
        if not os.path.isfile(fs_path(full_path)):
            raise RuntimeError(f"Installed file missing after extraction: {rel_path}")
        actual_hash = _sha256_file(full_path)
        expected_hash = entry["sha256"].upper()
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"SHA-256 mismatch for {rel_path}: expected {expected_hash}, got {actual_hash}"
            )
        verified_count += 1
    return {
        "verified_files": verified_count,
        "manifest_path": manifest_path,
        "exe_sha256": manifest.get("exe_sha256"),
    }


def _validate_install_destination_lengths(
    resolved_install_dir: str,
    resolved_start_menu_dir: str,
    resolved_user_data_dir: str,
    archive_path: str,
    create_shortcuts: bool,
) -> None:
    """Reject destinations whose installed binaries/files or shortcuts would exceed Windows MAX_PATH (259) before any mutation."""
    validate_win32_max_path(resolved_install_dir, "Installation folder")
    for meta_file in ("QA-Tester.exe", "QA-Tester-Uninstall.exe", "package_manifest.json", "install_receipt.json"):
        validate_win32_max_path(member_path(resolved_install_dir, meta_file), "Installed application path")

    with zipfile.ZipFile(archive_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            final_member_path = member_path(resolved_install_dir, member.filename)
            validate_win32_max_path(final_member_path, "Installed package file path")

    if create_shortcuts:
        validate_win32_max_path(
            os.path.join(resolved_start_menu_dir, "Uninstall QA Tester.lnk"),
            "Start Menu shortcut path",
        )
    else:
        validate_win32_max_path(resolved_start_menu_dir, "Start Menu folder")

    validate_win32_max_path(
        os.path.join(resolved_user_data_dir, "screenshots", "x" * 36 + ".png"),
        "User data folder",
    )


def _write_uninstall_registry(
    reg_subkey: str,
    install_dir: str,
    uninstaller_exe: str,
    app_exe: str,
    estimated_kb: int,
) -> bool:
    if os.name != "nt" or not reg_subkey:
        return False
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, reg_subkey, 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, APP_PUBLISHER)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, install_dir)
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, f"{app_exe},0")
        winreg.SetValueEx(
            key,
            "UninstallString",
            0,
            winreg.REG_SZ,
            f'"{uninstaller_exe}" --install-dir "{install_dir}"',
        )
        winreg.SetValueEx(
            key,
            "QuietUninstallString",
            0,
            winreg.REG_SZ,
            f'"{uninstaller_exe}" --silent --install-dir "{install_dir}"',
        )
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, int(estimated_kb))
    return True


def _ensure_user_data_dirs_preserved(user_data_dir: str) -> Dict[str, Any]:
    existed_before = os.path.isdir(user_data_dir)
    db_path = os.path.join(user_data_dir, "data", "qa-tester.db")
    db_existed_before = os.path.isfile(db_path)
    db_hash_before = _sha256_file(db_path) if db_existed_before else None

    for sub in ("data", "screenshots", "reports", "exports", "logs", "settings"):
        os.makedirs(os.path.join(user_data_dir, sub), exist_ok=True)

    db_hash_after = _sha256_file(db_path) if os.path.isfile(db_path) else None
    return {
        "user_data_dir": user_data_dir,
        "existed_before": existed_before,
        "db_existed_before": db_existed_before,
        "db_sha256_before": db_hash_before,
        "db_sha256_after": db_hash_after,
        "db_preserved_intact": (db_hash_before == db_hash_after),
    }


def run_install(
    install_dir: Optional[str] = None,
    start_menu_dir: Optional[str] = None,
    user_data_dir: Optional[str] = None,
    registry_key: Optional[str] = None,
    payload_zip: Optional[str] = None,
    create_shortcuts: bool = True,
    simulate_missing_webview2: bool = False,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Run the QA Tester 1.0 installation or recoverable in-place upgrade workflow."""
    def report(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    report("Checking Microsoft Edge WebView2 Runtime prerequisite...")
    wv_ok, wv_version, wv_error = check_webview2_runtime(simulate_missing=simulate_missing_webview2)
    if not wv_ok:
        return {
            "status": "error",
            "error_code": "MISSING_WEBVIEW2_RUNTIME",
            "message": wv_error,
            "webview2_download_url": WEBVIEW2_DOWNLOAD_URL,
        }

    resolved_install_dir = os.path.abspath(install_dir or default_install_dir())
    resolved_start_menu_dir = os.path.abspath(start_menu_dir or default_start_menu_dir())
    resolved_user_data_dir = os.path.abspath(user_data_dir or default_user_data_dir())
    resolved_reg_key = (
        registry_key if registry_key is not None else DEFAULT_UNINSTALL_REG_SUBKEY
    )

    for path in (resolved_install_dir, resolved_start_menu_dir, resolved_user_data_dir):
        check_tree(path)
    disjoint(resolved_install_dir, resolved_user_data_dir)
    disjoint(resolved_install_dir, resolved_start_menu_dir)
    disjoint(resolved_start_menu_dir, resolved_user_data_dir)
    if resolved_reg_key and resolved_reg_key != DEFAULT_UNINSTALL_REG_SUBKEY and not resolved_reg_key.startswith('Software\\QATester_Isolated_'):
        raise ValueError('Uninstall registry key is outside the application namespace')

    archive_path = _discover_payload_zip(payload_zip)
    _validate_install_destination_lengths(
        resolved_install_dir=resolved_install_dir,
        resolved_start_menu_dir=resolved_start_menu_dir,
        resolved_user_data_dir=resolved_user_data_dir,
        archive_path=archive_path,
        create_shortcuts=create_shortcuts,
    )

    recovery_result = recover_interrupted_upgrade(
        install_dir=resolved_install_dir,
        start_menu_dir=resolved_start_menu_dir,
        user_data_dir=resolved_user_data_dir,
        registry_key=resolved_reg_key,
        payload_zip=payload_zip,
    )

    parent = os.path.dirname(resolved_install_dir)
    parent_fs = _dir_fs_path(parent)
    os.makedirs(parent_fs, exist_ok=True)
    staging = strip_extended_prefix(tempfile.mkdtemp(prefix='.qat-stage-', dir=parent_fs))
    total_extracted_bytes = 0
    try:
        report("Extracting and verifying package in isolated staging directory...")
        with zipfile.ZipFile(archive_path, 'r') as zf:
            names = set()
            for member in zf.infolist():
                if member.is_dir():
                    continue
                dest = member_path(staging, member.filename)
                key = os.path.normcase(dest)
                if key in names or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Duplicate or linked archive member')
                names.add(key)
                dest_fs = fs_path(dest)
                os.makedirs(os.path.dirname(dest_fs), exist_ok=True)
                with zf.open(member) as source, open(dest_fs, 'xb') as target:
                    shutil.copyfileobj(source, target)
                total_extracted_bytes += member.file_size
        _verify_installed_manifest(staging)
        with open(fs_path(member_path(staging, 'package_manifest.json')), encoding='utf-8') as f:
            packaged = json.load(f)
        for entry in packaged['files']:
            if not _is_allowed_app_relative_path(entry['relative_path']):
                raise ValueError(f"Package entry outside application namespace: {entry['relative_path']}")
        expected = {os.path.normcase(member_path(staging, e['relative_path'])) for e in packaged['files']}
        expected.add(os.path.normcase(os.path.join(staging, 'package_manifest.json')))
        if names != expected:
            raise ValueError('Payload and manifest file sets differ')

        check_tree(resolved_install_dir)
        check_tree(resolved_start_menu_dir)
        check_tree(resolved_user_data_dir)

        menu_existed_before = os.path.isdir(fs_path(resolved_start_menu_dir))
        pre_shortcuts = _snapshot_and_verify_shortcuts(
            resolved_start_menu_dir, resolved_install_dir, create_shortcuts
        )
        pre_registry = _snapshot_and_verify_registry(resolved_reg_key, resolved_install_dir)

        existing_release: Optional[Dict[str, Any]] = None
        if os.path.isdir(fs_path(resolved_install_dir)) and os.listdir(fs_path(resolved_install_dir)):
            is_identical = False
            try:
                _verify_directory_matches_release(
                    resolved_install_dir,
                    packaged,
                    strict_no_unknown=False,
                    require_manifest_file=True,
                )
                is_identical = True
            except ValueError:
                is_identical = False

            if is_identical:
                mode = "identical"
                is_upgrade = True
            else:
                existing_release = _match_trusted_existing_installation(resolved_install_dir)
                mode = "upgrade"
                is_upgrade = True
        else:
            mode = "fresh"
            is_upgrade = False

        user_data_info = _ensure_user_data_dirs_preserved(resolved_user_data_dir)
        app_exe = os.path.join(resolved_install_dir, "QA-Tester.exe")
        uninstaller_exe = os.path.join(resolved_install_dir, "QA-Tester-Uninstall.exe")
        estimated_kb = max(1, total_extracted_bytes // 1024)

        backup_dir = _backup_dir_for(resolved_install_dir)
        journal_path = _journal_path_for(resolved_install_dir)
        activated_on_disk = False
        committed = False

        try:
            if mode == "fresh":
                if os.path.isdir(fs_path(resolved_install_dir)):
                    os.rmdir(fs_path(resolved_install_dir))  # empty only
                checked_path(resolved_install_dir)
                os.rename(fs_path(staging), fs_path(resolved_install_dir))
                staging = None
                activated_on_disk = True
            elif mode == "upgrade":
                assert existing_release is not None
                checked_path(backup_dir)
                checked_path(journal_path)
                if os.path.lexists(fs_path(backup_dir)):
                    raise ValueError("Stale backup directory already exists; refusing to overwrite.")
                journal_payload = {
                    "app_name": APP_NAME,
                    "started_at_utc": datetime.now(timezone.utc).isoformat(),
                    "install_dir": resolved_install_dir,
                    "previous_candidate": existing_release["candidate"],
                    "previous_exe_sha256": existing_release["exe_sha256"],
                    "phase": "prepared",
                    "current_candidate": packaged.get("candidate"),
                    "menu_existed_before": menu_existed_before,
                    "pre_shortcuts_hex": {
                        k: (v.hex() if v is not None else None) for k, v in pre_shortcuts.items()
                    },
                    "pre_registry": pre_registry,
                }
                _write_upgrade_journal(journal_path, journal_payload)
                report("Activating upgraded version with rollback protection...")
                os.rename(fs_path(resolved_install_dir), fs_path(backup_dir))
                os.rename(fs_path(staging), fs_path(resolved_install_dir))
                staging = None
                activated_on_disk = True

            verification = _verify_installed_manifest(resolved_install_dir)

            created_shortcuts = []
            if create_shortcuts:
                report("Creating Start Menu shortcuts...")
                os.makedirs(fs_path(resolved_start_menu_dir), exist_ok=True)
                main_lnk = create_windows_shortcut(
                    shortcut_path=os.path.join(resolved_start_menu_dir, "QA Tester.lnk"),
                    target_path=app_exe,
                    working_dir=resolved_install_dir,
                    description="QA Tester 1.0 - Safe Website Quality Assurance Desktop Tool",
                    icon_location=f"{app_exe},0",
                )
                created_shortcuts.append(main_lnk)
                if os.path.isfile(fs_path(uninstaller_exe)):
                    uninst_lnk = create_windows_shortcut(
                        shortcut_path=os.path.join(resolved_start_menu_dir, "Uninstall QA Tester.lnk"),
                        target_path=uninstaller_exe,
                        working_dir=resolved_install_dir,
                        description="Uninstall QA Tester 1.0",
                        icon_location=f"{uninstaller_exe},0",
                        arguments=f'--install-dir "{resolved_install_dir}"',
                    )
                    created_shortcuts.append(uninst_lnk)

            report("Registering Windows Uninstall metadata...")
            reg_written = _write_uninstall_registry(
                reg_subkey=resolved_reg_key,
                install_dir=resolved_install_dir,
                uninstaller_exe=uninstaller_exe,
                app_exe=app_exe,
                estimated_kb=estimated_kb,
            )

            receipt = {
                "app_name": APP_NAME,
                "version": APP_VERSION,
                "installed_at_utc": datetime.now(timezone.utc).isoformat(),
                "is_upgrade": is_upgrade,
                "upgrade_mode": mode,
                "upgraded_from_candidate": existing_release["candidate"] if existing_release else None,
                "previous_exe_sha256": existing_release["exe_sha256"] if existing_release else None,
                "recovered_prior_interruption": bool(recovery_result.get("recovered")),
                "install_dir": resolved_install_dir,
                "start_menu_dir": resolved_start_menu_dir,
                "user_data_dir": resolved_user_data_dir,
                "registry_key": resolved_reg_key,
                "webview2_version": wv_version,
                "app_exe": app_exe,
                "uninstaller_exe": uninstaller_exe,
                "shortcuts": created_shortcuts,
                "manifest_verification": verification,
                "user_data_preservation": user_data_info,
                "registry_written": reg_written,
            }
            receipt_path = member_path(resolved_install_dir, "install_receipt.json")
            # Keep partial JSON outside the application tree. A killed write
            # must not leave invalid metadata that prevents verified rollback.
            _write_upgrade_journal(receipt_path, receipt, temporary_dir=parent)

            if mode == "upgrade":
                assert existing_release is not None
                journal_payload['phase'] = 'committed'
                _write_upgrade_journal(journal_path, journal_payload)
                committed = True
                _remove_verified_release_dir(backup_dir, existing_release["files"])
                if os.path.exists(fs_path(backup_dir)):
                    raise OSError('Committed upgrade backup cleanup is still pending')
                if os.path.isfile(fs_path(journal_path)):
                    checked_path(journal_path)
                    os.remove(fs_path(journal_path))
        except BaseException:
            if committed:
                # Keep the complete activated app, shell metadata and journal.
                # --recover or the next Setup run resumes verified cleanup.
                raise
            if mode == "upgrade":
                if os.path.isdir(fs_path(backup_dir)):
                    if activated_on_disk and os.path.isdir(fs_path(resolved_install_dir)):
                        try:
                            _remove_verified_release_dir(resolved_install_dir, packaged["files"])
                        except Exception:
                            pass
                    if not os.path.exists(fs_path(resolved_install_dir)):
                        os.rename(fs_path(backup_dir), fs_path(resolved_install_dir))
                if os.path.isfile(fs_path(journal_path)):
                    try:
                        checked_path(journal_path)
                        os.remove(fs_path(journal_path))
                    except OSError:
                        pass
            elif mode == "fresh" and activated_on_disk and os.path.isdir(fs_path(resolved_install_dir)):
                try:
                    _remove_verified_release_dir(resolved_install_dir, packaged["files"])
                except Exception:
                    pass

            _restore_shortcuts(
                resolved_start_menu_dir, resolved_install_dir, pre_shortcuts, menu_existed_before
            )
            _restore_registry(resolved_reg_key, resolved_install_dir, pre_registry)
            raise
    finally:
        if staging and os.path.isdir(fs_path(staging)):
            check_tree(staging)
            shutil.rmtree(fs_path(staging))

    report("Installation complete.")
    return {
        "status": "success",
        "receipt": receipt,
    }


def _run_gui(args: argparse.Namespace) -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox

    wv_ok, wv_ver, wv_err = check_webview2_runtime(simulate_missing=args.simulate_missing_webview2)
    if not wv_ok:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Microsoft Edge WebView2 Runtime Required",
            f"{wv_err}\n\nDownload URL:\n{WEBVIEW2_DOWNLOAD_URL}",
        )
        root.destroy()
        return 2

    root = tk.Tk()
    root.title(f"Install {APP_NAME} {APP_VERSION}")
    root.geometry("560x360")
    root.resizable(False, False)

    frame = tk.Frame(root, padx=22, pady=18)
    frame.pack(fill=tk.BOTH, expand=True)

    tk.Label(
        frame,
        text=f"{APP_NAME} {APP_VERSION} Setup",
        font=("Segoe UI", 15, "bold"),
        anchor="w",
    ).pack(fill=tk.X, pady=(0, 6))

    tk.Label(
        frame,
        text=(
            f"Microsoft Edge WebView2 Runtime detected: v{wv_ver}\n"
            "Per-user installation (no administrator privileges required).\n"
            "Existing projects, test results, and settings are automatically preserved."
        ),
        font=("Segoe UI", 9),
        fg="#334155",
        justify=tk.LEFT,
        anchor="w",
    ).pack(fill=tk.X, pady=(0, 14))

    tk.Label(frame, text="Destination folder:", font=("Segoe UI", 9, "bold"), anchor="w").pack(fill=tk.X)
    dir_row = tk.Frame(frame)
    dir_row.pack(fill=tk.X, pady=(4, 12))
    dir_var = tk.StringVar(value=os.path.abspath(args.install_dir or default_install_dir()))
    tk.Entry(dir_row, textvariable=dir_var, font=("Segoe UI", 9)).pack(
        side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8)
    )

    def browse_dir():
        chosen = filedialog.askdirectory(initialdir=dir_var.get())
        if chosen:
            dir_var.set(chosen)

    tk.Button(dir_row, text="Browse...", command=browse_dir).pack(side=tk.RIGHT)

    shortcut_var = tk.BooleanVar(value=not args.no_shortcuts)
    tk.Checkbutton(
        frame,
        text="Create Start Menu shortcuts (QA Tester & Uninstall QA Tester)",
        variable=shortcut_var,
        font=("Segoe UI", 9),
        anchor="w",
    ).pack(fill=tk.X, pady=(0, 12))

    status_var = tk.StringVar(value="Ready to install.")
    tk.Label(frame, textvariable=status_var, font=("Segoe UI", 9), fg="#0f172a", anchor="w").pack(
        fill=tk.X, pady=(4, 12)
    )

    exit_code = [0]

    def start_install():
        install_btn.config(state=tk.DISABLED)
        root.update_idletasks()
        try:
            res = run_install(
                install_dir=dir_var.get(),
                start_menu_dir=args.start_menu_dir,
                user_data_dir=args.user_data_dir,
                registry_key=args.registry_key,
                payload_zip=args.payload_zip,
                create_shortcuts=bool(shortcut_var.get()),
                simulate_missing_webview2=args.simulate_missing_webview2,
                progress_cb=lambda m: (status_var.set(m), root.update_idletasks()),
            )
            if args.result_json:
                os.makedirs(os.path.dirname(os.path.abspath(args.result_json)), exist_ok=True)
                with open(args.result_json, "w", encoding="utf-8") as f:
                    json.dump(res, f, indent=2)
            if res.get("status") != "success":
                exit_code[0] = 2
                messagebox.showerror("Installation Failed", res.get("message", "Unknown error"))
                return
            messagebox.showinfo(
                "Installation Complete",
                f"{APP_NAME} {APP_VERSION} has been installed to:\n{res['receipt']['install_dir']}",
            )
            root.destroy()
        except Exception as exc:
            exit_code[0] = 1
            messagebox.showerror("Installation Error", str(exc))
            install_btn.config(state=tk.NORMAL)

    btn_frame = tk.Frame(frame)
    btn_frame.pack(fill=tk.X, side=tk.BOTTOM)
    tk.Button(btn_frame, text="Cancel", width=12, command=root.destroy).pack(side=tk.RIGHT, padx=(8, 0))
    install_btn = tk.Button(
        btn_frame,
        text="Install",
        width=14,
        bg="#2563eb",
        fg="white",
        command=start_install,
    )
    install_btn.pack(side=tk.RIGHT)

    root.mainloop()
    return exit_code[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="QA Tester 1.0 Setup Installer")
    parser.add_argument("--silent", action="store_true", help="Run non-interactively without GUI")
    parser.add_argument("--install-dir", help="Target installation directory")
    parser.add_argument("--start-menu-dir", help="Start Menu folder for shortcuts")
    parser.add_argument("--user-data-dir", help="User data folder path")
    parser.add_argument("--registry-key", help="HKCU Uninstall registry subkey")
    parser.add_argument("--payload-zip", help="Explicit path to payload zip")
    parser.add_argument("--no-shortcuts", action="store_true", help="Skip creating Start Menu shortcuts")
    parser.add_argument(
        "--simulate-missing-webview2",
        action="store_true",
        help="Simulate missing Microsoft Edge WebView2 Runtime for prerequisite testing",
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help="Recover a verified previous QA Tester installation after an interrupted upgrade",
    )
    parser.add_argument("--result-json", help="Write structured JSON result to path")
    args = parser.parse_args(argv)

    if args.recover:
        try:
            res = recover_interrupted_upgrade(
                install_dir=args.install_dir,
                start_menu_dir=args.start_menu_dir,
                user_data_dir=args.user_data_dir,
                registry_key=args.registry_key,
                payload_zip=args.payload_zip,
            )
        except Exception as exc:
            res = {"status": "error", "error_code": "RECOVERY_EXCEPTION", "message": str(exc)}
        if args.result_json:
            os.makedirs(os.path.dirname(os.path.abspath(args.result_json)), exist_ok=True)
            with open(args.result_json, "w", encoding="utf-8") as f:
                json.dump(res, f, indent=2)
        return 0 if res.get("status") in ("recovered", "noop") else 1

    if args.silent or args.simulate_missing_webview2:
        try:
            res = run_install(
                install_dir=args.install_dir,
                start_menu_dir=args.start_menu_dir,
                user_data_dir=args.user_data_dir,
                registry_key=args.registry_key,
                payload_zip=args.payload_zip,
                create_shortcuts=not args.no_shortcuts,
                simulate_missing_webview2=args.simulate_missing_webview2,
            )
        except Exception as exc:
            res = {"status": "error", "error_code": "INSTALL_EXCEPTION", "message": str(exc)}
        if args.result_json:
            os.makedirs(os.path.dirname(os.path.abspath(args.result_json)), exist_ok=True)
            with open(args.result_json, "w", encoding="utf-8") as f:
                json.dump(res, f, indent=2)
        if res.get("status") != "success":
            return 2 if res.get("error_code") == "MISSING_WEBVIEW2_RUNTIME" else 1
        return 0

    return _run_gui(args)


if __name__ == "__main__":
    sys.exit(main())

