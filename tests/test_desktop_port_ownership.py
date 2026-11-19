"""Windows listener ownership and occupied-port startup regressions."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import urllib.request

import pytest
from werkzeug.serving import make_server
from app.local_server import make_desktop_server, LocalServerStartError

ROOT = Path(__file__).resolve().parents[1]


def simple_app(environ, start_response):
    start_response('200 OK', [('Content-Type', 'text/plain')])
    return [b'original-owner']


@pytest.mark.parametrize('existing_legacy', [False, True])
def test_occupied_port_refused_and_original_survives(existing_legacy):
    first = make_server('127.0.0.1', 0, simple_app) if existing_legacy else make_desktop_server(0, simple_app)
    thread = threading.Thread(target=first.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(LocalServerStartError, match='port may already be in use'):
            make_desktop_server(first.port, simple_app)
        with urllib.request.urlopen(f'http://127.0.0.1:{first.port}', timeout=3) as response:
            assert response.read() == b'original-owner'
    finally:
        first.shutdown()
        first.server_close()
        thread.join(3)


def test_legacy_reuse_cannot_join_new_listener():
    first = make_desktop_server(0, simple_app)
    try:
        with socket.socket() as other:
            other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                other.bind(('127.0.0.1', first.port))
    finally:
        first.server_close()


def test_distinct_ports_still_allowed():
    first = make_desktop_server(0, simple_app)
    second = make_desktop_server(0, simple_app)
    try:
        assert first.port != second.port
    finally:
        first.server_close()
        second.server_close()


def test_desktop_occupied_port_reports_startup_failure(tmp_path):
    first = make_desktop_server(0, simple_app)
    thread = threading.Thread(target=first.serve_forever, daemon=True)
    thread.start()
    env = os.environ.copy()
    env.pop('QA_TESTER_DB_PATH', None)
    env.update(QA_TESTER_WRITABLE_DIR=str(tmp_path), QA_TESTER_PORT=str(first.port), QA_TESTER_SUPPRESS_ERROR_DIALOG='1')
    try:
        proc = subprocess.run([sys.executable, str(ROOT / 'desktop.py'), '--server-only'],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
        assert proc.returncode == 1
        assert 'QA Tester failed to start.' in proc.stdout
        assert 'port may already be in use' in proc.stdout
        assert 'Starting QA Tester' not in proc.stderr
        with urllib.request.urlopen(f'http://127.0.0.1:{first.port}', timeout=3) as response:
            assert response.read() == b'original-owner'
    finally:
        first.shutdown()
        first.server_close()
        thread.join(3)
