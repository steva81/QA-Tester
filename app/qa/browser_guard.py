"""Browser-owned interception survives the destruction of an automated target."""
import asyncio
import base64
import time
from types import SimpleNamespace

from app.qa.graphql_policy import is_readonly_graphql_post


class SafetyShutdownError(RuntimeError):
    pass


class BrowserMutationGuard:
    timeout = 5.0

    def __init__(self, context, original_pages, origin, navigation_reason=None, on_block=None, mutation_barrier=None):
        self.context = context
        self.original_pages = original_pages
        self.origin = origin
        self.navigation_reason = navigation_reason
        self.on_block = on_block
        self.mutation_barrier = mutation_barrier
        self.session = None
        self.context_id = None
        self.frames = {}
        self.roots = {}
        self.page_roots = {}
        self.query_routes = {}
        self.targets = {}
        self.page_sessions = {}
        self.page_owners = {}
        self.observing = {}
        self.ownership_ready = {}
        self.tasks = set()
        self.pending = set()
        self.failed = False
        self.errors = []
        self.active = False
        self.changed = asyncio.Event()

    def _task(self, operation):
        task = asyncio.create_task(operation)
        self.tasks.add(task)
        def finished(result):
            self.tasks.discard(result)
            if not result.cancelled() and result.exception() is not None:
                self.failed = True
                self.errors.append(result.exception())
            self.changed.set()
        task.add_done_callback(finished)
        return task

    def _target(self, event):
        info = event['targetInfo']
        self.targets[info['targetId']] = info
        if info.get('type') == 'page':
            self.roots[info['targetId']] = info['targetId']
        if self.context_id is not None:
            owner = 'unknown' if not info.get('browserContextId') else (
                'qa' if info['browserContextId'] == self.context_id else 'foreign')
            if self.frames.get(info.get('openerId')) == 'manual':
                owner = 'manual'
            self.frames.setdefault(info['targetId'], owner)

    def _frame_tree(self, tree, owner, root):
        self.frames[tree['frame']['id']] = owner
        self.roots[tree['frame']['id']] = root
        for child in tree.get('childFrames', []):
            self._frame_tree(child, owner, root)

    async def _observe(self, page):
        if page.is_closed():
            return
        owner = 'manual' if page in self.original_pages else 'qa'
        try:
            session = await self.context.new_cdp_session(page)
            self.page_sessions[page] = session
            info = (await session.send('Target.getTargetInfo'))['targetInfo']
            if self.frames.get(info.get('openerId')) == 'manual':
                owner = 'manual'
            self.page_owners[page] = owner
            self.context_id = info.get('browserContextId')
            self.targets[info['targetId']] = info
            self.frames[info['targetId']] = owner
            root = info['targetId']
            self.page_roots[page] = root
            self.roots[root] = root
            self.ownership_ready[page].set()
            def frame(frame_id):
                self.frames[frame_id] = owner
                self.roots[frame_id] = root
            # Keep frame ownership after frame removal and target detach.
            session.on('Page.frameAttached', lambda event: frame(event['frameId']))
            session.on('Page.frameNavigated', lambda event: frame(event['frame']['id']))
            await session.send('Page.enable')
            await session.send('Network.enable', {'maxPostDataSize': 1048576})
            self._frame_tree((await session.send('Page.getFrameTree'))['frameTree'], owner, root)
        except Exception:
            if not page.is_closed():
                raise

    def _page(self, page):
        if page not in self.observing:
            self.ownership_ready[page] = asyncio.Event()
            self.observing[page] = self._task(self._observe(page))
            self.observing[page].add_done_callback(lambda _: self.ownership_ready[page].set())

    async def observe(self, page, ownership_only=False):
        self._page(page)
        if ownership_only:
            # Page.enable may await a navigation held by the context route.
            # Target identity is available before renderer/frame observation.
            await self.ownership_ready[page].wait()
            if not self.observing[page].done():
                return
        await asyncio.shield(self.observing[page])

    def is_manual(self, page):
        return page in self.original_pages or self.page_owners.get(page) == 'manual'

    async def start(self):
        self.session = await self.context.browser.new_browser_cdp_session()
        self.session.on('Target.targetCreated', self._target)
        self.session.on('Target.targetInfoChanged', self._target)
        await self.session.send('Target.setDiscoverTargets', {'discover': True})
        self.context.on('page', self._page)
        # An otherwise empty owned context needs an inert target to establish
        # its CDP context ID; it is not a manual page and never navigates.
        seed = None
        if not self.context.pages:
            seed = await self.context.new_page()
        for page in list(self.context.pages):
            await self.observe(page)
        for info in (await self.session.send('Target.getTargets'))['targetInfos']:
            self._target({'targetInfo': info})
        self.session.on('Fetch.requestPaused', self._paused)
        self.context.browser.on('disconnected', self._disconnected)
        self.active = True
        if self.mutation_barrier:
            self.mutation_barrier.browser_upload_guard = self
        await self.session.send('Fetch.enable', {'patterns': [{'requestStage': 'Request'}]})
        if seed is not None:
            await seed.close()

    def _paused(self, event):
        self.pending.add(event['requestId'])
        self._task(self._handle(event))

    async def _owner(self, frame_id):
        if frame_id not in self.frames:
            # Main-frame IDs are target IDs. The discovery listener normally
            # registers them first; a protocol snapshot resolves event ordering.
            for info in (await self.session.send('Target.getTargets'))['targetInfos']:
                self._target({'targetInfo': info})
        return self.frames.get(frame_id, 'unknown')

    def _request(self, event, owner):
        request = event['request']
        body = None
        entries = request.get('postDataEntries')
        if entries and all('bytes' in entry for entry in entries):
            body = b''.join(base64.b64decode(entry['bytes'], validate=True) for entry in entries)
        elif isinstance(request.get('postData'), str):
            body = request['postData'].encode('utf-8', errors='strict')
        return SimpleNamespace(method=request['method'], url=request['url'],
            headers={key.lower(): value for key, value in request.get('headers', {}).items()},
            post_data_buffer=body,
            frame=SimpleNamespace(page=self if owner == 'qa' and event.get('frameId') in self.roots else None))

    def _query_key(self, root, request):
        return (root, request.url, request.post_data_buffer,
                tuple(sorted((name.lower(), value) for name, value in request.headers.items())))

    def register_query_route(self, route):
        root = self.page_roots.get(route.request.frame.page)
        if root is None:
            raise SafetyShutdownError('GraphQL request ownership is unavailable.')
        self.query_routes.setdefault(self._query_key(root, route.request), []).append(route)

    async def _forward_query(self, request, route):
        response = None
        try:
            if route is not None:
                response = await route.fetch(headers=request.headers,
                    post_data=request.post_data_buffer, max_redirects=0)
            else:
                response = await self.context.request.fetch(request.url, method='POST',
                    headers=request.headers, data=request.post_data_buffer, max_redirects=0)
            body = await response.body()
            headers = {key: value for key, value in response.headers.items()
                       if key.lower() not in ('content-encoding', 'transfer-encoding', 'content-length')}
            headers['content-length'] = str(len(body))
            return SimpleNamespace(status=response.status, headers=headers, body=body)
        finally:
            if response is not None:
                await response.dispose()

    async def _continue(self, request_id):
        try:
            await self.session.send('Fetch.continueRequest', {'requestId': request_id})
        except Exception as error:
            # A cancelled/fulfilled *permitted* resource no longer has a pause.
            # Never apply this exception to a destructive denial or query reply.
            if 'Invalid InterceptionId' not in str(error):
                raise

    # Bounded scope: small, single-file text uploads with a declared exact destination.
    UPLOAD_CDP_MAX_FILE = 65536
    UPLOAD_CDP_MAX_BODY = 131072

    async def _upload_body(self, page, network_id, permit):
        """Return the exact outgoing upload body via the page's CDP Network domain, or None.

        Playwright omits file bodies. Chromium exposes the multipart body of in-memory
        files by Network request ID; disk-file bytes are omitted (empty file part), which
        the barrier rejects. None means not verifiable and the barrier denies it as opaque.
        """
        size = getattr(permit, 'approved_file_size', None)
        if (not getattr(permit, 'upload_destination', None) or not getattr(permit, 'approved_file_hash', None)
                or not isinstance(size, int) or not 0 < size <= self.UPLOAD_CDP_MAX_FILE):
            return None
        session = self.page_sessions.get(page)
        if session is None:
            return None
        try:
            data = await session.send('Network.getRequestPostData', {'requestId': network_id})
            text = data['postData']
            if data.get('base64Encoded'):
                body = base64.b64decode(text, validate=True)
            else:
                # A string reply may be a lossy decoding of binary data; accept only ASCII.
                body = text.encode('ascii', errors='strict')
        except Exception:
            return None
        return body if 0 < len(body) <= self.UPLOAD_CDP_MAX_BODY else None

    async def _handle(self, event):
        request_id = event['requestId']
        owner = await self._owner(event.get('frameId'))
        destructive = event['request'].get('method', '').upper() in ('POST', 'PUT', 'PATCH', 'DELETE')
        if event['request'].get('method') == 'GET' and self.navigation_reason:
            reason = self.navigation_reason(event['request']['url'])
            destructive = reason == 'Potential destructive or sign-out action' or bool(reason and event.get('resourceType') == 'Document')
        query = False
        if destructive and owner == 'qa':
            try:
                query = is_readonly_graphql_post(self._request(event, owner), self.origin)
            except Exception:
                query = False
        if query:
            request = self._request(event, owner)
            routes = self.query_routes.get(self._query_key(self.roots[event['frameId']], request), [])
            route = routes.pop(0) if routes else None
            # Browser Fetch is the sole forwarding owner. The context route
            # delegates its proven query instead of independently sending it.
            response = await self._forward_query(request, route)
            if 300 <= response.status < 400:
                if self.on_block is not None:
                    await self.on_block(route.request if route is not None else request)
                await self.session.send('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Aborted'})
            else:
                try:
                    await self.session.send('Fetch.fulfillRequest', {'requestId': request_id,
                        'responseCode': response.status,
                        'responseHeaders': [{'name': name, 'value': value} for name, value in response.headers.items()],
                        'body': base64.b64encode(response.body).decode('ascii')})
                except Exception as error:
                    # The independently forwarded, AST-proven query is complete.
                    # Chromium can already have cancelled its original paused
                    # request when that request's owner closed. Require both
                    # explicit cancellation evidence and a vanished interception;
                    # this never acknowledges a failed destructive abort.
                    if not (str(error).endswith('Protocol error (Fetch.fulfillRequest): Invalid InterceptionId.')
                            and route is not None
                            and route.request.frame.page.is_closed()
                            and route.request.failure == 'net::ERR_ABORTED'):
                        raise
        elif destructive and owner not in ('manual', 'foreign'):
            req_method = event['request'].get('method', '').upper()
            req_url = event['request'].get('url', '')
            permit = getattr(self.mutation_barrier, 'active_permit', None) if self.mutation_barrier else None
            if permit and getattr(permit, 'endpoint_pattern', None):
                # Context routes defer mutations to this exact Fetch pause.
                # One authority validates and consumes the permit; no transferable receipt.
                page = next((p for p, root in self.page_roots.items()
                             if root == self.roots.get(event.get('frameId'))), None)
                if owner != 'qa' or page is None:
                    self.mutation_barrier.blocked_requests.append({'url': req_url, 'method': req_method,
                        'reason': 'Mutation without verified QA page ownership'})
                    await self.session.send('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Aborted'})
                    self.pending.discard(request_id)
                    self.changed.set()
                    return
                # This exact Fetch pause owns dispatch; no URL/time correlation or file inference.
                request = self._request(event, owner)
                request.frame = SimpleNamespace(page=page)
                request.post_data = ''
                if (permit.operation_type == 'UPLOAD_ATTACHMENT'
                        and request.post_data_buffer is None and event.get('networkId')):
                    request.post_data_buffer = await self._upload_body(page, event['networkId'], permit)
                decided = []
                async def abort(reason=None):
                    decided.append('abort')
                    await self.session.send('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Aborted'})
                async def proceed():
                    decided.append('continue')
                    await self._continue(request_id)
                try:
                    await self.mutation_barrier._handle_route(SimpleNamespace(
                        request=request, abort=abort, continue_=proceed))
                finally:
                    if not decided:
                        # Verification raised before a decision: the write stays undelivered.
                        self.mutation_barrier.blocked_requests.append({'url': req_url, 'method': req_method,
                            'reason': 'Mutation verification error; request aborted'})
                        await self.session.send('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Aborted'})
                    self.pending.discard(request_id)
                    self.changed.set()
                return
            if self.mutation_barrier:
                self.mutation_barrier.blocked_requests.append({
                    'url': req_url, 'method': req_method,
                    'reason': 'CDP mutation requires an active permit with endpoint_pattern'})
            await self.session.send('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Aborted'})
        else:
            await self._continue(request_id)
        self.pending.discard(request_id)
        self.changed.set()

    def retain(self):
        self.context._qa_safety_hold = self

    async def stop(self):
        if not self.context.browser.is_connected():
            self._disconnected()
            return
        # Page cleanup must finish before this method. A browser round trip
        # processes already queued target/Fetch events before testing the drain.
        live = (await self.session.send('Target.getTargets'))['targetInfos']
        if any(info.get('browserContextId') == self.context_id
               and info.get('type') in ('page', 'iframe')
               and self.frames.get(info['targetId']) != 'manual' for info in live):
            self.retain()
            raise SafetyShutdownError('Automated targets remain alive; browser interception retained.')
        deadline = asyncio.get_running_loop().time() + self.timeout
        while self.tasks or self.pending:
            if self.failed:
                break
            self.changed.clear()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except asyncio.TimeoutError:
                break
        if self.failed or self.tasks or self.pending:
            self.retain()
            raise SafetyShutdownError('Automated QA safety shutdown incomplete; interception retained. End the QA Browser session before another run.')
        await self.session.send('Fetch.disable')
        self.active = False
        self.session.remove_listener('Fetch.requestPaused', self._paused)
        self.context.remove_listener('page', self._page)
        self.context.browser.remove_listener('disconnected', self._disconnected)
        for session in self.page_sessions.values():
            try:
                await session.detach()
            except Exception:
                pass  # Closed targets have already disposed these observers.
        await self.session.detach()

    def _disconnected(self, *_args):
        # Browser shutdown ends network enforcement itself; no Fetch.disable is
        # issued to release traffic from a still-live shared browser.
        self.active = False
        for task in list(self.tasks):
            task.cancel()
        self.pending.clear()
        self.changed.set()
