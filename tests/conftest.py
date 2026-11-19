import os
import tempfile
import shutil
import pytest

_global_temp = None
_old_cwd = None
_old_env = None


@pytest.fixture
def preflight_calls(monkeypatch):
    """Observe actual workflow dispatch calls without replacing the network barrier."""
    from playwright.async_api import Locator
    from app.qa.scoped_mutation import ScopedMutationNetworkBarrier
    calls = dict(file_selection=0, clicks=0, permits=0)
    select, click = Locator.set_input_files, Locator.click
    acquire = ScopedMutationNetworkBarrier.acquire_permit
    async def observed_select(self, *args, **kwargs):
        calls['file_selection'] += 1
        return await select(self, *args, **kwargs)
    async def observed_click(self, *args, **kwargs):
        calls['clicks'] += 1
        return await click(self, *args, **kwargs)
    def observed_acquire(self, *args, **kwargs):
        calls['permits'] += 1
        return acquire(self, *args, **kwargs)
    monkeypatch.setattr(Locator, 'set_input_files', observed_select)
    monkeypatch.setattr(Locator, 'click', observed_click)
    monkeypatch.setattr(ScopedMutationNetworkBarrier, 'acquire_permit', observed_acquire)
    return calls

def pytest_configure(config):
    global _global_temp, _old_cwd, _old_env
    _global_temp = tempfile.mkdtemp()
    _old_cwd = os.getcwd()
    _old_env = dict(os.environ)
    
    os.environ['QA_TESTER_DB_PATH'] = os.path.join(_global_temp, 'test_db.sqlite')
    os.environ['QA_TESTER_TEST_DIR'] = _global_temp
    os.environ['QA_TESTER_WRITABLE_DIR'] = _global_temp
    os.environ['FLASK_ENV'] = 'testing'
    
    os.chdir(_global_temp)

def pytest_unconfigure(config):
    global _global_temp, _old_cwd, _old_env
    if _old_cwd:
        os.chdir(_old_cwd)
    if _old_env is not None:
        os.environ.clear()
        os.environ.update(_old_env)
    if _global_temp:
        shutil.rmtree(_global_temp, ignore_errors=True)

@pytest.fixture(autouse=True)
def isolated_paths(monkeypatch):
    import app.paths
    orig_is_packaged = app.paths.is_packaged
    temp_writable = os.environ['QA_TESTER_TEST_DIR']
    
    def mock_get_writable_dir():
        if not orig_is_packaged():
            return temp_writable
        return os.path.join(temp_writable, 'QATester')

    monkeypatch.setattr(app.paths, 'get_writable_dir', mock_get_writable_dir)
    yield
