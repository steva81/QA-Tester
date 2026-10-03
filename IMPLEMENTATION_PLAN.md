# QA Tester - Implementation Plan

## Architecture
- **Backend:** Python + Flask
- **Browser Automation:** Playwright (Chromium)
- **Frontend:** HTML, CSS, JavaScript (Vanilla, no complex frameworks)
- **Desktop Application Shell:** PyWebView
- **Database:** SQLite
- **Packaging:** PyInstaller

## Current Phase: Phase 1

## Exact Phase 1 Features
- Flask application backend
- PyWebView desktop wrapper (basic)
- Project management (Create Project with Target URL, Environment, Tester Name, Authorization Confirmation)
- Safe Mode (on by default)
- Dry Run preview
- SMART SMOKE QA execution engine
- Page-load testing, internal navigation testing, broken link testing, console-error collection
- Viewport testing (Desktop 1366x768, Mobile 390x844)
- Deterministic result classification (PASS, FAIL, OBSERVATION, MANUAL, SKIPPED)
- Screenshot capture on confirmed FAIL
- Stable QA finding IDs (QA-001, etc.)
- Results page (summary dashboard)
- Finding Details view with APL Report Mode structure
- Report exports: Copy for Google Docs, HTML, Markdown, Plain-text
- SQLite persistence (Projects, Runs, Findings, Screenshots)
- STOP QA RUN functionality
- Safe controlled testing against a local demo site for self-verification

## Features Explicitly Deferred (Phase 2 & 3)
- Authenticated QA & CAPTCHA/2FA handoff
- Form validation & Test Case Recorder
- Screenshot redaction (blur/mask)
- Custom viewport presets & Reusable test cases
- Duplicate detection & Flaky test auto-retry improvements
- Crash recovery & Evidence export package (.zip)
- AI report assistant, severity/risk suggestions, log explanation
- PDF/DOCX export & Direct Google Docs API integration
- Secure Windows AI key configuration (Credential Manager)
- Windows Installer (Inno Setup)

## File Structure
```
qa-tester/
    app.py                 # Main Flask app initialization
    desktop.py             # PyWebView desktop entry point
    requirements.txt       # Dependencies
    README.md              # Documentation
    IMPLEMENTATION_PLAN.md # This file
    .gitignore
    .env.example

    app/
        __init__.py
        routes/            # Flask route handlers
        qa/                # Playwright QA engine & Smart Smoke QA
        reports/           # Report generation & formatting
        models/            # SQLite database models
        services/          # Database interaction services
        safety/            # URL normalization, safety checks, scope limits
        utils/             # Helpers
        ai/                # AI stubs (unused in Phase 1)

    templates/             # HTML Jinja templates
    static/                # CSS, JS, images

    data/                  # SQLite db file
    screenshots/           # Evidence images
    reports/               # Generated report exports
    exports/               # Packaged evidence
    logs/                  # Application logs

    tests/                 # Pytest cases
```

## SQLite Model
- **Projects:** id, name, target_url, environment, tester_name, created_at, default_viewports, notes
- **Runs:** id, project_id, status, start_time, end_time, authorized_at, total_pages, total_tests
- **Tests:** id, run_id, url, expected_result, actual_result, status (PASS, FAIL, OBSERVATION, MANUAL, SKIPPED), timestamp, viewport, screenshot_path
- **Findings:** id, stable_id (QA-XXX), project_id, run_id, title, platform, area, severity, risk, status (OPEN, CLOSED, etc.), description, impact, recommendation, timestamp

## Smart Smoke QA Execution Flow
1. User configures run and confirms authorization.
2. Dry Run previews scope.
3. Launch Playwright (Chromium) in an isolated context.
4. Open target URL. Record URL, title, viewport, console errors.
5. Extract same-origin links safely.
6. Deduplicate and normalize URLs (ignore fragments/tracking where safe).
7. Visit pages up to limit (max 25 pages, depth 3).
8. Perform non-destructive checks: Page load, HTTP status (no 404/500), JS uncaught errors, responsive overflow.
9. Record evidence and screenshots for FAIL classifications.
10. Persist results to SQLite.
11. Generate report formats.

## Safety Boundaries
- **Strict Same-Origin:** Navigation is restricted to the starting hostname/origin.
- **Read-Only Actions:** Phase 1 does not click buttons, submit forms, or alter state.
- **Scope Limits:** Hardcoded to max 25 unique pages and depth 3.
- **Concurrency:** Maximum 1 active browser context.
- **No Destructive Actions:** No forms are filled. Explicit blacklist for terms like "delete", "purchase" from automatic crawling (even though Phase 1 just navigates safe links).

## Technical Risks
- **Playwright Installation on Windows:** Ensuring the Chromium binary is available when packaged via PyInstaller.
- **Flakiness:** Modern JS apps might have slow-loading elements or transitions that trigger false FAILs. (Mitigated by focusing on hard errors like 404, 500, unhandled JS exceptions).
- **Concurrency in Flask+SQLite:** SQLite needs to be carefully handled to avoid locking issues if Playwright and Flask interact simultaneously (though in Phase 1, it's mostly sequential).

## Windows Packaging Strategy
- Develop first using standard Python/Flask server.
- Introduce PyWebView to wrap the local Flask server.
- **[CURRENT]** Prepare PyInstaller packaging with `build_windows.ps1` and `QA-Tester.spec` without actually running the build yet.
    - `build_windows.ps1`: Validates the `.venv` for Python 3.12, installs PyInstaller, cleans previous outputs (`dist`/`build`), and executes the spec. It intentionally **does not** migrate or modify QA data.
    - `QA-Tester.spec`: Dynamically resolves the exact Playwright Chromium location/version from the active virtual environment and explicitly copies it to `dist\QA-Tester\playwright_browsers`. Explicitly includes `templates`/`static` and explicitly ignores private data directories.
- Package using PyInstaller `--onedir`, ensuring PyWebView and Playwright (with its browser binaries) are correctly bundled, using `%LOCALAPPDATA%` for persistent data.
- Provide a `build_windows.ps1` script to automate binary creation, downloading Playwright browsers explicitly to a known bundled location.

## Current Verified Status
**PHASE 1: COMPLETED AND MANUALLY VERIFIED**

End-to-end functionality manually verified, including:
- Smart Smoke QA
- Safe same-origin crawling
- Desktop 1366x768 testing
- Mobile 390x844 testing
- PASS / FAIL / OBSERVATION
- viewport deduplication
- one finding with multiple viewport evidence records
- separate Desktop and Mobile screenshots
- Finding Details
- English Plain-Language Explanation
- private Swedish learning explanation
- private Swedish verbal explanation
- APL Report Mode
- Google Docs copy
- HTML / Markdown / Plain Text exports
- Findings History grouped by date
- SQLite persistence
- screenshot evidence rendering

## Final Phase 1 UX and Reporting Polish
- **Application Shell:** Created \ase.html\ for a persistent sidebar and topbar structure.
- **Home Dashboard:** Redesigned into a professional dashboard displaying counts, quick actions, and recent activity.
- **Workflow:** Implemented a step-by-step indicator (Select Project -> Review Plan -> Run -> Results).
- **History and Findings:** Differentiated History (Runs) from Findings (Global review status filters).
- **Time Formatting:** Local time consistently applied across the UI layer in \iews.py\ and \exports.py\.
- **Export Formats:** Rewrote export logic to generate \DOCX\ (via \python-docx\) alongside \HTML\, \Markdown\, and \TXT\. Excluded private notes from exports.
- **Safe Drafts:** Engine now maps HTTP 404, 500, and connection errors to safe, conservative impact/recommendation templates.
- **Packaging:** Updated \QA-Tester.spec\ to use \console=False\ for the upcoming production build.
