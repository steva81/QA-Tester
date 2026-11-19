"""Microsoft Edge WebView2 Runtime prerequisite verification for Windows."""

import os
import sys
from typing import Optional, Tuple

WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def check_webview2_runtime(simulate_missing: bool = False) -> Tuple[bool, Optional[str], Optional[str]]:
    """Check whether Microsoft Edge WebView2 Evergreen Runtime is installed.

    Returns:
        (installed: bool, version: str | None, error_message: str | None)
    """
    if (
        simulate_missing
        or os.environ.get("QA_TESTER_SIMULATE_MISSING_WEBVIEW2") == "1"
        or "--simulate-missing-webview2" in sys.argv
    ):
        return (
            False,
            None,
            (
                "Microsoft Edge WebView2 Runtime was not detected (simulated missing runtime). "
                f"Please install Microsoft Edge WebView2 Evergreen Runtime from {WEBVIEW2_DOWNLOAD_URL} "
                "before installing or running QA Tester."
            ),
        )

    if os.name != "nt":
        return True, "non-windows", None

    import winreg

    reg_locations = [
        (
            winreg.HKEY_LOCAL_MACHINE,
            rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}",
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}",
        ),
        (
            winreg.HKEY_CURRENT_USER,
            rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}",
        ),
    ]

    for hive, subkey in reg_locations:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                pv, _ = winreg.QueryValueEx(key, "pv")
                if pv and str(pv).strip() and str(pv).strip() != "0.0.0.0":
                    return True, str(pv).strip(), None
        except OSError:
            continue

    return (
        False,
        None,
        (
            "Microsoft Edge WebView2 Runtime is required to install and run QA Tester 1.0. "
            f"Download and install the Evergreen Runtime from {WEBVIEW2_DOWNLOAD_URL} and run Setup again."
        ),
    )

