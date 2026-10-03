import pytest
import sqlite3
import os
from app.models.database import init_db, get_db_connection
from app.qa.assessment import assess_finding

def test_assess_finding_no_status():
    res = assess_finding("Page failed to return a status", "Failed to get response status", "Page Load", "Desktop 1366x768")
    assert res['severity'] == 'High'
    assert res['risk'] == 'Medium'
    assert res['confidence'] == 'Medium'

def test_assess_finding_404():
    res = assess_finding("Page returns HTTP 404", "Server returned HTTP 404", "Page Load", "Desktop 1366x768")
    assert res['severity'] == 'Medium'
    assert res['risk'] == 'Medium'
    assert res['confidence'] == 'High'
    assert "returned HTTP 404" in res['impact']

def test_assess_finding_fallback():
    res = assess_finding("Unknown Issue", "Something weird happened", "Unknown", "Desktop 1366x768")
    assert res['severity'] == 'Unreviewed'
    assert res['risk'] == 'Unreviewed'
    assert res['confidence'] == 'Unreviewed'
    assert res['impact'] == '[Requires Human Review]'

def test_safe_idempotent_migration(tmp_path):
    # Set up a fake database to test the migration
    db_path = tmp_path / "test_db.sqlite"
    os.environ['QA_TESTER_DB_PATH'] = str(db_path)
    
    # 1. Create a DB WITHOUT confidence column
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT, target_url TEXT, environment TEXT, tester_name TEXT)
    ''')
    c.execute('''
        CREATE TABLE runs (id INTEGER PRIMARY KEY, project_id INTEGER, status TEXT, start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP, authorized_at TIMESTAMP)
    ''')
    c.execute('''
        CREATE TABLE tests (id INTEGER PRIMARY KEY, run_id INTEGER, url TEXT, expected_result TEXT, actual_result TEXT, status TEXT, viewport TEXT, screenshot_path TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, stable_id TEXT)
    ''')
    c.execute('''
        CREATE TABLE findings (
            id INTEGER PRIMARY KEY,
            stable_id TEXT NOT NULL,
            project_id INTEGER NOT NULL,
            run_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            platform TEXT,
            area TEXT,
            severity TEXT,
            risk TEXT,
            status TEXT DEFAULT 'OPEN',
            description TEXT,
            impact TEXT,
            recommendation TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            review_status TEXT DEFAULT 'PENDING',
            plain_language_explanation TEXT,
            learning_explanation_sv TEXT,
            how_to_explain_sv TEXT
        )
    ''')
    # Insert an existing row
    c.execute("INSERT INTO projects (id, name, target_url, environment, tester_name) VALUES (1, 'T', 'http', 'dev', 'me')")
    c.execute("INSERT INTO runs (id, project_id, status) VALUES (1, 1, 'COMPLETED')")
    c.execute("INSERT INTO findings (stable_id, project_id, run_id, title, severity, risk) VALUES ('QA-001', 1, 1, 'Old Finding', 'High', 'Medium')")
    conn.commit()
    conn.close()
    
    # 2. Run init_db which should add the column
    init_db()
    
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("PRAGMA table_info(findings)")
    cols = [r[1] for r in c.fetchall()]
    assert 'confidence' in cols
    
    # 3. Check existing row received 'Unreviewed' default
    c.execute("SELECT severity, risk, confidence FROM findings WHERE stable_id = 'QA-001'")
    row = c.fetchone()
    assert row[0] == 'High'
    assert row[1] == 'Medium'
    assert row[2] == 'Unreviewed'
    conn.close()
    
    # 4. Run init_db again to test idempotency (startup/init when confidence exists)
    init_db()
    
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("PRAGMA table_info(findings)")
    cols_after = [r[1] for r in c.fetchall()]
    assert 'confidence' in cols_after
    conn.close()

    if 'QA_TESTER_DB_PATH' in os.environ:
        del os.environ['QA_TESTER_DB_PATH']
