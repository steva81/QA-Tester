"""Real-browser stringification before console collection, not native handles."""
import asyncio
import pytest
from test_full_site import site, isolated_state, browser_case, engine

@pytest.mark.parametrize('conversion', ['String(e)','e.stack','e.stack.replace(/\\n?\\[QA-BLOCK:[a-f0-9]{32}\\]/g,"")','JSON.parse(JSON.stringify({error:{error:String(e)}})).error.error'])
def test_serialized_exact_live_shape(site,conversion):
    site.links['/']=[]
    site.script='<script>fetch("/api/proxy/graphql",{method:"POST"}).catch(e=>console.error({error:{error:'+conversion+'}}));</script>'
    async def check(context,user):
        qa=engine(site,context);results=await qa.run()
        assert site.mutations==0 and any(r['status']=='SAFETY ACTION' for r in results)
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2,results
        assert not any(r['status']=='PASS' for r in results),results
        assert not any(r['status']=='OBSERVATION' for r in results),results
    asyncio.run(browser_case(site,check))

@pytest.mark.parametrize('payload,expected', [
    ('{error:{error:s},code:123}', '123'),
    ('{error:{error:s},message:"genuine problem"}', 'genuine problem'),
    ('{error:{error:s},object:{message:"GENUINE_OBJECT"}}', 'GENUINE_OBJECT'),
    ('{error:{error:s},other:new Error("GENUINE_MANUAL_FIXTURE_ERROR")}', 'GENUINE_MANUAL_FIXTURE_ERROR'),
    ('{error:{error:s+" GENUINE_SUFFIX"}}', 'GENUINE_SUFFIX'),
])
def test_serialized_genuine_siblings_remain(site,payload,expected):
    site.links['/']=[]
    site.script='<script>fetch("/api/proxy/graphql",{method:"POST"}).catch(e=>{const s=String(e);console.error('+payload+')});</script>'
    async def check(context,user):
        results=await engine(site,context).run();obs=[r for r in results if r['status']=='DIAGNOSTIC' and r['actual_result'].startswith('Console/JS errors detected:')]
        assert len(obs)==2 and all(expected in r['actual_result'] for r in obs),results
        assert site.mutations==0 and any(r['status']=='SAFETY ACTION' for r in results)
        assert all('QA-BLOCK' not in r['actual_result'] for r in obs)
    asyncio.run(browser_case(site,check))

@pytest.mark.parametrize('genuine', [
    'console.error({error:{error:"TypeError: Failed to fetch at fetch (QA observation) at window.fetch"}})',
    'console.error({error:{error:"TypeError: Failed to fetch [QA-BLOCK:00000000000000000000000000000000]"}})',
    'console.error({error:{error:"TypeError: Failed to fetch at _qa_fetch_00000000-0000-4000-8000-000000000000"}})',
    'console.error({error:{error:"GENUINE_SERVER_GRAPHQL_FAILURE"}})',
    'fetch("/blocked-get.js").catch(()=>console.error({error:{error:"GENUINE_FAILED_GET"}}))',
    'console.error(new Error("GENUINE_MANUAL_FIXTURE_ERROR"))',
])
def test_serialized_unproven_errors_remain(site,genuine):
    site.links['/']=[]
    site.script='<script>'+genuine+'</script>'
    async def check(context,user):
        if 'blocked-get' in genuine: await context.route('**/blocked-get.js',lambda route:route.abort())
        results=await engine(site,context).run()
        assert sum(r['status']=='OBSERVATION' for r in results)==2,results
        assert not any(r['status']=='SAFETY ACTION' for r in results)
    asyncio.run(browser_case(site,check))

@pytest.mark.parametrize('mode', ['delayed','many'])
def test_serialized_delayed_and_multiple_posts(site,mode):
    site.links['/']=[]
    emit='setTimeout(()=>console.error({error:{error:String(e)}}),150)' if mode=='delayed' else 'console.error({error:{error:e.stack}})'
    site.script='<script>for(let i=0;i<'+('1' if mode=='delayed' else '5')+';i++)fetch("/api/proxy/graphql",{method:"POST"}).catch(e=>{'+emit+'});</script>'
    async def check(context,user):
        results=await engine(site,context).run()
        assert site.mutations==0 and any(r['status']=='SAFETY ACTION' for r in results)
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2,results
        assert not any(r['status']=='PASS' for r in results),results
        assert not any(r['status']=='OBSERVATION' for r in results),results
    asyncio.run(browser_case(site,check))


@pytest.mark.parametrize('boundary', ['viewport','page','run'])
def test_serialized_receipt_replay_is_rejected(site,boundary):
    site.links['/']=[]
    block="fetch('/api/proxy/graphql',{method:'POST'}).catch(e=>{localStorage.setItem('serialized',e.stack);console.error({error:{error:e.stack}})})"
    replay="console.error({error:{error:localStorage.getItem('serialized')}})"
    site.script='<script>'+('if(innerWidth===1366){'+block+'}else if(innerWidth===390){'+replay+'}' if boundary=='viewport' else block)+'</script>'
    if boundary=='page':
        site.links['/']=[('/b','B')];site.links['/b']=[];site.scripts['/b']='<script>'+replay+'</script>'
    async def check(context,user):
        first=engine(site,context);results=await first.run()
        if boundary=='run':
            assert not any(r['status']=='OBSERVATION' for r in results),results
            site.script='<script>'+replay+'</script>'
            second=engine(site,context);results=await second.run()
            assert first._causality.owner!=second._causality.owner
        obs=[r for r in results if r['status']=='OBSERVATION']
        assert len(obs)==(1 if boundary=='viewport' else 2),results
        assert all('Failed to fetch' in r['actual_result'] for r in obs)
        assert all('QA-BLOCK' not in r['actual_result'] and '_qa_fetch_' not in r['actual_result'] for r in obs)
        if boundary=='viewport': assert 'Mobile' in obs[0]['viewport']
        if boundary=='page': assert all(r['url']==site.base+'/b' for r in obs)
        assert site.mutations==0
    asyncio.run(browser_case(site,check))

@pytest.mark.parametrize('boundary', ['expiry','navigation','frame','viewport_resize'])
def test_serialized_receipt_live_boundaries(site,boundary):
    from app.qa.causality import CONSOLE_ARGUMENT_SCRIPT
    from app.qa.full_site import FullSiteQA
    class Boundaries(FullSiteQA):
        async def _execute_queue(self):
            page=await self._new_qa_page(self.provided_context)
            try:
                await self._set_qa_viewport(page,self.viewports[0])
                await self._navigate_page(page,site.base+'/')
                handle=await page.evaluate_handle("async()=>{try{await fetch('/api/proxy/graphql',{method:'POST'})}catch(e){return {error:{error:String(e)}}}}")
                evidence=await handle.evaluate(CONSOLE_ARGUMENT_SCRIPT)
                remaining,removed=self._causality.filter_console_tree(page,evidence['tree'],evidence['generation'],evidence['viewport'])
                assert not removed and remaining is not None
                serialized=evidence['tree']['entries'][0][1]['entries'][0][1]['value']
                assert self._causality.attributable(page,serialized,evidence['generation'],evidence['viewport'])
                if boundary=='expiry':
                    for receipt in self._causality.receipts.values():receipt['time']-=self._causality.TTL+1
                elif boundary=='navigation':
                    await self._navigate_page(page,site.base+'/b')
                elif boundary=='viewport_resize':
                    await self._set_qa_viewport(page,self.viewports[1]);evidence=await handle.evaluate(CONSOLE_ARGUMENT_SCRIPT)
                else:
                    await page.evaluate("()=>{const f=document.createElement('iframe');f.src='/b';document.body.append(f)}")
                    frame=page.frames[-1];await frame.wait_for_url(site.base+'/b')
                    serialized=evidence['tree']['entries'][0][1]['entries'][0][1]['value']
                    other=await frame.evaluate_handle("s=>({error:{error:s}})",serialized)
                    evidence=await other.evaluate(CONSOLE_ARGUMENT_SCRIPT)
                remaining,removed=self._causality.filter_console_tree(page,evidence['tree'],evidence['generation'],evidence['viewport'])
                assert not removed and 'Failed to fetch' in str(remaining)
                assert not self._causality.attributable(page,serialized,evidence['generation'],evidence['viewport'])
                assert site.mutations==0
            finally: await self._close_tree(page)
    async def check(context,user):
        qa=Boundaries(site.base+'/',1,1,context=context,settle_timeout=600)
        await qa.run();assert not qa._causality.active and not qa._causality.receipts
    asyncio.run(browser_case(site,check))


def test_serialized_render_shape_can_also_be_native_error_without_appended_marker(site):
    site.links['/']=[]
    site.script=r"<script>fetch('/api/proxy/graphql',{method:'POST'}).catch(e=>{e.stack=e.stack.replace(/\n?\[QA-BLOCK:[a-f0-9]{32}\]/g,'');console.error({error:e})});</script>"
    async def check(context,user):
        results=await engine(site,context).run()
        assert site.mutations==0 and any(r['status']=='SAFETY ACTION' for r in results)
        assert sum(r['status']=='LIMITED COVERAGE' for r in results)==2,results
        assert not any(r['status']=='PASS' for r in results),results
        assert not any(r['status']=='OBSERVATION' for r in results),results
    asyncio.run(browser_case(site,check))
