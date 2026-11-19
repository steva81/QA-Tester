"""Phase B2c: State Reconstruction & Exact Revalidation.

Reconstructs browser state within an isolated, disposable BrowserContext (B2a),
protected by an irreversible network and channel hard barrier (B2b), and performs
strict, read-only revalidation (A2/A3/A4/B1) without invoking any UI actions.
"""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
import time
from typing import Any, AsyncIterator, Dict, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from app.qa.browser_snapshot_adapter import (
    ReadOnlyBrowserSnapshotAdapter,
    CancellationToken,
)
from app.qa.candidate_intent_factory import CandidateActionIntentFactory
from app.qa.candidate_intent_graph import CandidateActionIntent
from app.qa.execution_preflight import (
    ExecutionPreflightResult,
    PreflightStatus,
    SafeExecutionPreflight,
)
from app.qa.interaction_policy import InteractionSafetyPolicy, RiskClassification
from app.qa.semantic_normalizer import SemanticNormalizer
from app.qa.state_fingerprint import CanonicalStateFingerprint
from app.qa.isolated_execution_context import (
    IsolatedContextError,
    IsolatedExecutionContextFactory,
)
from app.qa.isolated_network_barrier import (
    BarrierState,
    IsolatedNetworkBarrier,
    IsolatedNetworkBarrierError,
    IsolatedNetworkBarrierSession,
    _extract_origin,
    _normalize_bootstrap_origin,
)


class StateReconstructionError(Exception):
    """Raised when state reconstruction or session lifecycle contracts are violated."""
    pass


class ReconstructionStatus:
    """Truthful execution-control statuses for state reconstruction outcomes."""
    REVALIDATED = "REVALIDATED"
    REVALIDATED_WITH_LIMITATIONS = "REVALIDATED_WITH_LIMITATIONS"
    URL_NOT_APPROVED = "URL_NOT_APPROVED"
    BOOTSTRAP_FAILED = "BOOTSTRAP_FAILED"
    UNEXPECTED_NAVIGATION = "UNEXPECTED_NAVIGATION"
    CROSS_ORIGIN_BLOCKED = "CROSS_ORIGIN_BLOCKED"
    LOCKDOWN_FAILED = "LOCKDOWN_FAILED"
    AUTH_LOSS = "AUTH_LOSS"
    STALE_STATE = "STALE_STATE"
    INTENT_NOT_PRESENT = "INTENT_NOT_PRESENT"
    TARGET_NOT_ELIGIBLE = "TARGET_NOT_ELIGIBLE"
    TARGET_CLOSED = "TARGET_CLOSED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


def _sanitize_url_metadata(url: Optional[str]) -> Optional[str]:
    """Sanitize URL for public reporting, strictly removing credentials, query, and fragments.

    Returns a privacy-safe representation: scheme://[host][:port][/path].
    Never retains userinfo (username/password), query parameters (?token=...), or fragments (#hash).
    If the URL cannot be parsed into a valid scheme + host, returns a safe fallback.
    """
    if url is None:
        return None
    trimmed = str(url).strip()
    if not trimmed:
        return ""
    try:
        parsed = urlsplit(trimmed)
        scheme = (parsed.scheme or "http").lower()
        if scheme not in ("http", "https"):
            scheme = "http"

        hostname = parsed.hostname
        if not hostname:
            return "<invalid-url>"

        host_str = hostname.lower()
        if ":" in host_str and not (host_str.startswith("[") and host_str.endswith("]")):
            host_str = f"[{host_str}]"

        port = parsed.port
        if port is not None:
            if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
                netloc = host_str
            else:
                netloc = f"{host_str}:{port}"
        else:
            netloc = host_str

        path = parsed.path or "/"
        return f"{scheme}://{netloc}{path}"
    except Exception:
        return "<invalid-url>"


def _classify_navigation_error(error: Optional[Exception], page_closed: bool = False) -> Tuple[str, str]:
    """Classify navigation failure into a truthful execution-control status and bounded, privacy-safe error message.

    Never interpolates raw exception messages, arbitrary stack traces, or unredacted URLs.
    """
    if page_closed:
        return ReconstructionStatus.BOOTSTRAP_FAILED, "Page was closed during navigation"
    if error is None:
        return ReconstructionStatus.BOOTSTRAP_FAILED, "Controlled navigation failed"

    err_str = str(error).lower()
    if "err_aborted" in err_str or "cross-origin" in err_str:
        return ReconstructionStatus.CROSS_ORIGIN_BLOCKED, "Navigation aborted by barrier policy"
    if "timeout" in err_str or "timed out" in err_str or "err_timed_out" in err_str:
        return ReconstructionStatus.BOOTSTRAP_FAILED, "Navigation timed out"
    if "err_connection_refused" in err_str or "connection refused" in err_str:
        return ReconstructionStatus.BOOTSTRAP_FAILED, "Navigation connection refused"
    if "err_name_not_resolved" in err_str or "name resolution" in err_str:
        return ReconstructionStatus.BOOTSTRAP_FAILED, "Navigation host resolution failed"
    return ReconstructionStatus.BOOTSTRAP_FAILED, "Controlled navigation failed"


def _validate_target_url_auth(url_str: str) -> Tuple[Optional[str], Optional[str]]:
    """Validate target URL for authorization gating.

    Returns (bootstrap_origin, None) on success, or (None, privacy_safe_error_message) on rejection.
    Never returns unredacted raw strings, credentials, or secrets.
    """
    if not isinstance(url_str, str) or not url_str.strip():
        return None, "approved_target_url must be a non-empty string"
    trimmed = url_str.strip()
    try:
        parsed = urlsplit(trimmed)
        if parsed.scheme.lower() not in ("http", "https"):
            return None, "URL scheme must be http or https"
        if not parsed.hostname:
            return None, "URL hostname is required"
        if "@" in parsed.netloc or parsed.username or parsed.password:
            return None, "User credentials forbidden in target URL"
        raw_origin = _extract_origin(trimmed)
        if not raw_origin:
            return None, "Could not extract valid origin from target URL"
        bootstrap_origin = _normalize_bootstrap_origin(raw_origin)
        return bootstrap_origin, None
    except Exception:
        return None, "Target URL format is invalid"


@dataclass(frozen=True)
class StateReconstructionResult:
    """Structured, immutable outcome of state reconstruction and revalidation.

    Preserves privacy: contains zero cookies, raw tokens, credentials, or sensitive page content.
    Tagged as an execution-control outcome; never classified as a customer defect.
    """
    status: str
    is_revalidated: bool
    target_url: str
    reconstructed_url: Optional[str]
    parent_state_fingerprint: Optional[str]
    preflight_result: Optional[ExecutionPreflightResult]
    limitations: Tuple[str, ...]
    error_message: Optional[str] = None
    is_execution_control: bool = True

    def __repr__(self) -> str:
        return (
            f"<StateReconstructionResult status={self.status} "
            f"is_revalidated={self.is_revalidated} "
            f"target_url={self.target_url!r}>"
        )


class ReconstructedStateSession:
    """Encapsulates the active disposable session for verified state reconstruction.

    Enforces strict ownership: page and disposable context handles are accessible
    ONLY while the session is active and verified, and raise StateReconstructionError
    if accessed after disposal or upon revalidation failure.
    """

    def __init__(
        self,
        result: StateReconstructionResult,
        page: Optional[Any],
        disposable_context: Optional[Any],
        barrier_session: Optional[IsolatedNetworkBarrierSession],
    ) -> None:
        self._result = result
        self._page = page
        self._disposable_context = disposable_context
        self._barrier_session = barrier_session
        self._is_disposed = False

    @property
    def result(self) -> StateReconstructionResult:
        return self._result

    @property
    def is_revalidated(self) -> bool:
        return self._result.is_revalidated

    @property
    def is_disposed(self) -> bool:
        return self._is_disposed

    @property
    def page(self) -> Any:
        if self._is_disposed:
            raise StateReconstructionError("Cannot access page on disposed session")
        if not self._result.is_revalidated or self._page is None:
            raise StateReconstructionError(
                f"Cannot access page on unvalidated reconstruction session (status={self._result.status})"
            )
        return self._page

    @property
    def disposable_context(self) -> Any:
        if self._is_disposed or self._disposable_context is None:
            raise StateReconstructionError("Cannot access context on disposed session")
        return self._disposable_context

    @property
    def barrier_session(self) -> IsolatedNetworkBarrierSession:
        if self._is_disposed or self._barrier_session is None:
            raise StateReconstructionError("Cannot access barrier session on disposed session")
        return self._barrier_session

    def _mark_disposed(self) -> None:
        self._is_disposed = True
        self._page = None
        self._disposable_context = None
        self._barrier_session = None

    def __repr__(self) -> str:
        return f"<ReconstructedStateSession status={self._result.status} disposed={self._is_disposed}>"


def _normalize_url_path(url: str) -> Optional[str]:
    """Normalize URL scheme, host, port, path, and query for canonical equality comparison."""
    try:
        parsed = urlsplit(url)
        origin = _extract_origin(url)
        if not origin:
            return None
        path = parsed.path or "/"
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{origin}{path}{query}"
    except Exception:
        return None


def _is_login_path(url: str) -> bool:
    """Detect common login / signin redirect paths indicating authentication loss."""
    try:
        parsed = urlsplit(url)
        path = (parsed.path or "").lower()
        login_segments = ("/login", "/signin", "/sign-in", "/log-in", "/auth", "/authenticate")
        return any(path.startswith(seg) or seg in path for seg in login_segments)
    except Exception:
        return False


_PENDING_CLEANUP_TASKS: set[asyncio.Task] = set()


def _track_cleanup_task(task: asyncio.Task) -> None:
    _PENDING_CLEANUP_TASKS.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _PENDING_CLEANUP_TASKS.discard(t)
        try:
            if not t.cancelled():
                t.exception()
        except BaseException:
            pass

    task.add_done_callback(_on_done)


async def _run_cleanup_op(coro: Any, timeout: float) -> Tuple[bool, Optional[Exception], bool]:
    """Execute a cleanup coroutine boundedly, resilient to caller cancellation and repeated cancellation.

    Returns (completed: bool, error: Optional[Exception], was_cancelled: bool).
    """
    task = asyncio.create_task(coro)
    _track_cleanup_task(task)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.01, timeout)
    caller_cancelled = False
    op_error: Optional[Exception] = None

    while not task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except (asyncio.TimeoutError, TimeoutError):
            break
        except asyncio.CancelledError:
            caller_cancelled = True
        except Exception as e:
            op_error = e
            break

    if not task.done():
        task.cancel()
        cancel_bound = min(0.05, max(0.005, timeout * 0.05))
        cancel_deadline = loop.time() + cancel_bound
        while not task.done():
            remaining = cancel_deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except (asyncio.TimeoutError, TimeoutError):
                break
            except asyncio.CancelledError:
                caller_cancelled = True
            except Exception:
                break

    if not task.done():
        return False, TimeoutError(f"Cleanup operation timed out after {timeout}s"), caller_cancelled
    if task.cancelled():
        return False, RuntimeError("Cleanup operation was cancelled"), caller_cancelled
    if task.exception() is not None:
        return False, task.exception(), caller_cancelled

    return True, op_error, caller_cancelled


def _verify_context_closed(context: Any, close_event: Optional[asyncio.Event]) -> Tuple[bool, bool]:
    """Verify that a disposable BrowserContext has been cleanly closed.

    Returns (verified_closed, evidence_available).
    Uses supported Playwright evidence:
    1. 'close' event handler on context (if registered and fired).
    2. is_closed() method if provided by context.
    3. Browser context membership (context not in browser.contexts).
    """
    if context is None:
        return True, True

    # 1. Check if 'close' event fired
    if close_event is not None and close_event.is_set():
        return True, True

    # 2. Check if context provides is_closed()
    is_closed_fn = getattr(context, "is_closed", None)
    if callable(is_closed_fn):
        try:
            return bool(is_closed_fn()), True
        except Exception:
            return False, True

    # 3. Check browser context membership
    browser = getattr(context, "browser", None)
    if browser is not None:
        contexts_prop = getattr(browser, "contexts", None)
        contexts_list = contexts_prop if isinstance(contexts_prop, (list, tuple)) else (
            contexts_prop() if callable(contexts_prop) else None
        )
        if contexts_list is not None:
            is_member = context in contexts_list
            return (not is_member), True

    # No supported evidence available to confirm closure
    return False, False


class StateReconstructor:
    """Orchestrates Phase B2c state reconstruction and exact revalidation.

    Enforces that:
    1. An explicitly approved target URL and candidate intent are provided.
    2. A disposable BrowserContext is created via B2a (never mutating source_context).
    3. The B2b barrier is installed BEFORE any page is created.
    4. Bootstrap navigation is restricted to same-origin read-only GET/HEAD.
    5. Irreversible B2b lockdown is completed before accepting state.
    6. Fresh A2/A3/A4/B1 verification is executed under lockdown.
    7. All disposable resources are safely and reliably shut down before yielding
       any unsuccessful outcome or upon session context manager exit.
    """

    def __init__(
        self,
        context_factory: Optional[IsolatedExecutionContextFactory] = None,
        barrier: Optional[IsolatedNetworkBarrier] = None,
        preflight: Optional[SafeExecutionPreflight] = None,
        snapshot_adapter: Optional[ReadOnlyBrowserSnapshotAdapter] = None,
        normalizer: Optional[SemanticNormalizer] = None,
    ) -> None:
        self._context_factory = context_factory or IsolatedExecutionContextFactory()
        self._barrier = barrier or IsolatedNetworkBarrier()
        self._preflight = preflight or SafeExecutionPreflight()
        self._snapshot_adapter = snapshot_adapter or getattr(self._preflight, "_snapshot_adapter", None) or ReadOnlyBrowserSnapshotAdapter()
        self._normalizer = normalizer or getattr(self._preflight, "_normalizer", None) or SemanticNormalizer()

    def __repr__(self) -> str:
        return "<StateReconstructor>"

    def __str__(self) -> str:
        return "StateReconstructor"

    @asynccontextmanager
    async def reconstruct(
        self,
        source_context: Any,
        approved_target_url: str,
        candidate_intent: CandidateActionIntent,
        *,
        timeout: float = 15.0,
        wait_until: str = "load",
        lifecycle_tracker: Optional[Dict[str, Any]] = None,
        cancellation_token: Optional[CancellationToken] = None,
        viewport: Optional[Dict[str, int]] = None,
    ) -> AsyncIterator[ReconstructedStateSession]:
        """Asynchronously reconstruct state and revalidate intent within a disposable session.

        Yields a ReconstructedStateSession that holds the locked-down disposable page
        for the duration of the context manager block if revalidation succeeds.
        On any failure, all disposable resources are safely shut down before yielding,
        and accessing handles on failed sessions raises StateReconstructionError.
        """
        # 1. Type validation & authorization gating
        if source_context is None:
            raise StateReconstructionError("source_context must not be None")

        if not isinstance(candidate_intent, CandidateActionIntent):
            raise TypeError(
                f"candidate_intent must be a CandidateActionIntent; received {type(candidate_intent)!r}"
            )

        target_url_clean = str(approved_target_url or "").strip()
        sanitized_target_url = _sanitize_url_metadata(target_url_clean) or "<invalid-url>"

        bootstrap_origin, auth_err = _validate_target_url_auth(target_url_clean)
        if auth_err is not None:
            result = StateReconstructionResult(
                status=ReconstructionStatus.URL_NOT_APPROVED,
                is_revalidated=False,
                target_url=sanitized_target_url,
                reconstructed_url=None,
                parent_state_fingerprint=None,
                preflight_result=None,
                limitations=(),
                error_message=auth_err,
            )
            session = ReconstructedStateSession(result, None, None, None)
            session._mark_disposed()
            yield session
            return

        session: Optional[ReconstructedStateSession] = None
        page: Optional[Any] = None
        disposable_context: Optional[Any] = None
        barrier_session: Optional[IsolatedNetworkBarrierSession] = None
        stack = AsyncExitStack()
        cleaned_up = False
        context_closed_event = asyncio.Event()

        async def _safe_shutdown_resources() -> None:
            nonlocal cleaned_up, page
            if cleaned_up:
                return

            accumulated_cancelled = False
            terminal_error: Optional[Exception] = None

            # 1. Bounded Page Close (if page exists)
            if page is not None:
                is_closed_fn = getattr(page, "is_closed", None)
                page_already_closed = callable(is_closed_fn) and is_closed_fn()
                if not page_already_closed:
                    close_fn = getattr(page, "close", None)
                    if callable(close_fn):
                        try:
                            res = close_fn()
                            if asyncio.iscoroutine(res):
                                _, _, page_cancelled = await _run_cleanup_op(res, timeout=1.5)
                                if page_cancelled:
                                    accumulated_cancelled = True
                        except Exception:
                            pass
                page = None

            # 2. Bounded Stack Teardown (ALWAYS attempted even if page close stalled, failed, or caller cancelled)
            try:
                stack_res = stack.aclose()
                if asyncio.iscoroutine(stack_res):
                    stack_completed, stack_err, stack_cancelled = await _run_cleanup_op(stack_res, timeout=3.0)
                    if stack_cancelled:
                        accumulated_cancelled = True
                    if not stack_completed or stack_err is not None:
                        terminal_error = stack_err or TimeoutError("Stack aclose timed out")
            except Exception as e:
                terminal_error = e

            # 3. Context Closure Verification (ALWAYS verified)
            if disposable_context is not None:
                verified, evidence_available = _verify_context_closed(disposable_context, context_closed_event)
                if not evidence_available or not verified:
                    terminal_error = terminal_error or StateReconstructionError(
                        "Disposable context closure could not be verified; verified context-owner disposal required"
                    )

            if terminal_error is not None:
                # Do NOT mark cleaned_up = True if cleanup terminally failed!
                raise StateReconstructionError(
                    "Terminal cleanup failure during reconstruction teardown; verified context-owner disposal required"
                ) from terminal_error

            # Only mark cleaned_up = True when required teardown and verification completely finish!
            cleaned_up = True

            if accumulated_cancelled:
                raise asyncio.CancelledError()

        try:
            # 2. Enter B2a disposable context and B2b barrier guard
            disposable_context = await stack.enter_async_context(
                self._context_factory.open(source_context, viewport=viewport)
            )

            # Register close listener on disposable_context for verified closure tracking
            on_fn = getattr(disposable_context, "on", None)
            if callable(on_fn):
                try:
                    on_fn("close", lambda *args: context_closed_event.set())
                except Exception:
                    pass

            barrier_session = await stack.enter_async_context(
                self._barrier.guard(disposable_context, bootstrap_origin=bootstrap_origin)
            )

            # 3. Create first Page strictly AFTER barrier guard is active
            try:
                page = await disposable_context.new_page()
            except Exception:
                await _safe_shutdown_resources()
                result = StateReconstructionResult(
                    status=ReconstructionStatus.BOOTSTRAP_FAILED,
                    is_revalidated=False,
                    target_url=sanitized_target_url,
                    reconstructed_url=None,
                    parent_state_fingerprint=None,
                    preflight_result=None,
                    limitations=(),
                    error_message="Failed to create page under barrier",
                )
                session = ReconstructedStateSession(result, None, None, None)
                session._mark_disposed()
                yield session
                return

            # Apply and verify explicit target viewport on reconstructed page before navigation
            if viewport and isinstance(viewport, dict):
                req_w = int(viewport.get("width", 0))
                req_h = int(viewport.get("height", 0))
                if req_w > 0 and req_h > 0:
                    if hasattr(page, "set_viewport_size") and callable(page.set_viewport_size):
                        try:
                            await page.set_viewport_size({"width": req_w, "height": req_h})
                        except Exception:
                            pass
                    vp_size = getattr(page, "viewport_size", None)
                    actual_w = vp_size.get("width") if isinstance(vp_size, dict) else None
                    actual_h = vp_size.get("height") if isinstance(vp_size, dict) else None
                    if actual_w != req_w or actual_h != req_h:
                        await _safe_shutdown_resources()
                        result = StateReconstructionResult(
                            status=ReconstructionStatus.FAILED,
                            is_revalidated=False,
                            target_url=sanitized_target_url,
                            reconstructed_url=None,
                            parent_state_fingerprint=None,
                            preflight_result=None,
                            limitations=("viewport_establishment_failed",),
                            error_message=f"Intended viewport {req_w}x{req_h} could not be established at runtime; got {actual_w}x{actual_h}",
                        )
                        session = ReconstructedStateSession(result, None, None, None)
                        session._mark_disposed()
                        yield session
                        return

            # 4. Controlled navigation to approved target URL using exact URL
            nav_failed = False
            nav_error: Optional[Exception] = None
            try:
                await page.goto(
                    target_url_clean,
                    wait_until=wait_until,
                    timeout=max(1.0, timeout) * 1000,
                )
            except Exception as e:
                nav_failed = True
                nav_error = e

            if nav_failed or page.is_closed():
                nav_status, nav_msg = _classify_navigation_error(nav_error, page.is_closed())
                reconstructed_url = None
                try:
                    if not page.is_closed():
                        reconstructed_url = _sanitize_url_metadata(page.url)
                except Exception:
                    pass

                await _safe_shutdown_resources()

                result = StateReconstructionResult(
                    status=nav_status,
                    is_revalidated=False,
                    target_url=sanitized_target_url,
                    reconstructed_url=reconstructed_url,
                    parent_state_fingerprint=None,
                    preflight_result=None,
                    limitations=(),
                    error_message=nav_msg,
                )
                session = ReconstructedStateSession(result, None, None, None)
                session._mark_disposed()
                yield session
                return

            if viewport and isinstance(viewport, dict) and not nav_failed and not page.is_closed():
                req_w = int(viewport.get("width", 0))
                req_h = int(viewport.get("height", 0))
                if req_w > 0 and req_h > 0:
                    try:
                        css_w = await page.evaluate("() => window.innerWidth")
                        if css_w is not None and abs(int(css_w) - req_w) > 25:
                            await _safe_shutdown_resources()
                            result = StateReconstructionResult(
                                status=ReconstructionStatus.FAILED,
                                is_revalidated=False,
                                target_url=sanitized_target_url,
                                reconstructed_url=_sanitize_url_metadata(page.url),
                                parent_state_fingerprint=None,
                                preflight_result=None,
                                limitations=("viewport_establishment_failed",),
                                error_message=f"Intended CSS viewport {req_w}x{req_h} mismatch; got innerWidth={css_w}",
                            )
                            session = ReconstructedStateSession(result, None, None, None)
                            session._mark_disposed()
                            yield session
                            return
                    except Exception:
                        pass

            # 5. Landing URL & origin verification
            actual_url = page.url
            actual_origin = _extract_origin(actual_url)
            sanitized_actual_url = _sanitize_url_metadata(actual_url)

            if actual_origin != bootstrap_origin:
                await _safe_shutdown_resources()
                result = StateReconstructionResult(
                    status=ReconstructionStatus.UNEXPECTED_NAVIGATION,
                    is_revalidated=False,
                    target_url=sanitized_target_url,
                    reconstructed_url=sanitized_actual_url,
                    parent_state_fingerprint=None,
                    preflight_result=None,
                    limitations=(),
                    error_message="Navigation landed on unapproved origin",
                )
                session = ReconstructedStateSession(result, None, None, None)
                session._mark_disposed()
                yield session
                return

            # Check for unexpected redirect to login page (auth loss)
            norm_target = _normalize_url_path(target_url_clean)
            norm_actual = _normalize_url_path(actual_url)
            if norm_actual != norm_target and _is_login_path(actual_url) and not _is_login_path(target_url_clean):
                await _safe_shutdown_resources()
                result = StateReconstructionResult(
                    status=ReconstructionStatus.AUTH_LOSS,
                    is_revalidated=False,
                    target_url=sanitized_target_url,
                    reconstructed_url=sanitized_actual_url,
                    parent_state_fingerprint=None,
                    preflight_result=None,
                    limitations=(),
                    error_message="Navigation redirected to login interface indicating lost authentication state",
                )
                session = ReconstructedStateSession(result, None, None, None)
                session._mark_disposed()
                yield session
                return

            # 6. Complete irreversible B2b lockdown
            try:
                await barrier_session.lock_down(timeout=5.0)
            except Exception as e:
                await _safe_shutdown_resources()
                err_msg = (
                    "Barrier lockdown timed out waiting for in-flight requests"
                    if isinstance(e, TimeoutError)
                    else "Irreversible barrier lockdown failed"
                )
                result = StateReconstructionResult(
                    status=ReconstructionStatus.LOCKDOWN_FAILED,
                    is_revalidated=False,
                    target_url=sanitized_target_url,
                    reconstructed_url=sanitized_actual_url,
                    parent_state_fingerprint=None,
                    preflight_result=None,
                    limitations=(),
                    error_message=err_msg,
                )
                session = ReconstructedStateSession(result, None, None, None)
                session._mark_disposed()
                yield session
                return

            # Replay preceding safe actions for multi-step bounded sequence under guarded sequence contract
            if hasattr(candidate_intent, "preceding_intents") and candidate_intent.preceding_intents:
                success, err_status, err_msg = await self._replay_preceding_actions_guarded(
                    page=page,
                    disposable_context=disposable_context,
                    preceding_intents=candidate_intent.preceding_intents,
                    cancellation_token=cancellation_token,
                    total_budget_s=timeout,
                )
                if not success:
                    await _safe_shutdown_resources()
                    result = StateReconstructionResult(
                        status=err_status or ReconstructionStatus.BOOTSTRAP_FAILED,
                        is_revalidated=False,
                        target_url=sanitized_target_url,
                        reconstructed_url=sanitized_actual_url,
                        parent_state_fingerprint=None,
                        preflight_result=None,
                        limitations=(),
                        error_message=err_msg or "Failed to replay preceding actions under guarded contract",
                    )
                    session = ReconstructedStateSession(result, None, None, None)
                    session._mark_disposed()
                    yield session
                    return

            # 7. Fresh A2/A3/A4/B1 revalidation under lockdown
            eligibility_contract = {
                "qa_owned": True,
                "manual_visible_page": False,
                "expected_context": disposable_context,
            }

            preflight_res: Optional[ExecutionPreflightResult] = None
            try:
                preflight_res = await self._preflight.preflight(
                    intent=candidate_intent,
                    page=page,
                    eligibility_contract=eligibility_contract,
                )
            except Exception:
                await _safe_shutdown_resources()
                result = StateReconstructionResult(
                    status=ReconstructionStatus.FAILED,
                    is_revalidated=False,
                    target_url=sanitized_target_url,
                    reconstructed_url=sanitized_actual_url,
                    parent_state_fingerprint=None,
                    preflight_result=None,
                    limitations=(),
                    error_message="Preflight revalidation failed",
                )
                session = ReconstructedStateSession(result, None, None, None)
                session._mark_disposed()
                yield session
                return

            # 8. Evaluate revalidation outcome
            is_reval = False
            if preflight_res.status == PreflightStatus.REVALIDATED:
                status = ReconstructionStatus.REVALIDATED
                is_reval = True
            elif preflight_res.status == PreflightStatus.REVALIDATED_WITH_LIMITATIONS:
                status = ReconstructionStatus.REVALIDATED_WITH_LIMITATIONS
                is_reval = True
            elif preflight_res.status == PreflightStatus.STALE_STATE:
                status = ReconstructionStatus.STALE_STATE
            elif preflight_res.status == PreflightStatus.INTENT_NOT_PRESENT:
                status = ReconstructionStatus.INTENT_NOT_PRESENT
            elif preflight_res.status == PreflightStatus.TARGET_CLOSED:
                status = ReconstructionStatus.TARGET_CLOSED
            elif preflight_res.status == PreflightStatus.CANCELLED:
                status = ReconstructionStatus.CANCELLED
            elif preflight_res.status == PreflightStatus.TARGET_NOT_ELIGIBLE:
                status = ReconstructionStatus.TARGET_NOT_ELIGIBLE
            else:
                status = ReconstructionStatus.FAILED

            result = StateReconstructionResult(
                status=status,
                is_revalidated=is_reval,
                target_url=sanitized_target_url,
                reconstructed_url=sanitized_actual_url,
                parent_state_fingerprint=preflight_res.current_parent_state_fingerprint,
                preflight_result=preflight_res,
                limitations=preflight_res.limitations,
                error_message=None,
            )

            # 9. Handle failure vs success branching:
            if not is_reval:
                # Reconstruction failed (e.g. STALE_STATE, INTENT_NOT_PRESENT).
                # Safely shut down all resources BEFORE yielding to caller.
                await _safe_shutdown_resources()
                session = ReconstructedStateSession(
                    result=result,
                    page=None,
                    disposable_context=None,
                    barrier_session=None,
                )
                session._mark_disposed()
                yield session
                return

            # SUCCESS: Yield active session holding page and context
            session = ReconstructedStateSession(
                result=result,
                page=page,
                disposable_context=disposable_context,
                barrier_session=barrier_session,
            )
            yield session

        finally:
            # 10. Guaranteed disposal and ownership cleanup when caller exits (or on exception)
            if session is not None:
                session._mark_disposed()
            try:
                await _safe_shutdown_resources()
                if lifecycle_tracker is not None:
                    lifecycle_tracker["phase"] = "CLEANUP"
                    lifecycle_tracker["cleanup_completed"] = True
            except Exception:
                if lifecycle_tracker is not None:
                    lifecycle_tracker["phase"] = "CLEANUP"
                    lifecycle_tracker["cleanup_failed"] = True
                    lifecycle_tracker["cleanup_completed"] = False
                raise

    async def _replay_preceding_actions_guarded(
        self,
        page: Any,
        disposable_context: Any,
        preceding_intents: Sequence[CandidateActionIntent],
        cancellation_token: Optional[CancellationToken],
        total_budget_s: float,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Replay preceding actions under the guarded sequence contract.

        Guarantees for every preceding action:
        - Fresh snapshot and semantic normalization under isolated barrier.
        - Validate parent state fingerprint (fails closed with STALE_STATE).
        - Verify target control exists and is unique (fails closed on target ambiguity).
        - Enforce visibility, enabled, and SAFE_CANDIDATE risk classification.
        - Enforce positive ephemeral disclosure/tab pattern gate (via GuardedActionExecutor._is_ephemeral_disclosure_control).
        - Enforce supported action type ('click').
        - Resolve target frame strictly without fallback to main page.
        - Robust locator resolution and DOM uniqueness verification.
        - Evaluate element and associated panel state directly on the resolved Playwright locator (no synthetic document.getElementById).
        - Verify element is not already selected/expanded (fails closed if no transition required).
        - Recheck cancellation and remaining budget immediately before click dispatch, clamping click timeout.
        - Bounded transition observation polling observing positive state transition (not generic class/page changes).
        - Verify associated panel state: must exist and be visible after transition.
        - Fail closed without dispatching disallowed clicks.
        """
        from app.qa.guarded_execution import _resolve_target_frame, GuardedActionExecutor

        seq_start = time.monotonic()
        max_budget = min(total_budget_s, 5.0)

        for prec_idx, prec_intent in enumerate(preceding_intents):
            # 1. Budget and cancellation check
            if cancellation_token and cancellation_token.is_cancelled():
                return False, ReconstructionStatus.CANCELLED, "Preceding action replay cancelled"

            remaining_time = max_budget - (time.monotonic() - seq_start)
            if remaining_time <= 0:
                return False, ReconstructionStatus.BOOTSTRAP_FAILED, "Preceding action replay sequence budget exceeded"

            # 2. Fresh A2/A3 read-only capture and normalization
            eligibility = {
                "qa_owned": True,
                "manual_visible_page": False,
                "expected_context": disposable_context,
            }
            try:
                snap = await self._snapshot_adapter.capture_snapshot(
                    page=page,
                    eligibility_contract=eligibility,
                    cancellation_token=cancellation_token,
                )
            except Exception:
                return False, ReconstructionStatus.BOOTSTRAP_FAILED, "Failed to capture snapshot for preceding action"

            if snap.coverage.terminal_status != "COMPLETE":
                return False, ReconstructionStatus.BOOTSTRAP_FAILED, f"Snapshot for preceding action incomplete ({snap.coverage.terminal_status})"

            norm = self._normalizer.normalize_snapshot(snap)
            if norm.terminal_status != "COMPLETE" or norm.semantic_state is None:
                return False, ReconstructionStatus.BOOTSTRAP_FAILED, "Semantic normalization for preceding action incomplete"

            # 3. Validate state fingerprint
            curr_fp = CanonicalStateFingerprint.generate(norm.semantic_state)
            if prec_intent.parent_state_fingerprint and curr_fp != prec_intent.parent_state_fingerprint:
                return False, ReconstructionStatus.STALE_STATE, "Preceding action parent state fingerprint mismatch (stale state)"

            # 4. Locate target control in semantic model
            matching = [c for c in norm.semantic_state.controls if c.control_id == prec_intent.control_fingerprint]
            if not matching:
                return False, ReconstructionStatus.INTENT_NOT_PRESENT, f"Preceding action target control '{prec_intent.control_fingerprint}' not found in current state"
            if len(matching) > 1:
                return False, ReconstructionStatus.BOOTSTRAP_FAILED, f"Preceding action target control '{prec_intent.control_fingerprint}' is ambiguous ({len(matching)} matches)"

            target_ctrl = matching[0]

            # 5. Enforce visibility, enabled, and safety policy
            if not target_ctrl.visibility or not target_ctrl.enabled:
                return False, ReconstructionStatus.TARGET_NOT_ELIGIBLE, "Preceding action target control is not visible or disabled"

            risk = InteractionSafetyPolicy.classify(target_ctrl)
            if risk != RiskClassification.SAFE_CANDIDATE:
                return False, ReconstructionStatus.TARGET_NOT_ELIGIBLE, f"Preceding action safety classification is {risk.value}; only SAFE_CANDIDATE allowed"

            action_type = CandidateActionIntentFactory._action_type_for_role(target_ctrl.accessible_role)
            if action_type != "click" or action_type != prec_intent.expected_action_type:
                return False, ReconstructionStatus.BOOTSTRAP_FAILED, f"Preceding action role or action type mismatch ({action_type!r} vs {prec_intent.expected_action_type!r})"

            # 6. Resolve target frame strictly without fallback
            if ":" in target_ctrl.control_id:
                frame_path, raw_id = target_ctrl.control_id.split(":", 1)
            else:
                frame_path = "0"
                raw_id = target_ctrl.control_id

            target_frame, frame_err = _resolve_target_frame(page, frame_path)
            if target_frame is None or frame_err is not None:
                return False, ReconstructionStatus.BOOTSTRAP_FAILED, f"Preceding action frame '{frame_path}' could not be resolved: {frame_err}"

            # 7. Robust target element resolution in target frame
            if raw_id and not raw_id.startswith("auto-"):
                escaped_id = raw_id.replace("\\", "\\\\").replace('"', '\\"')
                locator = target_frame.locator(f'[id="{escaped_id}"]')
            else:
                locator = target_frame.get_by_role(
                    target_ctrl.accessible_role,
                    name=target_ctrl.accessible_name,
                    exact=True,
                )

            try:
                dom_count = await locator.count()
            except Exception:
                dom_count = 0

            if dom_count == 0:
                return False, ReconstructionStatus.INTENT_NOT_PRESENT, f"Preceding action target element not found in DOM for id '{raw_id}'"
            if dom_count > 1:
                return False, ReconstructionStatus.BOOTSTRAP_FAILED, f"Preceding action target element is ambiguous in DOM ({dom_count} elements)"

            # Read pre-action DOM metadata directly on the resolved locator
            js_read_meta = """(el) => {
                const controlsId = el.getAttribute('aria-controls') || el.getAttribute('data-modal-target');
                let panelVisible = null;
                let panelExists = false;
                let isDialog = false;
                let dialogIsOpen = false;
                let dialogIsModal = false;

                const hasPopup = el.getAttribute('aria-haspopup');
                const isMenuTrigger = (hasPopup === 'menu' || hasPopup === 'true' || el.getAttribute('data-toggle') === 'dropdown');

                if (controlsId) {
                    const root = el.getRootNode ? el.getRootNode() : document;
                    const panel = (root && root.getElementById) ? root.getElementById(controlsId) : document.getElementById(controlsId);
                    if (panel) {
                        panelExists = true;
                        const style = window.getComputedStyle(panel);
                        isDialog = (panel.tagName.toLowerCase() === 'dialog' || panel.getAttribute('role') === 'dialog');
                        dialogIsOpen = isDialog && (panel.hasAttribute('open') || panel.open);
                        try {
                            dialogIsModal = isDialog && (typeof panel.matches === 'function' ? panel.matches(':modal') : dialogIsOpen);
                        } catch(e) {
                            dialogIsModal = dialogIsOpen;
                        }
                        panelVisible = (isDialog && dialogIsOpen) || (!panel.hidden && style.display !== 'none' && style.visibility !== 'hidden');
                    }
                }
                if (hasPopup === 'dialog' || el.hasAttribute('data-modal-target') || el.getAttribute('data-toggle') === 'modal') {
                    isDialog = true;
                }
                const anyModal = Array.from(document.querySelectorAll('dialog')).some(d => {
                    try { return d.open && (typeof d.matches === 'function' ? d.matches(':modal') : true); }
                    catch(e) { return d.open; }
                });

                return {
                    tagName: el.tagName.toLowerCase(),
                    role: el.getAttribute('role') || el.tagName.toLowerCase(),
                    ariaExpanded: el.getAttribute('aria-expanded'),
                    hasAriaExpanded: el.hasAttribute('aria-expanded'),
                    ariaSelected: el.getAttribute('aria-selected'),
                    hasAriaSelected: el.hasAttribute('aria-selected'),
                    hasAriaControls: !!controlsId,
                    controlsId: controlsId,
                    panelExists: panelExists,
                    panelVisible: panelVisible,
                    isDialog: isDialog,
                    dialogIsOpen: dialogIsOpen || anyModal,
                    dialogIsModal: dialogIsModal || anyModal,
                    isMenuTrigger: isMenuTrigger,
                };
            }"""

            pre_meta = None
            try:
                pre_meta = await locator.evaluate(js_read_meta)
            except Exception:
                pass

            # 7b. Enforce positive ephemeral disclosure/tab pattern gate matching main executor
            if not GuardedActionExecutor._is_ephemeral_disclosure_control(target_ctrl, dom_meta=pre_meta):
                return False, ReconstructionStatus.TARGET_NOT_ELIGIBLE, f"Preceding action target control '{target_ctrl.control_id}' is an unsupported ordinary control, not an ephemeral disclosure or tab"

            # 7c. Transition prerequisite check: reject already active controls
            is_dialog = bool(pre_meta and pre_meta.get("isDialog"))
            is_tab = target_ctrl.accessible_role == "tab" or bool(pre_meta and pre_meta.get("hasAriaSelected"))
            is_menu = bool(pre_meta and pre_meta.get("isMenuTrigger"))

            if is_dialog:
                if pre_meta and pre_meta.get("dialogIsOpen"):
                    return False, ReconstructionStatus.TARGET_NOT_ELIGIBLE, f"Preceding dialog trigger '{target_ctrl.control_id}' dialog is already open; no positive transition required"
            elif is_tab:
                if pre_meta and pre_meta.get("ariaSelected") == "true":
                    return False, ReconstructionStatus.TARGET_NOT_ELIGIBLE, f"Preceding tab '{target_ctrl.control_id}' is already selected; no positive transition required"
            else:
                if pre_meta and pre_meta.get("ariaExpanded") == "true":
                    return False, ReconstructionStatus.TARGET_NOT_ELIGIBLE, f"Preceding disclosure '{target_ctrl.control_id}' is already expanded; no positive transition required"

            # 8. Recheck cancellation and remaining budget IMMEDIATELY before dispatch
            if cancellation_token and cancellation_token.is_cancelled():
                return False, ReconstructionStatus.CANCELLED, "Preceding action cancelled immediately before dispatch"

            remaining_time = max_budget - (time.monotonic() - seq_start)
            if remaining_time <= 0:
                return False, ReconstructionStatus.BOOTSTRAP_FAILED, "Sequence budget exhausted before click dispatch"

            click_timeout_ms = max(50, min(1500, int(remaining_time * 1000)))

            # Dispatch guarded action
            try:
                await locator.click(timeout=click_timeout_ms)
            except Exception as e:
                return False, ReconstructionStatus.BOOTSTRAP_FAILED, f"Failed to dispatch preceding action click: {e}"

            # 9. Bounded transition observation polling
            transition_observed = False
            remaining_obs_time = min(1.0, max_budget - (time.monotonic() - seq_start))
            if remaining_obs_time <= 0:
                return False, ReconstructionStatus.BOOTSTRAP_FAILED, "Sequence budget exhausted before transition observation"

            poll_start = time.monotonic()
            while (time.monotonic() - poll_start) < remaining_obs_time:
                if cancellation_token and cancellation_token.is_cancelled():
                    return False, ReconstructionStatus.CANCELLED, "Cancelled while observing preceding action transition"

                await asyncio.sleep(0.05)
                try:
                    post_meta = await locator.evaluate(js_read_meta)
                    if post_meta:
                        if is_dialog:
                            if post_meta.get("dialogIsOpen"):
                                transition_observed = True
                                break
                        elif is_tab:
                            # Required: aria-selected becomes true
                            if post_meta.get("ariaSelected") == "true":
                                if post_meta.get("hasAriaControls"):
                                    if post_meta.get("panelExists") and post_meta.get("panelVisible"):
                                        transition_observed = True
                                        break
                                else:
                                    transition_observed = True
                                    break
                        elif is_menu:
                            if post_meta.get("ariaExpanded") == "true" or post_meta.get("panelVisible"):
                                transition_observed = True
                                break
                        else:
                            # Required: aria-expanded becomes true
                            if post_meta.get("ariaExpanded") == "true":
                                if post_meta.get("hasAriaControls"):
                                    if post_meta.get("panelExists") and post_meta.get("panelVisible"):
                                        transition_observed = True
                                        break
                                else:
                                    transition_observed = True
                                    break
                except Exception:
                    pass

            if not transition_observed:
                return False, ReconstructionStatus.BOOTSTRAP_FAILED, "Expected state transition or associated panel state not observed after preceding action within timeout budget"

        return True, None, None

    async def revalidate(
        self,
        source_context: Any,
        approved_target_url: str,
        candidate_intent: CandidateActionIntent,
        *,
        timeout: float = 15.0,
        wait_until: str = "load",
        viewport: Optional[Dict[str, int]] = None,
    ) -> StateReconstructionResult:
        """Convenience method to execute full reconstruction and revalidation, returning an immutable result.

        Guarantees that all disposable contexts, pages, and barrier sessions
        are fully closed and disposed before returning.
        """
        async with self.reconstruct(
            source_context=source_context,
            approved_target_url=approved_target_url,
            candidate_intent=candidate_intent,
            timeout=timeout,
            wait_until=wait_until,
            viewport=viewport,
        ) as session:
            return session.result
