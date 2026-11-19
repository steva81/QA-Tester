"""One disposable entry point for final UX acceptance; no remote QA is run."""
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from manual_reporting import seed_reporting_fixture


def seed_ux_fixture():
    from app.models.database import get_db_connection
    from app.routes.api import _save_results
    limited_id = seed_reporting_fixture()
    conn = get_db_connection()
    pid = conn.execute('SELECT project_id FROM runs WHERE id=?', (limited_id,)).fetchone()[0]
    conn.execute("UPDATE projects SET name='Customer portal', environment='Disposable demo' WHERE id=?", (pid,))
    conn.execute("UPDATE runs SET start_time='2026-10-05 12:00:00' WHERE id=?", (limited_id,))
    completed_id = conn.execute("INSERT INTO runs(project_id,status,start_time) VALUES (?,'RUNNING','2026-10-04 12:00:00')", (pid,)).lastrowid
    conn.commit(); conn.close()
    _save_results([dict(url='http://127.0.0.1/fixture', status='PASS', viewport=viewport,
                        actual_result='Local fixture evaluated without findings.', expected_result='No customer errors',
                        finding_id=None, screenshot=None) for viewport in ('Desktop 1366x768', 'Mobile 390x844')], completed_id, pid)
    return dict(project_id=pid, limited_id=limited_id, completed_id=completed_id)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='qa-final-ux-') as isolated:
        os.environ['QA_TESTER_DB_PATH'] = str(Path(isolated) / 'qa.sqlite')
        import app.paths
        app.paths.get_writable_dir = lambda: isolated
        app.paths.ensure_dirs()
        from server import app
        from app.routes.api import shutdown_qa_workers
        from werkzeug.serving import make_server
        seed_ux_fixture()
        server = make_server('127.0.0.1', 5053, app, threaded=True)
        print('Open: http://127.0.0.1:5053/', flush=True)
        print('Open Customer portal, then View latest results: LIMITED COVERAGE, 114 checks, 3 separate findings, a related theme and a review queue.', flush=True)
        print('Use the run tabs to review Findings, Reports and Technical. Preview/copy/export both modes in Reports. Older completed run: 2 PASS checks.', flush=True)
        print('Open a finding to inspect its evidence and advisory suggestions, edit manual values, and review private notes. No remote site is tested by this fixture.', flush=True)
        print('Exports are disposable. Ctrl+C stops the helper and removes all helper data.', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            shutdown_qa_workers()
            server.server_close()
