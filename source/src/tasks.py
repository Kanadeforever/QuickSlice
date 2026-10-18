"""图片预览解码和成品导出工作线程。

耗时任务不进入 GUI 主线程，因此大图打开、写盘和压缩期间窗口仍可响应。
任务的执行入口只读取不可变参数，不从工作线程直接操作 Qt 控件。
"""

from __future__ import annotations

import math
import threading

from PIL import Image, ImageOps
from PySide6.QtCore import QObject, QRunnable, Signal

from core import CropTile, export_png_tiles, read_image_info


class LoadSignals(QObject):
    """跨线程发送解码后的原始 RGBA 字节和尺寸。"""

    ready = Signal(int, object)
    failed = Signal(int, str)


class LoadTask(QRunnable):
    """限制预览缓存像素量，始终让绘制成本与原图尺寸脱钩。"""

    def __init__(self, identifier: int, path: str, pixel_budget: int = 24_000_000) -> None:
        super().__init__()
        self.identifier = identifier
        self.path = path
        self.pixel_budget = pixel_budget
        self.signals = LoadSignals()

    def run(self) -> None:
        try:
            width, height, orientation, frames = read_image_info(self.path)
            if frames != 1:
                raise ValueError("多帧 GIF / WebP / TIFF 尚不支持，请先转换为单帧图片。")
            # 预览像素上限与最长边双重保护，减少占用内存和 GPU/Qt 位图压力。
            factor = min(1.0, math.sqrt(self.pixel_budget / (width * height)), 16384 / max(width, height))
            preview_size = (max(1, round(width * factor)), max(1, round(height * factor)))
            with Image.open(self.path) as image:
                # JPEG 可以在解码阶段下采样，大幅缩短打开超大 JPEG 的时间。
                draft_size = preview_size if orientation not in (5, 6, 7, 8) else preview_size[::-1]
                image.draft("RGB", draft_size)
                image.thumbnail(draft_size, Image.Resampling.BILINEAR, reducing_gap=2.5)
                if orientation in (2, 3, 4, 5, 6, 7, 8):
                    oriented = ImageOps.exif_transpose(image)
                else:
                    oriented = image
                try:
                    bitmap = oriented.convert("RGBA")
                    try:
                        payload = bitmap.tobytes("raw", "RGBA")
                        pw, ph = bitmap.size
                    finally:
                        bitmap.close()
                finally:
                    if oriented is not image:
                        oriented.close()
            self.signals.ready.emit(self.identifier, (self.path, width, height, pw, ph, payload))
        except Exception as exc:
            self.signals.failed.emit(self.identifier, f"{type(exc).__name__}: {exc}")


class ExportSignals(QObject):
    """导出进度和错误通过信号回到主线程。"""

    progress = Signal(int, int)
    finished = Signal(object)
    failed = Signal(str)


class ExportTask(QRunnable):
    """逐块裁切原图，输出严格无重采样的 PNG。"""

    def __init__(self, path: str, folder: str, tiles: list[CropTile]) -> None:
        super().__init__()
        self.path, self.folder, self.tiles = path, folder, tiles
        self.signals = ExportSignals()
        self._stop = threading.Event()

    def cancel(self) -> None:
        """用户要求取消时，在当前写盘结束后停止创建下一块。"""
        self._stop.set()

    def run(self) -> None:
        try:
            paths = export_png_tiles(
                self.path,
                self.folder,
                self.tiles,
                progress=lambda index, total: self.signals.progress.emit(index, total),
                cancel=self._stop.is_set,
            )
            self.signals.finished.emit((paths, self._stop.is_set()))
        except Exception as exc:
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")


class BatchExportTask(QRunnable):
    """逐张处理队列图片；共享取消标记，任何时刻只加载一张完整原图。

    导出文件都进入各自原图同名子目录；主线程提前检查覆盖风险，
    工作者只负责无损裁切和报告累计进度，不接触 Qt 控件。
    """

    def __init__(self, jobs: list[tuple[str, str, list[CropTile]]]) -> None:
        super().__init__()
        self.jobs = jobs
        self.signals = ExportSignals()
        self._stop = threading.Event()

    def cancel(self) -> None:
        self._stop.set()

    def run(self) -> None:
        saved: list[str] = []
        total = sum(len(tiles) for _, _, tiles in self.jobs)
        try:
            for path, folder, tiles in self.jobs:
                if self._stop.is_set():
                    break
                base = len(saved)
                current = export_png_tiles(
                    path, folder, tiles,
                    progress=lambda count, _total, start=base:
                        self.signals.progress.emit(start + count, total),
                    cancel=self._stop.is_set,
                )
                saved.extend(current)
            self.signals.finished.emit((saved, self._stop.is_set()))
        except Exception as exc:
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")
