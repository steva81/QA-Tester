import json
import pytest
from tests.test_v1_0_release_and_installer import isolated_v1_0_db
from server import app


def payload():
    return dict(contract_id='explicit',rule_name='Required email',target_control_id='submit',
                feedback_selector='#feedback',completion_mode='case_marker',completion_marker_attr='data-case',
                field_selectors={'email':'#email'},test_cases=[dict(case_id='empty',description='Empty rejected',
                input_values={'email':''},expected_valid=False,expected_feedback_substring='Required')])


@pytest.mark.parametrize('fault',['duplicate_cases','duplicate_fields','missing_id','foreign_replace','invalid_replace'])
def test_invalid_replacement_preserves_old_row(isolated_v1_0_db,fault):
    conn=isolated_v1_0_db
    conn.execute("INSERT INTO validation_contracts(id,project_id,contract_id,rule_name,config_json) VALUES(40,1,'old','Old','{}')")
    conn.commit()
    p=payload(); p['replace_row_id']=40
    if fault=='duplicate_cases': p['test_cases']*=2
    if fault=='duplicate_fields': p['field_selectors'][' email ']='#other'
    if fault=='missing_id': del p['contract_id']
    if fault=='foreign_replace': p['replace_row_id']=999
    if fault=='invalid_replace': p['completion_marker_attr']=''
    r=app.test_client().post('/api/projects/1/contracts',json=p)
    assert r.status_code in (400,404)
    assert conn.execute('SELECT contract_id FROM validation_contracts WHERE id=40').fetchone()[0]=='old'


def test_empty_negative_input_is_persisted_exactly(isolated_v1_0_db):
    p=payload()
    r=app.test_client().post('/api/projects/1/contracts',json=p)
    assert r.status_code==201
    row=isolated_v1_0_db.execute('SELECT config_json FROM validation_contracts').fetchone()
    assert json.loads(row[0])['test_cases'][0]['input_values']=={'email':''}


def test_visual_builder_duplicate_and_empty_value(isolated_v1_0_db):
    from playwright.sync_api import sync_playwright
    html=app.test_client().get('/project/1').get_data(as_text=True)
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page()
        page.route('**/*',lambda route: route.abort())
        page.set_content(html,wait_until='domcontentloaded')
        page.evaluate('''() => {
            window.showToast=()=>{}; window.setTimeout=()=>{};
            window.fetch=async (url, opts)=>{window.sent=JSON.parse(opts.body); return {ok:true,json:async()=>({})}};
            resetContractModal();
            for (const [id,v] of Object.entries({c_id:'blank',c_name:'Empty rejected',c_ctrl:'submit',c_fb:'#feedback',c_mode:'case_marker',c_marker:'data-case'})) document.getElementById(id).value=v;
            document.getElementById('fieldRowsContainer').innerHTML='';
            addFieldRow('email','#email');
            document.querySelector('.rule-case-feedback').value='Required';
        }''')
        page.evaluate('submitAddContract()')
        assert page.evaluate('window.sent.test_cases[0].input_values')=={'email':''}
        assert page.evaluate('window.sent.test_cases[0].expected_valid') is False
        page.evaluate("window.sent=null; addFieldRow('email','#other'); submitAddContract()")
        assert page.evaluate('window.sent') is None
        assert 'unique Field Name' in page.locator('#contractFormError').inner_text()
        assert page.locator('#c_fields').get_attribute('readonly') is not None
        browser.close()
