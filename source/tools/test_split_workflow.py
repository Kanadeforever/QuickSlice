"""断点选择和目标目录规则的无界面回归测试。

执行本文件不需要 Qt；验证用户看到的候选断点不会预先改变线段，
交点吸附满足屏幕容差规则，确认后才可独立编辑分断出的线段。
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from core import SliceModel, export_subfolder, export_png_tiles
from PIL import Image


class SplitWorkflowTests(unittest.TestCase):
    def test_intersection_is_preferred_within_screen_tolerance(self):
        """靠近十字交叉点时，候选断点应自动采用精确相交的整数坐标。"""
        model = SliceModel()
        model.reset(300, 200)
        horizontal = model.add("h", 80)
        model.add("v", 125)
        model.add("v", 225, 20, 170)
        self.assertEqual(model.split_intersections(horizontal.identifier), (125, 225))
        self.assertEqual(model.split_candidate(horizontal.identifier, 121, 7), (125, True))
        self.assertEqual(model.split_candidate(horizontal.identifier, 222, 7), (225, True))
        self.assertEqual(model.split_candidate(horizontal.identifier, 172, 7), (172, False))

    def test_intersection_must_really_touch_segment(self):
        """看起来在同一坐标方向但实际未相交的短线不应该诱发吸附。"""
        model = SliceModel()
        model.reset(200, 100)
        target = model.add("h", 40, 10, 100)
        model.add("v", 60, 70, 100)
        model.add("v", 120, 0, 100)
        model.add("v", 75, 40, 100)
        self.assertEqual(model.split_intersections(target.identifier), (75,))
        self.assertEqual(model.split_candidate(target.identifier, 76, 10), (75, True))

    def test_marker_choosing_does_not_modify_tiles(self):
        """断点放置属于非破坏性预览；真正执行分断后也不凭空制造透明缝。"""
        model = SliceModel()
        model.reset(80, 60)
        guide = model.add("v", 30)
        model.add("h", 25)
        old = model.snapshot()
        parts = tuple(t.bounds for t in model.tiles())
        point, snapped = model.split_candidate(guide.identifier, 23, 4)
        self.assertTrue(snapped)
        self.assertEqual(point, 25)
        self.assertEqual(old, model.snapshot())
        other = model.split(guide.identifier, point)
        self.assertEqual(parts, tuple(t.bounds for t in model.tiles()))
        self.assertEqual(other.start, 25)
        model.remove(other.identifier)
        self.assertEqual(len(model.tiles()), 3)

    def test_candidate_is_always_internal(self):
        """鼠标即使放在外沿或线端，也只允许选中内部合法的分断坐标。"""
        model = SliceModel()
        model.reset(80, 60)
        g = model.add("h", 20, 10, 13)
        self.assertEqual(model.split_candidate(g.identifier, -100), (11, False))
        self.assertEqual(model.split_candidate(g.identifier, 100), (12, False))
        model.add("v", 17)
        self.assertEqual(model.split_intersections(g.identifier), ())

    def test_output_subfolder_is_created_by_export(self):
        """只选父目录，实际 PNG 自动放到源图同名子目录，而不是父目录根部。"""
        model = SliceModel()
        model.reset(4, 3)
        model.add("v", 2)
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "演示图片.png"
            Image.new("RGBA", (4, 3), (11, 22, 33, 44)).save(source)
            parent = Path(root) / "目标目录"
            subdir = export_subfolder(str(parent), str(source))
            self.assertEqual(subdir, parent / "演示图片")
            self.assertFalse(subdir.exists())
            outputs = export_png_tiles(str(source), str(subdir), model.tiles())
            self.assertEqual(len(outputs), 2)
            self.assertTrue(all(Path(filename).parent == subdir for filename in outputs))
            self.assertEqual(list(parent.glob("*.png")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
