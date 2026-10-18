"""自由正交线段、T 字分区和非矩形透明导出的回归验证。

本测试不需要 Qt；通过逐像素验证确保任何线段组合都不会造成重叠、漏切、
拉伸、重采样或意外损坏原有透明通道。用户图片不会被测试程序读取或修改。
"""

from __future__ import annotations

from pathlib import Path
import random
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from core import SliceModel, export_png_tiles


class SegmentTests(unittest.TestCase):
    def assert_partition(self, model: SliceModel) -> None:
        """检查所有区域对整张图片恰好覆盖一次，即使区域不规则。"""
        marks = [[0] * model.width for _ in range(model.height)]
        for tile in model.tiles():
            for left, top, right, bottom in (tile.parts or (tile.bounds,)):
                for y in range(top, bottom):
                    for x in range(left, right):
                        marks[y][x] += 1
        self.assertTrue(all(pixel == 1 for row in marks for pixel in row))

    def test_t_junction_three_rectangles(self):
        """一条通高纵线加一条仅到纵线的横线，应产生三个而非四个区域。"""
        m = SliceModel()
        m.reset(20, 10)
        m.add("v", 12)
        m.add("h", 4, 0, 12)
        self.assertEqual({t.bounds for t in m.tiles()}, {
            (0, 0, 12, 4), (12, 0, 20, 10), (0, 4, 12, 10)})
        self.assertTrue(all(t.is_rectangle for t in m.tiles()))
        self.assert_partition(m)

    def test_split_and_remove_half_line(self):
        """分断本身不制造缝隙，删除其中一半后才改变真实区域。"""
        m = SliceModel()
        m.reset(20, 10)
        m.add("v", 12)
        line = m.add("h", 4)
        self.assertEqual(len(m.tiles()), 4)
        before = m.snapshot()
        second = m.split(line.identifier, 12)
        self.assertEqual(len(m.tiles()), 4)
        self.assertEqual((line.start, line.end), (0, 12))
        self.assertEqual((second.start, second.end), (12, 20))
        m.remove(second.identifier)
        self.assertEqual(len(m.tiles()), 3)
        self.assert_partition(m)
        m.restore(before)
        self.assertEqual(len(m.tiles()), 4)
        self.assertEqual(m.snapshot(), before)

    def test_l_shaped_region_transparency_and_rgba_equality(self):
        """非矩形区域导出以外接框保存，其余区域透明，内部 RGBA 逐像素一致。"""
        m = SliceModel()
        m.reset(20, 14)
        m.add("v", 6, 0, 4)
        m.add("h", 4, 0, 6)
        tiles = m.tiles()
        self.assertEqual(len(tiles), 2)
        self.assertEqual(sum(bool(tile.parts) for tile in tiles), 1)
        self.assert_partition(m)
        with tempfile.TemporaryDirectory() as tmp:
            src = Image.new("RGBA", (20, 14))
            src.putdata([((x * 13) % 256, (y * 17) % 256, (x*y) % 256,
                          (x*29 + y*11) % 256) for y in range(14) for x in range(20)])
            original = Path(tmp) / "中文源图.png"
            src.save(original)
            files = export_png_tiles(str(original), str(Path(tmp) / "输出"), tiles)
            self.assertEqual(len(files), 2)
            visited = [[0] * 20 for _ in range(14)]
            for tile, file in zip(tiles, files):
                with Image.open(file) as image:
                    self.assertEqual(image.size, (tile.bounds[2]-tile.bounds[0],
                                                  tile.bounds[3]-tile.bounds[1]))
                    for yy in range(image.height):
                        for xx in range(image.width):
                            x = tile.bounds[0]+xx
                            y = tile.bounds[1]+yy
                            inside = any(a <= x < c and b <= y < d
                                         for a, b, c, d in (tile.parts or (tile.bounds,)))
                            pixel = image.getpixel((xx, yy))
                            if inside:
                                self.assertEqual(pixel, src.getpixel((x, y)))
                                visited[y][x] += 1
                            else:
                                self.assertEqual(pixel[3], 0)
            self.assertTrue(all(v == 1 for row in visited for v in row))

    def test_dangling_line_does_not_split_image(self):
        """没有碰到外框或其他切割线的悬空线段，不应凭空产生两个区域。"""
        m = SliceModel()
        m.reset(100, 100)
        m.add("h", 50, 10, 60)
        m.add("v", 25, 25, 75)
        self.assertEqual(len(m.tiles()), 1)
        self.assertTrue(m.tiles()[0].is_rectangle)

    def test_resize_overlap_and_coordinate_validation(self):
        """同轴重叠线不得创建，起止坐标不能颠倒；端点允许连接。"""
        m = SliceModel()
        m.reset(90, 60)
        first = m.add("h", 20, 0, 50)
        other = m.add("h", 20, 50, 90)
        with self.assertRaises(ValueError):
            m.add("h", 20, 30, 70)
        self.assertFalse(m.resize(first.identifier, "end", 60))
        self.assertTrue(m.resize(first.identifier, "end", 40))
        self.assertTrue(m.resize(other.identifier, "start", 40))
        self.assertFalse(m.resize(other.identifier, "start", 90))
        self.assert_partition(m)

    def test_random_intersections_are_complete(self):
        """使用确定种子的随机线段寻找漏格或重复划分。"""
        rand = random.Random(126)
        for _ in range(45):
            m = SliceModel()
            m.reset(30, 25)
            for _ in range(30):
                axis = rand.choice(("h", "v"))
                position = rand.randrange(1, m.limit(axis))
                start = rand.randrange(m.span_limit(axis))
                end = rand.randrange(start+1, m.span_limit(axis)+1)
                try:
                    m.add(axis, position, start, end)
                except ValueError:
                    pass
            self.assert_partition(m)

    def test_large_collinear_split_and_cache(self):
        """大量位于同一坐标的分段不应使网格拓扑随帧数重复计算。"""
        m = SliceModel()
        m.reset(1000, 1000)
        for i in range(100):
            m.add("h", 550, i*10, (i+1)*10)
        self.assertEqual(len(m.tiles()), 2)
        self.assertIs(m.tiles(), m.tiles())
        self.assert_partition(m)


if __name__ == "__main__":
    unittest.main(verbosity=2)
