"""避让性能场景：快速手势 + 窄安全区的高负载避让查询基准。

场景设计（对应任务要求）：
- 快速手势：手语动作区每 0.15s 跳变一次，覆盖整条时间线 —— 区域断点密集，
  每条字幕被切分成大量时间区间，重复求人物区域与候选位置的成本被放大。
- 窄安全区：safe_margin 加宽、网格步长收紧 —— 候选位置数量与冲突检查对数上升。
- 短暂全遮挡：0.24s 的全画面占用 —— 验证检查帧数不减少时短暂动作仍被暴露。
- 连续字幕：字幕首尾相接覆盖整个时间线（连续帧避让）。

基准流程：先跑运行参照（未优化路径），再完整执行避让查询优化后的路径，
最后比较速度与成本分解。两条路径的输出必须逐字段一致；除遮挡比例与布局
耗时外的任何行为差异都视为优化退化，应撤回相应优化。
"""

from __future__ import annotations

import time
from typing import Any

from .fontmetrics import load_font
from .layout import (
    AvoidanceStats,
    _region_breakpoints,
    evaluate_timeline,
    evaluate_timeline_reference,
)
from .model import AvoidRegion, CaptionCue, LayoutSettings, Project

SCENARIO_NAME = "fast-gesture-narrow-safe"
W, H, FPS, DUR = 1280, 720, 25.0, 12.0

# 与仓库内置字体保持一致（测试/脚本都会解析真实路径）。
FONT_REGULAR = "assets/fonts/SignCaptionSans-Regular.ttf"
FONT_BOLD = "assets/fonts/SignCaptionSans-Bold.ttf"


def fast_gesture_project(root: str = ".") -> Project:
    """Build the high-load avoidance scenario (deterministic, no randomness)."""
    settings = LayoutSettings(
        font_size=38.0,
        max_lines=2,
        max_chars_per_line=15,
        safe_margin=88.0,      # 窄安全区：可用区域被明显压缩
        grid_step=28.0,        # 更密的候选网格 → 更多候选位置
        sticky_tolerance_px=36.0,
        min_font_ratio=0.55,
    )
    cues = [
        CaptionCue("pc1", 0.20, 1.70, "快速手语开始，人物在画面中移动讲解。"),
        CaptionCue("pc2", 1.70, 3.20, "第二句紧贴上一句，连续帧都要重新避让。"),
        CaptionCue("pc3", 3.20, 4.70, "这一句经过短暂全画面遮挡，必须按区间报告。"),
        CaptionCue("pc4", 4.70, 6.20, "遮挡结束后恢复放置，位置应稳定不跳动。"),
        CaptionCue("pc5", 6.20, 7.70, "窄安全区内候选位置密集，避让查询成本最高。"),
        CaptionCue("pc6", 7.70, 9.20, "重要图文出现时字幕需要让出右侧区域。"),
        CaptionCue("pc7", 9.20, 10.70, "手势速度加快，动作区几乎每帧都在跳变。"),
        CaptionCue("pc8", 10.70, 12.00, "最后一句验证过期区域不再阻挡后续字幕。"),
        # 完全落在 0.24s 全遮挡内的短字幕：必须被显式判为无解，不得被跳过。
        CaptionCue("pc9", 3.62, 3.82, "短暂遮挡。"),
    ]
    regions: list[AvoidRegion] = []
    # 人物保护区：移动窗口，用 0.5s 时间片近似连续移动（区域跳变）。
    for i in range(24):
        t0 = i * 0.5
        x = 200 + (i % 6) * 60
        y = 150 + (i % 3) * 40
        regions.append(AvoidRegion(
            f"signer-{i:02d}", "人物保护区（移动窗口）", "signer", t0, t0 + 0.5,
            [float(x), float(y), 360.0, 340.0],
        ))
    # 快速手势：手语动作区每 0.15s 跳变（约 4 帧@25fps），覆盖整条时间线。
    t = 0.0
    gi = 0
    while t < DUR - 1e-6:
        x = 60.0 + (gi * 137) % 880
        y = 96.0 + (gi * 89) % 400
        regions.append(AvoidRegion(
            f"gesture-{gi:03d}", f"快速手势{gi}", "sign", t, t + 0.15,
            [x, y, 240.0, 170.0],
        ))
        t += 0.15
        gi += 1
    # 重要图文：两个时段的关键画面。
    regions.append(AvoidRegion("imp-1", "重要图文（前段）", "important", 3.0, 5.5,
                               [820.0, 90.0, 360.0, 260.0]))
    regions.append(AvoidRegion("imp-2", "重要图文（后段）", "important", 7.5, 9.5,
                               [60.0, 420.0, 380.0, 240.0]))
    # 短暂全画面遮挡：0.24s —— 检查帧数不得减少，短暂动作不能被隐藏。
    regions.append(AvoidRegion("wash", "全画面短暂遮挡", "custom", 3.60, 3.84,
                               [0.0, 0.0, float(W), float(H)]))
    return Project(
        id="perf-avoidance", name="避让性能场景（快速手势+窄安全区）",
        video_path="media/sample-signer.mp4",
        width=W, height=H, fps=FPS, duration=DUR,
        font_path=f"{root}/{FONT_REGULAR}" if root != "." else FONT_REGULAR,
        font_bold_path=f"{root}/{FONT_BOLD}" if root != "." else FONT_BOLD,
        cues=cues, regions=regions, settings=settings,
    )


def expected_intervals(project: Project) -> dict[str, int]:
    """每条字幕按区域断点应检查的时间区间数（检查帧密度的期望值）。

    与 evaluate_cue 的口径一致：断点切分后，相邻且活动区域集完全相同的
    区间合并为一个；整段可解时记 1。
    """
    from .layout import _active_regions_for

    out: dict[str, int] = {}
    for cue in project.cues:
        pts = _region_breakpoints(project, cue)
        merged = 0
        prev_ids: list[str] | None = None
        for a, b in zip(pts, pts[1:]):
            if b - a <= 1e-6:
                continue
            ids = [r.id for r in _active_regions_for(project, a, b)]
            if prev_ids is not None and ids == prev_ids:
                continue
            merged += 1
            prev_ids = ids
        out[cue.id] = max(1, merged)
    return out


def coverage_gaps(project: Project, results: dict) -> list[str]:
    """每条的渲染分段必须连续覆盖整条字幕：任何时刻要么已解、要么显式无解，
拆分区间求解时区间数必须等于断点推导值（检查帧数不得减少）。"""
    problems: list[str] = []
    expected = expected_intervals(project)
    for cue in project.cues:
        res = results.get(cue.id)
        if res is None:
            problems.append(f"{cue.id}: 缺少评估结果")
            continue
        segs = res.segments
        if not segs:
            problems.append(f"{cue.id}: 没有任何分段，时段被跳过")
            continue
        t = cue.start
        for seg in segs:
            if abs(seg.start - t) > 1e-3:
                problems.append(f"{cue.id}: 分段在 {t:.2f}s 处断开，{seg.start:.2f}s 前未检查")
                break
            t = seg.end
        else:
            if abs(t - cue.end) > 1e-3:
                problems.append(f"{cue.id}: 分段只覆盖到 {t:.2f}s，字幕结束于 {cue.end:.2f}s")
        if len(segs) > 1 and len(segs) != expected[cue.id]:
            problems.append(
                f"{cue.id}: 检查了 {len(segs)} 个区间，断点推导应为 {expected[cue.id]} 个"
            )
    return problems


def compare_results(ref: dict, opt: dict) -> list[str]:
    """Deep-compare two timeline evaluations; returns human-readable diffs."""
    diffs: list[str] = []
    if set(ref) != set(opt):
        diffs.append(f"cue 集合不同: {sorted(set(ref) ^ set(opt))}")
        return diffs
    for cid in sorted(ref):
        a, b = ref[cid].to_dict(), opt[cid].to_dict()
        if a == b:
            continue
        for key in a:
            if a[key] != b[key]:
                diffs.append(f"{cid}.{key}: 参照={a[key]!r} 优化={b[key]!r}")
    return diffs


def run_benchmark(project: Project | None = None, repeat: int = 1) -> dict[str, Any]:
    """先跑运行参照，再完整执行避让查询优化，最后比较速度与成本分解。"""
    proj = project or fast_gesture_project()
    load_font(proj.font_path)  # 字体解析不计入布局耗时

    # 1) 运行参照（未优化路径）。
    ref_stats = AvoidanceStats()
    t0 = time.perf_counter()
    ref = evaluate_timeline_reference(proj, stats=ref_stats)
    ref_wall = time.perf_counter() - t0

    # 2) 避让查询优化后的完整执行。
    opt_stats = AvoidanceStats()
    t0 = time.perf_counter()
    opt = evaluate_timeline(proj, stats=opt_stats)
    opt_wall = time.perf_counter() - t0

    # 3) 输出一致性：除遮挡比例/布局耗时外不得有任何行为差异。
    diffs = compare_results(ref, opt)

    expected = expected_intervals(proj)
    gaps_ref = coverage_gaps(proj, ref)
    gaps_opt = coverage_gaps(proj, opt)
    occupancy_same = ref_stats.occupancy_ratio == opt_stats.occupancy_ratio

    return {
        "scenario": SCENARIO_NAME,
        "project": {
            "cues": len(proj.cues),
            "regions": len(proj.regions),
            "safeMargin": proj.settings.safe_margin,
            "gridStep": proj.settings.grid_step,
            "frame": [proj.width, proj.height],
            "duration": proj.duration,
        },
        "reference": {"wallSeconds": round(ref_wall, 6), **ref_stats.to_dict()},
        "optimized": {"wallSeconds": round(opt_wall, 6), **opt_stats.to_dict()},
        "speedup": round(ref_wall / opt_wall, 3) if opt_wall > 0 else None,
        "identicalOutput": not diffs,
        "outputDiffs": diffs,
        "intervalsExpected": expected,
        "intervalsCheckedRef": ref_stats.intervals_checked,
        "intervalsCheckedOpt": opt_stats.intervals_checked,
        "coverageProblemsRef": gaps_ref,
        "coverageProblemsOpt": gaps_opt,
        "intervalsNotReduced": not gaps_ref and not gaps_opt,
        "occupancyRatioIdentical": occupancy_same,
        "occupancyRatio": opt_stats.occupancy_ratio,
    }
