"""Project data model.

A project is the single source of truth for the UI and the exporter:
- timeline: caption cues with text and time range
- signer windows / sign regions: time-ranged rectangles that captions must avoid
- important regions: time-ranged human-annotated critical imagery
- layout settings: font size, allowed lines, safe margins, stability knobs
- placements: per-cue adopted layout (manual or auto), recorded as revisions
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

# Video frame coordinates use top-left origin, y down. All rects are pixel
# rects [x, y, w, h] in video frame pixels at the project's export resolution.


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float

    def to_list(self) -> list[float]:
        return [self.x, self.y, self.w, self.h]

    @classmethod
    def from_list(cls, v: list[float]) -> "Rect":
        return cls(float(v[0]), float(v[1]), float(v[2]), float(v[3]))

    def right(self) -> float:
        return self.x + self.w

    def bottom(self) -> float:
        return self.y + self.h

    def contains(self, other: "Rect") -> bool:
        return (
            other.x >= self.x
            and other.y >= self.y
            and other.right() <= self.right()
            and other.bottom() <= self.bottom()
        )

    def intersects(self, other: "Rect") -> bool:
        return not (
            other.x >= self.right()
            or other.right() <= self.x
            or other.y >= self.bottom()
            or other.bottom() <= self.y
        )

    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def intersection(self, other: "Rect") -> "Rect | None":
        x0 = max(self.x, other.x)
        y0 = max(self.y, other.y)
        x1 = min(self.right(), other.right())
        y1 = min(self.bottom(), other.bottom())
        if x1 <= x0 or y1 <= y0:
            return None
        return Rect(x0, y0, x1 - x0, y1 - y0)


@dataclass
class TimeRange:
    start: float
    end: float

    def covers(self, t: float) -> bool:
        return self.start <= t < self.end

    @classmethod
    def from_obj(cls, obj: Any) -> "TimeRange":
        return cls(float(obj["start"]), float(obj["end"]))


@dataclass
class CaptionCue:
    id: str
    start: float
    end: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AvoidRegion:
    """A rectangle that captions must avoid, valid only during [start, end)."""

    id: str
    label: str
    kind: str  # 'signer' | 'sign' | 'important' | 'custom'
    start: float
    end: float
    rect: list[float]
    # For human annotation provenance; never claim automatic full coverage.
    source: str = "manual"
    note: str = ""

    def active_at(self, t: float) -> bool:
        return self.start <= t < self.end

    def rect_obj(self) -> Rect:
        return Rect.from_list(self.rect)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LayoutSettings:
    font_size: float = 40.0
    bold: bool = False
    max_lines: int = 2
    max_chars_per_line: int = 16
    safe_margin: float = 24.0
    outline: float = 3.0
    line_spacing: float = 1.18
    # Stability: prefer the previous cue's position if a candidate is within
    # this many px of it; prevents vertical hopping from minor mask jitter.
    sticky_tolerance_px: float = 36.0
    # Minimum legible text height ratio; below this we report UNSOLVED instead
    # of shrinking to unreadable text.
    min_font_ratio: float = 0.55
    # Candidate grid resolution.
    grid_step: float = 40.0
    # Per-frame verification threshold (0-255) for diff pixel detection.
    verify_threshold: int = 24

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "LayoutSettings":
        if not d:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Placement:
    cue_id: str
    x: float
    y: float
    lines: list[str]
    font_size: float
    mode: str  # 'auto' | 'manual'
    solved: bool
    adopted_revision: int = 0
    adopted_at: float = 0.0
    adopted_by: str = "human"
    edited_at: float | None = None

    def ink_rect(self) -> Rect:
        # Actual painted extent is computed by the layout engine and cached on
        # the derived view; here we expose the placement anchor only.
        return Rect(self.x, self.y, 0.0, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Revision:
    n: int
    kind: str  # 'adopt' | 'manual-edit' | 'settings' | 'regions'
    at: float
    actor: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Project:
    id: str
    name: str
    video_path: str
    width: int
    height: int
    fps: float
    duration: float
    font_path: str
    font_bold_path: str
    cues: list[CaptionCue] = field(default_factory=list)
    regions: list[AvoidRegion] = field(default_factory=list)
    settings: LayoutSettings = field(default_factory=LayoutSettings)
    placements: dict[str, Placement] = field(default_factory=dict)
    # Unsolved cue ids reported by the latest evaluation: cue id -> explanation.
    unsolved: dict[str, str] = field(default_factory=dict)
    revisions: list[Revision] = field(default_factory=list)
    rev_counter: int = 0

    # -- persistence -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "video": {
                "path": self.video_path,
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "duration": self.duration,
            },
            "fonts": {"regular": self.font_path, "bold": self.font_bold_path},
            "cues": [c.to_dict() for c in self.cues],
            "regions": [r.to_dict() for r in self.regions],
            "settings": self.settings.to_dict(),
            "placements": {k: v.to_dict() for k, v in self.placements.items()},
            "unsolved": self.unsolved,
            "revisions": [r.to_dict() for r in self.revisions],
            "revCounter": self.rev_counter,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Project":
        v = d["video"]
        f = d.get("fonts", {})
        return cls(
            id=d["id"],
            name=d["name"],
            video_path=v["path"],
            width=int(v["width"]),
            height=int(v["height"]),
            fps=float(v["fps"]),
            duration=float(v["duration"]),
            font_path=f.get("regular", ""),
            font_bold_path=f.get("bold", ""),
            cues=[CaptionCue(**c) for c in d.get("cues", [])],
            regions=[AvoidRegion(**r) for r in d.get("regions", [])],
            settings=LayoutSettings.from_dict(d.get("settings")),
            placements={k: Placement(**p) for k, p in d.get("placements", {}).items()},
            unsolved=d.get("unsolved", {}),
            revisions=[Revision(**r) for r in d.get("revisions", [])],
            rev_counter=int(d.get("revCounter", 0)),
        )

    def add_revision(self, kind: str, summary: str, payload: dict[str, Any], actor: str = "human") -> Revision:
        self.rev_counter += 1
        rev = Revision(
            n=self.rev_counter,
            kind=kind,
            at=time.time(),
            actor=actor,
            summary=summary,
            payload=payload,
        )
        self.revisions.append(rev)
        return rev


class ProjectStore:
    """Thread-safe JSON-on-disk store. Atomic writes, one file per project."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, Project] = {}

    def _path(self, pid: str) -> str:
        safe = "".join(c for c in pid if c.isalnum() or c in "-_")
        return os.path.join(self.root, f"{safe}.json")

    def list_ids(self) -> list[str]:
        return sorted(f[:-5] for f in os.listdir(self.root) if f.endswith(".json"))

    def get(self, pid: str) -> Project:
        with self._lock:
            if pid in self._cache:
                return self._cache[pid]
            path = self._path(pid)
            if not os.path.exists(path):
                raise KeyError(pid)
            with open(path, "r", encoding="utf-8") as fh:
                proj = Project.from_dict(json.load(fh))
            self._cache[pid] = proj
            return proj

    def save(self, proj: Project) -> None:
        with self._lock:
            path = self._path(proj.id)
            tmp = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.root, delete=False, suffix=".tmp"
            )
            try:
                json.dump(proj.to_dict(), tmp, ensure_ascii=False, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp.close()
                os.replace(tmp.name, path)
            finally:
                if not tmp.closed:
                    tmp.close()
            self._cache[proj.id] = proj

    def delete(self, pid: str) -> None:
        with self._lock:
            self._cache.pop(pid, None)
            try:
                os.remove(self._path(pid))
            except FileNotFoundError:
                pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"
