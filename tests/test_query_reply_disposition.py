"""Only confirmed cancelled query replies may retire a vanished interception."""
import asyncio
from types import SimpleNamespace
import pytest
from app.qa.browser_guard import BrowserMutationGuard, SafetyShutdownError


@pytest.mark.parametrize('case', ['confirmed', 'open_owner', 'no_failure', 'other_failure',
                                'no_route', 'other_error', 'redirect_abort', 'mutation_abort',
                                'forward_failure', 'handler_cancelled'])
def test_terminal_query_reply_requires_all_proofs(case):
    async def check():
        commands = []
        class Session:
            async def send(self, method, params=None):
                commands.append(method)
                if method == 'Target.getTargets': return {'targetInfos': []}
                if method in ('Fetch.fulfillRequest','Fetch.failRequest'):
                    detail = 'Some unrelated error.' if case == 'other_error' else 'Invalid InterceptionId.'
                    raise RuntimeError(f'Protocol error ({method}): {detail}')
                return {}
            def remove_listener(self,*args): pass
            async def detach(self): pass
        context = SimpleNamespace(browser=SimpleNamespace(is_connected=lambda:True,remove_listener=lambda *args:None),
                                  remove_listener=lambda *args:None)
        guard = BrowserMutationGuard(context, [], 'https://fixture.test:443')
        guard.active = True
        guard.session = Session()
        guard.frames['frame'] = 'qa'
        guard.roots['frame'] = 'frame'
        event = {'requestId':'request','frameId':'frame','request':{
            'method':'POST','url':'https://fixture.test/graphql','headers':{'Content-Type':'application/json'},
            'postData':'{"query":"mutation { write }"}' if case=='mutation_abort' else '{"query":"{ read }"}'}}
        request = guard._request(event,'qa')
        route = SimpleNamespace(request=SimpleNamespace(
            frame=SimpleNamespace(page=SimpleNamespace(is_closed=lambda:case!='open_owner')),
            failure=None if case=='no_failure' else 'net::ERR_FAILED' if case=='other_failure' else 'net::ERR_ABORTED'))
        if case != 'no_route': guard.query_routes[guard._query_key('frame',request)] = [route]
        forwards = []
        entered = asyncio.Event()
        async def forward(request,route):
            forwards.append(True)
            if case == 'forward_failure': raise RuntimeError('Read forwarding failed')
            if case == 'handler_cancelled':
                entered.set()
                await asyncio.Event().wait()
            return SimpleNamespace(status=307 if case=='redirect_abort' else 200,headers={},body=b'{}')
        guard._forward_query = forward
        guard._paused(event)
        if case == 'handler_cancelled':
            await asyncio.wait_for(entered.wait(),1)
            for task in guard.tasks: task.cancel()
            guard.timeout = 0  # Immediately exercise unresolved-pending timeout.
        await asyncio.gather(*list(guard.tasks),return_exceptions=True)
        if case=='confirmed':
            assert not guard.pending and not guard.tasks and not guard.failed and not guard.errors
            await guard.stop()
            assert commands.count('Fetch.disable')==1 and not guard.active
        else:
            assert guard.pending=={'request'}
            assert guard.failed is (case != 'handler_cancelled')
            assert len(guard.errors)==(0 if case=='handler_cancelled' else 1)
            with pytest.raises(SafetyShutdownError,match='shutdown incomplete'):
                await guard.stop()
            assert 'Fetch.disable' not in commands and guard.active
            assert context._qa_safety_hold is guard
        assert len(forwards)==(0 if case=='mutation_abort' else 1)
    asyncio.run(check())
