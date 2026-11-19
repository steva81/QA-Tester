"""Independent counterexamples and focused post-r10 regression coverage."""
import json
import os
from pathlib import Path
import pytest
from test_post_r10_maintenance import _run_ps51, VERIFY_SCRIPT

VERIFY_SCRIPT = Path(os.environ.get('QA_REVIEW_HARNESS', str(VERIFY_SCRIPT)))


@pytest.mark.parametrize('failure', ["throw 'HELPER_CRASH'", "Write-Error 'HELPER_CRASH'"])
def test_failed_helper_cannot_report_success(tmp_path, failure):
    output = tmp_path / 'failed.json'
    proc = _run_ps51(f"""
    . '{VERIFY_SCRIPT}' -LoadHelpersOnly
    $ps = [PowerShell]::Create()
    $null = $ps.AddScript({{ {failure} }})
    $h = $ps.BeginInvoke()
    $null = $h.AsyncWaitHandle.WaitOne(3000)
    $site = [pscustomobject]@{{ Runner=$ps; Handle=$h; Listener=$null; StopEvent=[System.Threading.ManualResetEventSlim]::new($false) }}
    $report = [ordered]@{{}}
    try {{
        Invoke-BoundedVerificationLifecycle $report '{output}' {{}} {{
            $r = Stop-LocalLoopbackTestSite $site
            return [ordered]@{{ errors=@(if ($r.error) {{ $r.error }}); passed=$r.stopped }}
        }}
    }} catch {{ Write-Output $_.Exception.Message }}
    """)
    assert proc.returncode == 0, proc.stderr
    saved = json.loads(output.read_text(encoding='utf-8'))
    assert saved['all_passed'] is False
    assert saved['cleanup_failed'] is True
    assert 'HELPER_CRASH' in str(saved['cleanup_errors'])


def test_cleanup_false_without_error_list_is_failure(tmp_path):
    output = tmp_path / 'cleanup.json'
    proc = _run_ps51(f"""
    . '{VERIFY_SCRIPT}' -LoadHelpersOnly
    $r = [ordered]@{{}}
    try {{ Invoke-BoundedVerificationLifecycle $r '{output}' {{}} {{ return @{{passed=$false; errors=@()}} }} }} catch {{ Write-Output $_.Exception.Message }}
    """)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(output.read_text())['all_passed'] is False


def test_incomplete_client_and_scope_survive_lifecycle(tmp_path):
    output = tmp_path / 'scope.json'
    proc = _run_ps51(f"""
    . '{VERIFY_SCRIPT}' -LoadHelpersOnly
    $state = @{{LocalSite=$null; AppProc=$null; Client=$null}}
    $report = [ordered]@{{steps=@{{}}}}
    Invoke-BoundedVerificationLifecycle $report '{output}' {{
        $state.LocalSite = Start-LocalLoopbackTestSite
        $state.AppProc = 'scope-marker'
        $report.steps.body = 'retained'
        $state.Client = [System.Net.Sockets.TcpClient]::new('127.0.0.1', $state.LocalSite.Port)
        $s = $state.Client.GetStream()
        $b = [Text.Encoding]::ASCII.GetBytes('GET / HTTP/1.1')
        $s.Write($b,0,$b.Length)
        Start-Sleep -Milliseconds 150
    }} {{
        Assert-Condition ($state.AppProc -eq 'scope-marker') 'lost AppProc'
        $r = Stop-LocalLoopbackTestSite $state.LocalSite -TimeoutMs 3000
        $state.Client.Close()
        Assert-Condition ($r.elapsed_ms -lt 2800) 'unbounded socket read'
        return @{{passed=$r.stopped; errors=@(if ($r.error) {{$r.error}})}}
    }}
    """, timeout=12)
    assert proc.returncode == 0, proc.stderr
    saved = json.loads(output.read_text())
    assert saved['all_passed'] is True
    assert saved['steps']['body'] == 'retained'


def test_slow_trickling_request_stops_without_timeout():
    proc = _run_ps51(f"""
    . '{VERIFY_SCRIPT}' -LoadHelpersOnly
    $site = Start-LocalLoopbackTestSite
    $sender = [PowerShell]::Create()
    $null = $sender.AddScript({{
        param($port)
        $c = [Net.Sockets.TcpClient]::new('127.0.0.1',$port)
        try {{
            $stream = $c.GetStream()
            for ($i=0; $i -lt 50; $i++) {{
                $stream.WriteByte(65)
                Start-Sleep -Milliseconds 100
            }}
        }} catch {{}} finally {{ $c.Close() }}
    }}).AddArgument($site.Port)
    $h = $sender.BeginInvoke()
    Start-Sleep -Milliseconds 400
    $r = Stop-LocalLoopbackTestSite $site -TimeoutMs 1000
    $null = $h.AsyncWaitHandle.WaitOne(6000)
    $null = $sender.EndInvoke($h)
    $sender.Dispose()
    Assert-Condition $r.stopped "Trickling request defeated shutdown: $($r.error)"
    Assert-Condition ($r.elapsed_ms -lt 1000) 'Read did not observe stop event'
    """, timeout=12)
    assert proc.returncode == 0, proc.stderr


def test_noninterruptible_fallback_returns_and_saves_failure(tmp_path):
    output = tmp_path / 'fallback.json'
    proc = _run_ps51(f"""
    . '{VERIFY_SCRIPT}' -LoadHelpersOnly
    $ps = [PowerShell]::Create()
    $null = $ps.AddScript({{ [Threading.Thread]::Sleep(4000) }})
    $h = $ps.BeginInvoke()
    Start-Sleep -Milliseconds 150
    $site = [pscustomobject]@{{Runner=$ps; Handle=$h; Listener=$null; StopEvent=[Threading.ManualResetEventSlim]::new($false)}}
    $report = [ordered]@{{}}
    $clock = [Diagnostics.Stopwatch]::StartNew()
    try {{
        Invoke-BoundedVerificationLifecycle $report '{output}' {{}} {{
            $r = Stop-LocalLoopbackTestSite $site -TimeoutMs 100
            return @{{passed=$r.stopped; errors=@($r.error)}}
        }}
    }} catch {{ Write-Output $_.Exception.Message }}
    Assert-Condition ($clock.ElapsedMilliseconds -lt 2000) 'Fallback blocked on Dispose/EndStop'
    Assert-Condition (Test-Path '{output}') 'JSON was not saved before helper returned'
    # Dispose only after the deliberately uninterruptible operation completes.
    $null = $h.AsyncWaitHandle.WaitOne(5000)
    $ps.Dispose()
    """, timeout=12)
    assert proc.returncode == 0, proc.stderr
    saved = json.loads(output.read_text())
    assert saved['all_passed'] is False and saved['cleanup_failed'] is True


@pytest.mark.parametrize('existing_database', [False, True])
def test_receipt_preservation_fields_for_fresh_and_reinstall(tmp_path, existing_database):
    from installer.setup_installer import _ensure_user_data_dirs_preserved
    data = tmp_path / 'userdata'
    if existing_database:
        (data / 'data').mkdir(parents=True)
        (data / 'data/qa-tester.db').write_bytes(b'isolated preserved database marker')
    preservation = _ensure_user_data_dirs_preserved(str(data))
    # Only the fields actually asserted by the harness, using the installer's
    # real preservation result rather than the maintenance test's extra keys.
    receipt = dict(app_name='QA Tester', version='1.0.0', webview2_version='156.0.4314.58',
                   registry_written=True, upgrade_mode='fresh',
                   manifest_verification=dict(verified_files=532, exe_sha256='FDD6ACFA332B0B0837E300D16EC4CF39A55DD74F1D78CC21D00BC0F6153F5C92'),
                   user_data_preservation=preservation)
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps(receipt), encoding='utf-8')
    expected = '$true' if existing_database else '$false'
    proc = _run_ps51(f"""
    . '{VERIFY_SCRIPT}' -LoadHelpersOnly
    $r = Read-Utf8Json '{path}'
    Assert-ValidInstallReceipt $r -ExpectedMode fresh -ExpectedDbExistedBefore {expected}
    $rejected = $false
    try {{ Assert-ValidInstallReceipt $r -ExpectedDbExistedBefore (-not {expected}) }} catch {{ $rejected = $true }}
    Assert-Condition $rejected 'Incorrect database pre-state accepted'
    """)
    assert proc.returncode == 0, proc.stderr
    if existing_database:
        assert preservation['db_sha256_before'] == preservation['db_sha256_after']


def test_repeated_immediate_idle_shutdown_has_no_listener_stop_race():
    proc = _run_ps51(f"""
    . '{VERIFY_SCRIPT}' -LoadHelpersOnly
    for ($i=0; $i -lt 30; $i++) {{
        $site = Start-LocalLoopbackTestSite
        if ($i % 2 -eq 0) {{ Start-Sleep -Milliseconds 45 }}
        $r = Stop-LocalLoopbackTestSite $site -TimeoutMs 1000
        Assert-Condition $r.stopped "Stop race at iteration $i : $($r.error)"
    }}
    """, timeout=20)
    assert proc.returncode == 0, proc.stderr
