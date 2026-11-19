# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
import playwright

# ==========================================
# 0. CONTROLLED BUILD ENVIRONMENT (SANITIZED PATH)
# ==========================================
# Prevent PyInstaller bindepend from harvesting arbitrary CRT/API-set DLLs
# from unrelated PATH directories (such as Eclipse Adoptium JDK or user tools).
_venv_scripts = os.path.abspath(os.path.dirname(sys.executable))
_base_py = os.path.abspath(getattr(sys, 'base_prefix', sys.prefix))
_sys_root = os.environ.get('SystemRoot', r'C:\Windows')
_controlled_path_entries = [
    _venv_scripts,
    _base_py,
    os.path.join(_base_py, 'DLLs'),
    os.path.join(_base_py, 'Library', 'bin'),
    os.path.join(_sys_root, 'System32'),
    _sys_root,
    os.path.join(_sys_root, 'System32', 'Wbem'),
    os.path.join(_sys_root, 'System32', 'WindowsPowerShell', 'v1.0'),
]
for _untrusted_var in (
    'PYTHONPATH',
    'PYTHONHOME',
    'PYTHONSTARTUP',
    'PYTHONUSERBASE',
    'PYTHONEXECUTABLE',
    '__PYVENV_LAUNCHER__',
):
    os.environ.pop(_untrusted_var, None)
os.environ['PYTHONNOUSERSITE'] = '1'
os.environ['PATH'] = os.pathsep.join([p for p in _controlled_path_entries if os.path.isdir(p)])

# ==========================================
# 1. DYNAMIC PLAYWRIGHT DISCOVERY
# ==========================================
# We do not hardcode Playwright browser revisions.
# Instead, we query the currently installed version and browsers.json.
import json

try:
    with sync_playwright() as p:
        chromium_exe = p.chromium.executable_path
except Exception as e:
    print(f"ERROR: Could not discover Playwright Chromium path. {e}")
    sys.exit(1)

# Get the python playwright package directory (to include driver/node and read browsers.json)
playwright_pkg_dir = os.path.dirname(playwright.__file__)
browsers_json_path = os.path.join(playwright_pkg_dir, 'driver', 'package', 'browsers.json')
if not os.path.isfile(browsers_json_path):
    print(f"ERROR: Playwright browsers.json not found at {browsers_json_path}")
    sys.exit(1)

with open(browsers_json_path, 'r', encoding='utf-8') as f:
    browsers_spec = json.load(f)

# ms-playwright root directory: C:\Users\<user>\AppData\Local\ms-playwright
ms_playwright_dir = os.path.dirname(os.path.dirname(os.path.dirname(chromium_exe)))

from PyInstaller.utils.hooks import collect_data_files

# ==========================================
# 2. BUNDLED RESOURCES
# ==========================================
# We explicitly include read-only Flask resources and required browser binaries.
# We explicitly EXCLUDE: data/, screenshots/, reports/, exports/, logs/, and settings/
chromium_datas = []
for b in browsers_spec.get('browsers', []):
    b_name = b.get('name')
    b_rev = b.get('revision')
    if b_name in ('chromium', 'chromium-headless-shell'):
        dir_name = f"{b_name.replace('-', '_')}-{b_rev}" if b_name == 'chromium-headless-shell' else f"{b_name}-{b_rev}"
        b_dir = os.path.join(ms_playwright_dir, dir_name)
        if not os.path.isdir(b_dir):
            raise FileNotFoundError(f"Required Playwright browser directory missing: {b_dir}")
        for root, dirs, files in os.walk(b_dir):
            for f in files:
                if f.endswith('.log'):
                    continue
                full_p = os.path.join(root, f)
                rel_p = os.path.relpath(root, b_dir)
                target_dir = os.path.normpath(os.path.join(f'playwright_browsers/{dir_name}', rel_p)).replace('\\', '/')
                chromium_datas.append((full_p, target_dir))

datas = [
    ('templates', 'templates'),
    ('static', 'static'),
    # Playwright's driver binaries
    (os.path.join(playwright_pkg_dir, 'driver'), 'playwright/driver'),
]
datas += chromium_datas

datas += collect_data_files('docx')
datas += collect_data_files('webview')
datas += collect_data_files('pythonnet')
datas += collect_data_files('clr_loader')

from PyInstaller.utils.hooks import collect_submodules

# ==========================================
# 3. PYINSTALLER CONFIGURATION
# ==========================================
block_cipher = None

hiddenimports = [
    'flask',
    'jinja2.ext',
    'playwright',
    'greenlet',
    'werkzeug',
    'webview',
    'docx',
    'lxml',
    'Pillow',
    'clr',
    'pythonnet',
    'clr_loader',
]
hiddenimports += collect_submodules('app')
hiddenimports += collect_submodules('graphql')
hiddenimports += collect_submodules('webview')
hiddenimports += collect_submodules('pythonnet')
hiddenimports += collect_submodules('clr_loader')
hiddenimports += collect_submodules('docx')

a = Analysis(
    ['desktop.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# NOTE: console=True is kept for the first debugging builds. 
# It should be set to False for the final production build.
_version_file = 'installer/version_info_app.txt' if os.path.isfile('installer/version_info_app.txt') else None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='QA-Tester',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=_version_file,
    uac_admin=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='QA-Tester',
)
