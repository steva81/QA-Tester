import os
import pytest
from app.models.database import get_db_connection, init_db

@pytest.fixture
def temp_db():
    # Use in-memory SQLite for testing
    import sqlite3
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    
    # Run the same init queries
    c = conn.cursor()
    c.execute('''CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT NOT NULL, target_url TEXT NOT NULL, environment TEXT NOT NULL, tester_name TEXT NOT NULL)''')
    conn.commit()
    yield conn
    conn.close()

def test_database_connection(temp_db):
    c = temp_db.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Test', 'https://example.com', 'dev', 'Tester')")
    temp_db.commit()
    
    c.execute("SELECT * FROM projects")
    rows = c.fetchall()
    assert len(rows) == 1
    assert rows[0]['name'] == 'Test'
    assert rows[0]['target_url'] == 'https://example.com'
