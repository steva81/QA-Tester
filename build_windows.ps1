<#
.SYNOPSIS
    Builds the QA Tester Windows executable using PyInstaller in --onedir mode.

.DESCRIPTION
    This script performs the following tasks:
    1. Validates the .venv and Python 3.12 environment.
    2. Installs PyInstaller if missing.
    3. Cleans previous build/dist folders.
    4. Executes PyInstaller against QA-Tester.spec.

    The QA-Tester.spec file dynamically discovers the currently installed
    Playwright Chromium browser and bundles it into the dist folder.
    It intentionally excludes all local development data (data, screenshots, etc.).
#>

$ErrorActionPreference = 'Stop'

Write-Host "Starting QA Tester Windows Build Preparation..." -ForegroundColor Cyan

# 1. Verify Virtual Environment
$PythonPath = ".\venv\Scripts\python.exe"
if (-not (Test-Path $PythonPath)) {
    Write-Error "Python virtual environment not found at $PythonPath. Please run inside the project .venv."
}

# 2. Verify Python Version
$PyVersion = & $PythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($PyVersion -ne "3.12") {
    Write-Error "Expected Python 3.12, but found $PyVersion. Please recreate the .venv with Python 3.12."
}
Write-Host "Found Python $PyVersion." -ForegroundColor Green

# 3. Check for PyInstaller
$PyInstallerPath = ".\venv\Scripts\pyinstaller.exe"
if (-not (Test-Path $PyInstallerPath)) {
    Write-Host "PyInstaller not found. Installing..." -ForegroundColor Yellow
    & $PythonPath -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install PyInstaller."
    }
}
Write-Host "PyInstaller is ready." -ForegroundColor Green

# 4. Clean previous builds
Write-Host "Cleaning previous build directories..." -ForegroundColor Yellow
if (Test-Path ".\build") { Remove-Item -Recurse -Force ".\build" }
if (Test-Path ".\dist") { Remove-Item -Recurse -Force ".\dist" }

# 5. Check Playwright Chromium discovery
Write-Host "Verifying Playwright browser discovery..." -ForegroundColor Yellow
$ChromiumPath = & $PythonPath -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $ChromiumPath)) {
    Write-Error "Failed to discover Playwright Chromium runtime! Please run 'playwright install chromium'."
}
Write-Host "Discovered Chromium at: $ChromiumPath" -ForegroundColor Green

# 6. Build Command
# NOTE: The build step was previously disabled for Step C.
# We are now proceeding with the actual PyInstaller execution.
Write-Host "Sanitizing PATH for deterministic DLL resolution..." -ForegroundColor Yellow
$BasePyDir = & $PythonPath -c "import sys; print(sys.base_prefix)"
$VenvScripts = (Resolve-Path ".\venv\Scripts").Path
$SysRoot = $env:SystemRoot
$env:PATH = "$VenvScripts;$BasePyDir;$BasePyDir\DLLs;$SysRoot\System32;$SysRoot;$SysRoot\System32\Wbem;$SysRoot\System32\WindowsPowerShell\v1.0"

Write-Host "Executing PyInstaller..." -ForegroundColor Yellow
& $PyInstallerPath --clean .\QA-Tester.spec
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller build failed."
}

Write-Host "Build complete! Output is in .\dist\QA-Tester" -ForegroundColor Green
