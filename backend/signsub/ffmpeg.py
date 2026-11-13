"""FFmpeg integration: probing, export, and frame-accurate post-export checks.

Verification method
-------------------
The export burns subtitles with libass into the encoded video.  To check the
*actual exported pixels* (not the intended layout), we also encode a reference
from the same source with identical encoder settings but no subtitle filter.
x264 is deterministic for identical input and settings, so the only differing
pixels are the burned captions.  A difference blend followed by FFmpeg's `bbox`
filter yields the exact bounding box of painted caption pixels per frame.
Cropping the difference to each protected region tells us whether any caption
pixel landed inside it, and the uncropped bbox tells us whether any caption
pixel escaped the safe area.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .ass import build_ass
from .model import Project, Rect

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))

_duration_re = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")
_video_re = re.compile(r"Stream #0:\d+.*?: Video: .*?, (\d+)x(\d+)")
_fps_re = re.compile(r"([\d.]+) fps")
_bbox_re = re.compile(r"Parsed_bbox_\d+ @ .*? n:\d+ pts:\S+ pts_time:\S+(.*)")


class FFmpegError(RuntimeError):
    pass


def find_ffmpeg() -> str:
    for key in ("SIGSUB_FFMPEG", "FFMPEG"):
        p = os.environ.get(key)
        if p and os.path.exists(p):
            return p
    p = shutil.which("ffmpeg")
    if p:
        return p
    candidates = [
        os.path.join(ROOT, "node_modules", "ffmpeg-static", "ffmpeg"),
        os.path.join(ROOT, "tools", "ffmpeg"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise FFmpegError(
        "找不到 FFmpeg。请运行 scripts/setup.sh（npm install 会安装 ffmpeg-static），"
        "或通过环境变量 SIGSUB_FFMPEG 指定路径。"
    )


def find_fonts_dir() -> str:
    return os.path.join(ROOT, "assets", "fonts")


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    duration: float
    has_audio: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "duration": round(self.duration, 3),
            "hasAudio": self.has_audio,
        }


def probe(video_path: str) -> VideoInfo:
    ff = find_ffmpeg()
    out = subprocess.run(
        [ff, "-hide_banner", "-i", video_path],
        capture_output=True, text=True,
    ).stderr
    dur = 0.0
    m = _duration_re.search(out)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    vm = _video_re.search(out)
    if not vm:
        raise FFmpegError(f"无法读取视频流信息: {out[-800:]}")
    width, height = int(vm.group(1)), int(vm.group(2))
    fm = _fps_re.search(out)
    fps = float(fm.group(1)) if fm else 25.0
    has_audio = "Audio:" in out
    if dur <= 0:
        raise FFmpegError("视频时长无效")
    return VideoInfo(width, height, fps, dur, has_audio)


@dataclass
class ExportResult:
    output: str
    ass_path: str
    skipped_cues: list[str]
    rendered_cues: int
    drawn_segments: list[list[Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": self.output,
            "assPath": self.ass_path,
            "skippedCues": self.skipped_cues,
            "renderedCues": self.rendered_cues,
            "drawnSegments": self.drawn_segments,
        }


def _encoder_args(project: Project) -> list[str]:
    return [
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]


def encode_reference(project: Project, output_path: str) -> str:
    """Encode the source with the same settings but without subtitles."""
    ff = find_ffmpeg()
    args = [ff, "-hide_banner", "-y", "-i", project.video_path]
    args += _encoder_args(project)
    args += ["-an", output_path]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(proc.stderr[-1500:])
    return output_path


class ExportJob:
    def __init__(self) -> None:
        self.progress = 0.0
        self.state = "pending"  # pending | running | done | error
        self.message = ""
        self.result: ExportResult | None = None
        self.verification: dict[str, Any] | None = None
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "progress": round(self.progress, 4),
            "message": self.message,
            "result": self.result.to_dict() if self.result else None,
            "verification": self.verification,
        }


def export_with_subtitles(
    project: Project,
    per_cue: dict,
    output_path: str,
    job: ExportJob | None = None,
    reference_path: str | None = None,
) -> ExportResult:
    ff = find_ffmpeg()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    ass_text, skipped, drawn = build_ass(project, per_cue)
    workdir = os.path.dirname(os.path.abspath(output_path))
    ass_path = os.path.join(workdir, os.path.basename(output_path) + ".ass")
    with open(ass_path, "w", encoding="utf-8") as fh:
        fh.write(ass_text)
    rendered = sum(
        1 for c in project.cues if (per_cue.get(c.id) or {}).get("solved")
    )
    fontsdir = find_fonts_dir()
    # ass filter path escaping: use forward slashes and escape colon/comma.
    ass_arg = ass_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    fonts_arg = fontsdir.replace("\\", "/").replace(":", "\\:")
    vf = f"ass={ass_arg}:fontsdir={fonts_arg}"

    args = [ff, "-hide_banner", "-y", "-i", project.video_path]
    args += ["-vf", vf, "-map", "0:v:0", "-map", "0:a?"]
    args += _encoder_args(project)
    args += ["-progress", "pipe:1", "-nostats", output_path]

    if job:
        job.state = "running"
        job.message = "正在导出字幕视频"
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )
    assert proc.stdout is not None
    last_err = ""
    try:
        for line in proc.stdout:
            if job and job.cancelled():
                proc.kill()
                raise FFmpegError("已取消")
            if line.startswith("out_time_ms=") and job is not None:
                try:
                    ms = int(line.split("=", 1)[1])
                    if project.duration > 0:
                        job.progress = min(0.9, 0.9 * (ms / 1_000_000) / project.duration)
                except ValueError:
                    pass
            elif line.startswith("progress=") and line.strip().endswith("end"):
                if job:
                    job.progress = 0.99
        stderr = proc.stderr.read() if proc.stderr else ""
    finally:
        proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
    proc.wait()
    if proc.returncode != 0:
        raise FFmpegError(stderr[-2000:] or last_err)

    if job:
        job.progress = 0.92
        job.message = "正在生成逐帧核验基准"
    if reference_path:
        encode_reference(project, reference_path)

    return ExportResult(output=output_path, ass_path=ass_path, skipped_cues=skipped,
                        rendered_cues=rendered,
                        drawn_segments=[[cid, round(a, 3), round(b, 3)] for cid, a, b in drawn])


# ---------------------------------------------------------------------------
# Post-export verification
# ---------------------------------------------------------------------------


@dataclass
class RegionHit:
    region_id: str
    label: str
    bbox: list[int] | None  # diff bbox inside region, frame coords
    frames_hit: int
    first_time: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "regionId": self.region_id,
            "label": self.label,
            "bbox": self.bbox,
            "framesHit": self.frames_hit,
            "firstTime": self.first_time,
        }


@dataclass
class CueCheck:
    cue_id: str
    solved: bool
    rendered_pixels: bool
    bbox: list[int] | None
    outside_safe: bool
    region_hits: list[RegionHit] = field(default_factory=list)
    ok: bool = True
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cueId": self.cue_id,
            "solved": self.solved,
            "renderedPixels": self.rendered_pixels,
            "bbox": self.bbox,
            "outsideSafe": self.outside_safe,
            "regionHits": [h.to_dict() for h in self.region_hits],
            "ok": self.ok,
            "note": self.note,
        }


def _safe_rect(project: Project) -> Rect:
    s = project.settings
    return Rect(s.safe_margin, s.safe_margin,
                project.width - 2 * s.safe_margin, project.height - 2 * s.safe_margin)


def _run_diff_bbox(
    source: str,
    export: str,
    start: float,
    end: float,
    crop: tuple[int, int, int, int] | None,
    threshold: int,
) -> tuple[list[dict[str, Any]], str]:
    """Measure the caption-pixel bbox of frames with start <= t < end.

    Frame selection uses an explicit half-open time predicate on both inputs;
    `trim`/`-ss` boundary handling can include one extra frame on one side and
    contaminate the diff, which shows up as full-frame noise.
    """
    ff = find_ffmpeg()
    sel = f"select='gte(t,{start:.4f})*lt(t,{end:.4f})',setpts=N/FRAME_RATE/TB"
    chain = (
        f"[0:v]format=rgb24,{sel}[a];"
        f"[1:v]format=rgb24,{sel}[b];"
        f"[a][b]blend=difference"
    )
    if crop:
        x, y, w, h = crop
        chain += f",crop={max(1, w)}:{max(1, h)}:{max(0, x)}:{max(0, y)}"
    chain += f",bbox=min_val={threshold}"
    args = [ff, "-hide_banner", "-nostats", "-vsync", "0",
            "-i", source, "-i", export,
            "-filter_complex", chain, "-f", "null", "-"]
    proc = subprocess.run(args, capture_output=True, text=True)
    frames = []
    for line in proc.stderr.splitlines():
        m = _bbox_re.search(line)
        if not m:
            continue
        tail = m.group(1)
        entry: dict[str, Any] = {}
        tm = re.search(r"pts_time:([\d.]+)", line)
        entry["t"] = float(tm.group(1)) if tm else None
        vals = dict(re.findall(r"(x1|x2|y1|y2|w|h):(\d+)", tail))
        if vals:
            entry.update({k: int(v) for k, v in vals.items()})
            if crop:
                entry["x1"] += crop[0]
                entry["x2"] += crop[0]
                entry["y1"] += crop[1]
                entry["y2"] += crop[1]
        frames.append(entry)
    return frames, proc.stderr


MIN_ARTIFACT_PIXELS = 24  # ignore isolated encoder noise, keep real glyph strokes


def verify_export(
    project: Project,
    per_cue: dict,
    source_path: str,
    export_path: str,
    reference_path: str,
    sample_fps: float | None = None,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Frame-accurate verification of the real exported video."""
    safe = _safe_rect(project)
    s = project.settings
    thr = s.verify_threshold
    cues = sorted(project.cues, key=lambda c: (c.start, c.id))
    checks: dict[str, CueCheck] = {}

    # Reference vs export, per cue time range, full frame: caption bbox + bounds.
    from concurrent.futures import ThreadPoolExecutor

    def cue_intervals(cue) -> list[tuple[float, float, bool]]:
        entry = per_cue.get(cue.id) or {}
        segs = entry.get("segments")
        if segs:
            out = [(float(s["start"]), float(s["end"]), bool(s["solved"])) for s in segs]
            return [(a, b, ok) for a, b, ok in out if b - a > 1e-3]
        if entry.get("solved"):
            return [(cue.start, cue.end, True)]
        return [(cue.start, cue.end, False)]

    def check_cue(cue) -> CueCheck:
        entry = per_cue.get(cue.id) or {}
        solved_any = bool(entry.get("solved"))
        intervals = cue_intervals(cue)
        any_pixels = False
        any_outside = False
        bbox_all: list[int] | None = None
        hits: list[RegionHit] = []
        drawn_empty = False
        occluded_but_drawn = False
        for (a, b, ok) in intervals:
            frames, _ = _run_diff_bbox(source_path, export_path, a, b, None, thr)
            nonempty = [f for f in frames if "x1" in f]
            if ok:
                if not nonempty:
                    drawn_empty = True
                else:
                    any_pixels = True
                    x1 = min(f["x1"] for f in nonempty)
                    x2 = max(f["x2"] for f in nonempty)
                    y1 = min(f["y1"] for f in nonempty)
                    y2 = max(f["y2"] for f in nonempty)
                    if bbox_all is None:
                        bbox_all = [x1, y1, x2 - x1 + 1, y2 - y1 + 1]
                    else:
                        bx, by, bw, bh = bbox_all
                        nx0 = min(bx, x1); ny0 = min(by, y1)
                        nx1 = max(bx + bw - 1, x2); ny1 = max(by + bh - 1, y2)
                        bbox_all = [nx0, ny0, nx1 - nx0 + 1, ny1 - ny0 + 1]
                    tol = 1
                    if (x1 < safe.x - tol or y1 < safe.y - tol
                            or x2 + 1 > safe.right() + tol or y2 + 1 > safe.bottom() + tol):
                        any_outside = True
                for r in project.regions:
                    if not (r.start < b and r.end > a):
                        continue
                    rs = max(a, r.start)
                    re_ = min(b, r.end)
                    if re_ <= rs:
                        continue
                    rect = r.rect_obj()
                    rf, _ = _run_diff_bbox(source_path, export_path, rs, re_,
                                           (int(rect.x), int(rect.y), int(rect.w), int(rect.h)), thr)
                    rn = [f for f in rf if "x1" in f]
                    if rn:
                        rx1 = min(f["x1"] for f in rn)
                        rx2 = max(f["x2"] for f in rn)
                        ry1 = min(f["y1"] for f in rn)
                        ry2 = max(f["y2"] for f in rn)
                        hits.append(RegionHit(
                            region_id=r.id, label=r.label,
                            bbox=[rx1, ry1, rx2 - rx1 + 1, ry2 - ry1 + 1],
                            frames_hit=len(rn),
                            first_time=rn[0].get("t"),
                        ))
            else:
                # Fully occluded stretch must contain no caption pixels.
                if nonempty:
                    occluded_but_drawn = True
                    any_pixels = True
        ok = not any_outside and not hits
        note = ""
        if solved_any and drawn_empty:
            ok = False
            note = "已解出布局但导出画面中未检测到字幕像素"
        if occluded_but_drawn:
            ok = False
            note = "完全遮挡时段仍检测到字幕像素"
        if not solved_any and any_pixels:
            ok = False
            note = "无解字幕却被渲染"
        if entry.get("partial"):
            note = (note + "；" if note else "") + "该字幕部分时段被完全遮挡，已按可解时段渲染"
        return CueCheck(
            cue_id=cue.id, solved=solved_any, rendered_pixels=any_pixels, bbox=bbox_all,
            outside_safe=any_outside, region_hits=hits, ok=ok, note=note,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for chk in pool.map(check_cue, cues):
            checks[chk.cue_id] = chk

    all_ok = all(c.ok for c in checks.values())
    return {
        "ok": all_ok,
        "threshold": thr,
        "safeRect": [safe.x, safe.y, safe.w, safe.h],
        "cues": {k: v.to_dict() for k, v in checks.items()},
        "summary": {
            "cues": len(checks),
            "withPixels": sum(1 for c in checks.values() if c.rendered_pixels),
            "outsideSafe": sum(1 for c in checks.values() if c.outside_safe),
            "regionHits": sum(1 for c in checks.values() if c.region_hits),
            "failed": [k for k, v in checks.items() if not v.ok],
        },
        "method": "reference/export 差异像素 + bbox 逐帧测量（真实导出文件）",
    }
