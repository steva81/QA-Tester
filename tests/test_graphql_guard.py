"""Local transport regression: real HTTP, authenticated background QA, no live target."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from test_full_site import site, isolated_state, browser_case, engine
from app.qa.engine import SmartSmokeQA

QUERY = 'query Viewer { viewer { id } }'
MUTATION = 'mutation Update { update { id } }'


def request(payload, **changes):
    values = dict(method='POST', url='https://example.test/api/proxy/graphql',
                  headers={'content-type': 'application/json'},
                  frame=SimpleNamespace(page=object()),
                  post_data_buffer=json.dumps(payload).encode())
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize('payload,allowed', [
    ({'query': QUERY}, True),
    ({'query': MUTATION}, False),
    ({'query': QUERY+' '+MUTATION, 'operationName': 'Viewer'}, True),
    ({'query': QUERY+' '+MUTATION, 'operationName': 'Update'}, False),
    ({'query': QUERY+' '+MUTATION}, False),
    ({'query': QUERY+' '+MUTATION, 'operationName': 'Missing'}, False),
    ({'query': '{ viewer { id } }'}, True),
    ({'query': 'query { viewer { id } }'}, True),
    ({'query': '# mutation comment\nquery Viewer { viewer { ...Fields } } fragment Fields on Viewer { id }'}, True),
    ({'query': 'query { __schema { queryType { name } } }'}, True),
    ({'query': 'query X($id: ID!) { viewer(id: $id) { id } }', 'variables': {'id':'mutation'}}, True),
    ({'query': 'query { viewer @include(if: true) { id } }'}, True),
    ({'query': 'query { viewer @write { id } }'}, False),
    ([{'query': QUERY}, {'query': '{ viewer { id } }'}], True),
    ([{'query': QUERY}, {'query': MUTATION}], False),
    ([{'query': QUERY}, {'query': QUERY+' '+MUTATION}], False),
    ([{'query': QUERY}, None], False),
    ([[{'query': QUERY}]], False),
    ([], False),
    ([{'query': QUERY}]*21, False),
    ({'extensions': {'persistedQuery': {'sha256Hash': '0'*64}}}, False),
    ({'query': QUERY, 'extensions': {'persistedQuery': {'sha256Hash': '0'*64}}}, False),
    ({'query': QUERY, 'extensions': {}}, True),
    ({'query': QUERY, 'extensions': None}, False),
    ({'query': QUERY, 'action': 'delete'}, False),
    ({'operationName': 'Viewer'}, False),
    ({'query': None}, False),
    ({'query': 'query Viewer {'}, False),
    ({'query': 'subscription Feed { updates { id } }'}, False),
    ({'query': QUERY+' query Viewer { viewer { name } }', 'operationName':'Viewer'}, False),
    ({'query': '{ viewer { id } } '+QUERY, 'operationName':'Viewer'}, False),
    ({'query': 'query { viewer { ...Missing } }'}, False),
    ({'query': 'query { ...A } fragment A on Query { ...A }'}, False),
    ({'query': QUERY+' type NewType { id: ID }'}, False),
    ({'query': QUERY, 'operationName': ''}, False),
    ({'query': QUERY, 'operationName': 1}, False),
    ({'query': QUERY, 'variables': []}, False),
    ({'action': 'save'}, False),
])
def test_operation_semantics(payload, allowed):
    from app.qa.graphql_policy import is_readonly_graphql_post
    assert is_readonly_graphql_post(request(payload), 'https://example.test:443') is allowed


@pytest.mark.parametrize('changes', [
    {'method': 'PUT'}, {'method':'PATCH'}, {'method':'DELETE'},
    {'url': 'https://external.test/graphql'},
    {'url': 'http://example.test/graphql'},
    {'url': 'https://example.test:444/graphql'},
    {'url': 'https://example.test/write'},
    {'url': 'https://example.test/graphql/delete'},
    {'url': 'https://example.test/logout/graphql'},
    {'url': 'https://example.test/graphql?operationName=Update'},
    {'url': 'https://user:secret@example.test/graphql'},
    {'frame': None},
    {'frame': SimpleNamespace(page=None)},
    {'headers': {}},
    {'headers': {'content-type': 'multipart/form-data; boundary=x'}},
    {'headers': {'content-type': 'application/graphql'}},
    {'headers': {'content-type': 'text/plain'}},
    {'headers': {'content-type': 'application/json; charset=utf-16'}},
    {'headers': {'content-type': 'application/json', 'content-encoding': 'gzip'}},
    {'headers': {'content-type': 'application/json', 'x-http-method-override': 'DELETE'}},
    {'post_data_buffer': b'not JSON'},
    {'post_data_buffer': b'\xff'},
    {'post_data_buffer': None},
    {'post_data_buffer': b'{"query":"{ x }","query":"mutation { x }"}'},
    {'post_data_buffer': b'{"query":"{ x }","variables":{"x":NaN}}'},
    {'post_data_buffer': b' '*262145},
    {'post_data_buffer': ('{"query":"{'+'x '*11000+'}"}').encode()},
])
def test_unsupported_transport_fails_closed(changes):
    from app.qa.graphql_policy import is_readonly_graphql_post
    assert not is_readonly_graphql_post(request({'query': QUERY}, **changes), 'https://example.test:443')


def test_json_utf8_content_type():
    from app.qa.graphql_policy import is_readonly_graphql_post
    assert is_readonly_graphql_post(request({'query':QUERY},
        headers={'content-type':'Application/JSON; charset="UTF-8"'}), 'https://example.test:443')


@pytest.mark.parametrize('destination', ['/write', '/graphql', 'https://external.invalid/graphql'])
def test_allowed_query_redirect_is_not_followed(site, monkeypatch, destination):
    requests = []
    def post(handler):
        handler.rfile.read(int(handler.headers['Content-Length']))
        requests.append(handler.path)
        handler.send_response(307)
        handler.send_header('Location', destination)
        handler.send_header('Content-Length', '0')
        handler.end_headers()
    monkeypatch.setattr(site.server.RequestHandlerClass, 'do_POST', post)
    site.links['/'] = []
    site.script = '<script>fetch("/api/proxy/graphql",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({query:"{ viewer { id } }"})}).catch(e=>console.error(e));</script>'
    async def check(context, user):
        qa = engine(site, context)
        results = await qa.run()
        assert requests == ['/api/proxy/graphql']*3
        assert any(r['status']=='SAFETY ACTION' for r in results)
        assert qa.get_progress()['pages_fully_evaluated'] == 0
        assert not any(r['status'] in ('PASS','FAIL','OBSERVATION') for r in results)
    asyncio.run(browser_case(site, check))


@pytest.mark.parametrize('mode', ['current', 'full'])
@pytest.mark.parametrize('query,allowed', [(QUERY, True), (MUTATION, False)])
def test_graphql_background_reads_and_blocks(site, monkeypatch, mode, query, allowed):
    reads = []
    def post(handler):
        payload = json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
        if payload['query'] != QUERY:
            site.mutations += 1
        reads.append(handler.headers.get('Cookie', ''))
        body = b'{"data":{"viewer":{"id":"fixture"}}}'
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    monkeypatch.setattr(site.server.RequestHandlerClass, 'do_POST', post)
    site.links['/'] = []
    site.script = '''<script>fetch('/api/proxy/graphql', {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(%s)
    }).then(r=>r.json()).then(data=>{document.querySelector('label').textContent=data.data.viewer.id;window.readDone=true})
      .catch(e=>console.error({error:e}));</script>''' % json.dumps({'query': query})
    async def check(context, user):
        qa = engine(site, context) if mode == 'full' else SmartSmokeQA(site.base+'/', 1, 1, mode='current', context=context)
        if allowed:
            navigate = qa._navigate_page
            async def loaded(page, url):
                response = await navigate(page, url)
                await page.wait_for_function('window.readDone===true', timeout=2000)
                return response
            monkeypatch.setattr(qa, '_navigate_page', loaded)
        results = await qa.run()
        assert site.mutations == 0
        if allowed:
            assert len(reads) == (3 if mode == 'full' else 2), results
            assert all('qa_login=yes' in cookie for cookie in reads)
            assert not any(r['status'] in ('SAFETY ACTION', 'LIMITED COVERAGE', 'DIAGNOSTIC') for r in results), results
            assert sum(r['status'] == 'PASS' for r in results) == 2
            if mode == 'full':
                assert qa.get_progress()['pages_fully_evaluated'] == 1
                assert not qa.get_progress()['discovery_safety_limited']
        else:
            assert not reads
            assert sum(r['status'] == 'LIMITED COVERAGE' for r in results) == 2
            assert any(r['status'] == 'SAFETY ACTION' for r in results)
            assert any(r['status'] == 'DIAGNOSTIC' for r in results)
            assert not any(r['status'] in ('PASS', 'FAIL', 'OBSERVATION') for r in results)
        # browser_case additionally verifies visible page, viewport, cookies and cleanup.
    asyncio.run(browser_case(site, check))
