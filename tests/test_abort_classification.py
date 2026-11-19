"""Local Current Page cancellation evidence must not invent customer defects."""
import asyncio
import pytest
from test_full_site import site, isolated_state, browser_case
from app.qa.engine import SmartSmokeQA


@pytest.mark.parametrize('kind', ['browser_abort', 'qa_stop', 'transport_failure', 'http_failure', 'abort_with_dom_failure', 'abort_with_js_failure'])
def test_current_page_abort_classification(site, kind):
    site.links['/'] = []
    site.script = '''<script>
      window.pendingController = new AbortController();
      fetch('/community?_rsc=local', {signal:pendingController.signal}).catch(()=>{});
    </script>'''
    async def check(context, user):
        routes = asyncio.Queue()
        failures = []
        async def held(route):
            await routes.put(route)
        await context.route('**/community?*', held)
        class Probe(SmartSmokeQA):
            async def _navigate_page(self, page, url):
                response = await super()._navigate_page(page, url)
                route = await asyncio.wait_for(routes.get(), 3)
                if kind == 'http_failure':
                    await route.fulfill(status=503, body='unavailable')
                    return response
                async with page.expect_event('requestfailed', predicate=lambda r: '/community?' in r.url) as failure:
                    if kind == 'qa_stop':
                        session = await context.new_cdp_session(page)
                        try: await session.send('Page.stopLoading')
                        finally: await session.detach()
                    elif kind == 'transport_failure':
                        await route.abort('connectionfailed')
                    else:
                        await page.evaluate('pendingController.abort()')
                failures.append((await failure.value).failure)
                if kind != 'transport_failure':
                    try: await route.abort('aborted')
                    except Exception:
                        if not page.is_closed():
                            # Chromium may have already disposed the cancelled route.
                            pass
                if kind == 'abort_with_dom_failure':
                    await page.evaluate("document.querySelector('label').remove()")
                if kind == 'abort_with_js_failure':
                    await page.evaluate("console.error('Independent profile rendering failure')")
                return response
        results = await Probe(site.base+'/', 1, 1, mode='current', context=context).run()
        customer = [r for r in results if r['status'] in ('FAIL','OBSERVATION')]
        if kind in ('browser_abort', 'qa_stop'):
            assert not customer, results
            assert sum(r['status']=='PASS' for r in results) == 2
        elif kind == 'abort_with_dom_failure':
            assert len(customer) == 2 and all(r['title']=='Missing form label' for r in customer)
        elif kind == 'abort_with_js_failure':
            assert len(customer) == 2 and all('Independent profile rendering failure' in r['actual_result'] for r in customer)
        else:
            assert len(customer) == 2, results
            assert all('503' in r['actual_result'] or 'ERR_CONNECTION_FAILED' in r['actual_result'] for r in customer)
        if kind not in ('transport_failure','http_failure'):
            assert failures == ['net::ERR_ABORTED']*2
            diagnostics = [r for r in results if r['status']=='DIAGNOSTIC' and 'ERR_ABORTED' in r['actual_result']]
            assert len(diagnostics)==2 and all(r['finding_id'] is None for r in diagnostics)
        assert not any(r['status'] in ('SAFETY ACTION','LIMITED COVERAGE') for r in results)
        assert site.mutations == 0
    asyncio.run(browser_case(site, check))
