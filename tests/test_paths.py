import os
import sys
import pytest

def test_development_mode_paths():
    import app.paths
    import importlib
    importlib.reload(app.paths)
    
    assert app.paths.is_packaged() is False
    
    base_dir = app.paths.get_base_dir()
    writable_dir = app.paths.get_writable_dir()
    
    assert base_dir == writable_dir
    assert os.path.basename(app.paths.get_data_dir()) == 'data'
    assert os.path.basename(app.paths.get_screenshots_dir()) == 'screenshots'

def test_packaged_mode_paths(monkeypatch):
    import app.paths
    import importlib
    importlib.reload(app.paths)
    
    # Simulate packaged mode
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', 'C:\\tmp\\meipass', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', 'C:\\tmp\\localappdata')
    
    assert app.paths.is_packaged() is True
    assert app.paths.get_base_dir() == 'C:\\tmp\\meipass'
    
    writable_dir = app.paths.get_writable_dir()
    assert writable_dir == os.path.join('C:\\tmp\\localappdata', 'QATester')
    
    assert app.paths.get_data_dir() == os.path.join(writable_dir, 'data')
    assert app.paths.get_screenshots_dir() == os.path.join(writable_dir, 'screenshots')
