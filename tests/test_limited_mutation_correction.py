import hashlib
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.qa.scoped_mutation import (
    AutonomousProfileWorkflowRunner, AuthenticatedIdentityDiscovery,
    ScopedMutationNetworkBarrier, OperationType,
)


def test_normal_runner_has_no_destructive_probe_or_global_upload_headers():
    source = inspect.getsource(AutonomousProfileWorkflowRunner)
    assert not hasattr(AutonomousProfileWorkflowRunner, '_exercise_boundary_rejection')
    assert '/api/account/delete' not in source
    assert 'delete_account_permanently' not in source
    upload = inspect.getsource(AutonomousProfileWorkflowRunner._exercise_upload_workflow)
    assert 'set_extra_http_headers' not in upload
    assert 'window.fetch' not in upload
    assert '__QA_DISPATCH_TOKEN__' not in upload


@pytest.mark.asyncio
async def test_opaque_upload_with_matching_token_is_blocked_then_real_bytes_allowed(monkeypatch):
    actor = 'local-test-actor'
    origin = 'http://127.0.0.1:8080'
    monkeypatch.setattr(AuthenticatedIdentityDiscovery, 'discover_session_identity', AsyncMock(return_value=actor))
    barrier = ScopedMutationNetworkBarrier(origin)
    barrier.set_verified_session_identity(actor)
    content = b'approved attachment content'
    permit = barrier.acquire_permit(
        operation_type=OperationType.UPLOAD_ATTACHMENT,
        workflow_name='profile_attachment', allowed_methods=('POST',),
        endpoint_pattern='/api/attachments', account_context=actor,
        verified_account=actor, approved_file_name='test.txt',
        approved_file_hash=hashlib.sha256(content).hexdigest(),
        approved_file_size=len(content), controlled_dispatch_token='copied-token',
    )
    page = SimpleNamespace(evaluate=AsyncMock())
    def route(body):
        request = SimpleNamespace(
            method='POST', url=origin+'/api/attachments',
            post_data_buffer=body, post_data=None,
            headers={'content-type':'multipart/form-data; boundary=review-boundary', 'X-Dispatch-Token':'copied-token'},
            frame=SimpleNamespace(page=page),
        )
        return SimpleNamespace(request=request, abort=AsyncMock(), continue_=AsyncMock())
    opaque = route(None)
    await barrier._handle_route(opaque)
    opaque.abort.assert_awaited_once()
    opaque.continue_.assert_not_awaited()
    assert permit.use_count == 0 and not permit.consumed
    assert 'unsupported' in barrier.blocked_requests[-1]['reason'].lower()
    body = (b'--review-boundary\r\nContent-Disposition: form-data; name="file"; filename="test.txt"\r\nContent-Type: text/plain\r\n\r\n'
            + content + b'\r\n--review-boundary--\r\n')
    approved = route(body)
    await barrier._handle_route(approved)
    approved.continue_.assert_awaited_once()
    approved.abort.assert_not_awaited()
    assert permit.use_count == 1 and permit.consumed
