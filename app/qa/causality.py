"""Provenance for rejected fetches that the active QA guard actually aborted.

No error text/time heuristic grants suppression. A browser rejection receives an
opaque receipt only after the Python guard confirms its frame/URL/method attempt.
Receipts expire and are scoped to a live document of a live QA-owned page/run.
"""
import json
import asyncio
import re
import time
import uuid

BINDING = '__qa_causal_event_v1'
SCRIPT = r"""(() => {
  const uuid = () => {
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    // randomUUID is secure-context-only; getRandomValues also works on HTTP.
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes,b => b.toString(16).padStart(2,'0')).join('');
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  };
  const install = owner => {
    if (window.__qaCausalOwner) return;
    Object.defineProperty(window, '__qaCausalOwner', {value: owner});
    const generation = uuid();
    Object.defineProperty(window, "__qaCausalGeneration", {value: generation});
    const send = data => window.__qa_causal_event_v1({...data, owner, generation, viewport:[innerWidth,innerHeight]});
    const ready = send({kind:'document'}).catch(() => false);
    const nativeFetch = window.fetch;
    window.fetch = async function(input, options) {
      let url, method;
      try {
        url = new URL(input instanceof Request ? input.url : String(input), location.href).href;
        method = String(options?.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
      } catch (_) { return nativeFetch.apply(this, arguments); }
      // Only unsafe methods can acquire a causal receipt. Genuine GET rejection
      // and genuine GraphQL/server errors remain entirely native.
      if (!['POST','PUT','PATCH','DELETE'].includes(method)) return nativeFetch.apply(this, arguments);
      const token = uuid();
      await ready;
      await send({kind:'attempt',token,url,method}).catch(() => false);
      const invocation = {['_qa_fetch_'+token](receiver,args) { return nativeFetch.apply(receiver,args); }};
      try { return await invocation['_qa_fetch_'+token](this, arguments); }
      catch (error) {
        const receipt = await send({kind:'rejected',token}).catch(() => null);
        if (receipt && error instanceof Error) {
          // Preserve name/message and the rejected promise. Attach provenance to
          // the same Error object, not to all errors near it in time.
          const marker = '\n[QA-BLOCK:' + receipt + ']';
          try {
            error.stack = String(error.stack || error) + marker;
            const originalToString = error.toString.bind(error);
            Object.defineProperty(error,'toString',{value:() => originalToString()+marker});
          } catch (_) {}
        }
        throw error;
      } finally { await send({kind:'finished',token}).catch(() => false); }
    };
  };
  window.__qaInstallCausality = install;
  try { if (opener && opener.__qaCausalOwner) install(opener.__qaCausalOwner); } catch (_) {}
})();"""


# Inspect handles in their original realm before JSON erases native Error values.
# Only enumerable data descriptors on plain containers are traversed. Accessors,
# cycles, exotic objects and bounded-out payloads fail closed as a whole argument.
CONSOLE_ARGUMENT_SCRIPT = r"""value => {
    const generation = window.__qaCausalGeneration || null;
    const stack = value instanceof Error ? String(value.stack) : "";
    const text = typeof value === "string" ? value : "";
    const nativeStackGetter = Object.getOwnPropertyDescriptor(new Error(), 'stack')?.get;
    let budget = 256;
    const seen = new Set();
    const walk = (v, depth) => {
        if (--budget < 0 || depth > 8) throw new Error('Inspection bound');
        if (v instanceof Error) {
            const descriptors = Object.getOwnPropertyDescriptors(v);
            const d=descriptors.stack;
            if (!d) throw new Error('Opaque Error');
            // V8 exposes Error.stack through its native accessor in this browser.
            // Never invoke an application-defined getter while inspecting a log.
            let errorStack;
            if ('value' in d) errorStack=d.value;
            else if (d.get && d.get === nativeStackGetter)
                errorStack=Reflect.apply(d.get,v,[]);
            else throw new Error('Opaque Error');
            // Additional enumerable Error fields are independent evidence.
            const entries = [];
            for (const [key,d] of Object.entries(descriptors)) {
                if (!d.enumerable) continue;
                if (!('value' in d)) throw new Error('Accessor');
                entries.push([key,walk(d.value,depth+1)]);
            }
            return {kind:'error',stack:String(errorStack),entries};
        }
        if (v === null || ['string','number','boolean'].includes(typeof v)) return {kind:'value',value:v};
        if (typeof v !== 'object') throw new Error('Opaque primitive');
        if (seen.has(v)) throw new Error('Cycle');
        seen.add(v);
        const array = Array.isArray(v);
        if (!array && Object.getPrototypeOf(v) !== Object.prototype && Object.getPrototypeOf(v) !== null)
            throw new Error('Exotic object');
        const descriptors = Object.getOwnPropertyDescriptors(v);
        const entries = [];
        for (const key of Reflect.ownKeys(descriptors)) {
            const d=descriptors[key];
            if (!d.enumerable) continue;
            if (typeof key !== 'string' || !('value' in d)) throw new Error('Accessor or symbol');
            entries.push([key,walk(d.value,depth+1)]);
        }
        seen.delete(v);
        return {kind:array?'array':'object',entries};
    };
    let tree=null, opaque="";
    if (!stack && typeof value !== 'string') {
        try { tree=walk(value,0); }
        catch (_) { opaque='Uninspectable non-error console argument: Object'; }
    }
    return {stack,text,opaque,tree,generation,viewport:[innerWidth,innerHeight]};
}"""


class BlockCausality:
    TTL = 30
    MAX_ENTRIES = 1024

    @staticmethod
    def clean_text(text):
        text = re.sub(r'\n?\[QA-BLOCK:[a-f0-9]{32}\]', '', text)
        return re.sub(r'_qa_fetch_[a-f0-9-]{36}', 'fetch (QA observation)', text)

    def __init__(self, engine, original_pages):
        self.engine = engine
        self.owner = uuid.uuid4().hex
        self.original_pages = original_pages
        self.documents = {}
        self.attempts = {}
        self.receipts = {}
        self.pages = set()
        self.network_sessions = {}
        self.binding_tasks = {}
        self._network_lock = asyncio.Lock()
        self.active = True
        self.enabled = False
        self.suppressed = 0

    async def attach(self, context):
        context._qa_causality = self
        # Provenance observation is optional; mutation protection is mandatory.
        # A context exposing only the guard API cannot grant any receipts.
        if not callable(getattr(context, 'expose_binding', None)):
            return
        if not getattr(context, '_qa_causality_binding_installed', False):
            async def dispatch(source, data):
                current = getattr(context, '_qa_causality', None)
                state = None
                if current and source['page'] not in current.original_pages:
                    if getattr(source['page'], '_qa_causal_closing', False):
                        raise asyncio.CancelledError()
                    task = asyncio.current_task()
                    state = dict(page=source['page'], resolving=False)
                    current.binding_tasks[task] = state
                    task.add_done_callback(lambda done: current.binding_tasks.pop(done, None))
                try:
                    result = await current.receive(source, data) if current else None
                except Exception:
                    if source['page'].is_closed():
                        raise asyncio.CancelledError()
                    raise
                # Playwright 1.42 tries to reject a failed binding resolution
                # in an untracked task. A dead document has no recipient; cancel
                # this binding callback rather than sending into a closed page.
                if source['page'].is_closed() or getattr(source['page'], '_qa_causal_closing', False):
                    raise asyncio.CancelledError()
                if state is not None:
                    state['resolving'] = True
                return result
            await context.expose_binding(BINDING, dispatch)
            context._qa_causality_binding_installed = True
        if not getattr(context, '_qa_causality_installed', False):
            await context.add_init_script(SCRIPT)
            context._qa_causality_installed = True
        self.enabled = True

    async def install(self, page):
        if not self.enabled:
            return
        self.watch(page)
        await page.add_init_script(SCRIPT + '\nwindow.__qaInstallCausality(' + json.dumps(self.owner) + ');')

    def watch(self, page):
        if page in self.pages:
            return
        self.pages.add(page)
        page.on('framenavigated', self.invalidate_frame)
        page.on('framedetached', self.invalidate_frame)
        page.on('close', lambda: self.drop_page(page))

    def invalidate_frame(self, frame, clear_requests=True):
        self.documents.pop(frame, None)
        self.attempts = {k:v for k,v in self.attempts.items() if v['frame'] is not frame}
        self.receipts = {k:v for k,v in self.receipts.items() if v['frame'] is not frame}
        if clear_requests and frame == frame.page.main_frame and hasattr(frame.page, 'qa_state'):
            frame.page.qa_state['blocked_requests'] = []

    def drop_page(self, page):
        for frame in list(self.documents):
            if frame.page is page:
                self.invalidate_frame(frame)
        self.network_sessions.pop(page, None)

    @staticmethod
    def _cancel_binding_task(task):
        # Only call for a tracked QA-owned binding. Retire its protocol reply
        # before cancelling the waiter, including CDP registration in receive().
        operation = task.get_coro()
        seen = set()
        while operation is not None and id(operation) not in seen:
            seen.add(id(operation))
            frame = getattr(operation, 'cr_frame', None)
            if (frame and frame.f_code.co_name == 'inner_send'
                    and frame.f_code.co_filename.replace('\\', '/').endswith('playwright/_impl/_connection.py')):
                callback = frame.f_locals.get('callback')
                future = getattr(callback, 'future', None)
                if isinstance(future, asyncio.Future):
                    if future.done() and not future.cancelled():
                        future.exception()
                    elif not future.done():
                        future.cancel()
            operation = getattr(operation, 'cr_await', None)
        task.cancel()

    async def finish_page_bindings(self, page):
        """Finish owned binding resolutions before destroying their recipient.

        Playwright 1.42 otherwise launches an untracked rejection into a closed
        target. Callbacks still computing a receipt can be cancelled safely;
        callbacks already resolving over the protocol must finish first.
        """
        page._qa_causal_closing = True
        tasks = [task for task,state in list(self.binding_tasks.items()) if state['page'] is page]
        for task in tasks:
            if not self.binding_tasks[task]['resolving']:
                self._cancel_binding_task(task)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=1)
            for task in pending:
                # A stuck renderer must not make desktop shutdown unbounded.
                # Retire this owned binding's protocol reply before cancelling
                # its waiter; Playwright 1.42 otherwise leaves a future behind.
                self._cancel_binding_task(task)
            await asyncio.gather(*tasks, return_exceptions=True)

    async def receive(self, source, data):
        if (self.active and isinstance(data, dict) and data.get('owner') == self.owner
                and data.get('kind') == 'document' and source['page'] not in self.original_pages):
            page = source['page']
            async with self._network_lock:
                if not self.active or page.is_closed():
                    return None
                if page not in self.network_sessions:
                    session = await page.context.new_cdp_session(page)
                    try:
                        session.on('Network.requestWillBeSent', lambda event: self.observed(page, event))
                        await session.send('Network.enable')
                        if not self.active or page.is_closed():
                            await session.detach()
                            return None
                        self.network_sessions[page] = session
                    except Exception:
                        try:
                            await session.detach()
                        except Exception:
                            pass
                        raise
        return self.event(source, data)

    def observed(self, page, event):
        if not self.active or event.get('type') != 'Fetch':
            return
        stack = event.get('initiator', {}).get('stack', {})
        names = []
        while stack:
            names.extend(frame.get('functionName', '') for frame in stack.get('callFrames', []))
            stack = stack.get('parent', {})
        # Only the nearest native fetch invocation proves this request. An
        # ancestor fetch can execute getters which issue a different request.
        token = next((name[len('_qa_fetch_'):] for name in names
                      if name.startswith('_qa_fetch_')), None)
        request = event['request']
        for key, attempt in self.attempts.items():
            if (key[2] == token and attempt['page'] is page and attempt['url'] == request['url']
                    and attempt['method'] == request['method']):
                attempt['observed'] = True

    def prune(self):
        now = time.monotonic()
        self.attempts = {k:v for k,v in self.attempts.items() if now-v['time'] <= self.TTL}
        self.receipts = {k:v for k,v in self.receipts.items() if now-v['time'] <= self.TTL}

    def event(self, source, data):
        if not self.active or not isinstance(data, dict) or data.get('owner') != self.owner:
            return None
        page, frame = source['page'], source['frame']
        if page in self.original_pages or page.is_closed():
            return None
        self.watch(page)
        self.prune()
        generation = data.get('generation')
        if data.get('kind') == 'document':
            # framenavigated already cleared the previous document's request
            # state. GETs can start while CDP registration is awaiting enable;
            # do not erase guard receipts already recorded for this document.
            self.invalidate_frame(frame, clear_requests=False)
            self.documents[frame] = generation
            return True
        if not generation or self.documents.get(frame) != generation:
            return None
        key = (frame, generation, data.get('token'))
        kind = data.get('kind')
        if kind == 'attempt' and len(self.attempts) < self.MAX_ENTRIES:
            self.attempts[key] = dict(frame=frame,page=page,generation=generation,
                                     url=data.get('url'),method=data.get('method'),viewport=data.get('viewport'),blocked=False,observed=False,time=time.monotonic())
        elif kind == 'rejected':
            attempt = self.attempts.get(key)
            if attempt and attempt['blocked'] and attempt['observed'] and len(self.receipts) < self.MAX_ENTRIES:
                receipt = uuid.uuid4().hex
                self.receipts[receipt] = dict(attempt, time=time.monotonic())
                return receipt
        elif kind == 'finished':
            self.attempts.pop(key, None)
        return None

    def blocked(self, request):
        self.prune()
        try:
            frame = request.frame
        except Exception:
            return
        for attempt in self.attempts.values():
            if (attempt['frame'] is frame and self.documents.get(frame) == attempt['generation']
                    and attempt['url'] == request.url and attempt['method'] == request.method.upper()):
                attempt['blocked'] = True

    def attributable(self, page, text, generation=None, viewport=None):
        if not self.active:
            return False
        self.prune()
        for marker in re.findall(r'\[QA-BLOCK:([a-f0-9]{32})\]', text):
            receipt = self.receipts.get(marker)
            if (receipt and receipt['page'] is page and not page.is_closed()
                    and self.documents.get(receipt['frame']) == receipt['generation']
                    and (generation is None or generation == receipt['generation'])
                    and (viewport is None or viewport == receipt.get('viewport'))):
                self.suppressed += 1
                return True
        return False

    def filter_console_tree(self, page, node, generation, viewport=None):
        """Remove only proven nested Errors; preserve unproven empty containers.

        The handle's realm generation must match the receipt's exact frame/document.
        Page identity additionally isolates targets/viewports; owner, expiry and
        navigation validity are checked by attributable before any leaf is removed.
        """
        kind = node['kind']
        if kind == 'value':
            return node['value'], False
        removed = False
        values = {}
        for key, child in node.get('entries', []):
            value, child_removed = self.filter_console_tree(page, child, generation, viewport)
            removed = removed or child_removed
            if not (value is None and child_removed):
                values[key] = value
        if kind == 'error':
            proven = bool(generation) and self.attributable(page, node['stack'], generation, viewport)
            if not proven:
                values['error'] = self.clean_text(node['stack'])
            removed = removed or proven
        if removed and not values:
            return None, True
        # Array values retain order; independent named properties are not discarded.
        if kind == 'array' and all(key.isdigit() for key in values):
            return list(values.values()), removed
        return values, removed

    def close(self, context):
        self.active = False
        self.documents.clear()
        self.attempts.clear()
        self.receipts.clear()
        for page in self.pages:
            page.remove_listener('framenavigated', self.invalidate_frame)
            page.remove_listener('framedetached', self.invalidate_frame)
        self.pages.clear()
        self.network_sessions.clear()
        if getattr(context, '_qa_causality', None) is self:
            context._qa_causality = None
