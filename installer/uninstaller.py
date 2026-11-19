"""QA Tester 1.0 Windows Uninstaller (GUI + Silent CLI).

Removes the installed QA Tester application binaries, Start Menu shortcuts, and
per-user Windows Uninstall registry entry while strictly preserving user projects,
test results, reports, exports, and settings by default unless explicit purge is requested.
"""

import argparse
import hashlib
from installer.path_safety import known_folder, checked_path, check_tree, disjoint, fs_path, member_path
from installer.shortcut_helper import read_windows_shortcut
import json
import os
import shutil
import stat
import subprocess
import sys
from typing import Any, Dict, Optional

DEFAULT_UNINSTALL_REG_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\QATester"


def _default_install_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    local_app_data = known_folder()
    return os.path.join(local_app_data, "Programs", "QATester")


def _default_start_menu_dir() -> str:
    appdata = known_folder(False)
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "QA Tester")


def _default_user_data_dir() -> str:
    local_app_data = known_folder()
    return os.path.join(local_app_data, "QATester")


def _load_receipt(install_dir: str) -> Dict[str, Any]:
    receipt_path = os.path.join(install_dir, "install_receipt.json")
    if os.path.isfile(receipt_path):
        try:
            with open(receipt_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _remove_registry_key(reg_subkey: str) -> bool:
    if os.name != "nt" or not reg_subkey:
        return False
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, reg_subkey)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _owned_files():
    # Build-time allowlist embedded in the executable, never an installed receipt.
    base = sys._MEIPASS if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
    with open(os.path.join(base, 'owned_files.json'), encoding='utf-8') as f:
        return json.load(f)


def _hash(path):
    h = hashlib.sha256()
    with open(fs_path(path), 'rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest().upper()


def _schedule_self_delete(self_exe, install_dir):
    # No cmd expansion, no recursive deletion. Only this exact binary with the
    # expected content is removed after this process exits. Preserve other files.
    script = r"""
Wait-Process -Id ([int]$env:QAT_REMOVE_PID) -ErrorAction SilentlyContinue
$p = $env:QAT_REMOVE_EXE
# The onefile bootloader can still hold the executable after its Python child
# exits. Retry sharing violations while checking identity on every attempt.
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    if (-not (Test-Path -LiteralPath $p)) { break }
    try {
        $item = Get-Item -LiteralPath $p -Force
        $linked = $false
        $ancestor = $item
        while ($ancestor) {
            if ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) { $linked = $true; break }
            if ($ancestor -is [IO.FileInfo]) { $ancestor = $ancestor.Directory } else { $ancestor = $ancestor.Parent }
        }
        if ($linked) { break }
        $hasher = [Security.Cryptography.SHA256]::Create()
        $stream = [IO.File]::OpenRead($p)
        try { $actual = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '') }
        finally { $stream.Dispose(); $hasher.Dispose() }
        if ($actual -ne $env:QAT_REMOVE_HASH) { break }
        Remove-Item -LiteralPath $p -Force -ErrorAction Stop
        break
    } catch {
        Start-Sleep -Milliseconds 250
    }
}
$d = $env:QAT_REMOVE_DIR
if ((Test-Path -LiteralPath $d) -and -not (Get-ChildItem -LiteralPath $d -Force)) {
    Remove-Item -LiteralPath $d
}
"""
    env = os.environ.copy()
    env.update(QAT_REMOVE_PID=str(os.getpid()), QAT_REMOVE_EXE=self_exe,
               QAT_REMOVE_HASH=_hash(self_exe), QAT_REMOVE_DIR=install_dir)
    import ctypes
    buf = ctypes.create_unicode_buffer(32768)
    ctypes.windll.kernel32.GetSystemDirectoryW(buf, len(buf))
    subprocess.Popen([os.path.join(buf.value, 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
                      '-NoProfile', '-NonInteractive', '-Command', script], env=env,
                     creationflags=0x08000000, close_fds=True)


def run_uninstall(install_dir=None, start_menu_dir=None, user_data_dir=None,
                  registry_key=None, purge_user_data=False):
    target = checked_path(os.path.abspath(install_dir or _default_install_dir()))
    frozen = getattr(sys, 'frozen', False)
    if frozen and os.path.normcase(target) != os.path.normcase(os.path.dirname(os.path.abspath(sys.executable))):
        raise ValueError('Uninstaller can only remove its own installation')
    data = checked_path(os.path.abspath(user_data_dir or _default_user_data_dir()))
    menu = checked_path(os.path.abspath(start_menu_dir or _default_start_menu_dir()))
    # Receipts and environment variables are not deletion authority.
    if purge_user_data and os.path.normcase(data) != os.path.normcase(checked_path(_default_user_data_dir())):
        raise ValueError('Purge is restricted to the Windows user QA Tester data directory')
    disjoint(target, data)
    disjoint(target, menu)
    disjoint(data, menu)
    check_tree(target)
    if purge_user_data:
        check_tree(data)
    check_tree(menu)
    reg = DEFAULT_UNINSTALL_REG_SUBKEY if registry_key is None else registry_key
    if reg and reg != DEFAULT_UNINSTALL_REG_SUBKEY and not reg.startswith('Software\\QATester_Isolated_'):
        raise ValueError('Invalid application registry namespace')
    owned = _owned_files()
    # Validate entire allowlist before deleting even one file.
    candidates = [(member_path(target, e['relative_path']), e['sha256']) for e in owned]
    removed = 0
    for path, expected in candidates:
        p_fs = fs_path(path)
        if os.path.isfile(p_fs) and _hash(path) == expected.upper():
            checked_path(path)
            try:
                os.chmod(p_fs, stat.S_IWRITE)
            except OSError:
                pass
            os.remove(p_fs)
            removed += 1
    # Only metadata written by Setup, never follow paths contained in it.
    for name in ('install_receipt.json', 'package_manifest.json'):
        path = member_path(target, name)
        p_fs = fs_path(path)
        if os.path.isfile(p_fs):
            os.remove(p_fs)
    target_fs = fs_path(target)
    for base_fs, dirs, files in os.walk(target_fs, topdown=False, followlinks=False):
        rel_b = os.path.relpath(base_fs, target_fs)
        base_clean = target if rel_b == '.' else os.path.join(target, rel_b)
        for name in dirs:
            try:
                dpath = checked_path(os.path.join(base_clean, name))
                d_fs = fs_path(dpath)
                if not os.listdir(d_fs):
                    os.chmod(d_fs, stat.S_IWRITE)
                    os.rmdir(d_fs)
            except OSError:
                pass  # nonempty directories are preserved
    shortcuts = []
    for name, exe in [('QA Tester.lnk', 'QA-Tester.exe'), ('Uninstall QA Tester.lnk', 'QA-Tester-Uninstall.exe')]:
        path = os.path.join(menu, name)
        if os.path.isfile(fs_path(path)):
            info = read_windows_shortcut(path)
            if os.path.normcase(info['target_path']) == os.path.normcase(os.path.join(target, exe)):
                os.remove(fs_path(path))
                shortcuts.append(path)
    try: os.rmdir(fs_path(menu))
    except OSError: pass
    reg_removed = False
    if reg and os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg) as key:
                location, _ = winreg.QueryValueEx(key, 'InstallLocation')
            if os.path.normcase(location) == os.path.normcase(target):
                reg_removed = _remove_registry_key(reg)
        except OSError: pass
    purged = False
    if purge_user_data and os.path.isdir(fs_path(data)):
        # Canonical Windows folder only; reject reparse points before traversal.
        check_tree(data)
        shutil.rmtree(fs_path(data))
        purged = True
    if frozen and os.path.isfile(sys.executable):
        _schedule_self_delete(os.path.abspath(sys.executable), target)
    try: os.rmdir(target_fs)
    except OSError: pass
    residuals = []
    if os.path.isdir(target_fs):
        for b_fs, ds, fs in os.walk(target_fs, followlinks=False):
            for n in fs:
                residuals.append(os.path.relpath(os.path.join(b_fs, n), target_fs))
    return dict(status='success', install_dir=target, start_menu_dir=menu,
                user_data_dir=data, registry_key=reg, removed_app_files=removed,
                shortcuts_removed=shortcuts, registry_removed=reg_removed,
                purge_user_data_requested=bool(purge_user_data), user_data_purged=purged,
                user_data_preserved=os.path.isdir(fs_path(data)),
                residual_files=residuals)


def _run_gui(args: argparse.Namespace) -> int:
    import tkinter as tk
    from tkinter import messagebox

    resolved_install_dir = os.path.abspath(args.install_dir or _default_install_dir())
    receipt = _load_receipt(resolved_install_dir)
    resolved_user_data_dir = os.path.abspath(
        args.user_data_dir or _default_user_data_dir()
    )

    root = tk.Tk()
    root.title("Uninstall QA Tester 1.0")
    root.geometry("520x300")
    root.resizable(False, False)

    frame = tk.Frame(root, padx=20, pady=18)
    frame.pack(fill=tk.BOTH, expand=True)

    tk.Label(
        frame,
        text="Uninstall QA Tester 1.0",
        font=("Segoe UI", 14, "bold"),
        anchor="w",
    ).pack(fill=tk.X, pady=(0, 8))

    tk.Label(
        frame,
        text=f"Application folder:\n{resolved_install_dir}",
        font=("Segoe UI", 9),
        justify=tk.LEFT,
        anchor="w",
    ).pack(fill=tk.X, pady=(0, 10))

    tk.Label(
        frame,
        text=(
            "By default, your projects, test results, exported reports, and settings in:\n"
            f"{resolved_user_data_dir}\n"
            "are preserved so you do not lose any work."
        ),
        font=("Segoe UI", 9),
        fg="#1e293b",
        justify=tk.LEFT,
        anchor="w",
    ).pack(fill=tk.X, pady=(0, 12))

    purge_var = tk.BooleanVar(value=bool(args.purge_user_data))
    tk.Checkbutton(
        frame,
        text="Also permanently delete all projects, test results, and user settings",
        variable=purge_var,
        font=("Segoe UI", 9, "bold"),
        fg="#991b1b",
        anchor="w",
    ).pack(fill=tk.X, pady=(0, 18))

    exit_code = [0]

    def do_uninstall():
        purge = bool(purge_var.get())
        if purge:
            confirm = messagebox.askyesno(
                "Confirm Data Deletion",
                "Are you sure you want to permanently delete all QA Tester projects, runs, findings, and exports?",
                icon="warning",
            )
            if not confirm:
                return
        res = run_uninstall(
            install_dir=args.install_dir,
            start_menu_dir=args.start_menu_dir,
            user_data_dir=args.user_data_dir,
            registry_key=args.registry_key,
            purge_user_data=purge,
        )
        if args.result_json:
            with open(args.result_json, "w", encoding="utf-8") as f:
                json.dump(res, f, indent=2)
        msg = "QA Tester 1.0 has been uninstalled."
        if not purge:
            msg += f"\n\nYour user data was preserved in:\n{res['user_data_dir']}"
        messagebox.showinfo("Uninstall Complete", msg)
        root.destroy()

    btn_frame = tk.Frame(frame)
    btn_frame.pack(fill=tk.X, side=tk.BOTTOM)
    tk.Button(btn_frame, text="Cancel", width=12, command=root.destroy).pack(side=tk.RIGHT, padx=(8, 0))
    tk.Button(
        btn_frame,
        text="Uninstall",
        width=14,
        bg="#dc2626",
        fg="white",
        command=do_uninstall,
    ).pack(side=tk.RIGHT)

    root.mainloop()
    return exit_code[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="QA Tester 1.0 Uninstaller")
    parser.add_argument("--silent", action="store_true", help="Run non-interactively without GUI")
    parser.add_argument("--install-dir", help="Target installation directory to remove")
    parser.add_argument("--start-menu-dir", help="Start Menu shortcut folder to remove")
    parser.add_argument("--user-data-dir", help="User data folder path")
    parser.add_argument("--registry-key", help="HKCU registry subkey to remove")
    parser.add_argument(
        "--purge-user-data",
        action="store_true",
        help="Explicitly delete user projects, test results, and settings",
    )
    parser.add_argument("--result-json", help="Write JSON result summary to path")
    args = parser.parse_args(argv)

    if args.silent:
        try:
            res = run_uninstall(
                install_dir=args.install_dir,
                start_menu_dir=args.start_menu_dir,
                user_data_dir=args.user_data_dir,
                registry_key=args.registry_key,
                purge_user_data=args.purge_user_data,
            )
        except Exception as exc:
            res = {"status": "error", "message": str(exc)}
        if args.result_json:
            os.makedirs(os.path.dirname(os.path.abspath(args.result_json)), exist_ok=True)
            with open(args.result_json, "w", encoding="utf-8") as f:
                json.dump(res, f, indent=2)
        return 0 if res['status'] == 'success' else 1

    return _run_gui(args)


if __name__ == "__main__":
    sys.exit(main())

