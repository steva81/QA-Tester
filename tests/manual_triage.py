"""Disposable Phase 3A manual acceptance with local fixtures only."""
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

with tempfile.TemporaryDirectory(prefix='qa-phase3a-manual-') as isolated:
    os.environ['QA_TESTER_DB_PATH'] = str(Path(isolated) / 'qa.sqlite')
    import app.paths
    app.paths.get_writable_dir = lambda: isolated
    app.paths.ensure_dirs()
    from test_full_site import LocalSite
    site = LocalSite()
    site.links = {'/': [('/one', 'Error one'), ('/same', 'Matching error'),
                         ('/different', 'Different error'), ('/safety', 'Safety limited')],
                  '/one': [], '/same': [], '/different': [], '/safety': []}
    matching = '<script>throw new TypeError("Cannot read customerProfile.name");</script>'
    site.scripts['/one'] = site.scripts['/same'] = matching
    site.scripts['/different'] = '<script>throw new ReferenceError("accountPreferences is not defined");</script>'
    site.scripts['/safety'] = '<script>fetch("/write",{method:"POST"}).catch(error=>console.error({error:{error:String(error)}}));</script>'
    from server import app
    from app.routes.api import shutdown_qa_workers
    from werkzeug.serving import make_server
    server = make_server('127.0.0.1', 0, app, threaded=True)
    print(f'Disposable QA app: http://127.0.0.1:{server.server_port}', flush=True)
    print('Local project URL:', site.base + '/', flush=True)
    print('Open the QA Browser, visit /login, then return to the root and run Full Site QA.', flush=True)
    print('Only temporary QA data is used. Ctrl+C stops workers/servers and removes it.', flush=True)
    try:
        server.serve_forever()
    finally:
        shutdown_qa_workers()
        server.server_close()
        site.close()
