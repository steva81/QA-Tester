"""Comprehensive verification for QA-Tester's Home Launch Page Redesign."""
import os
import sqlite3
import pytest
from app.models.database import get_db_connection, init_db
from app.utils import format_local_time


@pytest.fixture
def clean_db():
    test_db = "test_home_launchpad.sqlite"
    if os.path.exists(test_db):
        os.remove(test_db)
    os.environ['QA_TESTER_DB_PATH'] = test_db
    init_db()
    yield test_db
    if os.path.exists(test_db):
        os.remove(test_db)


@pytest.fixture
def test_client(clean_db):
    from server import app
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


def test_home_heading_and_three_action_cards(test_client):
    """Verify welcoming heading and 3 prominent action cards."""
    res = test_client.get('/')
    assert res.status_code == 200
    html = res.data.decode('utf-8')

    # Welcoming heading & subtitle
    assert '<h1>Welcome to QA Tester</h1>' in html
    assert 'What would you like to do?' in html
    # Redundant eyebrow should not appear on Home
    assert '<div class="eyebrow">Your QA workspace</div>' not in html

    # Three prominent action cards
    assert 'Start a new test' in html
    assert 'Choose an existing project or create a new one, then continue into test setup.' in html
    assert 'href="/run-test"' in html

    assert 'My projects' in html
    assert 'Open and manage saved projects.' in html
    assert 'href="/projects"' in html

    assert 'Results &amp; reports' in html or 'Results & reports' in html
    assert 'Find previous test runs and their reports.' in html
    assert 'href="/reports"' in html

    # Primary card styling & distinction
    assert 'action-card-primary' in html
    assert 'action-card-badge' in html
    assert 'Primary action' in html

    # Duplicate header CTA must NOT be present on Home
    assert 'header-cta' not in html
    # But Home nav link is marked aria-current="page"
    assert '<a href="/" aria-current="page">Home</a>' in html


def test_home_removes_old_dashboard_clutter(test_client):
    """Verify raw target URLs, project grids, run history and finding lists are removed from Home."""
    # Seed a project and run with raw URL and findings
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Secret Site', 'https://raw-internal-site.local:8080/app', 'Staging', 'QA Lead')")
    pid = c.lastrowid
    c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (pid,))
    rid = c.lastrowid
    c.execute("INSERT INTO findings (project_id, run_id, stable_id, title, status) VALUES (?, ?, 'F-01', 'Critical bug finding', 'OPEN')", (pid, rid))
    conn.commit()
    conn.close()

    res = test_client.get('/')
    html = res.data.decode('utf-8')

    # No old dashboard panels
    assert 'home-projects' not in html
    assert 'home-runs' not in html
    assert 'home-review' not in html
    assert 'project-grid' not in html

    # Raw internal target URL and detailed finding title should not clutter Home
    assert 'https://raw-internal-site.local:8080/app' not in html
    assert 'Critical bug finding' not in html


def test_continue_where_you_left_off_with_run(test_client):
    """When a run exists, continue where you left off accurately identifies it."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('E-Commerce', 'https://store.local', 'Production', 'Alice')")
    pid = c.lastrowid
    c.execute("INSERT INTO runs (project_id, status, start_time) VALUES (?, 'COMPLETED', '2026-10-18 14:00:00')", (pid,))
    rid = c.lastrowid
    c.execute("INSERT INTO findings (project_id, run_id, stable_id, title, status) VALUES (?, ?, 'F-01', 'Broken cart', 'OPEN')", (pid, rid))
    c.execute("INSERT INTO findings (project_id, run_id, stable_id, title, status) VALUES (?, ?, 'F-02', 'Slow checkout', 'OPEN')", (pid, rid))
    conn.commit()
    conn.close()

    res = test_client.get('/')
    html = res.data.decode('utf-8')

    assert 'Continue where you left off' in html
    assert 'E-Commerce' in html
    assert f'Run #{rid}' in html
    assert 'COMPLETED' in html
    assert '2 findings' in html
    assert f'href="/run/{rid}"' in html
    assert 'View results →' in html


def test_continue_where_you_left_off_with_running_run(test_client):
    """When an active run is in progress, continue section truthfully indicates it."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Active App', 'https://app.local', 'Test', 'Bob')")
    pid = c.lastrowid
    c.execute("INSERT INTO runs (project_id, status, start_time) VALUES (?, 'RUNNING', '2026-10-18 15:00:00')", (pid,))
    rid = c.lastrowid
    conn.commit()
    conn.close()

    res = test_client.get('/')
    html = res.data.decode('utf-8')

    assert 'Continue where you left off' in html
    assert 'Active App' in html
    assert 'In progress' in html
    assert f'Run #{rid} is currently running' in html
    assert f'href="/run/{rid}"' in html
    assert 'Resume run →' in html


def test_continue_where_you_left_off_with_project_only(test_client):
    """When a project exists but no runs exist yet, continue section points to project setup."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name, created_at) VALUES ('Brand New Project', 'https://new.local', 'Staging', 'Eve', '2026-10-18 10:00:00')")
    pid = c.lastrowid
    conn.commit()
    conn.close()

    res = test_client.get('/')
    html = res.data.decode('utf-8')

    assert 'Continue where you left off' in html
    assert 'Brand New Project' in html
    assert 'Environment: Staging' in html
    assert f'href="/project/{pid}"' in html
    assert 'Open project →' in html


def test_continue_omitted_when_database_is_empty(test_client):
    """When database is completely empty, 'Continue where you left off' is omitted."""
    res = test_client.get('/')
    html = res.data.decode('utf-8')

    assert 'Continue where you left off' not in html
    # But 3 cards are present
    assert 'Start a new test' in html
    assert 'My projects' in html
    assert 'Results &amp; reports' in html or 'Results & reports' in html


def test_card_1_destination_flow(test_client):
    """Card 1 (/run-test) allows selecting existing project or creating new project."""
    # 1. When empty
    res = test_client.get('/run-test')
    html = res.data.decode('utf-8')
    assert 'No projects created yet' in html
    assert 'href="/projects"' in html

    # 2. When projects exist
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Proj Alpha', 'https://alpha.local', 'Dev', 'Tester')")
    p1 = c.lastrowid
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Proj Beta', 'https://beta.local', 'Prod', 'Tester')")
    p2 = c.lastrowid
    conn.commit()
    conn.close()

    res = test_client.get('/run-test')
    html = res.data.decode('utf-8')
    assert 'Select a project' in html
    assert 'Proj Alpha' in html
    assert f'href="/project/{p1}/dry-run"' in html
    assert 'Proj Beta' in html
    assert f'href="/project/{p2}/dry-run"' in html
    # Create new project path is readily accessible
    assert 'Create new project' in html
    assert 'href="/projects"' in html


def test_card_2_destination_flow(test_client):
    """Card 2 (/projects) displays projects and project management."""
    # 1. Empty state
    res = test_client.get('/projects')
    html = res.data.decode('utf-8')
    assert 'Create your first project' in html

    # 2. With project
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Managed Site', 'https://site.local', 'Prod', 'QA')")
    pid = c.lastrowid
    conn.commit()
    conn.close()

    res = test_client.get('/projects')
    html = res.data.decode('utf-8')
    assert 'Managed Site' in html
    assert f'href="/project/{pid}"' in html
    assert 'Open workspace' in html


def test_card_3_destination_flow(test_client):
    """Card 3 (/reports) lists runs with results and reports buttons, and has graceful empty state."""
    # 1. Empty state
    res = test_client.get('/reports')
    html = res.data.decode('utf-8')
    assert 'No test runs or reports recorded yet' in html
    assert 'href="/run-test"' in html

    # 2. With multiple runs across multiple projects
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Site One', 'https://one.local', 'Dev', 'QA')")
    p1 = c.lastrowid
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Site Two', 'https://two.local', 'Prod', 'QA')")
    p2 = c.lastrowid

    c.execute("INSERT INTO runs (project_id, status, start_time) VALUES (?, 'COMPLETED', '2026-10-18 10:00:00')", (p1,))
    r1 = c.lastrowid
    c.execute("INSERT INTO runs (project_id, status, start_time) VALUES (?, 'FAILED', '2026-10-18 11:00:00')", (p2,))
    r2 = c.lastrowid
    conn.commit()
    conn.close()

    res = test_client.get('/reports')
    html = res.data.decode('utf-8')
    assert 'Site One' in html
    assert f'Run #{r1}' in html
    assert f'href="/run/{r1}"' in html
    assert f'href="/run/{r1}?tab=reports"' in html

    assert 'Site Two' in html
    assert f'Run #{r2}' in html
    assert f'href="/run/{r2}"' in html
    assert f'href="/run/{r2}?tab=reports"' in html


def test_header_shows_start_test_button_only_on_non_home_pages(test_client):
    """Header CTA button should be omitted on Home, but available on sub-pages."""
    # On Home: no header-cta
    res_home = test_client.get('/')
    assert 'header-cta' not in res_home.data.decode('utf-8')
    assert '＋ Start New QA Test</a>' not in res_home.data.decode('utf-8')

    # On Projects: header CTA is present
    res_proj = test_client.get('/projects')
    assert '＋ Start New QA Test</a>' in res_proj.data.decode('utf-8')

    # On Reports: header CTA is present
    res_reports = test_client.get('/reports')
    assert '＋ Start New QA Test</a>' in res_reports.data.decode('utf-8')
