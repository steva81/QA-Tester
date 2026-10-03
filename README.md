# QA Tester

QA Tester is a professional, automated functional QA testing platform designed for authorized website testing.

## What QA Tester Is
QA Tester is a tool designed to reduce repetitive manual functional QA work by behaving like a careful human QA tester using a normal browser. It relies on Playwright to navigate, interact, and report on the deterministic state of web applications.

## What it does NOT do
This is **NOT** a penetration-testing tool, vulnerability scanner, or security attack framework. It does not perform SQL injection testing, XSS testing, brute force attacks, or API fuzzing. 

## Functional QA vs Penetration Testing
Functional QA focuses on answering "Does the application behave as expected for a normal user?" 
Penetration testing focuses on "Can this application be exploited?" 
QA Tester only performs the former.

## Safety Rules
- Must be used only on authorized websites.
- Safe Mode is ON by default.
- Navigation is strictly limited to same-origin.
- No destructive actions (e.g., clicking delete, purchase, submit application).
- Strict scope limits (max 25 pages, depth 3).

## Architecture
- **Backend:** Python + Flask
- **Browser Automation:** Playwright (Chromium)
- **Database:** SQLite
- **Desktop UI:** PyWebView
- **Packaging:** PyInstaller

## Project Structure
```
qa-tester/
    app.py                 # Main Flask app initialization
    desktop.py             # PyWebView desktop entry point
    app/                   # Core application logic
    templates/             # HTML views
    static/                # CSS, JS assets
    data/                  # SQLite db
    screenshots/           # Evidence images
```

## Installation & Development Mode
1. Clone the repository.
2. Create a virtual environment: `python -m venv venv`
3. Activate the virtual environment: `venv\Scripts\activate`
4. Install dependencies: `pip install -r requirements.txt`
5. Install Playwright browsers: `playwright install chromium`
6. Run the application in development mode: `python app.py` (Flask server only) or `python desktop.py` (PyWebView desktop shell).

## How Playwright Works
QA Tester uses Playwright to launch a Chromium browser context. It navigates to the target URL, extracts page state, visible elements, console errors, and captures screenshots when expected behaviors fail.

## How to use QA Tester (Phase 1)
1. **Create a project:** Provide project name, target URL, environment, tester name.
2. **Confirm Authorization:** Explicitly confirm you have permission to test.
3. **Dry Run:** Preview the planned scope of the test.
4. **Run Smart Smoke QA:** The engine will systematically navigate same-origin links and verify page loads, broken links, console errors, and responsive layouts.
5. **Results:** View PASS, FAIL, OBSERVATION, MANUAL, and SKIPPED results.
6. **APL Report Mode:** Review structured findings and use "Copy for Google Docs" for easy reporting.

## Data Storage
- SQLite database stored in `data/qa-tester.db`.
- Screenshots stored in `screenshots/`.
- Reports stored in `reports/`.

## Testing
Run internal tests using pytest:
`pytest tests/`

## Future AI Support
Phase 2/3 will introduce optional AI support to help write prose (descriptions, impact, recommendations) based on the deterministic evidence collected by Playwright. The AI will not drive the browser or invent root causes.

## Future Windows Packaging
Phase 3 will finalize packaging the application into a portable `QA-Tester.exe` using PyInstaller, complete with a bundled Chromium runtime.

## Known Limitations
- Phase 1 does not support authenticated QA or CAPTCHA bypass.
- Phase 1 focuses on read-only navigation and smoke testing (no interactive form submissions).
