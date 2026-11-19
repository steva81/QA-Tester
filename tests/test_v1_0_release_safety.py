"""Independent gap tests: synthetic trees, no real user profile mutations."""
import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path
import pytest
from installer import setup_installer as setup, uninstaller as uninstall
from installer.path_safety import checked_path, member_path


@pytest.fixture
def layout(tmp_path, monkeypatch):
    app, data, menu = (tmp_path/n for n in ('app', 'QATester', 'menu'))
    monkeypatch.setattr(setup, 'check_webview2_runtime', lambda **kw: (True,'test',None))
    monkeypatch.setattr(uninstall, '_default_user_data_dir', lambda: str(data))
    content = b'application'
    entry = {'relative_path':'QA-Tester.exe', 'sha256':hashlib.sha256(content).hexdigest().upper()}
    monkeypatch.setattr(uninstall, '_owned_files', lambda: [entry])
    archive = tmp_path/'payload.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('QA-Tester.exe',content)
        z.writestr('package_manifest.json',json.dumps({'files':[entry]}))
    args = dict(install_dir=str(app),user_data_dir=str(data),start_menu_dir=str(menu),registry_key='')
    return app, data, menu, archive, args


def install(layout):
    return setup.run_install(**layout[-1], payload_zip=str(layout[3]), create_shortcuts=False)


def test_install_reinstall_default_remove_preserves_unknown_and_data(layout):
    app,data,menu,archive,args = layout
    install(layout)
    (data/'data/project.db').write_bytes(b'project and results')
    (app/'unrelated.txt').write_text('preserve')
    install(layout)
    uninstall.run_uninstall(**args)
    assert not (app/'QA-Tester.exe').exists()
    assert (app/'unrelated.txt').read_text() == 'preserve'
    assert (data/'data/project.db').read_bytes() == b'project and results'


def test_bad_payload_never_changes_existing_application(layout):
    app,data,menu,archive,args = layout
    install(layout)
    before=(app/'QA-Tester.exe').read_bytes()
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('QA-Tester.exe',b'corrupt')
        z.writestr('package_manifest.json',json.dumps({'files':[{'relative_path':'QA-Tester.exe','sha256':'0'*64}]}))
    with pytest.raises(RuntimeError): install(layout)
    assert (app/'QA-Tester.exe').read_bytes()==before


def test_changed_version_fails_without_overwriting_old(layout):
    app,data,menu,archive,args=layout
    install(layout)
    (app/'QA-Tester.exe').write_bytes(b'previous version')
    with pytest.raises(ValueError,match='preserved'): install(layout)
    assert (app/'QA-Tester.exe').read_bytes()==b'previous version'


def test_unrelated_directory_never_recursively_deleted(layout):
    app,data,menu,archive,args=layout
    app.mkdir(); (app/'sentinel').write_text('keep')
    uninstall.run_uninstall(**args)
    assert (app/'sentinel').read_text()=='keep'


@pytest.mark.parametrize('operation',['install','uninstall'])
def test_overlapping_user_data_rejected_before_mutation(layout, operation):
    app,data,menu,archive,args=layout
    app.mkdir(); (app/'sentinel').write_text('keep')
    args['user_data_dir']=str(app/'data')
    with pytest.raises(ValueError,match='overlap'):
        install(layout) if operation=='install' else uninstall.run_uninstall(**args)
    assert (app/'sentinel').exists()


def test_receipt_cannot_redirect_purge_or_registry(layout):
    app,data,menu,archive,args=layout
    install(layout)
    victim=app.parent/'victim'; victim.mkdir(); (victim/'sentinel').write_text('keep')
    (app/'install_receipt.json').write_text(json.dumps({'user_data_dir':str(victim),'registry_key':'Software\\Victim'}))
    result=uninstall.run_uninstall(**args,purge_user_data=True)
    assert result['user_data_purged'] and (victim/'sentinel').exists()


def test_custom_purge_target_rejected(layout):
    app,data,menu,archive,args=layout
    install(layout)
    args['user_data_dir']=str(app.parent/'victim')
    with pytest.raises(ValueError,match='Purge'): uninstall.run_uninstall(**args,purge_user_data=True)
    assert (app/'QA-Tester.exe').exists()


@pytest.mark.parametrize('name',['../outside','/outside','C:/outside','a:stream','a/../../outside','a./file','NUL','a\\..\\outside'])
def test_unsafe_members_rejected(tmp_path,name):
    with pytest.raises(ValueError): member_path(str(tmp_path),name)


def test_junction_write_and_delete_rejected(layout):
    app,data,menu,archive,args=layout
    victim=app.parent/'victim'; victim.mkdir(); (victim/'sentinel').write_text('keep')
    # Native PowerShell junction creation; no cross-shell deletion.
    env=os.environ.copy(); env['QAT_JUNCTION']=str(app); env['QAT_TARGET']=str(victim)
    subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                    'New-Item -ItemType Junction -Path $env:QAT_JUNCTION -Target $env:QAT_TARGET | Out-Null'],env=env,check=True)
    try:
        with pytest.raises(ValueError,match='reparse'): install(layout)
        with pytest.raises(ValueError,match='reparse'): uninstall.run_uninstall(**args)
        assert (victim/'sentinel').read_text()=='keep'
    finally:
        os.rmdir(app)  # Remove the junction itself, never its target.


def test_frozen_uninstaller_rejects_foreign_target(layout,monkeypatch):
    app,data,menu,archive,args=layout
    app.mkdir(); (app/'sentinel').write_text('keep')
    monkeypatch.setattr(uninstall.sys,'frozen',True,raising=False)
    with pytest.raises(ValueError,match='own installation'): uninstall.run_uninstall(**args)
    assert (app/'sentinel').exists()


def test_frozen_setup_rejects_external_payload(layout,monkeypatch):
    monkeypatch.setattr(setup.sys,'frozen',True,raising=False)
    with pytest.raises(ValueError,match='embedded payload'): setup._discover_payload_zip(str(layout[3]))


def test_environment_does_not_change_default_data_directory(monkeypatch,tmp_path):
    before=uninstall._default_user_data_dir()
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path))
    assert uninstall._default_user_data_dir()==before


def test_hardlinked_receipt_rejected_before_install(layout):
    app,data,menu,archive,args=layout
    install(layout)
    victim=app.parent/'victim.txt'; victim.write_text('unrelated')
    (app/'install_receipt.json').unlink()
    os.link(victim,app/'install_receipt.json')
    with pytest.raises(ValueError,match='Hard-linked'): install(layout)
    assert victim.read_text()=='unrelated'


def _seed_trusted_v2_layout(tmp_path, monkeypatch):
    app, data, menu = (tmp_path / n for n in ('app', 'QATester', 'menu'))
    monkeypatch.setattr(setup, 'check_webview2_runtime', lambda **kw: (True, 'test', None))
    monkeypatch.setattr(uninstall, '_default_user_data_dir', lambda: str(data))

    old_exe = b'MZ-OLD-V2-BINARY'
    old_dll = b'DLL-OLD-V2-ONLY'
    old_shared = b'DLL-SHARED-V2'
    old_files = [
        {'relative_path': 'QA-Tester.exe', 'sha256': hashlib.sha256(old_exe).hexdigest().upper()},
        {'relative_path': '_internal/old_only.dll', 'sha256': hashlib.sha256(old_dll).hexdigest().upper()},
        {'relative_path': '_internal/shared.dll', 'sha256': hashlib.sha256(old_shared).hexdigest().upper()},
    ]
    old_release = {
        'candidate': 'v2_verified',
        'version': '2.0',
        'exe_sha256': old_files[0]['sha256'],
        'files': old_files,
    }

    new_exe = b'MZ-NEW-V1-0-BINARY-DIFFERENT-BYTES'
    new_uninst = b'MZ-NEW-V1-0-UNINSTALLER'
    new_shared = b'DLL-SHARED-V1-0-UPDATED'
    new_files = [
        {'relative_path': 'QA-Tester.exe', 'sha256': hashlib.sha256(new_exe).hexdigest().upper()},
        {'relative_path': 'QA-Tester-Uninstall.exe', 'sha256': hashlib.sha256(new_uninst).hexdigest().upper()},
        {'relative_path': '_internal/shared.dll', 'sha256': hashlib.sha256(new_shared).hexdigest().upper()},
    ]
    new_release = {
        'candidate': 'v1_0_verified',
        'version': '1.0.0',
        'exe_sha256': new_files[0]['sha256'],
        'files': new_files,
    }

    monkeypatch.setattr(setup, '_trusted_releases', lambda: [old_release, new_release])
    monkeypatch.setattr(uninstall, '_owned_files', lambda: new_files)

    # Populate verified v2 installation on disk
    (app / '_internal').mkdir(parents=True)
    (app / 'QA-Tester.exe').write_bytes(old_exe)
    (app / '_internal/old_only.dll').write_bytes(old_dll)
    (app / '_internal/shared.dll').write_bytes(old_shared)
    (app / 'package_manifest.json').write_text(json.dumps(old_release), encoding='utf-8')
    (app / 'install_receipt.json').write_text(
        json.dumps({'app_name': 'QA Tester', 'version': '2.0', 'install_dir': str(app)}),
        encoding='utf-8',
    )

    # Create new payload zip
    archive = tmp_path / 'payload_v1_0.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('QA-Tester.exe', new_exe)
        z.writestr('QA-Tester-Uninstall.exe', new_uninst)
        z.writestr('_internal/shared.dll', new_shared)
        z.writestr('package_manifest.json', json.dumps(new_release))

    (data / 'data').mkdir(parents=True)
    (data / 'data/qa-tester.db').write_bytes(b'SQLITE-USER-PROJECTS-AND-RESULTS')

    args = dict(install_dir=str(app), user_data_dir=str(data), start_menu_dir=str(menu), registry_key='')
    return app, data, menu, archive, args, old_release, new_release


def test_verified_version_upgrade_replaces_old_files_and_preserves_user_data(tmp_path, monkeypatch):
    import stat

    app, data, menu, archive, args, _old_rel, _new_rel = _seed_trusted_v2_layout(tmp_path, monkeypatch)
    # Mark existing _internal directory as ReadOnly (as OneDrive/Windows often does) to verify clean backup removal
    os.chmod(app / '_internal', stat.S_IREAD)
    res = setup.run_install(**args, payload_zip=str(archive), create_shortcuts=False)
    assert res['status'] == 'success'
    assert res['receipt']['is_upgrade'] is True
    assert res['receipt']['upgrade_mode'] == 'upgrade'
    assert res['receipt']['upgraded_from_candidate'] == 'v2_verified'
    assert (app / 'QA-Tester.exe').read_bytes() == b'MZ-NEW-V1-0-BINARY-DIFFERENT-BYTES'
    assert (app / 'QA-Tester-Uninstall.exe').read_bytes() == b'MZ-NEW-V1-0-UNINSTALLER'
    assert (app / '_internal/shared.dll').read_bytes() == b'DLL-SHARED-V1-0-UPDATED'
    assert not (app / '_internal/old_only.dll').exists()
    assert not Path(setup._backup_dir_for(str(app))).exists()
    assert (data / 'data/qa-tester.db').read_bytes() == b'SQLITE-USER-PROJECTS-AND-RESULTS'

    # Clean uninstallation of upgraded directory removes all new files and directory itself
    un_res = uninstall.run_uninstall(**args)
    assert un_res['status'] == 'success'
    assert not app.exists()
    assert (data / 'data/qa-tester.db').read_bytes() == b'SQLITE-USER-PROJECTS-AND-RESULTS'


def test_upgrade_rejects_unknown_file_or_dir_in_verified_installation(tmp_path, monkeypatch):
    app, data, _menu, archive, args, _old_rel, _new_rel = _seed_trusted_v2_layout(tmp_path, monkeypatch)
    (app / 'notes.txt').write_text('user note in app dir')
    with pytest.raises(ValueError, match='unknown or foreign file'):
        setup.run_install(**args, payload_zip=str(archive), create_shortcuts=False)
    assert (app / 'QA-Tester.exe').read_bytes() == b'MZ-OLD-V2-BINARY'
    assert (app / 'notes.txt').read_text() == 'user note in app dir'

    (app / 'notes.txt').unlink()
    (app / 'extra_dir').mkdir()
    with pytest.raises(ValueError, match='unknown directory'):
        setup.run_install(**args, payload_zip=str(archive), create_shortcuts=False)
    assert (app / 'QA-Tester.exe').read_bytes() == b'MZ-OLD-V2-BINARY'


def test_upgrade_rejects_forged_manifest_in_untrusted_directory(tmp_path, monkeypatch):
    app, data, _menu, archive, args, _old_rel, _new_rel = _seed_trusted_v2_layout(tmp_path, monkeypatch)
    forged_bytes = b'MZ-FORGED-UNKNOWN-APP'
    forged_hash = hashlib.sha256(forged_bytes).hexdigest().upper()
    (app / '_internal/old_only.dll').unlink()
    (app / '_internal/shared.dll').unlink()
    (app / '_internal').rmdir()
    (app / 'QA-Tester.exe').write_bytes(forged_bytes)
    (app / 'package_manifest.json').write_text(
        json.dumps({'files': [{'relative_path': 'QA-Tester.exe', 'sha256': forged_hash}]}),
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='preserved'):
        setup.run_install(**args, payload_zip=str(archive), create_shortcuts=False)
    assert (app / 'QA-Tester.exe').read_bytes() == forged_bytes


def test_upgrade_rejects_tampered_dll_in_trusted_installation(tmp_path, monkeypatch):
    app, _data, _menu, archive, args, _old_rel, _new_rel = _seed_trusted_v2_layout(tmp_path, monkeypatch)
    (app / '_internal/old_only.dll').write_bytes(b'TAMPERED-DLL')
    with pytest.raises(ValueError, match='preserved'):
        setup.run_install(**args, payload_zip=str(archive), create_shortcuts=False)
    assert (app / 'QA-Tester.exe').read_bytes() == b'MZ-OLD-V2-BINARY'
    assert (app / '_internal/old_only.dll').read_bytes() == b'TAMPERED-DLL'


def test_upgrade_rejects_foreign_shortcut_and_foreign_registry(tmp_path, monkeypatch):
    app, _data, menu, archive, args, _old_rel, _new_rel = _seed_trusted_v2_layout(tmp_path, monkeypatch)
    foreign_exe = tmp_path / 'foreign' / 'QA-Tester.exe'
    foreign_exe.parent.mkdir()
    foreign_exe.write_bytes(b'foreign')
    setup.create_windows_shortcut(
        shortcut_path=str(menu / 'QA Tester.lnk'),
        target_path=str(foreign_exe),
        working_dir=str(foreign_exe.parent),
    )
    with pytest.raises(ValueError, match='different installation'):
        setup.run_install(**args, payload_zip=str(archive), create_shortcuts=True)
    assert (app / 'QA-Tester.exe').read_bytes() == b'MZ-OLD-V2-BINARY'


@pytest.mark.parametrize('failure_stage', ['manifest_check', 'shortcut_write', 'registry_write'])
def test_upgrade_rolls_back_completely_on_activation_failures(tmp_path, monkeypatch, failure_stage):
    app, data, menu, archive, args, _old_rel, _new_rel = _seed_trusted_v2_layout(tmp_path, monkeypatch)
    reg_key = r'Software\QATester_Isolated_RollbackTest\Uninstall\QATester'
    args['registry_key'] = reg_key
    import winreg

    try:
        # Establish initial v2 shortcut and v2 registry entry
        setup.create_windows_shortcut(
            shortcut_path=str(menu / 'QA Tester.lnk'),
            target_path=str(app / 'QA-Tester.exe'),
            working_dir=str(app),
            description='QA Tester v2',
        )
        old_lnk_bytes = (menu / 'QA Tester.lnk').read_bytes()
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, 'DisplayVersion', 0, winreg.REG_SZ, '2.0')
            winreg.SetValueEx(k, 'InstallLocation', 0, winreg.REG_SZ, str(app))

        if failure_stage == 'manifest_check':
            orig_verify = setup._verify_installed_manifest
            calls = {'n': 0}

            def fail_second_verify(d):
                calls['n'] += 1
                if calls['n'] >= 2:
                    raise RuntimeError('Simulated post-swap manifest verification failure')
                return orig_verify(d)

            monkeypatch.setattr(setup, '_verify_installed_manifest', fail_second_verify)
        elif failure_stage == 'shortcut_write':
            def fail_shortcut(**kw):
                raise RuntimeError('Simulated shortcut creation failure after swap')

            monkeypatch.setattr(setup, 'create_windows_shortcut', fail_shortcut)
        elif failure_stage == 'registry_write':
            def fail_reg(**kw):
                raise OSError('Simulated registry write failure after swap and shortcuts')

            monkeypatch.setattr(setup, '_write_uninstall_registry', fail_reg)

        with pytest.raises((RuntimeError, OSError)):
            setup.run_install(**args, payload_zip=str(archive), create_shortcuts=True)

        # Verify complete rollback of application files, shortcuts, registry, and user data
        assert (app / 'QA-Tester.exe').read_bytes() == b'MZ-OLD-V2-BINARY'
        assert (app / '_internal/old_only.dll').read_bytes() == b'DLL-OLD-V2-ONLY'
        assert (app / '_internal/shared.dll').read_bytes() == b'DLL-SHARED-V2'
        assert not (app / 'QA-Tester-Uninstall.exe').exists()
        assert (menu / 'QA Tester.lnk').read_bytes() == old_lnk_bytes
        assert not (menu / 'Uninstall QA Tester.lnk').exists()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_READ) as k:
            assert winreg.QueryValueEx(k, 'DisplayVersion')[0] == '2.0'
        assert (data / 'data/qa-tester.db').read_bytes() == b'SQLITE-USER-PROJECTS-AND-RESULTS'
        assert not Path(setup._backup_dir_for(str(app))).exists()
        assert not Path(setup._journal_path_for(str(app))).exists()
    finally:
        for subk in (
            r'Software\QATester_Isolated_RollbackTest\Uninstall\QATester',
            r'Software\QATester_Isolated_RollbackTest\Uninstall',
            r'Software\QATester_Isolated_RollbackTest',
        ):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subk)
            except OSError:
                pass


@pytest.mark.parametrize('crash_state', ['after_backup_only', 'after_new_moved_in'])
def test_recover_interrupted_upgrade_and_auto_recover_on_reinstall(tmp_path, monkeypatch, crash_state):
    app, data, menu, archive, args, _old_rel, new_rel = _seed_trusted_v2_layout(tmp_path, monkeypatch)
    backup = Path(setup._backup_dir_for(str(app)))
    journal = Path(setup._journal_path_for(str(app)))

    # Simulate hard process termination mid-swap leaving backup and journal on disk
    os.rename(app, backup)
    journal.write_text(
        json.dumps({'app_name': 'QA Tester', 'install_dir': str(app), 'previous_candidate': 'v2_verified'}),
        encoding='utf-8',
    )
    if crash_state == 'after_new_moved_in':
        with zipfile.ZipFile(archive, 'r') as z:
            z.extractall(app)

    rec = setup.recover_interrupted_upgrade(**args, payload_zip=str(archive))
    assert rec['status'] == 'recovered'
    assert rec['recovered'] is True
    assert rec['restored_candidate'] == 'v2_verified'
    assert (app / 'QA-Tester.exe').read_bytes() == b'MZ-OLD-V2-BINARY'
    assert (app / '_internal/old_only.dll').read_bytes() == b'DLL-OLD-V2-ONLY'
    assert not backup.exists()
    assert not journal.exists()

    # Now simulate crash again and verify that run_install automatically recovers first and then upgrades
    os.rename(app, backup)
    journal.write_text(
        json.dumps({'app_name': 'QA Tester', 'install_dir': str(app), 'previous_candidate': 'v2_verified'}),
        encoding='utf-8',
    )
    res = setup.run_install(**args, payload_zip=str(archive), create_shortcuts=False)
    assert res['status'] == 'success'
    assert res['receipt']['recovered_prior_interruption'] is True
    assert res['receipt']['upgrade_mode'] == 'upgrade'
    assert (app / 'QA-Tester.exe').read_bytes() == b'MZ-NEW-V1-0-BINARY-DIFFERENT-BYTES'
    assert not backup.exists()
    assert not journal.exists()
