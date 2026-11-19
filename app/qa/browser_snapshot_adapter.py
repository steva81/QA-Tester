import urllib.parse
from typing import Dict, Any, Optional
from app.qa.browser_snapshot_model import BrowserSnapshot, SnapshotCoverage, FrameSnapshot, ControlSnapshot

class CancellationToken:
    def __init__(self, cancelled=False):
        self._cancelled = cancelled

    def is_cancelled(self) -> bool:
        return self._cancelled

    def cancel(self):
        self._cancelled = True

class ReadOnlyBrowserSnapshotAdapter:
    def __init__(self, max_controls_per_frame: int = 500, max_frames: int = 10, max_text_length: int = 200):
        self.max_controls_per_frame = max_controls_per_frame
        self.max_frames = max_frames
        self.max_text_length = max_text_length
        self.redaction_string = "***"

    async def capture_snapshot(self, page: Any, eligibility_contract: Dict[str, Any], cancellation_token: Optional[CancellationToken] = None) -> BrowserSnapshot:
        if cancellation_token and cancellation_token.is_cancelled():
            return BrowserSnapshot(page_url="", main_frame=None, coverage=SnapshotCoverage("CANCELLED", ()))

        if not eligibility_contract or not eligibility_contract.get("qa_owned", False):
            return BrowserSnapshot(page_url="", main_frame=None, coverage=SnapshotCoverage("NOT_ELIGIBLE", ()))

        if eligibility_contract.get("manual_visible_page", False):
            return BrowserSnapshot(page_url="", main_frame=None, coverage=SnapshotCoverage("MANUAL_PAGE_EXCLUDED", ()))

        expected_context = eligibility_contract.get("expected_context")
        if expected_context is None or page.context != expected_context:
            return BrowserSnapshot(page_url="", main_frame=None, coverage=SnapshotCoverage("WRONG_CONTEXT", ()))

        if page.is_closed():
            return BrowserSnapshot(page_url="", main_frame=None, coverage=SnapshotCoverage("TARGET_CLOSED", ()))

        try:
            page_url = page.url
        except Exception:
            return BrowserSnapshot(page_url="", main_frame=None, coverage=SnapshotCoverage("TARGET_CLOSED", ()))

        main_origin = self._extract_origin(page_url)
        if main_origin == "null":
            return BrowserSnapshot(
                page_url=page_url[:self.max_text_length],
                main_frame=None,
                coverage=SnapshotCoverage("OPAQUE_ORIGIN", ())
            )

        global_frame_count = [0]

        main_frame_snapshot = await self._capture_frame(
            frame=page.main_frame,
            eligibility_contract=eligibility_contract,
            is_main=True,
            main_origin=main_origin,
            global_frame_count=global_frame_count,
            cancellation_token=cancellation_token
        )

        return BrowserSnapshot(
            page_url=page_url[:self.max_text_length],
            main_frame=main_frame_snapshot,
            coverage=main_frame_snapshot.coverage
        )

    def _extract_origin(self, url: str) -> str:
        try:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme in ('http', 'https') and parsed.hostname:
                port = parsed.port
                if not port:
                    port = 80 if parsed.scheme == 'http' else 443
                return f"{parsed.scheme}://{parsed.hostname}:{port}"
        except Exception:
            pass
        return "null"

    async def _capture_frame(self, frame: Any, eligibility_contract: Dict[str, Any], is_main: bool, main_origin: str, global_frame_count: list, cancellation_token: Optional[CancellationToken]) -> FrameSnapshot:
        if cancellation_token and cancellation_token.is_cancelled():
            return FrameSnapshot(frame_id="unknown", url="", origin="", name=None, is_same_origin=False, controls=(), child_frames=(), coverage=SnapshotCoverage("CANCELLED", ()))

        global_frame_count[0] += 1
        limitations = []

        if global_frame_count[0] > self.max_frames:
            return FrameSnapshot(frame_id="unknown", url="", origin="", name=None, is_same_origin=False, controls=(), child_frames=(), coverage=SnapshotCoverage("LIMITED_FRAME_BUDGET", ()))

        if frame.is_detached():
            return FrameSnapshot(frame_id="unknown", url="", origin="", name=None, is_same_origin=False, controls=(), child_frames=(), coverage=SnapshotCoverage("FRAME_DETACHED", ()))

        try:
            url = frame.url
            name = frame.name
            frame_origin = self._extract_origin(url)

            if is_main:
                is_same_origin = True
            else:
                if frame_origin != "null" and frame_origin == main_origin:
                    is_same_origin = True
                else:
                    is_same_origin = False
        except Exception:
            return FrameSnapshot(frame_id="unknown", url="", origin="", name=None, is_same_origin=False, controls=(), child_frames=(), coverage=SnapshotCoverage("FRAME_DETACHED", ()))

        safe_name = name[:self.max_text_length] if name else None
        safe_frame_id = (name or ("main" if is_main else "cross_origin"))[:self.max_text_length]
        safe_url = url[:self.max_text_length]
        safe_origin = frame_origin[:self.max_text_length]

        if not is_same_origin and not is_main:
            return FrameSnapshot(
                frame_id=safe_frame_id,
                url=safe_url,
                origin=safe_origin,
                name=safe_name,
                is_same_origin=False,
                controls=(),
                child_frames=(),
                coverage=SnapshotCoverage("CROSS_ORIGIN_LIMITED", ())
            )

        try:
            dom_data = await frame.evaluate(self._extractor_script(), {
                "maxControls": self.max_controls_per_frame,
                "redactionString": self.redaction_string,
                "maxTextLength": self.max_text_length
            })
        except Exception as e:
            err_str = str(e).lower()
            if "navigation" in err_str or "execution context was destroyed" in err_str:
                return FrameSnapshot(frame_id=safe_frame_id, url=safe_url, origin=safe_origin, name=safe_name, is_same_origin=True, controls=(), child_frames=(), coverage=SnapshotCoverage("NAVIGATION_RACE", ()))
            return FrameSnapshot(frame_id=safe_frame_id, url=safe_url, origin=safe_origin, name=safe_name, is_same_origin=True, controls=(), child_frames=(), coverage=SnapshotCoverage("FRAME_DETACHED", ()))

        if cancellation_token and cancellation_token.is_cancelled():
            return FrameSnapshot(frame_id=safe_frame_id, url=safe_url, origin=safe_origin, name=safe_name, is_same_origin=True, controls=(), child_frames=(), coverage=SnapshotCoverage("CANCELLED", ()))

        if dom_data.get("status") == "LIMITED_CONTROL_BUDGET":
            limitations.append("LIMITED_CONTROL_BUDGET")

        controls = []
        raw_controls = dom_data.get("controls", [])
        for idx, c in enumerate(raw_controls):
            if idx % 50 == 0 and cancellation_token and cancellation_token.is_cancelled():
                limitations.append("CANCELLED")
                break

            if len(controls) >= self.max_controls_per_frame:
                if "LIMITED_CONTROL_BUDGET" not in limitations:
                    limitations.append("LIMITED_CONTROL_BUDGET")
                break

            controls.append(ControlSnapshot(
                control_id=c.get("control_id", "")[:self.max_text_length],
                raw_role=(c.get("raw_role", "") or "")[:self.max_text_length],
                raw_name=(c.get("raw_name", "") or "")[:self.max_text_length],
                raw_visible=c.get("raw_visible", True),
                raw_enabled=c.get("raw_enabled", True),
                checked=c.get("checked"),
                selected=c.get("selected"),
                expanded=c.get("expanded"),
                href=(c.get("href", "") or "")[:self.max_text_length] if c.get("href") is not None else None,
                target=(c.get("target", "") or "")[:self.max_text_length] if c.get("target") else None,
                rel=(c.get("rel", "") or "")[:self.max_text_length] if c.get("rel") else None,
                download_metadata=(c.get("download", "") or "")[:self.max_text_length] if c.get("download") else None,
                form_ownership=(c.get("form_ownership", "") or "")[:self.max_text_length] if c.get("form_ownership") else None,
                semantic_parent_context=(c.get("semantic_parent_context", "") or "")[:self.max_text_length] if c.get("semantic_parent_context") else None,
                is_private=c.get("is_private", False),
                is_volatile=c.get("is_volatile", False)
            ))

        child_snapshots = []
        for child in frame.child_frames:
            if cancellation_token and cancellation_token.is_cancelled():
                if "CANCELLED" not in limitations:
                    limitations.append("CANCELLED")
                break
            if global_frame_count[0] >= self.max_frames:
                if "LIMITED_FRAME_BUDGET" not in limitations:
                    limitations.append("LIMITED_FRAME_BUDGET")
                break
            child_snap = await self._capture_frame(child, eligibility_contract, False, main_origin, global_frame_count, cancellation_token)
            child_snapshots.append(child_snap)
            if cancellation_token and cancellation_token.is_cancelled():
                if "CANCELLED" not in limitations:
                    limitations.append("CANCELLED")
                break

        for cs in child_snapshots:
            for lim in cs.coverage.limitations:
                if lim not in limitations:
                    limitations.append(lim)
            if cs.coverage.terminal_status not in ("COMPLETE", "CANCELLED", "LIMITED_FRAME_BUDGET"):
                if cs.coverage.terminal_status not in limitations:
                    limitations.append(cs.coverage.terminal_status)

        terminal = "CANCELLED" if cancellation_token and cancellation_token.is_cancelled() else "COMPLETE"

        final_origin = safe_origin

        return FrameSnapshot(
            frame_id=safe_frame_id,
            url=safe_url,
            origin=final_origin,
            name=safe_name,
            is_same_origin=is_same_origin,
            controls=tuple(controls),
            child_frames=tuple(child_snapshots),
            coverage=SnapshotCoverage(terminal, tuple(sorted(limitations)))
        )

    def _extractor_script(self) -> str:
        return """
        (config) => {
            const result = { controls: [], status: "COMPLETE", origin: window.origin };
            const elements = document.querySelectorAll('a, button, input, select, option, textarea, [role="button"], [role="link"], [role="checkbox"], [role="menuitem"], [role="tab"], [tabindex]:not([tabindex="-1"])');

            const isSensitiveWord = (str) => {
                if (!str) return false;
                const s = str.toLowerCase();
                return s.includes('password') || s.includes('token') || s.includes('secret') || s.includes('session') || s.includes('auth');
            };

            const isButtonLike = (el) => {
                const tag = el.tagName.toLowerCase();
                if (tag === 'button') return true;
                if (tag === 'input') {
                    const t = (el.getAttribute('type') || '').toLowerCase();
                    return t === 'button' || t === 'submit' || t === 'reset';
                }
                const role = el.getAttribute('role');
                return role === 'button' || role === 'menuitem' || role === 'tab';
            };

            const truncate = (str, len) => str ? str.substring(0, len) : null;

            const getParentContext = (el) => {
                let current = el.parentElement;
                const tags = [];
                let depth = 0;
                while (current && depth < 3) {
                    const t = current.tagName.toLowerCase();
                    if (t === 'html' || t === 'body') break;
                    let desc = t;
                    if (current.id) desc += '#' + current.id;
                    const r = current.getAttribute('role');
                    if (r) desc += '[role=' + r + ']';
                    tags.push(desc);
                    current = current.parentElement;
                    depth++;
                }
                return tags.length > 0 ? tags.reverse().join(' > ') : null;
            };

            let count = 0;
            for (const el of elements) {
                if (count >= config.maxControls) {
                    result.status = "LIMITED_CONTROL_BUDGET";
                    break;
                }

                const tagName = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                let role = el.getAttribute('role');
                if (!role) {
                    if (tagName === 'input') {
                        role = 'input ' + (el.type || 'text').toLowerCase();
                    } else {
                        role = tagName;
                    }
                }

                const autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase();

                const isPrivate = type === 'password' ||
                                 type === 'hidden' ||
                                 isSensitiveWord(el.name) ||
                                 isSensitiveWord(el.id) ||
                                 autocomplete.includes('password') ||
                                 autocomplete.includes('one-time-code');

                const style = window.getComputedStyle(el);
                const isVisible = style.display !== 'none' && style.visibility !== 'hidden' && el.offsetWidth > 0 && el.offsetHeight > 0;
                const isEnabled = !el.disabled && !el.hasAttribute('disabled') && !el.closest('fieldset[disabled]');

                let isSelected = null;
                if (tagName === 'option') {
                    isSelected = el.selected;
                } else if (el.hasAttribute('aria-selected')) {
                    isSelected = el.getAttribute('aria-selected') === 'true';
                }

                let name = el.getAttribute('aria-label') || '';
                if (!name && el.labels && el.labels.length > 0) {
                    name = Array.from(el.labels).map(l => l.innerText).join(' ');
                }
                if (!name && el.hasAttribute('aria-labelledby')) {
                    const labelEl = document.getElementById(el.getAttribute('aria-labelledby'));
                    if (labelEl) name = labelEl.innerText;
                }
                if (!name) name = el.innerText || '';
                if (!name && isButtonLike(el)) name = el.value || '';
                if (!name && tagName === 'input' && !isButtonLike(el)) name = el.placeholder || '';

                let href = el.getAttribute('href');

                if (isPrivate) {
                    name = config.redactionString;
                    if (href) href = config.redactionString;
                }

                let isVolatile = false;
                if (el.id && el.id.match(/^ember\\d+$/)) {
                    isVolatile = true;
                }

                let controlId = el.id || '';
                if (!controlId) {
                    controlId = 'auto-' + tagName + '-' + count;
                }

                let parentCtx = getParentContext(el);

                result.controls.push({
                    control_id: truncate(controlId, config.maxTextLength),
                    raw_role: truncate(role, config.maxTextLength),
                    raw_name: typeof name === 'string' ? truncate(name.trim(), config.maxTextLength) : '',
                    raw_visible: isVisible,
                    raw_enabled: isEnabled,
                    checked: el.checked !== undefined ? el.checked : null,
                    selected: isSelected,
                    expanded: el.getAttribute('aria-expanded') === 'true' ? true : (el.getAttribute('aria-expanded') === 'false' ? false : null),
                    href: truncate(href, config.maxTextLength),
                    target: truncate(el.getAttribute('target'), config.maxTextLength),
                    rel: truncate(el.getAttribute('rel'), config.maxTextLength),
                    download: truncate((() => {
                        let dl = el.getAttribute('download');
                        let parts = dl ? [dl] : [];
                        const ac = el.getAttribute('aria-controls');
                        if (ac) parts.push('aria-controls:' + ac);
                        if (el.classList.contains('accordion-btn') || el.hasAttribute('aria-haspopup') || el.hasAttribute('data-action-pattern')) {
                            parts.push('supported_pattern');
                        }
                        return parts.length > 0 ? parts.join(';') : null;
                    })(), config.maxTextLength),
                    form_ownership: el.form ? truncate(el.form.id || 'form', config.maxTextLength) : null,
                    semantic_parent_context: truncate(parentCtx, config.maxTextLength),
                    is_private: isPrivate,
                    is_volatile: isVolatile
                });
                count++;
            }
            return result;
        }
        """
