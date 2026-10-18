"""不依赖 GUI 的端到端像素校验。

使用标准库 unittest 自动创建临时样本，覆盖网格完整性、透明通道、
JPEG 解码像素无二次损失、EXIF 方向，以及失败时的文件清理。
测试只使用临时目录，不会修改用户现有图片。
"""

from __future__ import annotations

from pathlib import Path
import struct
import sys
import tempfile
import unittest

from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from core import CropTile, SliceModel, export_png_tiles, read_image_info


class CoreTests(unittest.TestCase):
    def test_full_coverage_and_undo(self):
        """切割产生的每个像素只属于一个网格。"""
        model = SliceModel()
        model.reset(17, 11)
        model.add("v", 7)
        model.add("v", 15)
        model.add("h", 3)
        model.add("h", 8)
        marks = [[0 for _ in range(17)] for _ in range(11)]
        for tile in model.tiles():
            left, top, right, bottom = tile.bounds
            for yy in range(top, bottom):
                for xx in range(left, right):
                    marks[yy][xx] += 1
        self.assertTrue(all(pixel == 1 for row in marks for pixel in row))
        state = model.snapshot()
        model.add("h", 5)
        model.restore(state)
        self.assertEqual(model.snapshot(), state)
        self.assertEqual(len(model.tiles()), 9)

    def test_guide_limit(self):
        """图像最外沿、非法方向与同位置重叠必须拒绝。"""
        model = SliceModel()
        model.reset(100, 40)
        guide = model.add("h", 15)
        with self.assertRaises(ValueError):
            model.add("h", 15)
        with self.assertRaises(ValueError):
            model.add("h", 40)
        with self.assertRaises(ValueError):
            model.add("z", 20)
        self.assertTrue(model.move(guide.identifier, 39))
        self.assertEqual(guide.position, 39)
        self.assertFalse(model.move(guide.identifier, 200))

    def test_rgba_png_pixel_equality(self):
        """重组全部切块后，RGBA 每个像素必须与源图相等。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = Image.new("RGBA", (109, 77))
            source.putdata([
                ((xx * 19) % 256, (yy * 23) % 256, (xx * 3 + yy * 9) % 256, (xx * yy) % 256)
                for yy in range(77) for xx in range(109)
            ])
            image_path = folder / "中文图片.png"
            source.save(image_path, format="PNG")
            model = SliceModel()
            model.reset(109, 77)
            model.add("v", 41)
            model.add("v", 100)
            model.add("h", 17)
            model.add("h", 57)
            pieces = model.tiles()
            filenames = export_png_tiles(str(image_path), str(folder / "导出"), pieces)
            restored = Image.new("RGBA", source.size)
            for tile, filename in zip(pieces, filenames):
                with Image.open(filename) as part:
                    restored.paste(part, tile.bounds[:2])
            self.assertEqual(restored.tobytes(), source.tobytes())
            self.assertEqual(len(filenames), 9)

    def test_jpeg_no_additional_loss(self):
        """JPEG 仅解码一次再输出 PNG，像素不得发生第二次有损压缩。"""
        with tempfile.TemporaryDirectory() as tmp:
            filename = Path(tmp) / "sample.jpg"
            sample = Image.new("RGB", (37, 25))
            sample.putdata([((x * 11) % 256, (y * 13) % 256, (x + y) % 256)
                            for y in range(25) for x in range(37)])
            sample.save(filename, "JPEG", quality=82)
            with Image.open(filename) as decoder:
                expected = decoder.crop((3, 4, 25, 20)).tobytes()
            result = export_png_tiles(str(filename), str(Path(tmp) / "out"), [CropTile(1, 1, (3, 4, 25, 20))])
            with Image.open(result[0]) as actual:
                self.assertEqual(actual.tobytes(), expected)

    def test_exif_orientation(self):
        """方向标签为 6 的 JPEG，预览元数据和 PNG 裁切须使用一致坐标。"""
        with tempfile.TemporaryDirectory() as tmp:
            filename = Path(tmp) / "turn.jpg"
            image = Image.new("RGB", (22, 13), color=(50, 120, 200))
            exif = Image.Exif()
            exif[274] = 6
            image.save(filename, "JPEG", exif=exif)
            width, height, orientation, frames = read_image_info(str(filename))
            self.assertEqual((width, height, orientation, frames), (13, 22, 6, 1))
            with Image.open(filename) as raw:
                reference = ImageOps.exif_transpose(raw).crop((2, 1, 11, 19)).tobytes()
            result = export_png_tiles(str(filename), str(Path(tmp) / "output"), [CropTile(1, 1, (2, 1, 11, 19))])
            with Image.open(result[0]) as actual:
                self.assertEqual(actual.tobytes(), reference)

    def test_cancel_does_not_create_partial_tile(self):
        """取消前已经写好的切块保留，不生成后续图片。"""
        with tempfile.TemporaryDirectory() as tmp:
            filename = Path(tmp) / "plain.png"
            Image.new("RGB", (10, 10), "red").save(filename)
            pieces = [CropTile(1, 1, (0, 0, 5, 10)), CropTile(1, 2, (5, 0, 10, 10))]
            progress = []
            result = export_png_tiles(str(filename), str(Path(tmp) / "out"), pieces,
                progress=lambda current, total: progress.append(current),
                cancel=lambda: len(progress) >= 1)
            self.assertEqual(len(result), 1)
            self.assertEqual(list((Path(tmp) / "out").glob(".quickslice_*")), [])

    def test_reject_16bit_rgba_png(self):
        """对于 Pillow 会静默降位深的格式，直接拒绝而不是假称无损。"""
        with tempfile.TemporaryDirectory() as tmp:
            filename = Path(tmp) / "wide.png"
            image = Image.new("RGBA", (2, 2), (1, 2, 3, 4))
            image.save(filename)
            # PNG 头中 IHDR 的偏移 24 是位深；修改为 16 位并修复 CRC。
            # 此时压缩数据仍为 8 位，但解析阶段会把它视为 16 位，
            # 测试导出函数可以在读取像素前阻止错误转换。
            blob = bytearray(filename.read_bytes())
            blob[24] = 16
            import zlib
            checksum = zlib.crc32(blob[12:29]) & 0xffffffff
            blob[29:33] = struct.pack(">I", checksum)
            filename.write_bytes(blob)
            with self.assertRaises(ValueError):
                export_png_tiles(str(filename), str(Path(tmp) / "out"), [CropTile(1, 1, (0, 0, 2, 2))])


if __name__ == "__main__":
    unittest.main(verbosity=2)
