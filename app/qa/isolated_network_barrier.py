"""Phase B2b: Network and Channel Hard Barrier.

Provides a strict, non-configurable network and channel isolation boundary
installed on a disposable BrowserContext before any Page or script is created.
Guarantees zero mutation escapes during bootstrap and absolute network isolation
following lockdown.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator, Optional
from urllib.parse import urljoin, urlsplit


class IsolatedNetworkBarrierError(Exception):
    """Raised when the network barrier encounters a security violation or lifecycle failure."""
    pass


class BarrierState(str, Enum):
    """Lifecycle states of the network barrier session."""
    BOOTSTRAP = "BOOTSTRAP"
    LOCKED_DOWN = "LOCKED_DOWN"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class NetworkBarrierMetrics:
    """Bounded, privacy-safe counts of intercepted network actions."""
    bootstrap_allowed: int
    bootstrap_mutations_blocked: int
    cross_origin_blocked: int
    lockdown_blocked: int


_INIT_SCRIPT_CHANNELS_BLOCK = """
(() => {
    const makeBlockedConstructor = (name) => {
        function BlockedConstructor() {
            throw new Error(`${name} creation blocked by IsolatedNetworkBarrier`);
        }
        BlockedConstructor.prototype = {};
        return BlockedConstructor;
    };

    const blockProperty = (obj, name) => {
        if (!obj) return;
        try {
            Object.defineProperty(obj, name, {
                value: makeBlockedConstructor(name),
                writable: false,
                configurable: false
            });
        } catch (e) {
            try {
                obj[name] = makeBlockedConstructor(name);
            } catch (e2) {}
        }
    };

    const targets = [
        window,
        (typeof globalThis !== 'undefined' ? globalThis : null)
    ];

    const blockedNames = ['WebSocket', 'Worker', 'SharedWorker', 'EventSource', 'WebTransport'];

    for (const target of targets) {
        if (!target) continue;
        for (const name of blockedNames) {
            blockProperty(target, name);
        }
    }
})();
"""
 
 
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


async def _fail_closed_cleanup(context: Any, *, timeout: float = 2.0) -> bool:
    """Independently set offline and dispose of a context when barrier installation or operation fails.

    Returns True if context closure was successfully completed.
    Implements strictly bounded cleanup where no cancellation-resistant operation can hold cleanup
    indefinitely. Always attempts context closure if offline activation stalls, fails, or times out.
    Preserves cancellation semantics. If closure cannot be verified, reports explicit terminal failure
    requiring verified context/browser owner disposal.
    """
    if context is None:
        return False

    loop = asyncio.get_running_loop()
    start_time = loop.time()
    effective_timeout = max(0.001, timeout)
    total_deadline = start_time + effective_timeout
    caller_cancelled = False

    # Allocate time budget: offline activation gets at most 40% of the timeout,
    # reserving at least 60% for context closure.
    offline_budget = max(0.001, effective_timeout * 0.4)
    offline_deadline = start_time + offline_budget

    # 1. Bounded offline activation
    try:
        set_offline_fn = getattr(context, "set_offline", None)
        if callable(set_offline_fn):
            res = set_offline_fn(True)
            if asyncio.iscoroutine(res):
                offline_task = asyncio.create_task(res)
                _track_cleanup_task(offline_task)

                while not offline_task.done():
                    remaining = offline_deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        await asyncio.wait_for(asyncio.shield(offline_task), timeout=remaining)
                    except (asyncio.TimeoutError, TimeoutError):
                        break
                    except asyncio.CancelledError:
                        caller_cancelled = True
                    except Exception:
                        break

                if not offline_task.done():
                    offline_task.cancel()
                    # Strictly bounded cancellation wait to prevent cancellation-resistant tasks from stalling cleanup
                    cancel_bound = min(0.005, max(0.0, offline_deadline - loop.time()))
                    if cancel_bound > 0:
                        try:
                            await asyncio.wait_for(asyncio.shield(offline_task), timeout=cancel_bound)
                        except BaseException:
                            pass
    except BaseException:
        pass

    # 2. Bounded context closure - ALWAYS attempted
    disposed = False
    close_error: Optional[Exception] = None
    try:
        close_fn = getattr(context, "close", None)
        if callable(close_fn):
            res = close_fn()
            if asyncio.iscoroutine(res):
                close_task = asyncio.create_task(res)
                _track_cleanup_task(close_task)

                while not close_task.done():
                    remaining = total_deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        await asyncio.wait_for(asyncio.shield(close_task), timeout=remaining)
                    except (asyncio.TimeoutError, TimeoutError):
                        break
                    except asyncio.CancelledError:
                        caller_cancelled = True
                    except Exception as e:
                        close_error = e
                        break

                if not close_task.done():
                    close_task.cancel()
                    # Strictly bounded cancellation wait so cancellation-resistant close operations cannot stall cleanup
                    cancel_bound = min(0.005, max(0.0005, effective_timeout * 0.05))
                    try:
                        await asyncio.wait_for(asyncio.shield(close_task), timeout=cancel_bound)
                    except BaseException:
                        pass

                if not close_task.done():
                    close_error = TimeoutError(f"Context close did not complete within {timeout}s timeout")
                elif close_task.cancelled():
                    close_error = RuntimeError("Context close was cancelled")
                elif close_task.exception() is not None:
                    close_error = close_task.exception()
                else:
                    disposed = True
            else:
                disposed = True
        else:
            return False
    except BaseException as e:
        if isinstance(e, Exception):
            close_error = e

    if not disposed:
        raise IsolatedNetworkBarrierError(
            f"Context closure failed terminally ({close_error}); verified context-owner disposal required"
        )

    if caller_cancelled:
        raise asyncio.CancelledError()

    return disposed


def _extract_origin(url: str) -> Optional[str]:
    """Extract and normalize scheme + host + effective port from an absolute URL string."""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https") or not parsed.hostname:
            return None
        host = parsed.hostname.lower()
        if ":" in host and not (host.startswith("[") and host.endswith("]")):
            formatted_host = f"[{host}]"
        else:
            formatted_host = host
        port = parsed.port
        if port is not None:
            if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
                netloc = formatted_host
            else:
                netloc = f"{formatted_host}:{port}"
        else:
            netloc = formatted_host
        return f"{scheme}://{netloc}"
    except Exception:
        return None


def _normalize_bootstrap_origin(origin: str) -> str:
    """Strictly validate and normalize an approved bootstrap HTTP(S) origin."""
    if not isinstance(origin, str) or not origin.strip():
        raise IsolatedNetworkBarrierError("bootstrap_origin must be a non-empty string")
    trimmed = origin.strip()
    try:
        parsed = urlsplit(trimmed)
    except Exception:
        raise IsolatedNetworkBarrierError("Invalid bootstrap_origin") from None

    if parsed.scheme.lower() not in ("http", "https"):
        raise IsolatedNetworkBarrierError("bootstrap_origin scheme must be http or https")
    if not parsed.hostname:
        raise IsolatedNetworkBarrierError("bootstrap_origin must include a valid host")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise IsolatedNetworkBarrierError("bootstrap_origin must not contain path, query, or fragment")
    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise IsolatedNetworkBarrierError("bootstrap_origin must not contain user credentials or userinfo")

    normalized = _extract_origin(trimmed)
    if not normalized:
        raise IsolatedNetworkBarrierError("Failed to normalize bootstrap_origin")
    return normalized


class IsolatedNetworkBarrierSession:
    """Active session managing the irreversible lockdown lifecycle and barrier state."""

    def __init__(self, context: Any, bootstrap_origin: str) -> None:
        self._context: Optional[Any] = context
        self._bootstrap_origin: str = bootstrap_origin
        self._state: BarrierState = BarrierState.BOOTSTRAP
        self._bootstrap_allowed: int = 0
        self._bootstrap_mutations_blocked: int = 0
        self._cross_origin_blocked: int = 0
        self._lockdown_blocked: int = 0
        self._inflight_count: int = 0
        self._all_drained: asyncio.Event = asyncio.Event()
        self._all_drained.set()
        self._lockdown_lock: asyncio.Lock = asyncio.Lock()
        self._lockdown_initiated: bool = False
        self._lockdown_completed: bool = False
        self._lockdown_error: Optional[Exception] = None

    @property
    def state(self) -> BarrierState:
        return self._state

    @property
    def bootstrap_origin(self) -> str:
        return self._bootstrap_origin

    @property
    def inflight_count(self) -> int:
        return self._inflight_count

    @property
    def metrics(self) -> NetworkBarrierMetrics:
        return NetworkBarrierMetrics(
            bootstrap_allowed=self._bootstrap_allowed,
            bootstrap_mutations_blocked=self._bootstrap_mutations_blocked,
            cross_origin_blocked=self._cross_origin_blocked,
            lockdown_blocked=self._lockdown_blocked,
        )

    def _increment_bootstrap_allowed(self) -> None:
        self._bootstrap_allowed += 1

    def _increment_mutation_blocked(self) -> None:
        self._bootstrap_mutations_blocked += 1

    def _increment_cross_origin_blocked(self) -> None:
        self._cross_origin_blocked += 1

    def _increment_lockdown_blocked(self) -> None:
        self._lockdown_blocked += 1

    def _mark_closed(self) -> None:
        self._state = BarrierState.CLOSED

    def _detach_context(self) -> None:
        self._context = None

    def _enter_inflight(self) -> bool:
        """Register entry of an in-flight network request under active bootstrap."""
        if self._state != BarrierState.BOOTSTRAP or self._lockdown_initiated:
            return False
        self._inflight_count += 1
        self._all_drained.clear()
        return True

    def _exit_inflight(self) -> None:
        """Deregister an in-flight network request upon completion or abort."""
        if self._inflight_count > 0:
            self._inflight_count -= 1
        if self._inflight_count == 0:
            self._all_drained.set()

    async def _wait_drained(self) -> None:
        await self._all_drained.wait()

    async def lock_down(self, *, timeout: float = 5.0) -> None:
        """Irreversibly lock down the context, aborting all network traffic and enabling offline mode.

        Guarantees that no concurrent or repeated invocation reports successful lockdown
        unless the complete required transition (offline mode + in-flight request drain)
        has succeeded. Preserves terminal failure behavior.
        """
        if self._state == BarrierState.CLOSED:
            raise IsolatedNetworkBarrierError("Barrier session is already closed")
        if self._state == BarrierState.FAILED:
            msg = str(self._lockdown_error) if self._lockdown_error else "Lockdown previously failed terminally"
            raise IsolatedNetworkBarrierError(f"Lockdown failed terminally: {msg}")
        if self._state == BarrierState.LOCKED_DOWN and self._lockdown_completed:
            return  # Idempotent lock down

        if self._context is None:
            raise IsolatedNetworkBarrierError("Cannot lock down session without active context")

        # Coordinate concurrent and repeated invocations with an asyncio lock
        async with self._lockdown_lock:
            if self._state == BarrierState.CLOSED:
                raise IsolatedNetworkBarrierError("Barrier session is already closed")
            if self._state == BarrierState.FAILED:
                msg = str(self._lockdown_error) if self._lockdown_error else "Lockdown previously failed terminally"
                raise IsolatedNetworkBarrierError(f"Lockdown failed terminally: {msg}")
            if self._state == BarrierState.LOCKED_DOWN and self._lockdown_completed:
                return  # Idempotent lock down

            self._lockdown_initiated = True

            try:
                # 1. Activate browser-level offline mode
                try:
                    await self._context.set_offline(True)
                except Exception:
                    self._state = BarrierState.FAILED
                    self._lockdown_error = IsolatedNetworkBarrierError("Failed to enable offline mode on context")
                    raise self._lockdown_error from None

                # 2. Account for in-flight requests: wait boundedly for in-flight requests to complete/abort
                if self._inflight_count > 0:
                    try:
                        await asyncio.wait_for(self._all_drained.wait(), timeout=timeout)
                    except (asyncio.TimeoutError, TimeoutError):
                        self._state = BarrierState.FAILED
                        self._lockdown_error = IsolatedNetworkBarrierError(
                            f"Lockdown timed out waiting for {self._inflight_count} in-flight request(s) to drain"
                        )
                        raise self._lockdown_error from None
                    except Exception as e:
                        self._state = BarrierState.FAILED
                        self._lockdown_error = IsolatedNetworkBarrierError(f"Lockdown drain failed: {e}")
                        raise self._lockdown_error from None

                self._state = BarrierState.LOCKED_DOWN
                self._lockdown_completed = True
            except asyncio.CancelledError:
                self._state = BarrierState.FAILED
                self._lockdown_error = IsolatedNetworkBarrierError(
                    "Lockdown was cancelled while transition was in progress"
                )
                try:
                    await self._context.set_offline(True)
                except BaseException:
                    pass
                raise

    def __repr__(self) -> str:
        return f"<IsolatedNetworkBarrierSession state={self._state.value}>"

    def __str__(self) -> str:
        return f"IsolatedNetworkBarrierSession({self._state.value})"


class IsolatedNetworkBarrier:
    """Enforces network lockdown and prevents unmonitored communication channels."""

    def __init__(self) -> None:
        """Initialize with zero configurable overrides to prevent replacement of safety policy."""
        pass

    def __repr__(self) -> str:
        return "<IsolatedNetworkBarrier>"

    def __str__(self) -> str:
        return "IsolatedNetworkBarrier"

    @asynccontextmanager
    async def guard(
        self,
        disposable_context: Any,
        *,
        bootstrap_origin: str,
    ) -> AsyncIterator[IsolatedNetworkBarrierSession]:
        """Install channel barriers and network routing onto an empty disposable BrowserContext."""
        session: Optional[IsolatedNetworkBarrierSession] = None
        try:
            if disposable_context is None:
                raise IsolatedNetworkBarrierError("disposable_context must not be None")

            # Verify required Playwright BrowserContext methods exist
            required_methods = ("route", "unroute", "set_offline", "add_init_script")
            for m in required_methods:
                if not callable(getattr(disposable_context, m, None)):
                    raise IsolatedNetworkBarrierError("disposable_context must provide required Playwright methods")

            pages = getattr(disposable_context, "pages", None)
            if pages is None:
                raise IsolatedNetworkBarrierError("disposable_context must provide pages collection")

            pages_collection = pages if isinstance(pages, (list, tuple)) else (pages() if callable(pages) else None)
            if pages_collection is None:
                raise IsolatedNetworkBarrierError("Failed to inspect pages of disposable_context")

            if len(pages_collection) > 0:
                raise IsolatedNetworkBarrierError("Context must have zero pages before barrier installation")

            normalized_origin = _normalize_bootstrap_origin(bootstrap_origin)
            session = IsolatedNetworkBarrierSession(disposable_context, normalized_origin)

            # 1. Block WebSocket, Worker, SharedWorker, EventSource, WebTransport before page creation
            try:
                await disposable_context.add_init_script(_INIT_SCRIPT_CHANNELS_BLOCK)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise IsolatedNetworkBarrierError("Failed to install channel blocking init script") from None

            # 2. Context route handler
            async def _handle_route(route: Any) -> None:
                if not session._enter_inflight():
                    session._increment_lockdown_blocked()
                    try:
                        await route.abort()
                    except Exception:
                        pass
                    return

                try:
                    req = route.request
                    method = getattr(req, "method", "").upper()

                    # Only GET and HEAD permitted during bootstrap
                    if method not in ("GET", "HEAD"):
                        session._increment_mutation_blocked()
                        try:
                            await route.abort()
                        except Exception:
                            pass
                        return

                    req_url = getattr(req, "url", "")
                    req_origin = _extract_origin(req_url)

                    # Only approved bootstrap origin permitted
                    if req_origin != normalized_origin:
                        session._increment_cross_origin_blocked()
                        try:
                            await route.abort()
                        except Exception:
                            pass
                        return

                    # Follow same-origin redirects with zero auto-redirects to abort any cross-origin hop
                    current_url = req_url
                    current_method = method
                    resp: Any = None
                    max_redirects = 5
                    for _ in range(max_redirects):
                        # Async boundary 1: verify active BOOTSTRAP before issuing fetch
                        if session.state != BarrierState.BOOTSTRAP or session._lockdown_initiated:
                            session._increment_lockdown_blocked()
                            try:
                                await route.abort()
                            except Exception:
                                pass
                            return

                        try:
                            resp = await route.fetch(
                                url=current_url,
                                method=current_method,
                                max_redirects=0,
                            )
                        except Exception:
                            try:
                                await route.abort()
                            except Exception:
                                pass
                            return

                        # Async boundary 2: re-verify active BOOTSTRAP after fetch returns
                        if session.state != BarrierState.BOOTSTRAP or session._lockdown_initiated:
                            session._increment_lockdown_blocked()
                            try:
                                await route.abort()
                            except Exception:
                                pass
                            return

                        status = getattr(resp, "status", 200)
                        if status in (301, 302, 303, 307, 308):
                            headers = getattr(resp, "headers", {})
                            location = headers.get("location") or headers.get("Location")
                            if not location or not isinstance(location, str):
                                try:
                                    await route.abort()
                                except Exception:
                                    pass
                                return

                            location = location.strip()
                            if not location:
                                try:
                                    await route.abort()
                                except Exception:
                                    pass
                                return

                            next_url = urljoin(current_url, location)
                            if _extract_origin(next_url) != normalized_origin:
                                session._increment_cross_origin_blocked()
                                try:
                                    await route.abort()
                                except Exception:
                                    pass
                                return

                            if status == 303:
                                current_method = "GET"

                            current_url = next_url
                        else:
                            break

                    # Async boundary 3: re-verify active BOOTSTRAP before fulfill
                    if session.state != BarrierState.BOOTSTRAP or session._lockdown_initiated:
                        session._increment_lockdown_blocked()
                        try:
                            await route.abort()
                        except Exception:
                            pass
                        return

                    # If response resolved to a non-redirect within budget, fulfill it; otherwise abort
                    if resp is not None and getattr(resp, "status", 200) not in (301, 302, 303, 307, 308):
                        session._increment_bootstrap_allowed()
                        try:
                            await route.fulfill(response=resp)
                        except Exception:
                            try:
                                await route.abort()
                            except Exception:
                                pass
                    else:
                        try:
                            await route.abort()
                        except Exception:
                            pass
                finally:
                    session._exit_inflight()

            try:
                await disposable_context.route("**/*", _handle_route)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise IsolatedNetworkBarrierError("Failed to install context route handler") from None

        except (Exception, asyncio.CancelledError) as setup_exc:
            if session is not None:
                session._mark_closed()
                session._detach_context()
            cleanup_error: Optional[Exception] = None
            cleanup_cancelled = False
            try:
                await _fail_closed_cleanup(disposable_context)
            except asyncio.CancelledError:
                cleanup_cancelled = True
            except Exception as e:
                cleanup_error = e

            if cleanup_cancelled:
                raise asyncio.CancelledError()

            if cleanup_error is not None:
                if isinstance(setup_exc, asyncio.CancelledError):
                    raise IsolatedNetworkBarrierError(
                        f"Barrier installation was cancelled and cleanup closure failed terminally ({cleanup_error}); verified context-owner disposal required"
                    ) from setup_exc
                else:
                    raise IsolatedNetworkBarrierError(
                        f"Barrier installation failed ({setup_exc}) and cleanup closure failed terminally ({cleanup_error}); verified context-owner disposal required"
                    ) from setup_exc

            raise setup_exc

        body_failed = False
        teardown_interrupted = False
        teardown_offline_error: Optional[Exception] = None
        try:
            yield session
        except BaseException:
            body_failed = True
            raise
        finally:
            lockdown_failed = (session.state == BarrierState.FAILED)
            session._mark_closed()
            if not lockdown_failed:
                try:
                    await disposable_context.set_offline(True)
                except asyncio.CancelledError:
                    teardown_interrupted = True
                except Exception as e:
                    teardown_offline_error = e

            if session.inflight_count > 0:
                try:
                    await asyncio.wait_for(session._wait_drained(), timeout=2.0)
                except asyncio.CancelledError:
                    teardown_interrupted = True
                except Exception:
                    pass

            # Fail-closed protection is preserved through context disposal.
            # We deliberately do NOT call unroute("**/*") so the deny-all barrier
            # remains permanently active on the disposable context and unrelated
            # context routes are not destroyed.
            session._detach_context()

            if teardown_interrupted:
                # Interrupted safety cleanup: ensure context is fail-closed and propagate cancellation
                cleanup_error: Optional[Exception] = None
                try:
                    await _fail_closed_cleanup(disposable_context)
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    cleanup_error = e

                if cleanup_error is not None:
                    raise IsolatedNetworkBarrierError(
                        f"Teardown was cancelled and context closure failed terminally ({cleanup_error}); verified context-owner disposal required"
                    )
                raise asyncio.CancelledError()
            elif teardown_offline_error is not None:
                cleanup_error: Optional[Exception] = None
                try:
                    await _fail_closed_cleanup(disposable_context)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    cleanup_error = e

                if not body_failed:
                    if cleanup_error is not None:
                        raise IsolatedNetworkBarrierError(
                            f"Teardown offline activation failed ({teardown_offline_error}) and context closure failed terminally ({cleanup_error}); verified context-owner disposal required"
                        ) from teardown_offline_error
                    else:
                        raise IsolatedNetworkBarrierError(
                            f"Teardown offline activation failed ({teardown_offline_error}); context fail-closed and disposed"
                        ) from teardown_offline_error
                else:
                    if cleanup_error is not None:
                        raise IsolatedNetworkBarrierError(
                            f"Body exception occurred and cleanup closure failed terminally ({cleanup_error}); verified context-owner disposal required"
                        )
