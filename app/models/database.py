import sqlite3
import os

def get_db_path():
    db_path = os.environ.get('QA_TESTER_DB_PATH')
    if db_path:
        return db_path
    from app.paths import get_data_dir
    return os.path.join(get_data_dir(), 'qa-tester.db')

def get_db_connection():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    
    # Projects table
    c.execute('''
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            target_url TEXT NOT NULL,
            environment TEXT NOT NULL,
            tester_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT
        )
    ''')
    
    # Runs table
    c.execute('''
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_time TIMESTAMP,
            authorized_at TIMESTAMP,
            total_pages INTEGER DEFAULT 0,
            total_tests INTEGER DEFAULT 0,
            FOREIGN KEY (project_id) REFERENCES projects (id)
        )
    ''')
    
    # Tests table (Evidence)
    c.execute('''
        CREATE TABLE IF NOT EXISTS tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            expected_result TEXT,
            actual_result TEXT,
            status TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            viewport TEXT,
            screenshot_path TEXT,
            stable_id TEXT,
            FOREIGN KEY (run_id) REFERENCES runs (id)
        )
    ''')
    
    # Findings table (Structured for reporting)
    c.execute('''
        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            FOREIGN KEY (project_id) REFERENCES projects (id),
            FOREIGN KEY (run_id) REFERENCES runs (id)
        )
    ''')
    
    
    # Safely migrate existing tables
    try:
        c.execute("ALTER TABLE findings ADD COLUMN review_status TEXT DEFAULT 'PENDING'")
    except sqlite3.OperationalError:
        pass
        
    try:
        c.execute("ALTER TABLE findings ADD COLUMN plain_language_explanation TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE findings ADD COLUMN learning_explanation_sv TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE findings ADD COLUMN how_to_explain_sv TEXT")
    except sqlite3.OperationalError:
        pass
        
    # Safe Idempotent Migration for Confidence
    c.execute("PRAGMA table_info(findings)")
    columns = [col['name'] for col in c.fetchall()]
    if 'confidence' not in columns:
        c.execute("ALTER TABLE findings ADD COLUMN confidence TEXT DEFAULT 'Unreviewed'")
        
    conn.commit()
    conn.close()
