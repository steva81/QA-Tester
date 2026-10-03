import os
import pytest
import tempfile
import shutil

@pytest.fixture(autouse=True)
def isolated_paths(monkeypatch):
    temp_writable = tempfile.mkdtemp()
    
    import app.paths
    orig_is_packaged = app.paths.is_packaged
    orig_get_base_dir = app.paths.get_base_dir
    
    def mock_get_writable_dir():
        if not orig_is_packaged():
            return temp_writable
        return os.path.join(temp_writable, 'QATester')

    monkeypatch.setattr(app.paths, 'get_writable_dir', mock_get_writable_dir)
    
    yield
    
    # Clean up
    shutil.rmtree(temp_writable, ignore_errors=True)
