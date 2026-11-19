"""Tests for Chromium upgrade, Windows sandbox enforcement, and CDP functionality:
1. Verifies Playwright == 1.64.1 and Chromium 156.0.8078.4 / revision 1248.
2. Verifies Windows multi-process architecture, strict absence of --no-sandbox flags,
   and verifies native Windows token integrity levels (UNTRUSTED/LOW for renderers,
   MEDIUM for broker) and Job Object containment.
3. Verifies CDP session lifecycle, Fetch domain interception, and security guard boundaries.
"""
import ctypes
from ctypes import wintypes
import importlib.metadata
import os
import subprocess
import json
import hashlib
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import pytest
from playwright.async_api import async_playwright

from app.paths import chromium_launch_args
from app.qa.browser_guard import BrowserMutationGuard
from app.qa.scoped_mutation import ScopedMutationNetworkBarrier
from windows_job_policy import query_broker_jobs

# Win32 definitions for Token / Job inspection
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25
TokenIsAppContainer = 29

class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]

class TOKEN_GROUPS(ctypes.Structure):
    _fields_ = [('Count', wintypes.DWORD), ('Groups', SID_AND_ATTRIBUTES * 1)]

advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.LocalFree.restype = ctypes.c_void_p

advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)

advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL

advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
advapi32.GetTokenInformation.restype = wintypes.BOOL

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
kernel32.IsProcessInJob.restype = wintypes.BOOL

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.GetProcessMitigationPolicy.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
kernel32.GetProcessMitigationPolicy.restype = wintypes.BOOL


def query_token_buffer(token, info_class):
    size = wintypes.DWORD()
    ctypes.set_last_error(0)
    ok = advapi32.GetTokenInformation(token, info_class, None, 0, ctypes.byref(size))
    if ok or ctypes.get_last_error() != 122 or not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    data = (ctypes.c_byte * size.value)()
    if not advapi32.GetTokenInformation(token, info_class, data, size.value, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    return data


def query_windows_process_security(pid: int):
    """Inspect actual Windows token integrity level and job containment for a process."""
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | 0x0400, False, pid)
    if not h_proc:
        return {"pid": pid, "error": f"OpenProcess failed: {ctypes.get_last_error()}"}
    try:
        in_job = wintypes.BOOL()
        if not kernel32.IsProcessInJob(h_proc, None, ctypes.byref(in_job)):
            raise ctypes.WinError(ctypes.get_last_error())

        h_token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(h_proc, TOKEN_QUERY, ctypes.byref(h_token)):
            return {
                "pid": pid,
                "in_job": bool(in_job.value),
                "error": f"OpenProcessToken failed: {ctypes.get_last_error()}",
            }
        try:
            buf = query_token_buffer(h_token, TokenIntegrityLevel)

            tml = ctypes.cast(buf, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
            sid = tml.Label.Sid
            sub_auth_count_ptr = advapi32.GetSidSubAuthorityCount(sid)
            if not sub_auth_count_ptr:
                return {"pid": pid, "in_job": bool(in_job.value), "error": "GetSidSubAuthorityCount failed"}
            count = sub_auth_count_ptr.contents.value
            sub_auth_ptr = advapi32.GetSidSubAuthority(sid, count - 1)
            rid = sub_auth_ptr.contents.value

            level_name = "UNKNOWN"
            if rid < 0x1000:
                level_name = "UNTRUSTED"
            elif rid < 0x2000:
                level_name = "LOW"
            elif rid < 0x3000:
                level_name = "MEDIUM"
            elif rid < 0x4000:
                level_name = "HIGH"
            elif rid >= 0x4000:
                level_name = "SYSTEM"

            is_app_container = wintypes.DWORD()
            ret_len = wintypes.DWORD()
            if not advapi32.GetTokenInformation(
                h_token, TokenIsAppContainer, ctypes.byref(is_app_container),
                ctypes.sizeof(is_app_container), ctypes.byref(ret_len)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            restricted = query_token_buffer(h_token, 11)  # TokenRestrictedSids / TOKEN_GROUPS
            restricted_count = ctypes.cast(restricted, ctypes.POINTER(wintypes.DWORD)).contents.value
            offset = TOKEN_GROUPS.Groups.offset
            assert not restricted_count or offset + restricted_count * ctypes.sizeof(SID_AND_ATTRIBUTES) <= len(restricted)
            groups = (SID_AND_ATTRIBUTES * restricted_count).from_buffer(restricted, offset) if restricted_count else []
            restricted_sids = []
            for group in groups:
                text = wintypes.LPWSTR()
                if not advapi32.ConvertSidToStringSidW(group.Sid, ctypes.byref(text)):
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    restricted_sids.append({'sid': text.value, 'attributes': group.Attributes})
                finally:
                    assert not kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
            privileges = query_token_buffer(h_token, 3)  # TokenPrivileges
            privilege_count = wintypes.DWORD.from_buffer(privileges).value
            mitigations = {}
            for name, policy in [('aslr', 1), ('strict_handle', 3), ('win32k_disable', 4), ('extension_point', 6)]:
                flags = wintypes.DWORD()
                if not kernel32.GetProcessMitigationPolicy(h_proc, policy, ctypes.byref(flags), ctypes.sizeof(flags)):
                    raise ctypes.WinError(ctypes.get_last_error())
                mitigations[name] = flags.value

            return {
                "pid": pid,
                "in_job": bool(in_job.value),
                "integrity_rid": hex(rid),
                "integrity_level": level_name,
                "is_app_container": bool(is_app_container.value),
                "restricted_sid_count": restricted_count,
                "restricted_sids": restricted_sids,
                "privilege_count": privilege_count,
                "kernel_mitigations": mitigations,
            }
        finally:
            kernel32.CloseHandle(h_token)
    finally:
        kernel32.CloseHandle(h_proc)


class TestChromiumSandboxAndCDP:

    def test_playwright_and_chromium_version(self):
        pw_ver = importlib.metadata.version("playwright")
        assert pw_ver == "1.64.1", f"Playwright must be 1.64.1, got {pw_ver}"

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe_path = p.chromium.executable_path
            assert os.path.exists(exe_path), f"Chromium executable not found at {exe_path}"

        if os.name == "nt":
            ps_cmd = f"(Get-Item '{exe_path}').VersionInfo.ProductVersion"
            ver_out = subprocess.check_output(["powershell", "-NoProfile", "-Command", ps_cmd], text=True).strip()
            major_ver = int(ver_out.split(".")[0])
            assert ver_out == '156.0.8078.4', ver_out
        manifest = Path(__import__('playwright').__file__).parent / 'driver/package/browsers.json'
        entries = json.loads(manifest.read_text())['browsers']
        for name in ('chromium', 'chromium-headless-shell'):
            entry = next(e for e in entries if e['name'] == name)
            assert entry['revision'] == '1248' and entry['browserVersion'] == '156.0.8078.4'

    @pytest.mark.asyncio
    @pytest.mark.parametrize("headless_mode", [True, False])
    async def test_windows_sandbox_actual_token_integrity_and_job(self, headless_mode, tmp_path):
        """Verify native Windows sandbox token restrictions and job containment on headless and headed launches."""
        async with async_playwright() as p:
            launch_args = chromium_launch_args(headless=headless_mode) if headless_mode else chromium_launch_args()
            ignore_args = ["--disable-popup-blocking"] if not headless_mode else None
            browser = await p.chromium.launch(
                headless=headless_mode,
                args=launch_args,
                chromium_sandbox=True,
                ignore_default_args=ignore_args,
            )
            evidence = {'headless': headless_mode, 'processes': [],
                        'renderer_policy_verification': 'PENDING'}
            output = Path(os.environ.get('QA_SECURITY_EVIDENCE_DIR', str(tmp_path))) / f'sandbox_{headless_mode}.json'
            try:
                evidence['browser_version'] = browser.version
                assert browser.version == '156.0.8078.4'
                # Open harmless page to ensure renderer process is initialized
                page = await browser.new_page()
                await page.goto("data:text/html,<html><body><h1>Sandbox Integrity Verification</h1></body></html>")

                cdp = await browser.new_browser_cdp_session()
                proc_info = await cdp.send("SystemInfo.getProcessInfo")
                process_list = proc_info.get("processInfo", [])
                assert len(process_list) >= 2, "Expected at least browser broker and renderer process"
                evidence['cdp_processes'] = process_list
                pids = [int(proc['id']) for proc in process_list]
                query = ' OR '.join(f'ProcessId = {pid}' for pid in pids)
                command = "Get-CimInstance Win32_Process -Filter '" + query + "' | Select-Object ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine | ConvertTo-Json -Depth 4 -Compress"
                raw = subprocess.check_output(['powershell', '-NoProfile', '-Command', command], text=True)
                evidence['windows_processes'] = json.loads(raw)
                windows = {row['ProcessId']: row for row in evidence['windows_processes']}
                assert set(windows) == set(pids), 'Incomplete process identity evidence'
                brokers = [p for p in process_list if p['type'] == 'browser']
                assert len(brokers) == 1
                broker_pid = int(brokers[0]['id'])
                for pid, row in windows.items():
                    assert row['CreationDate'] and row['ExecutablePath'] and row['CommandLine']
                    if pid != broker_pid:
                        assert row['ParentProcessId'] == broker_pid, row
                    for forbidden in ('--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu-sandbox', '--no-zygote-sandbox'):
                        assert forbidden not in row['CommandLine']
                binary = Path(windows[broker_pid]['ExecutablePath'])
                evidence['executable_sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
                assert ('chromium_headless_shell-1248' if headless_mode else 'chromium-1248') in str(binary)
                # Capture the live process snapshot before diagnostic navigation can
                # consume/replace Chromium's spare renderer.
                security = {int(proc['id']): query_windows_process_security(int(proc['id'])) for proc in process_list}
                evidence['broker_jobs'] = query_broker_jobs(broker_pid)
                diagnostic = await browser.new_page()
                try:
                    await diagnostic.goto('chrome://sandbox')
                    await diagnostic.wait_for_function("document.body.innerText.includes('policies:')")
                    policy_text = await diagnostic.locator('body').inner_text()
                    evidence['chrome_sandbox_raw'] = policy_text
                except Exception as exc:
                    evidence['chrome_sandbox_error'] = str(exc)
                    assert headless_mode and 'ERR_INVALID_URL' in str(exc), 'Sandbox policy unavailable unexpectedly'
                    policy_text = None
                finally:
                    await diagnostic.close()
                policies = json.JSONDecoder().raw_decode(policy_text.split('policies:', 1)[1].lstrip())[0] if policy_text else []
                by_pid = {row['processId']: row for row in policies}

                renderer_found = False
                broker_found = False
                correlated_renderers = 0

                for proc in process_list:
                    p_type = proc.get("type")
                    pid = proc.get("id")

                    if os.name == "nt":
                        sec_info = security[int(pid)]
                        evidence['processes'].append(dict(role=p_type, **sec_info))
                        assert "error" not in sec_info, f"Failed querying process security for PID {pid}: {sec_info.get('error')}"

                        # Every browser-owned process in the sandbox hierarchy must be contained in a job
                        assert sec_info["in_job"] is True, f"Process {p_type} (PID {pid}) is not in a Job Object"

                        if p_type == "renderer":
                            renderer_found = True
                            # Renderer MUST be restricted: UNTRUSTED or LOW integrity level
                            assert sec_info["integrity_level"] in ("UNTRUSTED", "LOW"), (
                                f"Renderer PID {pid} must have UNTRUSTED or LOW integrity, got {sec_info['integrity_level']}"
                            )
                            assert sec_info['restricted_sid_count'] > 0, 'Renderer has no restricting SIDs'
                            assert 'S-1-0-0' in [s['sid'] for s in sec_info['restricted_sids']], 'Lockdown restricting NULL SID absent'
                            assert sec_info['privilege_count'] == 0, 'Renderer retained token privileges'
                            jobs = [j for j in evidence['broker_jobs']['jobs'] if int(pid) in j['pids']]
                            assert len(jobs) == 1, 'Renderer job must be identified through its own broker handle'
                            job = jobs[0]
                            evidence['processes'][-1]['kernel_job_policy'] = job
                            # Read actual job policy, not a surrounding test runner job.
                            assert job['ui_restrictions'] == 0xff, 'Renderer UI restrictions are incomplete'
                            required = 0x0008 | 0x0400 | 0x2000  # active-process / die-on-exception / kill-on-close
                            assert job['limits']['flags'] & required == required
                            assert job['limits']['active_process_limit'] <= 1
                            assert sec_info['kernel_mitigations']['win32k_disable'] & 1, 'Win32k system calls are not disabled'
                            assert sec_info['kernel_mitigations']['strict_handle'] & 1, 'Strict handle checks absent'
                            if not headless_mode:
                                # The diagnostic navigation may consume a spare renderer.
                                # Its kernel policy above is still mandatory; do not
                                # invent a chrome://sandbox record for a replaced PID.
                                policy = by_pid.get(int(pid))
                                evidence['processes'][-1]['diagnostic_policy'] = policy
                                if policy is not None:
                                    correlated_renderers += 1
                                    assert policy['lockdownLevel'] == 'Lockdown'
                                    assert policy['jobLevel'] == 'Lockdown'
                                    assert policy['desiredIntegrityLevel'] == 'S-1-16-0 Untrusted'
                                    assert policy['platformMitigations']
                                features = json.JSONDecoder().raw_decode(policy_text.split('features:', 1)[1].lstrip())[0]
                                appcontainer = next(f['enabled'] for f in features if f['name'] == 'RendererAppContainer')
                                assert sec_info['is_app_container'] == appcontainer
                        elif p_type == "browser":
                            broker_found = True
                            # Broker runs as MEDIUM integrity (standard user token)
                            assert sec_info["integrity_level"] in ("MEDIUM", "HIGH"), (
                                f"Broker PID {pid} unexpected integrity: {sec_info['integrity_level']}"
                            )

                assert renderer_found, "Renderer process was not identified in processInfo"
                assert broker_found, "Browser broker process was not identified in processInfo"
                if not headless_mode:
                    assert correlated_renderers > 0, 'No live renderer correlated with Chromium diagnostic policy'
                evidence['renderer_policy_verification'] = 'PASS_KERNEL_TOKEN_JOB_MITIGATIONS'
                evidence['policy_limitation'] = ('Headless shell has no chrome://sandbox diagnostic. '
                    'Every snapshot renderer instead requires broker-handle/PID-correlated kernel job limits, '
                    'lockdown token and mitigation measurements. This is not a guarantee against sandbox escape '
                    'or a claim that all browser services are sandboxed.')

            finally:
                output.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
                await browser.close()

    @pytest.mark.asyncio
    async def test_cdp_fetch_interception_and_guard_boundaries(self):
        """Verify CDP Fetch domain interception operates correctly under modern Chromium with sandbox enforced."""
        writes = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'<html><body>Local guard fixture</body></html>')
            def write_request(self):
                writes.append((self.command, self.path))
                self.send_response(200); self.end_headers()
            do_POST = do_PUT = do_PATCH = do_DELETE = write_request
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f'http://127.0.0.1:{server.server_port}'
        try:
            await self._exercise_cdp_guard(origin, writes)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            assert not thread.is_alive()

    async def _exercise_cdp_guard(self, origin, writes):
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=chromium_launch_args(headless=True),
                chromium_sandbox=True,
            )
            try:
                context = await browser.new_context()
                barrier = ScopedMutationNetworkBarrier(origin)
                guard = BrowserMutationGuard(context, [], origin, mutation_barrier=barrier)
                await guard.start()

                page = await context.new_page()
                # Test CDP session attaches and handles navigation without error
                await page.goto(origin)
                title = await page.title()
                assert title == ""

                # Verify guard status
                assert guard.active is True
                for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                    outcome = await page.evaluate("async method => { try { await fetch('/forbidden', {method, body:'forbidden'}); return 'sent'; } catch { return 'blocked'; } }", method)
                    assert outcome == 'blocked'
                assert writes == [], 'An unauthorized mutation reached the local server'

                await page.close()
                await guard.stop()
                assert guard.active is False
            finally:
                await browser.close()
