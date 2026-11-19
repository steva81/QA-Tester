import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.qa.scoped_mutation import (
    AutonomousProfileWorkflowRunner,
    AuthenticatedIdentityDiscovery,
    ScopedOwnershipRecord,
)

@pytest.fixture
def mock_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(AuthenticatedIdentityDiscovery, 'discover_session_identity', AsyncMock(return_value='tester@account'))
    runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path / "scratch"))
    return runner

@pytest.mark.asyncio
@pytest.mark.parametrize("remote_id,confirmed_at,expected_status,readback_called", [
    (None, None, "PENDING", False),
    ("rem-123", None, "PENDING", False),
    (None, "2026-11-05T10:00:00Z", "PENDING", False),
])
async def test_provenance_gate_blocks_readback(mock_runner, remote_id, confirmed_at, expected_status, readback_called, monkeypatch):
    """When remote_entity_id or confirmed_at is absent, readback must NOT be called and status stays PENDING."""
    readback_mock = AsyncMock(return_value=('CONFIRMED_SUCCESS', 'Absent', {}))
    monkeypatch.setattr(mock_runner, '_verify_authenticated_scope_readback', readback_mock)
    
    details = {}
    if confirmed_at:
        details["confirmed_at"] = confirmed_at
        
    rec = ScopedOwnershipRecord(
        id=1, run_id=1, project_id=1, target_url="http://127.0.0.1:8000/profile",
        account_context="tester@account", entity_type="profile_attachment",
        intent_id="i-1", run_prefix="[QA]", entity_id="e-1",
        remote_entity_id=remote_id, entity_name="doc.txt",
        status="CREATED", remote_status="CONFIRMED_EXISTS",
        local_cleaned=True, remote_cleaned=False,
        details_json=json.dumps(details),
    )
    monkeypatch.setattr(mock_runner.registry, 'get_pending_or_uncleaned_records', MagicMock(return_value=[rec]))
    
    res = await mock_runner.reconcile_and_recover_pending_entities(
        page=MagicMock(url="http://127.0.0.1:8000/profile"),
        project_id=1,
        target_url="http://127.0.0.1:8000/profile",
        account_context="tester@account",
        run_id=1,
    )
    assert len(res) == 1
    assert res[0].cleanup_status == expected_status
    assert readback_mock.called == readback_called

@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_json_val", [
    "{invalid-json",
    "\"just a string\"",
    "[1, 2, 3]",
    "12345",
    "null",
])
async def test_corrupt_or_non_object_json_blocks_recovery_without_readback(mock_runner, invalid_json_val, monkeypatch):
    """Invalid JSON or non-dict JSON must fail safe, leave item uncleaned and never call readback."""
    readback_mock = AsyncMock(return_value=('CONFIRMED_SUCCESS', 'Absent', {}))
    monkeypatch.setattr(mock_runner, '_verify_authenticated_scope_readback', readback_mock)
    
    rec = ScopedOwnershipRecord(
        id=2, run_id=1, project_id=1, target_url="http://127.0.0.1:8000/profile",
        account_context="tester@account", entity_type="profile_attachment",
        intent_id="i-2", run_prefix="[QA]", entity_id="e-2",
        remote_entity_id="rem-corrupt", entity_name="doc.txt",
        status="CREATED", remote_status="CONFIRMED_EXISTS",
        local_cleaned=True, remote_cleaned=False,
        details_json=invalid_json_val,
    )
    monkeypatch.setattr(mock_runner.registry, 'get_pending_or_uncleaned_records', MagicMock(return_value=[rec]))
    
    res = await mock_runner.reconcile_and_recover_pending_entities(
        page=MagicMock(url="http://127.0.0.1:8000/profile"),
        project_id=1,
        target_url="http://127.0.0.1:8000/profile",
        account_context="tester@account",
        run_id=1,
    )
    assert len(res) == 1
    assert res[0].cleanup_status == "PENDING"
    assert not readback_mock.called

@pytest.mark.asyncio
async def test_positive_confirmed_absent_recovery(mock_runner, monkeypatch):
    """When remote_entity_id and confirmed_at exist and readback confirms absent, marks CLEANED_UP without double DELETE."""
    readback_mock = AsyncMock(return_value=('CONFIRMED_SUCCESS', 'Confirmed absent', {}))
    monkeypatch.setattr(mock_runner, '_verify_authenticated_scope_readback', readback_mock)
    
    confirm_del_mock = MagicMock()
    monkeypatch.setattr(mock_runner.registry, 'confirm_remote_deletion', confirm_del_mock)
    
    rec = ScopedOwnershipRecord(
        id=3, run_id=1, project_id=1, target_url="http://127.0.0.1:8000/profile",
        account_context="tester@account", entity_type="profile_attachment",
        intent_id="i-3", run_prefix="[QA]", entity_id="e-3",
        remote_entity_id="rem-ok", entity_name="doc.txt",
        status="CREATED", remote_status="CONFIRMED_EXISTS",
        local_cleaned=True, remote_cleaned=False,
        details_json=json.dumps({"confirmed_at": "2026-11-05T10:00:00Z"}),
    )
    monkeypatch.setattr(mock_runner.registry, 'get_pending_or_uncleaned_records', MagicMock(return_value=[rec]))
    
    res = await mock_runner.reconcile_and_recover_pending_entities(
        page=MagicMock(url="http://127.0.0.1:8000/profile"),
        project_id=1,
        target_url="http://127.0.0.1:8000/profile",
        account_context="tester@account",
        run_id=1,
    )
    assert len(res) == 1
    assert res[0].cleanup_status == "CLEANED_UP"
    assert res[0].verdict == "PASS"
    assert readback_mock.called
    assert confirm_del_mock.called

@pytest.mark.asyncio
async def test_uncertain_readback_stays_pending(mock_runner, monkeypatch):
    """When readback is uncertain, status must remain PENDING without confirmed deletion."""
    readback_mock = AsyncMock(return_value=('UNCERTAIN_READBACK', 'Page timeout', {}))
    monkeypatch.setattr(mock_runner, '_verify_authenticated_scope_readback', readback_mock)
    
    confirm_del_mock = MagicMock()
    monkeypatch.setattr(mock_runner.registry, 'confirm_remote_deletion', confirm_del_mock)
    
    rec = ScopedOwnershipRecord(
        id=4, run_id=1, project_id=1, target_url="http://127.0.0.1:8000/profile",
        account_context="tester@account", entity_type="profile_attachment",
        intent_id="i-4", run_prefix="[QA]", entity_id="e-4",
        remote_entity_id="rem-ok", entity_name="doc.txt",
        status="CREATED", remote_status="CONFIRMED_EXISTS",
        local_cleaned=True, remote_cleaned=False,
        details_json=json.dumps({"confirmed_at": "2026-11-05T10:00:00Z"}),
    )
    monkeypatch.setattr(mock_runner.registry, 'get_pending_or_uncleaned_records', MagicMock(return_value=[rec]))
    
    res = await mock_runner.reconcile_and_recover_pending_entities(
        page=MagicMock(url="http://127.0.0.1:8000/profile"),
        project_id=1,
        target_url="http://127.0.0.1:8000/profile",
        account_context="tester@account",
        run_id=1,
    )
    assert len(res) == 1
    assert res[0].cleanup_status == "PENDING"
    assert res[0].verdict == "LIMITED_COVERAGE"
    assert not confirm_del_mock.called

