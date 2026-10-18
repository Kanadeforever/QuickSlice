"""直接绘制的 PySide6 切图画布，重点保证鼠标缩放、平移和线段拖动跟手。

图片只在后台解码一次预览缓存；鼠标移动不重新裁切图像，也不计算分区。
本模块的所有坐标转换以完整原图像素为准，导出从不使用预览图。
切割线采用双层高对比描边，线段端点可以独立拖动，右键可分断。
"""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, QPointF, QRectF, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget, QMenu

from core import Guide, SliceModel


class ImageCanvas(QWidget):
    """画布保持独立于导出计算；每次鼠标操作只触发一次同步重绘。"""

    selectGuide = Signal(object)
    guidePressed = Signal(int)
    guideEdited = Signal(int)
    guideReleased = Signal(int)
    addGuide = Signal(str, int)
    chooseSplitPoint = Signal(int, int)
    moveSplitPoint = Signal(int, int)
    confirmSplit = Signal()
    deleteGuide = Signal(int)
    viewportChanged = Signal(float)
    mousePosition = Signal(int, int)
    filesDropped = Signal(object)
    nudgeGuide = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)
        self.model: SliceModel | None = None
        self.pixmap = QPixmap()
        self.scale = 1.0
        self.offset = QPointF(0, 0)
        self.selected: int | None = None
        self._drag_guide: int | None = None
        self._drag_action = "move"
        self._drag_breakpoint = False
        # 仅保存界面候选标记，不改变原线段；主窗口执行确认后才写入模型。
        self.split_point: tuple[int, int] | None = None
        self._pan_start: QPointF | None = None
        self._pan_origin = QPointF(0, 0)
        self._space_down = False
        self._last_cursor = QPointF(-100, -100)
        self._preview_axis = "v"
        self.line_color = QColor("#00d8f7")
        self.high_contrast = True
        self.line_width = 2
        self.setMinimumSize(340, 260)

    def set_line_style(self, color: str, high_contrast: bool, width: int) -> None:
        """外观只在预览绘制中生效，不会修改任何图像像素。"""
        chosen = QColor(color)
        if chosen.isValid():
            self.line_color = chosen
        self.high_contrast = bool(high_contrast)
        self.line_width = max(1, min(5, int(width)))
        self.update()

    def set_image(self, model: SliceModel, pixmap: QPixmap) -> None:
        self.model, self.pixmap = model, pixmap
        self.selected = None
        self._drag_guide = None
        self._drag_breakpoint = False
        self.split_point = None
        self._pan_start = None
        self.fit_image()

    def has_image(self) -> bool:
        return self.model is not None and self.model.width > 0 and not self.pixmap.isNull()

    def fit_image(self) -> None:
        if not self.has_image():
            self.update()
            return
        available_w = max(1, self.width() - 48)
        available_h = max(1, self.height() - 48)
        self.scale = min(available_w / self.model.width, available_h / self.model.height)
        self.offset = QPointF(
            (self.width() - self.model.width * self.scale) / 2,
            (self.height() - self.model.height * self.scale) / 2,
        )
        self.viewportChanged.emit(self.scale)
        self.update()

    def actual_size(self) -> None:
        self.set_zoom(1.0, QPointF(self.width() / 2, self.height() / 2))

    def set_zoom(self, zoom: float, pivot: QPointF) -> None:
        """以当前指针位置为支点，使频繁缩放时图片不发生突然跳跃。"""
        if not self.has_image():
            return
        zoom = max(0.002, min(24.0, zoom))
        old_point = self.map_to_image(pivot)
        if abs(zoom - self.scale) < 1e-9:
            return
        self.scale = zoom
        self.offset = QPointF(pivot.x() - old_point.x() * zoom, pivot.y() - old_point.y() * zoom)
        self.viewportChanged.emit(self.scale)
        self.update()

    def map_to_image(self, point: QPointF) -> QPointF:
        return QPointF((point.x() - self.offset.x()) / self.scale,
                       (point.y() - self.offset.y()) / self.scale)

    def map_from_image(self, x: float, y: float) -> QPointF:
        return QPointF(self.offset.x() + x * self.scale,
                       self.offset.y() + y * self.scale)

    def _guide_points(self, guide: Guide) -> tuple[QPointF, QPointF]:
        """线段两端屏幕坐标是唯一的渲染和命中来源，不再把线延伸到边框。"""
        if guide.axis == "h":
            return (self.map_from_image(guide.start, guide.position),
                    self.map_from_image(guide.end, guide.position))
        return (self.map_from_image(guide.position, guide.start),
                self.map_from_image(guide.position, guide.end))

    def _guide_at(self, point: QPointF) -> int | None:
        """按屏幕像素命中线段，缩放时仍保持一致的抓取宽度。"""
        if not self.has_image():
            return None
        best = 9.0
        selected = None
        for guide in self.model.guides:
            a, b = self._guide_points(guide)
            if guide.axis == "h":
                inside = min(a.x(), b.x()) - 6 <= point.x() <= max(a.x(), b.x()) + 6
                distance = abs(point.y() - a.y())
            else:
                inside = min(a.y(), b.y()) - 6 <= point.y() <= max(a.y(), b.y()) + 6
                distance = abs(point.x() - a.x())
            if inside and (distance < best or (distance == best and guide.identifier == self.selected)):
                best, selected = distance, guide.identifier
        return selected

    def _handle_at(self, point: QPointF) -> str | None:
        """选中线段显示两个可拖动端点，优先级高于整段平移。"""
        guide = self.model.get(self.selected) if self.model else None
        if guide is None:
            return None
        a, b = self._guide_points(guide)
        for endpoint, screen in (("start", a), ("end", b)):
            if (point - screen).manhattanLength() < 11:
                return endpoint
        return None

    def set_split_point(self, identifier: int | None, coordinate: int | None) -> None:
        """显示可拖动的断点标记；它不是一条新的切割线。"""
        self.split_point = ((identifier, coordinate)
                            if identifier is not None and coordinate is not None else None)
        self.update()

    def _breakpoint_at(self, point: QPointF) -> bool:
        """断点标记具有独立抓取范围，优先于线段移动和端点伸缩。"""
        if self.split_point is None or self.model is None:
            return False
        identifier, coordinate = self.split_point
        guide = self.model.get(identifier)
        if guide is None:
            return False
        marker = (self.map_from_image(coordinate, guide.position) if guide.axis == "h"
                  else self.map_from_image(guide.position, coordinate))
        return (point - marker).manhattanLength() <= 16

    def _inside_image(self, point: QPointF) -> bool:
        if not self.has_image():
            return False
        src = self.map_to_image(point)
        return 0 <= src.x() <= self.model.width and 0 <= src.y() <= self.model.height

    def _snap_endpoint(self, guide: Guide, value: float) -> int:
        """端点吸附到图片边缘或与其相交的另一方向切割线，阈值为 9 屏幕像素。"""
        limit = self.model.span_limit(guide.axis)
        candidates = [0, limit]
        for other in self.model.guides:
            if other.axis == guide.axis:
                continue
            if other.start <= guide.position <= other.end:
                candidates.append(other.position)
        closest = min(candidates, key=lambda target: abs(target - value))
        if abs(closest - value) * self.scale <= 9:
            return closest
        return round(value)

    def _line(self, painter: QPainter, a: QPointF, b: QPointF, color: QColor,
              width: float, dashed: bool = False) -> None:
        pen = QPen(color, width)
        pen.setCosmetic(True)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(a, b)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#171b23"))
        if not self.has_image():
            painter.setPen(QColor("#9daabe"))
            painter.setFont(QFont("Microsoft YaHei", 11))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "拖拽图片到这里\n\n滚轮缩放 · 中键 / 空格 / Ctrl＋左键平移\n右键竖线 · Shift＋右键横线")
            return
        dest = QRectF(self.offset.x(), self.offset.y(),
                      self.model.width * self.scale, self.model.height * self.scale)
        visible = dest.intersected(QRectF(self.rect()))
        if not visible.isEmpty():
            painter.fillRect(visible, QColor("white"))
            sx = (visible.x() - dest.x()) / dest.width() * self.pixmap.width()
            sy = (visible.y() - dest.y()) / dest.height() * self.pixmap.height()
            sw = visible.width() / dest.width() * self.pixmap.width()
            sh = visible.height() / dest.height() * self.pixmap.height()
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.scale < 2)
            painter.drawPixmap(visible, self.pixmap, QRectF(sx, sy, sw, sh))
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            painter.setPen(QPen(QColor("#8993a3"), 1))
            painter.drawRect(dest)
        painter.save()
        painter.setClipRect(visible)
        for guide in self.model.guides:
            chosen = guide.identifier == self.selected
            a, b = self._guide_points(guide)
            inner = QColor("#ffdb54") if chosen else self.line_color
            if self.high_contrast:
                self._line(painter, a, b, QColor("#08111c"), self.line_width + 3)
            self._line(painter, a, b, inner, self.line_width + (0.5 if chosen else 0))
            if chosen:
                for end in (a, b):
                    painter.setPen(QPen(QColor("#07111d"), 2))
                    painter.setBrush(inner)
                    painter.drawEllipse(end, 5, 5)
        if self.split_point is not None:
            identifier, coordinate = self.split_point
            guide = self.model.get(identifier)
            if guide is not None:
                pos = (self.map_from_image(coordinate, guide.position) if guide.axis == "h"
                       else self.map_from_image(guide.position, coordinate))
                # 黄色菱形断点及深色轮廓与切割线端点的圆形作出视觉区分。
                painter.setPen(QPen(QColor("#07111d"), 3))
                painter.setBrush(QColor("#ff9d32"))
                corners = [QPointF(pos.x(), pos.y() - 8), QPointF(pos.x() + 8, pos.y()),
                           QPointF(pos.x(), pos.y() + 8), QPointF(pos.x() - 8, pos.y())]
                from PySide6.QtGui import QPolygonF
                painter.drawPolygon(QPolygonF(corners))
        # 悬停时显示将要添加的贯穿线，Shift 可实时切换方向。
        if self._pan_start is None and self._drag_guide is None and self._inside_image(self._last_cursor):
            img = self.map_to_image(self._last_cursor)
            color = QColor(self.line_color)
            color.setAlpha(130)
            if self._preview_axis == "h":
                a, b = self.map_from_image(0, round(img.y())), self.map_from_image(self.model.width, round(img.y()))
            else:
                a, b = self.map_from_image(round(img.x()), 0), self.map_from_image(round(img.x()), self.model.height)
            self._line(painter, a, b, color, 1, dashed=True)
        painter.restore()
        text = f"{self.model.width} × {self.model.height}  |  {self.scale * 100:.1f}%  |  {len(self.model.guides)} 条线段"
        metrics = painter.fontMetrics()
        label = QRectF(12, self.height() - 34, metrics.horizontalAdvance(text) + 24, 25)
        painter.fillRect(label, QColor(12, 17, 24, 215))
        painter.setPen(QColor("#d2d9e7"))
        painter.drawText(label, Qt.AlignmentFlag.AlignCenter, text)

    def wheelEvent(self, event) -> None:
        if self.has_image():
            steps = event.angleDelta().y() / 120
            if abs(steps) > 1e-5:
                self.set_zoom(self.scale * math.pow(1.17, steps), event.position())
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event) -> None:
        """保留中键、空格＋左键，增加 Ctrl＋左键平移，不抢占普通左键编辑。"""
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        point = event.position()
        if event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton and
            (self._space_down or bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier))
        ):
            self._pan_start = point
            self._pan_origin = QPointF(self.offset)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self.has_image():
            if self._breakpoint_at(point):
                self._drag_breakpoint = True
                self.setCursor(Qt.CursorShape.SizeHorCursor if self.model.get(self.split_point[0]).axis == "h"
                               else Qt.CursorShape.SizeVerCursor)
                event.accept()
                return
            handle = self._handle_at(point)
            found = self.selected if handle else self._guide_at(point)
            self.selected = found
            self.selectGuide.emit(found)
            if found is not None:
                self._drag_action = handle or "move"
                self._drag_guide = found
                self.guidePressed.emit(found)
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        """移动中只更新像素坐标和画布，拓扑计算延迟至鼠标释放。"""
        point = event.position()
        self._last_cursor = point
        self._preview_axis = "h" if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else "v"
        if self.has_image():
            img = self.map_to_image(point)
            self.mousePosition.emit(int(img.x()), int(img.y()))
        if self._pan_start is not None:
            self.offset = self._pan_origin + (point - self._pan_start)
            self.update()
            return
        if self._drag_breakpoint and self.split_point is not None:
            identifier, _ = self.split_point
            guide = self.model.get(identifier)
            if guide is not None:
                src = self.map_to_image(point)
                coord = src.x() if guide.axis == "h" else src.y()
                snapped, _ = self.model.split_candidate(identifier, coord, 11 / max(self.scale, .002))
                if snapped != self.split_point[1]:
                    self.set_split_point(identifier, snapped)
                    self.moveSplitPoint.emit(identifier, snapped)
            return
        if self._drag_guide is not None and self.model is not None:
            guide = self.model.get(self._drag_guide)
            if guide is not None:
                src = self.map_to_image(point)
                if self._drag_action == "move":
                    coordinate = round(src.y() if guide.axis == "h" else src.x())
                    changed = self.model.move(guide.identifier, coordinate)
                else:
                    raw = src.x() if guide.axis == "h" else src.y()
                    changed = self.model.resize(guide.identifier, self._drag_action,
                                                self._snap_endpoint(guide, raw))
                if changed:
                    self.guideEdited.emit(guide.identifier)
                    self.update()
            return
        handle = self._handle_at(point)
        found = self._guide_at(point)
        if self._breakpoint_at(point):
            guide = self.model.get(self.split_point[0])
            self.setCursor(Qt.CursorShape.SizeHorCursor if guide.axis == "h" else Qt.CursorShape.SizeVerCursor)
        elif handle:
            self.setCursor(Qt.CursorShape.SizeHorCursor if self.model.get(self.selected).axis == "h"
                           else Qt.CursorShape.SizeVerCursor)
        elif found is not None:
            guide = self.model.get(found)
            self.setCursor(Qt.CursorShape.SizeVerCursor if guide.axis == "h" else Qt.CursorShape.SizeHorCursor)
        else:
            pan_hint = self._space_down or bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            self.setCursor(Qt.CursorShape.OpenHandCursor if pan_hint else Qt.CursorShape.ArrowCursor)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_breakpoint:
            self._drag_breakpoint = False
            event.accept()
            return
        if self._pan_start is not None:
            self._pan_start = None
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._space_down else Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        if self._drag_guide is not None:
            identifier = self._drag_guide
            self._drag_guide = None
            self.guideReleased.emit(identifier)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        """右键在线上只设置断点；通过独立确认操作才修改线段。"""
        point = QPointF(event.pos())
        if not self._inside_image(point):
            return
        src = self.map_to_image(point)
        horizontal = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        hit = self._guide_at(point)
        if horizontal:
            self.addGuide.emit("h", round(src.y()))
        elif hit is None:
            self.addGuide.emit("v", round(src.x()))
        else:
            guide = self.model.get(hit)
            along = round(src.x() if guide.axis == "h" else src.y())
            menu = QMenu(self)
            if guide.start < along < guide.end:
                candidate, snapped = self.model.split_candidate(hit, along, 11 / max(self.scale, .002))
                suffix = "（已吸附交叉点）" if snapped else ""
                menu.addAction("在此设置断点" + suffix,
                               lambda: self.chooseSplitPoint.emit(hit, candidate))
            if self.split_point is not None and self.split_point[0] == hit:
                menu.addAction("确认在已设置的断点处分断", lambda: self.confirmSplit.emit())
            menu.addAction("删除该线段", lambda: self.deleteGuide.emit(hit))
            menu.addSeparator()
            menu.addAction("在此添加水平线", lambda: self.addGuide.emit("h", round(src.y())))
            menu.addAction("在此添加垂直线", lambda: self.addGuide.emit("v", round(src.x())))
            menu.exec(event.globalPos())
        event.accept()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.filesDropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    def keyPressEvent(self, event) -> None:
        # 按住 Shift 即刻显示横向放置预览，不必再移动鼠标刷新提示线。
        if event.key() == Qt.Key.Key_Shift and not event.isAutoRepeat():
            self._preview_axis = "h"
            self.update()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_down = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.split_point is not None:
            self.confirmSplit.emit()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_Right, Qt.Key.Key_Down):
            guide = self.model.get(self.selected) if self.model else None
            if guide is not None:
                horizontal_key = event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right)
                if horizontal_key == (guide.axis == "v"):
                    positive = event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Down)
                    amount = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
                    self.nudgeGuide.emit(amount if positive else -amount)
                    event.accept()
                    return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Shift and not event.isAutoRepeat():
            self._preview_axis = "v"
            self.update()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_down = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event) -> None:
        """窗口切换时重置空格状态，防止画布永久停留在手掌工具。"""
        self._space_down = False
        self._drag_breakpoint = False
        if self._drag_guide is not None:
            # 窗口失焦时完成已有拖动的撤销记录，避免 Alt+Tab 留下悬挂编辑状态。
            identifier = self._drag_guide
            self._drag_guide = None
            self.guideReleased.emit(identifier)
        self._pan_start = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().focusOutEvent(event)

    def resizeEvent(self, event) -> None:
        if self.has_image():
            old_width, old_height = event.oldSize().width(), event.oldSize().height()
            if old_width > 0 and old_height > 0:
                center = self.map_to_image(QPointF(old_width / 2, old_height / 2))
                self.offset = QPointF(self.width() / 2 - center.x() * self.scale,
                                      self.height() / 2 - center.y() * self.scale)
        super().resizeEvent(event)
