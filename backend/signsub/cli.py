"""Auxiliary command-line interface.

The graphical entry point is the React app (see docs/USAGE.md); this CLI exists
only for batch inspection and export, and never becomes the primary workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import ffmpeg as ff
from .layout import effective_layout
from .model import ProjectStore

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
PROJECTS = os.environ.get("SIGSUB_PROJECTS") or os.path.join(ROOT, "data", "projects")
EXPORTS = os.environ.get("SIGSUB_EXPORTS") or os.path.join(ROOT, "data", "exports")


def _store() -> ProjectStore:
    return ProjectStore(PROJECTS)


def _fmt_rect(v) -> str:
    return "[" + ", ".join(f"{x:.0f}" for x in v) + "]"


def cmd_list(_args: argparse.Namespace) -> int:
    store = _store()
    ids = store.list_ids()
    if not ids:
        print("没有工程。启动图形界面会自动创建验收样例。")
        return 0
    for pid in ids:
        p = store.get(pid)
        eff = effective_layout(p)
        print(
            f"{p.id}  {p.name}  "
            f"字幕 {len(p.cues)} · 区域 {len(p.regions)} · "
            f"无解 {len(eff['unsolvedCueIds'])} · 修订 {p.rev_counter}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    p = _store().get(args.project)
    eff = effective_layout(p)
    print(f"工程 {p.id} · {p.name}")
    print(f"视频 {p.width}x{p.height} @ {p.fps:g}fps · {p.duration:.2f}s")
    print(f"设置 字号 {p.settings.font_size:g}px · 行数 {p.settings.max_lines} · "
          f"安全边距 {p.settings.safe_margin:g}px")
    print()
    for cue in sorted(p.cues, key=lambda c: (c.start, c.id)):
        entry = eff["perCue"].get(cue.id) or {}
        status = "无解"
        if entry.get("stale"):
            status = "标注冲突(已采用位置失效)"
        elif entry.get("partial"):
            status = "部分遮挡"
        elif entry.get("solved"):
            status = "已解"
        print(f"- {cue.start:6.2f}–{cue.end:5.2f}s  {status:12s}  {cue.text}")
        geo = entry.get("geometry")
        if geo:
            print(f"    ink {_fmt_rect(geo['ink'])} · {geo['fontPx']:.0f}px · "
                  f"行数 {len(geo['lines'])} · 模式 {geo['mode']}")
        segs = entry.get("segments") or []
        if segs:
            for seg in segs:
                mark = "渲染" if seg.get("solved") else "不渲染"
                print(f"    段 {seg['start']:.2f}–{seg['end']:.2f}s {mark}")
    if eff["problems"]:
        print()
        print("问题：")
        for prob in eff["problems"]:
            print(f"- {prob['cueId']}: {'；'.join(prob['problems'])}")
    print()
    print(eff["annotationNotice"])
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    store = _store()
    p = store.get(args.project)
    eff = effective_layout(p)
    per_cue = eff["perCue"]
    if not any(v.get("solved") for v in per_cue.values()):
        print("没有可导出的已解出字幕。", file=sys.stderr)
        return 2
    os.makedirs(EXPORTS, exist_ok=True)
    out = args.output or os.path.join(
        EXPORTS, f"{p.id}-{time.strftime('%Y%m%d-%H%M%S')}.mp4"
    )
    ref = os.path.join(os.path.dirname(os.path.abspath(out)), f".ref-{p.id}.mp4")
    job = ff.ExportJob()
    res = ff.export_with_subtitles(p, per_cue, out, job=job, reference_path=ref)
    print(f"导出：{res.output}")
    print(f"ASS：{res.ass_path}")
    if res.skipped_cues:
        print(f"未渲染字幕（无解）：{', '.join(res.skipped_cues)}")
    print(f"渲染字幕段：{len(res.drawn_segments)}")
    if args.no_verify:
        return 0
    print("核验导出像素…")
    verification = ff.verify_export(p, per_cue, p.video_path, res.output, ref)
    print(json.dumps(verification, ensure_ascii=False, indent=2))
    return 0 if verification.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="signsub",
        description="手语字幕布局服务（辅助命令行；图形入口见前端应用）",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出工程").set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="打印某工程的实际布局结果")
    show.add_argument("project")
    show.set_defaults(func=cmd_show)

    exp = sub.add_parser("export", help="导出并逐帧核验字幕视频")
    exp.add_argument("project")
    exp.add_argument("-o", "--output")
    exp.add_argument("--no-verify", action="store_true", help="跳过逐帧核验")
    exp.set_defaults(func=cmd_export)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except KeyError:
        print(f"工程 {getattr(args, 'project', '')} 不存在", file=sys.stderr)
        return 2
    except ff.FFmpegError as exc:
        print(f"FFmpeg 错误：{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
