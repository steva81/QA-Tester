"""Focused regressions for request identity, cancellation and permit atomicity."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.qa.browser_guard import BrowserMutationGuard
from app.qa.scoped_mutation import ScopedMutationNetworkBarrier, AuthenticatedIdentityDiscovery

ORIGIN = 'http://127.0.0.1:9876'

def setup_guard(monkeypatch):
    barrier = ScopedMutationNetworkBarrier(ORIGIN)
    permit = barrier.acquire_permit(operation_type='EDIT_ENTITY', workflow_name='profile_experience',
        allowed_methods=('PUT',), endpoint_pattern=r'^/api/experience/12$', resource_id='12',
        account_context='actor-a', verified_account='actor-a', max_uses=1)
    guard = BrowserMutationGuard(SimpleNamespace(), [], ORIGIN, mutation_barrier=barrier)
    guard.session = SimpleNamespace(send=AsyncMock())
    guard.frames['frame'] = 'qa'
    guard.roots['frame'] = 'root'
    page = SimpleNamespace(evaluate=AsyncMock())
    # Hashable page stand-in, as in Playwright.
    class Page:
        evaluate = AsyncMock()
    page = Page()
    guard.page_roots[page] = 'root'
    monkeypatch.setattr(AuthenticatedIdentityDiscovery, 'discover_session_identity', AsyncMock(return_value='actor-a'))
    return barrier, permit, guard

def event(id='request-1', body='{"id":"12"}'):
    return {'requestId': id, 'frameId': 'frame', 'request': {
        'method':'PUT', 'url':ORIGIN+'/api/experience/12',
        'headers':{'Content-Type':'application/json'}, 'postData':body}}

@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['different_body','cancelled','expired','changed_actor'])
async def test_url_timestamp_receipt_never_authorizes_another_request(monkeypatch, case):
    barrier, permit, guard = setup_guard(monkeypatch)
    permit.use_count = 1
    permit.consumed = True
    permit._approved_inflight = ('PUT', ORIGIN+'/api/experience/12', time.time())
    if case == 'cancelled': barrier.is_cancelled = lambda: True
    if case == 'expired': permit.expires_at = time.time()-1
    if case == 'changed_actor':
        monkeypatch.setattr(AuthenticatedIdentityDiscovery, 'discover_session_identity', AsyncMock(return_value='actor-b'))
    await guard._handle(event(body='{"id":"99"}' if case == 'different_body' else '{"id":"12"}'))
    calls = [c.args[0] for c in guard.session.send.call_args_list]
    assert 'Fetch.continueRequest' not in calls
    assert 'Fetch.failRequest' in calls

@pytest.mark.asyncio
@pytest.mark.parametrize('case',['cancelled','changed_actor'])
async def test_cdp_only_checks_live_cancellation_and_actor(monkeypatch, case):
    barrier, permit, guard = setup_guard(monkeypatch)
    if case == 'cancelled': barrier.is_cancelled = lambda: True
    else: monkeypatch.setattr(AuthenticatedIdentityDiscovery, 'discover_session_identity', AsyncMock(return_value='actor-b'))
    await guard._handle(event())
    assert not any(c.args[0] == 'Fetch.continueRequest' for c in guard.session.send.call_args_list)
    assert permit.use_count == 0

@pytest.mark.asyncio
async def test_concurrent_identity_checks_cannot_spend_one_permit_twice(monkeypatch):
    barrier, permit, guard = setup_guard(monkeypatch)
    ready = asyncio.Event()
    calls = 0
    async def identity(*args):
        nonlocal calls
        calls += 1
        if calls == 2: ready.set()
        await ready.wait()
        return 'actor-a'
    monkeypatch.setattr(AuthenticatedIdentityDiscovery, 'discover_session_identity', identity)
    page = next(iter(guard.page_roots))
    routes=[]
    for _ in range(2):
        req = SimpleNamespace(url=ORIGIN+'/api/experience/12', method='PUT',
            headers={'content-type':'application/json'}, post_data_buffer=b'{"id":"12"}',
            frame=SimpleNamespace(page=page))
        routes.append(SimpleNamespace(request=req, continue_=AsyncMock(), abort=AsyncMock()))
    await asyncio.wait_for(asyncio.gather(*(barrier._handle_route(r) for r in routes)),2)
    assert sum(r.continue_.await_count for r in routes) == 1
    assert sum(r.abort.await_count for r in routes) == 1
    assert permit.use_count == 1

from tests.test_scoped_mutation_adversarial import adv_server, isolated_adv_db
from app.qa.scoped_mutation import AutonomousProfileWorkflowRunner, ScopedEntityOwnershipRegistry
from playwright.async_api import async_playwright

@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['create', 'upload'])
async def test_failed_creation_cannot_promote_dom_id_to_recovery_authority(adv_server, isolated_adv_db, tmp_path, monkeypatch, kind):
    runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path))
    prefix = '[QA-TEST-701-REVIEW]'
    name = prefix + ' Nordic Quality AB' if kind == 'create' else 'review-upload.txt'
    class FailedPermit:
        use_count = 1
        recorded_responses = [{'status':503}]
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(runner, '_permit', lambda **kwargs: FailedPermit())
    file = tmp_path / name
    if kind == 'upload':
        file.write_text('harmless review test')
        monkeypatch.setattr(runner.registry, 'generate_harmless_file', lambda *args: (str(file),name))
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, )
        try:
            page = await browser.new_page()
            if kind == 'upload':
                html = f"""<meta name="authenticated-user" content="tester@example.com">
                  <section data-section="attachments" data-qa-inventory-complete="true">
                    <h4>Attachment</h4><input type="file" data-qa-upload-destination="/api/profile/attachments/upload">
                    <button id="btn-upload-file">Upload</button>
                    <div data-attachment-id="preexisting-victim"><span>{name}</span></div>
                  </section>"""
            else:
                html = f"""<meta name="authenticated-user" content="tester@example.com">
                  <section id="section-experience-management" data-qa-inventory-complete="true">
                    <button id="btn-add-experience">Add Experience</button>
                    <form id="form-add-experience" onsubmit="return false">
                      <input id="exp-company"><input id="exp-role"><textarea id="exp-description"></textarea>
                      <button id="btn-submit-experience" type="button">Save Experience</button>
                    </form>
                    <div class="exp-item" data-id="preexisting-victim"><span>{name}</span></div>
                  </section>"""
            await page.route(adv_server.origin+'/review',lambda r:r.fulfill(body=html,content_type='text/html'))
            await page.goto(adv_server.origin+'/review')
            args=dict(page=page,target_url=adv_server.origin+'/review',run_id=701,project_id=1,
                run_prefix=prefix,viewport_name='Desktop',account_context='tester@example.com',
                cancellation_token=None,screenshot_fn=None)
            if kind == 'upload': result = await runner._exercise_upload_workflow(**args)
            else: _, result = await runner._exercise_create_workflow(**args)
            assert '503' in result.details, result.details
            record = ScopedEntityOwnershipRegistry.get_records_for_run(701)[0]
            assert record.remote_entity_id is None
            assert record.remote_cleaned is False
            assert result.cleanup_status == 'PENDING'
        finally:
            await browser.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('sweep', ['recovery', 'cleanup'])
@pytest.mark.parametrize('provenance', ['missing', 'renamed'])
async def test_legacy_ambiguous_id_without_confirmation_cannot_authorize_recovery(adv_server, isolated_adv_db, tmp_path, sweep, provenance):
    registry=ScopedEntityOwnershipRegistry()
    name='[QA-TEST-702-REVIEW] Owned-looking name'
    adv_server.attachments['exp-victim']={'id':'exp-victim','filename':name,'content':b'keep'}
    record=registry.record_creation_intent(run_id=702,project_id=1,target_url=adv_server.origin+'/profile',
        entity_type='profile_attachment',run_prefix='[QA-TEST-702-REVIEW]',proposed_name=name,account_context='tester@example.com')
    # Reproduce the journal produced by the old HTTP-error DOM-ID adoption path.
    if provenance in ('renamed', 'confirmed'):
        registry.confirm_creation(record.id,'exp-victim',name)
    if provenance == 'renamed':
        adv_server.attachments['exp-victim']['filename']='Another resource'
    registry.mark_ambiguous_creation(record.id,'Server error; DOM ID guessed',remote_entity_id='exp-victim')
    async with async_playwright() as p:
        browser=await p.chromium.launch(chromium_sandbox=True, )
        try:
            page=await browser.new_page()
            await page.goto(adv_server.origin+'/profile')
            runner=AutonomousProfileWorkflowRunner(registry=registry,scratch_dir=str(tmp_path))
            if sweep == 'recovery':
                results=await runner.reconcile_and_recover_pending_entities(page,1,adv_server.origin+'/profile')
            else:
                results=await runner._cleanup_run_entities(page,702,1,target_url=adv_server.origin+'/profile')
            current=registry.get_records_for_run(702)[0]
            if provenance == 'confirmed':
                assert 'exp-victim' not in adv_server.attachments
                assert current.remote_cleaned
            else:
                assert 'exp-victim' in adv_server.attachments
                assert not current.remote_cleaned
                assert any(r.cleanup_status=='PENDING' for r in results)
        finally:
            await browser.close()

from tests.test_cdp_upload_verification import guarded_page, profile_server

@pytest.mark.asyncio
async def test_real_cdp_same_url_concurrent_bodies_are_bound_and_single_use(profile_server):
    profile_server.experiences['12']={'id':'12','company':'Original','role':'keep','desc':'keep'}
    async with guarded_page(profile_server) as (page, barrier, guard):
        permit=barrier.acquire_permit(operation_type='EDIT_ENTITY',workflow_name='profile_experience',
            allowed_methods=('PUT',),endpoint_pattern=r'^/api/profile/experience/12$',resource_id='12',
            account_context='tester@example.com',verified_account='tester@example.com',max_uses=1)
        results=await page.evaluate("""async () => {
            const send = async id => {
                try {const r=await fetch('/api/profile/experience/12',{method:'PUT',
                    headers:{'Content-Type':'application/json'},body:JSON.stringify({id,company:'Approved edit'})});
                    return r.status;} catch {return 'blocked';}
            };
            return await Promise.all([send('99'),send('12'),send('12')]);
        }""")
        assert results[0]=='blocked'
        assert results.count(200)==1
        assert results.count('blocked')==2
        assert permit.use_count==1
        assert len([op for op in profile_server.operations_log if op['op']=='EDIT' and op['id']=='12'])==1

@pytest.mark.asyncio
@pytest.mark.parametrize("sweep", ["recovery", "cleanup"])
async def test_supported_attachment_sweeps_confirm_remote_deletion(profile_server, isolated_adv_db, tmp_path, sweep):
    adv_server = profile_server
    provenance = "confirmed"
    registry=ScopedEntityOwnershipRegistry()
    name='[QA-TEST-702-REVIEW] Owned-looking name'
    adv_server.attachments['exp-victim']={'id':'exp-victim','filename':name,'content':b'keep'}
    record=registry.record_creation_intent(run_id=702,project_id=1,target_url=adv_server.origin+'/profile?mutation=true',
        entity_type='profile_attachment',run_prefix='[QA-TEST-702-REVIEW]',proposed_name=name,account_context='tester@example.com')
    # Reproduce the journal produced by the old HTTP-error DOM-ID adoption path.
    if provenance in ('renamed', 'confirmed'):
        registry.confirm_creation(record.id,'exp-victim',name)
    if provenance == 'renamed':
        adv_server.attachments['exp-victim']['filename']='Another resource'
    registry.mark_ambiguous_creation(record.id,'Server error; DOM ID guessed',remote_entity_id='exp-victim')
    async with async_playwright() as p:
        browser=await p.chromium.launch(chromium_sandbox=True, )
        try:
            page=await browser.new_page()
            await page.goto(adv_server.origin+'/profile?mutation=true')
            runner=AutonomousProfileWorkflowRunner(registry=registry,scratch_dir=str(tmp_path))
            if sweep == 'recovery':
                results=await runner.reconcile_and_recover_pending_entities(page,1,adv_server.origin+'/profile?mutation=true')
            else:
                results=await runner._cleanup_run_entities(page,702,1,target_url=adv_server.origin+'/profile?mutation=true')
            current=registry.get_records_for_run(702)[0]
            if provenance == 'confirmed':
                assert 'exp-victim' not in adv_server.attachments
                assert current.remote_cleaned
            else:
                assert 'exp-victim' in adv_server.attachments
                assert not current.remote_cleaned
                assert any(r.cleanup_status=='PENDING' for r in results)
        finally:
            await browser.close()

