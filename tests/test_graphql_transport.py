"""End-to-end GraphQL batches, operation selection and genuine server errors."""
import asyncio
import json
import pytest
from test_full_site import site, isolated_state, browser_case, engine

READ = {'query': 'query Read { viewer { id } }'}
MIXED = 'query Read { viewer { id } } mutation Write { update { id } }'


@pytest.mark.parametrize('payload,allowed', [
    ([READ, READ], True),
    ({'query': MIXED, 'operationName':'Read'}, True),
    ([READ, {'query':'mutation Write { update { id } }'}], False),
    ({'query': MIXED, 'operationName':'Write'}, False),
    ({'query': MIXED}, False),
])
def test_selected_operations_reach_only_read_server(site, monkeypatch, payload, allowed):
    received = []
    def post(handler):
        received.append(json.loads(handler.rfile.read(int(handler.headers['Content-Length']))))
        body = b'{"data":{"viewer":{"id":"fixture"}}}'
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    monkeypatch.setattr(site.server.RequestHandlerClass, 'do_POST', post)
    site.links['/'] = []
    site.script = '''<script>fetch('/graphql', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(%s)})
        .then(r=>r.json()).then(()=>window.loaded=true).catch(e=>console.error(e));</script>''' % json.dumps(payload)
    async def check(context, user):
        qa = engine(site, context)
        results = await qa.run()
        assert received == ([payload]*3 if allowed else [])
        assert qa.get_progress()['pages_fully_evaluated'] == int(allowed)
        assert any(r['status']=='SAFETY ACTION' for r in results) is (not allowed)
        assert any(r['status']=='LIMITED COVERAGE' for r in results) is (not allowed)
        assert site.mutations == 0
    asyncio.run(browser_case(site, check))


def test_genuine_query_http_failure_is_not_safety_diagnostic(site, monkeypatch):
    received = []
    def post(handler):
        handler.rfile.read(int(handler.headers['Content-Length']))
        received.append(handler.path)
        handler.send_response(500)
        handler.send_header('Content-Length', '0')
        handler.end_headers()
    monkeypatch.setattr(site.server.RequestHandlerClass, 'do_POST', post)
    site.links['/'] = []
    site.script = '''<script>fetch('/graphql',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:'{ viewer { id } }'})})
        .then(r=>{if(!r.ok)console.error('GENUINE_GRAPHQL_HTTP_'+r.status)});</script>'''
    async def check(context, user):
        results = await engine(site, context).run()
        assert received == ['/graphql']*3
        assert not any(r['status'] in ('SAFETY ACTION','LIMITED COVERAGE','DIAGNOSTIC') for r in results)
        # Existing reporting retains the first independent console error; the
        # browser's HTTP 500 message can precede the application's own message.
        findings = [r for r in results if r['status'] in ('FAIL','OBSERVATION') and '500' in r['actual_result']]
        assert {r['viewport'] for r in findings} == {'Desktop 1366x768', 'Mobile 390x844'}, results
        assert all(r['finding_id'] for r in findings)
    asyncio.run(browser_case(site, check))
