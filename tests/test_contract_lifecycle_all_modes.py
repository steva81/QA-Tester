import json
import pytest
from app.models.database import init_db, get_db_connection
from app.routes.api import _load_project_validation_contracts
from app.qa.validation_contract import (
    find_validation_contract_for_control,
    clear_validation_contracts,
    register_validation_contract,
    unregister_validation_contract,
)
import tempfile
import os


@pytest.fixture(autouse=True)
def clean_registry():
    clear_validation_contracts()
    yield
    clear_validation_contracts()


@pytest.fixture
def temp_db(monkeypatch):
    db_fd, db_path = tempfile.mkstemp()
    monkeypatch.setenv('QA_TESTER_DB_PATH', db_path)
    init_db()
    conn = get_db_connection()
    yield conn
    conn.close()
    os.close(db_fd)
    try:
        os.unlink(db_path)
    except OSError:
        pass


def test_load_project_validation_contracts(temp_db):
    c = temp_db.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Proj Contracts', 'http://127.0.0.1:8000', 'local', 'Tester')")
    proj_id = c.lastrowid

    # Insert a validation contract in SQLite
    cfg = {
        "field_selectors": {"email": "#email-input"},
        "completion_mode": "loading_transition",
        "test_cases": [
            {
                "case_id": "tc-1",
                "description": "Invalid email syntax",
                "input_values": {"email": "invalid"},
                "expected_valid": False,
                "expected_feedback_substring": "Invalid email",
            }
        ],
    }
    c.execute(
        """
        INSERT INTO validation_contracts
        (project_id, contract_id, rule_name, target_url_pattern, target_control_id, trigger_selector, accessible_name, feedback_selector, config_json)
        VALUES (?, 'contract-email-val', 'Email Validation Rule', '*/register', 'btn-submit-reg', '#btn-submit-reg', 'Register Button', '#email-error', ?)
        """,
        (proj_id, json.dumps(cfg)),
    )
    temp_db.commit()

    # Before loading, registry does not have the contract
    assert find_validation_contract_for_control('btn-submit-reg', target_url='http://127.0.0.1:8000/register', project_id=proj_id) is None

    # Load contracts for this project and verify returned count
    count = _load_project_validation_contracts(temp_db, proj_id)
    assert count == 1

    # Now find_validation_contract_for_control must find it!
    found = find_validation_contract_for_control('btn-submit-reg', target_url='http://127.0.0.1:8000/register', project_id=proj_id)
    assert found is not None
    assert found.contract_id == 'contract-email-val'
    assert found.rule_name == 'Email Validation Rule'
    assert found.feedback_selector == '#email-error'
    assert len(found.test_cases) == 1
    assert found.test_cases[0].case_id == 'tc-1'

    # Project isolation: a different project_id does NOT match
    assert find_validation_contract_for_control('btn-submit-reg', target_url='http://127.0.0.1:8000/register', project_id=999) is None


def test_contracts_activate_across_all_modes(temp_db, monkeypatch):
    from server import app
    app.config['TESTING'] = True
    client = app.test_client()

    c = temp_db.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('All Modes Proj', 'http://127.0.0.1:8000', 'local', 'Tester')")
    proj_id = c.lastrowid

    # Insert a validation contract in SQLite
    c.execute(
        """
        INSERT INTO validation_contracts
        (project_id, contract_id, rule_name, target_url_pattern, target_control_id, trigger_selector, accessible_name, feedback_selector, config_json)
        VALUES (?, 'contract-mode-test', 'All Mode Rule', '*/test', 'btn-test', '#btn-test', 'Test Button', '#feedback', ?)
        """,
        (proj_id, json.dumps({'field_selectors': {'email':'#email'}, 'completion_mode':'loading_transition','test_cases':[{'case_id':'bad','description':'Bad email','input_values':{'email':'bad'},'expected_valid':False}]})),
    )
    temp_db.commit()

    dispatched_args = []
    def fake_run_qa(*args):
        dispatched_args.append(args)

    monkeypatch.setattr('app.routes.api.run_qa_async_in_thread', fake_run_qa)

    # Mode 1: crawl
    res_crawl = client.post('/api/runs', json={'project_id': proj_id, 'mode': 'crawl'}, headers={'X-QA-Request': '1'})
    assert res_crawl.status_code == 200
    assert len(dispatched_args) == 1
    # Check that run_options (args[5]) enabled interactive disclosure for contracts
    crawl_opts = dispatched_args[0][5]
    assert crawl_opts['enable_interactive_disclosure'] is True

    # Check that contract is registered in memory
    assert find_validation_contract_for_control('btn-test', target_url='http://127.0.0.1:8000/test', project_id=proj_id) is not None

