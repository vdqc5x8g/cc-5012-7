#!/usr/bin/env python3
"""避让查询优化基准：快速手势 + 窄安全区场景的成本分解与速度对比。

用法：
    python3 scripts/perf_avoidance.py [--out data/perf/avoidance-report.json]

流程（与 docs/PERF.md 一致）：
1. 构建避让性能场景（快速手势 0.15s 跳变、窄安全区、0.24s 短暂全遮挡）。
2. 先跑运行参照（未优化路径），记录成本分解、遮挡比例、布局耗时。
3. 完整执行避让查询优化后的路径，同样记录。
4. 比较：输出必须逐字段一致、检查区间不减少、遮挡比例一致，然后才比较速度。
报告写入 --out（默认 data/perf/avoidance-report.json）作为执行证据。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from signsub.perf import run_benchmark  # noqa: E402


def _fmt_stage(name: str, ref: dict, opt: dict) -> str:
    return (
        f"  {name:<12} 参照 {ref['calls']:>7} 次 / {ref['units']:>9} 单元 / "
        f"{ref['seconds'] * 1000:>8.1f} ms   优化 {opt['calls']:>7} 次 "
        f"(命中 {opt['hits']:>7}) / {opt['units']:>9} 单元 / "
        f"{opt['seconds'] * 1000:>8.1f} ms"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="避让查询优化基准（快速手势+窄安全区）")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "perf", "avoidance-report.json"))
    args = ap.parse_args()

    report = run_benchmark()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    ref, opt = report["reference"], report["optimized"]
    print(f"场景 {report['scenario']}: 字幕 {report['project']['cues']} 条 · "
          f"区域 {report['project']['regions']} 个 · "
          f"安全边距 {report['project']['safeMargin']}px · 网格 {report['project']['gridStep']}px")
    print("成本分解（参照 = 未优化运行参照；优化 = 避让查询优化后）：")
    for stage, label in (("regionQuery", "人物区域查询"), ("positions", "候选位置网格"),
                         ("conflicts", "冲突检查"), ("wrap", "文本换行"),
                         ("measure", "墨迹度量"), ("occupancy", "占用率")):
        print(_fmt_stage(label, ref[stage], opt[stage]))
    print(f"布局耗时：参照 {ref['totalLayoutSeconds'] * 1000:.1f} ms → "
          f"优化 {opt['totalLayoutSeconds'] * 1000:.1f} ms "
          f"（整线 {ref['wallSeconds'] * 1000:.1f} → {opt['wallSeconds'] * 1000:.1f} ms，"
          f"加速 {report['speedup']}x）")
    print(f"遮挡比例（逐字幕，两路径一致={report['occupancyRatioIdentical']}）：")
    for cid, v in report["occupancyRatio"].items():
        print(f"  {cid}: {v:.1%} · 布局耗时 参照 {ref['cueLayoutSeconds'][cid] * 1000:.1f} ms → "
              f"优化 {opt['cueLayoutSeconds'][cid] * 1000:.1f} ms · "
              f"检查区间 {report['intervalsCheckedOpt'][cid]}")
    print(f"输出一致: {report['identicalOutput']} · 检查区间不减少: {report['intervalsNotReduced']}")
    print(f"报告已写入 {args.out}")

    ok = (report["identicalOutput"] and report["intervalsNotReduced"]
          and report["occupancyRatioIdentical"] and report["speedup"] > 1.0)
    if not ok:
        print("基准未通过：存在输出差异/检查区间减少/遮挡比例不一致/无加速", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
