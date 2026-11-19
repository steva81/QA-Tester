"""Disposable manual Phase 2 QA environment; never opens the user's QA DB."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

with tempfile.TemporaryDirectory(prefix='qa-phase2-manual-') as isolated:
    os.environ['QA_TESTER_DB_PATH'] = str(Path(isolated) / 'qa.sqlite')
    import app.paths
    app.paths.get_writable_dir = lambda: isolated
    app.paths.ensure_dirs()
    from test_full_site import LocalSite
    site = LocalSite()
    site.links['/'] += [('/b#duplicate','Duplicate'),('/alias','Redirect'),('/logout','Logout'),('/delete','Delete')]
    site.delays['/b'] = 2
    site.script = '''<script>
      if ([1366,390].includes(innerWidth)) {
        fetch('/write',{method:'POST'}).catch(error =>
          console.error({error:{error:String(error)}}));
        window.open('/popup');
      }
    </script>'''
    site.scripts['/c'] = '<script>console.error(new Error("GENUINE_MANUAL_FIXTURE_ERROR"));</script>'
    from server import app
    from app.routes.api import shutdown_qa_workers
    print('Disposable QA app: http://127.0.0.1:5052', flush=True)
    print('Local project URL:', site.base + '/', flush=True)
    print('Local login: open /login, then navigate back to the root.', flush=True)
    print('QA data exists only in:', isolated, flush=True)
    print('Ctrl+C stops the app, workers, browsers and fixture; temporary data is removed.', flush=True)
    try:
        app.run(host='127.0.0.1', port=5052, debug=False, use_reloader=False, threaded=True)
    finally:
        shutdown_qa_workers()
        site.close()
