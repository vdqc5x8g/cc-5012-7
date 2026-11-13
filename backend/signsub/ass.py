"""ASS subtitle generation.

Positions are emitted per line with explicit \\an7\\pos() so that libass never
re-wraps or re-spaces our pre-computed layout.  The bundled font has
ascent+descent == upem, so `fontsize` equals em pixels; the layout engine and
libass therefore agree on glyph advances to within antialiasing.
"""

from __future__ import annotations

from .model import Project


def _ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", " ")
    )


def build_ass(
    project: Project,
    per_cue: dict,
    font_family: str = "SignCaption Sans",
) -> tuple[str, list[str], list[tuple[str, float, float]]]:
    """Build the ASS document.

    Returns (ass_text, skipped_cue_ids, drawn_segments) where drawn_segments is
    a list of (cue_id, start, end) actually emitted.  A cue with partial
    occlusion may emit one event per solvable time segment.
    """
    s = project.settings
    bold = -1 if s.bold else 0
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {project.width}",
        f"PlayResY: {project.height}",
        "WrapStyle: 2",  # never re-wrap; lines are pre-computed
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # White fill, near-black outline. Alignment 7 (top-left) because every
        # line is positioned explicitly.
        f"Style: SignCaption,{font_family},{s.font_size:.2f},&H00FFFFFF,&H000000FF,"
        f"&H00141414,&H00000000,{bold},0,0,0,100,100,0,0,1,{s.outline:.2f},0,7,0,0,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events: list[str] = []
    skipped: list[str] = []
    drawn: list[tuple[str, float, float]] = []

    def emit(cue: CaptionCue, geo: dict, start: float, end: float) -> None:
        if end - start <= 1e-3:
            return
        size = geo["fontPx"]
        ascent_px = 0.88 * size
        for line in geo["lines"]:
            x = line["penX"]
            y = line["baselineY"] - ascent_px
            text = _escape(line["text"])
            events.append(
                f"Dialogue: 0,{_ts(start)},{_ts(end)},SignCaption,,0,0,0,,"
                f"{{\\an7\\fs{size:.2f}\\pos({x:.2f},{y:.2f})\\q2}}{text}"
            )
        drawn.append((cue.id, start, end))

    for cue in sorted(project.cues, key=lambda c: (c.start, c.id)):
        entry = per_cue.get(cue.id)
        if not entry:
            skipped.append(cue.id)
            continue
        segs = entry.get("segments")
        if segs:
            any_drawn = False
            for seg in segs:
                if seg.get("solved") and seg.get("geometry"):
                    emit(cue, seg["geometry"], seg["start"], seg["end"])
                    any_drawn = True
            if not any_drawn:
                skipped.append(cue.id)
            continue
        if not entry.get("solved") or not entry.get("geometry"):
            skipped.append(cue.id)
            continue
        emit(cue, entry["geometry"], cue.start, cue.end)
    return "\n".join(header + events) + "\n", skipped, drawn
