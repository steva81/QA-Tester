import os
from app.paths import get_writable_dir
from app.models.database import get_db_path

def test_isolation_preflight():
    writable = get_writable_dir()
    db_path = get_db_path()
    test_dir = os.environ.get('QA_TESTER_TEST_DIR')
    writable_env = os.environ.get('QA_TESTER_WRITABLE_DIR')
    
    assert test_dir is not None, "QA_TESTER_TEST_DIR must be set"
    assert writable_env is not None, "QA_TESTER_WRITABLE_DIR must be set"
    assert writable.startswith(test_dir), f"get_writable_dir ({writable}) must be within test_dir ({test_dir})"
    assert db_path.startswith(test_dir), f"get_db_path ({db_path}) must be within test_dir ({test_dir})"
    
    # Verify neither points to repository root or real AppData
    assert not db_path.endswith("qa-tester.db"), "Must not point to repository root database"
    assert "pytest" in test_dir.lower() or "tmp" in test_dir.lower() or "temp" in test_dir.lower(), "Must be temp directory"

