import threading
from flask import g, request

_mutation_lock = threading.RLock()


def get_mutation_lock():
    return _mutation_lock


def init_data_lifecycle(app):
    @app.before_request
    def acquire_mutation_lock():
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            _mutation_lock.acquire()
            g.qa_mutation_lock_acquired = True

    @app.teardown_request
    def release_mutation_lock(error=None):
        if g.pop("qa_mutation_lock_acquired", False):
            _mutation_lock.release()

