"""Focused regression tests for post-r10 maintenance (Tasks 1 & 2)."""
import io
import json
from pathlib import Path
import subprocess

from docx import Document
from PIL import Image, ImageDraw
import pytest

import app.paths as app_paths
from app.qa.report_renderers import render_docx, render_html, render_markdown, render_text
from app.qa.reporting import build_report

REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = REPO_ROOT / "review_evidence" / "v1_0_independent_20261028" / "verify_clean_vm_r10.ps1"
POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def _run_ps51(script_body: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
    wrapped = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n" + script_body
    return subprocess.run(
        [POWERSHELL_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", wrapped],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _count_docx_page_breaks(doc: Document) -> int:
    return sum(
        1
        for p in doc.paragraphs
        for r in p.runs
        if 'w:type="page"' in r._r.xml or "w:type='page'" in r._r.xml
    )


def _docx_all_text(doc: Document) -> str:
    paras = [p.text for p in doc.paragraphs]
    tables = [
        "\n".join(" | ".join(cell.text for cell in row.cells) for row in tbl.rows)
        for tbl in doc.tables
    ]
    return "\n".join(paras + tables)


# ==============================================================================
# TASK 1 — Clean-VM Verification Script Schema, Lifecycle, and Safety Guard
# ==============================================================================


def test_verify_script_has_zero_outdated_receipt_references_and_valid_ps51_ast():
    content = VERIFY_SCRIPT.read_text(encoding="utf-8")
    assert "verified_file_count" not in content
    assert "webview2_runtime_version" not in content
    assert "manifest_verification.verified_files" in content
    assert "webview2_version" in content

    ps_ast_check = f"""
    $errors = $null
    $tokens = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile('{VERIFY_SCRIPT}', [ref]$tokens, [ref]$errors)
    Write-Output "PARSE_ERRORS=$($errors.Count)"
    if ($errors.Count -gt 0) {{
        $errors | ForEach-Object {{ Write-Error $_.ToString() }}
        exit 1
    }}
    """
    proc = _run_ps51(ps_ast_check)
    assert proc.returncode == 0, proc.stderr
    assert "PARSE_ERRORS=0" in proc.stdout


def test_host_safety_guard_blocks_developer_host_execution(tmp_path):
    out_json = tmp_path / "should_not_run.json"
    proc = subprocess.run(
        [
            POWERSHELL_EXE,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(VERIFY_SCRIPT),
            "-OutputJsonPath",
            str(out_json),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert proc.returncode != 0
    combined = proc.stdout + "\n" + proc.stderr
    assert "SAFETY BLOCK" in combined
    assert not out_json.exists()


def test_receipt_validator_matches_actual_setup_installer_schema():
    ps_script = f"""
    . '{VERIFY_SCRIPT}' -LoadHelpersOnly

    $validReceipt = [pscustomobject]@{{
        schema_version  = 2
        app_name        = 'QA Tester'
        publisher       = 'QA Tester Project'
        version         = '1.0.0'
        install_scope   = 'per-user'
        install_dir     = 'C:\\Users\\TestUser\\AppData\\Local\\Programs\\QATester'
        user_data_dir   = 'C:\\Users\\TestUser\\AppData\\Local\\QATester'
        webview2_version = '131.0.2903.112'
        upgrade_mode    = 'fresh'
        previous_version = $null
        manifest_verification = [pscustomobject]@{{
            verified_files = 532
            exe_sha256     = 'FDD6ACFA332B0B0837E300D16EC4CF39A55DD74F1D78CC21D00BC0F6153F5C92'
        }}
        shortcuts = @('C:\\StartMenu\\QA Tester.lnk')
        registry_written = $true
        user_data_preservation = [pscustomobject]@{{
            user_data_dir       = 'C:\\Users\\TestUser\\AppData\\Local\\QATester'
            db_existed_before   = $false
            db_sha256_before    = $null
            db_exists_after     = $false
            db_sha256_after     = $null
            db_preserved_intact = $true
        }}
    }}

    Assert-ValidInstallReceipt -Receipt $validReceipt -ExpectedFiles 532 -ExpectedMode 'fresh' -ExpectedDbExistedBefore $false

    # Outdated schema must fail strict validation
    $legacyOnlyReceipt = [pscustomobject]@{{
        app_name                 = 'QA Tester'
        version                  = '1.0.0'
        verified_file_count      = 532
        webview2_runtime_version = '131.0.2903.112'
        registry_written         = $true
    }}
    $legacyRejected = $false
    try {{
        Assert-ValidInstallReceipt -Receipt $legacyOnlyReceipt -ExpectedFiles 532
    }} catch {{
        $legacyRejected = $true
    }}
    Assert-Condition $legacyRejected 'Legacy receipt schema should have been rejected'

    # Wrong verified_files count must fail
    $badCountReceipt = $validReceipt | ConvertTo-Json -Depth 6 | ConvertFrom-Json
    $badCountReceipt.manifest_verification.verified_files = 531
    $badCountRejected = $false
    try {{
        Assert-ValidInstallReceipt -Receipt $badCountReceipt -ExpectedFiles 532
    }} catch {{
        $badCountRejected = $true
    }}
    Assert-Condition $badCountRejected 'Mismatched verified_files count should have been rejected'

    Write-Output 'RECEIPT_SCHEMA_CHECKS_PASSED'
    """
    proc = _run_ps51(ps_script)
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "RECEIPT_SCHEMA_CHECKS_PASSED" in proc.stdout


def test_local_loopback_helper_serves_swedish_utf8_and_stops_boundedly_without_clients():
    ps_script = f"""
    . '{VERIFY_SCRIPT}' -LoadHelpersOnly

    # Verify HTTP serving with Swedish UTF-8 characters AND idle shutdown with zero waiting clients
    $site1 = Start-LocalLoopbackTestSite
    $resp = Wait-HttpReady -Url $site1.Url -TimeoutSeconds 5
    Assert-Condition ($resp.StatusCode -eq 200) "Expected HTTP 200"
    Assert-Condition ($resp.Content.Contains("Lokal Testsida f" + [char]0x00F6 + "r Ren Windows-VM (" + [char]0x00C5 + [char]0x00C4 + [char]0x00D6 + " " + [char]0x00E5 + [char]0x00E4 + [char]0x00F6 + ")")) "Swedish UTF-8 characters corrupted: $($resp.Content)"
    $stop1 = Stop-LocalLoopbackTestSite -Site $site1 -TimeoutMs 3000
    Assert-Condition ($stop1.stopped -eq $true) "Site1 failed to stop cleanly: $($stop1.error)"
    Assert-Condition ($null -eq $stop1.error) "Site1 reported unexpected error: $($stop1.error)"
    Assert-Condition ($stop1.elapsed_ms -lt 2500) "Site1 stop took too long: $($stop1.elapsed_ms)ms"

    # Also test pure idle start -> immediate stop with zero HTTP connections ever made (exact AcceptTcpClient hang trigger)
    $site2 = Start-LocalLoopbackTestSite
    Start-Sleep -Milliseconds 120
    $stop2 = Stop-LocalLoopbackTestSite -Site $site2 -TimeoutMs 3000
    Assert-Condition ($stop2.stopped -eq $true) "Idle Site2 failed to stop cleanly: $($stop2.error)"
    Assert-Condition ($stop2.elapsed_ms -lt 2500) "Idle Site2 stop took too long: $($stop2.elapsed_ms)ms"

    Write-Output "HELPER_STOP_OK elapsed1=$($stop1.elapsed_ms) elapsed2=$($stop2.elapsed_ms)"
    """
    proc = _run_ps51(ps_script, timeout=15.0)
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "HELPER_STOP_OK" in proc.stdout


def test_bounded_lifecycle_saves_utf8_results_and_reports_cleanup_failure_when_helper_hangs(tmp_path):
    out_json = tmp_path / "cleanup_failure_result.json"
    ps_script = f"""
    . '{VERIFY_SCRIPT}' -LoadHelpersOnly

    $report = [ordered]@{{
        note           = "Svensk rapport kontroll " + [char]0x00C5 + [char]0x00C4 + [char]0x00D6 + " " + [char]0x00E5 + [char]0x00E4 + [char]0x00F6
        steps          = [ordered]@{{ step_body = [ordered]@{{ passed = $true }} }}
        cleanup_failed = $false
        all_passed     = $false
    }}

    # Simulate an uncooperative/hung runspace that ignores StopEvent
    $hungPs = [PowerShell]::Create()
    $null = $hungPs.AddScript({{ Start-Sleep -Seconds 30 }})
    $hungHandle = $hungPs.BeginInvoke()
    $fakeHungSite = [pscustomobject]@{{
        Port      = 0
        Url       = "http://127.0.0.1:0/"
        Listener  = $null
        StopEvent = [System.Threading.ManualResetEventSlim]::new($false)
        Runner    = $hungPs
        Handle    = $hungHandle
    }}

    try {{
        Invoke-BoundedVerificationLifecycle -Report $report -OutputPath '{out_json}' -Body {{
            $report.steps["step_body"].passed = $true
        }} -Cleanup {{
            $errs = [System.Collections.Generic.List[string]]::new()
            $sc = Stop-LocalLoopbackTestSite -Site $fakeHungSite -TimeoutMs 300
            if (-not $sc.stopped -or $sc.error) {{
                $errs.Add([string]$sc.error)
            }}
            return [ordered]@{{
                passed     = ($errs.Count -eq 0)
                local_site = $sc
                errors     = @($errs)
            }}
        }}
        Write-Output "UNEXPECTED_SUCCESS"
        exit 0
    }} catch {{
        Write-Output "CAUGHT_EXPECTED_CLEANUP_FAILURE=$($_.Exception.Message)"
        exit 2
    }}
    """
    proc = _run_ps51(ps_script, timeout=12.0)
    assert proc.returncode == 2, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "CAUGHT_EXPECTED_CLEANUP_FAILURE=CLEANUP FAILED:" in proc.stdout
    assert out_json.exists(), "Result JSON must be written even when cleanup times out"

    raw_bytes = out_json.read_bytes()
    assert not raw_bytes.startswith(b"\xef\xbb\xbf"), "UTF-8 output JSON should not include BOM"
    saved = json.loads(raw_bytes.decode("utf-8"))
    assert saved["note"] == "Svensk rapport kontroll ÅÄÖ åäö"
    assert saved["all_passed"] is False
    assert saved["cleanup_failed"] is True
    assert any("did not stop within 300ms" in err for err in saved["cleanup_errors"])


def test_bounded_lifecycle_preserves_original_test_failure_when_cleanup_also_fails(tmp_path):
    out_json = tmp_path / "both_failed_result.json"
    ps_script = f"""
    . '{VERIFY_SCRIPT}' -LoadHelpersOnly

    $report = [ordered]@{{
        steps          = [ordered]@{{}}
        cleanup_failed = $false
        all_passed     = $false
    }}

    try {{
        Invoke-BoundedVerificationLifecycle -Report $report -OutputPath '{out_json}' -Body {{
            throw ("PRIMARY_ASSERTION_FAILURE_" + [char]0x00C5 + [char]0x00C4 + [char]0x00D6)
        }} -Cleanup {{
            return [ordered]@{{
                passed = $false
                errors = @("SECONDARY_CLEANUP_FAILURE")
            }}
        }}
        exit 0
    }} catch {{
        Write-Output "RETHROWN_MESSAGE=$($_.Exception.Message)"
        exit 3
    }}
    """
    proc = _run_ps51(ps_script, timeout=10.0)
    assert proc.returncode == 3, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "RETHROWN_MESSAGE=PRIMARY_ASSERTION_FAILURE_ÅÄÖ" in proc.stdout
    assert out_json.exists()

    saved = json.loads(out_json.read_text(encoding="utf-8"))
    assert saved["all_passed"] is False
    assert saved["failure_message"] == "PRIMARY_ASSERTION_FAILURE_ÅÄÖ"
    assert saved["cleanup_failed"] is True
    assert "SECONDARY_CLEANUP_FAILURE" in saved["cleanup_errors"]


# ==============================================================================
# TASK 2 — Empty DOCX Findings Page Fix & Non-Empty Findings Preservation
# ==============================================================================


def _build_zero_findings_report(with_crawl_coverage: bool = True):
    run = dict(id=1, project_id=1, status="COMPLETED", start_time="2026-10-28 19:55:00", total_tests=2)
    project = dict(
        id=1,
        name="Clean VM Verification Project",
        target_url="http://127.0.0.1:50000/index.html",
        environment="Local Disposable VM",
        tester_name="CleanVM-Automation",
    )
    tests = [
        dict(
            id=1,
            run_id=1,
            status="PASS",
            viewport="Desktop 1366x768",
            url="http://127.0.0.1:50000/index.html",
            final_url="http://127.0.0.1:50000/index.html",
            stable_id=None,
            timestamp="2026-10-28 19:55:01",
            screenshot_path=None,
            expected_result="Page loads cleanly without errors",
            actual_result="HTTP 200 OK",
        ),
        dict(
            id=2,
            run_id=1,
            status="PASS",
            viewport="Mobile 390x844",
            url="http://127.0.0.1:50000/index.html",
            final_url="http://127.0.0.1:50000/index.html",
            stable_id=None,
            timestamp="2026-10-28 19:55:02",
            screenshot_path=None,
            expected_result="Page loads cleanly without errors",
            actual_result="HTTP 200 OK",
        ),
    ]
    cov = None
    if with_crawl_coverage:
        from app.models.crawl import METRICS

        cov = dict.fromkeys(METRICS, 0)
        cov.update(
            mode="crawl",
            phase="COMPLETED",
            pages_discovered=1,
            pages_eligible=1,
            pages_attempted=1,
            pages_tested=1,
            pages_fully_evaluated=1,
            desktop_pages_tested=1,
            mobile_pages_tested=1,
            page_limit=10,
            page_limit_reached=False,
            discovery_limit_reached=False,
            discovery_safety_limited=False,
            coverage_complete=True,
            pages=[
                dict(
                    requested_url="http://127.0.0.1:50000/index.html",
                    final_url="http://127.0.0.1:50000/index.html",
                    state="TESTED",
                    reason="",
                    desktop_tested=True,
                    mobile_tested=True,
                    desktop_limited=0,
                    mobile_limited=0,
                    discovery_limited=0,
                )
            ],
        )
    return build_report(run, project, [], tests, coverage=cov)


@pytest.mark.parametrize("with_crawl_coverage", [False, True])
def test_zero_findings_reports_omit_empty_detailed_findings_section_and_page_break(with_crawl_coverage):
    report = _build_zero_findings_report(with_crawl_coverage=with_crawl_coverage)
    assert report["customer_finding_count"] == 0
    assert report["findings"] == []

    # Standard DOCX
    std_doc = render_docx(report, "standard")
    std_h1 = [p.text for p in std_doc.paragraphs if p.style.name == "Heading 1"]
    assert std_h1 == ["Run / Project Details", "Executive Summary", "Coverage", "Findings Overview"]
    assert "Detailed Customer Findings" not in _docx_all_text(std_doc)
    assert "No customer findings recorded." in _docx_all_text(std_doc)
    # Exactly 1 controlled page break before 'Coverage', ZERO trailing page breaks after Technical Summary!
    assert _count_docx_page_breaks(std_doc) == 1

    # Full Technical DOCX
    full_doc = render_docx(report, "full")
    full_h1 = [p.text for p in full_doc.paragraphs if p.style.name == "Heading 1"]
    assert "Detailed Customer Findings" not in full_h1
    assert full_h1[-1] == "Technical Appendix — Technical Data"
    assert "Every Technical Record" in _docx_all_text(full_doc)
    # Exactly 2 controlled page breaks: before 'Coverage' and before 'Technical Appendix — Technical Data'
    assert _count_docx_page_breaks(full_doc) == 2

    # HTML / Markdown / Text consistency
    for mode in ("standard", "full"):
        assert "Detailed Customer Findings" not in render_html(report, mode)
        assert "Detailed Customer Findings" not in render_markdown(report, mode)
        assert "Detailed Customer Findings" not in render_text(report, mode)


def test_with_findings_docx_preserves_details_page_breaks_modes_and_privacy(tmp_path, monkeypatch):
    shot_dir = tmp_path / "screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app_paths, "get_screenshots_dir", lambda: str(shot_dir))

    img = Image.new("RGB", (640, 360), color=(240, 248, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([30, 30, 610, 110], fill=(18, 102, 95))
    shot_file = shot_dir / "qa101.png"
    img.save(shot_file)

    run = dict(id=2, project_id=1, status="COMPLETED", start_time="2026-10-28 20:05:00", total_tests=3)
    project = dict(
        id=1,
        name="Findings Verification Project",
        target_url="http://127.0.0.1:50000/",
        environment="Local",
        tester_name="Tester",
    )
    tests = [
        dict(
            id=10,
            run_id=2,
            status="PASS",
            viewport="Desktop 1366x768",
            url="http://127.0.0.1:50000/",
            final_url="http://127.0.0.1:50000/",
            stable_id=None,
            timestamp="2026-10-28 20:05:01",
            screenshot_path=None,
            expected_result="Home loads",
            actual_result="HTTP 200 OK",
        ),
        dict(
            id=11,
            run_id=2,
            status="FAIL",
            viewport="Desktop 1366x768",
            url="http://127.0.0.1:50000/profile",
            final_url="http://127.0.0.1:50000/profile",
            stable_id="QA-101",
            timestamp="2026-10-28 20:05:02",
            screenshot_path=str(shot_file),
            expected_result="Profile save succeeds without JS errors",
            actual_result="Console/JS errors detected: TypeError: profileForm is null",
        ),
        dict(
            id=12,
            run_id=2,
            status="OBSERVATION",
            viewport="Mobile 390x844",
            url="http://127.0.0.1:50000/settings",
            final_url="http://127.0.0.1:50000/settings",
            stable_id="QA-102",
            timestamp="2026-10-28 20:05:03",
            screenshot_path=None,
            expected_result="Settings accordion opens",
            actual_result="Target disclosure remained hidden",
        ),
    ]
    private_sv = "PRIVAT-SVENSK-ANTECKNING-999"
    private_oral = "PRIVAT-MUNTLIG-FORKLARING-999"
    findings = [
        dict(
            id=1,
            project_id=1,
            run_id=2,
            stable_id="QA-101",
            title="Profile action throws TypeError",
            area="Profile Controls",
            severity="High",
            risk="High",
            confidence="High",
            severity_origin="manual",
            risk_origin="baseline",
            confidence_origin="baseline",
            status="OPEN",
            review_status="APPROVED",
            plain_language_explanation="Clicking save throws a TypeError and blocks profile updates.",
            description="Detailed technical description for QA-101",
            impact="Desktop users cannot save profile changes.",
            recommendation="Check form reference before reading properties.",
            learning_explanation_sv=private_sv,
            how_to_explain_sv=private_oral,
        ),
        dict(
            id=2,
            project_id=1,
            run_id=2,
            stable_id="QA-102",
            title="Settings disclosure stays hidden",
            area="Settings",
            severity="Medium",
            risk="Medium",
            confidence="High",
            severity_origin="baseline",
            risk_origin="baseline",
            confidence_origin="baseline",
            status="OPEN",
            review_status="PENDING",
            plain_language_explanation="Tapping the settings toggle leaves the panel hidden on mobile.",
            description="Detailed technical description for QA-102",
            impact="Mobile users cannot access advanced settings.",
            recommendation="Toggle hidden attribute on controlled panel.",
            learning_explanation_sv=private_sv,
            how_to_explain_sv=private_oral,
        ),
    ]

    report = build_report(run, project, findings, tests, coverage=None)

    # Standard DOCX
    buf_std = io.BytesIO()
    render_docx(report, "standard").save(buf_std)
    std_doc = Document(io.BytesIO(buf_std.getvalue()))
    std_text = _docx_all_text(std_doc)
    std_h1 = [p.text for p in std_doc.paragraphs if p.style.name == "Heading 1"]
    assert "Detailed Customer Findings" in std_h1
    assert "Technical Appendix — Technical Data" not in std_h1
    assert "QA-101" in std_text and "QA-102" in std_text
    assert "Clicking save throws a TypeError and blocks profile updates." in std_text
    assert "Desktop users cannot save profile changes." in std_text
    assert "Check form reference before reading properties." in std_text
    assert len(std_doc.inline_shapes) == 1
    # 3 page breaks: before Coverage, before Detailed Customer Findings, and before second finding QA-102
    assert _count_docx_page_breaks(std_doc) == 3
    assert private_sv not in std_text and private_oral not in std_text

    # Full Technical DOCX
    buf_full = io.BytesIO()
    render_docx(report, "full").save(buf_full)
    full_doc = Document(io.BytesIO(buf_full.getvalue()))
    full_text = _docx_all_text(full_doc)
    full_h1 = [p.text for p in full_doc.paragraphs if p.style.name == "Heading 1"]
    assert "Detailed Customer Findings" in full_h1
    assert "Technical Appendix — Technical Data" in full_h1
    assert "Suggested Severity" in full_text
    assert "Detailed technical description for QA-101" in full_text
    assert len(full_doc.inline_shapes) == 1
    # 4 page breaks: Coverage, Detailed Customer Findings, second finding QA-102, Technical Appendix
    assert _count_docx_page_breaks(full_doc) == 4
    assert private_sv not in full_text and private_oral not in full_text
