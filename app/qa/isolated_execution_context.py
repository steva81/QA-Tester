"""Phase B2a: Disposable Isolated BrowserContext Lifecycle & Authentication Cloning.

Provides a strict, minimal production boundary for creating short-lived,
disposable BrowserContexts cloned from an authenticated source BrowserContext.
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional


class IsolatedContextError(Exception):
    """Raised when an isolated execution context cannot be created or safely managed."""
    pass


class IsolatedExecutionContextFactory:
    """Factory for managing disposable, isolated BrowserContext lifecycles."""

    def __init__(self) -> None:
        """Initialize with zero configurable overrides to enforce production safety."""
        pass

    def __repr__(self) -> str:
        return "<IsolatedExecutionContextFactory>"

    def __str__(self) -> str:
        return "IsolatedExecutionContextFactory"

    @asynccontextmanager
    async def open(
        self,
        source_context: Any,
        viewport: Optional[dict] = None,
    ) -> AsyncIterator[Any]:
        """Asynchronous context manager yielding a genuinely distinct, disposable BrowserContext.

        Extracts authenticated storage state in memory, instantiates a separate
        BrowserContext with service workers blocked and optional explicit target viewport,
        and guarantees teardown upon exit.
        """
        if source_context is None:
            raise IsolatedContextError("source_context must not be None")

        storage_state_fn = getattr(source_context, "storage_state", None)
        if not callable(storage_state_fn):
            raise IsolatedContextError("source_context must provide a callable storage_state method")

        browser = getattr(source_context, "browser", None)
        if browser is None:
            raise IsolatedContextError("source_context must be associated with an active Browser instance")

        new_context_fn = getattr(browser, "new_context", None)
        if not callable(new_context_fn):
            raise IsolatedContextError("source_context.browser must provide a callable new_context method")

        # 1. Read-only in-memory storage state export
        auth_state: Optional[dict] = None
        try:
            auth_state = await source_context.storage_state()
        except Exception:
            raise IsolatedContextError("Failed to export storage_state from source_context") from None

        if not isinstance(auth_state, dict):
            auth_state = None
            raise IsolatedContextError("Exported storage_state must be a dictionary")

        # 2. Instantiate disposable context from the same browser instance
        disposable_context: Any = None
        context_kwargs: dict = {
            "storage_state": auth_state,
            "service_workers": "block",
            "ignore_https_errors": True,
        }
        if viewport and isinstance(viewport, dict):
            w = viewport.get("width")
            h = viewport.get("height")
            if w and h:
                context_kwargs["viewport"] = {"width": int(w), "height": int(h)}

        try:
            disposable_context = await browser.new_context(**context_kwargs)
        except Exception:
            raise IsolatedContextError("Failed to create disposable execution context") from None
        finally:
            # Immediate in-memory clearing of auth state reference
            auth_state = None

        # 3. Fail closed if disposable context is not a distinct object
        if disposable_context is None or disposable_context is source_context:
            if disposable_context is not None and disposable_context is not source_context:
                try:
                    await disposable_context.close()
                except Exception:
                    pass
            raise IsolatedContextError("Disposable context must be a distinct BrowserContext from source_context")

        # 4. Lifecycle management with guaranteed teardown
        try:
            yield disposable_context
        finally:
            if disposable_context is not None:
                try:
                    await disposable_context.close()
                except Exception:
                    pass
