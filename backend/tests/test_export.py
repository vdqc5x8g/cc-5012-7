"""End-to-end export test: real FFmpeg output, real pixel verification.

Builds a small synthetic video, lays out captions with the layout engine,
exports with burned subtitles, then measures the encoded pixels against a
subtitle-free reference encode to prove captions never leave the safe area or
land inside protected regions, and that a fully occluded stretch contains no
caption pixels.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from signsub import ffmpeg as ff  # noqa: E402
from signsub.layout import effective_layout  # noqa: E402
from signsub.model import AvoidRegion, CaptionCue, LayoutSettings, Project  # noqa: E402

FONT = os.path.join(ROOT, "assets", "fonts", "SignCaptionSans-Regular.ttf")
FONT_BOLD = os.path.join(ROOT, "assets", "fonts", "SignCaptionSans-Bold.ttf")


@unittest.skipUnless(os.path.exists(FONT), "需要内置字体")
class ExportVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="signsub-test-")
        cls.video = os.path.join(cls.tmp, "source.mp4")
        ffmpeg = ff.find_ffmpeg()
        vf = (
            "drawbox=x='180+120*sin(t*0.55)':y='200+40*sin(t*0.9)':w=340:h=380:"
            "color=#d9a06a@1:t=fill,"
            "drawbox=x=0:y=0:w=1280:h=720:color=#222222@0.92:t=fill:"
            "enable='between(t,3.0,4.0)'"
        )
        args = [
            ffmpeg, "-hide_banner", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=#1b1e28:s=1280x720:d=5:r=25",
            "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", cls.video,
        ]
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode != 0:
            raise unittest.SkipTest(f"无法生成测试视频: {proc.stderr[-400:]}")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _project(self):
        cues = [
            CaptionCue("c1", 0.4, 2.6, "欢迎收看本期手语讲解，先看左侧人物区域。"),
            CaptionCue("c2", 2.8, 4.6, "画面被完全占用时应当报告无解，而不是缩小字号。"),
            CaptionCue("c3", 4.6, 4.9, "恢复后稳定放置。"),
        ]
        regions = [
            # Moving signer region stays clear of the caption band.
            AvoidRegion("r1", "人物保护区（移动窗口）", "signer", 0.0, 5.0, [200, 160, 380, 360]),
            # Full-frame wash for one second: the c2 interval must stay blank.
            AvoidRegion("r2", "全画面占用", "custom", 3.0, 4.0, [0, 0, 1280, 720]),
            # Important graphic on the right, expires after 2.0s.
            AvoidRegion("r3", "过期的重要图文", "important", 0.0, 2.0, [900, 80, 300, 280]),
        ]
        return Project(
            id="t", name="t", video_path=self.video,
            width=1280, height=720, fps=25.0, duration=5.0,
            font_path=FONT, font_bold_path=FONT_BOLD,
            cues=cues, regions=regions, settings=LayoutSettings(),
        )

    def test_exported_pixels_respect_safe_area_and_regions(self):
        proj = self._project()
        eff = effective_layout(proj)
        per_cue = eff["perCue"]
        self.assertTrue(per_cue["c1"]["solved"])
        self.assertTrue(per_cue["c2"]["partial"], "全画面占用时段应报告为部分遮挡")
        self.assertTrue(any(not s["solved"] for s in per_cue["c2"]["segments"]))

        out = os.path.join(self.tmp, "export.mp4")
        ref = os.path.join(self.tmp, "reference.mp4")
        res = ff.export_with_subtitles(proj, per_cue, out, reference_path=ref)
        self.assertTrue(os.path.exists(out))
        self.assertTrue(os.path.exists(res.ass_path))
        verification = ff.verify_export(proj, per_cue, self.video, out, ref)

        self.assertTrue(verification["ok"], verification)
        self.assertEqual(verification["summary"]["failed"], [])
        self.assertEqual(verification["summary"]["outsideSafe"], 0)
        self.assertEqual(verification["summary"]["regionHits"], 0)
        self.assertGreaterEqual(verification["summary"]["withPixels"], 2)

        checks = verification["cues"]
        # c1 fully drawn and inside bounds.
        self.assertTrue(checks["c1"]["ok"])
        self.assertTrue(checks["c1"]["renderedPixels"])
        self.assertFalse(checks["c1"]["outsideSafe"])
        self.assertEqual(checks["c1"]["regionHits"], [])
        # c2: pixels exist in the solvable part, none during 3.0-4.0s.
        self.assertTrue(checks["c2"]["ok"])
        self.assertTrue(checks["c2"]["renderedPixels"])
        self.assertIn("部分时段被完全遮挡", checks["c2"]["note"])
        occ_frames, _ = ff._run_diff_bbox(
            self.video, out, 3.0, 4.0, None, proj.settings.verify_threshold
        )
        self.assertEqual([f for f in occ_frames if "x1" in f], [],
                         "完全遮挡时段不应出现字幕像素")
        # c3 drawn.
        self.assertTrue(checks["c3"]["ok"])
        self.assertTrue(checks["c3"]["renderedPixels"])


if __name__ == "__main__":
    unittest.main()
