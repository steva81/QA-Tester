"""Local architecture experiment, NOT a production GraphQL parser/policy.

Exact fixture documents form a trusted oracle. Never allows any external origin.
No app imports, DB access, real login, or dependency changes.
"""
import asyncio
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
from playwright.async_api import async_playwright

QUERY='query Viewer { viewer { id } }'
SHORT='{ viewer { id } }'
MULTI='query Viewer { viewer { id } } query Jobs { jobs { id } }'
MIXED='query Viewer { viewer { id } } mutation Update { update { id } }'
MUTATION='mutation Update { update { id } }'
HTTP_ERROR='query Error { error { id } }'
BAD_READ='query BadRead { counter { id } }'
# Fixture-only exact document/selection registry; no lexical query detection.
READS={(QUERY,None),(SHORT,None),(MULTI,'Viewer'),(MULTI,'Jobs'),(HTTP_ERROR,None),(BAD_READ,None)}
CASES=[
 ('query_required',{'query':QUERY},True),
 ('mutation',{'query':MUTATION},False),
 ('shorthand',{'query':SHORT},True),
 ('multiple_missing_name',{'query':MULTI},False),
 ('multiple_selected_query',{'query':MULTI,'operationName':'Jobs'},True),
 ('selected_mutation',{'query':MIXED,'operationName':'Update'},False),
 ('mixed_batch',[{'query':QUERY},{'query':MUTATION}],False),
 ('malformed','not JSON',False),
 ('non_graphql',{'action':'delete'},False),
 ('persisted_only',{'extensions':{'persistedQuery':{'version':1,'sha256Hash':'0'*64}}},False),
 ('http_error',{'query':HTTP_ERROR},True),
]

async def run():
    counts=Counter();metadata=[]; rows=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path=='/slow':time.sleep(.3)
            self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers()
            try:self.wfile.write(b'<title>GraphQL fixture</title><div id="content">WAITING</div>')
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            query=payload.get('query') if isinstance(payload,dict) else None
            counts['post']+=1
            if query in (MUTATION,MIXED): counts['mutation']+=1
            if query==BAD_READ:counts['query_side_effect']+=1
            status=500 if query==HTTP_ERROR else 200
            body=json.dumps({'data':{'viewer':{'id':'LOCAL_ONLY'}}}).encode()
            self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,name='GraphQLFixture');thread.start()
    origin='http://127.0.0.1:'+str(server.server_port)
    try:
        async with async_playwright() as p:
            browser=await p.chromium.launch()
            try:
                for policy in ('block_all','fixture_trusted_registry'):
                    context=await browser.new_context(service_workers='block')
                    blocks=[]
                    async def guard(route):
                        request=route.request
                        if not request.url.startswith(origin+'/'):
                            await route.abort();return
                        if request.method in ('POST','PUT','PATCH','DELETE'):
                            body=request.post_data_buffer
                            content_type=await request.header_value('content-type')
                            try:payload=json.loads(body) if body else None
                            except (ValueError,UnicodeError):payload=None
                            try:frame=request.frame;owner=frame.page
                            except Exception:frame=owner=None
                            metadata.append(dict(method=request.method,content_type=content_type,body_available=body is not None,
                                page_owned=owner is not None,frame_owned=frame is not None,viewport=owner.viewport_size if owner else None,
                                query_available=isinstance(payload,dict) and isinstance(payload.get('query'),str)))
                            allow=(policy=='fixture_trusted_registry' and request.method=='POST'
                                and request.url==origin+'/api/proxy/graphql' and content_type=='application/json'
                                and isinstance(payload,dict) and set(payload)<= {'query','operationName','variables'}
                                and (payload.get('query'),payload.get('operationName')) in READS)
                            if not allow:
                                blocks.append(request.url);await route.abort();return
                        await route.continue_()
                    await context.route('**/*',guard)
                    try:
                        for name,payload,known_read in CASES:
                            for width,height in ((1366,768),(390,844)):
                                page=await context.new_page()
                                await page.set_viewport_size({'width':width,'height':height});await page.goto(origin+'/')
                                before=len(blocks);writes=counts['mutation']
                                result=await page.evaluate("""async payload=>{try{const r=await fetch('/api/proxy/graphql',{method:'POST',headers:{'Content-Type':'application/json'},body:typeof payload==='string'?payload:JSON.stringify(payload)});if(!r.ok)throw new Error('GENUINE_HTTP_'+r.status);const data=await r.json();document.querySelector('#content').textContent=data.data.viewer.id;return {content:document.querySelector('#content').textContent}}catch(e){return {error:String(e),content:document.querySelector('#content').textContent}}}""",payload)
                                expected=policy=='fixture_trusted_registry' and known_read
                                assert (len(blocks)==before)==expected,(name,policy,result)
                                assert counts['mutation']==writes
                                if expected and name!='http_error':assert result['content']=='LOCAL_ONLY'
                                else:assert result['content']=='WAITING'
                                if expected and name=='http_error':assert result['error']=='Error: GENUINE_HTTP_500'
                                rows.append(dict(policy=policy,case=name,viewport=[width,height],blocked=not expected,**result))
                                await page.close()
                        # Independent errors remain evidence in a safety-limited page.
                        page=await context.new_page();await page.goto(origin+'/');errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        await page.evaluate("()=>setTimeout(()=>{throw new Error('GENUINE_INDEPENDENT_JS')},0)")
                        await page.wait_for_timeout(100);assert any('GENUINE_INDEPENDENT_JS' in e for e in errors)
                        rows.append(dict(policy=policy,case='independent_js',evidence=errors))
                        # Query/mutation coexistence: only the locally trusted query may succeed.
                        results=await page.evaluate("""async docs=>await Promise.all(docs.map(async query=>{try{await fetch('/api/proxy/graphql',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query})});return 'loaded'}catch(e){return 'blocked'}}))""",[QUERY,MUTATION])
                        assert results==(['loaded','blocked'] if policy=='fixture_trusted_registry' else ['blocked','blocked'])
                        rows.append(dict(policy=policy,case='mutation_with_query',results=results))
                        # Demonstrate semantic risk: a query resolver may write despite its type.
                        if policy=='fixture_trusted_registry':
                            before=counts['query_side_effect']
                            await page.evaluate("""query=>fetch('/api/proxy/graphql',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query})})""",BAD_READ)
                            assert counts['query_side_effect']==before+1
                            rows.append(dict(policy=policy,case='query_side_effect_counterexample',writes=1))
                        task=asyncio.create_task(page.evaluate("()=>fetch('/slow')"));await asyncio.sleep(.05);task.cancel()
                        await page.close();await asyncio.gather(task,return_exceptions=True);assert task.cancelled()
                        rows.append(dict(policy=policy,case='cancellation',cancelled=True))
                        loss_page=await context.new_page();await loss_page.goto(origin+'/')
                        lost=asyncio.create_task(loss_page.evaluate("()=>fetch('/slow')"));await asyncio.sleep(.05)
                        await context.close();outcome=await asyncio.gather(lost,return_exceptions=True)
                        assert isinstance(outcome[0],Exception) and not context.pages
                        rows.append(dict(policy=policy,case='session_loss',pages=0,request_interrupted=True))
                    finally:
                        await context.close()
                assert counts['mutation']==0
            finally:await browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5);assert not thread.is_alive()
    output=dict(rows=rows,metadata_samples=metadata,total_asserted_rows=len(rows),mutation_writes=counts['mutation'],semantic_counterexample_writes=counts['query_side_effect'],live_threads=[t.name for t in threading.enumerate()])
    Path('scratch/graphql_architecture_probe.json').write_text(json.dumps(output,indent=2),encoding='utf-8')
    print(json.dumps({key:output[key] for key in ('total_asserted_rows','mutation_writes','semantic_counterexample_writes','live_threads')},indent=2))

if __name__=='__main__':asyncio.run(run())
