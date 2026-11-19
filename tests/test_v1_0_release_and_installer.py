"""Targeted verification suite for QA Tester 1.0 rule configuration UI, legacy rule review,
visible limitation notices, and Windows installer/uninstaller lifecycle."""

import hashlib
import json
import os
import zipfile
import pytest

from app.models.database import get_db_connection, init_db
from app.paths import APP_VERSION
from app.qa.validation_contract import clear_validation_contracts
from app.routes.api import inspect_stored_contract_row
from installer.setup_installer import run_install
from installer.shortcut_helper import read_windows_shortcut
from installer.uninstaller import run_uninstall
from installer.webview2_check import check_webview2_runtime
from server import app


@pytest.fixture
def isolated_v1_0_db(tmp_path, monkeypatch):
    db_file = tmp_path / "v1_0_test.sqlite"
    monkeypatch.setenv("QA_TESTER_DB_PATH", str(db_file))
    init_db()
    clear_validation_contracts()
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO projects(id, name, target_url, environment, tester_name) "
        "VALUES (1, 'V1.0 Project', 'http://127.0.0.1:8123/profile', 'local', 'Tester')"
    )
    conn.execute("INSERT INTO runs(id, project_id, status) VALUES (1, 1, 'COMPLETED')")
    conn.execute(
        "INSERT INTO tests(run_id, url, expected_result, actual_result, status, viewport) "
        "VALUES (1, 'http://127.0.0.1:8123/profile', 'Page loads', 'Loaded', 'PASS', 'Desktop')"
    )
    conn.commit()
    yield conn
    conn.close()
    clear_validation_contracts()


def test_app_version_is_1_0_0():
    assert APP_VERSION == "1.0.0"


def test_legacy_misconfigured_contract_is_flagged_not_auto_migrated(isolated_v1_0_db):
    # Insert a legacy/incomplete rule without field_selectors or test_cases
    isolated_v1_0_db.execute(
        "INSERT INTO validation_contracts (id, project_id, contract_id, rule_name, target_url_pattern, "
        "target_control_id, feedback_selector, config_json) "
        "VALUES (10, 1, 'legacy_date_rule', 'Legacy Date Rule', '*/profile*', 'btn-validate', '#fb', '{}')"
    )
    isolated_v1_0_db.commit()

    row = isolated_v1_0_db.execute("SELECT * FROM validation_contracts WHERE id = 10").fetchone()
    info = inspect_stored_contract_row(row)
    assert info["config_valid"] is False
    assert "field" in (info["config_error"] or "").lower()

    client = app.test_client()
    # Viewing the project page must NOT auto-migrate the rule; it must render NEEDS REVIEW / INVALID CONFIG
    resp = client.get("/project/1")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "NEEDS REVIEW / INVALID CONFIG" in html
    assert "Review &amp; Replace" in html
    assert "fieldRowsContainer" in html
    assert "caseRowsContainer" in html

    # Attempting to start a run while the legacy rule is unreviewed must still fail closed (HTTP 400)
    run_resp = client.post("/api/runs", json={"project_id": 1, "mode": "crawl"})
    assert run_resp.status_code == 400

    # Explicitly reviewing and replacing the legacy rule via replace_row_id
    replace_payload = {
        "replace_row_id": 10,
        "contract_id": "reviewed_date_rule",
        "rule_name": "Reviewed Date Rule",
        "target_url_pattern": "*/profile*",
        "target_control_id": "btn-validate",
        "feedback_selector": "#fb",
        "completion_mode": "case_marker",
        "completion_marker_attr": "data-validation-case",
        "field_selectors": {"start_date": "#start", "end_date": "#end"},
        "test_cases": [
            {
                "case_id": "case_invalid",
                "description": "End before start rejected",
                "input_values": {"start_date": "2025-05-01", "end_date": "2024-01-01"},
                "expected_valid": False,
                "expected_feedback_substring": "cannot be earlier",
            },
            {
                "case_id": "case_valid",
                "description": "Valid date interval accepted",
                "input_values": {"start_date": "2024-01-01", "end_date": "2025-05-01"},
                "expected_valid": True,
                "expected_feedback_substring": "Dates are valid",
            },
        ],
    }
    rep_resp = client.post("/api/projects/1/contracts", json=replace_payload)
    assert rep_resp.status_code == 201
    rep_json = rep_resp.get_json()
    assert rep_json["replaced_id"] == 10

    # Verify old row 10 is gone and new row is VALID CONTRACT
    rows = isolated_v1_0_db.execute("SELECT * FROM validation_contracts WHERE project_id = 1").fetchall()
    assert len(rows) == 1
    assert rows[0]["contract_id"] == "reviewed_date_rule"
    new_info = inspect_stored_contract_row(rows[0])
    assert new_info["config_valid"] is True
    assert new_info["positive_count"] == 1
    assert new_info["negative_count"] == 1

    html_after = client.get("/project/1").get_data(as_text=True)
    assert "VALID CONTRACT" in html_after
    assert "NEEDS REVIEW / INVALID CONFIG" not in html_after


def test_contract_validation_clear_error_messages(isolated_v1_0_db):
    client = app.test_client()

    # 1. Missing feedback_selector (must NOT silently fallback to WORK_HISTORY_DATE_CONTRACT)
    r1 = client.post(
        "/api/projects/1/contracts",
        json={
            "contract_id": "no_fb",
            "rule_name": "No Feedback Selector",
            "target_control_id": "btn-submit",
            "completion_mode": "loading_transition",
            "field_selectors": {"email": "#email"},
            "test_cases": [
                {
                    "case_id": "c1",
                    "description": "Invalid email",
                    "input_values": {"email": "bad"},
                    "expected_valid": False,
                    "expected_feedback_substring": "Invalid",
                }
            ],
        },
    )
    assert r1.status_code == 400
    assert "Feedback selector" in r1.get_json()["error"]

    # 2. case_marker mode without completion_marker_attr
    r2 = client.post(
        "/api/projects/1/contracts",
        json={
            "contract_id": "missing_marker",
            "rule_name": "Missing Marker",
            "target_control_id": "btn-submit",
            "feedback_selector": "#fb",
            "completion_mode": "case_marker",
            "field_selectors": {"email": "#email"},
            "test_cases": [
                {
                    "case_id": "c1",
                    "description": "Invalid email",
                    "input_values": {"email": "bad"},
                    "expected_valid": False,
                    "expected_feedback_substring": "Invalid",
                }
            ],
        },
    )
    assert r2.status_code == 400
    assert "completion_marker_attr" in r2.get_json()["error"]


def test_results_page_visibly_renders_manual_acknowledgement_limitation(isolated_v1_0_db):
    isolated_v1_0_db.execute(
        """INSERT INTO mutation_ownership_records(
            id, run_id, project_id, target_url, account_context, entity_type,
            intent_id, run_prefix, entity_id, entity_name, status, remote_status,
            local_cleaned, remote_cleaned, details_json
        ) VALUES (
            1, 1, 1, 'http://127.0.0.1:8123/profile', 'actor', 'profile_attachment',
            'intent', '[QA-TEST]', 'att-1', 'test.txt', 'CREATED', 'AMBIGUOUS',
            1, 0, '{"ambiguity_reason":"readback_500"}'
        )"""
    )
    isolated_v1_0_db.commit()

    client = app.test_client()
    res = client.post("/api/projects/1/pending-resources/1/resolve", json={"note": "Verified on staging server"})
    assert res.status_code == 200

    page_html = client.get("/run/1").get_data(as_text=True)
    assert 'id="limitation-reasons-notice"' in page_html
    assert "1 test resource(s) manually acknowledged; server deletion is NOT confirmed" in page_html


def test_webview2_runtime_check_and_simulation():
    ok_real, ver_real, err_real = check_webview2_runtime(simulate_missing=False)
    assert ok_real is True
    assert ver_real is not None
    assert err_real is None

    ok_sim, ver_sim, err_sim = check_webview2_runtime(simulate_missing=True)
    assert ok_sim is False
    assert ver_sim is None
    assert "WebView2" in (err_sim or "")


def test_isolated_installer_upgrade_and_uninstaller_lifecycle(tmp_path, monkeypatch):
    # Create a small synthetic payload zip with QA-Tester.exe, QA-Tester-Uninstall.exe, and package_manifest.json
    staging_app = tmp_path / "staging_app"
    staging_app.mkdir()
    fake_exe = staging_app / "QA-Tester.exe"
    fake_exe.write_bytes(b"MZ-FAKE-QA-TESTER-1.0.0")
    fake_uninst = staging_app / "QA-Tester-Uninstall.exe"
    fake_uninst.write_bytes(b"MZ-FAKE-UNINSTALLER-1.0.0")

    def sha256_bytes(b: bytes) -> str:
        return hashlib.sha256(b).hexdigest().upper()

    manifest = {
        "candidate": "synthetic_v1_0",
        "version": "1.0.0",
        "exe_sha256": sha256_bytes(fake_exe.read_bytes()),
        "files": [
            {
                "relative_path": "QA-Tester.exe",
                "size_bytes": fake_exe.stat().st_size,
                "sha256": sha256_bytes(fake_exe.read_bytes()),
            },
            {
                "relative_path": "QA-Tester-Uninstall.exe",
                "size_bytes": fake_uninst.stat().st_size,
                "sha256": sha256_bytes(fake_uninst.read_bytes()),
            },
        ],
    }
    (staging_app / "package_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    payload_zip = tmp_path / "QA-Tester-1.0.0-payload.zip"
    with zipfile.ZipFile(payload_zip, "w") as zf:
        for f in staging_app.iterdir():
            zf.write(f, f.name)

    install_dir = tmp_path / "ProgramFilesLocal" / "QATester"
    start_menu_dir = tmp_path / "StartMenu" / "Programs" / "QA Tester"
    user_data_dir = tmp_path / "LocalAppData" / "QATester"
    # Bind trust roots in-process; production purge cannot accept arbitrary paths.
    from installer import uninstaller
    monkeypatch.setattr(uninstaller, '_default_user_data_dir', lambda: str(user_data_dir))
    monkeypatch.setattr(uninstaller, '_owned_files', lambda: manifest['files'])
    test_reg_key = r"Software\QATester_Isolated_Pytest_1_0\Uninstall\QATester"

    try:
        # 1. Missing WebView2 blocks installation cleanly
        blocked = run_install(
            install_dir=str(install_dir),
            start_menu_dir=str(start_menu_dir),
            user_data_dir=str(user_data_dir),
            registry_key=test_reg_key,
            payload_zip=str(payload_zip),
            simulate_missing_webview2=True,
        )
        assert blocked["status"] == "error"
        assert blocked["error_code"] == "MISSING_WEBVIEW2_RUNTIME"
        assert not install_dir.exists()

        # 2. Fresh installation succeeds and creates shortcuts + registry + user data folders
        res1 = run_install(
            install_dir=str(install_dir),
            start_menu_dir=str(start_menu_dir),
            user_data_dir=str(user_data_dir),
            registry_key=test_reg_key,
            payload_zip=str(payload_zip),
            simulate_missing_webview2=False,
        )
        assert res1["status"] == "success"
        assert res1["receipt"]["is_upgrade"] is False
        assert (install_dir / "QA-Tester.exe").is_file()
        assert (start_menu_dir / "QA Tester.lnk").is_file()
        sc_info = read_windows_shortcut(str(start_menu_dir / "QA Tester.lnk"))
        assert os.path.normcase(sc_info["target_path"]) == os.path.normcase(str(install_dir / "QA-Tester.exe"))

        # Create user project DB and settings inside user_data_dir
        user_db = user_data_dir / "data" / "qa-tester.db"
        user_db.write_text("USER-PROJECTS-AND-RUNS-V1", encoding="utf-8")
        user_pref = user_data_dir / "settings" / "preferences.json"
        user_pref.write_text('{"theme":"light","preserved":true}', encoding="utf-8")

        # 3. Upgrade installation preserves user DB and settings
        res_upgrade = run_install(
            install_dir=str(install_dir),
            start_menu_dir=str(start_menu_dir),
            user_data_dir=str(user_data_dir),
            registry_key=test_reg_key,
            payload_zip=str(payload_zip),
        )
        assert res_upgrade["status"] == "success"
        assert res_upgrade["receipt"]["is_upgrade"] is True
        assert res_upgrade["receipt"]["user_data_preservation"]["db_preserved_intact"] is True
        assert user_db.read_text(encoding="utf-8") == "USER-PROJECTS-AND-RUNS-V1"
        assert user_pref.read_text(encoding="utf-8") == '{"theme":"light","preserved":true}'

        # 4. Default uninstallation removes app and shortcuts but preserves user data
        uninst_default = run_uninstall(
            install_dir=str(install_dir),
            start_menu_dir=str(start_menu_dir),
            user_data_dir=str(user_data_dir),
            registry_key=test_reg_key,
            purge_user_data=False,
        )
        assert uninst_default["status"] == "success"
        assert uninst_default["user_data_preserved"] is True
        assert uninst_default["user_data_purged"] is False
        assert not (install_dir / "QA-Tester.exe").exists()
        assert not (start_menu_dir / "QA Tester.lnk").exists()
        assert user_db.read_text(encoding="utf-8") == "USER-PROJECTS-AND-RUNS-V1"
        assert user_pref.read_text(encoding="utf-8") == '{"theme":"light","preserved":true}'

        # 5. Explicit purge uninstallation deletes user data when requested
        uninst_purge = run_uninstall(
            install_dir=str(install_dir),
            start_menu_dir=str(start_menu_dir),
            user_data_dir=str(user_data_dir),
            registry_key=test_reg_key,
            purge_user_data=True,
        )
        assert uninst_purge["status"] == "success"
        assert uninst_purge["user_data_purged"] is True
        assert not user_data_dir.exists()
    finally:
        # Ensure test registry parent key is cleaned up
        if os.name == "nt":
            import winreg

            for subk in (
                r"Software\QATester_Isolated_Pytest_1_0\Uninstall\QATester",
                r"Software\QATester_Isolated_Pytest_1_0\Uninstall",
                r"Software\QATester_Isolated_Pytest_1_0",
            ):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subk)
                except OSError:
                    pass

