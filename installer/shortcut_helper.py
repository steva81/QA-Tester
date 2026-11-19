"""Windows Shell Link (.lnk) creation and inspection utilities."""

import json
import os
import subprocess
import ctypes
from typing import Dict, Optional


def _powershell_path():
    buf = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetSystemDirectoryW(buf, len(buf)):
        raise RuntimeError('Cannot locate Windows system directory')
    return os.path.join(buf.value, 'WindowsPowerShell', 'v1.0', 'powershell.exe')


def create_windows_shortcut(
    shortcut_path: str,
    target_path: str,
    working_dir: Optional[str] = None,
    description: str = "",
    icon_location: Optional[str] = None,
    arguments: str = "",
) -> str:
    """Create a standard Windows .lnk shortcut using WScript.Shell COM."""
    abs_shortcut = os.path.abspath(shortcut_path)
    abs_target = os.path.abspath(target_path)
    abs_work = os.path.abspath(working_dir or os.path.dirname(abs_target))
    if os.name == "nt":
        for label, val in (
            ("Shortcut path", abs_shortcut),
            ("Shortcut target", abs_target),
            ("Shortcut working directory", abs_work),
        ):
            if len(val) > 259:
                raise ValueError(
                    f"{label} exceeds Windows Shell Link MAX_PATH limit ({len(val)} > 259 characters): {val}"
                )
    os.makedirs(os.path.dirname(abs_shortcut), exist_ok=True)

    ps_script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($env:QAT_SC_PATH)
$sc.TargetPath = $env:QAT_SC_TARGET
$sc.WorkingDirectory = $env:QAT_SC_WORKDIR
if ($env:QAT_SC_DESC) { $sc.Description = $env:QAT_SC_DESC }
if ($env:QAT_SC_ICON) { $sc.IconLocation = $env:QAT_SC_ICON }
if ($env:QAT_SC_ARGS) { $sc.Arguments = $env:QAT_SC_ARGS }
$sc.Save()
"""
    env = os.environ.copy()
    env["QAT_SC_PATH"] = abs_shortcut
    env["QAT_SC_TARGET"] = abs_target
    env["QAT_SC_WORKDIR"] = abs_work
    env["QAT_SC_DESC"] = description
    env["QAT_SC_ICON"] = icon_location or f"{abs_target},0"
    env["QAT_SC_ARGS"] = arguments

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
    proc = subprocess.run(
        [_powershell_path(), "-NoProfile", "-NonInteractive", "-Command", ps_script],
        env=env,
        capture_output=True,
        text=True,
        encoding='utf-8',
        timeout=20,
        creationflags=creationflags,
    )
    if proc.returncode != 0 or not os.path.isfile(abs_shortcut):
        raise RuntimeError(f"Failed to create shortcut {abs_shortcut}: {proc.stderr.strip()}")
    return abs_shortcut


def read_windows_shortcut(shortcut_path: str) -> Dict[str, str]:
    """Read target, working directory, arguments, and description from a Windows .lnk file."""
    abs_shortcut = os.path.abspath(shortcut_path)
    if not os.path.isfile(abs_shortcut):
        raise FileNotFoundError(f"Shortcut not found: {abs_shortcut}")

    ps_script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($env:QAT_SC_PATH)
$obj = @{
    shortcut_path = $env:QAT_SC_PATH
    target_path = $sc.TargetPath
    working_directory = $sc.WorkingDirectory
    arguments = $sc.Arguments
    description = $sc.Description
    icon_location = $sc.IconLocation
}
$obj | ConvertTo-Json -Compress
"""
    env = os.environ.copy()
    env["QAT_SC_PATH"] = abs_shortcut
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
    proc = subprocess.run(
        [_powershell_path(), "-NoProfile", "-NonInteractive", "-Command", ps_script],
        env=env,
        capture_output=True,
        text=True,
        encoding='utf-8',
        timeout=20,
        creationflags=creationflags,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to read shortcut {abs_shortcut}: {proc.stderr.strip()}")
    return json.loads(proc.stdout.strip())

