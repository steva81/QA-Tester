"""Independent tests: non-clean process teardown must fail the VM helper contract."""
import json
import subprocess
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / 'review_evidence/r11_candidate_20261102/verify_clean_vm_r11.ps1'
PS = r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
@pytest.mark.parametrize('kind', ['forced', 'nonzero', 'clean'])
def test_vm_teardown_truthful(kind, tmp_path):
    output = tmp_path / "lifecycle.json"
    body = 'Start-Sleep -Seconds 20' if kind == 'forced' else ('exit 7' if kind == 'nonzero' else 'exit 0')
    script = f"""
    . '{HARNESS}' -LoadHelpersOnly
    $p = Start-Process -FilePath '{PS}' -ArgumentList @('-NoProfile','-Command','{body}') -WindowStyle Hidden -PassThru
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
    """
    proc = subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script], capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    saved = json.loads(output.read_text(encoding='utf-8'))
    assert saved['all_passed'] is (kind == 'clean')
    if kind == 'clean':
        assert result['stopped'] and not result['forced'] and result['exit_code'] == 0 and not result['error']
    else:
        assert result['error'], f'{kind} teardown must surface failure to lifecycle: {result}'
