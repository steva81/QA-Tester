from dataclasses import dataclass
from typing import Tuple, Optional

@dataclass(frozen=True)
class SnapshotCoverage:
    terminal_status: str
    limitations: Tuple[str, ...]

@dataclass(frozen=True)
class ControlSnapshot:
    control_id: str
    raw_role: str
    raw_name: str
    raw_visible: bool
    raw_enabled: bool
    checked: Optional[bool]
    selected: Optional[bool]
    expanded: Optional[bool]
    href: Optional[str]
    target: Optional[str]
    rel: Optional[str]
    download_metadata: Optional[str]
    form_ownership: Optional[str]
    semantic_parent_context: Optional[str]
    is_private: bool
    is_volatile: bool

@dataclass(frozen=True)
class FrameSnapshot:
    frame_id: str
    url: str
    origin: str
    name: Optional[str]
    is_same_origin: bool
    controls: Tuple[ControlSnapshot, ...]
    child_frames: Tuple['FrameSnapshot', ...]
    coverage: SnapshotCoverage

@dataclass(frozen=True)
class BrowserSnapshot:
    page_url: str
    main_frame: Optional[FrameSnapshot]
    coverage: SnapshotCoverage
