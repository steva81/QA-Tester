"""Sequential authenticated BFS with explicit, checkpointed coverage."""
import asyncio
import base64
from collections import deque, defaultdict
import hashlib
import time
from urllib.parse import urljoin, urlsplit
from app.qa.engine import SmartSmokeQA
from app.qa.crawl_policy import normalize_crawl_url, link_reason, query_family, AUTH_PATH
from app.qa.redaction import redact_string


class SessionLost(RuntimeError):
    pass


class NavigationExcluded(RuntimeError):
    def __init__(self, final, reason):
        super().__init__(reason)
        self.final = final


class FullSiteQA(SmartSmokeQA):
    def __init__(self, target_url, project_id, run_id, context, page_limit=250,
                 current_url=None, session_validator=None, checkpoint=None,
                 query_limit=10, depth_limit=50, url_limit=None, navigation_limit=None,
                 delay=.2, navigation_timeout=30000, settle_timeout=1000,
                 enable_interactive_disclosure=False, interactive_options=None,
                 enable_authorized_mutations=False):
        if context is None:
            raise SessionLost('Full Site QA requires the authenticated Interactive context.')
        super().__init__(target_url, project_id, run_id, mode='current', context=context,
                         enable_interactive_disclosure=enable_interactive_disclosure,
                         interactive_options=interactive_options,
                         enable_authorized_mutations=enable_authorized_mutations)
        self.mode = 'full'
        self.page_limit = page_limit
        self.query_limit = query_limit
        self.depth_limit = depth_limit
        self.url_limit = url_limit or min(10000, page_limit * 10 + 20)
        self.navigation_limit = navigation_limit or page_limit * 4 + 100
        self.delay = delay
        self.navigation_timeout = navigation_timeout
        self.settle_timeout = settle_timeout
        self.session_validator = session_validator
        self.checkpoint = checkpoint
        self.records = {}
        self.pending = deque()
        self.families = defaultdict(set)
        self.tested_targets = set()
        self.phase = 'QUEUED'
        self.last_url = None
        self.page_limit_reached = False
        self.termination_reason = ''
        self.terminal_status = 'COMPLETED'
        self.context_lost = False
        self.navigations = 0
        self.saved_offset = 0
        self.discovery_safety_limited = False
        self.discovery_truncated = False
        self.started_at = time.monotonic()
        self._progress = {}
        self._target_creation_lock = asyncio.Lock()
        self._discover(target_url, 0)
        if current_url:
            self._discover(current_url, 0)
        self._publish()

    def normalize_url(self, url):
        return normalize_crawl_url(url)

    def navigation_reason(self, url):
        reason = link_reason(url, self.target_origin)
        # Login redirects are allowed solely to record AUTH REQUIRED honestly.
        return None if reason == 'Authentication entry point' else reason

    def _discover(self, url, depth, parent=None, label=''):
        if self.is_stopped:
            return
        try:
            normalized = normalize_crawl_url(url)
        except (ValueError, TypeError):
            normalized = url
        if normalized in self.records:
            return
        if len(self.records) >= self.url_limit:
            self.termination_reason = 'Discovery URL limit reached'
            self.discovery_truncated = True
            return
        reason = link_reason(url, self.target_origin, label)
        record = dict(url=normalized, final_url=None, depth=depth, parent=parent,
                      state='DISCOVERED', eligible=False, desktop_tested=False,
                      mobile_tested=False, desktop_limited=False, mobile_limited=False, discovery_limited=False, reason=reason or '', form_count=0)
        self.records[normalized] = record
        if reason:
            record['state'] = 'SKIPPED SAFELY'
            return
        record['eligible'] = True
        if depth > self.depth_limit:
            record.update(state='COVERAGE GAP', reason='Depth limit reached')
            return
        family = query_family(normalized)
        variants = self.families[family]
        if urlsplit(normalized).query and len(variants) >= self.query_limit:
            record.update(state='COVERAGE GAP', reason='Query variant limit reached')
            return
        variants.add(normalized)
        record['state'] = 'QUEUED'
        self.pending.append(normalized)

    def _publish(self):
        records = list(self.records.values())
        gaps = sum(r['state'] in ('COVERAGE GAP', 'FAILED TO LOAD', 'LIMITED COVERAGE') or
                   (r['eligible'] and not (r['desktop_tested'] and r['mobile_tested'])) for r in records)
        gaps += int(self.discovery_truncated or self.discovery_safety_limited)
        self._progress = dict(mode='full', phase=self.phase, current_url=redact_string(self.last_url),
            pages_discovered=len(records), pages_eligible=sum(r['eligible'] for r in records),
            pages_tested=sum(r['desktop_tested'] and r['mobile_tested'] for r in records),
            pages_attempted=sum(r['desktop_tested'] or r['mobile_tested'] for r in records),
            desktop_pages_tested=sum(r['desktop_tested'] for r in records),
            mobile_pages_tested=sum(r['mobile_tested'] for r in records),
            pages_fully_evaluated=sum(r['state'] == 'TESTED' and r['desktop_tested'] and r['mobile_tested'] and not r['desktop_limited'] and not r['mobile_limited'] for r in records),
            desktop_limited=sum(r['desktop_limited'] for r in records),
            mobile_limited=sum(r['mobile_limited'] for r in records),
            safety_limited_pages=sum(r['desktop_limited'] or r['mobile_limited'] or r['discovery_limited'] for r in records),
            safety_limited_viewports=sum(r['desktop_limited']+r['mobile_limited'] for r in records),
            discovery_safety_limited=self.discovery_safety_limited,
            redirected=sum(r['state'] == 'REDIRECTED' for r in records),
            auth_required=sum(r['state'] == 'AUTH REQUIRED' for r in records),
            skipped_safely=sum(r['state'] == 'SKIPPED SAFELY' for r in records),
            failed_to_load=sum(r['state'] == 'FAILED TO LOAD' for r in records),
            coverage_gaps=gaps, page_limit=self.page_limit, page_limit_reached=self.page_limit_reached,
            discovery_limit_reached=self.discovery_truncated,
            termination_reason=self.termination_reason,
            coverage_complete=self.phase == 'COMPLETED' and not gaps and not self.termination_reason)

    def get_progress(self):
        return dict(self._progress)

    def snapshot(self):
        self._publish()
        return dict(progress=self.get_progress(), limits=dict(query_limit=self.query_limit,
                    depth_limit=self.depth_limit, url_limit=self.url_limit, navigation_limit=self.navigation_limit),
                    pages=[dict(r, url_key=hashlib.sha256(r['url'].encode()).hexdigest()) for r in self.records.values()])

    def _persist(self):
        snapshot = self.snapshot()
        if self.checkpoint:
            self.checkpoint(self.results[self.saved_offset:], snapshot)
            self.saved_offset = len(self.results)

    def _boundary(self):
        if self.is_stopped:
            raise asyncio.CancelledError()
        if self.context_lost or (self.session_validator and not self.session_validator()):
            raise SessionLost('Authenticated Interactive session or BrowserContext was lost.')
        browser = self.provided_context.browser
        if browser and not browser.is_connected():
            raise SessionLost('Authenticated browser disconnected.')

    def _note(self, requested, final, status, text):
        self.results.append(dict(url=requested, final_url=final, status=status, actual_result=text,
                                 expected_result='Safely reach the requested page', viewport='Discovery',
                                 screenshot=None, finding_id=None))

    def record_safety_event(self, request):
        self._note(self.last_url or self.target_url, None, 'SAFETY ACTION',
                   f'Method: {request.method}\nURL: {request.url}\nReason: Blocked by QA Tester.')
        try:
            source = request.frame.page
            target = getattr(source, 'qa_owner_target', source)
            state = target.qa_safety
            self.results[-1].update(_qa_target_id=state['target_id'], viewport=state['viewport'], url=state['url'])
        except Exception:
            pass

    async def _new_qa_page(self, context):
        # expect_page is context-wide: concurrent waiters otherwise receive the
        # same first target event. Serialize creation and identity verification.
        async with self._target_creation_lock:
            return await self._create_qa_page(context)

    async def _create_qa_page(self, context):
        # Target.createTarget's background flag avoids activating the QA tab.
        if not getattr(self, '_context_id', None):
            if not context.pages:
                raise SessionLost('Interactive user page is unavailable.')
            session = await context.new_cdp_session(context.pages[0])
            try:
                info = await session.send('Target.getTargetInfo')
                self._context_id = info['targetInfo']['browserContextId']
            finally:
                await session.detach()
        control = await context.browser.new_browser_cdp_session()
        try:
            async with context.expect_page() as pending:
                created = await control.send('Target.createTarget', dict(url='about:blank', browserContextId=self._context_id, background=True))
            page = await pending.value
            identity = await context.new_cdp_session(page)
            try:
                info = await identity.send('Target.getTargetInfo')
                if info['targetInfo']['targetId'] != created['targetId']:
                    raise SessionLost('Page ownership changed during QA target creation.')
            finally:
                await identity.detach()
            await self._causality.install(page)
            return page
        finally:
            await control.detach()

    async def _set_qa_viewport(self, page, vp):
        # page.set_viewport_size also changes the containing Chromium window.
        # Per-target emulation changes QA layout without touching that window.
        session = await self.provided_context.new_cdp_session(page)
        page._qa_metrics_session = session
        await session.send('Emulation.setDeviceMetricsOverride', dict(width=vp['width'], height=vp['height'],
                           screenWidth=vp['width'], screenHeight=vp['height'], deviceScaleFactor=1,
                           mobile=False, dontSetVisibleSize=True))

    async def _capture_image(self, page, filepath):
        # Playwright's screenshot preparation waits for animation frames in an
        # inactive headful tab. Capture this target's surface directly instead
        # of activating it and disturbing the interactive user page.
        session = await self.provided_context.new_cdp_session(page)
        async def capture():
            metrics = await session.send('Page.getLayoutMetrics')
            size = metrics['cssContentSize']
            viewport = metrics['cssLayoutViewport']
            manual = next((p for p in self._causality.original_pages if not p.is_closed()), None)
            bounds = await manual.evaluate('() => [innerWidth,innerHeight]') if manual else [800,600]
            # Chromium 123 can stall capture when an inactive target's surface
            # exceeds the containing window. Fit only the capture surface;
            # CSS layout and the output clip retain their requested dimensions.
            scale = min(.5, bounds[0] / size['width'], bounds[1] / size['height'])
            await session.send('Emulation.setDeviceMetricsOverride', dict(width=int(viewport['clientWidth']),height=int(viewport['clientHeight']),deviceScaleFactor=1,mobile=False,
                viewport=dict(x=0,y=0,width=size['width'],height=size['height'],scale=scale)))
            return await session.send('Page.captureScreenshot', dict(format='png',
                fromSurface=True, captureBeyondViewport=(size['width'] > viewport['clientWidth'] or size['height'] > viewport['clientHeight']),
                clip=dict(x=0, y=0, width=size['width'], height=size['height'], scale=1)))
        try:
            operation = asyncio.create_task(capture())
            try:
                image = await asyncio.wait_for(asyncio.shield(operation), timeout=5)
            except BaseException:
                # Closing this owned target releases the protocol operation;
                # cancelling Channel.send directly leaves its future orphaned.
                try:
                    await self._quiesce_and_close_page(page)
                except BaseException:
                    # A failed preparation leaves the target alive. Retrieve
                    # the cancelled read waiter without waiting for unsafe close
                    # to unblock it; the browser guard remains installed.
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    raise
                await asyncio.gather(operation, return_exceptions=True)
                raise
            with open(filepath, 'wb') as evidence:
                evidence.write(base64.b64decode(image['data']))
        finally:
            try:
                await session.detach()
            except Exception:
                if not page.is_closed():
                    raise
                # Target destruction already disposed the capture session;
                # preserve the original cancellation or timeout exception.

    async def _navigate_page(self, page, url):
        for _ in range(20):
            self._boundary()
            reason = self.navigation_reason(url)
            if reason:
                raise NavigationExcluded(url, reason)
            page._qa_redirect = None
            response = await self._navigate_once(page, url)
            redirect = page._qa_redirect
            if not redirect:
                return response
            url = redirect
        raise RuntimeError('Redirect hop limit reached')

    async def _route_navigation(self, route):
        # Browser routing only covers the first request in a redirect chain.
        # Fetch one response without redirects and replace redirect responses
        # with an inert document. Only our checked navigation loop follows them.
        response = None
        try:
            response = await route.fetch(max_redirects=0, timeout=min(self.navigation_timeout, 5000))
            if 300 <= response.status < 400 and response.headers.get('location'):
                page = route.request.frame.page
                page._qa_redirect = urljoin(response.url, response.headers['location'])
                await route.fulfill(status=200, content_type='text/html', body='<title>QA redirect boundary</title>')
            else:
                await route.fulfill(response=response)
        except Exception:
            try:
                await route.abort()
            except Exception:
                pass
        finally:
            if response:
                await response.dispose()

    async def _navigate_once(self, page, url):
        operation = asyncio.create_task(super()._navigate_page(page, url))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Close the target to unblock Playwright, then retrieve its outcome.
            # Cancelling the Python waiter alone leaves protocol errors unobserved.
            try:
                await self._quiesce_and_close_page(page)
            except BaseException:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                raise
            await asyncio.gather(operation, return_exceptions=True)
            raise

    async def _inspect(self, url):
        page = await self._new_qa_page(self.provided_context)
        page.is_qa_automated_page = True
        page.qa_state = {'blocked_requests': []}
        page.qa_discovery_url = url
        redirect_target = None
        def on_response(response):
            nonlocal redirect_target
            if 300 <= response.status < 400 and response.headers.get('location'):
                redirect_target = urljoin(response.url, response.headers['location'])
        page.on('response', on_response)
        try:
            response = await self._navigate_page(page, url)
            self._boundary()
            try:
                await page.wait_for_load_state('networkidle', timeout=self.settle_timeout)
            except Exception:
                pass
            self._boundary()
            final = page.url
            status = response.status if response else 0
            if status in (401, 403) or AUTH_PATH.search(urlsplit(final).path):
                return final, status, [], 0, True
            if response and 'text/html' not in response.headers.get('content-type', '').lower():
                return final, status, [], 0, False
            links = await page.evaluate("""limit => Array.from(document.querySelectorAll('a[href]')).slice(0, limit + 1).map(a =>
                ({url:a.href,label:[a.textContent,a.getAttribute('aria-label'),a.title].filter(Boolean).join(' ')}))""", self.url_limit)
            if len(links) > self.url_limit:
                self.discovery_truncated = True
                self.termination_reason = 'Discovery URL limit reached'
            forms = await page.evaluate('() => document.forms.length')
            return final, status, links, forms, False
        except Exception:
            if redirect_target and self.navigation_reason(redirect_target):
                raise NavigationExcluded(redirect_target, self.navigation_reason(redirect_target))
            raise
        finally:
            await self._close_tree(page)

    async def _execute_queue(self):
        def context_closed(*args):
            self.context_lost = True
        self.provided_context.on('close', context_closed)
        try:
            self.phase = 'DISCOVERING'
            self._persist()
            while self.pending:
                self._boundary()
                if len(self.tested_targets) >= self.page_limit:
                    self.page_limit_reached = True
                    self.termination_reason = 'Page limit reached'
                    break
                if self.navigations >= self.navigation_limit or time.monotonic() - self.started_at > 1800:
                    self.termination_reason = 'Navigation or duration limit reached'
                    break
                requested = self.pending.popleft()
                record = self.records[requested]
                self.last_url = requested
                record['state'] = 'TESTING'
                self.phase = 'DISCOVERING'
                self.navigations += 1
                self._persist()
                try:
                    final, status, links, forms, auth = await self._inspect(requested)
                except (SessionLost, asyncio.CancelledError):
                    raise
                except NavigationExcluded as exc:
                    record.update(state='COVERAGE GAP', final_url=exc.final, reason=str(exc))
                    self._note(requested, exc.final, 'REDIRECTED', f'Redirect target excluded: {exc}')
                    self._persist()
                    continue
                except Exception as exc:
                    self._boundary()
                    record.update(state='FAILED TO LOAD', reason=str(exc))
                    self._note(requested, None, 'FAIL', f'Navigation failed: {exc}')
                    self._persist()
                    continue
                self._boundary()
                record['final_url'] = final
                if auth:
                    record.update(state='AUTH REQUIRED', reason='Authentication prevented verification')
                    self._note(requested, final, 'AUTH REQUIRED', record['reason'])
                    self.terminal_status = 'FAILED'
                    self.termination_reason = 'Authentication required; authenticated crawl stopped'
                    self._persist()
                    break
                if status == 429:
                    record.update(state='FAILED TO LOAD', reason='Target rate limit reached')
                    self._note(requested, final, 'FAIL', 'HTTP 429: target throttled QA; crawl stopped.')
                    self.terminal_status = 'FAILED'
                    self.termination_reason = record['reason']
                    self._persist()
                    break
                reason = self.navigation_reason(final)
                if reason:
                    record.update(state='COVERAGE GAP', reason=f'Redirect target excluded: {reason}')
                    self._note(requested, final, 'REDIRECTED', record['reason'])
                    self._persist()
                    continue
                canonical = normalize_crawl_url(final)
                if canonical != requested:
                    record.update(state='REDIRECTED', eligible=False, reason='Redirect alias')
                    self._note(requested, final, 'REDIRECTED', f'Requested URL redirected to {final}')
                    self._discover(canonical, record['depth'], requested)
                    target = self.records.get(canonical)
                    if target is None or not target['eligible'] or target['state'] == 'COVERAGE GAP':
                        record.update(state='COVERAGE GAP', reason='Redirect target could not be admitted')
                        self._persist()
                        continue
                    record = target
                    record['final_url'] = final
                    # The preflight already reached the target; reuse its discovered links.
                    try:
                        self.pending.remove(canonical)
                    except ValueError:
                        pass
                if canonical in self.tested_targets:
                    self._persist()
                    continue
                record['form_count'] = forms
                self.last_url = canonical
                record['state'] = 'TESTING'
                self.phase = 'TESTING'
                for vp in self.viewports:
                    self._boundary()
                    offset = len(self.results)
                    try:
                        actual_final = await self._test_page(self.provided_context, vp, canonical, record['depth'])
                    finally:
                        # Teardown can retain LIMITED evidence during cancellation.
                        # Include that partial attempt in the terminal snapshot.
                        batch = self.results[offset:]
                        # A cancellation diagnostic can be retained before any
                        # viewport evaluation completes. It is not coverage.
                        record[vp['name'].lower() + '_tested'] = any(r['status'] != 'DIAGNOSTIC' for r in batch)
                        record[vp['name'].lower() + '_limited'] = any(r['status']=='LIMITED COVERAGE' for r in batch)
                    if any(r['status'] == 'AUTH REQUIRED' for r in batch):
                        record.update(state='AUTH REQUIRED', reason='Authentication lost while testing')
                        self.terminal_status = 'FAILED'
                        self.termination_reason = record['reason']
                        self._persist()
                        break
                    if any(r['status'] == 'REDIRECTED' for r in batch):
                        record.update(state='COVERAGE GAP', reason='Target changed while testing', final_url=actual_final)
                        self._persist()
                        break
                    if any(r['status'] == 'FAIL' and ('HTTP ' in r['actual_result'] or 'Navigation failed' in r['actual_result']) for r in batch):
                        record['reason'] = 'Page failed navigation or returned an HTTP error'
                    # Each finished viewport is committed before starting the next.
                    self._persist()
                if record['desktop_tested'] and record['mobile_tested']:
                    record['state'] = 'LIMITED COVERAGE' if record['desktop_limited'] or record['mobile_limited'] else ('FAILED TO LOAD' if status >= 400 or record['reason'] else 'TESTED')
                    self.tested_targets.add(canonical)
                if self.terminal_status == 'FAILED':
                    break
                for link in links:
                    self._discover(urljoin(final, link['url']), record['depth'] + 1, canonical, link['label'])
                self._persist()
                await asyncio.sleep(self.delay)
            self._boundary()
            for record in self.records.values():
                if record['state'] in ('QUEUED', 'TESTING'):
                    record.update(state='COVERAGE GAP', reason=self.termination_reason or 'Incomplete viewport checks')
            self.phase = self.terminal_status
            self._persist()
        finally:
            self.provided_context.remove_listener('close', context_closed)
