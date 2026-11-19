"""Real execution regression tests for packaging P1: Playwright browser collection in QA-Tester.spec."""
from __future__ import annotations

import hashlib
import os
import shutil
from types import SimpleNamespace
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SPEC_PATH = os.path.join(REPO_ROOT, "QA-Tester.spec")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _execute_real_spec(monkeypatch, extra_env: dict[str, str] | None = None) -> dict:
    """Execute the actual QA-Tester.spec file with lightweight PyInstaller spec primitives."""
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    if extra_env:
        for k, v in extra_env.items():
            monkeypatch.setenv(k, v)

    class _AnalysisStub:
        def __init__(self, scripts, **kwargs):
            self.scripts = scripts
            self.kwargs = kwargs
            self.datas = list(kwargs.get("datas", []))
            self.binaries = list(kwargs.get("binaries", []))
            self.hiddenimports = list(kwargs.get("hiddenimports", []))
            self.pure = []
            self.zipped_data = []
            self.zipfiles = []

    def _pyz_stub(*args, **kwargs):
        return SimpleNamespace(args=args, kwargs=kwargs)

    def _exe_stub(*args, **kwargs):
        return SimpleNamespace(args=args, kwargs=kwargs)

    def _collect_stub(*args, **kwargs):
        return SimpleNamespace(args=args, kwargs=kwargs)

    ns = {
        "__file__": SPEC_PATH,
        "__name__": "__main__",
        "Analysis": _AnalysisStub,
        "PYZ": _pyz_stub,
        "EXE": _exe_stub,
        "COLLECT": _collect_stub,
    }
    with open(SPEC_PATH, "r", encoding="utf-8") as sf:
        code = compile(sf.read(), SPEC_PATH, "exec")
    exec(code, ns)
    return ns


def test_real_spec_collects_both_chromium_and_headless_shell_rev_1248(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", r"C:\poisoned\pythonpath")
    ns = _execute_real_spec(monkeypatch)

    # Verify untrusted env var was stripped by QA-Tester.spec itself
    assert "PYTHONPATH" not in os.environ
    assert os.environ.get("PYTHONNOUSERSITE") == "1"

    analysis = ns["a"]
    collected_map = {}
    for src, target_dir in analysis.datas:
        rel_target = os.path.normpath(os.path.join(target_dir, os.path.basename(src))).replace("\\", "/")
        collected_map[rel_target] = src

    chrome_key = "playwright_browsers/chromium-1248/chrome-win64/chrome.exe"
    hs_key = "playwright_browsers/chromium_headless_shell-1248/chrome-headless-shell-win64/chrome-headless-shell.exe"
    assert chrome_key in collected_map, "Real QA-Tester.spec did not collect chromium-1248 chrome.exe"
    assert hs_key in collected_map, "Real QA-Tester.spec did not collect chromium_headless_shell-1248 chrome-headless-shell.exe"

    assert _sha256_file(collected_map[chrome_key]) == "7F08161BBEB2853FFE1780A9DED4985087F52EFB9725D7D8207AABDD42F7CFB9"
    assert _sha256_file(collected_map[hs_key]) == "F77D8E27C8DB1E4AC0399459A00E0C97BB9417ADC393474CABAFFEBE2314ECB0"

    # Verify pythonnet and clr_loader runtime data files were collected by real QA-Tester.spec
    assert any("pythonnet" in t.replace("\\", "/") for _, t in analysis.datas)
    assert any("clr_loader" in t.replace("\\", "/") for _, t in analysis.datas)

    # Verify EXE security flags (console=False, uac_admin=False)
    exe_cfg = ns["exe"].kwargs
    assert exe_cfg["console"] is False
    assert exe_cfg["uac_admin"] is False


def test_real_spec_fails_closed_when_headless_shell_dir_missing(monkeypatch, tmp_path):
    """Negative test: if chromium_headless_shell-1248 is absent in PLAYWRIGHT_BROWSERS_PATH, QA-Tester.spec fails."""
    fake_browsers = tmp_path / "ms-playwright"
    fake_chrome_dir = fake_browsers / "chromium-1248" / "chrome-win64"
    fake_chrome_dir.mkdir(parents=True)
    (fake_chrome_dir / "chrome.exe").write_bytes(b"MZ_STUB")

    with pytest.raises(FileNotFoundError, match="chromium_headless_shell-1248"):
        _execute_real_spec(monkeypatch, {"PLAYWRIGHT_BROWSERS_PATH": str(fake_browsers)})
