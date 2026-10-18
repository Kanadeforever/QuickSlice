"""原图像素坐标的自由切割线模型、区域拓扑和无损 PNG 导出。

界面仅负责编辑线段和显示缩略图；所有切割结果都从完整原图重新读取。
切割线只允许水平或垂直，但每条线都有独立的起点、终点。
将各线段的端点投影为小矩形网格，再根据有无切割线决定相邻小格是否相通。
这样 T 字、十字和非矩形封闭区域都可以用同一套规则处理。
"""

from __future__ import annotations

from collections import deque
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import os
import tempfile

from PIL import Image, ImageChops, ImageDraw, ImageOps


@dataclass
class Guide:
    """可独立编辑的切割线：position 是垂直于线段的坐标，start/end 是沿线坐标。"""

    identifier: int
    axis: str
    position: int
    start: int
    end: int


@dataclass(frozen=True)
class CropTile:
    """待导出的独立区域。parts 为空代表整个 bounds 都属于该矩形。

    非矩形时 parts 是构成区域的无重叠矩形，坐标仍以原图左上角为原点。
    PNG 导出将区域外的像素变透明，区域内的原始像素不经过插值。
    """

    row: int
    column: int
    bounds: tuple[int, int, int, int]
    parts: tuple[tuple[int, int, int, int], ...] = ()

    @property
    def is_rectangle(self) -> bool:
        return not self.parts


class SliceModel:
    """管理可分断线段，生成由实际线段分隔出来的独立区域。"""

    MAX_CELLS = 350_000

    def __init__(self) -> None:
        self.width = 0
        self.height = 0
        self.guides: list[Guide] = []
        self._next_id = 1
        self._cached_tiles: list[CropTile] | None = None

    def _invalidate(self) -> None:
        """编辑后失效区域缓存；鼠标每帧移动时不执行区域分析。"""
        self._cached_tiles = None

    def reset(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("图片尺寸必须大于零。")
        self.width, self.height = int(width), int(height)
        self.guides.clear()
        self._next_id = 1
        self._invalidate()

    def limit(self, axis: str) -> int:
        """返回垂直于线的坐标上限，不能把切割线放在图片边缘。"""
        if axis not in ("h", "v"):
            raise ValueError("未知的切割线方向。")
        return self.height if axis == "h" else self.width

    def span_limit(self, axis: str) -> int:
        """沿线方向允许使用图片外框坐标，便于线段贴合边界。"""
        return self.width if axis == "h" else self.height

    def _validate(self, axis: str, position: int, start: int, end: int,
                  exclude: int | None = None) -> None:
        if not 0 < position < self.limit(axis):
            raise ValueError(f"切割位置必须在 1～{self.limit(axis) - 1} 像素之间。")
        if not 0 <= start < end <= self.span_limit(axis):
            raise ValueError("线段端点必须位于图片边界内，且长度至少为 1 像素。")
        for guide in self.guides:
            if guide.identifier != exclude and guide.axis == axis and guide.position == position:
                # 允许两段线只在端点接触；真正重叠的像素区间会导致编辑歧义。
                if max(start, guide.start) < min(end, guide.end):
                    raise ValueError("同方向、同坐标的线段不能重叠。")

    def add(self, axis: str, position: int, start: int | None = None,
            end: int | None = None) -> Guide:
        """未指定起点和终点时创建贯穿整图的线条，便于快速建立规则切割。"""
        position = int(position)
        start = 0 if start is None else int(start)
        end = self.span_limit(axis) if end is None else int(end)
        self._validate(axis, position, start, end)
        guide = Guide(self._next_id, axis, position, start, end)
        self._next_id += 1
        self.guides.append(guide)
        self._invalidate()
        return guide

    def get(self, identifier: int | None) -> Guide | None:
        return next((guide for guide in self.guides if guide.identifier == identifier), None)

    def move(self, identifier: int, position: int) -> bool:
        """整段平移只改变垂直于线的坐标，线段长度保持不变。"""
        guide = self.get(identifier)
        if guide is None:
            return False
        position = max(1, min(self.limit(guide.axis) - 1, int(position)))
        if position == guide.position:
            return False
        try:
            self._validate(guide.axis, position, guide.start, guide.end, identifier)
        except ValueError:
            return False
        guide.position = position
        self._invalidate()
        return True

    def resize(self, identifier: int, endpoint: str, coordinate: int) -> bool:
        """只移动指定端点，避免拖动端点时另一端跟着变化。"""
        guide = self.get(identifier)
        if guide is None or endpoint not in ("start", "end"):
            return False
        value = max(0, min(self.span_limit(guide.axis), int(coordinate)))
        start, end = (value, guide.end) if endpoint == "start" else (guide.start, value)
        if (start, end) == (guide.start, guide.end):
            return False
        try:
            self._validate(guide.axis, guide.position, start, end, identifier)
        except ValueError:
            return False
        guide.start, guide.end = start, end
        self._invalidate()
        return True

    def split(self, identifier: int, coordinate: int) -> Guide:
        """在任意内部像素坐标把线分为两条独立线段；两条线仍彼此接触。"""
        guide = self.get(identifier)
        if guide is None:
            raise ValueError("所选线段不存在。")
        coordinate = int(coordinate)
        if not guide.start < coordinate < guide.end:
            raise ValueError("只能在线段内部进行分断，不能选择端点。")
        old_end = guide.end
        guide.end = coordinate
        new_guide = Guide(self._next_id, guide.axis, guide.position, coordinate, old_end)
        self._next_id += 1
        self.guides.append(new_guide)
        self._invalidate()
        return new_guide

    def split_intersections(self, identifier: int) -> tuple[int, ...]:
        """找出目标线段内部真正存在的横纵交点，供界面优先吸附断点。

        只有另一条线确实覆盖交点位置，才视为候选；与目标端点重合
        的点不能形成两段有效线条，因此不提供给分断工具。
        """
        guide = self.get(identifier)
        if guide is None:
            return ()
        points: set[int] = set()
        for other in self.guides:
            if other.axis == guide.axis:
                continue
            if (other.start <= guide.position <= other.end and
                    guide.start < other.position < guide.end):
                points.add(other.position)
        return tuple(sorted(points))

    def split_candidate(self, identifier: int, coordinate: float,
                        snap_distance: float = 0) -> tuple[int, bool]:
        """计算待确认断点；在屏幕换算后的吸附距离内交点优先。

        参数 snap_distance 使用原图像素单位，画布负责由屏幕像素换算，
        这样放大/缩小时断点抓取范围始终一致；返回值第二项表示已吸附。
        本方法不修改线段和区域，用户确认后才真正分断。
        """
        guide = self.get(identifier)
        if guide is None or guide.end - guide.start < 2:
            raise ValueError("所选线段无法分断。")
        value = max(guide.start + 1, min(guide.end - 1, round(coordinate)))
        candidates = self.split_intersections(identifier)
        if candidates:
            nearest = min(candidates, key=lambda point: (abs(point - coordinate), point))
            if abs(nearest - coordinate) <= max(0, snap_distance):
                return nearest, True
        return value, False


    def remove(self, identifier: int) -> bool:
        for index, guide in enumerate(self.guides):
            if guide.identifier == identifier:
                del self.guides[index]
                self._invalidate()
                return True
        return False

    def snapshot(self) -> tuple[tuple[int, str, int, int, int], ...]:
        """撤销记录包括位置与两个端点，恢复后仍能继续独立编辑。"""
        return tuple((g.identifier, g.axis, g.position, g.start, g.end) for g in self.guides)

    def restore(self, state: tuple[tuple[int, str, int, int, int], ...]) -> None:
        self.guides = [Guide(*values) for values in state]
        self._next_id = max((g.identifier for g in self.guides), default=0) + 1
        self._invalidate()

    def tiles(self) -> list[CropTile]:
        """用障碍边界的连通域求实际分区，支持任意正交线段与非矩形区域。

        切割线在小格之间形成不可穿越的墙；没有墙的相邻格连成同一区域。
        外框本身视作封闭边界，不需要用户沿图片外边再绘制四条线。
        结果只在编辑后重新计算，普通画布绘制不会调用它。
        """
        if self._cached_tiles is not None:
            return self._cached_tiles
        if not self.width or not self.height:
            return []
        xs = sorted({0, self.width} |
                    {g.position for g in self.guides if g.axis == "v"} |
                    {value for g in self.guides if g.axis == "h" for value in (g.start, g.end)})
        ys = sorted({0, self.height} |
                    {g.position for g in self.guides if g.axis == "h"} |
                    {value for g in self.guides if g.axis == "v" for value in (g.start, g.end)})
        columns, rows = len(xs) - 1, len(ys) - 1
        if columns * rows > self.MAX_CELLS:
            raise ValueError("切割线交点与端点过多，区域网格超过安全上限；请减少线段或端点。")
        xi = {value: index for index, value in enumerate(xs)}
        yi = {value: index for index, value in enumerate(ys)}
        vertical_walls: set[int] = set()
        horizontal_walls: set[int] = set()
        for guide in self.guides:
            if guide.axis == "v":
                col = xi[guide.position]
                for row in range(yi[guide.start], yi[guide.end]):
                    vertical_walls.add(row * (columns + 1) + col)
            else:
                row = yi[guide.position]
                for col in range(xi[guide.start], xi[guide.end]):
                    horizontal_walls.add(row * columns + col)
        visited = bytearray(columns * rows)
        regions: list[tuple[tuple[int, int, int, int], tuple[tuple[int, int, int, int], ...]]] = []
        for origin in range(columns * rows):
            if visited[origin]:
                continue
            visited[origin] = 1
            queue = deque([origin])
            cells: dict[int, list[int]] = {}
            while queue:
                cell = queue.popleft()
                row, col = divmod(cell, columns)
                cells.setdefault(row, []).append(col)
                neighbors: list[int] = []
                if col and row * (columns + 1) + col not in vertical_walls:
                    neighbors.append(cell - 1)
                if col + 1 < columns and row * (columns + 1) + col + 1 not in vertical_walls:
                    neighbors.append(cell + 1)
                if row and row * columns + col not in horizontal_walls:
                    neighbors.append(cell - columns)
                if row + 1 < rows and (row + 1) * columns + col not in horizontal_walls:
                    neighbors.append(cell + columns)
                for neighbor in neighbors:
                    if not visited[neighbor]:
                        visited[neighbor] = 1
                        queue.append(neighbor)
            # 先把每行相邻小格合并，再把上下行相同区间合并成矩形，
            # 避免一张不规则区域被表示成大量细碎的单格矩形。
            finished: list[tuple[int, int, int, int]] = []
            active: dict[tuple[int, int], tuple[int, int, int, int]] = {}
            for row in sorted(cells):
                sorted_cols = sorted(cells[row])
                runs: list[tuple[int, int]] = []
                start = last = sorted_cols[0]
                for col in sorted_cols[1:]:
                    if col == last + 1:
                        last = col
                    else:
                        runs.append((xs[start], xs[last + 1]))
                        start = last = col
                runs.append((xs[start], xs[last + 1]))
                current = set(runs)
                for key in tuple(active):
                    if key not in current or active[key][3] != ys[row]:
                        finished.append(active.pop(key))
                for key in runs:
                    if key in active:
                        old = active[key]
                        active[key] = (old[0], old[1], old[2], ys[row + 1])
                    else:
                        active[key] = (key[0], ys[row], key[1], ys[row + 1])
            finished.extend(active.values())
            left = min(rect[0] for rect in finished)
            top = min(rect[1] for rect in finished)
            right = max(rect[2] for rect in finished)
            bottom = max(rect[3] for rect in finished)
            area = sum((r[2] - r[0]) * (r[3] - r[1]) for r in finished)
            bound = (left, top, right, bottom)
            parts = () if area == (right - left) * (bottom - top) else tuple(sorted(finished))
            regions.append((bound, parts))
        # 对同一顶部像素行按从左到右编号，使用稳定的 Rxx_Cxx 顺序命名，便于对应切块位置。
        regions.sort(key=lambda data: (data[0][1], data[0][0], data[0][3], data[0][2]))
        result: list[CropTile] = []
        row_number, column_number, previous_top = 0, 0, None
        for bound, parts in regions:
            if bound[1] != previous_top:
                row_number += 1
                column_number = 0
                previous_top = bound[1]
            column_number += 1
            result.append(CropTile(row_number, column_number, bound, parts))
        self._cached_tiles = result
        return result


# Windows 传统 MAX_PATH 限制为 260 个 UTF-16 单元。留出临时文件名、
# 运行库尾部字符和少量路径拼接空间；不依赖系统是否开启长路径策略。
MAX_SAFE_EXPORT_PATH = 240
MAX_SAFE_NAME_UNITS = 240


def _windows_name_units(value: str) -> int:
    """用 Windows 使用的 UTF-16 单元计数，表情符号通常占两个单元。"""
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def _within_export_limits(folder: Path, filename: str = "") -> bool:
    """同时检查单层文件名和最终绝对路径，防止仅考虑文件名漏掉深目录。"""
    if _windows_name_units(folder.name) > MAX_SAFE_NAME_UNITS:
        return False
    if _windows_name_units(str(folder.absolute())) > MAX_SAFE_EXPORT_PATH:
        return False
    if not filename:
        return True
    target = folder / filename
    return (_windows_name_units(filename) <= MAX_SAFE_NAME_UNITS and
            _windows_name_units(str(target.absolute())) <= MAX_SAFE_EXPORT_PATH)


def _short_export_stem(input_path: str) -> str:
    """只有原名确实超限才使用十字符前缀，并追加摘要防止不同长文件重名。"""
    path = Path(input_path)
    digest = hashlib.sha256(path.name.casefold().encode("utf-8", errors="surrogatepass")).hexdigest()[:8]
    return f"{path.stem[:10]}_{digest}"


def _full_tile_suffix(row_digits: int = 5, col_digits: int = 5) -> str:
    """为最多一万切块的行列编号留足位置，保证目录规划与文件导出一致。"""
    return f"_R{'9' * max(2, row_digits)}_C{'9' * max(2, col_digits)}.png"


def export_subfolder(parent_directory: str, input_path: str, suffix: str = "") -> Path:
    """计算无损切图子目录。正常情况保留原名，超限才退回十字符加摘要。

    suffix 用于同名图片的扩展名区分或批量队列的序号，必须参与
    路径长度验证；本函数不创建目录，取消导出不会留下空文件夹。
    """
    parent = Path(parent_directory)
    original = Path(input_path).stem
    for stem in (original, _short_export_stem(input_path)):
        folder = parent / (stem + suffix)
        if _within_export_limits(folder, f"{stem}{_full_tile_suffix()}"):
            return folder
    raise ValueError("目标路径过深：即使使用原图名前十个字符及短摘要也无法安全创建切图目录。请将原图移动到更浅的目录，或手动选择更短的导出路径。")


def tile_output_path(input_path: str, directory: str | Path,
                     tile: CropTile, row_digits: int = 2, col_digits: int = 2) -> Path:
    """生成预先校验过的切块路径，GUI 覆盖检测和工作线程必须共用本函数。

    目录已经按长文件名策略缩短时，切块文件名前缀也采用相同的安全简称。
    绝不在 GUI 和导出线程分别拼接不同名称，避免覆盖检测失效。
    """
    folder = Path(directory)
    original = Path(input_path).stem
    short = _short_export_stem(input_path)
    suffix = f"_R{tile.row:0{row_digits}d}_C{tile.column:0{col_digits}d}.png"
    # 子目录以简称开头，表示目录规划阶段已遇到文件名或路径长度限制。
    if folder.name.startswith(short):
        prefixes = (short,)
    else:
        prefixes = (original, short)
    for prefix in prefixes:
        filename = prefix + suffix
        if _within_export_limits(folder, filename):
            return folder / filename
    raise ValueError("切图输出路径过长，无法安全创建 PNG；请缩短目标目录路径。")


def next_queue_path(paths: list[str], current: str) -> str | None:
    """返回当前队列图片之后的下一张；单张、末尾或不在队列时均不切换。"""
    try:
        index = paths.index(current)
    except ValueError:
        return None
    return paths[index + 1] if index + 1 < len(paths) else None


def source_folder_suffix(input_path: str) -> str:
    """相同目录中若有同主名、不同扩展名的图片，追加格式名避免互相覆盖。

    例如同时存在 scene.png 与 scene.jpg，分别输出 scene_png 和
    scene_jpg。只处理受支持的静态图片后缀；不改动源文件。
    """
    path = Path(input_path)
    extensions = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
    try:
        # 发现第二张同名图片便立即退出，不把海量目录内容全部放入内存。
        matches = 0
        for entry in path.parent.iterdir():
            if (entry.is_file() and entry.stem.casefold() == path.stem.casefold()
                    and entry.suffix.casefold() in extensions):
                matches += 1
                if matches > 1:
                    return "_" + path.suffix.lstrip(".").lower()
    except OSError:
        return ""
    return ""


def read_image_info(path: str) -> tuple[int, int, int, int]:
    """获取按 EXIF 方向校正后的宽高、原始方向号和图像帧数。"""
    with Image.open(path) as source:
        orientation = int(source.getexif().get(274, 1) or 1)
        frames = int(getattr(source, "n_frames", 1))
        width, height = source.size
        if orientation in (5, 6, 7, 8):
            width, height = height, width
        return width, height, orientation, frames


def export_png_tiles(
    input_path: str,
    directory: str,
    tiles: list[CropTile],
    progress: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> list[str]:
    """按原图整数坐标导出 PNG；非矩形区域的外接框外部使用透明像素。

    先生成同目录临时 PNG，再用原子替换生成品。矩形图块不改变图像色彩模式。
    非矩形图块采用 RGBA+Alpha 蒙版，不重采样区域内像素。
    不能保持原始像素位深的高位深非矩形图块会报错，避免静默丢失精度。
    """
    if not tiles:
        raise ValueError("没有可导出的裁切区域。")
    output = Path(directory)
    output.mkdir(parents=True, exist_ok=True)
    total = len(tiles)
    row_digits = max(2, len(str(max(t.row for t in tiles))))
    col_digits = max(2, len(str(max(t.column for t in tiles))))
    result: list[str] = []
    with Image.open(input_path) as original:
        if getattr(original, "n_frames", 1) != 1:
            raise ValueError("暂不支持多帧动画图片，请先拆分为单帧图片。")
        if original.mode not in {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16", "I;16L", "I;16B"}:
            raise ValueError(f"原始像素格式 {original.mode} 尚不支持保证无损 PNG 导出。")
        if original.format == "PNG" and original.mode in {"RGB", "RGBA"}:
            with open(input_path, "rb") as binary:
                header = binary.read(26)
            if header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) >= 26 and header[24] == 16:
                raise ValueError("暂不支持 16 位 RGB/RGBA PNG 原始位深，已中止以避免降精度。")
        if any(tile.parts for tile in tiles) and original.mode in {"I", "I;16", "I;16L", "I;16B"}:
            raise ValueError("高位深图像的非矩形透明导出无法保持原始位深，已停止导出。")
        saved_info = original.info.copy()
        orientation = int(original.getexif().get(274, 1) or 1)
        source = ImageOps.exif_transpose(original) if orientation in range(2, 9) else original
        try:
            options: dict = {}
            if isinstance(saved_info.get("icc_profile"), bytes):
                options["icc_profile"] = saved_info["icc_profile"]
            if isinstance(saved_info.get("dpi"), tuple) and len(saved_info["dpi"]) == 2:
                options["dpi"] = saved_info["dpi"]
            # 导出过程中不会接触 Qt 预览图。源图保留在后台线程中。
            for index, tile in enumerate(tiles):
                if cancel is not None and cancel():
                    break
                x1, y1, x2, y2 = tile.bounds
                if not (0 <= x1 < x2 <= source.width and 0 <= y1 < y2 <= source.height):
                    raise ValueError(f"导出坐标越界：{tile.bounds}")
                # 与主窗口的覆盖检查共用同一命名规则，包括长文件名回退。
                final_path = tile_output_path(input_path, output, tile, row_digits, col_digits)
                temp_name: str | None = None
                try:
                    with tempfile.NamedTemporaryFile(dir=output, prefix=".quickslice_", suffix=".png", delete=False) as temp:
                        temp_name = temp.name
                    crop = source.crop(tile.bounds)
                    try:
                        if tile.parts:
                            rgba = crop.convert("RGBA")
                            try:
                                mask = Image.new("L", crop.size, 0)
                                try:
                                    draw = ImageDraw.Draw(mask)
                                    for a, b, c, d in tile.parts:
                                        if not (x1 <= a < c <= x2 and y1 <= b < d <= y2):
                                            raise ValueError("非矩形区域片段坐标越界。")
                                        # Pillow 的 rectangle 右下角是闭区间，坐标减 1 保证无交叠。
                                        draw.rectangle((a-x1, b-y1, c-x1-1, d-y1-1), fill=255)
                                    alpha = rgba.getchannel("A")
                                    rgba.putalpha(ImageChops.multiply(alpha, mask))
                                    rgba.save(temp_name, format="PNG", compress_level=1, **options)
                                finally:
                                    mask.close()
                            finally:
                                rgba.close()
                        else:
                            crop.save(temp_name, format="PNG", compress_level=1, **options)
                    finally:
                        crop.close()
                    os.replace(temp_name, final_path)
                    temp_name = None
                finally:
                    if temp_name and os.path.exists(temp_name):
                        os.unlink(temp_name)
                result.append(str(final_path))
                if progress is not None:
                    progress(index + 1, total)
        finally:
            if source is not original:
                source.close()
    return result
