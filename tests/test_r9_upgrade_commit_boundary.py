"""Concrete missing failure boundary: deleting the old backup after activation."""
from pathlib import Path
import json
import os
import zipfile
import pytest
from installer import setup_installer as setup
from tests.test_v1_0_release_safety import _seed_trusted_v2_layout


def test_failure_halfway_through_backup_cleanup_keeps_complete_new_release(tmp_path, monkeypatch):
    app, data, menu, archive, args, old, new = _seed_trusted_v2_layout(tmp_path, monkeypatch)
    remove = setup._remove_verified_release_dir
    def fail_cleanup(target, files):
        if str(target) == setup._backup_dir_for(str(app)):
            (Path(target) / 'QA-Tester.exe').unlink()
            raise OSError('Injected failure after the first old backup file was deleted')
        return remove(target, files)
    monkeypatch.setattr(setup, '_remove_verified_release_dir', fail_cleanup)
    try:
        setup.run_install(**args, payload_zip=str(archive), create_shortcuts=False)
    except OSError:
        pass
    assert (app / 'QA-Tester.exe').is_file(), 'Both complete versions were lost during cleanup rollback'
    assert (app / 'QA-Tester.exe').read_bytes() == b'MZ-NEW-V1-0-BINARY-DIFFERENT-BYTES'
    assert (data / 'data/qa-tester.db').read_bytes() == b'SQLITE-USER-PROJECTS-AND-RESULTS'
    monkeypatch.setattr(setup, '_remove_verified_release_dir', remove)
    # Missing files are allowed only for resumed cleanup, not foreign files.
    foreign = Path(setup._backup_dir_for(str(app))) / 'foreign.txt'
    foreign.write_text('must not delete')
    with pytest.raises(ValueError, match='unknown or foreign'):
        setup.recover_interrupted_upgrade(**args, payload_zip=str(archive))
    assert foreign.read_text() == 'must not delete'
    assert (app / 'QA-Tester.exe').read_bytes() == b'MZ-NEW-V1-0-BINARY-DIFFERENT-BYTES'
    foreign.unlink()
    recovered = setup.recover_interrupted_upgrade(**args, payload_zip=str(archive))
    assert recovered['status'] in ('recovered', 'noop')
    assert not Path(setup._backup_dir_for(str(app))).exists()
    assert (app / 'QA-Tester.exe').read_bytes() == b'MZ-NEW-V1-0-BINARY-DIFFERENT-BYTES'


def test_partial_receipt_write_restores_old_release(tmp_path, monkeypatch):
    app, data, menu, archive, args, old, new = _seed_trusted_v2_layout(tmp_path, monkeypatch)
    dump = json.dump
    def broken_dump(obj, stream, **kwargs):
        if 'installed_at_utc' in obj:
            stream.write('{')
            raise OSError('Injected disk write failure in receipt')
        return dump(obj, stream, **kwargs)
    monkeypatch.setattr(setup.json, 'dump', broken_dump)
    with pytest.raises(OSError):
        setup.run_install(**args, payload_zip=str(archive), create_shortcuts=False)
    assert (app / 'QA-Tester.exe').read_bytes() == b'MZ-OLD-V2-BINARY'
    assert (app / '_internal/old_only.dll').read_bytes() == b'DLL-OLD-V2-ONLY'


def test_recovery_can_resume_interrupted_deletion_of_new_files(tmp_path, monkeypatch):
    app, data, menu, archive, args, old, new = _seed_trusted_v2_layout(tmp_path, monkeypatch)
    backup=Path(setup._backup_dir_for(str(app)))
    os.rename(app, backup)
    with zipfile.ZipFile(archive) as z:
        z.extractall(app)
    (app/'QA-Tester.exe').unlink()  # termination during rollback deletion
    recovered=setup.recover_interrupted_upgrade(**args,payload_zip=str(archive))
    assert recovered['recovered']
    assert (app/'QA-Tester.exe').read_bytes()==b'MZ-OLD-V2-BINARY'
    assert (data / 'data/qa-tester.db').read_bytes() == b'SQLITE-USER-PROJECTS-AND-RESULTS'
