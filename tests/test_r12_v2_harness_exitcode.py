"""Regression tests for r12_v2 Clean-VM harness process handle & ExitCode verification.

Covers:
1. Byte-for-byte preservation of original verify_clean_vm_r12.ps1 and identity/AST/BOM/host-guard of verify_clean_vm_r12_v2.ps1.
2. Side-by-side .lnk + Get-Process reproduction (unmodified r12 yields forced=False, exit_code=None) vs r12_v2 (yields verified exit_code=0).
3. Non-zero exit code (exit 7) on voluntary WM_CLOSE via .lnk + Get-Process fails with exact exit_code=7 and preserves first-close diagnostics.
4. Forced termination when window ignores WM_CLOSE via .lnk + Get-Process fails with forced=True and preserves first-close diagnostics.
5. Unreadable exit code (exception on unattached exited Get-Process), null exit code, false, and string "0" all fail without coercion to 0.
6. End-to-end isolated check on actual packaged r12 QA-Tester.exe launched via .lnk and discovered via Get-Process:
   unmodified r12 fails with exit_code=None (while observer handle proves true kernel exit code 0), whereas r12_v2 passes with exit_code=0.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket
import subprocess
import pytest

ROOT = Path(__file__).resolve().parent.parent
R12_ORIG_SCRIPT = ROOT / "review_evidence" / "r12_candidate_20261103" / "verify_clean_vm_r12.ps1"
R12_IDENTITY = ROOT / "review_evidence" / "r12_candidate_20261103" / "identity_r12.json"
R12_V2_DIR = ROOT / "review_evidence" / "r12_harness_exitcode_20261103"
R12_V2_SCRIPT = R12_V2_DIR / "verify_clean_vm_r12_v2.ps1"
R12_V2_IDENTITY = R12_V2_DIR / "harness_r12_v2_identity.json"
R12_APP_DIR = ROOT / "dist_candidate_20261103_v1_0_independent_r12" / "QA-Tester"
R12_APP_EXE = R12_APP_DIR / "QA-Tester.exe"

PS51 = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
EXPECTED_R12_ORIG_SHA256 = "5B1AB49AEB3EA054AEA7471A9638625203E302795A9CE03059DED97D317519D0"
R10_APP_SHA = "FDD6ACFA332B0B0837E300D16EC4CF39A55DD74F1D78CC21D00BC0F6153F5C92"
R11_APP_SHA = "6DF1C27BBA283FE0B66D35DDA482E179A3C209AED43BE8DEC4C508304A22121F"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_ps51_file(ps1_path: Path, timeout: int = 45, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    cmd = [PS51, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1_path)]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _write_gui_helper_sources(tmp_path: Path) -> tuple[Path, Path]:
    """Write C# sources for a controllable WinForms GUI target and a Z-order helper."""
    gui_cs = tmp_path / "ControllableGuiTarget.cs"
    gui_cs.write_text(
        r"""
using System;
using System.Runtime.InteropServices;
using System.Windows.Forms;

public static class Program {
    private static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
    private const uint SWP_NOMOVE = 0x0002;
    private const uint SWP_NOSIZE = 0x0001;
    private const uint SWP_NOACTIVATE = 0x0010;
    private const uint SWP_SHOWWINDOW = 0x0040;

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    private static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);

    [STAThread]
    public static int Main(string[] args) {
        int code = 0;
        bool ignoreClose = false;
        if (args.Length > 0) int.TryParse(args[0], out code);
        if (args.Length > 1 && args[1] == "ignore_close") ignoreClose = true;

        Application.EnableVisualStyles();
        var form = new Form {
            Text = "ShortcutGuiTarget",
            Width = 280,
            Height = 160,
            TopMost = true,
            ShowInTaskbar = true,
            StartPosition = FormStartPosition.Manual,
            Location = new System.Drawing.Point(120, 120)
        };
        IntPtr createdHandle = form.Handle;
        ShowWindow(createdHandle, 4); // SW_SHOWNOACTIVATE
        SetWindowPos(createdHandle, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
        var t = new Timer { Interval = 50 };
        t.Tick += (s, e) => {
            if (!form.IsDisposed && form.IsHandleCreated) {
                ShowWindow(form.Handle, 4);
                SetWindowPos(form.Handle, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
            }
        };
        t.Start();
        form.FormClosing += (s, e) => {
            if (ignoreClose) {
                e.Cancel = true;
            }
        };
        form.FormClosed += (s, e) => {
            Environment.ExitCode = code;
            Application.ExitThread();
        };
        Application.Run(form);
        return code;
    }
}
""",
        encoding="utf-8",
    )

    zorder_cs = tmp_path / "PromoteWindowHelper.cs"
    zorder_cs.write_text(
        r"""
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class PromoteWindowHelper {
    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    private static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
    private const uint SWP_NOMOVE = 0x0002;
    private const uint SWP_NOSIZE = 0x0001;
    private const uint SWP_NOACTIVATE = 0x0010;
    private const uint SWP_SHOWWINDOW = 0x0040;

    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] private static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);

    public static bool PromoteTitledWindowForPid(int targetPid, string expectedTitle) {
        IntPtr found = IntPtr.Zero;
        EnumWindows((hWnd, lParam) => {
            uint pid;
            GetWindowThreadProcessId(hWnd, out pid);
            if (pid == (uint)targetPid) {
                var sb = new StringBuilder(256);
                GetWindowText(hWnd, sb, 256);
                if (sb.ToString() == expectedTitle) {
                    found = hWnd;
                    return false;
                }
            }
            return true;
        }, IntPtr.Zero);
        if (found == IntPtr.Zero) return false;
        ShowWindow(found, 4); // SW_SHOWNOACTIVATE
        SetWindowPos(found, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
        return true;
    }
}
""",
        encoding="utf-8",
    )
    return gui_cs, zorder_cs


def test_r12_v2_identity_utf8_bom_ps51_ast_and_original_r12_preserved(tmp_path: Path):
    # 1. Original r12 harness MUST remain 100% byte-identical
    orig_bytes = R12_ORIG_SCRIPT.read_bytes()
    assert len(orig_bytes) == 37368
    assert _sha256(R12_ORIG_SCRIPT) == EXPECTED_R12_ORIG_SHA256

    # 2. v2 harness has UTF-8 BOM, CRLF line endings, and matches recorded identity
    v2_bytes = R12_V2_SCRIPT.read_bytes()
    assert v2_bytes.startswith(b"\xef\xbb\xbf"), "verify_clean_vm_r12_v2.ps1 must retain UTF-8 BOM for PowerShell 5.1"
    v2_text = v2_bytes[3:].decode("utf-8")
    assert "\r\n" in v2_text and "\n" not in v2_text.replace("\r\n", ""), "Expected CRLF line endings"

    v2_identity = json.loads(R12_V2_IDENTITY.read_text(encoding="utf-8"))
    assert _sha256(R12_V2_SCRIPT) == v2_identity["corrected_r12_v2_harness"]["sha256"]
    assert len(v2_bytes) == v2_identity["corrected_r12_v2_harness"]["size_bytes"]
    assert _sha256(R12_V2_SCRIPT) != EXPECTED_R12_ORIG_SHA256

    # 3. Valid PowerShell 5.1 AST, expected r12 constants, and receipt hash enforcement
    r12_identity = json.loads(R12_IDENTITY.read_text(encoding="utf-8"))
    check_ps1 = tmp_path / "check_ast_and_receipts.ps1"
    check_ps1.write_text(
        f"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile('{R12_V2_SCRIPT}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw "PS5.1 parse errors: $($errors | Out-String)" }}
. '{R12_V2_SCRIPT}' -LoadHelpersOnly

$goodReceipt = [pscustomobject]@{{
    app_name = 'QA Tester'
    version = '1.0.0'
    webview2_version = '156.0.4314.58'
    registry_written = $true
    upgrade_mode = 'fresh'
    manifest_verification = [pscustomobject]@{{ verified_files = 532; exe_sha256 = $ExpectedAppExeHash }}
    user_data_preservation = [pscustomobject]@{{ db_preserved_intact = $true; db_existed_before = $false }}
}}
Assert-ValidInstallReceipt $goodReceipt -ExpectedMode 'fresh' -ExpectedDbExistedBefore $false

foreach ($stale in @('{R10_APP_SHA}', '{R11_APP_SHA}')) {{
    $bad = [pscustomobject]@{{
        app_name = 'QA Tester'
        version = '1.0.0'
        webview2_version = '156.0.4314.58'
        registry_written = $true
        upgrade_mode = 'fresh'
        manifest_verification = [pscustomobject]@{{ verified_files = 532; exe_sha256 = $stale }}
        user_data_preservation = [pscustomobject]@{{ db_preserved_intact = $true; db_existed_before = $false }}
    }}
    $rejected = $false
    try {{ Assert-ValidInstallReceipt $bad -ExpectedMode 'fresh' -ExpectedDbExistedBefore $false }} catch {{ $rejected = $true }}
    Assert-Condition $rejected "Failed to reject stale hash $stale"
}}

[ordered]@{{
    setup = $ExpectedSetupHash
    app = $ExpectedAppExeHash
    uninstall = $ExpectedUninstallHash
    files = $ExpectedFileCount
}} | ConvertTo-Json -Compress
""",
        encoding="utf-8-sig",
    )
    proc = _run_ps51_file(check_ps1)
    assert proc.returncode == 0, proc.stderr
    vals = json.loads(proc.stdout.strip())
    assert vals["setup"] == r12_identity["r12_hashes"]["setup_exe_sha256"]
    assert vals["app"] == r12_identity["r12_hashes"]["app_exe_sha256"]
    assert vals["uninstall"] == r12_identity["r12_hashes"]["uninstaller_exe_sha256"]
    assert vals["files"] == 532

    # 4. Host safety block still refuses execution on developer host
    blocked_out = tmp_path / "must_not_be_created.json"
    host_proc = _run_ps51_file(R12_V2_SCRIPT, timeout=15, extra_args=["-OutputJsonPath", str(blocked_out)])
    assert host_proc.returncode != 0
    assert "SAFETY BLOCK:" in ((host_proc.stdout or "") + "\n" + (host_proc.stderr or ""))
    assert not blocked_out.exists()


def test_lnk_get_process_reproduces_r12_null_exitcode_and_passes_on_r12_v2(tmp_path: Path):
    gui_cs, zorder_cs = _write_gui_helper_sources(tmp_path)
    gui_exe = tmp_path / "ControllableGuiTarget.exe"
    lifecycle_out = tmp_path / "v2_clean_lifecycle.json"

    ps1 = tmp_path / "run_side_by_side.ps1"
    ps1.write_text(
        f"""
$ErrorActionPreference = 'Stop'
$cs = [System.IO.File]::ReadAllText('{gui_cs}')
Add-Type -TypeDefinition $cs -ReferencedAssemblies 'System.Windows.Forms','System.Drawing' -OutputAssembly '{gui_exe}' -OutputType WindowsApplication
$csZ = [System.IO.File]::ReadAllText('{zorder_cs}')
Add-Type -TypeDefinition $csZ

$wsh = New-Object -ComObject WScript.Shell
$lnk = Join-Path '{tmp_path}' 'GuiCleanExit0.lnk'
$sc = $wsh.CreateShortcut($lnk)
$sc.TargetPath = '{gui_exe}'
$sc.Arguments = '0'
$sc.WindowStyle = 1
$sc.Save()

function Start-AndDiscoverViaGetProcess {{
    $before = @(Get-Process -Name 'ControllableGuiTarget' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    Start-Process -FilePath $lnk
    $dl = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $dl) {{
        $cands = @(Get-Process -Name 'ControllableGuiTarget' -ErrorAction SilentlyContinue | Where-Object {{ $before -notcontains $_.Id }})
        foreach ($c in $cands) {{
            if ([PromoteWindowHelper]::PromoteTitledWindowForPid($c.Id, 'ShortcutGuiTarget')) {{
                $found = @(Get-Process -Name 'ControllableGuiTarget' -ErrorAction SilentlyContinue | Where-Object {{
                    $_.Id -eq $c.Id -and $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -eq 'ShortcutGuiTarget'
                }})
                if ($found.Count -gt 0) {{
                    Start-Sleep -Milliseconds 150
                    $null = [PromoteWindowHelper]::PromoteTitledWindowForPid($c.Id, 'ShortcutGuiTarget')
                    return $found[0]
                }}
            }}
        }}
        Start-Sleep -Milliseconds 100
    }}
    throw 'Failed to discover ControllableGuiTarget via Get-Process'
}}

try {{
    # --- Part A: Unmodified verify_clean_vm_r12.ps1 reproduces forced=false, exit_code=null ---
    . '{R12_ORIG_SCRIPT}' -LoadHelpersOnly
    $gpOrig = Start-AndDiscoverViaGetProcess
    $obs = [System.Diagnostics.Process]::GetProcessById($gpOrig.Id)
    $obsHandle = $obs.SafeHandle
    $origRes = Stop-QATesterWindow -Process $gpOrig -WaitMs 10000
    $null = $obs.WaitForExit(5000)
    $trueKernelExit = $obs.get_ExitCode()
    $obsHandle.Dispose()
    $obs.Dispose()

    # --- Part B: Corrected verify_clean_vm_r12_v2.ps1 with Initialize-TrackedProcessHandle ---
    . '{R12_V2_SCRIPT}' -LoadHelpersOnly
    $gpV2 = Start-AndDiscoverViaGetProcess
    $retained = Initialize-TrackedProcessHandle -Process $gpV2
    Assert-Condition $retained 'Expected Initialize-TrackedProcessHandle to return true'

    $report = [ordered]@{{
        candidate = 'r12'
        harness_version = 'r12_v2'
        close_attempts = [ordered]@{{}}
    }}
    $v2Res = $null
    Invoke-BoundedVerificationLifecycle -Report $report -OutputPath '{lifecycle_out}' -Body {{
        $script:v2Res = Stop-QATesterWindow -Process $gpV2 -WaitMs 10000
        $report.close_attempts['before_uninstall'] = $script:v2Res
        Assert-Condition ($script:v2Res.stopped -eq $true -and $script:v2Res.forced -eq $false -and $null -eq $script:v2Res.error -and $null -eq $script:v2Res.exit_code_error -and ($script:v2Res.exit_code -is [int]) -and $script:v2Res.exit_code -eq 0) "Unexpected close failure: $($script:v2Res.error)"
        try {{ $gpV2.Dispose() }} catch {{}}
    }} -Cleanup {{
        return [ordered]@{{ passed = $true; errors = @() }}
    }}

    # --- Part C: Direct Stop-QATesterWindow call on Get-Process object without prior Initialize call ---
    $gpDirect = Start-AndDiscoverViaGetProcess
    $directRes = Stop-QATesterWindow -Process $gpDirect -WaitMs 10000
    try {{ $gpDirect.Dispose() }} catch {{}}

    [ordered]@{{
        orig_pid = $gpOrig.Id
        true_kernel_exit = $trueKernelExit
        orig_result = $origRes
        v2_result = $v2Res
        direct_result = $directRes
    }} | ConvertTo-Json -Depth 6 -Compress
}} finally {{
    Get-Process -Name 'ControllableGuiTarget' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}}
""",
        encoding="utf-8-sig",
    )

    proc = _run_ps51_file(ps1)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip())

    # Verify Part A: Unmodified r12 reproduces exact VM failure while OS exit code was 0
    assert data["true_kernel_exit"] == 0
    orig = data["orig_result"]
    assert orig["stopped"] is True
    assert orig["forced"] is False
    assert orig["exit_code"] is None
    assert orig["error"] == f"QA-Tester process PID {data['orig_pid']} exited without a verified zero exit code (exit_code=)"

    # Verify Part B: Corrected r12_v2 verifies genuine integer exit_code == 0
    v2 = data["v2_result"]
    assert v2["stopped"] is True
    assert v2["forced"] is False
    assert v2["handle_retained"] is True
    assert v2["had_exited_before_close"] is False
    assert v2["close_main_window_sent"] is True
    assert type(v2["exit_code"]) is int and v2["exit_code"] == 0
    assert v2["exit_code_error"] is None
    assert v2["error"] is None

    saved = json.loads(lifecycle_out.read_text(encoding="utf-8"))
    assert not lifecycle_out.read_bytes().startswith(b"\xef\xbb\xbf")
    assert saved["all_passed"] is True
    assert saved["cleanup_failed"] is False
    assert saved["close_attempts"]["before_uninstall"]["exit_code"] == 0

    # Verify Part C: Direct Stop-QATesterWindow also acquires handle and verifies 0
    direct = data["direct_result"]
    assert direct["handle_retained"] is True
    assert type(direct["exit_code"]) is int and direct["exit_code"] == 0
    assert direct["error"] is None


@pytest.mark.parametrize("mode", ["nonzero_exit_7", "forced_ignore_close"])
def test_lnk_get_process_failure_modes_fail_and_preserve_first_close_diagnostics(tmp_path: Path, mode: str):
    gui_cs, zorder_cs = _write_gui_helper_sources(tmp_path)
    gui_exe = tmp_path / f"ControllableGuiTarget_{mode}.exe"
    out_json = tmp_path / f"result_{mode}.json"
    args_str = "7" if mode == "nonzero_exit_7" else "0 ignore_close"
    wait_ms = 10000 if mode == "nonzero_exit_7" else 500

    ps1 = tmp_path / f"run_{mode}.ps1"
    ps1.write_text(
        f"""
$ErrorActionPreference = 'Stop'
$cs = [System.IO.File]::ReadAllText('{gui_cs}')
Add-Type -TypeDefinition $cs -ReferencedAssemblies 'System.Windows.Forms','System.Drawing' -OutputAssembly '{gui_exe}' -OutputType WindowsApplication
$csZ = [System.IO.File]::ReadAllText('{zorder_cs}')
Add-Type -TypeDefinition $csZ
. '{R12_V2_SCRIPT}' -LoadHelpersOnly

$wsh = New-Object -ComObject WScript.Shell
$lnk = Join-Path '{tmp_path}' 'Target_{mode}.lnk'
$sc = $wsh.CreateShortcut($lnk)
$sc.TargetPath = '{gui_exe}'
$sc.Arguments = '{args_str}'
$sc.WindowStyle = 1
$sc.Save()

$State = @{{ AppProc = $null }}
$Report = [ordered]@{{
    candidate = 'r12'
    harness_version = 'r12_v2'
    steps = [ordered]@{{}}
    close_attempts = [ordered]@{{}}
    cleanup_failed = $false
    all_passed = $false
}}

try {{
    $before = @(Get-Process -Name '{gui_exe.stem}' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    Start-Process -FilePath $lnk
    $dl = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $dl) {{
        $cands = @(Get-Process -Name '{gui_exe.stem}' -ErrorAction SilentlyContinue | Where-Object {{ $before -notcontains $_.Id }})
        foreach ($c in $cands) {{
            if ([PromoteWindowHelper]::PromoteTitledWindowForPid($c.Id, 'ShortcutGuiTarget')) {{
                $found = @(Get-Process -Name '{gui_exe.stem}' -ErrorAction SilentlyContinue | Where-Object {{
                    $_.Id -eq $c.Id -and $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -eq 'ShortcutGuiTarget'
                }})
                if ($found.Count -gt 0) {{ $State.AppProc = $found[0]; break }}
            }}
        }}
        if ($null -ne $State.AppProc) {{ break }}
        Start-Sleep -Milliseconds 100
    }}
    Assert-Condition ($null -ne $State.AppProc) 'Failed to discover GUI process'
    $retained = Initialize-TrackedProcessHandle -Process $State.AppProc
    Assert-Condition $retained 'Expected handle_retained=true'

    $threw = $false
    try {{
        Invoke-BoundedVerificationLifecycle -Report $Report -OutputPath '{out_json}' -Body {{
            $Report.steps['step4_project_local_test_and_report'] = [ordered]@{{
                passed = $false
                project_id = 1
                run_id = 1
                run_status = 'COMPLETED'
                close_before_uninstall = $null
            }}
            $close1 = Stop-QATesterWindow -Process $State.AppProc -WaitMs {wait_ms}
            $Report.close_attempts['before_uninstall'] = $close1
            $Report.steps['step4_project_local_test_and_report']['close_before_uninstall'] = $close1
            Assert-Condition ($close1.stopped -eq $true -and $close1.forced -eq $false -and $null -eq $close1.error -and $null -eq $close1.exit_code_error -and ($close1.exit_code -is [int]) -and $close1.exit_code -eq 0) "QA-Tester window did not close cleanly before uninstallation: $($close1.error)"
            $State.AppProc = $null
            $Report.steps['step4_project_local_test_and_report']['passed'] = $true
        }} -Cleanup {{
            $cleanupErrorsList = [System.Collections.Generic.List[string]]::new()
            $procToCleanup = $State.AppProc
            $appCleanup = Stop-QATesterWindow -Process $procToCleanup -WaitMs 500
            $State.AppProc = $null
            if ($null -ne $procToCleanup) {{ try {{ $procToCleanup.Dispose() }} catch {{}} }}
            if (-not $appCleanup.stopped -or $appCleanup.error) {{
                $cleanupErrorsList.Add([string]$appCleanup.error)
            }}
            return [ordered]@{{
                passed = ($cleanupErrorsList.Count -eq 0)
                app_window = $appCleanup
                errors = @($cleanupErrorsList)
            }}
        }}
    }} catch {{
        $threw = $true
    }}
    Assert-Condition $threw 'Expected lifecycle to throw on non-clean shutdown'
}} finally {{
    Get-Process -Name '{gui_exe.stem}' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}}
""",
        encoding="utf-8-sig",
    )

    proc = _run_ps51_file(ps1)
    assert proc.returncode == 0, proc.stderr

    saved = json.loads(out_json.read_text(encoding="utf-8"))
    assert saved["all_passed"] is False
    assert saved["cleanup_failed"] is True
    assert saved["steps"]["step4_project_local_test_and_report"]["passed"] is False

    first_close = saved["close_attempts"]["before_uninstall"]
    assert first_close["handle_retained"] is True
    assert first_close["close_main_window_sent"] is True

    if mode == "nonzero_exit_7":
        assert first_close["forced"] is False
        assert type(first_close["exit_code"]) is int and first_close["exit_code"] == 7
        assert first_close["exit_code_error"] is None
        assert "exit_code=7" in first_close["error"]
        assert "exit_code=7" in saved["failure_message"]
        # Cleanup also still sees the retained exit code 7 on the undisposed handle!
        assert saved["cleanup"]["app_window"]["exit_code"] == 7
    else:
        assert first_close["forced"] is True
        assert "required forced termination; shutdown was not clean" in first_close["error"]
        assert "required forced termination; shutdown was not clean" in saved["failure_message"]


def test_unreadable_exception_null_false_and_string_exit_codes_all_rejected(tmp_path: Path):
    ps1 = tmp_path / "check_unverified_cases.ps1"
    ps1.write_text(
        f"""
$ErrorActionPreference = 'Stop'
. '{R12_V2_SCRIPT}' -LoadHelpersOnly

# 1. Real unattached Get-Process object whose process already exited BEFORE SafeHandle was opened -> throws InvalidOperationException
$shortProc = Start-Process -FilePath '{PS51}' -ArgumentList @('-NoProfile','-Command','Start-Sleep -Milliseconds 400; exit 0') -WindowStyle Hidden -PassThru
$unattachedGp = Get-Process -Id $shortProc.Id
$null = $shortProc.WaitForExit(10000)
Start-Sleep -Milliseconds 100
$resException = Stop-QATesterWindow -Process $unattachedGp -WaitMs 500

function New-ProcessDouble($ExitReturn, [bool]$ThrowOnExit = $false) {{
    $d = [pscustomobject]@{{
        Id = 99999
        HasExited = $true
        SafeHandle = $null
    }}
    $d | Add-Member -MemberType ScriptMethod -Name Refresh -Value {{}}
    $d | Add-Member -MemberType ScriptMethod -Name CloseMainWindow -Value {{ return $true }}
    $d | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {{ param($ms) return $true }}
    if ($ThrowOnExit) {{
        $d | Add-Member -MemberType ScriptMethod -Name get_ExitCode -Value {{ throw [System.ComponentModel.Win32Exception]::new(5, 'Access is denied') }}
    }} else {{
        $d | Add-Member -MemberType ScriptMethod -Name get_ExitCode -Value ({{ return $ExitReturn }}.GetNewClosure())
    }}
    return $d
}}

$resNull   = Stop-QATesterWindow -Process (New-ProcessDouble $null)
$resFalse  = Stop-QATesterWindow -Process (New-ProcessDouble $false)
$resStr0   = Stop-QATesterWindow -Process (New-ProcessDouble '0')
$resWin32E = Stop-QATesterWindow -Process (New-ProcessDouble $null -ThrowOnExit $true)

[ordered]@{{
    unattached_exited = $resException
    null_exit         = $resNull
    false_exit        = $resFalse
    string_zero_exit  = $resStr0
    win32_exception   = $resWin32E
}} | ConvertTo-Json -Depth 5 -Compress
""",
        encoding="utf-8-sig",
    )

    proc = _run_ps51_file(ps1)
    assert proc.returncode == 0, proc.stderr
    cases = json.loads(proc.stdout.strip())

    # 1. Real unattached exited Get-Process captures InvalidOperationException and FAILS
    unattached = cases["unattached_exited"]
    assert unattached["handle_retained"] is False
    assert unattached["had_exited_before_close"] is True
    assert unattached["exit_code"] is None
    assert "System.InvalidOperationException" in (unattached["exit_code_error"] or "")
    assert "exit code could not be read" in (unattached["error"] or "")

    # 2. Null exit code is never coerced to 0 and FAILS
    null_c = cases["null_exit"]
    assert null_c["exit_code"] is None
    assert null_c["exit_code_error"] is None
    assert "without a verified zero exit code" in (null_c["error"] or "")

    # 3. Boolean false is never coerced to 0 and FAILS
    false_c = cases["false_exit"]
    assert false_c["exit_code"] is None
    assert "System.Boolean" in (false_c["exit_code_error"] or "")
    assert "exit code could not be read" in (false_c["error"] or "")

    # 4. String "0" is never coerced to int 0 and FAILS
    str_c = cases["string_zero_exit"]
    assert str_c["exit_code"] is None
    assert "System.String" in (str_c["exit_code_error"] or "")
    assert "exit code could not be read" in (str_c["error"] or "")

    # 5. Explicit Win32Exception on get_ExitCode() is captured and FAILS
    win32_c = cases["win32_exception"]
    assert win32_c["exit_code"] is None
    assert "Win32Exception" in (win32_c["exit_code_error"] or "")
    assert "Access is denied" in (win32_c["error"] or "")


def test_packaged_r12_exe_via_lnk_and_get_process_reproduces_r12_fail_and_passes_r12_v2(tmp_path: Path):
    assert R12_APP_EXE.is_file(), f"Missing packaged r12 app executable: {R12_APP_EXE}"
    _, zorder_cs = _write_gui_helper_sources(tmp_path)

    port_orig = _get_free_port()
    port_v2 = _get_free_port()
    while port_v2 == port_orig:
        port_v2 = _get_free_port()

    writable_orig = tmp_path / "writable_orig"
    writable_v2 = tmp_path / "writable_v2"
    writable_orig.mkdir()
    writable_v2.mkdir()

    ps1 = tmp_path / "verify_packaged_r12_lnk.ps1"
    ps1.write_text(
        f"""
$ErrorActionPreference = 'Stop'
$csZOrder = [System.IO.File]::ReadAllText('{zorder_cs}')
Add-Type -TypeDefinition $csZOrder

function Invoke-LnkCleanStartup([int]$Port, [string]$WritableDir) {{
    $env:QA_TESTER_WRITABLE_DIR = $WritableDir
    $env:QA_TESTER_PORT = "$Port"
    $wsh = New-Object -ComObject WScript.Shell
    $lnk = Join-Path $WritableDir "QA Tester $Port.lnk"
    $sc = $wsh.CreateShortcut($lnk)
    $sc.TargetPath = '{R12_APP_EXE}'
    $sc.Arguments = "--port $Port"
    $sc.WorkingDirectory = '{R12_APP_DIR}'
    $sc.WindowStyle = 1
    $sc.Save()

    $existing = @(Get-Process -Name 'QA-Tester' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    Start-Process -FilePath $lnk

    $dl = (Get-Date).AddSeconds(35)
    $appProc = $null
    while ((Get-Date) -lt $dl) {{
        $candidates = @(Get-Process -Name 'QA-Tester' -ErrorAction SilentlyContinue | Where-Object {{ $existing -notcontains $_.Id }})
        foreach ($cand in $candidates) {{
            # Promote 'QA Tester' window above host UAC_InputIndicatorOverlayWnd so .NET MainWindowHandle matches clean VM
            if ([PromoteWindowHelper]::PromoteTitledWindowForPid($cand.Id, 'QA Tester')) {{
                $fresh = @(Get-Process -Name 'QA-Tester' -ErrorAction SilentlyContinue | Where-Object {{
                    $_.Id -eq $cand.Id -and $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -eq 'QA Tester'
                }})
                if ($fresh.Count -gt 0) {{
                    $appProc = $fresh[0]
                    break
                }}
            }}
        }}
        if ($null -ne $appProc) {{ break }}
        Start-Sleep -Milliseconds 300
    }}
    if ($null -eq $appProc) {{ throw "Packaged QA-Tester.exe window did not appear on port $Port" }}
    return $appProc
}}

$procOrig = $null
$procV2 = $null
try {{
    # 1. Packaged r12 QA-Tester.exe under UNMODIFIED r12 harness
    . '{R12_ORIG_SCRIPT}' -LoadHelpersOnly
    $procOrig = Invoke-LnkCleanStartup -Port {port_orig} -WritableDir '{writable_orig}'
    $null = Wait-HttpReady -Url 'http://127.0.0.1:{port_orig}/' -TimeoutSeconds 25
    Start-Sleep -Seconds 3
    $null = [PromoteWindowHelper]::PromoteTitledWindowForPid($procOrig.Id, 'QA Tester')

    $obs = [System.Diagnostics.Process]::GetProcessById($procOrig.Id)
    $origPidVal = $procOrig.Id
    $obsH = $obs.SafeHandle
    $origStop = Stop-QATesterWindow -Process $procOrig -WaitMs 15000
    $procOrig = $null
    $null = $obs.WaitForExit(5000)
    $origTrueKernelExit = $obs.get_ExitCode()
    $obsH.Dispose()
    $obs.Dispose()

    # 2. Packaged r12 QA-Tester.exe under CORRECTED r12_v2 harness
    . '{R12_V2_SCRIPT}' -LoadHelpersOnly
    $procV2 = Invoke-LnkCleanStartup -Port {port_v2} -WritableDir '{writable_v2}'
    $retainedV2 = Initialize-TrackedProcessHandle -Process $procV2
    Assert-Condition $retainedV2 'Failed to retain handle on packaged QA-Tester.exe'
    $null = Wait-HttpReady -Url 'http://127.0.0.1:{port_v2}/' -TimeoutSeconds 25
    Start-Sleep -Seconds 3
    $null = [PromoteWindowHelper]::PromoteTitledWindowForPid($procV2.Id, 'QA Tester')

    $v2Stop = Stop-QATesterWindow -Process $procV2 -WaitMs 15000
    try {{ $procV2.Dispose() }} catch {{}}
    $procV2 = $null

    [ordered]@{{
        orig_pid = $origPidVal
        orig_true_kernel_exit = $origTrueKernelExit
        orig_stop = $origStop
        v2_stop = $v2Stop
    }} | ConvertTo-Json -Depth 6 -Compress
}} finally {{
    Remove-Item Env:\\QA_TESTER_WRITABLE_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:\\QA_TESTER_PORT -ErrorAction SilentlyContinue
    if ($null -ne $procOrig) {{ try {{ if (-not $procOrig.HasExited) {{ Stop-Process -Id $procOrig.Id -Force -ErrorAction SilentlyContinue }} }} catch {{}} }}
    if ($null -ne $procV2) {{ try {{ if (-not $procV2.HasExited) {{ Stop-Process -Id $procV2.Id -Force -ErrorAction SilentlyContinue }} }} catch {{}} }}
}}
""",
        encoding="utf-8-sig",
    )

    proc = _run_ps51_file(ps1, timeout=90)
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout.strip())

    # 1. Unmodified r12 reproduces exact VM failure on real packaged r12 QA-Tester.exe while OS exit code was 0!
    assert res["orig_true_kernel_exit"] == 0
    orig_stop = res["orig_stop"]
    assert orig_stop["stopped"] is True
    assert orig_stop["forced"] is False
    assert orig_stop["exit_code"] is None
    assert orig_stop["error"] == f"QA-Tester process PID {res['orig_pid']} exited without a verified zero exit code (exit_code=)"

    # 2. Corrected r12_v2 cleanly closes real packaged r12 QA-Tester.exe with verified exit_code == 0!
    v2_stop = res["v2_stop"]
    assert v2_stop["stopped"] is True
    assert v2_stop["forced"] is False
    assert v2_stop["handle_retained"] is True
    assert v2_stop["had_exited_before_close"] is False
    assert v2_stop["close_main_window_sent"] is True
    assert type(v2_stop["exit_code"]) is int and v2_stop["exit_code"] == 0
    assert v2_stop["exit_code_error"] is None
    assert v2_stop["error"] is None
