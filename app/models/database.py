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

    # Validation contracts table (project/target-scoped explicit contract bindings)
    c.execute('''
        CREATE TABLE IF NOT EXISTS validation_contracts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            contract_id TEXT NOT NULL,
            rule_name TEXT NOT NULL,
            target_url_pattern TEXT,
            target_control_id TEXT,
            trigger_selector TEXT,
            accessible_name TEXT,
            feedback_selector TEXT,
            config_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects (id)
        )
    ''')

    # Mutation ownership records table (run-scoped entity tracking for authorized state-changing workflows and interruption cleanup)
    c.execute('''
        CREATE TABLE IF NOT EXISTS mutation_ownership_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            target_url TEXT NOT NULL,
            account_context TEXT,
            entity_type TEXT NOT NULL,
            intent_id TEXT,
            run_prefix TEXT,
            entity_id TEXT NOT NULL,
            remote_entity_id TEXT,
            entity_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'CREATED',
            remote_status TEXT,
            local_cleaned INTEGER DEFAULT 0,
            remote_cleaned INTEGER DEFAULT 0,
            details_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES runs (id),
            FOREIGN KEY (project_id) REFERENCES projects (id)
        )
    ''')

    # Safe Idempotent Migration for mutation_ownership_records
    c.execute("PRAGMA table_info(mutation_ownership_records)")
    mut_columns = [col['name'] for col in c.fetchall()]
    for col_name, col_type in (
        ('account_context', 'TEXT'),
        ('intent_id', 'TEXT'),
        ('run_prefix', 'TEXT'),
        ('remote_entity_id', 'TEXT'),
        ('remote_status', 'TEXT'),
        ('local_cleaned', 'INTEGER DEFAULT 0'),
        ('remote_cleaned', 'INTEGER DEFAULT 0'),
    ):
        if col_name not in mut_columns:
            try:
                c.execute(f"ALTER TABLE mutation_ownership_records ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

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

    # Preserve historical values as authoritative; only newly created baseline
    # assessments are distinguished from later tester choices.
    for field in ('severity', 'risk', 'confidence'):
        if field + '_origin' not in columns:
            c.execute(f"ALTER TABLE findings ADD COLUMN {field}_origin TEXT NOT NULL DEFAULT 'legacy'")
        
    c.execute("PRAGMA table_info(tests)")
    test_columns = [col['name'] for col in c.fetchall()]
    if 'final_url' not in test_columns:
        c.execute("ALTER TABLE tests ADD COLUMN final_url TEXT")

        
    from app.models.crawl import create_crawl_tables
    create_crawl_tables(conn)
    conn.commit()
    conn.close()
