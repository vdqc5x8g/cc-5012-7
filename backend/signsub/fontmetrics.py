"""Real glyph metrics read directly from the bundled TrueType font.

The layout engine must work with actual glyph shapes, not assumed em boxes:
CJK ideographs are full-width but punctuation such as '，' or '。' carries
ink only in a small quadrant, and Latin glyphs vary from 0.25 em ('i') to 0.7 em
('A').  We parse the `glyf` table to get per-glyph ink bounds and the `hmtx`/`cmap`
tables to get advances, so candidate rectangles always correspond to pixels that
the renderer will actually paint.

The bundled font has been normalised so that `fontsize` in an ASS script equals
the em size in pixels (upem == winAscent + winDescent, see build_fonts.py).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from functools import lru_cache

PROGMETRIC_UPEM_OFFSET = 18


@dataclass(frozen=True)
class GlyphBox:
    """Ink bounds of a single glyph relative to its baseline origin, in em units."""

    advance: float  # horizontal advance, em
    x_min: float  # left ink edge, em (may be negative)
    y_min: float  # bottom ink edge relative to baseline, em (usually negative)
    x_max: float  # right ink edge, em
    y_max: float  # top ink edge relative to baseline, em
    is_empty: bool

    @property
    def width(self) -> float:
        return self.x_max - self.x_min


class TrueTypeFont:
    """Minimal TrueType reader: cmap, hmtx, head, hhea, maxp, loca, glyf."""

    def __init__(self, path: str):
        with open(path, "rb") as fh:
            self._data = fh.read()
        self._parse_directory()
        self._parse_head()
        self._parse_hhea()
        self._parse_maxp()
        self._parse_cmap()
        self.upem = self._u16at(self._tables["head"] + PROGMETRIC_UPEM_OFFSET)
        # Metrics normalised at build time: fontsize == em px.
        self.ascent = self.hhea_ascent / self.upem
        self.descent = -self.hhea_descent / self.upem

    # -- table directory -------------------------------------------------
    def _parse_directory(self) -> None:
        d = self._data
        num = self._u16at(4)
        self._tables: dict[str, int] = {}
        for i in range(num):
            rec = 12 + i * 16
            tag = d[rec : rec + 4].decode("latin1")
            offset = struct.unpack(">I", d[rec + 8 : rec + 12])[0]
            self._tables[tag] = offset

    def _tag(self, name: str) -> int:
        if name not in self._tables:
            raise KeyError(f"font table {name!r} missing")
        return self._tables[name]

    def _u16at(self, off: int) -> int:
        return struct.unpack(">H", self._data[off : off + 2])[0]

    def _i16at(self, off: int) -> int:
        return struct.unpack(">h", self._data[off : off + 2])[0]

    def _u32at(self, off: int) -> int:
        return struct.unpack(">I", self._data[off : off + 4])[0]

    def _parse_head(self) -> None:
        head = self._tag("head")
        self.index_to_loc_format = self._i16at(head + 50)

    def _parse_hhea(self) -> None:
        hhea = self._tag("hhea")
        self.hhea_ascent = self._i16at(hhea + 4)
        self.hhea_descent = self._i16at(hhea + 6)
        self.num_h_metrics = self._u16at(hhea + 34)

    def _parse_maxp(self) -> None:
        maxp = self._tag("maxp")
        self.num_glyphs = self._u16at(maxp + 4)

    # -- cmap ------------------------------------------------------------
    def _parse_cmap(self) -> None:
        cmap = self._tag("cmap")
        count = self._u16at(cmap + 2)
        best = None
        for i in range(count):
            rec = cmap + 4 + i * 8
            pid = self._u16at(rec)
            eid = self._u16at(rec + 2)
            off = cmap + self._u32at(rec + 4)
            fmt = self._u16at(off)
            # Prefer full-repertoire format 12, fall back to format 4.
            score = 0
            if fmt == 12:
                score = 100 + (10 if (pid, eid) in ((3, 10), (0, 4)) else 0)
            elif fmt == 4:
                score = 50 + (10 if (pid, eid) in ((3, 1), (0, 3)) else 0)
            if score and (best is None or score > best[0]):
                best = (score, off, fmt)
        if best is None:
            raise ValueError("font has no usable cmap (needs format 4 or 12)")
        self._cmap_off = best[1]
        self._cmap_fmt = best[2]

    def glyph_id(self, codepoint: int) -> int:
        off = self._cmap_off
        d = self._data
        if self._cmap_fmt == 12:
            num_groups = self._u32at(off + 12)
            lo, hi = 0, num_groups - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                g = off + 16 + mid * 12
                start = self._u32at(g)
                end = self._u32at(g + 4)
                if codepoint < start:
                    hi = mid - 1
                elif codepoint > end:
                    lo = mid + 1
                else:
                    return self._u32at(g + 8) + (codepoint - start)
            return 0
        # format 4, binary search over the sorted segment end codes
        seg_x2 = self._u16at(off + 6)
        seg = seg_x2 // 2
        end_off = off + 14
        start_off = off + 16 + seg_x2
        delta_off = off + 16 + seg_x2 * 2
        range_off_base = off + 16 + seg_x2 * 3
        lo, hi = 0, seg - 1
        found = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._u16at(end_off + mid * 2) < codepoint:
                lo = mid + 1
            else:
                hi = mid - 1
            if self._u16at(start_off + mid * 2) <= codepoint:
                found = mid
        if found == -1:
            return 0
        delta = self._i16at(delta_off + found * 2)
        range_off = self._u16at(range_off_base + found * 2)
        if range_off == 0:
            return (codepoint + delta) & 0xFFFF
        addr = range_off_base + found * 2 + range_off + (codepoint - self._u16at(start_off + found * 2)) * 2
        if addr + 2 > len(d):
            return 0
        glyph = self._u16at(addr)
        return 0 if glyph == 0 else (glyph + delta) & 0xFFFF

    # -- hmtx / loca / glyf ----------------------------------------------
    @lru_cache(maxsize=None)
    def advance(self, gid: int) -> int:
        hmtx = self._tag("hmtx")
        idx = min(gid, self.num_h_metrics - 1)
        return self._u16at(hmtx + idx * 4)

    def _loca(self, gid: int) -> int:
        loca = self._tag("loca")
        if self.index_to_loc_format == 0:
            return self._u16at(loca + gid * 2) * 2
        return self._u32at(loca + gid * 4)

    @lru_cache(maxsize=None)
    def glyph_box(self, gid: int) -> GlyphBox:
        glyf = self._tag("glyf")
        start = self._loca(gid)
        end = self._loca(gid + 1)
        adv = self.advance(gid) / self.upem
        if start == end:
            return GlyphBox(adv, 0.0, 0.0, 0.0, 0.0, True)
        num_contours = self._i16at(glyf + start)
        x_min = self._i16at(glyf + start + 2)
        y_min = self._i16at(glyf + start + 4)
        x_max = self._i16at(glyf + start + 6)
        y_max = self._i16at(glyf + start + 8)
        if num_contours < 0:
            # Composite glyph: the stored bbox is authoritative.
            pass
        return GlyphBox(
            adv,
            x_min / self.upem,
            y_min / self.upem,
            x_max / self.upem,
            y_max / self.upem,
            False,
        )

    def glyph_for_char(self, ch: str) -> GlyphBox:
        return self.glyph_box(self.glyph_id(ord(ch)))

    # -- convenience -----------------------------------------------------
    def line_ink_bounds(self, text: str, size_px: float) -> tuple[float, float, float, float]:
        """Ink bounds of one line laid out on a baseline, in px relative to origin
        (origin = left edge of the pen at x=0, y=0 baseline).

        Returns (x_min, y_min, x_max, y_max) in px.
        """
        x = 0.0
        left = right = top = bottom = 0.0
        first = True
        for ch in text:
            box = self.glyph_for_char(ch)
            if not box.is_empty:
                lx = (x + box.x_min) * size_px
                rx = (x + box.x_max) * size_px
                by = box.y_min * size_px
                ty = box.y_max * size_px
                if first:
                    left, right, bottom, top = lx, rx, by, ty
                    first = False
                else:
                    left = min(left, lx)
                    right = max(right, rx)
                    bottom = min(bottom, by)
                    top = max(top, ty)
            x += box.advance
        if first:
            return (0.0, 0.0, 0.0, 0.0)
        return (left, bottom, right, top)

    def advance_width(self, text: str, size_px: float, spacing_px: float = 0.0) -> float:
        total = sum(self.glyph_for_char(ch).advance for ch in text) * size_px
        if spacing_px and len(text) > 1:
            total += spacing_px * (len(text) - 1)
        return total


@lru_cache(maxsize=None)
def load_font(path: str) -> TrueTypeFont:
    return TrueTypeFont(path)


def text_ink_extent(font: TrueTypeFont, text: str, size_px: float) -> tuple[float, float, float, float]:
    """Ink extent of a run of text with its pen origin at (0,0), baseline coords."""
    return font.line_ink_bounds(text, size_px)
