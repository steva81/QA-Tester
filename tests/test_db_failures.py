import pytest
from pathlib import Path
from app.models.database import init_db, get_db_connection
from app.routes import api
import sqlite3

@pytest.fixture
def client(tmp_path,monkeypatch):
    import app.paths
    monkeypatch.setattr(app.paths,'get_writable_dir',lambda:str(tmp_path/'managed'))
    monkeypatch.setenv('QA_TESTER_DB_PATH',str(tmp_path/'gate.sqlite'))
    app.paths.ensure_dirs();init_db()
    from server import app
    app.config['TESTING']=True
    with app.test_client() as c:yield c

def seed():
    conn=get_db_connection()
    pid=conn.execute("INSERT INTO projects(name,target_url,environment,tester_name) VALUES ('Gate','https://example.test/','Test','Gate')").lastrowid
    rid=conn.execute("INSERT INTO runs(project_id,status) VALUES (?, 'RUNNING')",(pid,)).lastrowid
    conn.commit();conn.close();return pid,rid

def test_reset_db_commit_failure_restores_staged_files(client, monkeypatch):
    from app.paths import get_screenshots_dir
    pid, rid = seed()
    shot = Path(get_screenshots_dir()) / 'first.png'
    shot.write_bytes(b'fixture')
    
    conn = get_db_connection()
    conn.execute("INSERT INTO tests(run_id,url,status,screenshot_path) VALUES (?, 'url', 'FAIL', ?)", (rid, str(shot)))
    conn.commit()
    conn.close()
    
    original_get = api.get_db_connection
    class FailingConn:
        def __init__(self):
            self.conn = original_get()
        def cursor(self): return self.conn.cursor()
        def execute(self, *args, **kwargs): return self.conn.execute(*args, **kwargs)
        def commit(self): raise RuntimeError("simulated DB failure")
        def close(self): self.conn.close()
        def rollback(self): self.conn.rollback()
    
    with monkeypatch.context() as patch:
        patch.setattr(api, 'get_db_connection', lambda: FailingConn())
        resp = client.post('/api/reset')
        
    assert resp.status_code == 500
    assert shot.exists(), "File was not restored after DB commit failed"
    conn = get_db_connection()
    assert conn.execute("SELECT COUNT(*) FROM tests").fetchone()[0] == 1, "DB was not rolled back"
    conn.close()

def test_delete_project_db_commit_failure_restores_staged_files(client, monkeypatch):
    from app.paths import get_screenshots_dir
    pid_a, rid_a = seed()
    pid_b, rid_b = seed()
    shot_a = Path(get_screenshots_dir()) / f'proj{pid_a}_first.png'
    shot_b = Path(get_screenshots_dir()) / f'proj{pid_b}_second.png'
    shot_a.write_bytes(b'fixture_a')
    shot_b.write_bytes(b'fixture_b')
    
    original_get = api.get_db_connection
    class FailingConn:
        def __init__(self):
            self.conn = original_get()
        def cursor(self): return self.conn.cursor()
        def execute(self, *args, **kwargs): return self.conn.execute(*args, **kwargs)
        def commit(self): raise RuntimeError("simulated DB failure")
        def close(self): self.conn.close()
        def rollback(self): self.conn.rollback()
        
    with monkeypatch.context() as patch:
        patch.setattr(api, 'get_db_connection', lambda: FailingConn())
        resp = client.post(f'/api/projects/{pid_a}/delete')
        
    assert resp.status_code == 500
    assert shot_a.exists(), "File A was not restored"
    assert shot_b.exists(), "File B was touched"
