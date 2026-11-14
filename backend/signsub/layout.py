"""Caption layout engine.

Design goals (mirrors the product requirements):
- Never blanket-fix captions at the bottom: candidates are evaluated over the
  whole safe area, and avoidance of signer / signing / important regions wins.
- Use real glyph ink bounds (fontmetrics) so boxes match painted pixels.
- Respect allowed line count and safe margins.
- Stability: keep the previous cue's position while it remains valid; small
  mask jitter must not cause vertical hopping.
- If every position at the minimum legible size is blocked, report UNSOLVED
  with an explanation and do not silently shrink the text.
- Time-ranged regions: a region only blocks cues whose span intersects it.
"""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from .fontmetrics import TrueTypeFont, load_font
from .model import AvoidRegion, CaptionCue, LayoutSettings, Project, Rect

# Punctuation that must not start a line / end a line (basic CJK kinsoku).
NO_LINE_START = set("，。、；：！？）》」』】〕〉’”…—·%,.!?;:)]}")
NO_LINE_END = set("（《「『【〔〈‘“([{")
CJK_RANGES = (
    (0x2E80, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2FA1F),
    (0x3000, 0x303F),
    (0xFF00, 0xFFEF),
)


def is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(a <= cp <= b for a, b in CJK_RANGES)


# ---------------------------------------------------------------------------
# Avoidance query cost breakdown + reuse of still-valid geometry
# ---------------------------------------------------------------------------
#
# 避让查询优化的计算顺序（详见 docs/PERF.md）：
#   1. evaluate_timeline 逐条字幕评估；每条字幕先校验区域签名，快速动作或
#      区域跳变会改变签名，旧候选立即失效（AvoidanceOptimizer.fresh）。
#   2. 对整条字幕求活动区域并集（按 (start, end) 缓存）；能整段放置则结束。
#   3. 否则按区域断点切分时间区间，逐区间求解——断点数量与求解次数不减少，
#      短暂遮挡不会被跳过。
#   4. 每个区间内：字号×宽度变体 → 换行（缓存）→ 墨迹度量（缓存）→
#      候选位置网格（缓存）→ 安全区过滤 → 冲突检查（区域 Rect 预计算 +
#      结果缓存）→ 评分 → 稳定性保持。
#   5. 全部几何缓存键包含完整输入（区域矩形/时间、块尺寸、网格步长、文本、
#      字号），命中即"仍然有效"；优化只省重复计算，不改变任何输出。


@dataclass
class StageCost:
    """One stage of the avoidance query: calls, cache hits, time, work units."""

    calls: int = 0
    hits: int = 0
    seconds: float = 0.0
    units: int = 0  # points generated / box×region pairs / glyphs measured ...

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "hits": self.hits,
            "seconds": round(self.seconds, 6),
            "units": self.units,
        }


@dataclass
class AvoidanceStats:
    """成本分解：连续帧字幕避让中各查询阶段的成本与每条字幕的遮挡/耗时。"""

    region_query: StageCost = field(default_factory=StageCost)
    positions: StageCost = field(default_factory=StageCost)
    conflicts: StageCost = field(default_factory=StageCost)
    wrap: StageCost = field(default_factory=StageCost)
    measure: StageCost = field(default_factory=StageCost)
    occupancy: StageCost = field(default_factory=StageCost)
    score_calls: int = 0
    # 每条字幕的布局耗时（秒）与遮挡比例（活动区域对安全区的占用率）。
    cue_layout_seconds: dict[str, float] = field(default_factory=dict)
    occupancy_ratio: dict[str, float] = field(default_factory=dict)
    # 每条字幕实际求解的时间区间数：检查帧密度的直接证据（不得减少）。
    intervals_checked: dict[str, int] = field(default_factory=dict)

    @property
    def total_layout_seconds(self) -> float:
        return sum(self.cue_layout_seconds.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "regionQuery": self.region_query.to_dict(),
            "positions": self.positions.to_dict(),
            "conflicts": self.conflicts.to_dict(),
            "wrap": self.wrap.to_dict(),
            "measure": self.measure.to_dict(),
            "occupancy": self.occupancy.to_dict(),
            "scoreCalls": self.score_calls,
            "cueLayoutSeconds": {k: round(v, 6) for k, v in self.cue_layout_seconds.items()},
            "totalLayoutSeconds": round(self.total_layout_seconds, 6),
            "occupancyRatio": {k: round(v, 4) for k, v in self.occupancy_ratio.items()},
            "intervalsChecked": dict(self.intervals_checked),
        }


def _regions_signature(regions: list[AvoidRegion]) -> tuple:
    """Identity of every region's geometry+time; any jump changes this."""
    return tuple(
        (r.id, r.kind, round(r.start, 6), round(r.end, 6),
         tuple(round(v, 3) for v in r.rect))
        for r in regions
    )


class AvoidanceOptimizer:
    """Reuse of still-valid geometry within one timeline evaluation.

    Every cache key contains the full geometric input of its computation, so a
    hit means the reused value is still valid. `fresh()` re-checks the region
    signature: fast gestures / region jumps invalidate all cached candidates
    immediately. Nothing here reduces the number of checked intervals/frames.
    """

    def __init__(self, project: Project):
        self._project = project
        self._signature = _regions_signature(project.regions)
        self.active: dict[tuple[float, float], list[AvoidRegion]] = {}
        self.positions: dict[tuple, list[tuple[float, float]]] = {}
        self.wrap: dict[tuple, list[str] | None] = {}
        self.measure: dict[tuple, BlockMeasure] = {}
        self.occupancy: dict[tuple, float] = {}
        self.conflicts: dict[tuple, list[Conflict]] = {}
        self.rects: dict[str, Rect] = {}

    def fresh(self) -> bool:
        """False (and fully cleared) when regions moved since last check."""
        sig = _regions_signature(self._project.regions)
        if sig == self._signature:
            return True
        self.__init__(self._project)
        return False

    def rect_of(self, region: AvoidRegion) -> Rect:
        r = self.rects.get(region.id)
        if r is None:
            r = region.rect_obj()
            self.rects[region.id] = r
        return r


class _AvoidCtx:
    """Per-evaluation context: optional optimizer + optional stats."""

    __slots__ = ("opt", "stats")

    def __init__(self, opt: AvoidanceOptimizer | None = None,
                 stats: AvoidanceStats | None = None):
        self.opt = opt
        self.stats = stats


@contextmanager
def _timed(stage: StageCost | None) -> Iterator[None]:
    if stage is None:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        stage.seconds += time.perf_counter() - t0
        stage.calls += 1


@dataclass
class LineLayout:
    text: str
    pen_x: float
    baseline_y: float
    ink: Rect

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "penX": round(self.pen_x, 2),
            "baselineY": round(self.baseline_y, 2),
            "ink": [round(v, 2) for v in self.ink.to_list()],
        }


@dataclass
class Conflict:
    region_id: str
    label: str
    kind: str
    rect: list[float]
    time_range: list[float]
    overlap_area: float
    coverage_of_box: float  # fraction of the caption box covered by this region
    coverage_of_region: float  # fraction of the region covered by the box

    def to_dict(self) -> dict[str, Any]:
        return {
            "regionId": self.region_id,
            "label": self.label,
            "kind": self.kind,
            "rect": [round(v, 1) for v in self.rect],
            "timeRange": [round(v, 2) for v in self.time_range],
            "overlapArea": round(self.overlap_area, 1),
            "coverageOfBox": round(self.coverage_of_box, 4),
            "coverageOfRegion": round(self.coverage_of_region, 4),
        }


@dataclass
class Candidate:
    x: float
    y: float
    w: float
    h: float
    font_px: float
    lines: list[str]
    align: str
    valid: bool
    cost: float
    cost_parts: dict[str, float]
    conflicts: list[Conflict] = field(default_factory=list)
    source: str = "grid"
    sticky: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "w": round(self.w, 2),
            "h": round(self.h, 2),
            "fontPx": round(self.font_px, 2),
            "lines": self.lines,
            "align": self.align,
            "valid": self.valid,
            "cost": round(self.cost, 2),
            "costParts": {k: round(v, 2) for k, v in self.cost_parts.items()},
            "conflicts": [c.to_dict() for c in self.conflicts],
            "source": self.source,
            "sticky": self.sticky,
            "note": self.note,
        }


@dataclass
class UnsolvedReport:
    reason: str
    detail: str
    blockers: list[dict[str, Any]]
    occupancy: float
    attempted: list[dict[str, Any]]
    annotation_present: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "blockers": self.blockers,
            "occupancy": round(self.occupancy, 4),
            "attempted": self.attempted,
            "annotationPresent": self.annotation_present,
        }


@dataclass
class LayoutSegment:
    start: float
    end: float
    solved: bool
    geometry: "Geometry | None"
    unsolved: UnsolvedReport | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "solved": self.solved,
            "geometry": self.geometry.to_dict() if self.geometry else None,
            "unsolved": self.unsolved.to_dict() if self.unsolved else None,
        }


@dataclass
class LayoutResult:
    cue_id: str
    solved: bool
    chosen: Candidate | None
    candidates: list[Candidate]
    geometry: "Geometry | None"
    unsolved: UnsolvedReport | None
    active_regions: list[AvoidRegion]
    previous_cue_id: str | None
    sticky: bool
    segments: list[LayoutSegment] = field(default_factory=list)
    partial: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "cueId": self.cue_id,
            "solved": self.solved,
            "chosen": self.chosen.to_dict() if self.chosen else None,
            "candidates": [c.to_dict() for c in self.candidates],
            "geometry": self.geometry.to_dict() if self.geometry else None,
            "unsolved": self.unsolved.to_dict() if self.unsolved else None,
            "activeRegions": [r.to_dict() for r in self.active_regions],
            "previousCueId": self.previous_cue_id,
            "sticky": self.sticky,
            "segments": [s.to_dict() for s in self.segments],
            "partial": self.partial,
        }


@dataclass
class Geometry:
    cue_id: str
    solved: bool
    ink: Rect
    padded: Rect
    font_px: float
    align: str
    lines: list[LineLayout]
    conflicts: list[Conflict]
    mode: str = "auto"

    def to_dict(self) -> dict[str, Any]:
        return {
            "cueId": self.cue_id,
            "solved": self.solved,
            "ink": [round(v, 2) for v in self.ink.to_list()],
            "padded": [round(v, 2) for v in self.padded.to_list()],
            "fontPx": round(self.font_px, 2),
            "align": self.align,
            "lines": [l.to_dict() for l in self.lines],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "mode": self.mode,
        }


# ---------------------------------------------------------------------------
# Text measurement / wrapping
# ---------------------------------------------------------------------------


@dataclass
class LineMeasure:
    text: str
    ink_left: float
    ink_right: float
    ink_top: float
    ink_bottom: float
    advance: float

    @property
    def ink_w(self) -> float:
        return max(0.0, self.ink_right - self.ink_left)


def measure_line(font: TrueTypeFont, text: str, font_px: float, spacing_px: float = 0.0) -> LineMeasure:
    x = 0.0
    left = right = top = bottom = 0.0
    seen = False
    for idx, ch in enumerate(text):
        g = font.glyph_for_char(ch)
        if not g.is_empty:
            lx = x + g.x_min * font_px
            rx = x + g.x_max * font_px
            ty = -g.y_max * font_px  # above baseline => negative video offset
            by = -g.y_min * font_px
            if not seen:
                left, right, top, bottom = lx, rx, ty, by
                seen = True
            else:
                left = min(left, lx)
                right = max(right, rx)
                top = min(top, ty)
                bottom = max(bottom, by)
        x += g.advance * font_px
        if spacing_px and idx != len(text) - 1:
            x += spacing_px
    if not seen:
        return LineMeasure(text, 0.0, 0.0, 0.0, 0.0, x)
    return LineMeasure(text, left, right, top, bottom, x)


def _units(text: str) -> list[str]:
    """Split into unbreakable-ish units: CJK chars, Latin words, punctuation glue."""
    units: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            units.append("\n")
            i += 1
        elif ch.isspace():
            if units and units[-1] != " ":
                units.append(" ")
            i += 1
        elif ch in NO_LINE_START:
            if units and units[-1] not in ("\n", " "):
                units[-1] += ch
            else:
                units.append(ch)
            i += 1
        elif ch in NO_LINE_END:
            piece = ch
            i += 1
            if i < n and text[i] not in "\n":
                piece += text[i]
                i += 1
            units.append(piece)
        elif is_cjk(ch):
            units.append(ch)
            i += 1
        else:
            j = i
            while j < n and (not text[j].isspace()) and (not is_cjk(text[j])) and text[j] not in NO_LINE_START and text[j] not in NO_LINE_END:
                j += 1
            units.append(text[i:j])
            i = j
    return units


def _unit_too_wide(font: TrueTypeFont, unit: str, font_px: float, max_w: float) -> bool:
    if unit in ("\n", " "):
        return False
    return measure_line(font, unit, font_px).advance > max_w + 1e-6


def wrap_text(
    font: TrueTypeFont,
    text: str,
    font_px: float,
    max_width: float,
    max_lines: int,
    max_chars: int | None = None,
    spacing_px: float = 0.0,
) -> list[str] | None:
    """Greedy wrap using real advances. Returns None when it cannot fit."""
    units = _units(text)
    lines: list[str] = []
    cur = ""
    cur_chars = 0

    def flush() -> bool:
        nonlocal cur, cur_chars
        s = cur.strip()
        if s:
            lines.append(s)
        cur = ""
        cur_chars = 0
        return len(lines) <= max_lines

    for unit in units:
        if unit == "\n":
            if not flush():
                return None
            if len(lines) > max_lines:
                return None
            continue
        if unit == " ":
            if cur and not cur.endswith(" "):
                cur += " "
                cur_chars += 1
            continue
        units_to_try = [unit]
        if _unit_too_wide(font, unit, font_px, max_width) and len(unit) > 1:
            units_to_try = list(unit)  # hard-split an over-long word
        for piece in units_to_try:
            cand = cur + piece
            width = measure_line(font, cand, font_px, spacing_px).advance
            too_wide = width > max_width + 1e-6
            too_many = max_chars is not None and cur_chars + len(piece) > max_chars
            if (too_wide or too_many) and cur.strip():
                if not flush():
                    return None
                # Do not start a line with closing punctuation; pull previous
                # character down instead (basic kinsoku).
                if piece and piece[0] in NO_LINE_START and lines:
                    prev = lines[-1]
                    if len(prev) > 1:
                        lines[-1] = prev[:-1].rstrip()
                        cur = prev[-1] + piece
                        cur_chars = len(cur)
                        continue
                cand = piece
                width = measure_line(font, cand, font_px, spacing_px).advance
                if width > max_width + 1e-6:
                    return None
                if max_chars is not None and len(piece) > max_chars:
                    return None
            cur = cand
            cur_chars += len(piece)
    if cur.strip():
        if not flush():
            return None
    if not lines:
        return None
    if len(lines) > max_lines:
        return None
    return lines


def _wrap_ctx(font: TrueTypeFont, text: str, font_px: float, max_width: float,
              max_lines: int, max_chars: int | None, ctx: _AvoidCtx | None) -> list[str] | None:
    """wrap_text with reuse: identical (text, size, width) wraps are computed once."""
    stats = ctx.stats if ctx else None
    stage = stats.wrap if stats else None
    opt = ctx.opt if ctx else None
    with _timed(stage):
        if opt is not None:
            key = (text, round(font_px, 4), round(max_width, 4), max_lines, max_chars)
            if key in opt.wrap:
                if stage is not None:
                    stage.hits += 1
                return opt.wrap[key]
        out = wrap_text(font, text, font_px, max_width, max_lines, max_chars)
        if stage is not None:
            stage.units += len(text)
        if opt is not None:
            opt.wrap[key] = out
        return out


@dataclass
class BlockMeasure:
    lines: list[str]
    measures: list[LineMeasure]
    ink_w: float
    ink_h: float
    line_step: float
    top_offset: float  # ink_top relative to first baseline
    bottom_offset: float
    left_offset: float  # for left alignment (min ink_left)


def measure_block(
    font: TrueTypeFont,
    lines: list[str],
    font_px: float,
    line_spacing: float,
    spacing_px: float = 0.0,
) -> BlockMeasure:
    step = font_px * line_spacing
    ms = [measure_line(font, ln, font_px, spacing_px) for ln in lines]
    ink_w = max((m.ink_right - m.ink_left) for m in ms) if ms else 0.0
    top_off = min((i * step + m.ink_top) for i, m in enumerate(ms)) if ms else 0.0
    bottom_off = max((i * step + m.ink_bottom) for i, m in enumerate(ms)) if ms else 0.0
    left_off = min((m.ink_left for m in ms), default=0.0)
    return BlockMeasure(
        lines=lines,
        measures=ms,
        ink_w=ink_w,
        ink_h=bottom_off - top_off,
        line_step=step,
        top_offset=top_off,
        bottom_offset=bottom_off,
        left_offset=left_off,
    )


def _measure_ctx(font: TrueTypeFont, lines: list[str], font_px: float,
                 line_spacing: float, ctx: _AvoidCtx | None) -> BlockMeasure:
    """measure_block with reuse: identical (lines, size) blocks measured once."""
    stats = ctx.stats if ctx else None
    stage = stats.measure if stats else None
    opt = ctx.opt if ctx else None
    with _timed(stage):
        if opt is not None:
            key = (tuple(lines), round(font_px, 4), round(line_spacing, 4))
            hit = opt.measure.get(key)
            if hit is not None:
                if stage is not None:
                    stage.hits += 1
                return hit
        out = measure_block(font, lines, font_px, line_spacing)
        if stage is not None:
            stage.units += sum(len(ln) for ln in lines)
        if opt is not None:
            opt.measure[key] = out
        return out


def place_lines(
    block: BlockMeasure,
    x: float,
    y: float,
    align: str,
) -> tuple[list[LineLayout], Rect]:
    """Lay out lines so the block's ink bbox top-left is exactly (x, y)."""
    # Determine first baseline from desired ink top.
    first_baseline = y - block.top_offset
    lines: list[LineLayout] = []
    left = right = top = bottom = 0.0
    seen = False
    if align == "center":
        center = x + block.ink_w / 2.0
        pen = [center - (m.ink_left + m.ink_right) / 2.0 for m in block.measures]
    else:  # left
        pen = [x - block.left_offset for _ in block.measures]
    for i, m in enumerate(block.measures):
        baseline = first_baseline + i * block.line_step
        ink = Rect(
            pen[i] + m.ink_left,
            baseline + m.ink_top,
            max(0.0, m.ink_right - m.ink_left),
            max(0.0, m.ink_bottom - m.ink_top),
        )
        lines.append(LineLayout(text=m.text, pen_x=pen[i], baseline_y=baseline, ink=ink))
        if not seen:
            left, right, top, bottom = ink.x, ink.right(), ink.y, ink.bottom()
            seen = True
        else:
            left = min(left, ink.x)
            right = max(right, ink.right())
            top = min(top, ink.y)
            bottom = max(bottom, ink.bottom())
    if not seen:
        left, right, top, bottom = x, x, y, y
    return lines, Rect(left, top, max(0.0, right - left), max(0.0, bottom - top))


# ---------------------------------------------------------------------------
# Collision helpers
# ---------------------------------------------------------------------------


def _active_regions(project: Project, cue: CaptionCue) -> list[AvoidRegion]:
    return _active_regions_for(project, cue.start, cue.end)


def _active_regions_for(project: Project, start: float, end: float) -> list[AvoidRegion]:
    out = []
    for r in project.regions:
        # A region blocks a caption only if their time spans intersect.
        if r.start < end and r.end > start:
            out.append(r)
    return sorted(out, key=lambda r: (r.start, r.id))


def _active_regions_ctx(project: Project, start: float, end: float,
                        ctx: _AvoidCtx | None) -> list[AvoidRegion]:
    """Active-region query; reuses the still-valid set for identical spans."""
    stats = ctx.stats if ctx else None
    stage = stats.region_query if stats else None
    opt = ctx.opt if ctx else None
    key = (round(start, 6), round(end, 6))
    with _timed(stage):
        if stage is not None:
            stage.units += len(project.regions)
        if opt is not None:
            hit = key in opt.active
            if hit:
                if stage is not None:
                    stage.hits += 1
                return opt.active[key]
        out = _active_regions_for(project, start, end)
        if opt is not None:
            opt.active[key] = out
        return out


def _region_breakpoints(project: Project, cue: CaptionCue) -> list[float]:
    """Time points inside the cue where the active region set changes."""
    pts = {cue.start, cue.end}
    for r in project.regions:
        for t in (r.start, r.end):
            if cue.start < t < cue.end:
                pts.add(t)
    return sorted(pts)


def _outline_pad(settings: LayoutSettings) -> float:
    # Outline is drawn outward; keep a small antialias allowance.
    return settings.outline + 2.0


def expand(rect: Rect, pad: float) -> Rect:
    return Rect(rect.x - pad, rect.y - pad, rect.w + 2 * pad, rect.h + 2 * pad)


def conflicts_for(box: Rect, regions: list[AvoidRegion]) -> list[Conflict]:
    return _conflicts_with_rects(box, [(r, r.rect_obj()) for r in regions])


def _conflicts_with_rects(box: Rect, region_rects: list[tuple[AvoidRegion, Rect]]) -> list[Conflict]:
    conflicts: list[Conflict] = []
    for r, rr in region_rects:
        inter = box.intersection(rr)
        if inter is None:
            continue
        area = inter.area()
        conflicts.append(
            Conflict(
                region_id=r.id,
                label=r.label,
                kind=r.kind,
                rect=r.rect,
                time_range=[r.start, r.end],
                overlap_area=area,
                coverage_of_box=area / box.area() if box.area() > 0 else 0.0,
                coverage_of_region=area / rr.area() if rr.area() > 0 else 0.0,
            )
        )
    conflicts.sort(key=lambda c: c.overlap_area, reverse=True)
    return conflicts


def _conflicts_ctx(box: Rect, region_rects: list[tuple[AvoidRegion, Rect]],
                   regions_sig: tuple, ctx: _AvoidCtx | None) -> list[Conflict]:
    """Conflict check with precomputed region rects; result cached per box+set."""
    stats = ctx.stats if ctx else None
    stage = stats.conflicts if stats else None
    opt = ctx.opt if ctx else None
    if not region_rects:
        return []
    with _timed(stage):
        if opt is not None:
            key = (round(box.x, 3), round(box.y, 3), round(box.w, 3), round(box.h, 3),
                   regions_sig)
            hit = opt.conflicts.get(key)
            if hit is not None:
                if stage is not None:
                    stage.hits += 1
                return hit
        if stage is not None:
            stage.units += len(region_rects)
        out = _conflicts_with_rects(box, region_rects)
        if opt is not None:
            opt.conflicts[key] = out
        return out


def occupancy(regions: list[AvoidRegion], safe: Rect, step: float = 12.0) -> float:
    """Fraction of the safe area covered by annotated regions (union)."""
    if safe.area() <= 0:
        return 0.0
    rects = [r.rect_obj() for r in regions]
    total = 0
    covered = 0
    y = safe.y
    while y < safe.bottom():
        x = safe.x
        while x < safe.right():
            total += 1
            cx, cy = x + step / 2, y + step / 2
            if any(rr.x <= cx < rr.right() and rr.y <= cy < rr.bottom() for rr in rects):
                covered += 1
            x += step
        y += step
    return covered / total if total else 0.0


def _occupancy_ctx(regions: list[AvoidRegion], safe: Rect, ctx: _AvoidCtx | None,
                   step: float = 12.0) -> float:
    """Occupancy query （遮挡比例）; cached per region-subset signature."""
    stats = ctx.stats if ctx else None
    stage = stats.occupancy if stats else None
    opt = ctx.opt if ctx else None
    with _timed(stage):
        if opt is not None:
            key = (_regions_signature(regions),
                   (round(safe.x, 3), round(safe.y, 3), round(safe.w, 3), round(safe.h, 3)),
                   step)
            hit = opt.occupancy.get(key)
            if hit is not None:
                if stage is not None:
                    stage.hits += 1
                return hit
        out = occupancy(regions, safe, step)
        if stage is not None:
            nx = max(0, int((safe.w or 0.0) / step))
            stage.units += nx * max(0, int((safe.h or 0.0) / step))
        if opt is not None:
            opt.occupancy[key] = out
        return out


# ---------------------------------------------------------------------------
# Candidate generation and scoring
# ---------------------------------------------------------------------------


def _dedupe_positions(points: list[tuple[float, float]], tol: float = 6.0) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for p in points:
        if not any(abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol for q in out):
            out.append(p)
    return out


def _positions(safe: Rect, w: float, h: float, step: float) -> list[tuple[float, float]]:
    max_x = safe.right() - w
    max_y = safe.bottom() - h
    if max_x < safe.x - 1e-6 or max_y < safe.y - 1e-6:
        return []
    pts: list[tuple[float, float]] = []
    # Edges and centers first (visually meaningful anchors).
    xs = {safe.x, max_x, safe.x + (safe.w - w) / 2.0}
    ys = {safe.y, max_y, safe.y + (safe.h - h) / 2.0}
    for x in xs:
        for y in ys:
            pts.append((x, y))
    # Grid.
    nx = max(1, int(math.floor((max_x - safe.x) / step)) + 1)
    ny = max(1, int(math.floor((max_y - safe.y) / step)) + 1)
    for i in range(nx + 1):
        x = min(max_x, safe.x + i * step)
        for j in range(ny + 1):
            y = min(max_y, safe.y + j * step)
            pts.append((x, y))
    return _dedupe_positions(pts)


def _positions_ctx(safe: Rect, w: float, h: float, step: float,
                   ctx: _AvoidCtx | None) -> list[tuple[float, float]]:
    """Candidate grid; identical (safe, block, step) grids are generated once."""
    stats = ctx.stats if ctx else None
    stage = stats.positions if stats else None
    opt = ctx.opt if ctx else None
    with _timed(stage):
        if opt is not None:
            key = (round(safe.x, 3), round(safe.y, 3), round(safe.w, 3), round(safe.h, 3),
                   round(w, 3), round(h, 3), round(step, 3))
            hit = opt.positions.get(key)
            if hit is not None:
                if stage is not None:
                    stage.hits += 1
                return hit
        out = _positions(safe, w, h, step)
        if stage is not None:
            stage.units += len(out)
        if opt is not None:
            opt.positions[key] = out
        return out


def _score(
    box: Rect,
    safe: Rect,
    font_px: float,
    base_font: float,
    n_lines: int,
    prev_box: Rect | None,
    sticky_tol: float,
    frame_h: float,
    frame_w: float,
) -> tuple[float, dict[str, float]]:
    target_y = safe.y + safe.h * 0.78
    center_y = box.y + box.h / 2.0
    center_x = box.x + box.w / 2.0
    position = 30.0 * abs(center_y - target_y) / max(1.0, safe.h)
    horizontal = 8.0 * abs(center_x - (safe.x + safe.w / 2.0)) / max(1.0, safe.w)
    lines_cost = 10.0 * max(0, n_lines - 1)
    size_cost = 60.0 * max(0.0, 1.0 - font_px / base_font)
    stability = 0.0
    sticky = 0.0
    if prev_box is not None:
        dx = abs(box.x - prev_box.x)
        dy = abs(box.y - prev_box.y)
        stability = 120.0 * (dy / max(1.0, frame_h)) + 45.0 * (dx / max(1.0, frame_w))
        if dy <= sticky_tol and dx <= sticky_tol * 2:
            sticky = -6.0
    parts = {
        "position": position,
        "horizontal": horizontal,
        "lines": lines_cost,
        "size": size_cost,
        "stability": stability,
        "stickyBonus": sticky,
    }
    return sum(parts.values()), parts


def _interval_free_ratio(project: Project, interval: tuple[float, float],
                         ctx: _AvoidCtx | None = None) -> float:
    s = project.settings
    safe = Rect(s.safe_margin, s.safe_margin,
                project.width - 2 * s.safe_margin, project.height - 2 * s.safe_margin)
    regs = _active_regions_ctx(project, interval[0], interval[1], ctx)
    return 1.0 - _occupancy_ctx(regs, safe, ctx)


def _solve_static(
    project: Project,
    cue: CaptionCue,
    font: TrueTypeFont,
    regions: list[AvoidRegion],
    prev: Candidate | None = None,
    prev_cue_id: str | None = None,
    adopted: Candidate | None = None,
    max_candidates: int = 8,
    window: tuple[float, float] | None = None,
    ctx: _AvoidCtx | None = None,
) -> LayoutResult:
    s = project.settings
    pad = _outline_pad(s)
    safe = Rect(s.safe_margin, s.safe_margin,
                project.width - 2 * s.safe_margin, project.height - 2 * s.safe_margin)
    stats = ctx.stats if ctx else None
    opt = ctx.opt if ctx else None
    # Region rects are computed once per solve (not once per candidate box).
    if opt is not None:
        region_rects = [(r, opt.rect_of(r)) for r in regions]
        regions_sig = _regions_signature(regions)
    else:
        region_rects = [(r, r.rect_obj()) for r in regions]
        regions_sig = ()
    base_font = float(s.font_size)
    min_font = max(base_font * s.min_font_ratio, 16.0)
    sizes = []
    for f in (base_font, base_font * 0.92, base_font * 0.84, base_font * 0.76, base_font * 0.68, min_font):
        f = max(f, min_font)
        if not sizes or f < sizes[-1] - 0.5:
            sizes.append(round(f, 2))
    # Width variants: full width, and narrower columns (useful beside a signer).
    width_caps = [safe.w, safe.w * 0.68, safe.w * 0.5]
    width_caps = sorted({round(w, 2) for w in width_caps if w > 40}, reverse=True)

    prev_box = None
    if prev is not None:
        prev_box = Rect(prev.x, prev.y, prev.w, prev.h)

    best_by_size: list[Candidate] = []
    attempted: list[dict[str, Any]] = []
    chosen_size = None
    used_sizes = []

    for font_px in sizes:
        valid_here: list[Candidate] = []
        invalid_here: list[Candidate] = []
        variants = []
        for cap in width_caps:
            lines = _wrap_ctx(
                font, cue.text, font_px, cap, s.max_lines,
                s.max_chars_per_line or None, ctx,
            )
            if lines is None:
                attempted.append({"fontPx": font_px, "widthCap": round(cap, 1), "result": "line-wrap failed"})
                continue
            block = _measure_ctx(font, lines, font_px, s.line_spacing, ctx)
            if block.ink_w > safe.w + 1e-6 or block.ink_h > safe.h + 1e-6:
                attempted.append({"fontPx": font_px, "widthCap": round(cap, 1), "result": "block exceeds safe area"})
                continue
            variants.append((cap, block))
        if not variants:
            continue
        used_sizes.append(font_px)
        for cap, block in variants:
            for (x, y) in _positions_ctx(safe, block.ink_w, block.ink_h, s.grid_step, ctx):
                padded = expand(Rect(x, y, block.ink_w, block.ink_h), pad)
                if not (padded.x >= safe.x - 1e-6 and padded.y >= safe.y - 1e-6
                        and padded.right() <= safe.right() + 1e-6
                        and padded.bottom() <= safe.bottom() + 1e-6):
                    continue
                cf = _conflicts_ctx(padded, region_rects, regions_sig, ctx)
                valid = not cf
                cost, parts = _score(
                    Rect(x, y, block.ink_w, block.ink_h), safe, font_px, base_font,
                    len(block.lines), prev_box, s.sticky_tolerance_px,
                    project.height, project.width,
                )
                if stats is not None:
                    stats.score_calls += 1
                if not valid:
                    covered = sum(c.overlap_area for c in cf) / max(1.0, padded.area())
                    cost = 9000.0 + 1000.0 * covered
                    parts = {"blocked": cost}
                cand = Candidate(
                    x=x, y=y, w=block.ink_w, h=block.ink_h, font_px=font_px,
                    lines=block.lines, align="center", valid=valid, cost=cost,
                    cost_parts=parts, conflicts=cf,
                    source="anchor" if (x in (safe.x, safe.right() - block.ink_w)
                                        or y in (safe.y, safe.bottom() - block.ink_h)) else "grid",
                )
                (valid_here if valid else invalid_here).append(cand)
        if valid_here:
            best_by_size = valid_here
            chosen_size = font_px
            break
        else:
            # Remember the least-bad invalid candidate for the report.
            invalid_here.sort(key=lambda c: c.cost)
            best_by_size.extend(invalid_here[:2])

    all_valid = [c for c in best_by_size if c.valid]
    # ---- stability: if the adopted or previous position is still valid, keep it.
    sticky_candidate = None
    for ref, ref_label in ((adopted, "adopted"), (prev, "previous")):
        if ref is None:
            continue
        # Re-wrap at the reference font size to keep lines consistent.
        for cap in width_caps:
            lines = _wrap_ctx(font, cue.text, ref.font_px, cap, s.max_lines,
                              s.max_chars_per_line or None, ctx)
            if not lines:
                continue
            block = _measure_ctx(font, lines, ref.font_px, s.line_spacing, ctx)
            ref_padded = expand(Rect(ref.x, ref.y, block.ink_w, block.ink_h), pad)
            if not (ref_padded.x >= safe.x - 1e-6 and ref_padded.y >= safe.y - 1e-6
                    and ref_padded.right() <= safe.right() + 1e-6
                    and ref_padded.bottom() <= safe.bottom() + 1e-6):
                continue
            cf = _conflicts_ctx(ref_padded, region_rects, regions_sig, ctx)
            if cf:
                continue
            sticky_candidate = Candidate(
                x=ref.x, y=ref.y, w=block.ink_w, h=block.ink_h, font_px=ref.font_px,
                lines=block.lines, align="center", valid=True, cost=-1000.0,
                cost_parts={"sticky": -1000.0}, source=ref_label, sticky=True,
                note=f"保持{ '已采用' if ref_label=='adopted' else '上一条' }字幕位置",
            )
            break
        if sticky_candidate:
            break

    chosen = None
    if sticky_candidate is not None:
        chosen = sticky_candidate
        # Keep nearby alternatives for the UI, but never let an alternative
        # outrank the sticky choice.
        for c in all_valid:
            c.note = c.note or "自动候选"
        all_valid = [c for c in all_valid if abs(c.x - chosen.x) > 6 or abs(c.y - chosen.y) > 6]
    elif all_valid:
        all_valid.sort(key=lambda c: c.cost)
        chosen = all_valid[0]

    geometry = None
    if chosen is not None:
        block = _measure_ctx(font, chosen.lines, chosen.font_px, s.line_spacing, ctx)
        lines, ink = place_lines(block, chosen.x, chosen.y, chosen.align)
        geometry = Geometry(
            cue_id=cue.id,
            solved=True,
            ink=ink,
            padded=expand(ink, pad),
            font_px=chosen.font_px,
            align=chosen.align,
            lines=lines,
            conflicts=[],
            mode="sticky" if chosen.sticky else "auto",
        )

    unsolved = None
    if chosen is None:
        occ = _occupancy_ctx(regions, safe, ctx)
        blockers = [
            {
                "regionId": r.id,
                "label": r.label,
                "kind": r.kind,
                "rect": r.rect,
                "timeRange": [r.start, r.end],
            }
            for r in regions
        ]
        if not regions:
            reason = "文本无法放入安全区"
            detail = (
                "未提供有效区域标注时仍无法容纳该字幕：请在当前字号/行数限制下缩短文本，"
                "放宽行数或缩小字号。"
            )
        elif occ >= 0.995:
            reason = "安全区被标注区域完全占据"
            detail = (
                f"该字幕时间范围内的 {len(regions)} 个标注区域覆盖了 {occ:.0%} 的安全区，"
                f"在最小可读字号 {min_font:.0f}px 下没有任何位置可放置。"
                "按策略不缩小到不可读尺寸，已报告无解。"
            )
        else:
            reason = "所有候选位置均与标注区域重叠"
            detail = (
                f"在安全区内尝试了 {len(attempted) or len(used_sizes)} 组字号/宽度组合与网格位置，"
                f"标注区域占用安全区 {occ:.0%}，剩余空隙无法容纳 {s.max_lines} 行以内的该文本。"
                "未采用极小字号规避，已报告无解。"
            )
        unsolved = UnsolvedReport(
            reason=reason,
            detail=detail,
            blockers=blockers,
            occupancy=occ,
            attempted=attempted[:12],
            annotation_present=bool(regions),
        )

    # Rank candidates for UI: valid first, then invalid examples.
    candidates = sorted(all_valid, key=lambda c: c.cost)
    if chosen is not None:
        candidates = [chosen] + [c for c in candidates if c is not chosen]
    else:
        invalid = [c for c in best_by_size if not c.valid]
        invalid.sort(key=lambda c: c.cost)
        candidates = invalid[:max_candidates]
    candidates = candidates[:max_candidates]

    return LayoutResult(
        cue_id=cue.id,
        solved=chosen is not None,
        chosen=chosen,
        candidates=candidates,
        geometry=geometry,
        unsolved=unsolved,
        active_regions=regions,
        previous_cue_id=prev_cue_id,
        sticky=bool(chosen and chosen.sticky),
    )


def evaluate_cue(
    project: Project,
    cue: CaptionCue,
    font: TrueTypeFont,
    prev: Candidate | None = None,
    prev_cue_id: str | None = None,
    adopted: Candidate | None = None,
    max_candidates: int = 8,
    ctx: _AvoidCtx | None = None,
) -> LayoutResult:
    """Solve a cue whose regions may change within its time span.

    1. Preferred: one placement valid against every region active at any point
       of the cue (no jumping inside a cue).
    2. If impossible, degrade to per-interval rendering: each sub-interval is
       solved against the regions active then, so a fully occluded stretch is
       reported precisely instead of hiding the whole cue.
    If every sub-interval fails, the cue is UNSOLVED -- never shrunk to an
    unreadable size.
    """
    stats = ctx.stats if ctx else None
    opt = ctx.opt if ctx else None
    t0 = time.perf_counter() if stats is not None else 0.0
    if opt is not None:
        # 快速动作/区域跳变：签名变化立即失效全部旧候选。
        opt.fresh()
    union_regions = _active_regions_ctx(project, cue.start, cue.end, ctx)
    whole = _solve_static(project, cue, font, union_regions, prev, prev_cue_id, adopted,
                          max_candidates, ctx=ctx)
    if whole.solved:
        whole.segments = [LayoutSegment(cue.start, cue.end, True, whole.geometry, None)]
        _record_cue_stats(ctx, project, cue, union_regions, 1, t0)
        return whole

    points = _region_breakpoints(project, cue)
    intervals: list[tuple[float, float, list[AvoidRegion]]] = []
    for a, b in zip(points, points[1:]):
        if b - a <= 1e-6:
            continue
        regs = _active_regions_ctx(project, a, b, ctx)
        if intervals and [r.id for r in intervals[-1][2]] == [r.id for r in regs]:
            intervals[-1] = (intervals[-1][0], b, regs)
        else:
            intervals.append((a, b, regs))
    if len(intervals) <= 1:
        whole.segments = [LayoutSegment(cue.start, cue.end, False, None, whole.unsolved)]
        _record_cue_stats(ctx, project, cue, union_regions, 1, t0)
        return whole

    segments: list[LayoutSegment] = []
    solved_results: list[LayoutResult] = []
    failures: list[tuple[tuple[float, float], LayoutResult]] = []
    running_prev = prev
    for (a, b, regs) in intervals:
        res = _solve_static(project, cue, font, regs, running_prev, prev_cue_id, adopted,
                            max_candidates, ctx=ctx)
        if res.solved and res.geometry is not None:
            segments.append(LayoutSegment(a, b, True, res.geometry, None))
            solved_results.append(res)
            running_prev = res.chosen
        else:
            segments.append(LayoutSegment(a, b, False, None, res.unsolved))
            failures.append(((a, b), res))
    if solved_results:
        base = solved_results[0]
        base.segments = segments
        base.partial = any(not seg.solved for seg in segments)
        # Report the first blocked interval so the UI can explain the gap.
        if failures:
            base.unsolved = failures[0][1].unsolved
        _record_cue_stats(ctx, project, cue, union_regions, len(intervals), t0)
        return base

    # Nothing solvable: report the interval with the most free space.
    best = max(failures, key=lambda f: (_interval_free_ratio(project, f[0], ctx), f[0][1] - f[0][0]))
    best[1].segments = segments
    _record_cue_stats(ctx, project, cue, union_regions, len(intervals), t0)
    return best[1]


def _record_cue_stats(ctx: _AvoidCtx | None, project: Project, cue: CaptionCue,
                      union_regions: list[AvoidRegion], intervals: int, t0: float) -> None:
    """Record per-cue 布局耗时 / 遮挡比例 / 检查区间数 (only when stats asked)."""
    stats = ctx.stats if ctx else None
    if stats is None:
        return
    stats.cue_layout_seconds[cue.id] = time.perf_counter() - t0
    stats.intervals_checked[cue.id] = intervals
    s = project.settings
    safe = Rect(s.safe_margin, s.safe_margin,
                project.width - 2 * s.safe_margin, project.height - 2 * s.safe_margin)
    stats.occupancy_ratio[cue.id] = _occupancy_ctx(union_regions, safe, ctx)


def evaluate_timeline(
    project: Project,
    font_bold: bool = False,
    *,
    stats: AvoidanceStats | None = None,
    optimize: bool = True,
) -> dict[str, LayoutResult]:
    """Evaluate all cues in time order, feeding each solved cue forward for stability.

    With ``optimize=True`` (default) an :class:`AvoidanceOptimizer` reuses
    still-valid geometry across the continuous-frame evaluation; the reference
    path (``optimize=False``) recomputes every query and is kept as the runtime
    baseline for speed comparison. Both paths must produce identical results.
    """
    font_path = project.font_bold_path if font_bold else project.font_path
    font = load_font(font_path)
    opt = AvoidanceOptimizer(project) if optimize else None
    ctx = _AvoidCtx(opt, stats)
    ordered = sorted(project.cues, key=lambda c: (c.start, c.id))
    results: dict[str, LayoutResult] = {}
    prev: Candidate | None = None
    prev_cue_id: str | None = None
    for cue in ordered:
        adopted = None
        p = project.placements.get(cue.id)
        if p is not None and p.solved:
            adopted = Candidate(
                x=p.x, y=p.y, w=0.0, h=0.0, font_px=p.font_size,
                lines=p.lines, align="center", valid=True, cost=0.0,
                cost_parts={}, source="adopted", sticky=True,
            )
        res = evaluate_cue(project, cue, font, prev=prev, prev_cue_id=prev_cue_id,
                           adopted=adopted, ctx=ctx)
        results[cue.id] = res
        if res.chosen is not None:
            prev = res.chosen
            prev_cue_id = cue.id
    return results


def evaluate_timeline_reference(
    project: Project,
    font_bold: bool = False,
    *,
    stats: AvoidanceStats | None = None,
) -> dict[str, LayoutResult]:
    """运行参照：未优化的避让查询路径（每次查询都重新计算）。

    保留用于速度对比与回归校验；产品路径是 optimize=True 的 evaluate_timeline。
    """
    return evaluate_timeline(project, font_bold, stats=stats, optimize=False)


def validate_placement(
    project: Project,
    cue: CaptionCue,
    x: float,
    y: float,
    font_px: float,
    lines: list[str],
    font_bold: bool = False,
) -> tuple[Geometry, list[Conflict], list[str]]:
    """Validate a manually proposed block (top-left of ink bbox). Returns geometry."""
    s = project.settings
    pad = _outline_pad(s)
    safe = Rect(s.safe_margin, s.safe_margin,
                project.width - 2 * s.safe_margin, project.height - 2 * s.safe_margin)
    font = load_font(project.font_bold_path if font_bold else project.font_path)
    block = measure_block(font, lines, font_px, s.line_spacing)
    line_layouts, ink = place_lines(block, x, y, "center")
    padded = expand(ink, pad)
    regions = _active_regions(project, cue)
    conflicts = conflicts_for(padded, regions)
    problems: list[str] = []
    if not (padded.x >= safe.x - 1e-6 and padded.y >= safe.y - 1e-6
            and padded.right() <= safe.right() + 1e-6
            and padded.bottom() <= safe.bottom() + 1e-6):
        problems.append("字幕超出安全边距")
    if conflicts:
        problems.append("字幕与人物保护区/重要画面区域重叠")
    geo = Geometry(
        cue_id=cue.id,
        solved=not problems,
        ink=ink,
        padded=padded,
        font_px=font_px,
        align="center",
        lines=line_layouts,
        conflicts=conflicts,
        mode="manual",
    )
    return geo, conflicts, problems


def effective_layout(project: Project, font_bold: bool = False) -> dict[str, Any]:
    """Compute the layout used for preview/export.

    Adopted placements win (and are re-validated against current regions), then
    automatic candidates. Unsolved cues are reported, never drawn at tiny size.
    """
    results = evaluate_timeline(project, font_bold=font_bold)
    font = load_font(project.font_bold_path if font_bold else project.font_path)
    per_cue = {}
    problems = []
    for cue in sorted(project.cues, key=lambda c: (c.start, c.id)):
        res = results[cue.id]
        p = project.placements.get(cue.id)
        if p is not None and p.solved:
            geo, conflicts, probs = validate_placement(
                project, cue, p.x, p.y, p.font_size, p.lines, font_bold=font_bold
            )
            geo.mode = "manual" if p.mode == "manual" else "adopted"
            if probs:
                problems.append({
                    "cueId": cue.id,
                    "problems": probs,
                    "conflicts": [c.to_dict() for c in conflicts],
                })
                per_cue[cue.id] = {
                    "cueId": cue.id,
                    "solved": False,
                    "stale": True,
                    "geometry": geo.to_dict(),
                    "unsolved": {
                        "reason": "已采用的位置与当前标注冲突",
                        "detail": "标注区域在采用后发生变化，请重新选择候选或手动移动字幕。",
                        "conflicts": [c.to_dict() for c in conflicts],
                    },
                }
            else:
                per_cue[cue.id] = {
                    "cueId": cue.id,
                    "solved": True,
                    "stale": False,
                    "geometry": geo.to_dict(),
                    "adopted": True,
                }
        else:
            per_cue[cue.id] = {
                "cueId": cue.id,
                "solved": res.solved,
                "partial": res.partial,
                "stale": False,
                "geometry": res.geometry.to_dict() if res.geometry else None,
                "segments": [seg.to_dict() for seg in res.segments],
                "unsolved": res.unsolved.to_dict() if res.unsolved else None,
                "chosen": res.chosen.to_dict() if res.chosen else None,
            }
    unsolved_ids = [cid for cid, v in per_cue.items() if not v["solved"]]
    return {
        "results": results,
        "perCue": per_cue,
        "problems": problems,
        "unsolvedCueIds": unsolved_ids,
        "annotationNotice": (
            "关键画面/人物区域全部来自人工标注；未标注区域未做自动识别，"
            "因此不能据此宣称已覆盖所有关键画面。"
        ),
    }


def placement_box_for_cue(
    project: Project, cue: CaptionCue, font_bold: bool = False
) -> tuple[Candidate | None, list[Conflict], Rect | None]:
    """Return the effective candidate (adopted or auto) and its padded box."""
    res = evaluate_cue(
        project, cue, load_font(project.font_bold_path if font_bold else project.font_path),
        adopted=_adopted_candidate(project, cue),
    )
    if res.chosen is None:
        return None, [], None
    s = project.settings
    block = measure_block(load_font(project.font_bold_path if font_bold else project.font_path),
                          res.chosen.lines, res.chosen.font_px, s.line_spacing)
    _, ink = place_lines(block, res.chosen.x, res.chosen.y, "center")
    return res.chosen, [], expand(ink, _outline_pad(s))


def _adopted_candidate(project: Project, cue: CaptionCue) -> Candidate | None:
    p = project.placements.get(cue.id)
    if p is None or not p.solved:
        return None
    return Candidate(
        x=p.x, y=p.y, w=0.0, h=0.0, font_px=p.font_size, lines=p.lines,
        align="center", valid=True, cost=0.0, cost_parts={}, source="adopted", sticky=True,
    )
