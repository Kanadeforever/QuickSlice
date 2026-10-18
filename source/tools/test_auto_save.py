"""自动保存、队列顺序以及 Windows 文件名回退的无界面测试。

本测试不依赖 PySide6。所有路径预先计算，避免导出时才因为过长报错，
也验证界面覆盖检测与实际写盘调用相同的路径规划函数。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PIL import Image
from core import (CropTile, SliceModel, export_png_tiles, export_subfolder,
                  next_queue_path, source_folder_suffix, tile_output_path,
                  _windows_name_units, MAX_SAFE_EXPORT_PATH)


class AutoSaveTests(unittest.TestCase):
    def test_next_in_queue(self):
        """从当前张到下一张，单图和末尾都不循环回第一张。"""
        paths = ["第一张", "第二张", "第三张"]
        self.assertEqual(next_queue_path(paths, "第一张"), "第二张")
        self.assertEqual(next_queue_path(paths, "第二张"), "第三张")
        self.assertIsNone(next_queue_path(paths, "第三张"))
        self.assertIsNone(next_queue_path(["第一张"], "第一张"))
        self.assertIsNone(next_queue_path(paths, "未导入"))

    def test_normal_longer_than_ten_is_preserved(self):
        """十字符不是固定上限，未触及 Windows 限额的文件名完整保留。"""
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "这是十二个字的文件名称abcdef.png"
            output = export_subfolder(str(Path(temp)), str(source))
            self.assertEqual(output.name, source.stem)
            tile = CropTile(1, 2, (0, 0, 1, 1))
            self.assertEqual(tile_output_path(str(source), output, tile).name,
                             f"{source.stem}_R01_C02.png")

    def test_long_name_fallback_is_deterministic_and_distinct(self):
        """超限才裁取原名前十字符，并用摘要区分相同前缀的不同图片。"""
        with tempfile.TemporaryDirectory() as temp:
            stem = "很长的名称" * 35
            path_a = str(Path(temp) / (stem + "甲.png"))
            path_b = str(Path(temp) / (stem + "乙.png"))
            folder_a = export_subfolder(temp, path_a)
            folder_b = export_subfolder(temp, path_b)
            self.assertTrue(folder_a.name.startswith(stem[:10] + "_"))
            self.assertNotEqual(folder_a, folder_b)
            self.assertEqual(folder_a, export_subfolder(temp, path_a))
            tile = CropTile(1, 1, (0, 0, 3, 4))
            result_a = tile_output_path(path_a, folder_a, tile)
            self.assertTrue(result_a.name.startswith(stem[:10] + "_"))
            self.assertLessEqual(_windows_name_units(str(result_a.absolute())), MAX_SAFE_EXPORT_PATH)

    def test_deep_parent_reports_useful_failure(self):
        """父路径本身过深时不静默裁剪到空名，而是明确要求换一个短路径。"""
        with tempfile.TemporaryDirectory() as temp:
            long_parent = Path(temp) / ("p" * 150) / ("q" * 150)
            with self.assertRaisesRegex(ValueError, "路径过深"):
                export_subfolder(str(long_parent), "image.png")

    def test_shared_name_for_check_and_real_output(self):
        """实际无损导出文件名必须与提前规划和覆盖检查使用的名称完全一致。"""
        with tempfile.TemporaryDirectory() as temp:
            stem = "a" * 195
            source = Path(temp) / f"{stem}.png"
            Image.new("RGBA", (5, 4), (17, 42, 73, 191)).save(source)
            model = SliceModel()
            model.reset(5, 4)
            model.add("v", 2)
            folder = export_subfolder(temp, str(source))
            tiles = model.tiles()
            planned = [tile_output_path(str(source), folder, tile) for tile in tiles]
            result = export_png_tiles(str(source), str(folder), tiles)
            self.assertEqual(result, [str(x) for x in planned])
            for filename in result:
                with Image.open(filename) as output:
                    self.assertEqual(output.getpixel((0, 0)), (17, 42, 73, 191))

    def test_same_basename_with_different_formats(self):
        """同一源目录的同名 PNG/JPEG 需要以扩展名区分保存目录。"""
        with tempfile.TemporaryDirectory() as temp:
            png = Path(temp) / "图片.png"
            jpg = Path(temp) / "图片.jpg"
            Image.new("RGB", (3, 3), "red").save(png)
            Image.new("RGB", (3, 3), "blue").save(jpg)
            self.assertEqual(source_folder_suffix(str(png)), "_png")
            self.assertEqual(source_folder_suffix(str(jpg)), "_jpg")
            self.assertNotEqual(export_subfolder(temp, str(png), source_folder_suffix(str(png))),
                                export_subfolder(temp, str(jpg), source_folder_suffix(str(jpg))))

    def test_safe_name_counts_utf16(self):
        """Windows 对辅助平面字符使用代理项，因此需按 UTF-16 单元检查。"""
        self.assertEqual(_windows_name_units("abc"), 3)
        self.assertEqual(_windows_name_units("照片😀"), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
