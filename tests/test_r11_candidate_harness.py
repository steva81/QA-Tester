"""Focused tests for candidate-specific r11 Clean-VM verification harness (`verify_clean_vm_r11.ps1`)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parent.parent
R11_SCRIPT = ROOT / "review_evidence" / "r11_candidate_20261102" / "verify_clean_vm_r11.ps1"
R11_IDENTITY = ROOT / "review_evidence" / "r11_candidate_20261102" / "identity_r11.json"
PS51 = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def _run_ps51(command: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PS51, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def test_r11_harness_constants_match_built_candidate_identity():
    identity = json.loads(R11_IDENTITY.read_text(encoding="utf-8"))
    proc = _run_ps51(f"""
    . '{R11_SCRIPT}' -LoadHelpersOnly
    [ordered]@{{
        setup = $ExpectedSetupHash
        app = $ExpectedAppExeHash
        uninstall = $ExpectedUninstallHash
        files = $ExpectedFileCount
    }} | ConvertTo-Json -Compress
    """)
    assert proc.returncode == 0, proc.stderr
    vals = json.loads(proc.stdout.strip())
    assert vals["setup"] == identity["r11_hashes"]["setup_exe_sha256"]
    assert vals["app"] == identity["r11_hashes"]["app_exe_sha256"]
    assert vals["uninstall"] == identity["r11_hashes"]["uninstaller_exe_sha256"]
    assert vals["files"] == identity["payload_file_count"] == 532


def test_r11_harness_accepts_r11_receipt_and_rejects_r10_exe_hash(tmp_path):
    identity = json.loads(R11_IDENTITY.read_text(encoding="utf-8"))
    r11_app_sha = identity["r11_hashes"]["app_exe_sha256"]
    r10_app_sha = "FDD6ACFA332B0B0837E300D16EC4CF39A55DD74F1D78CC21D00BC0F6153F5C92"

    good_receipt = {
        "app_name": "QA Tester",
        "version": "1.0.0",
        "webview2_version": "156.0.4314.58",
        "registry_written": True,
        "upgrade_mode": "fresh",
        "manifest_verification": {
            "verified_files": identity["payload_file_count"],
            "exe_sha256": r11_app_sha,
        },
        "user_data_preservation": {
            "db_preserved_intact": True,
            "db_existed_before": False,
        },
    }
    bad_receipt = dict(
        good_receipt,
        manifest_verification={"verified_files": 532, "exe_sha256": r10_app_sha},
    )
    good_path = tmp_path / "good_r11.json"
    bad_path = tmp_path / "bad_r10.json"
    good_path.write_text(json.dumps(good_receipt), encoding="utf-8")
    bad_path.write_text(json.dumps(bad_receipt), encoding="utf-8")

    proc = _run_ps51(f"""
    . '{R11_SCRIPT}' -LoadHelpersOnly
    $good = Read-Utf8Json '{good_path}'
    Assert-ValidInstallReceipt $good -ExpectedMode fresh -ExpectedDbExistedBefore $false
    $rejected = $false
    try {{
        $bad = Read-Utf8Json '{bad_path}'
        Assert-ValidInstallReceipt $bad -ExpectedMode fresh -ExpectedDbExistedBefore $false
    }} catch {{
        $rejected = $true
    }}
    Assert-Condition $rejected 'r11 harness failed to reject r10 application hash!'
    """)
    assert proc.returncode == 0, proc.stderr


def test_r11_harness_host_safety_block_refuses_developer_host(tmp_path):
    out_json = tmp_path / "should_not_exist.json"
    proc = subprocess.run(
        [
            PS51,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(R11_SCRIPT),
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


@pytest.mark.parametrize("failure", ["throw 'R11_HELPER_ERR'", "Write-Error 'R11_HELPER_ERR'"])
def test_r11_harness_bounded_cleanup_and_utf8_preserved(tmp_path, failure):
    output = tmp_path / "r11_lifecycle.json"
    proc = _run_ps51(f"""
    . '{R11_SCRIPT}' -LoadHelpersOnly
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
    assert "R11_HELPER_ERR" in str(saved["cleanup_errors"])

