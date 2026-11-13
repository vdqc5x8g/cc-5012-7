"""HTTP API for the sign-language caption layout service.

The CLI in cli.py is intentionally only an auxiliary path (batch/export); the
graphical entry point is the React app served alongside this API.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import ffmpeg as ff
from .layout import effective_layout, evaluate_cue, evaluate_timeline, validate_placement
from .model import (
    AvoidRegion,
    CaptionCue,
    LayoutSettings,
    Project,
    ProjectStore,
    new_id,
)
from .fontmetrics import load_font

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
STATIC = os.path.join(ROOT, "frontend", "dist")
MEDIA = os.path.join(ROOT, "media")
EXPORTS = os.path.join(ROOT, "data", "exports")
PROJECTS = os.path.join(ROOT, "data", "projects")
FRAMES = os.path.join(ROOT, "data", "frames")
FONTS = os.path.join(ROOT, "assets", "fonts")
DEFAULT_FONT = os.path.join(FONTS, "SignCaptionSans-Regular.ttf")
DEFAULT_FONT_BOLD = os.path.join(FONTS, "SignCaptionSans-Bold.ttf")

store = ProjectStore(PROJECTS)
_jobs: dict[str, ff.ExportJob] = {}
_jobs_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


def _sanitize(name: str) -> str:
    return re.sub(r"[^\w\-.]", "_", name)


def create_project(payload: dict[str, Any]) -> Project:
    video = payload.get("videoPath") or os.path.join(MEDIA, "sample-signer.mp4")
    info = ff.probe(video)
    pid = new_id("prj")
    proj = Project(
        id=pid,
        name=payload.get("name") or "未命名手语字幕工程",
        video_path=video,
        width=info.width,
        height=info.height,
        fps=info.fps,
        duration=info.duration,
        font_path=DEFAULT_FONT,
        font_bold_path=DEFAULT_FONT_BOLD,
    )
    cues = payload.get("cues")
    if cues:
        proj.cues = [
            CaptionCue(id=new_id("cue"), start=float(c["start"]), end=float(c["end"]),
                       text=str(c["text"]))
            for c in cues
        ]
    regions = payload.get("regions")
    if regions:
        proj.regions = [_region_from_payload(r) for r in regions]
    if payload.get("settings"):
        proj.settings = LayoutSettings.from_dict(payload["settings"])
    proj.add_revision("project", "创建工程", {"video": video}, actor="human")
    store.save(proj)
    return proj


def _region_from_payload(r: dict[str, Any]) -> AvoidRegion:
    rect = r["rect"]
    if len(rect) != 4:
        raise ApiError(400, "区域 rect 需要 [x,y,w,h]")
    kind = r.get("kind", "custom")
    if kind not in ("signer", "sign", "important", "custom"):
        raise ApiError(400, f"未知区域类型 {kind}")
    return AvoidRegion(
        id=r.get("id") or new_id("reg"),
        label=r.get("label") or {"signer": "人物保护区", "sign": "手语动作区",
                                  "important": "重要画面", "custom": "自定义区域"}[kind],
        kind=kind,
        start=float(r.get("start", 0)),
        end=float(r.get("end", 1e9)),
        rect=[float(v) for v in rect],
        source=r.get("source", "manual"),
        note=r.get("note", ""),
    )


def _cue_geometry_payload(project: Project) -> dict[str, Any]:
    """Effective layout plus settings-derived display helpers for the UI."""
    eff = effective_layout(project)
    per_cue = eff["perCue"]
    return {
        "perCue": {
            cid: {
                "cueId": cid,
                "solved": v.get("solved"),
                "partial": v.get("partial", False),
                "stale": v.get("stale", False),
                "geometry": v.get("geometry"),
                "segments": v.get("segments"),
                "unsolved": v.get("unsolved"),
            }
            for cid, v in per_cue.items()
        },
        "unsolvedCueIds": eff["unsolvedCueIds"],
        "problems": eff["problems"],
        "annotationNotice": eff["annotationNotice"],
        "settings": project.settings.to_dict(),
        "frame": {"width": project.width, "height": project.height, "fps": project.fps,
                  "duration": project.duration},
    }


def _project_summary(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "video": {"path": project.video_path, "width": project.width,
                  "height": project.height, "fps": project.fps,
                  "duration": project.duration},
        "cueCount": len(project.cues),
        "regionCount": len(project.regions),
        "revCounter": project.rev_counter,
        "unsolvedCount": len(project.unsolved) if project.unsolved else 0,
    }


def _project_full(project: Project) -> dict[str, Any]:
    data = project.to_dict()
    data["layout"] = _cue_geometry_payload(project)
    data["videoUrl"] = f"/media/{os.path.basename(project.video_path)}"
    data["revisionsSummary"] = [r.to_dict() for r in project.revisions[-30:]]
    return data


def _ensure_dirs() -> None:
    for d in (MEDIA, EXPORTS, PROJECTS, FRAMES, FONTS):
        os.makedirs(d, exist_ok=True)


def _sample_media_path() -> str:
    return os.path.join(MEDIA, "sample-signer.mp4")


def ensure_sample_project() -> None:
    _ensure_dirs()
    sample_video = _sample_media_path()
    if not os.path.exists(sample_video):
        generate_sample(sample_video)
    existing = [p for p in store.list_ids()]
    if existing:
        return
    # Synthetic acceptance fixture: moving signer, two mutually exclusive
    # regions, a long caption, and a full-frame occupied stretch.
    proj = create_project({
        "name": "手语字幕验收样例",
        "videoPath": sample_video,
        "cues": [],
    })
    proj.cues = [
        CaptionCue(new_id("cue"), 0.5, 3.5,
                   "欢迎收看本期手语讲解，我们先看画面的左侧人物区域。"),
        CaptionCue(new_id("cue"), 3.5, 6.0,
                   "这一句字幕比较长，需要验证在移动的人物保护区之外能否稳定放置。"),
        CaptionCue(new_id("cue"), 6.0, 8.0,
                   "接下来画面被重要图文完全占用，请检查无解报告。"),
        CaptionCue(new_id("cue"), 8.0, 10.5,
                   "恢复后字幕回到稳定位置，不应该上下跳动。"),
        CaptionCue(new_id("cue"), 10.5, 12.5,
                   "最后一句，验证过期的重要区域不再阻挡后续字幕。"),
    ]
    # Moving signer window: approximate a moving person region.
    proj.regions = [
        AvoidRegion(new_id("reg"), "人物保护区（移动窗口）", "signer", 0.0, 12.5,
                    [220, 150, 380, 360]),
        AvoidRegion(new_id("reg"), "左侧互斥区域", "sign", 0.0, 5.0, [40, 60, 300, 240]),
        AvoidRegion(new_id("reg"), "右侧互斥区域", "sign", 5.0, 8.5, [940, 300, 300, 240]),
        AvoidRegion(new_id("reg"), "过期的重要图文", "important", 3.0, 7.5,
                    [800, 80, 380, 300]),
        AvoidRegion(new_id("reg"), "全画面占用", "custom", 7.0, 8.0,
                    [0, 0, 1280, 720]),
    ]
    proj.add_revision("project", "导入验收样例标注", {"regions": len(proj.regions)},
                      actor="fixture")
    store.save(proj)


def generate_sample(path: str) -> None:
    """Create the synthetic acceptance video (moving signer, regions change)."""
    ff_bin = ff.find_ffmpeg()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 1280x720 @25fps, 12.5s. A bright "signer" rectangle moves horizontally;
    # a "key graphic" block appears on the right from 6.0-7.5s; a full-frame
    # wash covers 7.0-8.0s.
    vf = (
        "drawbox=x='180+120*sin(t*0.55)':y='200+40*sin(t*0.9)':w=340:h=380:color=#d9a06a@1:t=fill,"
        "drawbox=x='250+120*sin(t*0.55)':y='170+40*sin(t*0.9)':w=140:h=140:color=#f0c9a8@1:t=fill,"
        "drawbox=x='840':y='90':w=320:h=250:color=#3c6e9c@1:t=fill:enable='between(t,5.6,7.6)',"
        "drawbox=x=0:y=0:w=1280:h=720:color=#222222@0.92:t=fill:enable='between(t,7.0,8.0)'"
    )
    args = [
        ff_bin, "-hide_banner", "-y",
        "-f", "lavfi", "-i", "color=c=#1b1e28:s=1280x720:d=12.5:r=25",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", path,
    ]
    import subprocess

    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ff.FFmpegError(proc.stderr[-1200:])


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "SignSub/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
        if os.environ.get("SIGSUB_HTTP_LOG"):
            super().log_message(fmt, *args)

    # -- helpers ---------------------------------------------------------
    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json({"error": message, "status": status}, status)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiError(400, f"JSON 解析失败: {exc}")
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    def _serve_file(self, path: str, content_type: str | None = None,
                    download_name: str | None = None) -> None:
        if not os.path.exists(path) or not os.path.isfile(path):
            self._send_error(404, "文件不存在")
            return
        ctype = content_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
        size = os.path.getsize(path)
        range_header = self.headers.get("Range")
        self.send_response(206 if range_header else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if download_name:
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{urllib.parse.quote(download_name)}",
            )
        start, end = 0, size - 1
        if range_header:
            m = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                end = min(end, size - 1)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    # -- routing ---------------------------------------------------------
    def do_GET(self) -> None:
        try:
            self._route_get()
        except ApiError as e:
            self._send_error(e.status, e.message)
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            self._send_error(500, str(e))

    def do_POST(self) -> None:
        try:
            self._route_post()
        except ApiError as e:
            self._send_error(e.status, e.message)
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            self._send_error(500, str(e))

    def do_DELETE(self) -> None:
        try:
            self._route_delete()
        except ApiError as e:
            self._send_error(e.status, e.message)
        except Exception as e:  # noqa: BLE001
            self._send_error(500, str(e))

    def do_PUT(self) -> None:
        try:
            self._route_put()
        except ApiError as e:
            self._send_error(e.status, e.message)
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            self._send_error(500, str(e))

    def _route_get(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)

        if path.startswith("/api/"):
            return self._api_get(path, query)
        if path.startswith("/media/"):
            name = _sanitize(os.path.basename(path))
            return self._serve_file(os.path.join(MEDIA, name))
        if path.startswith("/frames/"):
            name = _sanitize(os.path.basename(path))
            return self._serve_file(os.path.join(FRAMES, name))
        if path.startswith("/exports/"):
            name = _sanitize(os.path.basename(path))
            dl = query.get("download", [None])[0]
            return self._serve_file(
                os.path.join(EXPORTS, name),
                download_name=os.path.basename(name) if dl else None,
            )
        if path.startswith("/fonts/"):
            name = _sanitize(os.path.basename(path))
            return self._serve_file(os.path.join(FONTS, name), "font/ttf")
        # SPA static
        return self._serve_static(path)

    def _serve_static(self, path: str) -> None:
        if not os.path.isdir(STATIC):
            self._send_error(
                503,
                "前端尚未构建。开发时请运行 npm run dev（Vite），"
                "生产构建后本服务会直接托管 frontend/dist。",
            )
            return
        rel = path.lstrip("/") or "index.html"
        target = os.path.normpath(os.path.join(STATIC, rel))
        if not target.startswith(STATIC):
            self._send_error(403, "非法路径")
            return
        if os.path.isdir(target) or not os.path.exists(target):
            target = os.path.join(STATIC, "index.html")
        return self._serve_file(target)

    def _project_or_404(self, pid: str) -> Project:
        try:
            return store.get(pid)
        except KeyError:
            raise ApiError(404, f"工程 {pid} 不存在")

    def _api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/health":
            return self._send_json({"ok": True, "ffmpeg": ff.find_ffmpeg()})
        if path == "/api/fonts":
            return self._send_json({"regular": DEFAULT_FONT, "bold": DEFAULT_FONT_BOLD,
                                    "family": "SignCaption Sans"})
        if path == "/api/projects":
            out, errors = [], []
            for pid in store.list_ids():
                try:
                    out.append(_project_summary(store.get(pid)))
                except Exception as exc:  # noqa: BLE001
                    errors.append({"id": pid, "error": str(exc)})
            out.sort(key=lambda x: x["name"])
            return self._send_json({"projects": out, "errors": errors})
        if path == "/api/projects/sample":
            ensure_sample_project()
            return self._send_json({"ok": True})
        m = re.match(r"^/api/projects/([\w-]+)$", path)
        if m:
            return self._send_json(_project_full(self._project_or_404(m.group(1))))
        m = re.match(r"^/api/projects/([\w-]+)/revisions$", path)
        if m:
            proj = self._project_or_404(m.group(1))
            return self._send_json({"revisions": [r.to_dict() for r in proj.revisions]})
        m = re.match(r"^/api/projects/([\w-]+)/layout$", path)
        if m:
            proj = self._project_or_404(m.group(1))
            return self._send_json(_cue_geometry_payload(proj))
        m = re.match(r"^/api/projects/([\w-]+)/cues/([\w-]+)/candidates$", path)
        if m:
            proj = self._project_or_404(m.group(1))
            cue = next((c for c in proj.cues if c.id == m.group(2)), None)
            if cue is None:
                raise ApiError(404, "字幕不存在")
            # Use the timeline result so the chosen/sticky position shown here is
            # the same one the exporter will burn (stability is timeline-wide).
            res = evaluate_timeline(proj).get(cue.id)
            if res is None:
                res = evaluate_cue(proj, cue, load_font(proj.font_path))
            return self._send_json(res.to_dict())
        m = re.match(r"^/api/projects/([\w-]+)/frames/([\d.]+)\.jpg$", path)
        if m:
            return self._serve_frame(m.group(1), float(m.group(2)))
        m = re.match(r"^/api/projects/([\w-]+)/composite/([\d.]+)\.png$", path)
        if m:
            return self._serve_composite(m.group(1), float(m.group(2)))
        m = re.match(r"^/api/jobs/([\w-]+)$", path)
        if m:
            with _jobs_lock:
                job = _jobs.get(m.group(1))
            if job is None:
                raise ApiError(404, "任务不存在")
            return self._send_json(job.to_dict())
        raise ApiError(404, f"未知接口 {path}")

    # -- frame passthrough (seek-accurate source frames) ------------------
    def _frame_path(self, proj: Project, t: float, suffix: str = "jpg") -> str:
        os.makedirs(FRAMES, exist_ok=True)
        key = f"{proj.id}-{int(round(t * max(1.0, proj.fps))):06d}.{suffix}"
        return os.path.join(FRAMES, key)

    def _extract_frame(self, video: str, t: float, out_path: str, suffix: str) -> bool:
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True
        ff_bin = ff.find_ffmpeg()
        fmt = ["-q:v", "3"] if suffix == "jpg" else []
        args = [ff_bin, "-hide_banner", "-v", "error", "-y",
                "-ss", f"{max(0.0, t):.4f}", "-i", video, "-frames:v", "1"]
        args += fmt + [out_path]
        proc = subprocess.run(args, capture_output=True, text=True)
        return proc.returncode == 0 and os.path.exists(out_path)

    def _serve_frame(self, pid: str, t: float) -> None:
        proj = self._project_or_404(pid)
        out = self._frame_path(proj, t, "jpg")
        if not self._extract_frame(proj.video_path, t, out, "jpg"):
            raise ApiError(500, "抽帧失败")
        return self._serve_file(out, "image/jpeg")

    def _serve_composite(self, pid: str, t: float) -> None:
        """Burn the current layout's cues onto a single source frame (preview aid)."""
        proj = self._project_or_404(pid)
        from .ass import build_ass
        from .layout import effective_layout

        src = self._frame_path(proj, t, "jpg")
        if not self._extract_frame(proj.video_path, t, src, "jpg"):
            raise ApiError(500, "抽帧失败")
        out = self._frame_path(proj, t, f"comp-{int(time.time()*1000)}.png")
        ass_text, _, _ = build_ass(proj, effective_layout(proj)["perCue"])
        work = os.path.join(FRAMES, f"{proj.id}.preview.ass")
        with open(work, "w", encoding="utf-8") as fh:
            fh.write(ass_text)
        ff_bin = ff.find_ffmpeg()
        asc = work.replace("\\", "/").replace(":", "\\:")
        fonts = ff.find_fonts_dir().replace("\\", "/").replace(":", "\\:")
        args = [ff_bin, "-hide_banner", "-v", "error", "-y", "-loop", "1", "-i", src,
                "-vf", f"ass={asc}:fontsdir={fonts}", "-frames:v", "1",
                "-ss", f"{t:.4f}", out]
        subprocess.run(args, capture_output=True, text=True)
        return self._serve_file(out, "image/png")

    # -- writes ----------------------------------------------------------
    def _route_post(self) -> None:
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if path == "/api/projects":
            payload = self._read_json()
            proj = create_project(payload)
            return self._send_json(_project_full(proj), 201)
        if path == "/api/projects/sample":
            ensure_sample_project()
            return self._send_json({"ok": True})
        m = re.match(r"^/api/projects/([\w-]+)/regions$", path)
        if m:
            proj = self._project_or_404(m.group(1))
            payload = self._read_json()
            region = _region_from_payload(payload)
            proj.regions.append(region)
            proj.add_revision("regions", f"新增区域：{region.label}",
                              {"region": region.to_dict()})
            store.save(proj)
            return self._send_json(_project_full(proj), 201)
        m = re.match(r"^/api/projects/([\w-]+)/cues$", path)
        if m:
            proj = self._project_or_404(m.group(1))
            payload = self._read_json()
            cue = CaptionCue(
                id=payload.get("id") or new_id("cue"),
                start=float(payload.get("start", 0)),
                end=float(payload.get("end", 2)),
                text=str(payload.get("text", "")),
            )
            if cue.end <= cue.start:
                raise ApiError(400, "字幕结束时间必须大于开始时间")
            proj.cues.append(cue)
            proj.add_revision("cues", "新增字幕", {"cue": cue.to_dict()})
            store.save(proj)
            return self._send_json(_project_full(proj), 201)
        m = re.match(r"^/api/projects/([\w-]+)/cues/([\w-]+)/adopt$", path)
        if m:
            return self._adopt(m.group(1), m.group(2))
        m = re.match(r"^/api/projects/([\w-]+)/cues/([\w-]+)/validate$", path)
        if m:
            return self._validate(m.group(1), m.group(2))
        m = re.match(r"^/api/projects/([\w-]+)/export$", path)
        if m:
            return self._start_export(m.group(1))
        if path == "/api/sample/regenerate":
            ensure_sample_project()
            return self._send_json({"ok": True})
        raise ApiError(404, f"未知接口 {path}")

    def _route_put(self) -> None:
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        m = re.match(r"^/api/projects/([\w-]+)$", path)
        if m:
            proj = self._project_or_404(m.group(1))
            payload = self._read_json()
            if "name" in payload:
                proj.name = str(payload["name"])
            if "settings" in payload:
                proj.settings = LayoutSettings.from_dict(payload["settings"])
                proj.add_revision("settings", "更新排版设置", payload["settings"])
            if "cues" in payload:
                proj.cues = [CaptionCue(**c) for c in payload["cues"]]
                proj.add_revision("cues", "更新时间线字幕", {"count": len(proj.cues)})
            store.save(proj)
            return self._send_json(_project_full(proj))
        m = re.match(r"^/api/projects/([\w-]+)/regions/([\w-]+)$", path)
        if m:
            proj = self._project_or_404(m.group(1))
            payload = self._read_json()
            region = next((r for r in proj.regions if r.id == m.group(2)), None)
            if region is None:
                raise ApiError(404, "区域不存在")
            if "rect" in payload:
                region.rect = [float(v) for v in payload["rect"]]
            for k in ("label", "start", "end", "note", "kind"):
                if k in payload:
                    setattr(region, k, payload[k] if k in ("label", "note", "kind")
                            else float(payload[k]))
            proj.add_revision("regions", f"修改区域：{region.label}",
                              {"region": region.to_dict()})
            store.save(proj)
            return self._send_json(_project_full(proj))
        raise ApiError(404, f"未知接口 {path}")

    def _route_delete(self) -> None:
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        m = re.match(r"^/api/projects/([\w-]+)$", path)
        if m:
            store.delete(m.group(1))
            return self._send_json({"ok": True})
        m = re.match(r"^/api/projects/([\w-]+)/regions/([\w-]+)$", path)
        if m:
            proj = self._project_or_404(m.group(1))
            before = len(proj.regions)
            proj.regions = [r for r in proj.regions if r.id != m.group(2)]
            if len(proj.regions) == before:
                raise ApiError(404, "区域不存在")
            proj.add_revision("regions", "删除区域", {"regionId": m.group(2)})
            store.save(proj)
            return self._send_json(_project_full(proj))
        m = re.match(r"^/api/projects/([\w-]+)/cues/([\w-]+)$", path)
        if m:
            proj = self._project_or_404(m.group(1))
            before = len(proj.cues)
            proj.cues = [c for c in proj.cues if c.id != m.group(2)]
            if len(proj.cues) == before:
                raise ApiError(404, "字幕不存在")
            proj.placements.pop(m.group(2), None)
            proj.add_revision("cues", "删除字幕", {"cueId": m.group(2)})
            store.save(proj)
            return self._send_json(_project_full(proj))
        raise ApiError(404, f"未知接口 {path}")

    # -- actions ---------------------------------------------------------
    def _adopt(self, pid: str, cue_id: str) -> None:
        proj = self._project_or_404(pid)
        payload = self._read_json()
        cue = next((c for c in proj.cues if c.id == cue_id), None)
        if cue is None:
            raise ApiError(404, "字幕不存在")
        # Candidate indices must match the list served by GET .../candidates,
        # which is the timeline evaluation (stability affects ranking).
        res = evaluate_timeline(proj).get(cue_id)
        if res is None:
            res = evaluate_cue(proj, cue, load_font(proj.font_path))
        idx = int(payload.get("candidateIndex", 0))
        if payload.get("x") is not None:
            x = float(payload["x"])
            y = float(payload["y"])
            font_px = float(payload.get("fontPx", proj.settings.font_size))
            lines = payload.get("lines") or (res.chosen.lines if res.chosen else [cue.text])
            mode = "manual"
        else:
            if idx < 0 or idx >= len(res.candidates):
                raise ApiError(400, "候选序号无效")
            cand = res.candidates[idx]
            if not cand.valid:
                raise ApiError(409, "该候选与标注区域冲突，不能采用")
            x, y, font_px, lines = cand.x, cand.y, cand.font_px, cand.lines
            mode = "auto"
        geo, conflicts, probs = validate_placement(proj, cue, x, y, font_px, lines)
        if probs:
            raise ApiError(409, "；".join(probs))
        from .model import Placement

        rev_n = proj.rev_counter + 1
        proj.placements[cue_id] = Placement(
            cue_id=cue_id, x=x, y=y, lines=lines, font_size=font_px,
            mode=mode, solved=True, adopted_revision=rev_n,
            adopted_at=time.time(), adopted_by="human",
        )
        proj.add_revision(
            "adopt", f"采用字幕布局（{mode}）",
            {"cueId": cue_id, "x": x, "y": y, "fontPx": font_px, "lines": lines},
        )
        store.save(proj)
        return self._send_json(_project_full(proj))

    def _validate(self, pid: str, cue_id: str) -> None:
        proj = self._project_or_404(pid)
        payload = self._read_json()
        cue = next((c for c in proj.cues if c.id == cue_id), None)
        if cue is None:
            raise ApiError(404, "字幕不存在")
        geo, conflicts, probs = validate_placement(
            proj, cue,
            float(payload["x"]), float(payload["y"]),
            float(payload.get("fontPx", proj.settings.font_size)),
            payload.get("lines") or [cue.text],
        )
        return self._send_json({
            "geometry": geo.to_dict(),
            "conflicts": [c.to_dict() for c in conflicts],
            "problems": probs,
            "valid": not probs,
        })

    def _start_export(self, pid: str) -> None:
        proj = self._project_or_404(pid)
        eff = effective_layout(proj)
        per_cue = eff["perCue"]
        for cid in eff["unsolvedCueIds"]:
            if cid in per_cue and not per_cue[cid].get("stale"):
                pass
        if not any(v.get("solved") for v in per_cue.values()):
            raise ApiError(409, "没有可导出的已解出字幕；请先处理无解项或采用布局。")
        job_id = new_id("job")
        job = ff.ExportJob()
        with _jobs_lock:
            _jobs[job_id] = job
        os.makedirs(EXPORTS, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = os.path.join(EXPORTS, f"{_sanitize(proj.name)}-{stamp}.mp4")
        ref = os.path.join(EXPORTS, f".ref-{_sanitize(proj.name)}-{stamp}.mp4")

        def run() -> None:
            try:
                res = ff.export_with_subtitles(proj, per_cue, out, job=job, reference_path=ref)
                job.message = "正在逐帧核验导出结果"
                job.progress = 0.95
                verification = ff.verify_export(
                    proj, per_cue, proj.video_path, out, ref
                )
                job.result = res
                job.verification = verification
                job.progress = 1.0
                job.state = "done"
                job.message = "导出完成" if verification["ok"] else "导出完成，但核验发现问题"
                # Persist a revision pointing at the export artifact.
                proj.add_revision(
                    "export", "导出字幕视频",
                    {"output": os.path.basename(out), "ass": os.path.basename(res.ass_path),
                     "skipped": res.skipped_cues, "verified": verification["ok"]},
                )
                store.save(proj)
            except Exception as exc:  # noqa: BLE001
                job.state = "error"
                job.message = str(exc)
                job.progress = min(job.progress, 0.99)
            finally:
                try:
                    if os.path.exists(ref):
                        pass  # keep reference for re-check entry
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=run, daemon=True).start()
        return self._send_json({"jobId": job_id}, 202)


def build_server(host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    _ensure_dirs()
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="手语字幕布局服务")
    ap.add_argument("--host", default=os.environ.get("SIGSUB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("SIGSUB_PORT", "8787")))
    ap.add_argument("--no-sample", action="store_true", help="不自动创建验收样例")
    args = ap.parse_args()
    if not args.no_sample:
        ensure_sample_project()
    server = build_server(args.host, args.port)
    print(f"手语字幕服务已启动: http://{args.host}:{args.port}")
    print(f"工程目录: {PROJECTS}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
