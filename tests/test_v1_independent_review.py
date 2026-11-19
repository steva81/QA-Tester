"""Bounded independent v1 review: production APIs, discovery and real local browser execution."""
import json
import threading
from dataclasses import asdict
import pytest
from app.models.database import init_db, get_db_connection
from app.qa.scoped_mutation import ScopedMutationRegistry, discover_attachment_form_destination
from app.qa.validation_contract import clear_validation_contracts, WORK_HISTORY_DATE_CONTRACT
from app.routes.api import _load_project_validation_contracts
from app.qa.reporting import load_report
from server import app

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('QA_TESTER_DB_PATH', str(tmp_path/'review.sqlite'))
    init_db(); clear_validation_contracts()
    conn=get_db_connection()
    conn.execute("INSERT INTO projects(id,name,target_url,environment,tester_name) VALUES(1,'Review','http://127.0.0.1','local','Review')")
    conn.execute("INSERT INTO runs(id,project_id,status) VALUES(1,1,'COMPLETED')")
    conn.execute("INSERT INTO tests(run_id,url,expected_result,actual_result,status,viewport) VALUES(1,'http://127.0.0.1','ok','ok','PASS','Desktop')")
    conn.execute("""INSERT INTO mutation_ownership_records(run_id,project_id,target_url,account_context,entity_type,intent_id,run_prefix,entity_id,entity_name,status,remote_status,local_cleaned,remote_cleaned,details_json)
    VALUES(1,1,'http://127.0.0.1','actor','profile_attachment','intent','[QA-TEST]','att','test.txt','CREATED','CONFIRMED_EXISTS',1,0,'{"proof":"original"}')""")
    conn.commit()
    yield conn
    conn.close();clear_validation_contracts()

@pytest.mark.parametrize('payload,headers,status', [({'note':'x'}, {'X-QA-Request':'1'},400), ({'project_id':2},{'X-QA-Request':'1'},403), ({'project_id':'oops'},{'X-QA-Request':'1'},400), ({'project_id':1},{'Origin':'https://untrusted.example'},403)])
def test_pending_rejects_missing_scope_and_foreign_origin(db,payload,headers,status):
    r=app.test_client().post('/api/pending-resources/1/resolve',json=payload,headers=headers)
    assert r.status_code==status
    assert db.execute('SELECT status FROM mutation_ownership_records').fetchone()[0]=='CREATED'

def test_manual_audit_remains_visible_and_immutable(db):
    c=app.test_client()
    assert c.post('/api/projects/1/pending-resources/1/resolve',json={'note':'   '},headers={'X-QA-Request':'1'}).status_code==400
    assert c.post('/api/projects/1/pending-resources/1/resolve',json={'note':'checked manually'},headers={'X-QA-Request':'1'}).status_code==200
    row=db.execute('SELECT * FROM mutation_ownership_records').fetchone()
    detail=json.loads(row['details_json'])
    assert detail['proof']=='original' and detail['previous_remote_status']=='CONFIRMED_EXISTS'
    assert row['remote_cleaned']==0 and detail['server_deletion_confirmed'] is False
    report=load_report(db,1)
    assert report['outcome']=='LIMITED COVERAGE'
    assert any('server deletion is NOT confirmed' in s for s in report['limitation_reasons'])
    assert 'server deletion is NOT confirmed' in c.get('/run/1').get_data(as_text=True)
    assert c.post('/api/projects/1/pending-resources/1/resolve',json={'note':'overwrite'},headers={'X-QA-Request':'1'}).status_code==400
    assert db.execute('SELECT details_json FROM mutation_ownership_records').fetchone()[0]==row['details_json']

@pytest.mark.parametrize('path',['/api/account/close','/api/users/42','/api/files/overwrite','/api/profile/attachments/42','/api/profile/attachments?parent_id=42','/api/profile/attachments#x','/%61pi/profile/attachments','/api/../attachments','//evil.example/attachments'])
def test_auto_destination_rejects_unsafe_or_unknown_path(path):
    assert discover_attachment_form_destination(path) is None

@pytest.mark.parametrize('path',['/api/attachments','/api/profile/attachments/upload'])
def test_auto_destination_supported_collection(path):
    assert discover_attachment_form_destination(path)==path

def test_arbitrary_rule_cannot_silently_become_date_rule(db):
    r=app.test_client().post('/api/projects/1/contracts',json={'contract_id':'email','rule_name':'Email','target_control_id':'submit','feedback_selector':'#email-feedback','completion_mode':'loading_transition'},headers={'X-QA-Request':'1'})
    assert r.status_code==400
    assert db.execute('SELECT COUNT(*) FROM validation_contracts').fetchone()[0]==0

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['full','current','collected','crawl'])
@pytest.mark.parametrize('broken',[False,True],ids=['valid','accepts_invalid'])
async def test_persisted_contract_is_evaluated_and_reported_in_each_mode(db,mode,broken):
    from playwright.async_api import async_playwright
    from tests.profile_test_site import ProfileTestServer
    from app.qa.engine import SmartSmokeQA
    from app.qa.full_site import FullSiteQA
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            html = """<!doctype html><html><head><title>Contract fixture</title></head><body>
            <label>Start<input id="input-start-date" type="date"></label><label>End<input id="input-end-date" type="date"></label>
            <button type="button" id="btn-validate-history" aria-controls="history-date-feedback">Validate Work History Dates</button>
            <div id="history-date-feedback" role="alert" hidden></div><script>
            document.getElementById('btn-validate-history').onclick=()=>{
            const f=document.getElementById('history-date-feedback');
            const invalid=document.getElementById('input-end-date').value<document.getElementById('input-start-date').value;
            f.textContent=invalid?'End date cannot be earlier than start date.':'Dates are valid.';
            f.hidden=false;f.setAttribute('data-validation-case',invalid?'case_invalid_interval':'case_valid_interval');
            f.setAttribute('data-validation-timestamp',Date.now().toString());};
            </script></body></html>""".encode()
            if broken:
                html=html.replace(b"invalid?'End date cannot be earlier than start date.':'Dates are valid.'", b"'Dates are valid.'")
            self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(html)));self.end_headers();self.wfile.write(html)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.origin='http://127.0.0.1:'+str(server.server_port)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=server.origin+'/profile'
    contract=WORK_HISTORY_DATE_CONTRACT
    payload={'contract_id':'review-dates','rule_name':'Review dates','target_url_pattern':url,'target_control_id':'btn-validate-history','feedback_selector':contract.feedback_selector,'field_selectors':contract.field_selectors,'test_cases':[asdict(t) for t in contract.test_cases],'completion_mode':contract.effective_completion_mode,'completion_marker_attr':contract.completion_marker_attr,'completion_marker_type':contract.completion_marker_type,'completion_timestamp_attr':contract.completion_timestamp_attr}
    assert app.test_client().post('/api/projects/1/contracts',json=payload,headers={'X-QA-Request':'1'}).status_code==201
    clear_validation_contracts()
    assert _load_project_validation_contracts(db,1)==1
    try:
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True, chromium_sandbox=True)
            context=await browser.new_context()
            await (await context.new_page()).goto(url)
            options=dict(enable_interactive_disclosure=True,interactive_options={'max_evaluations_per_page':30,'max_evaluations_per_run':30})
            if mode=='full':
                engine=FullSiteQA(url,1,1,context=context,page_limit=1,current_url=url,session_validator=lambda:True,**options)
            else:
                engine=SmartSmokeQA(url,1,1,mode=mode,max_pages=1,collected_urls=[url],context=context,**options)
            import asyncio
            await asyncio.wait_for(engine.run(),timeout=90)
            if broken:
                matches=[r for r in engine.results if r.get('status')=='FAIL']
                assert matches, [(r.get('status'),r.get('actual_result')) for r in engine.results]
                assert not any('Verified local input validation' in r.get('actual_result','') for r in engine.results)
            else:
                matches=[r for r in engine.results if 'Verified local input validation' in r.get('actual_result','')]
                assert matches, [(r.get('status'),r.get('actual_result')) for r in engine.results]
                assert all(r['status']=='PASS' for r in matches)
            await browser.close()
    finally:
        server.shutdown();server.server_close()


from tests.test_cdp_upload_verification import profile_server as upload_server, guarded_page, uploads, file_part
from tests.test_scoped_mutation_adversarial import isolated_adv_db

@pytest.mark.asyncio
@pytest.mark.parametrize('destination,allowed',[('/api/profile/attachments/upload',True),('/api/account/close',False),('/api/profile/attachments/att-preexisting',False)])
async def test_form_discovery_through_real_workflow(upload_server,isolated_adv_db,tmp_path,destination,allowed):
    from app.qa.scoped_mutation import AutonomousProfileWorkflowRunner
    upload_server.upload_destination_contract=False
    async with guarded_page(upload_server) as (page,barrier,guard):
        await page.locator('#input-experience-file').evaluate('''(el,action)=>{const f=document.createElement('form');f.setAttribute('action',action);el.parentNode.insertBefore(f,el);f.appendChild(el);}''',destination)
        runner=AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path/'upload'),barrier=barrier)
        result=await runner._exercise_upload_workflow(page=page,target_url=upload_server.origin+'/profile?mutation=true',run_id=71,project_id=1,run_prefix='[QA-TEST-71-ATT]',viewport_name='Desktop',account_context=None,cancellation_token=None,screenshot_fn=None)
    if allowed:
        assert result.verdict=='PASS',result.details
        sent=uploads(upload_server);assert len(sent)==1
        name,content=file_part(sent[0]);assert name==result.entity_name and b'QA-Tester harmless verification upload' in content
        assert [o['id'] for o in upload_server.operations_log if o['op']=='DELETE']==[sent[0]['id']]==[result.entity_id]
        assert list(upload_server.attachments)==['att-preexisting']
    else:
        assert not uploads(upload_server)
        assert result.verdict=='LIMITED_COVERAGE' and not result.is_customer_defect

def test_invalid_persisted_contract_blocks_run_instead_of_silent_skip(db):
    db.execute("INSERT INTO validation_contracts(project_id,contract_id,rule_name,config_json) VALUES(1,'bad','Bad','{broken')")
    db.commit()
    assert app.test_client().post('/api/runs',json={'project_id':1,'mode':'crawl'},headers={'X-QA-Request':'1'}).status_code==400

@pytest.mark.asyncio
async def test_absent_unconfirmed_intent_is_not_server_deletion_proof(db,tmp_path,monkeypatch):
    from unittest.mock import AsyncMock
    from app.qa.scoped_mutation import AutonomousProfileWorkflowRunner, AuthenticatedIdentityDiscovery
    monkeypatch.setattr(AuthenticatedIdentityDiscovery,'discover_session_identity',AsyncMock(return_value='actor'))
    runner=AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path/'recovery'))
    readback=AsyncMock(return_value=('CONFIRMED_SUCCESS','Name not visible',{}))
    monkeypatch.setattr(runner,'_verify_authenticated_scope_readback',readback)
    result=await runner.reconcile_and_recover_pending_entities(object(),1,'http://127.0.0.1',account_context='actor',run_id=1)
    assert len(result)==1 and result[0].cleanup_status=='PENDING'
    assert not result[0].is_customer_defect
    row=db.execute('SELECT remote_cleaned,remote_status FROM mutation_ownership_records').fetchone()
    assert row['remote_cleaned']==0 and row['remote_status']!='CONFIRMED_DELETED'
    readback.assert_not_awaited()
