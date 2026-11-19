"""Sanitized Apollo metadata regression; all browser traffic is localhost."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from test_full_site import site, isolated_state, browser_case
from app.qa.engine import SmartSmokeQA
from app.qa.browser_guard import BrowserMutationGuard
from app.qa.graphql_policy import is_readonly_graphql_post
from playwright.async_api import Route

LIVE_SHAPE = {
    'operationName': 'getTalkjsToken', 'variables': {},
    'extensions': {'clientLibrary': {'name': '@apollo/client', 'version': '4.1.7'}},
    'query': 'query getTalkjsToken {\n  talkjsToken {\n    token\n    __typename\n  }\n}',
}


def allowed(payload, **changes):
    values = dict(method='POST', url='https://example.test/api/proxy/graphql',
                  headers={'content-type': 'application/json'},
                  frame=SimpleNamespace(page=object()),
                  post_data_buffer=json.dumps(payload, separators=(',', ':')).encode())
    values.update(changes)
    return is_readonly_graphql_post(SimpleNamespace(**values), 'https://example.test:443')


def test_exact_sanitized_apollo_shape():
    assert len(json.dumps(LIVE_SHAPE, separators=(',', ':')).encode()) == 209
    assert allowed(LIVE_SHAPE)
    assert allowed({k: v for k, v in LIVE_SHAPE.items() if k != 'extensions'})


@pytest.mark.parametrize('extensions', [
    None, [], '', 1, True,
    {'persistedQuery': {'sha256Hash': '0' * 64}},
    {'clientLibrary': {'name': 'client', 'version': '1'}, 'persistedQuery': {}},
    {'clientLibrary': {'name': 'client', 'version': '1'}, 'unknown': {}},
    {'clientLibrary': {'name': 'client', 'version': '1', 'hash': 'abc'}},
    {'unknown': {}},
    *[{'clientLibrary': v} for v in (None, [], '', 1, True, {})],
    *[{'clientLibrary': {key: value, other: 'valid'}}
      for key, other in [('name', 'version'), ('version', 'name')]
      for value in (None, [], {}, 1, True, '', ' ', ' leading', 'trailing ', 'a\nb', 'a\x00b', 'a' * 129)],
    {'clientLibrary': {'name': 'client'}}, {'clientLibrary': {'version': '1'}},
])
def test_invalid_metadata_denied(extensions):
    assert not allowed({**LIVE_SHAPE, 'extensions': extensions})


def test_metadata_bounds_and_no_name_allowlist():
    payload = {**LIVE_SHAPE, 'operationName': 'Other', 'query': 'query Other { field }',
               'extensions': {'clientLibrary': {'name': 'x' * 128, 'version': 'v' * 128}}}
    assert allowed(payload)


@pytest.mark.parametrize('document,name', [
    ('mutation getTalkjsToken { write }', 'getTalkjsToken'),
    ('subscription getTalkjsToken { feed }', 'getTalkjsToken'),
    ('query A { a } query B { b }', None),
    ('query getTalkjsToken {', 'getTalkjsToken'),
    ('query Other { field }', 'getTalkjsToken'),
])
def test_metadata_does_not_override_ast(document, name):
    assert not allowed({**LIVE_SHAPE, 'query': document, 'operationName': name})


@pytest.mark.parametrize('changes', [
    {'url': 'https://other.test/graphql'}, {'headers': {'content-type': 'text/plain'}},
    {'frame': SimpleNamespace(page=None)}, {'post_data_buffer': None},
])
def test_metadata_does_not_override_transport(changes):
    assert not allowed(LIVE_SHAPE, **changes)


@pytest.mark.parametrize('status,mutation', [(200, False), (303, False), (307, False), (200, True)])
def test_apollo_context_cdp_exactly_once(site, monkeypatch, status, mutation):
    payload = copy.deepcopy(LIVE_SHAPE)
    if mutation:
        payload['query'] = 'mutation getTalkjsToken { write }'
    received, forwards, redirect_limits = [], [], []
    def post(handler):
        body = handler.rfile.read(int(handler.headers['Content-Length']))
        received.append(json.loads(body))
        if received[-1] != LIVE_SHAPE:
            site.mutations += 1
        data = b'{"data":{"talkjsToken":{"token":"local","__typename":"Token"}}}'
        handler.send_response(status)
        if status != 200:
            handler.send_header('Location', '/redirect-destination')
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    monkeypatch.setattr(site.server.RequestHandlerClass, 'do_POST', post)
    site.links['/'] = []
    site.script = '''<script>fetch('/api/proxy/graphql', {method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(%s)})
      .then(r=>r.json()).catch(()=>{}).finally(()=>window.apolloDone=true);</script>''' % json.dumps(payload)
    forward = BrowserMutationGuard._forward_query
    async def tracked(self, request, route):
        assert route is not None, 'Context route must delegate to browser CDP'
        forwards.append(json.loads(request.post_data_buffer))
        return await forward(self, request, route)
    monkeypatch.setattr(BrowserMutationGuard, '_forward_query', tracked)
    fetch = Route.fetch
    async def fetched(self, **kwargs):
        redirect_limits.append(kwargs.get('max_redirects'))
        return await fetch(self, **kwargs)
    monkeypatch.setattr(Route, 'fetch', fetched)
    async def check(context, user):
        qa = SmartSmokeQA(site.base + '/', 1, 1, mode='current', context=context)
        navigate = qa._navigate_page
        async def loaded(page, url):
            response = await navigate(page, url)
            await page.wait_for_function('window.apolloDone === true', timeout=3000)
            return response
        monkeypatch.setattr(qa, '_navigate_page', loaded)
        results = await qa.run()
        expected = [] if mutation else [LIVE_SHAPE] * 2
        assert received == expected
        assert forwards == expected
        assert redirect_limits == ([] if mutation else [0, 0])
        assert site.mutations == 0 and site.counts['/redirect-destination'] == 0
        assert sum(r['status'] == 'SAFETY ACTION' for r in results) == (0 if status == 200 and not mutation else 2)
    asyncio.run(browser_case(site, check))
