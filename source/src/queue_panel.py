"""QuickSlice 多图片队列侧栏。

队列只显示文件名和编辑状态，不在主线程读取所有大图。取消固定后，
侧栏以画布上方的浮动面板出现，离开面板时自动隐藏，不挤压主画布。
每张图片的完整切割线记录由主窗口维护，侧栏只发送选择/移除命令。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QSize, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)


def create_pin_icon(pinned: bool) -> QIcon:
    """绘制小型清晰图钉：竖直亮蓝代表固定，倾斜灰色代表自动隐藏。"""
    canvas = QPixmap(64, 64)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    # 使用两倍尺寸抗锯齿，然后由 QIcon 缩放到约 16 逻辑像素。
    painter.scale(2.0, 2.0)
    if not pinned:
        painter.translate(16.0, 16.0)
        painter.rotate(-35.0)
        painter.translate(-16.0, -16.0)
    # 使用 QPen 的明确设置方法，避免 QColor 与浮点线宽的重载匹配差异。
    # RoundCap 让直线末端圆润；RoundJoin 让线条拐角圆润。
    pin_pen = QPen(QColor("#67cfff" if pinned else "#b6c3d5"))
    pin_pen.setWidthF(2.3)
    pin_pen.setStyle(Qt.PenStyle.SolidLine)
    pin_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pin_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pin_pen)
    painter.setBrush(QColor("#244961" if pinned else "#354253"))
    painter.drawRoundedRect(QRectF(10.0, 5.0, 12.0, 8.0), 2.0, 2.0)
    painter.drawLine(QPointF(12, 13), QPointF(11, 20))
    painter.drawLine(QPointF(20, 13), QPointF(21, 20))
    painter.drawLine(QPointF(10, 20), QPointF(22, 20))
    painter.drawLine(QPointF(16, 20), QPointF(16, 28))
    painter.end()
    return QIcon(canvas)


class QueuePanel(QWidget):
    """负责用户列表操作；不对图像对象进行解码或复制。"""

    activated = Signal(str)
    removed = Signal(str)
    pinChanged = Signal(bool)
    exportAll = Signal()
    filesDropped = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("queuePanel")
        self.setAcceptDrops(True)
        self._updating = False
        self._pinned = True
        # 固定面板适当缩窄；浮动面板另由主窗口限制宽高，不再铺满整张画布。
        self.setMinimumWidth(215)
        self.setMaximumWidth(260)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header.addWidget(QLabel("图片队列"), 1)
        self.pin_button = QPushButton()
        self.pin_button.setObjectName("helpButton")
        self.pin_button.setFixedSize(30, 30)
        self.pin_button.setIconSize(QSize(18, 18))
        self.pin_button.setIcon(create_pin_icon(True))
        self.pin_button.setToolTip("取消固定：切换为鼠标靠近时展开的自动隐藏队列")
        self.pin_button.setAccessibleName("取消固定图片队列")
        self.pin_button.clicked.connect(lambda: self.pinChanged.emit(not self._pinned))
        header.addWidget(self.pin_button)
        layout.addLayout(header)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["文件", "状态"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemActivated.connect(self._open_item)
        self.tree.itemClicked.connect(self._open_item)
        # 最后一列固定为状态，文件名列自动吃掉剩余宽度并省略显示过长文本。
        # 文件完整路径仍在 tooltip 中，避免水平滚动条遮挡队列底部按钮。
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, 66)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.tree, 1)
        actions = QHBoxLayout()
        self.remove_button = QPushButton("移除选中")
        self.remove_button.clicked.connect(self._remove_selected)
        self.batch_button = QPushButton("导出全部")
        self.batch_button.setObjectName("primary")
        self.batch_button.clicked.connect(lambda: self.exportAll.emit())
        self.remove_button.setObjectName("smallAction")
        self.batch_button.setObjectName("smallAction")
        actions.setSpacing(5)
        actions.addWidget(self.remove_button)
        actions.addWidget(self.batch_button)
        layout.addLayout(actions)
        # 这条提示只属于说明文档，不再常驻占用队列底部高度。

    def set_pinned(self, pinned: bool) -> None:
        """固定/自动隐藏的切换由主窗口执行布局变更。"""
        self._pinned = pinned
        self.pin_button.setIcon(create_pin_icon(pinned))
        self.pin_button.setToolTip("取消固定：允许队列自动隐藏" if pinned else "固定侧栏：始终显示图片队列")
        self.pin_button.setAccessibleName("取消固定图片队列" if pinned else "固定图片队列")

    def update_items(self, documents, active_path: str | None) -> None:
        """重建少量文本行；不会重新创建或重新解码图片预览。"""
        self._updating = True
        try:
            self.tree.clear()
            for doc in documents:
                state = "已编辑" if doc.model and doc.model.guides else "待编辑"
                item = QTreeWidgetItem([Path(doc.path).name, state])
                item.setData(0, Qt.ItemDataRole.UserRole, doc.path)
                item.setToolTip(0, doc.path)
                self.tree.addTopLevelItem(item)
                if doc.path == active_path:
                    self.tree.setCurrentItem(item)
        finally:
            self._updating = False

    def _open_item(self, item, column: int) -> None:
        if not self._updating:
            path = str(item.data(0, Qt.ItemDataRole.UserRole))
            # 延后到本次 Qt 鼠标事件结束再切换，避免列表重建时删掉正在回调的条目。
            QTimer.singleShot(0, lambda: self.activated.emit(path))

    def _remove_selected(self) -> None:
        item = self.tree.currentItem()
        if item:
            self.removed.emit(str(item.data(0, Qt.ItemDataRole.UserRole)))

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and any(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.filesDropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()
