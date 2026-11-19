from app.paths import chromium_launch_args
import asyncio
import threading
from playwright.async_api import async_playwright
from urllib.parse import urlparse
import time

class InteractiveSession:
    def __init__(self, project_id, start_url):
        self.project_id = project_id
        self.start_url = start_url
        from app.utils import get_canonical_origin
        self.target_origin = get_canonical_origin(start_url)
        self.loop = None
        self.thread = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.collected_urls = set()
        self.is_running = False
        self._stop_event = None
        self._ready_event = None
        self._error = None
        self.lock = threading.Lock()
        self.qa_tasks = set()
        self.state = 'STOPPED'
        self._context_closed = False

    def start(self):
        with self.lock:
            if self.is_running:
                return
            self.is_running = True
            self.state = 'STARTING'
            self._context_closed = False
            self._stop_event = threading.Event()
            self._ready_event = threading.Event()
            self._error = None
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
            
        if not self._ready_event.wait(timeout=45):
            self.stop()
            raise Exception("Browser startup timed out")
        if self._error:
            self.stop()
            raise Exception(f"Browser startup failed: {self._error}")

    def _run_loop(self):
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._init_playwright())
        except Exception as e:
            self.state = 'FAILED'
            self.is_running = False
            self._error = str(e)
            self._ready_event.set()
            try:
                self.loop.run_until_complete(self._cleanup_playwright())
            except Exception:
                pass
            self.loop.close()
            with self.lock:
                self.state = 'STOPPED'
                self.is_running = False
            return
            
        def context_closed(*args):
            self._context_closed = True
            self.state = 'CLOSED'
        self.context.on('close', context_closed)
        self.state = 'READY'
        self._ready_event.set()
        
        async def keep_alive():
            while not self._stop_event.is_set():
                await asyncio.sleep(0.5)
        
        try:
            self.loop.run_until_complete(keep_alive())
        finally:
            try:
                pending = set(self.qa_tasks)
                for task in pending:
                    if not task.cancelling():
                        task.cancel()
                if pending:
                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self.loop.run_until_complete(self._cleanup_playwright())
                pending = asyncio.all_tasks(self.loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            self.loop.close()
            with self.lock:
                self.state = 'STOPPED'
                self.is_running = False

    async def _init_playwright(self):
        self.playwright = await async_playwright().start()
        # Keep Chromium's normal popup policy: scripts in unattended QA pages
        # must not activate new tabs/windows without a human gesture.
        self.browser = await self.playwright.chromium.launch(
            headless=False,
            args=chromium_launch_args(),
            chromium_sandbox=True,
            ignore_default_args=['--disable-popup-blocking'],
        )
        self.context = await self.browser.new_context(ignore_https_errors=True, service_workers='block', no_viewport=True)
        self.page = await self.context.new_page()
        await self.page.goto(self.start_url)

    async def _cleanup_playwright(self):
        try:
            if self.context: await self.context.close()
        except: pass
        try:
            if self.browser: await self.browser.close()
        except: pass
        try:
            if self.playwright: await self.playwright.stop()
        except: pass

    def stop(self, timeout=5):
        with self.lock:
            self.is_running = False
            self.state = 'STOPPED'
            if self._stop_event:
                self._stop_event.set()
            if self.loop and self.loop.is_running():
                def cancel_operations():
                    # Keep Playwright's transport alive until browser cleanup.
                    for task in list(self.qa_tasks):
                        if not task.cancelling():
                            task.cancel()
                self.loop.call_soon_threadsafe(cancel_operations)
            thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if thread and thread.is_alive():
            with self.lock:
                self.state = 'STOPPING'

    def is_ready(self):
        return bool(self.is_running and self.state == 'READY' and self.loop and
                    self.loop.is_running() and not self.loop.is_closed() and
                    self.context and not self._context_closed and self.browser and
                    self.browser.is_connected() and self.page and not self.page.is_closed() and self._stop_event and
                    not self._stop_event.is_set())

    def get_current_url(self):
        if not self.loop or not self.page:
            return None
        future = asyncio.run_coroutine_threadsafe(self._get_url(), self.loop)
        try:
            return future.result(timeout=2)
        except Exception:
            return None

    async def _get_url(self):
        if self.page and not self.page.is_closed():
            return self.page.url
        return None

    def add_collected_url(self, url):
        from app.utils import get_canonical_origin
        from urllib.parse import urlparse
        origin = get_canonical_origin(url)
        if origin != self.target_origin:
            return list(self.collected_urls)
        parsed = urlparse(url)
        path = parsed.path.rstrip('/') or '/'
        norm_url = parsed._replace(fragment="", path=path).geturl()
        self.collected_urls.add(norm_url)
        return list(self.collected_urls)

    def clear_collected_urls(self):
        self.collected_urls.clear()
        return []

sessions = {}
session_lock = threading.Lock()

def get_session(project_id):
    pid = str(project_id)
    with session_lock:
        return sessions.get(pid)

def start_session(project_id, start_url):
    pid = str(project_id)
    with session_lock:
        existing = sessions.get(pid)
        if existing:
            thread_alive = bool(existing.thread and existing.thread.is_alive())
            if thread_alive:
                if existing.state == 'STOPPING':
                    raise RuntimeError("Previous interactive session is still stopping")
                if existing.is_running:
                    return existing
                raise RuntimeError("Interactive session is already active or starting")
            else:
                del sessions[pid]

        session = InteractiveSession(project_id, start_url)
        sessions[pid] = session
    try:
        session.start()
        return session
    except Exception as e:
        with session_lock:
            if sessions.get(pid) is session:
                if not (session.thread and session.thread.is_alive()):
                    del sessions[pid]
                else:
                    with session.lock:
                        session.state = 'STOPPING'
        raise e

def stop_session(project_id):
    pid = str(project_id)
    with session_lock:
        session = sessions.get(pid)
    if not session:
        return True
    session.stop()
    with session_lock:
        if session.thread and session.thread.is_alive():
            with session.lock:
                session.state = 'STOPPING'
            return False
        if sessions.get(pid) is session:
            del sessions[pid]
        return True
