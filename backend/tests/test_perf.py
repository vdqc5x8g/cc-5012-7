"""避让性能场景测试：快速手势 + 窄安全区负载下的避让查询优化。

断言原则（对应任务要求）：
- 优化前后输出逐字段一致；除遮挡比例与布局耗时外的行为差异即退化，应撤回优化。
- 检查帧数不减少：分段连续覆盖整条字幕，区间数等于断点推导值。
- 遮挡比例与布局耗时必须来自真实测量，不得以默认值遮错。
- 速度比较只在避让查询优化完整执行后进行（先参照、后优化、再比较）。
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from signsub.perf import (  # noqa: E402
    SCENARIO_NAME,
    coverage_gaps,
    expected_intervals,
    fast_gesture_project,
    run_benchmark,
)


class AvoidancePerfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project = fast_gesture_project(root=ROOT)
        cls.report = run_benchmark(cls.project)

    def test_scenario_is_actually_loaded(self):
        """场景必须真的产生高负载：快速手势、窄安全区、短暂全遮挡。"""
        p = self.project
        self.assertEqual(p.settings.safe_margin, 88.0, "窄安全区设置未生效")
        gesture = [r for r in p.regions if r.kind == "sign"]
        self.assertGreaterEqual(len(gesture), 70, "快速手势区域数量不足")
        spans = [r.end - r.start for r in gesture]
        self.assertTrue(all(abs(s - 0.15) < 1e-9 for s in spans), "手势跳变周期不是 0.15s")
        wash = next(r for r in p.regions if r.id == "wash")
        self.assertAlmostEqual(wash.end - wash.start, 0.24, places=6)
        # 断点密度：长字幕都被切出多个时间区间；pc9 本身只有 0.2s。
        expected = expected_intervals(p)
        for cid, v in expected.items():
            if cid == "pc9":
                self.assertGreaterEqual(v, 2, expected)
            else:
                self.assertGreater(v, 5, expected)

    def test_optimized_output_identical_to_reference(self):
        """避让查询优化不得改变任何布局结果（唯一允许变化的是耗时指标）。"""
        self.assertEqual(self.report["outputDiffs"], [])
        self.assertTrue(self.report["identicalOutput"])

    def test_intervals_not_reduced_and_brief_occlusion_exposed(self):
        """检查帧数不减少：分段连续覆盖；0.24s 全遮挡必须显式成段。"""
        self.assertEqual(self.report["coverageProblemsRef"], [])
        self.assertEqual(self.report["coverageProblemsOpt"], [])
        self.assertTrue(self.report["intervalsNotReduced"])
        # pc3 覆盖 3.20–4.70s，包含 3.60–3.84s 的全画面短暂遮挡：
        # 两条路径都必须把遮挡时段的每一刻标为未解（短暂动作不被隐藏）。
        from signsub.layout import evaluate_timeline, evaluate_timeline_reference

        for label, fn in (("ref", evaluate_timeline_reference), ("opt", evaluate_timeline)):
            res = fn(self.project)["pc3"]
            self.assertTrue(res.partial, f"{label}: pc3 应为部分遮挡")
            for t in (3.62, 3.70, 3.80):
                seg = next(s for s in res.segments if s.start <= t < s.end)
                self.assertFalse(seg.solved, f"{label}: pc3 在 {t}s 应未解（全遮挡）")
            solved_spans = [(s.start, s.end) for s in res.segments if s.solved]
            for a, b in solved_spans:
                self.assertTrue(b <= 3.60 + 1e-6 or a >= 3.84 - 1e-6,
                                f"{label}: 遮挡时段内出现已解分段 {(a, b)}")
            # 完全落在遮挡内的短字幕 pc9 必须整条无解，不得被跳过。
            res9 = fn(self.project)["pc9"]
            self.assertFalse(res9.solved, f"{label}: pc9 应整条无解")
            self.assertTrue(all(not s.solved for s in res9.segments))

    def test_occupancy_ratio_measured_not_default(self):
        """遮挡比例必须是真实测量值：非默认 0/1（全遮挡 cue 除外）且两路径一致。"""
        occ = self.report["occupancyRatio"]
        self.assertEqual(set(occ), {c.id for c in self.project.cues})
        self.assertTrue(self.report["occupancyRatioIdentical"])
        for cid, v in occ.items():
            self.assertIsInstance(v, float)
            if cid in ("pc3", "pc9"):
                self.assertGreaterEqual(v, 0.99, "全遮挡字幕的占用率应接近 1")
            else:
                self.assertGreater(v, 0.3, f"{cid} 遮挡比例疑似默认值")
                self.assertLess(v, 0.99, f"{cid} 遮挡比例异常")

    def test_layout_time_measured_per_cue(self):
        """布局耗时必须逐字幕真实记录（非 0、非缺失），且优化后总耗时下降。"""
        for side in ("reference", "optimized"):
            per_cue = self.report[side]["cueLayoutSeconds"]
            self.assertEqual(set(per_cue), {c.id for c in self.project.cues})
            for cid, v in per_cue.items():
                self.assertGreater(v, 0.0, f"{side}.{cid} 布局耗时为默认值")
            self.assertGreater(self.report[side]["totalLayoutSeconds"], 0.0)
            self.assertGreater(self.report[side]["wallSeconds"], 0.0)
        # 避让查询优化完整执行后的速度比较。
        self.assertLess(
            self.report["optimized"]["totalLayoutSeconds"],
            self.report["reference"]["totalLayoutSeconds"],
            "优化后总布局耗时未下降",
        )
        self.assertGreater(self.report["speedup"], 1.0)

    def test_cost_breakdown_shows_reuse_and_less_wasted_search(self):
        """成本分解：优化路径复用几何（命中>0）且冲突检查工作量下降。"""
        ref, opt = self.report["reference"], self.report["optimized"]
        for stage in ("regionQuery", "positions", "conflicts", "wrap", "measure"):
            self.assertGreater(ref[stage]["calls"], 0, f"参照 {stage} 无调用，疑似遮错")
            self.assertGreater(opt[stage]["hits"], 0, f"优化 {stage} 无缓存命中")
        # 参照路径不得有任何缓存命中（它是未优化的运行参照）。
        for stage in ("regionQuery", "positions", "conflicts", "wrap", "measure"):
            self.assertEqual(ref[stage]["hits"], 0, f"参照 {stage} 不应有缓存命中")
        # 重复求候选位置的成本被复用消除：生成的网格点数显著下降。
        self.assertLess(opt["positions"]["units"], ref["positions"]["units"] // 3)
        # 无价值搜索被限制：实际执行的框×区域冲突对数显著下降。
        self.assertLess(opt["conflicts"]["units"], ref["conflicts"]["units"] // 2)
        self.assertGreater(ref["scoreCalls"], 0)
        self.assertEqual(ref["scoreCalls"], opt["scoreCalls"], "评分次数变化说明行为变了")

    def test_reference_and_optimized_see_same_occupancy(self):
        """两条路径的遮挡比例逐字幕一致（优化不得改变遮挡判定）。"""
        self.assertEqual(
            self.report["reference"]["occupancyRatio"],
            self.report["optimized"]["occupancyRatio"],
        )


if __name__ == "__main__":
    unittest.main()
