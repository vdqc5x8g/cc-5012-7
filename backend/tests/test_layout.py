"""Layout engine acceptance tests.

Fixtures use the meanings required by the product:
- moving signer window
- two mutually exclusive avoidance regions
- a long caption
- a full-frame occupied stretch (must report UNSOLVED, never shrink)
- time-ranged regions that expire
- stability under slight region changes
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from signsub.ass import build_ass  # noqa: E402
from signsub.layout import (  # noqa: E402
    conflicts_for,
    effective_layout,
    evaluate_cue,
    validate_placement,
)
from signsub.model import (  # noqa: E402
    AvoidRegion,
    CaptionCue,
    LayoutSettings,
    Project,
)

FONT = os.path.join(ROOT, "assets", "fonts", "SignCaptionSans-Regular.ttf")
FONT_BOLD = os.path.join(ROOT, "assets", "fonts", "SignCaptionSans-Bold.ttf")
W, H, FPS, DUR = 1280, 720, 25.0, 12.5


def make_project(cues, regions, settings=None) -> Project:
    return Project(
        id="test", name="test", video_path="/nonexistent.mp4",
        width=W, height=H, fps=FPS, duration=DUR,
        font_path=FONT, font_bold_path=FONT_BOLD,
        cues=list(cues), regions=list(regions),
        settings=settings or LayoutSettings(),
    )


def cue(cid, start, end, text):
    return CaptionCue(cid, start, end, text)


class LayoutTests(unittest.TestCase):
    def test_solve_avoids_signer_and_is_not_just_bottom(self):
        # Signer occupies the bottom-centre; captions must move elsewhere, and the
        # result must be a real glyph-metric box inside the safe area.
        regions = [
            AvoidRegion("r1", "人物保护区", "signer", 0.0, 12.5, [420, 430, 440, 260]),
        ]
        proj = make_project([cue("c1", 0.5, 3.5, "这是一句用于验证避障的手语字幕。")], regions)
        res = evaluate_cue(proj, proj.cues[0], _font())
        self.assertTrue(res.solved)
        geo = res.geometry
        self.assertIsNotNone(geo)
        padded = geo.padded
        s = proj.settings
        self.assertGreaterEqual(padded.x, s.safe_margin - 0.01)
        self.assertGreaterEqual(padded.y, s.safe_margin - 0.01)
        self.assertLessEqual(padded.right(), W - s.safe_margin + 0.01)
        self.assertLessEqual(padded.bottom(), H - s.safe_margin + 0.01)
        self.assertEqual(conflicts_for(padded, regions), [])
        # Glyph metrics are real: the ink box is derived from outlines, not an
        # estimated character count.
        self.assertGreater(geo.ink.w, 0)
        self.assertGreater(geo.ink.h, 0)
        self.assertEqual(len(geo.lines), len(res.chosen.lines))

    def test_two_mutually_exclusive_regions_switch_over_time(self):
        regions = [
            AvoidRegion("left", "左侧互斥区域", "sign", 0.0, 2.0, [40, 60, 400, 300]),
            AvoidRegion("right", "右侧互斥区域", "sign", 2.0, 4.0, [840, 60, 400, 300]),
        ]
        proj = make_project(
            [cue("c1", 0.3, 1.8, "第一句避开左侧。"), cue("c2", 2.1, 3.8, "第二句避开右侧。")],
            regions,
        )
        results = effective_layout(proj)["perCue"]
        self.assertTrue(results["c1"]["solved"])
        self.assertTrue(results["c2"]["solved"])
        # Both cues are only required to avoid the regions active in their span.
        self.assertEqual([r["regionId"] for r in results["c1"]["geometry"]["conflicts"]], [])
        self.assertEqual([r["regionId"] for r in results["c2"]["geometry"]["conflicts"]], [])

    def test_long_caption_wraps_within_allowed_lines(self):
        text = "这一句字幕比较长，需要验证在移动的人物保护区之外能否稳定放置。"
        proj = make_project(
            [cue("c1", 0.5, 3.5, text)],
            [AvoidRegion("r1", "人物保护区", "signer", 0.0, 12.5, [220, 150, 380, 360])],
        )
        res = evaluate_cue(proj, proj.cues[0], _font())
        self.assertTrue(res.solved)
        self.assertLessEqual(len(res.chosen.lines), proj.settings.max_lines)
        self.assertEqual("".join(res.chosen.lines), text)
        self.assertLessEqual(len(res.chosen.lines[0]), proj.settings.max_chars_per_line + 1)

    def test_text_too_long_is_unsolved_not_shrunk(self):
        text = "超长字幕" * 20
        proj = make_project([cue("c1", 0.5, 3.5, text)], [])
        res = evaluate_cue(proj, proj.cues[0], _font())
        self.assertFalse(res.solved)
        self.assertIsNotNone(res.unsolved)
        self.assertNotIn("完全占据", res.unsolved.reason)

    def test_full_frame_occupation_reports_unsolved(self):
        regions = [
            AvoidRegion("full", "全画面占用", "custom", 1.0, 2.0, [0, 0, W, H]),
        ]
        proj = make_project([cue("c1", 1.2, 1.8, "遮挡期间无解。")], regions)
        res = evaluate_cue(proj, proj.cues[0], _font())
        self.assertFalse(res.solved)
        self.assertIsNotNone(res.unsolved)
        self.assertEqual(res.unsolved.reason, "安全区被标注区域完全占据")
        self.assertGreaterEqual(res.unsolved.occupancy, 0.99)
        # Never silently shrink to unreadable text: no chosen geometry at all.
        self.assertIsNone(res.chosen)
        self.assertIsNone(res.geometry)

    def test_partially_occluded_cue_renders_only_solvable_intervals(self):
        regions = [
            AvoidRegion("full", "全画面占用", "custom", 7.0, 8.0, [0, 0, W, H]),
        ]
        proj = make_project([cue("c1", 6.0, 8.0, "前半段可解，后半段被完全遮挡。")], regions)
        eff = effective_layout(proj)
        entry = eff["perCue"]["c1"]
        self.assertTrue(entry["partial"])
        segs = entry["segments"]
        self.assertEqual(len(segs), 2)
        self.assertTrue(segs[0]["solved"])
        self.assertGreaterEqual(segs[0]["start"], 5.99)
        self.assertLessEqual(segs[0]["end"], 7.01)
        self.assertFalse(segs[1]["solved"])
        ass, skipped, drawn = build_ass(proj, eff["perCue"])
        self.assertNotIn("c1", skipped)
        self.assertEqual(len(drawn), 1)
        self.assertGreaterEqual(drawn[0][1], 5.99)
        self.assertLessEqual(drawn[0][2], 7.01)
        # Occluded interval must not emit Dialogue events.
        self.assertEqual(ass.count("Dialogue:"), len(entry["geometry"]["lines"]))

    def test_expired_region_does_not_block_later_cues(self):
        expired = AvoidRegion("old", "过期的重要图文", "important", 2.0, 3.0, [40, 60, 400, 300])
        proj = make_project([cue("c1", 6.0, 8.0, "过期区域不应继续阻挡。")], [expired])
        res = evaluate_cue(proj, proj.cues[0], _font())
        self.assertTrue(res.solved)
        self.assertEqual(res.active_regions, [])
        # A caption placed inside the expired region's rect is valid now.
        geo, conflicts, problems = validate_placement(
            proj, proj.cues[0], 60, 120, proj.settings.font_size, ["过期区域不应继续阻挡。"],
        )
        self.assertEqual(problems, [])
        self.assertEqual(conflicts, [])
        self.assertTrue(geo.solved)

    def test_slight_region_change_keeps_caption_position(self):
        base_region = [220, 150, 380, 360]
        cues = [
            cue("c1", 0.5, 2.5, "第一条字幕确定初始位置。"),
            cue("c2", 2.6, 4.6, "轻微区域变化不应让字幕上下跳动。"),
        ]
        proj_a = make_project(cues, [
            AvoidRegion("r1", "人物保护区", "signer", 0.0, 12.5, base_region),
        ])
        proj_b = make_project(cues, [
            AvoidRegion("r1", "人物保护区", "signer", 0.0, 12.5,
                        [base_region[0] + 8, base_region[1] + 6, base_region[2], base_region[3]]),
        ])
        a = effective_layout(proj_a)["perCue"]
        b = effective_layout(proj_b)["perCue"]
        self.assertTrue(a["c2"]["solved"] and b["c2"]["solved"])
        ga, gb = a["c2"]["geometry"], b["c2"]["geometry"]
        self.assertEqual(round(ga["ink"][1], 1), round(gb["ink"][1], 1),
                         "轻微标注变化导致字幕纵向跳动")
        self.assertFalse(b["c2"]["geometry"]["conflicts"])

    def test_unsolved_never_claims_auto_detection(self):
        proj = make_project([cue("c1", 0.5, 2.0, "未标注时不宣称已自动识别。")], [])
        entry = effective_layout(proj)["perCue"]["c1"]
        self.assertTrue(entry["solved"])
        notice = effective_layout(proj)["annotationNotice"]
        self.assertIn("人工标注", notice)
        self.assertIn("未做自动识别", notice)


def _font():
    from signsub.fontmetrics import load_font
    return load_font(FONT)


if __name__ == "__main__":
    unittest.main()
