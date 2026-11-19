"""Focused tests for candidate-specific r12 Clean-VM verification harness (`verify_clean_vm_r12.ps1`)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parent.parent
R12_SCRIPT = ROOT / "review_evidence" / "r12_candidate_20261103" / "verify_clean_vm_r12.ps1"
R12_IDENTITY = ROOT / "review_evidence" / "r12_candidate_20261103" / "identity_r12.json"
PS51 = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

R10_APP_SHA = "FDD6ACFA332B0B0837E300D16EC4CF39A55DD74F1D78CC21D00BC0F6153F5C92"
R11_APP_SHA = "6DF1C27BBA283FE0B66D35DDA482E179A3C209AED43BE8DEC4C508304A22121F"


def _run_ps51(command: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PS51, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def test_r12_harness_has_utf8_bom_valid_ps51_ast_and_matches_built_identity():
    raw_bytes = R12_SCRIPT.read_bytes()
    assert raw_bytes.startswith(b"\xef\xbb\xbf"), "verify_clean_vm_r12.ps1 must retain UTF-8 BOM for PowerShell 5.1"

    identity = json.loads(R12_IDENTITY.read_text(encoding="utf-8"))
    proc = _run_ps51(f"""
    $tokens = $null
    $errors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile('{R12_SCRIPT}', [ref]$tokens, [ref]$errors)
    if ($errors.Count -gt 0) {{ throw "PS5.1 parse errors: $($errors | Out-String)" }}
    . '{R12_SCRIPT}' -LoadHelpersOnly
    [ordered]@{{
        setup = $ExpectedSetupHash
        app = $ExpectedAppExeHash
        uninstall = $ExpectedUninstallHash
        files = $ExpectedFileCount
    }} | ConvertTo-Json -Compress
    """)
    assert proc.returncode == 0, proc.stderr
    vals = json.loads(proc.stdout.strip())
    assert vals["setup"] == identity["r12_hashes"]["setup_exe_sha256"]
    assert vals["app"] == identity["r12_hashes"]["app_exe_sha256"]
    assert vals["uninstall"] == identity["r12_hashes"]["uninstaller_exe_sha256"]
    assert vals["files"] == identity["payload_file_count"] == 532


def test_r12_harness_accepts_r12_receipt_and_rejects_both_r10_and_r11_exe_hashes(tmp_path):
    identity = json.loads(R12_IDENTITY.read_text(encoding="utf-8"))
    r12_app_sha = identity["r12_hashes"]["app_exe_sha256"]

    good_receipt = {
        "app_name": "QA Tester",
        "version": "1.0.0",
        "webview2_version": "156.0.4314.58",
        "registry_written": True,
        "upgrade_mode": "fresh",
        "manifest_verification": {
            "verified_files": identity["payload_file_count"],
            "exe_sha256": r12_app_sha,
        },
        "user_data_preservation": {
            "db_preserved_intact": True,
            "db_existed_before": False,
        },
    }
    bad_r10 = dict(good_receipt, manifest_verification={"verified_files": 532, "exe_sha256": R10_APP_SHA})
    bad_r11 = dict(good_receipt, manifest_verification={"verified_files": 532, "exe_sha256": R11_APP_SHA})

    good_path = tmp_path / "good_r12.json"
    bad_r10_path = tmp_path / "bad_r10.json"
    bad_r11_path = tmp_path / "bad_r11.json"
    good_path.write_text(json.dumps(good_receipt), encoding="utf-8")
    bad_r10_path.write_text(json.dumps(bad_r10), encoding="utf-8")
    bad_r11_path.write_text(json.dumps(bad_r11), encoding="utf-8")

    proc = _run_ps51(f"""
    . '{R12_SCRIPT}' -LoadHelpersOnly
    $good = Read-Utf8Json '{good_path}'
    Assert-ValidInstallReceipt $good -ExpectedMode fresh -ExpectedDbExistedBefore $false
    foreach ($p in @('{bad_r10_path}', '{bad_r11_path}')) {{
        $rejected = $false
        try {{
            $bad = Read-Utf8Json $p
            Assert-ValidInstallReceipt $bad -ExpectedMode fresh -ExpectedDbExistedBefore $false
        }} catch {{
            $rejected = $true
        }}
        Assert-Condition $rejected "r12 harness failed to reject stale hash in $p"
    }}
    """)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("kind", ["forced", "nonzero", "clean"])
def test_r12_harness_teardown_rejects_forced_and_nonzero_exit(kind, tmp_path):
    output = tmp_path / f"lifecycle_{kind}.json"
    body = "Start-Sleep -Seconds 20" if kind == "forced" else ("exit 7" if kind == "nonzero" else "exit 0")
    proc = _run_ps51(f"""
    . '{R12_SCRIPT}' -LoadHelpersOnly
    $p = Start-Process -FilePath '{PS51}' -ArgumentList @('-NoProfile','-Command','{body}') -WindowStyle Hidden -PassThru
    try {{
        if ('{kind}' -ne 'forced') {{ $null = $p.WaitForExit(10000) }}
        $r = Stop-QATesterWindow $p -WaitMs 100
        try {{
            Invoke-BoundedVerificationLifecycle ([ordered]@{{}}) '{output}' {{}} {{
                return [ordered]@{{ passed=($r.stopped -and -not $r.error); errors=@(if ($r.error) {{ $r.error }}) }}
            }}
        }} catch {{ $null = $_ }}
        $r | ConvertTo-Json -Compress
    }} finally {{ if (-not $p.HasExited) {{ Stop-Process -Id $p.Id -Force }} }}
    """)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip())
    saved = json.loads(output.read_text(encoding="utf-8"))
    # Output JSON itself must be UTF-8 without BOM
    assert not output.read_bytes().startswith(b"\xef\xbb\xbf")
    assert saved["all_passed"] is (kind == "clean")
    if kind == "clean":
        assert result["stopped"] and not result["forced"] and result["exit_code"] == 0 and not result["error"]
    else:
        assert result["error"], f"{kind} teardown must surface failure to lifecycle: {result}"


def test_r12_harness_host_safety_block_refuses_developer_host(tmp_path):
    out_json = tmp_path / "should_not_exist.json"
    proc = subprocess.run(
        [
            PS51,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(R12_SCRIPT),
            "-OutputJsonPath",
            str(out_json),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert proc.returncode != 0
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert "SAFETY BLOCK:" in combined
    assert not out_json.exists()


@pytest.mark.parametrize("failure", ["throw 'R12_HELPER_ERR'", "Write-Error 'R12_HELPER_ERR'"])
def test_r12_harness_bounded_cleanup_and_utf8_preserved(tmp_path, failure):
    output = tmp_path / "r12_lifecycle.json"
    proc = _run_ps51(f"""
    . '{R12_SCRIPT}' -LoadHelpersOnly
    $site = Start-LocalLoopbackTestSite
    $resp = Wait-HttpReady -Url $site.Url -TimeoutSeconds 10
    $expectedSv = "Lokal Testsida f$([char]0x00F6)r Ren Windows-VM ($([char]0x00C5)$([char]0x00C4)$([char]0x00D6) $([char]0x00E5)$([char]0x00E4)$([char]0x00F6))"
    Assert-Condition ($resp.Content.Contains($expectedSv)) 'UTF-8 mismatch'
    $cleanStop = Stop-LocalLoopbackTestSite $site -TimeoutMs 2000
    Assert-Condition $cleanStop.stopped 'Clean stop failed'

    $ps = [PowerShell]::Create()
    $null = $ps.AddScript({{ {failure} }})
    $h = $ps.BeginInvoke()
    $null = $h.AsyncWaitHandle.WaitOne(3000)
    $badSite = [pscustomobject]@{{ Runner=$ps; Handle=$h; Listener=$null; StopEvent=[System.Threading.ManualResetEventSlim]::new($false) }}
    $report = [ordered]@{{ note = "ÅÄÖ åäö" }}
    try {{
        Invoke-BoundedVerificationLifecycle $report '{output}' {{}} {{
            $r = Stop-LocalLoopbackTestSite $badSite
            return [ordered]@{{ errors=@(if ($r.error) {{ $r.error }}); passed=$r.stopped }}
        }}
    }} catch {{ Write-Output $_.Exception.Message }}
    """)
    assert proc.returncode == 0, proc.stderr
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["all_passed"] is False
    assert saved["cleanup_failed"] is True
    assert saved["note"] == "ÅÄÖ åäö"
    assert "R12_HELPER_ERR" in str(saved["cleanup_errors"])

