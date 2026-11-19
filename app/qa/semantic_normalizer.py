from dataclasses import dataclass
from typing import Tuple, Optional, List

from app.qa.semantic_model import SemanticControl, SemanticState
from app.qa.browser_snapshot_model import BrowserSnapshot, FrameSnapshot, ControlSnapshot


@dataclass(frozen=True)
class SemanticNormalizationResult:
    semantic_state: Optional[SemanticState]
    terminal_status: str
    limitations: Tuple[str, ...]


class SemanticNormalizer:
    def normalize_snapshot(self, snapshot: BrowserSnapshot) -> SemanticNormalizationResult:
        if snapshot.coverage.terminal_status != "COMPLETE":
            return SemanticNormalizationResult(
                semantic_state=None,
                terminal_status=snapshot.coverage.terminal_status,
                limitations=snapshot.coverage.limitations
            )

        limitations = list(snapshot.coverage.limitations)
        controls = []
        if snapshot.main_frame:
            controls.extend(self._normalize_frame(snapshot.main_frame, limitations, (0,)))

        semantic_state = SemanticState(
            url=snapshot.page_url,
            controls=tuple(controls),
            validation_messages=(),
            meaningful_dates=()
        )
        return SemanticNormalizationResult(
            semantic_state=semantic_state,
            terminal_status=snapshot.coverage.terminal_status,
            limitations=tuple(sorted(list(set(limitations))))
        )

    def _normalize_frame(self, frame: FrameSnapshot, limitations: List[str], frame_path: Tuple[int, ...]) -> List[SemanticControl]:
        for lim in frame.coverage.limitations:
            if lim not in limitations:
                limitations.append(lim)

        if not frame.is_same_origin and not frame.controls and "CROSS_ORIGIN_LIMITED" in frame.coverage.limitations:
            return []

        controls = []
        for raw_c in frame.controls:
            controls.append(self.normalize_control(raw_c, frame_path))

        for idx, child in enumerate(frame.child_frames):
            child_path = frame_path + (idx,)
            controls.extend(self._normalize_frame(child, limitations, child_path))

        return controls

    def normalize_control(self, raw: ControlSnapshot, frame_path: Tuple[int, ...]) -> SemanticControl:
        role = self._normalize_role(raw)

        name = raw.raw_name if raw.raw_name else ""
        if raw.is_private and raw.raw_name == "***":
            name = "***"

        volatile_items = ["volatile"] if raw.is_volatile else []
        if raw.download_metadata:
            for part in raw.download_metadata.split(";"):
                if part.startswith("aria-controls:") or part.startswith("controls:") or part == "supported_pattern":
                    volatile_items.append(part)
        volatile_metadata = tuple(volatile_items)

        parent_context = raw.semantic_parent_context if raw.semantic_parent_context else "body"

        path_str = ".".join(map(str, frame_path))
        frame_prefix = f"[{path_str}] "
        final_control_id = f"{path_str}:{raw.control_id}"
        final_context = f"{frame_prefix}{parent_context}"

        return SemanticControl(
            control_id=final_control_id,
            accessible_role=role,
            accessible_name=name,
            visibility=raw.raw_visible,
            enabled=raw.raw_enabled,
            href=raw.href,
            target=raw.target,
            form_ownership=raw.form_ownership,
            is_private=raw.is_private,
            volatile_metadata=volatile_metadata,
            parent_semantic_context=final_context,
            state_expanded=raw.expanded,
            state_selected=raw.selected,
            state_checked=raw.checked
        )

    def _normalize_role(self, raw: ControlSnapshot) -> str:
        role = raw.raw_role.lower()
        if role == "button":
            return "button"
        elif role == "a" and raw.href:
            return "link"
        elif role == "a":
            return "generic"
        elif role == "select" or role == "combobox":
            return "combobox"
        elif role in ("textarea", "input text", "input password", "input email", "input search", "input tel", "input url", "input number", "input date"):
            return "textbox"
        elif role in ("input submit", "input button", "input reset"):
            return "button"
        elif role == "input checkbox":
            return "checkbox"
        elif role == "input radio":
            return "radio"
        elif role == "option":
            return "option"

        valid_aria_roles = {
            "alert", "alertdialog", "application", "article", "banner", "button", "cell", "checkbox",
            "columnheader", "combobox", "complementary", "contentinfo", "definition", "dialog", "directory",
            "document", "feed", "figure", "form", "grid", "gridcell", "group", "heading", "img", "link",
            "list", "listbox", "listitem", "log", "main", "marquee", "math", "menu", "menubar", "menuitem",
            "menuitemcheckbox", "menuitemradio", "navigation", "none", "note", "option", "presentation",
            "progressbar", "radio", "radiogroup", "region", "row", "rowgroup", "rowheader", "scrollbar",
            "search", "searchbox", "separator", "slider", "spinbutton", "status", "switch", "tab", "table",
            "tablist", "tabpanel", "term", "textbox", "timer", "toolbar", "tooltip", "tree", "treegrid", "treeitem"
        }
        if role in valid_aria_roles:
            return role

        return "generic"
