# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
import playwright

# ==========================================
# 1. DYNAMIC PLAYWRIGHT DISCOVERY
# ==========================================
# We do not hardcode Playwright browser revisions.
# Instead, we query the currently installed version.
try:
    with sync_playwright() as p:
        chromium_exe = p.chromium.executable_path
except Exception as e:
    print(f"ERROR: Could not discover Playwright Chromium path. {e}")
    sys.exit(1)

# Get the chromium-XXXX directory containing chrome-win
chrome_win_dir = os.path.dirname(chromium_exe)
chromium_dir = os.path.dirname(chrome_win_dir)

# Get the python playwright package directory (to include driver/node)
playwright_pkg_dir = os.path.dirname(playwright.__file__)

from PyInstaller.utils.hooks import collect_data_files

# ==========================================
# 2. BUNDLED RESOURCES
# ==========================================
# We explicitly include read-only Flask resources.
# We explicitly EXCLUDE: data/, screenshots/, reports/, exports/, logs/, and settings/
datas = [
    ('templates', 'templates'),
    # Playwright's driver binaries
    (os.path.join(playwright_pkg_dir, 'driver'), 'playwright/driver'),
    # The dynamically discovered Chromium browser
    (chromium_dir, f'playwright_browsers/{os.path.basename(chromium_dir)}')
]

datas += collect_data_files('docx')

# ==========================================
# 3. PYINSTALLER CONFIGURATION
# ==========================================
block_cipher = None

a = Analysis(
    ['desktop.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    # Hidden imports to ensure critical modules aren't dropped
    hiddenimports=[
        'flask',
        'jinja2.ext',
        'playwright',
        'greenlet',
        'werkzeug',
        'webview',
        'docx',
        'lxml',
        'Pillow',
        # Explicit internal modules
        'app.paths',
        'app.models.database',
        'app.qa.engine',
        'app.qa.assessment',
        'app.routes.api',
        'app.routes.exports',
        'app.routes.views',
    ],
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
