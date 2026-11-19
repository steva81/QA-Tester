from app.paths import chromium_launch_args
from app.qa.causality import CONSOLE_ARGUMENT_SCRIPT
import asyncio
import json
from playwright.async_api import async_playwright
from app.qa.graphql_policy import is_readonly_graphql_post
from app.qa.browser_guard import BrowserMutationGuard, SafetyShutdownError
import time
from urllib.parse import urlparse, urljoin
import os
import datetime
import uuid
import re

class SmartSmokeQA:
    safety_command_timeout = 5
    def __init__(self, target_url, project_id, run_id, max_pages=25, max_depth=3, mode="crawl", collected_urls=None, context=None,
                 enable_interactive_disclosure=False, interactive_options=None, enable_authorized_mutations=False, account_context=None):
        self.target_url = target_url
        self.project_id = project_id
        self.run_id = run_id
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.mode = mode
        self.collected_urls = collected_urls or []
        self.provided_context = context
        from app.utils import get_canonical_origin
        self.target_origin = get_canonical_origin(target_url)
        self.account_context = account_context
        
        self.visited_urls = set()
        
        if self.mode == "crawl":
            self.queue = [(self.target_url, 0)]
        elif self.mode == "current":
            self.queue = [(self.target_url, 0)]
            self.max_pages = 1
        elif self.mode == "collected":
            self.queue = [(url, 0) for url in self.collected_urls]
            self.max_pages = len(self.collected_urls)
            
        self.results = []
        self.is_stopped = False
        self.finding_counter = 1
        self.cleanup_error = None
        self.browser = None
        self.playwright = None
        
        self.viewports = [
            {'name': 'Desktop', 'width': 1366, 'height': 768},
            {'name': 'Mobile', 'width': 390, 'height': 844}
        ]

        # Phase C3: Explicitly enabled controlled interactive disclosure workflow
        from app.qa.interactive_bridge import InteractiveDisclosureBridge, InteractiveWorkflowConfig
        if isinstance(interactive_options, dict):
            config = InteractiveWorkflowConfig(enabled=enable_interactive_disclosure, **interactive_options)
        elif isinstance(interactive_options, InteractiveWorkflowConfig):
            config = interactive_options
        else:
            config = InteractiveWorkflowConfig(enabled=enable_interactive_disclosure)
        self.interactive_bridge = InteractiveDisclosureBridge(config=config)
        self.enable_authorized_mutations = enable_authorized_mutations
        self._mutations_exercised = set()
        if self.enable_authorized_mutations:
            from app.qa.scoped_mutation import ScopedMutationNetworkBarrier
            self._mutation_barrier = ScopedMutationNetworkBarrier(target_origin=self.target_origin, is_cancelled=lambda: self.is_stopped)
        else:
            self._mutation_barrier = None

    def is_same_origin(self, url):
        from app.utils import get_canonical_origin
        return get_canonical_origin(url) == self.target_origin

    def normalize_url(self, url):
        parsed = urlparse(url)
        path = parsed.path.rstrip('/') or '/'
        return parsed._replace(fragment="", path=path).geturl()

    def stop(self):
        self.is_stopped = True
        
    def get_next_finding_id(self):
        fid = f"QA-{self.finding_counter:03d}"
        self.finding_counter += 1
        return fid

    async def capture_screenshot(self, page, name, finding_id, vp_name):
        vp_safe = vp_name.split(' ')[0]
        uid = uuid.uuid4().hex[:6]
        filename = f"proj{self.project_id}_run{self.run_id}_{finding_id}_{vp_safe}_{name}_{uid}.png"
        from app.paths import get_screenshots_dir
        filepath = os.path.join(get_screenshots_dir(), filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        try:
            await self._capture_image(page, filepath)
            return filepath
        except Exception as e:
            print(f"Screenshot failed: {e}")
            return None

    async def _capture_image(self, page, filepath):
        await page.screenshot(path=filepath, full_page=True)

    def get_existing_finding_id(self, url, status, actual_result):
        for r in self.results:
            if r['url'] == url and r['status'] == status and r['actual_result'] == actual_result and r['finding_id']:
                return r['finding_id']
        return None
    async def _quiesce_and_close_page(self, page):
        if page.is_closed():
            return
        session = await page.context.new_cdp_session(page)
        try:
            await asyncio.wait_for(session.send('Emulation.setScriptExecutionDisabled', {'value': True}), self.safety_command_timeout)
            await asyncio.wait_for(session.send('Page.stopLoading'), self.safety_command_timeout)
            await page.close()
        finally:
            try:
                await session.detach()
            except Exception:
                pass

    async def _close_tree(self, root_page):
        ctx = root_page.context
        pages_to_close = [root_page]
        while True:
            added = False
            for p in ctx.pages:
                if p not in pages_to_close:
                    try:
                        opener = await p.opener()
                        if opener in pages_to_close:
                            pages_to_close.append(p)
                            added = True
                    except: pass
            if not added: break
        for p in reversed(pages_to_close):
            try:
                try:
                    await self._causality.finish_page_bindings(p)
                finally:
                    await self._quiesce_and_close_page(p)
            except Exception as error:
                if not p.is_closed():
                    raise SafetyShutdownError('Automated target could not be safely quiesced; interception retained.') from error

    async def _run_interactive_evaluations(self, context, page, current_url, vp, target_id):
        if not getattr(self, 'interactive_bridge', None) or not self.interactive_bridge.is_enabled():
            return
        is_stopped_fn = lambda: getattr(self, 'is_stopped', False)

        def record_sink(r):
            if isinstance(r, dict):
                r['_qa_target_id'] = target_id
                self.results.append(r)

        if isinstance(vp, dict):
            vp_name = vp.get('name') or ('Mobile' if vp.get('width', 1366) <= 500 else 'Desktop')
            vp_dict = {'width': int(vp.get('width', 1366)), 'height': int(vp.get('height', 768))}
        else:
            vp_name = str(vp)
            vp_dict = {'width': 390, 'height': 844} if 'mobile' in vp_name.lower() else {'width': 1366, 'height': 768}

        try:
            await self.interactive_bridge.evaluate_page_disclosures(
                source_context=getattr(page, 'context', context),
                source_page=page,
                page_url=current_url,
                viewport_name=vp_name,
                run_id=self.run_id,
                is_stopped_fn=is_stopped_fn,
                record_sink=record_sink,
                project_id=self.project_id,
                viewport=vp_dict,
            )
        except Exception:
            if getattr(self, 'interactive_bridge', None):
                self.interactive_bridge.halt_cleanup()
            diag_rec = self.interactive_bridge.create_infrastructure_diagnostic_record(
                run_id=self.run_id,
                url=current_url,
                viewport=vp_name,
                reason="Unexpected error during interactive evaluation.",
            )
            record_sink(diag_rec)

    async def _run_scoped_mutations(self, context, page, current_url, vp, target_id):
        if not getattr(self, 'enable_authorized_mutations', False):
            return
        if not any(k in current_url.lower() for k in ('profile', 'account', 'user', 'experience', 'settings')):
            return

        vp_name_clean = vp.get('name', 'Desktop') if isinstance(vp, dict) else str(vp)
        mutation_key = (self.normalize_url(current_url), vp_name_clean)
        if mutation_key in self._mutations_exercised:
            return
        self._mutations_exercised.add(mutation_key)

        from app.qa.scoped_mutation import (
            AuthenticatedIdentityDiscovery,
            AutonomousProfileWorkflowRunner,
            ScopedMutationNetworkBarrier,
            ScopedProfileWorkflowResult,
            adapt_scoped_mutation_result_to_test_record,
        )
        if not getattr(self, '_mutation_barrier', None):
            self._mutation_barrier = ScopedMutationNetworkBarrier(target_origin=self.target_origin)
        self._mutation_barrier._installed_target = context
        self._mutation_barrier.is_cancelled = lambda: self.is_stopped

        vp_label = f"{vp_name_clean} {vp.get('width', 1366)}x{vp.get('height', 768)}" if isinstance(vp, dict) else vp_name_clean

        # Authoritatively discover and verify session identity
        discovered_identity = await AuthenticatedIdentityDiscovery.discover_session_identity(page, self.target_origin)
        effective_account = self.account_context or discovered_identity

        # If identity is unverified or missing: writes MUST BE PREVENTED
        if not effective_account:
            wr = ScopedProfileWorkflowResult(
                workflow_type="identity_verification",
                verdict="LIMITED_COVERAGE",
                action_type="verify_session_identity",
                details="No authoritative authenticated session identity established for target origin. Authorized state-changing profile writes prevented.",
                viewport_name=vp_label,
            )
            rec = adapt_scoped_mutation_result_to_test_record(wr, self.run_id, current_url)
            rec['_qa_target_id'] = target_id
            self.results.append(rec)
            return

        if self.account_context and discovered_identity and self.account_context != discovered_identity:
            wr = ScopedProfileWorkflowResult(
                workflow_type="identity_verification",
                verdict="LIMITED_COVERAGE",
                action_type="verify_session_identity",
                details=f"Authenticated identity conflict: expected '{self.account_context}' but active session identity is '{discovered_identity}'. Writes prevented.",
                viewport_name=vp_label,
            )
            rec = adapt_scoped_mutation_result_to_test_record(wr, self.run_id, current_url)
            rec['_qa_target_id'] = target_id
            self.results.append(rec)
            return

        self._mutation_barrier.set_verified_session_identity(effective_account)
        runner = AutonomousProfileWorkflowRunner(barrier=self._mutation_barrier)

        async def screenshot_fn(pg, name, fid):
            return await self.capture_screenshot(pg, name, fid, vp_label)

        from types import SimpleNamespace
        workflow_results = await runner.run_authorized_profile_suite(
            page=page,
            target_url=current_url,
            run_id=self.run_id,
            project_id=self.project_id,
            viewport_name=vp_label,
            account_context=effective_account,
            cancellation_token=SimpleNamespace(is_cancelled=lambda: self.is_stopped),
            screenshot_fn=screenshot_fn,
        )

        for wr in workflow_results:
            rec = adapt_scoped_mutation_result_to_test_record(wr, self.run_id, current_url)
            rec['_qa_target_id'] = target_id
            self.results.append(rec)

    async def _test_page(self, context, vp, current_url, depth):
        vp_name = f"{vp['name']} {vp['width']}x{vp['height']}"
        page = await self._new_qa_page(context)
        def init_qa_page(p, state):
            p.is_qa_automated_page = True
            p.qa_state = state
            p.qa_owner_target = page
            if p is not page:
                p.on("console", on_console)
                p.on("pageerror", on_pageerror)
            p.on("popup", lambda popup: init_qa_page(popup, {"blocked_requests": []}))
        init_qa_page(page, {"blocked_requests": []})
        await self._set_qa_viewport(page, vp)
        
        target_id = uuid.uuid4().hex
        impact = {'blocked': False, 'blocks': [], 'diagnostics': [], 'target_id': target_id, 'viewport': vp_name, 'url': current_url}
        page.qa_safety = impact
        page.qa_owner_target = page
        independent_errors = []
        page_errors = []
        def record_error(text, independent=False):
            text = self._causality.clean_text(text)
            page_errors.append(text)
            if independent:
                independent_errors.append(text)
        network_failures = []
        console_tasks = set()
        
        def on_pageerror(err):
            text = str(err)
            independent = not impact['blocked']
            impact['diagnostics'].append(self._causality.clean_text(text))
            if not self._causality.attributable(page, text + '\n' + (getattr(err, 'stack', '') or '')):
                record_error(text, independent)
        page.on("pageerror", on_pageerror)
        async def collect_console(msg, independent):
            if msg.type != 'error':
                return
            blocked = page.qa_state.get('blocked_requests', [])
            source = msg.location.get('url', '')
            if not msg.args:
                impact['diagnostics'].append(self._causality.clean_text(msg.text))
            if not msg.args and 'ERR_FAILED' in msg.text and any(b['url'] == source or re.search(re.escape(b['url']) + r"(?=$|[\s'\"\)\]])", msg.text) for b in blocked):
                return
            # Handles preserve full values. Chromium's object preview is lossy
            # and may render sensitive fields outside valid JSON.
            caused = False
            genuine = []
            leading_text = []
            for arg in msg.args:
                try:
                    evidence = await arg.evaluate(CONSOLE_ARGUMENT_SCRIPT)
                except Exception:
                    # Inspection failure must never turn an unknown log into a pass.
                    genuine.append(msg.text or 'Uninspectable console argument')
                    impact['diagnostics'].append(self._causality.clean_text(msg.text or 'Uninspectable console argument'))
                    continue
                if evidence.get('tree') is not None:
                    raw, _ = self._causality.filter_console_tree(page, evidence['tree'], None)
                    impact['diagnostics'].append(self._causality.clean_text(json.dumps(raw, ensure_ascii=False)))
                    remaining, removed = self._causality.filter_console_tree(
                        page, evidence['tree'], evidence.get('generation'), evidence.get('viewport'))
                    caused = caused or removed
                    if removed:
                        genuine.extend(leading_text)
                        leading_text.clear()
                    if remaining is not None or not removed:
                        genuine.append(json.dumps(remaining, ensure_ascii=False))
                    continue
                stack, text = evidence['stack'], evidence['text']
                if stack or text:
                    impact['diagnostics'].append(self._causality.clean_text(stack or text))
                if self._causality.attributable(page, stack or text):
                    caused = True
                    # Text following the receipt is separate evidence. A mixed
                    # log must not let a caused Error mask a genuine suffix.
                    suffix = re.split(r'\[QA-BLOCK:[a-f0-9]{32}\]', text)[-1].strip() if text else ''
                    if suffix:
                        genuine.append(suffix)
                elif stack:
                    genuine.append(stack)
                elif evidence['opaque']:
                    # Only the proven Error belongs to the safety block. Keep
                    # independent structured/primitive evidence in mixed logs.
                    impact['diagnostics'].append(self._causality.clean_text(evidence['opaque']))
                    genuine.append(evidence['opaque'] + ('\n' + msg.text if evidence['opaque'].startswith('Uninspectable') else ''))
                elif caused and text.strip():
                    genuine.append(text)
                elif text.strip():
                    leading_text.append(text)
            if genuine:
                record_error('\n'.join(genuine), independent)
                return
            if caused or self._causality.attributable(page, msg.text):
                return
            record_error(msg.text, independent)
        def on_console(msg):
            task = asyncio.create_task(collect_console(msg, not impact['blocked']))
            console_tasks.add(task)
            def finished(completed):
                console_tasks.discard(completed)
                if not completed.cancelled() and completed.exception():
                    record_error('Console inspection failed: ' + str(completed.exception()))
            task.add_done_callback(finished)
        page.on('console', on_console)
        
        async def on_response(response):
            if response.status >= 400 and response.request.method.upper() == 'GET' and response.request.resource_type in ['fetch', 'xhr', 'script', 'stylesheet', 'image']:
                network_failures.append(f"{response.request.resource_type} failed: {response.url} ({response.status})")
                impact['diagnostics'].append(network_failures[-1])
        page.on("response", on_response)
        
        async def on_requestfailed(request):
            if request.method.upper() == 'GET' and request.failure:
                if request.failure == 'net::ERR_ABORTED':
                    # Cancellation alone proves neither customer impact nor its
                    # cause. Retain it without bypassing independent JS/DOM/HTTP
                    # checks or inventing a customer defect from an aborted GET.
                    self.results.append(dict(_qa_target_id=target_id,
                        url=current_url, final_url=page.url,
                        expected_result='Aborted request requires independent failure evidence',
                        actual_result=f'{request.resource_type} cancelled: {request.url} ({request.failure}); cancellation cause and customer impact unconfirmed.',
                        status='DIAGNOSTIC', viewport=vp_name, screenshot=None, finding_id=None))
                    return
                if any(b['url'] == request.url and b['method'] == 'GET' for b in page.qa_state.get('blocked_requests', [])):
                    return
                network_failures.append(f"{request.resource_type} failed: {request.url} ({request.failure})")
                impact['diagnostics'].append(network_failures[-1])
        page.on("requestfailed", on_requestfailed)

        screenshot_path = None
        response = None
        
        try:
            response = await self._navigate_page(page, current_url)
            try:
                await page.wait_for_load_state("networkidle", timeout=getattr(self, 'settle_timeout', 10000))
            except Exception:
                pass # ignore networkidle timeout if dom is loaded
                
            final_url = page.url
            status = response.status if response else None
            title = await page.title()
            if self.mode == 'full' and status in (401, 403):
                self._note(current_url, final_url, 'AUTH REQUIRED', f'Authentication prevented verification (HTTP {status}).')
                return final_url
            
            if page.qa_state.get("blocked_requests"):
                for block in page.qa_state["blocked_requests"]:
                    actual_res = f"Method: {block['method']}\nURL: {block['url']}\nReason: Blocked by QA Tester."
                    fid = self.get_existing_finding_id(current_url, "SAFETY ACTION", actual_res) or self.get_next_finding_id()
                    self.results.append({
                    "_qa_target_id": target_id,
                        "url": block["source"], "final_url": final_url, "expected_result": "No mutations during automated QA",
                        "actual_result": actual_res, "status": "SAFETY ACTION",
                        "screenshot": None, "viewport": vp_name, "finding_id": fid,
                        "title": "Unsafe Request Prevented", "area": "Security / Network"
                    })
                # GET failures remain independent evidence. Only console errors
                # correlated to the exact blocked request are suppressed above.
            
            # 1. Navigation / Response Status
            if not status and not response:
                actual_res = "Failed to get response status"
                fid = self.get_existing_finding_id(current_url, "FAIL", actual_res) or self.get_next_finding_id()
                screenshot_path = await self.capture_screenshot(page, "No_Status", fid, vp_name)
                self.results.append({
                    "_qa_target_id": target_id,
                    "url": current_url, "final_url": final_url, "expected_result": "Page loads successfully",
                    "actual_result": actual_res, "status": "FAIL",
                    "screenshot": screenshot_path, "viewport": vp_name, "finding_id": fid,
                    "title": "Page failed to return a status", "area": "Page Load / Server Response"
                })
                return final_url
                
            if status and status >= 400:
                actual_res = f"Server returned HTTP {status}"
                fid = self.get_existing_finding_id(current_url, "FAIL", actual_res) or self.get_next_finding_id()
                screenshot_path = await self.capture_screenshot(page, f"HTTP_{status}", fid, vp_name)
                self.results.append({
                    "_qa_target_id": target_id,
                    "url": current_url, "final_url": final_url, "expected_result": "Page loads successfully (200 OK)",
                    "actual_result": actual_res, "status": "FAIL",
                    "screenshot": screenshot_path, "viewport": vp_name, "finding_id": fid,
                    "title": f"Page returns HTTP {status}", "area": "Page Load / Server Response"
                })
                return final_url

            # 2. Redirect Handling
            if self.normalize_url(current_url) != self.normalize_url(final_url):
                parsed_final = urlparse(final_url)
                path_parts = parsed_final.path.lower().split('/')
                if any(x in path_parts for x in ['signin', 'login', 'auth']):
                    self.results.append({
                    "_qa_target_id": target_id,
                        "url": current_url, "final_url": final_url, "expected_result": "Target reached",
                        "actual_result": "Authentication prevented functional verification of the requested page.", "status": "AUTH REQUIRED",
                        "screenshot": None, "viewport": vp_name, "finding_id": None,
                    })
                    return final_url
                else:
                    self.results.append({
                    "_qa_target_id": target_id,
                        "url": current_url, "final_url": final_url, "expected_result": "Target reached",
                        "actual_result": f"Requested URL redirected to {final_url}", "status": "REDIRECTED",
                        "screenshot": None, "viewport": vp_name, "finding_id": None,
                    })
                    return final_url

            # 3. Network & JS Errors
            if console_tasks:
                await asyncio.gather(*list(console_tasks))
            if page_errors:
                actual_res = f"Console/JS errors detected: {(independent_errors or page_errors)[0]}"
                fid = self.get_existing_finding_id(current_url, "OBSERVATION", actual_res) or self.get_next_finding_id()
                self.results.append({
                    "_qa_target_id": target_id,
                    "url": current_url, "final_url": final_url, "expected_result": "No JS errors",
                    "actual_result": actual_res, "status": "OBSERVATION",
                    "screenshot": None, "viewport": vp_name, "finding_id": fid,
                    "title": "Uncaught JavaScript exception", "area": "JavaScript Execution",
                    "independent_safety": bool(independent_errors)
                })
                await self._run_interactive_evaluations(context, page, current_url, vp, target_id)
                await self._run_scoped_mutations(context, page, current_url, vp, target_id)
                return final_url
                
            if network_failures:
                actual_res = f"Failed resource: {network_failures[0]}"
                fid = self.get_existing_finding_id(current_url, "OBSERVATION", actual_res) or self.get_next_finding_id()
                self.results.append({
                    "_qa_target_id": target_id,
                    "url": current_url, "final_url": final_url, "expected_result": "All critical resources load",
                    "actual_result": actual_res, "status": "OBSERVATION",
                    "screenshot": None, "viewport": vp_name, "finding_id": fid,
                    "title": "Failed GET resource", "area": "Network"
                })
                await self._run_interactive_evaluations(context, page, current_url, vp, target_id)
                await self._run_scoped_mutations(context, page, current_url, vp, target_id)
                return final_url

            # 4. Layout & DOM Checks
            overflow_detected = await page.evaluate('() => document.documentElement.scrollWidth > window.innerWidth')
            if overflow_detected and vp['name'] == 'Mobile':
                actual_res = "Horizontal overflow detected on mobile"
                fid = self.get_existing_finding_id(current_url, "FAIL", actual_res) or self.get_next_finding_id()
                screenshot_path = await self.capture_screenshot(page, "Mobile_Overflow", fid, vp_name)
                self.results.append({
                    "_qa_target_id": target_id,
                    "url": current_url, "final_url": final_url, "expected_result": "Page fits viewport without horizontal scrolling",
                    "actual_result": actual_res, "status": "FAIL",
                    "screenshot": screenshot_path, "viewport": vp_name, "finding_id": fid,
                    "title": "Horizontal overflow detected", "area": "Responsive Layout"
                })
                return final_url

            if not title or title.strip() == "":
                actual_res = "Missing or empty document title"
                fid = self.get_existing_finding_id(current_url, "OBSERVATION", actual_res) or self.get_next_finding_id()
                self.results.append({
                    "_qa_target_id": target_id,
                    "url": current_url, "final_url": final_url, "expected_result": "Document has a title",
                    "actual_result": actual_res, "status": "OBSERVATION",
                    "screenshot": None, "viewport": vp_name, "finding_id": fid,
                    "title": "Missing document title", "area": "DOM / Basic Quality"
                })
                await self._run_interactive_evaluations(context, page, current_url, vp, target_id)
                await self._run_scoped_mutations(context, page, current_url, vp, target_id)
                return final_url

            duplicate_ids = await page.evaluate('''() => {
                const ids = Array.from(document.querySelectorAll('[id]')).map(el => el.id);
                const duplicates = ids.filter((item, index) => ids.indexOf(item) !== index);
                return duplicates.length > 0 ? duplicates[0] : null;
            }''')
            if duplicate_ids:
                actual_res = f"Duplicate DOM ID found: {duplicate_ids}"
                fid = self.get_existing_finding_id(current_url, "OBSERVATION", actual_res) or self.get_next_finding_id()
                self.results.append({
                    "_qa_target_id": target_id,
                    "url": current_url, "final_url": final_url, "expected_result": "No duplicate IDs",
                    "actual_result": actual_res, "status": "OBSERVATION",
                    "screenshot": None, "viewport": vp_name, "finding_id": fid,
                    "title": "Duplicate DOM IDs", "area": "DOM / Basic Quality"
                })
                await self._run_interactive_evaluations(context, page, current_url, vp, target_id)
                await self._run_scoped_mutations(context, page, current_url, vp, target_id)
                return final_url

            missing_label = await page.evaluate('''() => {
                const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"])'));
                for (let input of inputs) {
                    if (input.getAttribute('aria-label') || input.getAttribute('aria-labelledby')) continue;
                    if (input.closest('label')) continue;
                    if (!input.id) return true;
                    let escapedId;
                    try { escapedId = CSS.escape(input.id); } catch(e) { escapedId = input.id; }
                    const label = document.querySelector(`label[for="${escapedId}"]`);
                    if (!label) return true;
                }
                return false;
            }''')
            if missing_label:
                actual_res = "Missing form label for input"
                fid = self.get_existing_finding_id(current_url, "OBSERVATION", actual_res) or self.get_next_finding_id()
                self.results.append({
                    "_qa_target_id": target_id,
                    "url": current_url, "final_url": final_url, "expected_result": "All inputs have labels",
                    "actual_result": actual_res, "status": "OBSERVATION",
                    "screenshot": None, "viewport": vp_name, "finding_id": fid,
                    "title": "Missing form label", "area": "Form Inspection"
                })
                await self._run_interactive_evaluations(context, page, current_url, vp, target_id)
                await self._run_scoped_mutations(context, page, current_url, vp, target_id)
                return final_url
                
            broken_images = await page.evaluate('''() => {
                const imgs = Array.from(document.querySelectorAll('img'));
                for (let img of imgs) {
                    if (!img.complete || img.naturalWidth === 0) return true;
                }
                return false;
            }''')
            if broken_images:
                actual_res = "Broken image detected"
                fid = self.get_existing_finding_id(current_url, "OBSERVATION", actual_res) or self.get_next_finding_id()
                self.results.append({
                    "_qa_target_id": target_id,
                    "url": current_url, "final_url": final_url, "expected_result": "All images load successfully",
                    "actual_result": actual_res, "status": "OBSERVATION",
                    "screenshot": None, "viewport": vp_name, "finding_id": fid,
                    "title": "Broken image", "area": "DOM / Basic Quality"
                })
                await self._run_interactive_evaluations(context, page, current_url, vp, target_id)
                await self._run_scoped_mutations(context, page, current_url, vp, target_id)
                return final_url

            # 5. Success
            self.results.append({
                    "_qa_target_id": target_id,
                "url": current_url, "final_url": final_url, "expected_result": "Page loads successfully",
                "actual_result": "Page loaded without errors", "status": "PASS",
                "screenshot": None, "viewport": vp_name, "finding_id": None
            })
            await self._run_interactive_evaluations(context, page, current_url, vp, target_id)
            await self._run_scoped_mutations(context, page, current_url, vp, target_id)
            return final_url
            
        except Exception as e:
            if self.mode == 'full' and hasattr(e, 'final'):
                self._note(current_url, e.final, 'REDIRECTED', f'Redirect target excluded: {e}')
                return e.final
            actual_res = f"Navigation failed: {str(e)}"
            fid = self.get_existing_finding_id(current_url, "FAIL", actual_res) or self.get_next_finding_id()
            screenshot_path = await self.capture_screenshot(page, "Navigation_Failed", fid, vp_name)
            self.results.append({
                    "_qa_target_id": target_id,
                "url": current_url, "final_url": current_url, "expected_result": "Page loads successfully",
                "actual_result": actual_res, "status": "FAIL",
                "screenshot": screenshot_path, "viewport": vp_name, "finding_id": fid,
                "title": "Page navigation failed", "area": "Page Load / Server Response"
            })
            return current_url
        finally:
            # Stop producers before finalizing validity, including late popup traffic.
            await self._close_tree(page)
            if console_tasks:
                await asyncio.gather(*list(console_tasks), return_exceptions=True)
            if impact['blocked']:
                batch = [r for r in self.results if r.get('_qa_target_id') == target_id]
                self.results[:] = [r for r in self.results if r.get('_qa_target_id') != target_id]
                for result in batch:
                    result.pop('_qa_target_id', None)
                    if result['status'] == 'PASS' and not result.get('independent_safety') and not result.get('is_mutation_record'):
                        continue
                    if result['status'] in ('OBSERVATION', 'FAIL') and not result.get('independent_safety'):
                        result.update(status='DIAGNOSTIC', finding_id=None)
                    if result['status'] == 'SAFETY ACTION':
                        result['finding_id'] = None
                    self.results.append(result)
                for block in impact['blocks']:
                    block_text = f"Method: {block['method']}\nURL: {block['url']}\nReason: Blocked by QA Tester."
                    if any(r['status']=='SAFETY ACTION' and r['actual_result']==block_text for r in batch):
                        continue
                    self.results.append(dict(url=current_url, final_url=page.url, expected_result='No unsafe automated requests',
                        actual_result=f"Method: {block['method']}\nURL: {block['url']}\nReason: Blocked by QA Tester.",
                        status='SAFETY ACTION', viewport=vp_name, screenshot=None, finding_id=None))
                seen = {r['actual_result'] for r in batch if r['status']=='DIAGNOSTIC'}
                for text in impact['diagnostics']:
                    if text and text not in seen:
                        self.results.append(dict(url=current_url, final_url=page.url, expected_result='Diagnostic retained; independence unverified',
                            actual_result=text, status='DIAGNOSTIC', viewport=vp_name, screenshot=None, finding_id=None))
                        seen.add(text)
                self.results.append(dict(url=current_url, final_url=page.url, expected_result='Faithful evaluation without safety perturbation',
                    actual_result='QA Tester blocked application traffic during this attempt, so this page could not be fully evaluated. Diagnostics are retained; unverified errors are not customer defects.',
                    status='LIMITED COVERAGE', viewport=vp_name, screenshot=None, finding_id=None))
            for result in self.results:
                if result.get('_qa_target_id') == target_id:
                    result.pop('_qa_target_id', None)

    async def _new_qa_page(self, context):
        page = await context.new_page()
        await self._causality.install(page)
        return page

    async def _set_qa_viewport(self, page, vp):
        await page.set_viewport_size({'width': vp['width'], 'height': vp['height']})

    async def _navigate_page(self, page, url):
        return await page.goto(url, timeout=getattr(self, 'navigation_timeout', 30000), wait_until='domcontentloaded')


    async def _execute_queue(self):
        intended_count = 0
        while self.queue and intended_count < self.max_pages and not self.is_stopped:
            current_url, depth = self.queue.pop(0)

            norm = self.normalize_url(current_url)
            if norm in self.visited_urls:
                continue
                
            self.visited_urls.add(norm)
            intended_count += 1
            final_url = current_url

            # Check Desktop
            if not self.is_stopped:
                final_url = await self._test_page(self.provided_context or self.browser, self.viewports[0], current_url, depth)

            # Check Mobile
            if not self.is_stopped:
                await self._test_page(self.provided_context or self.browser, self.viewports[1], current_url, depth)

            # Link extraction
            if self.mode == "crawl" and depth < self.max_depth and not self.is_stopped:
                page = await (self.provided_context or self.browser).new_page()
                def init_qa_page(p, state):
                    p.is_qa_automated_page = True
                    p.qa_state = state
                    p.on("popup", lambda popup: init_qa_page(popup, state))
                init_qa_page(page, {"blocked_requests": []})
                try:
                    await page.goto(final_url, timeout=30000, wait_until="domcontentloaded")
                    hrefs = await page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('a[href]')).map(a => a.href);
                    }""")
                    for href in hrefs:
                        full_url = urljoin(final_url, href)
                        norm_url = self.normalize_url(full_url)
                        if self.is_same_origin(norm_url) and norm_url not in self.visited_urls:
                            self.queue.append((norm_url, depth + 1))
                except Exception:
                    pass
                finally:
                    await self._close_tree(page)

    async def run(self):
        execute_queue = self._execute_queue

        async def guarded(context):
            # Serialize borrowers: page ownership and guard removal belong to one run.
            if not hasattr(context, '_qa_run_lock'):
                context._qa_run_lock = asyncio.Lock()
            async with context._qa_run_lock:
                if getattr(context, '_qa_safety_hold', None) is not None:
                    raise SafetyShutdownError('A previous automated safety shutdown is unresolved. End the QA Browser session before another run.')
                if context.service_workers:
                    raise RuntimeError('Automated QA requires a context with service workers blocked.')
                # App-owned contexts already use service_workers='block'. Apply
                # the same registration policy to explicitly borrowed contexts.
                block_workers = "(() => { if (navigator.serviceWorker) navigator.serviceWorker.register = async () => { throw new Error('Service workers blocked during QA'); }; })()"
                await context.add_init_script(block_workers)
                for existing_page in context.pages:
                    registered = await existing_page.evaluate("async () => { try { return navigator.serviceWorker && (await navigator.serviceWorker.getRegistrations()).length > 0; } catch (_) { return false; } }")
                    if registered:
                        raise RuntimeError('Existing service-worker registrations prevent safe automated QA.')
                    await existing_page.evaluate(block_workers)
                original_pages = set(context.pages)
                from app.qa.causality import BlockCausality
                self._causality = BlockCausality(self, original_pages)
                attachment = asyncio.create_task(self._causality.attach(context))
                try:
                    await asyncio.shield(attachment)
                except BaseException:
                    try:
                        # Retrieve installation before releasing this owner;
                        # cancellation must not strand a half-installed binding.
                        if not attachment.done():
                            await asyncio.gather(attachment, return_exceptions=True)
                    finally:
                        self._causality.close(context)
                    raise
                owned_pages = set()
                def remember_page(page):
                    owned_pages.add(page)
                context.on('page', remember_page)
                closed = False
                def context_closed(*args):
                    nonlocal closed
                    closed = True
                    # Browser shutdown can interrupt cleanup before it reaches
                    # the normal correlation cleanup. A destroyed context has
                    # no remaining document provenance to retain.
                    self._causality.close(context)
                context.on('close', context_closed)
                browser_guard = None

                async def record_qa_block(request):
                    try:
                        source_page = request.frame.page
                        target = source_page
                        visited = set()
                        while not hasattr(target, 'qa_safety') and target not in original_pages and target not in visited:
                            visited.add(target)
                            parent = getattr(target, 'qa_owner_target', None) or await target.opener()
                            if parent is None:
                                break
                            target = parent
                        if hasattr(target, 'qa_safety'):
                            target.qa_safety['blocked'] = True
                            target.qa_safety['blocks'].append(dict(method=request.method, url=request.url))
                        elif self.mode == 'full' and source_page not in original_pages:
                            self.discovery_safety_limited = True
                            discovery_url = getattr(target, 'qa_discovery_url', None)
                            if discovery_url in self.records:
                                self.records[discovery_url]['discovery_limited'] = True
                    except Exception:
                        if self.mode == 'full':
                            self.discovery_safety_limited = True
                    self._causality.blocked(request)
                    if self.mode == 'full':
                        self.record_safety_event(request)
                    # Ownership lookup is diagnostic only; unknown ownership still aborts.
                    try:
                        page = request.frame.page
                        if hasattr(page, 'qa_state'):
                            if 'blocked_requests' not in page.qa_state:
                                page.qa_state['blocked_requests'] = []
                            page.qa_state['blocked_requests'].append({
                                "method": request.method.upper(),
                                "url": request.url,
                                "source": page.url,
                                "timestamp": datetime.datetime.now().isoformat()
                            })
                    except Exception:
                        pass

                async def block_qa_mutations(route):
                    try:
                        source_page = route.request.frame.page
                        if browser_guard and source_page not in original_pages:
                            await browser_guard.observe(source_page, ownership_only=True)
                        if source_page in original_pages or (browser_guard and browser_guard.is_manual(source_page)):
                            await route.fallback()
                            return
                    except Exception:
                        pass
                    blocked = route.request.method.upper() in ('POST', 'PUT', 'PATCH', 'DELETE')
                    if blocked and getattr(self, '_mutation_barrier', None) and self._mutation_barrier.active_permit:
                        from app.utils import get_canonical_origin
                        permit = self._mutation_barrier.active_permit
                        req_origin = get_canonical_origin(route.request.url)
                        if browser_guard and browser_guard.active:
                            # Browser-owned Fetch pause validates and consumes each mutation once.
                            await route.continue_()
                            return
                        if permit.is_valid(route.request.method.upper(), req_origin):
                            await self._mutation_barrier._handle_route(route)
                            return
                    if blocked and is_readonly_graphql_post(route.request, self.target_origin):
                        response = None
                        try:
                            if browser_guard is not None:
                                browser_guard.register_query_route(route)
                                try:
                                    await route.continue_()
                                except Exception:
                                    if not route.request.frame.page.is_closed():
                                        raise
                                return
                            else:
                                response = await route.fetch(max_redirects=0)
                                if not 300 <= response.status < 400:
                                    await route.fulfill(response=response)
                                    return
                        except Exception:
                            pass
                        finally:
                            if response is not None:
                                await response.dispose()
                    if not blocked and self.mode == 'full' and route.request.method.upper() == 'GET' and hasattr(self, 'navigation_reason'):
                        reason = self.navigation_reason(route.request.url)
                        if reason == 'Potential destructive or sign-out action' or (reason and route.request.is_navigation_request()):
                            try:
                                manual_page = route.request.frame.page in original_pages
                            except Exception:
                                manual_page = False
                            blocked = not manual_page
                    if blocked:
                        await record_qa_block(route.request)
                        if getattr(self, '_mutation_barrier', None):
                            self._mutation_barrier.blocked_requests.append({
                                "url": route.request.url,
                                "method": route.request.method,
                                "reason": "Blocked by client network barrier",
                            })
                        # Isolate Playwright's protocol/stack capture from the
                        # route callback's transient exception frames. Complete
                        # the actual abort even if teardown cancels its caller.
                        abort = asyncio.create_task(route.abort('blockedbyclient'))
                        try:
                            await asyncio.shield(abort)
                        except asyncio.CancelledError:
                            await asyncio.gather(abort, return_exceptions=True)
                            raise
                    else:
                        if self.mode == 'full' and route.request.method.upper() == 'GET' and route.request.is_navigation_request():
                            try:
                                manual = route.request.frame.page in original_pages
                            except Exception:
                                manual = False
                            if not manual:
                                await self._route_navigation(route)
                                return
                        await route.fallback()

                async def cleanup():
                    safe = False
                    try:
                        while not closed:
                            pages = (owned_pages | set(context.pages)) - original_pages
                            if browser_guard:
                                await asyncio.gather(*(browser_guard.observe(page) for page in pages if not page.is_closed()))
                            pages = [page for page in pages if not page.is_closed() and not (browser_guard and browser_guard.is_manual(page))]
                            if not pages:
                                break
                            outcomes = await asyncio.gather(*(self._close_tree(page) for page in pages), return_exceptions=True)
                            if any(isinstance(outcome, BaseException) for outcome in outcomes):
                                raise SafetyShutdownError('Automated target cleanup failed; interception retained.')
                            await asyncio.sleep(0)
                        if browser_guard is not None:
                            await browser_guard.stop()
                        safe = True
                    finally:
                        if safe or browser_guard is None:
                            context.remove_listener('page', remember_page)
                            if not closed:
                                await context.unroute('**/*', block_qa_mutations)
                            context.remove_listener('close', context_closed)
                            self._causality.close(context)
                        else:
                            browser_guard.retain()

                try:
                    if getattr(self, 'enable_authorized_mutations', False) and not getattr(self, '_mutation_barrier', None):
                        from app.qa.scoped_mutation import ScopedMutationNetworkBarrier
                        self._mutation_barrier = ScopedMutationNetworkBarrier(target_origin=self.target_origin)
                    await context.route('**/*', block_qa_mutations)
                    # Real browser contexts require browser-owned enforcement.
                    # An empty route-only test double cannot execute browser work.
                    if getattr(context, 'browser', None) is not None:
                        browser_guard = BrowserMutationGuard(context, original_pages, self.target_origin,
                            getattr(self, 'navigation_reason', None) if self.mode == 'full' else None, on_block=record_qa_block,
                            mutation_barrier=getattr(self, '_mutation_barrier', None))
                        await browser_guard.start()
                    elif context.pages or self.queue:
                        raise SafetyShutdownError('Browser-owned mutation interception is unavailable.')
                    await execute_queue()
                finally:
                    # Finish cleanup despite cancellation arriving during teardown.
                    # Never remove protection while a QA descendant is still alive.
                    async def bounded_cleanup():
                        try:
                            await asyncio.wait_for(cleanup(), timeout=15)
                        except asyncio.TimeoutError as error:
                            if browser_guard is not None:
                                browser_guard.retain()
                            raise SafetyShutdownError('Automated safety cleanup timed out; interception retained.') from error
                    completion = asyncio.create_task(bounded_cleanup())
                    try:
                        await asyncio.shield(completion)
                    except asyncio.CancelledError:
                        await completion
                        raise

        if self.provided_context:
            await guarded(self.provided_context)
        else:
            p = await async_playwright().start()
            self.playwright = p
            browser = None
            run_exc = None
            run_cancelled = False
            try:
                self.browser = await p.chromium.launch(
                    headless=True,
                    args=chromium_launch_args(headless=True),
                    chromium_sandbox=True,
                )
                browser = self.browser
                self.context = await self.browser.new_context(service_workers='block')
                self.provided_context = self.context
                await guarded(self.context)
            except asyncio.CancelledError as ce:
                run_cancelled = True
                run_exc = ce
            except BaseException as be:
                run_exc = be
            finally:
                await self._teardown_standalone_playwright(p, browser, run_exc=run_exc, run_cancelled=run_cancelled)
        return self.results

    @staticmethod
    async def _run_shielded_step(coro, timeout=15):
        """
        Executes an asynchronous cleanup step with explicit task ownership.
        Ensures the cleanup task runs to completion even if the caller is repeatedly
        cancelled, preventing orphaned background tasks and ensuring loop closure
        cannot race with ongoing cleanup.
        Retains shielding and task ownership across repeated cancellation, including
        the timeout supervisory task, consumes final results, and reports incomplete
        teardown truthfully.
        Returns: (exc: BaseException | None, was_cancelled: bool)
        """
        task = coro if isinstance(coro, asyncio.Task) else asyncio.create_task(coro)
        was_cancelled = False
        exc = None
        timed_out = False
        curr_task = asyncio.current_task()

        supervisor = None
        if timeout is not None and timeout > 0:
            async def _supervise():
                nonlocal timed_out
                await asyncio.sleep(timeout)
                if not task.done():
                    timed_out = True
                    task.cancel()
            supervisor = asyncio.create_task(_supervise())

        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if curr_task is not None:
                        if hasattr(curr_task, "cancelling") and curr_task.cancelling() > 0:
                            was_cancelled = True
                        elif curr_task.cancelled():
                            was_cancelled = True
                except BaseException as be:
                    exc = be
                    break
        finally:
            if supervisor is not None:
                if not supervisor.done():
                    supervisor.cancel()
                while not supervisor.done():
                    try:
                        await asyncio.shield(supervisor)
                    except (asyncio.CancelledError, Exception):
                        pass

        if timed_out:
            exc = TimeoutError(f"Cleanup step timed out after {timeout} seconds")
        elif exc is None:
            if task.cancelled():
                exc = RuntimeError("Cleanup step was cancelled before completion")
            else:
                task_exc = task.exception()
                if task_exc is not None:
                    exc = task_exc

        return exc, was_cancelled

    async def _teardown_standalone_playwright(self, p, browser, run_exc=None, run_cancelled=False):
        """
        Performs comprehensive teardown of Playwright browser and driver instances:
        1. Attempts all required teardown even if an earlier step fails (both browser.close and p.stop).
        2. Retains explicit ownership of cleanup tasks; awaits them fully even under cancellation.
        3. Never silently discards browser-close or driver-stop failures.
        4. Persists any teardown errors on self.cleanup_error and propagates them.
        """
        cleanup_errors = []
        cancellation_encountered = run_cancelled

        # Step 1: Close browser if launched
        if browser is not None:
            try:
                b_exc, b_canc = await self._run_shielded_step(browser.close(), timeout=15)
                if b_canc:
                    cancellation_encountered = True
                if b_exc is not None:
                    cleanup_errors.append(b_exc)
            except BaseException as be:
                cleanup_errors.append(be)

        # Step 2: Stop Playwright driver (MUST run even if Step 1 failed)
        if p is not None:
            try:
                p_exc, p_canc = await self._run_shielded_step(p.stop(), timeout=15)
                if p_canc:
                    cancellation_encountered = True
                if p_exc is not None:
                    cleanup_errors.append(p_exc)
            except BaseException as be:
                cleanup_errors.append(be)
            finally:
                conn = getattr(p, "_connection", None)
                if conn is not None and hasattr(conn, "_callbacks"):
                    for cb in list(conn._callbacks.values()):
                        f = getattr(cb, "future", cb)
                        if hasattr(f, "done") and f.done() and not f.cancelled():
                            try:
                                f.exception()
                            except (asyncio.CancelledError, Exception):
                                pass

        if cleanup_errors:
            if len(cleanup_errors) == 1:
                self.cleanup_error = cleanup_errors[0]
            else:
                self.cleanup_error = ExceptionGroup("Multiple teardown failures in engine", cleanup_errors)

        # Failure propagation logic:
        if run_exc is not None:
            if cleanup_errors and hasattr(run_exc, "add_note"):
                run_exc.add_note(f"Teardown also failed: {self.cleanup_error}")
            raise run_exc

        if cleanup_errors:
            raise self.cleanup_error

        if cancellation_encountered:
            raise asyncio.CancelledError("Engine run cancelled during execution or teardown")
