"""Disposable Phase 3B acceptance app with deterministic local evidence only.

Run: venv/Scripts/python.exe tests/manual_reporting.py
No browser crawl or remote requests are needed. Ctrl+C removes isolated data.
"""
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def seed_reporting_fixture():
    from app.models.database import get_db_connection
    from app.routes.api import _save_results
    conn = get_db_connection()
    pid = conn.execute("INSERT INTO projects(name,target_url,environment,tester_name) VALUES ('Phase 3B local fixture','http://127.0.0.1/fixture','Disposable local fixture','Manual reviewer')").lastrowid
    rid = conn.execute("INSERT INTO runs(project_id,status) VALUES (?,'RUNNING')", (pid,)).lastrowid
    conn.commit(); conn.close()
    records = []
    def add(path, status, viewport, message, fid=None, title=None):
        records.append(dict(url='http://127.0.0.1/fixture'+path, status=status, viewport=viewport,
                            actual_result=message, expected_result='Page evaluates without customer errors',
                            finding_id=fid, title=title, area='JavaScript Execution', screenshot=None))
    for viewport in ('Desktop 1366x768', 'Mobile 390x844'):
        add('/', 'PASS', viewport, 'Root page evaluated without customer findings.')
        for path, fid in (('/one', 'QA-RELATED-1'), ('/two', 'QA-RELATED-2')):
            add(path, 'OBSERVATION', viewport, 'Console/JS errors detected: TypeError: Cannot read customerProfile.name', fid, 'JavaScript exception')
        add('/distinct', 'OBSERVATION', viewport, 'Console/JS errors detected: ReferenceError: accountSettings is not defined', 'QA-DISTINCT', 'JavaScript exception')
        add('/safety', 'SAFETY ACTION', viewport, 'Application POST was blocked by the QA safety policy.')
        add('/safety', 'LIMITED COVERAGE', viewport, 'Page execution was perturbed by blocked application traffic.')
        for index in range(51):
            add('/safety', 'DIAGNOSTIC', viewport, f'Unverified fixture diagnostic record {index+1}; token=FIXTURE-SECRET')
    _save_results(records, rid, pid)
    conn = get_db_connection()
    conn.execute("UPDATE findings SET learning_explanation_sv='PRIVATE-TEST-12345' WHERE run_id=?", (rid,))
    conn.execute("INSERT INTO crawl_runs(run_id,phase,page_limit,query_limit,depth_limit,url_limit,navigation_limit,pages_discovered,pages_eligible,pages_tested,pages_attempted,pages_fully_evaluated,desktop_pages_tested,mobile_pages_tested,desktop_limited,mobile_limited,safety_limited_pages,safety_limited_viewports,coverage_gaps,page_limit_reached,discovery_safety_limited,coverage_complete,termination_reason) VALUES (?,'COMPLETED',5,10,3,100,100,5,5,5,5,4,5,5,1,1,1,2,2,1,1,0,'Local fixture: safety-limited execution and constrained discovery.')", (rid,))
    for path in ('/', '/one', '/two', '/distinct', '/safety'):
        limited = int(path == '/safety')
        url = 'http://127.0.0.1/fixture'+path
        conn.execute('INSERT INTO crawl_pages(run_id,url_key,requested_url,final_url,state,eligible,desktop_tested,mobile_tested,depth,desktop_limited,mobile_limited,reason) VALUES (?,?,?,?,?,1,1,1,0,?,?,?)',
                     (rid, path, url, url, 'LIMITED COVERAGE' if limited else 'TESTED', limited, limited,
                      'Safety blocked application traffic.' if limited else ''))
    conn.commit(); conn.close()
    return rid


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='qa-phase3b-manual-') as isolated:
        os.environ['QA_TESTER_DB_PATH'] = str(Path(isolated) / 'qa.sqlite')
        import app.paths
        app.paths.get_writable_dir = lambda: isolated
        app.paths.ensure_dirs()
        from server import app
        from app.routes.api import shutdown_qa_workers
        from werkzeug.serving import make_server
        rid = seed_reporting_fixture()
        server = make_server('127.0.0.1', 0, app, threaded=True)
        print(f'Disposable Results: http://127.0.0.1:{server.server_port}/run/{rid}', flush=True)
        print(f'Reports: http://127.0.0.1:{server.server_port}/reports', flush=True)
        print('Expected: 114 Total Checks, 3 separate customer findings, 102 diagnostic records; LIMITED COVERAGE; 5 attempted / 4 fully evaluated.', flush=True)
        print('Private learning notes contain PRIVATE-TEST-12345. Standard copy is the default; exports default to Full Technical.', flush=True)
        print('Ctrl+C stops the local server/workers and removes all helper data.', flush=True)
        try:
            server.serve_forever()
        finally:
            shutdown_qa_workers()
            server.server_close()
