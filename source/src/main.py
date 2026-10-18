"""QuickSlice 主窗口：协调图片加载、切割线编辑和后台无损导出。

启动入口为本文件。图像数据的核心运算放在 core.py，交互绘制放在
canvas.py，耗时操作放在 tasks.py，避免窗口与图片处理逻辑混杂。
用户配置保存在软件所在目录；不会使用 Windows 注册表。
"""

from __future__ import annotations

import configparser
from collections import OrderedDict
from dataclasses import dataclass, field
import os
from pathlib import Path
import sys

from PySide6.QtCore import QEvent, QPointF, QThreadPool, Qt, QTimer, QSignalBlocker
from PySide6.QtGui import QAction, QCursor, QIcon, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QColorDialog, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QInputDialog, QMainWindow,
    QMessageBox, QProgressDialog, QPushButton, QSpinBox, QSplitter,
    QTextBrowser, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from canvas import ImageCanvas
from core import (SliceModel, export_subfolder, read_image_info,
                  source_folder_suffix, tile_output_path, next_queue_path)
from queue_panel import QueuePanel
from tasks import BatchExportTask, ExportTask, LoadTask


APP_VERSION = "0.4.0-test5"
VALID_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}

STYLE = """
QMainWindow, QWidget { background: #1b202a; color: #e5eaf4; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 10pt; }
QMenuBar, QMenu { background: #252b37; color: #edf0f6; }
QMenu::item:selected { background: #35435a; }
QToolBar { background: #202631; border-bottom: 1px solid #323b4a; spacing: 6px; padding: 4px; }
QPushButton { background: #323c4f; color: #f0f2f8; border: 1px solid #4a576e; border-radius: 6px; padding: 8px 10px; min-height: 22px; }
QPushButton:hover { background: #3b4b65; }
QPushButton:pressed { background: #253247; }
QPushButton:disabled { color: #778397; border-color: #384050; background: #222a35; }
QPushButton#primary { background: #1877c8; border-color: #278bdd; font-weight: bold; }
QPushButton#primary:hover { background: #2591e1; }
QPushButton#helpButton { background: #303a4c; border: 1px solid #46536b; border-radius: 5px; padding: 0px; min-height: 0px; font-size: 12pt; font-weight: bold; }
QPushButton#helpButton:hover { background: #435573; }
QPushButton#queueTab { background: #303a4c; border: 1px solid #46536b; border-radius: 5px; padding: 0px; min-height: 0px; font-size: 12pt; }
QPushButton#queueTab:hover { background: #40516a; }
QPushButton#smallAction { padding: 4px 7px; min-height: 24px; }
QTextBrowser#manualBrowser { background: #151b25; color: #e5eaf4; border: 1px solid #39475d; border-radius: 5px; padding: 8px; }
QTreeWidget, QSpinBox { background: #151b25; color: #e7edf7; border: 1px solid #354052; border-radius: 5px; padding: 4px; }
QTreeWidget::item { min-height: 29px; }
QTreeWidget::item:selected { background: #2c5c81; }
QHeaderView::section { background: #2a3240; color: #d4ddeb; padding: 6px; border: none; }
QSplitter::handle { background: #323b49; width: 2px; }
QLabel#heading { font-size: 12pt; font-weight: bold; color: #ffffff; }
QLabel#secondary { color: #9baac0; }
QStatusBar { background: #202631; color: #abb8ca; }
QScrollBar:vertical { background: #202734; width: 11px; }
QScrollBar::handle:vertical { background: #526076; border-radius: 4px; min-height: 30px; }
QWidget#queuePanel { background: #232b39; border-right: 1px solid #46536b; }
"""


@dataclass
class QueueDocument:
    """每张图片独立持有切割线及历史，不因队列切换而相互覆盖。"""

    path: str
    model: SliceModel | None = None
    selected: int | None = None
    undo: list[tuple] = field(default_factory=list)
    redo: list[tuple] = field(default_factory=list)
    scale: float | None = None
    offset: tuple[float, float] | None = None


def application_folder() -> Path:
    """打包后使用 EXE 所在目录，源码运行时则使用项目配置目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent / "config"


class MainWindow(QMainWindow):
    """操作图片的软件主窗口，所有可见控件只在主线程更新。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"QuickSlice  |  无损切图器 v{APP_VERSION}")
        self.setMinimumSize(900, 580)
        self.resize(1380, 840)
        self.setStyleSheet(STYLE)
        self.model = SliceModel()
        self.input_path: str | None = None
        self.last_folder = str(Path.home())
        self.last_export_folder = str(Path.home())
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._load_id = 0
        self._pending_load: dict[int, LoadTask] = {}
        self._export_task: ExportTask | BatchExportTask | None = None
        self._progress: QProgressDialog | None = None
        self._selected: int | None = None
        self._undo: list[tuple] = []
        self._redo: list[tuple] = []
        self._before_drag: tuple | None = None
        self._sidebar_updating = False
        self._documents: list[QueueDocument] = []
        self._active_document: QueueDocument | None = None
        self._load_target: QueueDocument | None = None
        # 预览 LRU 只保留有限的像素缓存，队列图片多时也不会无限吃内存。
        self._preview_cache: OrderedDict[str, tuple[QPixmap, int]] = OrderedDict()
        self._preview_bytes = 0
        self._preview_limit = 180 * 1024 * 1024
        self._queue_pinned = True
        self._queue_overlay_open = False
        self._export_actual_folder = ""
        # 自动保存只在当前单图导出成功后推进队列；失败或取消绝不跳图。
        self._auto_advance_to: str | None = None
        self._auto_export = False
        self._export_expected_files = 0
        # 右侧面板只在首次显示时扩展到既有最大宽度；以后由用户自由拖动。
        self._sidebar_initial_width_done = False
        self._make_ui()
        self._load_settings()
        self._refresh()

    def _make_ui(self) -> None:
        """中央画布加窄控制栏，所有常用命令在同一窗口即可完成。"""
        menu_file = self.menuBar().addMenu("文件(&F)")
        menu_edit = self.menuBar().addMenu("编辑(&E)")
        menu_view = self.menuBar().addMenu("视图(&V)")
        # 帮助按钮位于菜单栏最右边，即窗口关闭按钮下方，不占用侧边栏空间。
        self.help_button = QPushButton("?", self.menuBar())
        self.help_button.setObjectName("helpButton")
        self.help_button.setFixedSize(28, 26)
        self.help_button.setToolTip("QuickSlice 完整使用说明")
        self.help_button.setAccessibleName("打开完整使用说明")
        self.help_button.clicked.connect(self.show_user_manual)
        # 使用菜单栏内的绝对定位，避免 Qt 的 cornerWidget 在部分 DPI 下被右边界裁切。
        # 该按钮与关闭窗口的标题栏按钮并不重叠，右侧始终留出 8 逻辑像素。
        self.menuBar().installEventFilter(self)
        self.help_button.raise_()

        # 顶部只保留两项高频画布缩放快捷入口，其他功能从菜单或右侧编辑。
        toolbar = QToolBar("缩放快捷操作", self)
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        self.addToolBar(toolbar)

        self.act_open = QAction("打开图片", self)
        self.act_open.setShortcut(QKeySequence("Ctrl+O"))
        self.act_open.triggered.connect(self.open_dialog)
        menu_file.addAction(self.act_open)

        self.act_export = QAction("导出 PNG", self)
        self.act_export.setShortcut(QKeySequence("Ctrl+E"))
        self.act_export.triggered.connect(self.start_export)
        menu_file.addAction(self.act_export)
        # 自动保存为全局设置，只保留文件菜单一个开关，不再在侧栏重复显示。
        self.act_auto_next = QAction("自动保存＋下一张（原图目录）", self)
        self.act_auto_next.setCheckable(True)
        menu_file.addAction(self.act_auto_next)

        self.act_h = QAction("＋ 水平线", self)
        self.act_h.triggered.connect(lambda: self.add_center_guide("h"))
        self.act_v = QAction("＋ 垂直线", self)
        self.act_v.triggered.connect(lambda: self.add_center_guide("v"))
        for action in (self.act_h, self.act_v):
            menu_edit.addAction(action)

        self.act_remove = QAction("删除选中的切割线", self)
        self.act_remove.setShortcut(QKeySequence("Delete"))
        self.act_remove.triggered.connect(self.remove_selected)
        menu_edit.addAction(self.act_remove)
        self.act_undo = QAction("撤销", self)
        self.act_undo.setShortcut(QKeySequence("Ctrl+Z"))
        self.act_undo.triggered.connect(self.undo)
        menu_edit.addAction(self.act_undo)
        self.act_redo = QAction("重做", self)
        self.act_redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        self.act_redo.triggered.connect(self.redo)
        menu_edit.addAction(self.act_redo)

        self.act_fit = QAction("适应窗口", self)
        self.act_fit.setShortcut(QKeySequence("Ctrl+0"))
        self.act_fit.triggered.connect(self.canvas_fit)
        self.act_original = QAction("实际像素 100%", self)
        self.act_original.setShortcut(QKeySequence("Ctrl+1"))
        self.act_original.triggered.connect(self.canvas_actual)
        for action in (self.act_fit, self.act_original):
            menu_view.addAction(action)
            toolbar.addAction(action)

        # 颜色与凸显方式仅改变画布覆盖层，不参与 PNG 像素导出。
        menu_view.addSeparator()
        self.act_line_color = QAction("切割线颜色…", self)
        self.act_line_color.triggered.connect(self.choose_line_color)
        menu_view.addAction(self.act_line_color)
        self.act_high_contrast = QAction("高对比度描边", self)
        self.act_high_contrast.setCheckable(True)
        self.act_high_contrast.setChecked(True)
        self.act_high_contrast.toggled.connect(self.toggle_high_contrast)
        menu_view.addAction(self.act_high_contrast)
        self.act_line_width = QAction("切割线宽度…", self)
        self.act_line_width.triggered.connect(self.choose_line_width)
        menu_view.addAction(self.act_line_width)

        self.canvas = ImageCanvas(self)
        self.canvas.filesDropped.connect(self.open_paths)
        self.canvas.selectGuide.connect(self.select_guide)
        self.canvas.guidePressed.connect(self.drag_started)
        self.canvas.guideEdited.connect(self.guide_edited)
        self.canvas.guideReleased.connect(self.drag_finished)
        self.canvas.addGuide.connect(self.add_guide)
        self.canvas.chooseSplitPoint.connect(self.set_split_point)
        self.canvas.moveSplitPoint.connect(self._update_split_point_spin)
        self.canvas.confirmSplit.connect(self.split_selected)
        self.canvas.deleteGuide.connect(self.remove_guide)
        self.canvas.nudgeGuide.connect(self.nudge_selected)
        self.canvas.mousePosition.connect(self._show_mouse_position)

        panel = QWidget()
        # 维持原本的 255～360 逻辑像素边界；仅将首次展开宽度改成最大值。
        panel.setMinimumWidth(255)
        panel.setMaximumWidth(360)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(12, 12, 12, 12)
        panel_layout.setSpacing(8)

        label = QLabel("切割线管理")
        label.setObjectName("heading")
        panel_layout.addWidget(label)

        self.guide_list = QTreeWidget()
        self.guide_list.setHeaderLabels(["方向", "坐标", "起点～终点"])
        self.guide_list.setRootIsDecorated(False)
        self.guide_list.setAlternatingRowColors(True)
        self.guide_list.setUniformRowHeights(True)
        # 前两列固定短宽度，最后一列自动分配余量，避免反复拖动横向滚动条。
        header = self.guide_list.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.guide_list.setColumnWidth(0, 76)
        self.guide_list.setColumnWidth(1, 68)
        self.guide_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.guide_list.itemSelectionChanged.connect(self._list_selected)
        panel_layout.addWidget(self.guide_list, 1)

        position_row = QHBoxLayout()
        position_row.addWidget(QLabel("像素坐标"))
        self.pos_spin = QSpinBox()
        self.pos_spin.setRange(1, 1)
        self.pos_spin.setKeyboardTracking(False)
        self.pos_spin.setToolTip("输入后按回车，切割线立即移动到该原图像素位置")
        self.pos_spin.valueChanged.connect(self._numeric_move)
        position_row.addWidget(self.pos_spin, 1)
        panel_layout.addLayout(position_row)

        # 端点与位置均为整数原图像素；可精确输入，不依赖预览缩放比例。
        extent_row = QHBoxLayout()
        extent_row.addWidget(QLabel("起点"))
        self.start_spin = QSpinBox()
        self.start_spin.setKeyboardTracking(False)
        self.start_spin.valueChanged.connect(lambda value: self._numeric_endpoint("start", value))
        extent_row.addWidget(self.start_spin)
        extent_row.addWidget(QLabel("终点"))
        self.end_spin = QSpinBox()
        self.end_spin.setKeyboardTracking(False)
        self.end_spin.valueChanged.connect(lambda value: self._numeric_endpoint("end", value))
        extent_row.addWidget(self.end_spin)
        panel_layout.addLayout(extent_row)

        pair = QHBoxLayout()
        self.button_h = QPushButton("＋ 水平线")
        self.button_v = QPushButton("＋ 垂直线")
        self.button_h.setObjectName("smallAction")
        self.button_v.setObjectName("smallAction")
        self.button_h.clicked.connect(lambda: self.add_center_guide("h"))
        self.button_v.clicked.connect(lambda: self.add_center_guide("v"))
        pair.addWidget(self.button_h)
        pair.addWidget(self.button_v)
        panel_layout.addLayout(pair)

        breakpoint_row = QHBoxLayout()
        self.button_set_split = QPushButton("设置断点")
        self.button_set_split.clicked.connect(self.set_default_split_point)
        breakpoint_row.addWidget(self.button_set_split)
        self.split_spin = QSpinBox()
        self.split_spin.setToolTip("候选分断点的原图像素坐标；可精确输入或在画布上拖动菱形")
        self.split_spin.setKeyboardTracking(False)
        self.split_spin.valueChanged.connect(self._numeric_split_point)
        breakpoint_row.addWidget(self.split_spin, 1)
        panel_layout.addLayout(breakpoint_row)

        action_pair = QHBoxLayout()
        self.button_split = QPushButton("执行分断")
        self.button_split.clicked.connect(self.split_selected)
        action_pair.addWidget(self.button_split)
        self.button_clear_split = QPushButton("取消断点")
        self.button_clear_split.clicked.connect(lambda: self.clear_split_point())
        action_pair.addWidget(self.button_clear_split)
        self.button_del = QPushButton("删除选中段")
        self.button_del.clicked.connect(self.remove_selected)
        for button in (self.button_split, self.button_clear_split, self.button_del):
            button.setObjectName("smallAction")
        action_pair.addWidget(self.button_del)
        panel_layout.addLayout(action_pair)

        self.summary = QLabel("尚未打开图片")
        self.summary.setObjectName("secondary")
        self.summary.setWordWrap(True)
        panel_layout.addWidget(self.summary)

        # 导出当前图片仍是高频操作，保留侧栏主按钮；工作流开关只在文件菜单。
        self.button_export = QPushButton("导出当前图片（PNG）")
        self.button_export.setObjectName("primary")
        self.button_export.clicked.connect(self.start_export)
        panel_layout.addWidget(self.button_export)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.canvas)
        splitter.addWidget(panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setChildrenCollapsible(False)
        self.sidebar_splitter = splitter
        self.right_panel = panel
        splitter.setSizes([1020, panel.maximumWidth()])
        # 队列固定时占据左侧栏；取消固定时不进入布局，而是覆盖画布上方。
        # 这样队列自动展开时不会改变图像缩放、偏移或主画布宽度。
        self.workspace = QWidget(self)
        self.workspace.installEventFilter(self)
        layout = QHBoxLayout(self.workspace)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.workspace_layout = layout
        # 自动隐藏时的入口是独立的 30×30 浮动按钮，而不是占满高度的左边栏。
        # 不将它添加到 QHBoxLayout，防止关闭固定后仍为画布预留整条空白区。
        self.queue_tab = QPushButton("☰", self.workspace)
        self.queue_tab.setObjectName("queueTab")
        self.queue_tab.setToolTip("将鼠标移到这里展开图片队列")
        self.queue_tab.setAccessibleName("展开图片队列")
        self.queue_tab.setFixedSize(30, 30)
        self.queue_tab.installEventFilter(self)
        self.queue_tab.clicked.connect(self._show_queue_overlay)
        self.queue_panel = QueuePanel(self.workspace)
        self.queue_panel.installEventFilter(self)
        self.queue_panel.activated.connect(self.activate_path)
        self.queue_panel.removed.connect(self.remove_queue_path)
        self.queue_panel.pinChanged.connect(self.set_queue_pinned)
        self.queue_panel.exportAll.connect(self.start_batch_export)
        self.queue_panel.filesDropped.connect(self.open_paths)
        layout.addWidget(self.queue_panel)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(self.workspace)
        self._sync_queue_visibility()
        self._position_help_button()
        self.statusBar().showMessage("拖拽图片到画布，或按 Ctrl+O 打开。")

    def showEvent(self, event) -> None:
        """Qt 首次完成主窗口布局后，把右栏展开至原先已有的 360 像素上限。"""
        super().showEvent(event)
        QTimer.singleShot(0, self._position_help_button)
        if not self._sidebar_initial_width_done:
            self._sidebar_initial_width_done = True
            QTimer.singleShot(0, self._maximize_initial_sidebar)

    def _maximize_initial_sidebar(self) -> None:
        """仅初始化一次，随后不阻止用户拖动分隔线自行收窄。"""
        splitter = self.sidebar_splitter
        if splitter.width() > self.right_panel.maximumWidth():
            splitter.setSizes([splitter.width() - self.right_panel.maximumWidth(),
                               self.right_panel.maximumWidth()])

    def _position_help_button(self) -> None:
        """将帮助按钮始终放在菜单栏内部右上角，避免被高 DPI 或窗口边界裁切。"""
        bar = self.menuBar()
        if bar is None or not hasattr(self, "help_button"):
            return
        x = max(0, bar.width() - self.help_button.width() - 8)
        y = max(0, (bar.height() - self.help_button.height()) // 2)
        self.help_button.move(x, y)
        self.help_button.raise_()

    def show_user_manual(self) -> None:
        """从随 EXE 一起打包的使用说明读取文本，帮助对话框可以独立滚动。"""
        resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
        manual_path = resource_root / "assets" / "软件使用说明.md"
        try:
            content = manual_path.read_text(encoding="utf-8-sig")
        except OSError:
            QMessageBox.warning(self, "使用说明缺失", f"无法读取说明文档：\n{manual_path}")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("QuickSlice · 完整使用说明")
        # 使用可用桌面空间限制弹窗尺寸：默认更宽，且不超过当前显示器边界。
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        width = min(1000, max(520, available.width() - 100)) if available else 1000
        height = min(740, max(380, available.height() - 100)) if available else 740
        dialog.resize(width, height)
        dialog.setMinimumSize(min(680, width), min(420, height))
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        browser = QTextBrowser(dialog)
        browser.setObjectName("manualBrowser")
        browser.setOpenExternalLinks(False)
        browser.setMarkdown(content)
        layout.addWidget(browser, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dialog)
        # Qt 在部分系统语言下显示英文 Close，这里固定为简体中文“关闭”。
        close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_button is not None:
            close_button.setText("关闭")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def eventFilter(self, watched, event) -> bool:
        """自动隐藏队列使用悬浮覆盖层，不让队列弹出使画布瞬间位移。"""
        if watched is getattr(self, "workspace", None) and event.type() == QEvent.Type.Resize:
            self._position_queue_overlay()
        elif watched is self.menuBar() and event.type() == QEvent.Type.Resize:
            self._position_help_button()
        elif watched is getattr(self, "queue_tab", None) and event.type() == QEvent.Type.Enter:
            self._show_queue_overlay()
        elif watched in (getattr(self, "queue_tab", None), getattr(self, "queue_panel", None)):
            if event.type() == QEvent.Type.Leave and not self._queue_pinned:
                QTimer.singleShot(230, self._hide_queue_if_unhovered)
        return super().eventFilter(watched, event)

    def _position_queue_overlay(self) -> None:
        """重新布置浮动面板，不修改图像原图坐标或当前缩放参数。"""
        if not self._queue_pinned:
            # 左上角只放一个小展开按钮，不保留纵向空白条；队列仅临时覆盖小块画布。
            self.queue_tab.move(8, 10)
            available_width = max(0, self.workspace.width() - 16)
            available_height = max(0, self.workspace.height() - 54)
            if available_width < 180 or available_height < 120:
                return
            self.queue_panel.setGeometry(8, 44, min(248, available_width),
                                         min(640, available_height))
            if self._queue_overlay_open:
                self.queue_panel.raise_()
                self.queue_tab.raise_()

    def _show_queue_overlay(self) -> None:
        if len(self._documents) > 1 and not self._queue_pinned:
            self._queue_overlay_open = True
            self._position_queue_overlay()
            self.queue_panel.show()
            self.queue_panel.raise_()

    def _hide_queue_if_unhovered(self) -> None:
        # 使用屏幕坐标判断鼠标是否还在面板/窄按钮内，包含按钮和列表子控件。
        # 避免鼠标进入 QTreeWidget 子控件时 QWidget.underMouse 状态差异造成误隐藏。
        pointer = QCursor.pos()
        in_panel = (self.queue_panel.isVisible() and
                    self.queue_panel.rect().contains(self.queue_panel.mapFromGlobal(pointer)))
        in_tab = (self.queue_tab.isVisible() and
                  self.queue_tab.rect().contains(self.queue_tab.mapFromGlobal(pointer)))
        if not self._queue_pinned and not in_panel and not in_tab:
            self.queue_panel.hide()
            self._queue_overlay_open = False

    def set_queue_pinned(self, pinned: bool) -> None:
        """保持同一个队列控件，在布局侧栏与画布覆盖层之间切换。"""
        pinned = bool(pinned)
        if self._queue_pinned == pinned:
            return
        self.queue_panel.hide()
        if pinned:
            self.workspace_layout.insertWidget(0, self.queue_panel)
        else:
            self.workspace_layout.removeWidget(self.queue_panel)
            self.queue_panel.setParent(self.workspace)
        self._queue_pinned = pinned
        self._queue_overlay_open = False
        self.queue_panel.set_pinned(pinned)
        self._sync_queue_visibility()
        self.workspace_layout.invalidate()
        self._position_queue_overlay()

    def _sync_queue_visibility(self) -> None:
        """只有真正形成多图队列时显示侧栏，单图不显示队列，画布使用全部可用宽度。"""
        show = len(self._documents) > 1
        self.queue_tab.setVisible(show and not self._queue_pinned)
        self.queue_panel.setVisible(show and (self._queue_pinned or self._queue_overlay_open))
        if show and not self._queue_pinned:
            self._position_queue_overlay()
            self.queue_tab.raise_()
        self.queue_panel.update_items(self._documents, self.input_path)

    def _save_active_document(self) -> None:
        """切换图片前保存各自的选择、撤销栈及画布视图状态。"""
        doc = self._active_document
        if doc is not None:
            doc.selected = self._selected
            doc.undo = self._undo
            doc.redo = self._redo
            if self.canvas.has_image():
                doc.scale = self.canvas.scale
                doc.offset = (self.canvas.offset.x(), self.canvas.offset.y())

    def _cache_preview(self, path: str, pixmap: QPixmap) -> None:
        """限制队列预览总内存，避免一次拖入大量 4K 图片导致内存持续增长。"""
        if path in self._preview_cache:
            self._preview_bytes -= self._preview_cache.pop(path)[1]
        size = pixmap.width() * pixmap.height() * 4
        self._preview_cache[path] = (pixmap, size)
        self._preview_bytes += size
        while self._preview_bytes > self._preview_limit and len(self._preview_cache) > 1:
            key, (_, count) = self._preview_cache.popitem(last=False)
            self._preview_bytes -= count

    def _activate_loaded(self, doc: QueueDocument, pixmap: QPixmap,
                         width: int, height: int) -> None:
        """只有完整预览准备成功才切换画布；旧图在读取失败时原样保留。"""
        self._save_active_document()
        if doc.model is None or (doc.model.width, doc.model.height) != (width, height):
            doc.model = SliceModel()
            doc.model.reset(width, height)
            doc.selected, doc.undo, doc.redo = None, [], []
        self._active_document = doc
        self.model = doc.model
        self.input_path = doc.path
        self._selected = doc.selected
        self._undo = doc.undo
        self._redo = doc.redo
        self._before_drag = None
        self.canvas.set_image(self.model, pixmap)
        if doc.scale is not None and doc.offset is not None:
            self.canvas.scale = doc.scale
            self.canvas.offset = QPointF(*doc.offset)
            self.canvas.update()
        self.canvas.set_split_point(None, None)
        self._refresh()
        self._sync_queue_visibility()
        self.setWindowTitle(f"QuickSlice  |  {Path(doc.path).name}  |  v{APP_VERSION}")

    def remove_queue_path(self, path: str) -> None:
        """从队列移除只移除内存记录，不删除用户的原始图片文件。"""
        if self._export_task is not None:
            return
        doc = next((item for item in self._documents if item.path == path), None)
        if doc is None:
            return
        self._documents.remove(doc)
        if path in self._preview_cache:
            self._preview_bytes -= self._preview_cache.pop(path)[1]
        if self._load_target is doc:
            self._load_id += 1
            self._load_target = None
        if doc is self._active_document:
            self._active_document = None
            self.input_path = None
            self.model = SliceModel()
            self.canvas.model = None
            self.canvas.pixmap = QPixmap()
            self.canvas.set_split_point(None, None)
            self._selected, self._undo, self._redo = None, [], []
            if self._documents:
                self.activate_path(self._documents[0].path)
        self._sync_queue_visibility()
        self._refresh()

    def _load_settings(self) -> None:
        """使用软件目录下的 INI，保持绿色便携，不写入系统配置目录。"""
        config_file = application_folder() / "settings.ini"
        data = configparser.ConfigParser()
        try:
            data.read(config_file, encoding="utf-8")
            self.last_folder = data.get("folders", "open", fallback=self.last_folder)
            self.last_export_folder = data.get("folders", "export", fallback=self.last_export_folder)
            self.resize(data.getint("window", "width", fallback=self.width()),
                        data.getint("window", "height", fallback=self.height()))
            color = data.get("guides", "color", fallback="#00d8f7")
            contrast = data.getboolean("guides", "high_contrast", fallback=True)
            width = data.getint("guides", "width", fallback=2)
            self.canvas.set_line_style(color, contrast, width)
            self.act_high_contrast.setChecked(contrast)
            self.set_queue_pinned(data.getboolean("queue", "pinned", fallback=True))
            self.act_auto_next.setChecked(data.getboolean("export", "auto_next", fallback=False))
        except (OSError, ValueError, configparser.Error):
            self.statusBar().showMessage("配置文件无法读取，已使用默认设置。", 5000)

    def _save_settings(self) -> None:
        """仅在关闭窗口时落盘，避免频繁写文件影响交互。"""
        data = configparser.ConfigParser()
        data["folders"] = {"open": self.last_folder, "export": self.last_export_folder}
        data["window"] = {"width": str(self.width()), "height": str(self.height())}
        data["guides"] = {
            "color": self.canvas.line_color.name(),
            "high_contrast": str(self.canvas.high_contrast).lower(),
            "width": str(self.canvas.line_width),
        }
        data["queue"] = {"pinned": str(self._queue_pinned).lower()}
        data["export"] = {"auto_next": str(self.act_auto_next.isChecked()).lower()}
        path = application_folder() / "settings.ini"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="\r\n") as file:
                data.write(file)
        except OSError:
            self.statusBar().showMessage("当前目录无法写入设置，将使用默认设置。")

    def choose_line_color(self) -> None:
        """弹出系统颜色选取器；取消时保持原颜色。"""
        color = QColorDialog.getColor(self.canvas.line_color, self, "选择切割线颜色")
        if color.isValid():
            self.canvas.set_line_style(color.name(), self.canvas.high_contrast, self.canvas.line_width)

    def toggle_high_contrast(self, enabled: bool) -> None:
        """黑色底描边让亮色切割线在纯白图片上仍然清楚。"""
        self.canvas.set_line_style(self.canvas.line_color.name(), enabled, self.canvas.line_width)

    def choose_line_width(self) -> None:
        """线条粗细只影响屏幕像素，缩放不会使线条变得过粗。"""
        width, accepted = QInputDialog.getInt(self, "切割线宽度", "屏幕像素（1～5）：",
                                               self.canvas.line_width, 1, 5)
        if accepted:
            self.canvas.set_line_style(self.canvas.line_color.name(), self.canvas.high_contrast, width)

    def _refresh(self) -> None:
        """编辑提交后更新区域数量；鼠标逐帧拖动过程中只轻量更新控件。"""
        can_edit = self.canvas.has_image() and self._export_task is None
        has_selected = can_edit and self.model.get(self._selected) is not None
        breakable = (has_selected and
                     self.model.get(self._selected).end - self.model.get(self._selected).start > 1)
        split_ready = (self.canvas.split_point is not None and
                       self.canvas.split_point[0] == self._selected)
        for action in (self.act_h, self.act_v, self.act_fit, self.act_original):
            action.setEnabled(can_edit)
        self.act_export.setEnabled(can_edit)
        self.act_open.setEnabled(self._export_task is None)
        self.act_remove.setEnabled(has_selected)
        self.act_undo.setEnabled(can_edit and bool(self._undo))
        self.act_redo.setEnabled(can_edit and bool(self._redo))
        for button in (self.button_h, self.button_v):
            button.setEnabled(can_edit)
        self.button_del.setEnabled(has_selected)
        self.button_set_split.setEnabled(bool(breakable))
        self.button_split.setEnabled(bool(can_edit and split_ready))
        self.button_clear_split.setEnabled(bool(split_ready))
        self.split_spin.setEnabled(bool(can_edit and split_ready))
        self.button_export.setEnabled(can_edit)
        for spin in (self.pos_spin, self.start_spin, self.end_spin):
            spin.setEnabled(has_selected)
        self._refresh_list()
        self._sync_queue_visibility()
        if self.canvas.has_image():
            try:
                regions = self.model.tiles()
                count_text = f"预计输出 {len(regions)} 张 PNG（非矩形 {sum(bool(t.parts) for t in regions)} 张）"
            except ValueError as exc:
                count_text = f"区域过于复杂：{exc}"
            # 尺寸与缩放已在画布状态栏显示，右侧仅保留切割结果统计。
            self.summary.setText(
                f"水平 {sum(g.axis == 'h' for g in self.model.guides)} 段 / "
                f"垂直 {sum(g.axis == 'v' for g in self.model.guides)} 段\n"
                f"{count_text}"
            )
        else:
            self.summary.setText("尚未打开图片")
        self.canvas.selected = self._selected
        self.canvas.update()

    def _refresh_list(self) -> None:
        """列表始终按方向与坐标排序；用唯一标识保持选择。"""
        self._sidebar_updating = True
        try:
            self.guide_list.clear()
            for guide in sorted(self.model.guides, key=lambda guide: (guide.axis, guide.position)):
                item = QTreeWidgetItem(["水平 ↔" if guide.axis == "h" else "垂直 ↕",
                                        str(guide.position), f"{guide.start}～{guide.end}"])
                item.setData(0, Qt.ItemDataRole.UserRole, guide.identifier)
                self.guide_list.addTopLevelItem(item)
                if guide.identifier == self._selected:
                    item.setSelected(True)
            guide = self.model.get(self._selected)
            if guide is not None:
                for spin, low, high, value in (
                    (self.pos_spin, 1, self.model.limit(guide.axis) - 1, guide.position),
                    (self.start_spin, 0, guide.end - 1, guide.start),
                    (self.end_spin, guide.start + 1, self.model.span_limit(guide.axis), guide.end),
                ):
                    with QSignalBlocker(spin):
                        spin.setRange(low, high)
                        spin.setValue(value)
                if self.canvas.split_point is not None and self.canvas.split_point[0] == guide.identifier:
                    with QSignalBlocker(self.split_spin):
                        self.split_spin.setRange(guide.start + 1, guide.end - 1)
                        self.split_spin.setValue(self.canvas.split_point[1])
        finally:
            self._sidebar_updating = False

    def _show_mouse_position(self, x: int, y: int) -> None:
        """显示原图像素位置，不使用已经压缩的预览尺寸。"""
        if self.canvas.has_image() and 0 <= x < self.model.width and 0 <= y < self.model.height:
            self.statusBar().showMessage(f"鼠标原图坐标：X={x}，Y={y}    |    中键/空格/Ctrl＋左键平移    |    滚轮缩放")

    def open_dialog(self) -> None:
        """文件选择器支持多选；加入队列但只解码用户当前需要的图片。"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择一张或多张图片", self.last_folder,
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff);;所有文件 (*)",
        )
        if paths:
            self.open_paths(paths)

    def open_path(self, path: str) -> None:
        """保留命令行单文件启动接口；内部统一使用图片队列。"""
        self.open_paths([path])

    def open_paths(self, paths: list[str]) -> None:
        """一次加入全部有效图片，避免拖入多文件时只打开第一张。"""
        if self._export_task is not None:
            QMessageBox.information(self, "正在导出", "请先完成或取消当前导出任务。")
            return
        first: str | None = None
        present = {os.path.normcase(doc.path) for doc in self._documents}
        invalid = 0
        for raw in paths:
            path = os.path.abspath(raw)
            if not os.path.isfile(path) or Path(path).suffix.lower() not in VALID_EXTENSIONS:
                invalid += 1
                continue
            if first is None:
                first = path
            if os.path.normcase(path) not in present:
                self._documents.append(QueueDocument(path))
                present.add(os.path.normcase(path))
        if first is None:
            QMessageBox.warning(self, "无法打开", "没有有效图片；支持 PNG、JPG、WebP、BMP、TIFF。")
            return
        self._sync_queue_visibility()
        if invalid:
            self.statusBar().showMessage(f"已跳过 {invalid} 个不支持的文件。", 6000)
        self.activate_path(first)

    def activate_path(self, path: str) -> None:
        """切换队列项时优先复用受内存限制的预览，其余交给后台解码。"""
        if self._export_task is not None:
            return
        doc = next((item for item in self._documents if item.path == path), None)
        if doc is None:
            return
        if self._active_document is doc and self._load_target is None:
            return
        self._load_target = doc
        self._load_id += 1
        identifier = self._load_id
        self.last_folder = str(Path(path).parent)
        cached = self._preview_cache.get(path)
        if cached is not None and doc.model is not None:
            self._preview_cache.move_to_end(path)
            self._load_target = None
            self._activate_loaded(doc, cached[0], doc.model.width, doc.model.height)
            return
        task = LoadTask(identifier, path)
        self._pending_load[identifier] = task
        task.signals.ready.connect(self._loaded)
        task.signals.failed.connect(self._load_failed)
        self.statusBar().showMessage(f"正在后台解码预览：{Path(path).name}")
        self._pool.start(task)

    def _loaded(self, identifier: int, info: tuple) -> None:
        """只接受最新点击的队列项，避免慢速大图覆盖用户随后切换的图片。"""
        self._pending_load.pop(identifier, None)
        if identifier != self._load_id:
            return
        path, width, height, pw, ph, pixels = info
        try:
            # 从原始 RGBA 字节创建 QImage 后立即复制，避免后台缓冲提前释放。
            image = QImage(pixels, pw, ph, pw * 4, QImage.Format.Format_RGBA8888).copy()
            pixmap = QPixmap.fromImage(image)
            if pixmap.isNull():
                raise ValueError("无法构建 Qt 预览位图，可能超出图形设备支持的尺寸。")
            doc = next((item for item in self._documents if item.path == path), None)
            if doc is None:
                return
            self._cache_preview(path, pixmap)
            self._load_target = None
            self._activate_loaded(doc, pixmap, width, height)
            self.statusBar().showMessage(f"已打开：{Path(path).name}（{width} × {height}）", 7000)
        except Exception as exc:
            QMessageBox.critical(self, "预览创建失败", str(exc))

    def _load_failed(self, identifier: int, message: str) -> None:
        self._pending_load.pop(identifier, None)
        if identifier == self._load_id:
            self._load_target = None
            QMessageBox.warning(self, "打开图片失败", message)
            self.statusBar().showMessage("打开失败；旧图片和切割线保持不变。", 7000)

    def _push_undo(self) -> None:
        """每次独立编辑之前保存一个历史状态；最多保留 100 步。"""
        self._undo.append(self.model.snapshot())
        if len(self._undo) > 100:
            del self._undo[0]
        self._redo.clear()

    def undo(self) -> None:
        if not self._undo or self._export_task is not None:
            return
        self._redo.append(self.model.snapshot())
        self.model.restore(self._undo.pop())
        if self.model.get(self._selected) is None:
            self._selected = None
        self.clear_split_point(refresh=False)
        self._refresh()

    def redo(self) -> None:
        if not self._redo or self._export_task is not None:
            return
        self._undo.append(self.model.snapshot())
        self.model.restore(self._redo.pop())
        if self.model.get(self._selected) is None:
            self._selected = None
        self.clear_split_point(refresh=False)
        self._refresh()

    def add_center_guide(self, axis: str) -> None:
        """工具栏添加在视图当前中心，而不是总是图片中心。"""
        if not self.canvas.has_image():
            return
        from PySide6.QtCore import QPointF
        center = self.canvas.map_to_image(QPointF(self.canvas.width() / 2, self.canvas.height() / 2))
        length = self.model.limit(axis)
        wanted = round(center.y() if axis == "h" else center.x())
        position = max(1, min(length - 1, wanted))
        if any(g.axis == axis and g.position == position for g in self.model.guides):
            # 重复添加时尝试下一个空像素，而不是报错中断交互。
            occupied = {g.position for g in self.model.guides if g.axis == axis}
            position = next((pos for delta in range(1, length) for pos in (wanted + delta, wanted - delta)
                             if 0 < pos < length and pos not in occupied), position)
        self.add_guide(axis, position)

    def add_guide(self, axis: str, position: int) -> None:
        if not self.canvas.has_image() or self._export_task is not None:
            return
        try:
            if len(self.model.guides) >= 5000:
                raise ValueError("切割线已达到 5000 条的保护上限。")
            self._push_undo()
            guide = self.model.add(axis, position)
        except ValueError as exc:
            if self._undo and self._undo[-1] == self.model.snapshot():
                self._undo.pop()
            self.statusBar().showMessage(str(exc), 5000)
            return
        self._selected = guide.identifier
        self.clear_split_point(refresh=False)
        self._refresh()

    def set_split_point(self, identifier: int, coordinate: int) -> None:
        """第一步只定位可见断点，绝不立即修改线段和拓扑。"""
        guide = self.model.get(identifier)
        if guide is None or self._export_task is not None:
            return
        try:
            position, _ = self.model.split_candidate(identifier, coordinate)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 5000)
            return
        self._selected = identifier
        self.canvas.set_split_point(identifier, position)
        self._refresh()
        self.statusBar().showMessage("已放置候选断点。可拖动橙色菱形或输入精确坐标，再执行分断。")

    def set_default_split_point(self) -> None:
        """侧栏设置断点优先取最接近线段中点的交叉点，缺省才取中点。"""
        guide = self.model.get(self._selected)
        if guide is None or guide.end - guide.start < 2:
            return
        middle = (guide.start + guide.end) / 2
        crossings = self.model.split_intersections(guide.identifier)
        position = min(crossings, key=lambda x: abs(x-middle)) if crossings else round(middle)
        self.set_split_point(guide.identifier, position)

    def _numeric_split_point(self, coordinate: int) -> None:
        """数字框用于精确定位，用户显式输入时不强制吸附交叉点。"""
        marker = self.canvas.split_point
        if marker is not None:
            self.canvas.set_split_point(marker[0], coordinate)

    def _update_split_point_spin(self, identifier: int, coordinate: int) -> None:
        """拖动菱形标记时只更新一个数字框，不做列表或区域重计算。"""
        if self.canvas.split_point and identifier == self.canvas.split_point[0]:
            with QSignalBlocker(self.split_spin):
                self.split_spin.setValue(coordinate)

    def clear_split_point(self, refresh: bool = True) -> None:
        self.canvas.set_split_point(None, None)
        if refresh:
            self._refresh()

    def _split_guide(self, identifier: int, coordinate: int) -> None:
        """第二步确认分断：原段缩短，新段生成；两段相接不制造透明缝。"""
        if self._export_task is not None:
            return
        before = self.model.snapshot()
        try:
            new_guide = self.model.split(identifier, coordinate)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 5000)
            return
        self._undo.append(before)
        self._undo[:] = self._undo[-100:]
        self._redo.clear()
        self._selected = new_guide.identifier
        self.clear_split_point(refresh=False)
        self._refresh()

    def split_selected(self) -> None:
        """只有候选菱形断点存在时，按钮和回车才执行真正的分断。"""
        marker = self.canvas.split_point
        if marker is not None and marker[0] == self._selected:
            self._split_guide(*marker)

    def remove_guide(self, identifier: int) -> None:
        if self._export_task is not None or self.model.get(identifier) is None:
            return
        self._push_undo()
        self.model.remove(identifier)
        self.clear_split_point(refresh=False)
        if self._selected == identifier:
            self._selected = None
        self._refresh()

    def remove_selected(self) -> None:
        if self._selected is not None:
            self.remove_guide(self._selected)

    def select_guide(self, identifier: int | None) -> None:
        if self.canvas.split_point is not None and self.canvas.split_point[0] != identifier:
            self.clear_split_point(refresh=False)
        self._selected = identifier
        self._refresh()

    def _list_selected(self) -> None:
        if self._sidebar_updating:
            return
        items = self.guide_list.selectedItems()
        self.select_guide(int(items[0].data(0, Qt.ItemDataRole.UserRole)) if items else None)

    def drag_started(self, identifier: int) -> None:
        """记住鼠标按下时的状态，移动全程不重复创建撤销点。"""
        self._before_drag = self.model.snapshot()
        self._selected = identifier

    def guide_edited(self, identifier: int) -> None:
        """只刷新受影响的一行和三个数字框，不触发耗时区域划分。"""
        guide = self.model.get(identifier)
        if guide is None:
            return
        self._selected = identifier
        for spin, value in ((self.pos_spin, guide.position),
                            (self.start_spin, guide.start), (self.end_spin, guide.end)):
            with QSignalBlocker(spin):
                spin.setValue(value)
        for idx in range(self.guide_list.topLevelItemCount()):
            item = self.guide_list.topLevelItem(idx)
            if int(item.data(0, Qt.ItemDataRole.UserRole)) == identifier:
                item.setText(1, str(guide.position))
                item.setText(2, f"{guide.start}～{guide.end}")
                break

    def drag_finished(self, identifier: int) -> None:
        """移动确实发生时才登记撤销历史。"""
        before = self._before_drag
        self._before_drag = None
        if before is not None and before != self.model.snapshot():
            self._undo.append(before)
            if len(self._undo) > 100:
                self._undo.pop(0)
            self._redo.clear()
        marker = self.canvas.split_point
        if marker is not None:
            line = self.model.get(marker[0])
            if line is None or not line.start < marker[1] < line.end:
                self.clear_split_point(refresh=False)
        self._refresh()

    def _numeric_move(self, value: int) -> None:
        """坐标框提交新数字后立即更新同一条切割线。"""
        if self._sidebar_updating or self._selected is None or self._export_task is not None:
            return
        before = self.model.snapshot()
        if self.model.move(self._selected, value):
            self._undo.append(before)
            self._undo[:] = self._undo[-100:]
            self._redo.clear()
        self._refresh()

    def _numeric_endpoint(self, endpoint: str, coordinate: int) -> None:
        """端点精确输入；只有修改成功才记一次撤销。"""
        if self._sidebar_updating or self._selected is None or self._export_task is not None:
            return
        before = self.model.snapshot()
        if self.model.resize(self._selected, endpoint, coordinate):
            self._undo.append(before)
            self._undo[:] = self._undo[-100:]
            self._redo.clear()
        marker = self.canvas.split_point
        line = self.model.get(self._selected)
        if marker is not None and marker[0] == self._selected and line is not None:
            if not line.start < marker[1] < line.end:
                self.clear_split_point(refresh=False)
        self._refresh()

    def nudge_selected(self, amount: int) -> None:
        """键盘微调始终以原图像素为单位，完全不受预览缩放影响。"""
        guide = self.model.get(self._selected)
        if guide is not None:
            self._numeric_move(guide.position + amount)

    def canvas_fit(self) -> None:
        self.canvas.fit_image()

    def canvas_actual(self) -> None:
        self.canvas.actual_size()

    def start_export(self) -> None:
        """按当前模式导出图片；自动模式直接使用原图所在目录作为父目录。

        单图和队列中的当前图都通过同一个保存按钮操作；仅在导出成功后
        打开队列下一张，已经处理过的图片不会被自动删除或关闭。
        """
        if self.input_path is None or self._export_task is not None:
            return
        try:
            tiles = self.model.tiles()
        except ValueError as exc:
            QMessageBox.warning(self, "切割区域无效", str(exc))
            return
        if len(tiles) > 10000:
            QMessageBox.warning(self, "切块数量过多", "当前共有超过 10000 个切块。请减少切割线后再导出。")
            return
        auto_next = self.act_auto_next.isChecked()
        if auto_next:
            parent = str(Path(self.input_path).parent)
            folder_suffix = source_folder_suffix(self.input_path)
        else:
            parent = QFileDialog.getExistingDirectory(
                self, "选择切图存放位置（自动创建同名子目录）", self.last_export_folder)
            if not parent:
                return
            folder_suffix = ""
        try:
            output = str(export_subfolder(parent, self.input_path, folder_suffix))
        except ValueError as exc:
            QMessageBox.warning(self, "输出路径过长", str(exc))
            return
        next_path = (next_queue_path([doc.path for doc in self._documents], self.input_path)
                     if auto_next else None)
        self._run_export_jobs([(self.input_path, output, tiles)], parent, batch=False,
                              auto_next=auto_next, next_path=next_path)

    def _confirm_batch_export_scope(self) -> list[QueueDocument] | None:
        """先确定要保存哪些图片，避免“拒绝整图导出”直接丢弃已经编辑的工作。"""
        edited = [doc for doc in self._documents if doc.model is not None and doc.model.guides]
        plain_count = len(self._documents) - len(edited)
        if plain_count == 0:
            return list(self._documents)

        # 第一层问题保持原本的语义：“是”包括未编辑图片，“否”不包括整张原图。
        answer = QMessageBox.question(
            self, "队列中存在未编辑图片",
            f"有 {plain_count} 张图片没有切割线，是否也将它们按完整图片导出为 PNG？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            return list(self._documents)

        # 第二层仅询问一次。拒绝第一层不代表拒绝导出已经编辑过的图片。
        if not edited:
            QMessageBox.information(self, "没有可单独导出的已编辑图片",
                                    "队列内没有设置切割线的图片，本次导出已取消。")
            return None
        edited_answer = QMessageBox.question(
            self, "仅导出已编辑图片？",
            f"已有 {len(edited)} 张图片设置了切割线。\n"
            f"是否仅导出这 {len(edited)} 张，跳过 {plain_count} 张未编辑图片？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        return edited if edited_answer == QMessageBox.StandardButton.Yes else None

    def start_batch_export(self) -> None:
        """先确认批量范围，再选择父目录；只对确实要导出的图解码和检查。"""
        if self._export_task is not None or not self._documents:
            return
        self._save_active_document()
        documents = self._confirm_batch_export_scope()
        if not documents:
            return
        parent = QFileDialog.getExistingDirectory(self, "选择队列切图的目标父目录",
                                                  self.last_export_folder)
        if not parent:
            return
        jobs = []
        reserved: set[str] = set()
        for doc in documents:
            if doc.model is None:
                try:
                    width, height, _, frames = read_image_info(doc.path)
                    if frames != 1:
                        raise ValueError("动画图片暂不支持直接切图")
                    doc.model = SliceModel()
                    doc.model.reset(width, height)
                except Exception as exc:
                    QMessageBox.warning(self, "队列无法导出", f"{Path(doc.path).name}：{exc}")
                    return
            try:
                tiles = doc.model.tiles()
            except ValueError as exc:
                QMessageBox.warning(self, "队列切割区域无效", f"{Path(doc.path).name}：{exc}")
                return
            # 未编辑图片只在用户明确选“全部导出”时进入工作列表。
            try:
                output = export_subfolder(parent, doc.path)
            except ValueError as exc:
                QMessageBox.warning(self, "输出路径过长", f"{Path(doc.path).name}：{exc}")
                return
            # 不同路径的同名图片不能导出到同一目录，避免无提示相互覆盖。
            serial = 2
            while os.path.normcase(str(output)) in reserved:
                try:
                    output = export_subfolder(parent, doc.path, f"_{serial}")
                except ValueError as exc:
                    QMessageBox.warning(self, "输出路径过长", f"{Path(doc.path).name}：{exc}")
                    return
                serial += 1
            reserved.add(os.path.normcase(str(output)))
            jobs.append((doc.path, str(output), tiles))
        self._run_export_jobs(jobs, parent, batch=True)

    @staticmethod
    def _existing_outputs(jobs: list[tuple]) -> int:
        """仅检查本轮实际准备写出的文件，避免对目录中其他 PNG 误报。"""
        collisions = 0
        for path, folder, tiles in jobs:
            rows = max(2, len(str(max(tile.row for tile in tiles))))
            cols = max(2, len(str(max(tile.column for tile in tiles))))
            for tile in tiles:
                file = tile_output_path(path, folder, tile, rows, cols)
                collisions += file.exists()
        return collisions

    def _run_export_jobs(self, jobs: list[tuple], parent: str, batch: bool,
                         auto_next: bool = False, next_path: str | None = None) -> None:
        """共用导出启动流程；后台任务接收不可变切块坐标快照。"""
        total = sum(len(tiles) for _, _, tiles in jobs)
        if total > 10000:
            QMessageBox.warning(self, "切块过多", "本次总计超过 10000 个切块，请减少切割线。")
            return
        try:
            collisions = self._existing_outputs(jobs)
        except ValueError as exc:
            QMessageBox.warning(self, "输出路径过长", str(exc))
            return
        if collisions:
            answer = QMessageBox.question(
                self, "同名切块已经存在",
                f"目标文件夹内有 {collisions} 张同名切块文件。\n继续将覆盖这些文件，是否确认？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        if not auto_next:
            self.last_export_folder = parent
        self._export_actual_folder = parent if batch else jobs[0][1]
        self._auto_export = auto_next
        self._auto_advance_to = next_path if auto_next else None
        self._export_expected_files = total
        self._progress = QProgressDialog("正在无损导出 PNG…", "取消", 0, total, self)
        self._progress.setWindowTitle("导出进度")
        self._progress.setWindowModality(Qt.WindowModality.NonModal)
        self._progress.setMinimumDuration(0)
        self._progress.setValue(0)
        task = BatchExportTask(jobs) if batch else ExportTask(*jobs[0])
        self._export_task = task
        self._progress.canceled.connect(task.cancel)
        task.signals.progress.connect(self._export_progress)
        task.signals.finished.connect(self._export_finished)
        task.signals.failed.connect(self._export_failed)
        self._refresh()
        self._pool.start(task)

    def _export_progress(self, current: int, total: int) -> None:
        """工作线程报告已完成文件数，不在主线程调用 Pillow。"""
        if self._progress is not None:
            self._progress.setLabelText(f"正在无损导出 PNG：{current} / {total}")
            self._progress.setValue(current)

    def _finish_export_ui(self) -> None:
        """所有完成和失败路径共享同一套状态复位，避免导出按钮被锁死。"""
        if self._progress is not None:
            self._progress.close()
            self._progress.deleteLater()
            self._progress = None
        self._export_task = None
        self._refresh()

    def _export_finished(self, result: tuple) -> None:
        """仅在完整保存成功时切换下一张，导出不完整或取消均保持当前画布。"""
        filenames, canceled = result
        auto = self._auto_export
        next_path = self._auto_advance_to
        complete = not canceled and len(filenames) == self._export_expected_files
        self._auto_export = False
        self._auto_advance_to = None
        self._export_expected_files = 0
        self._finish_export_ui()
        if not complete:
            self.statusBar().showMessage(
                f"导出已取消或未完成，已保留 {len(filenames)} 个切块；当前图片不切换。", 12000)
            return
        if auto:
            if next_path:
                self.statusBar().showMessage(f"已保存 {len(filenames)} 个 PNG，正在打开下一张图片。", 12000)
                self.activate_path(next_path)
            else:
                self.statusBar().showMessage(
                    f"已保存 {len(filenames)} 个 PNG；队列已到末尾，当前图片保持待机。", 12000)
        else:
            QMessageBox.information(
                self, "导出完成", f"已导出 {len(filenames)} 张原像素 PNG。\n\n输出位置：\n{self._export_actual_folder}")
            self.statusBar().showMessage(f"导出完成：{len(filenames)} 张 PNG", 12000)

    def _export_failed(self, error: str) -> None:
        self._auto_export = False
        self._auto_advance_to = None
        self._export_expected_files = 0
        self._finish_export_ui()
        QMessageBox.critical(self, "导出失败", f"{error}\n\n已完成的文件将保留，未写完的临时文件已清理。")

    def closeEvent(self, event) -> None:
        """导出中关闭可能留下半套文件，先询问并等待取消确认。"""
        if self._export_task is not None:
            QMessageBox.information(self, "导出未结束", "请先在进度窗口中取消导出或完成当前任务。")
            event.ignore()
            return
        self._save_settings()
        event.accept()


def main() -> int:
    """启动 Qt，Windows 文件关联/拖入中文路径由 Qt 统一处理。"""
    app = QApplication(sys.argv)
    app.setApplicationName("QuickSlice")
    app.setOrganizationName("QuickSlicePortable")
    # --add-data 打包后图标位于应用资源目录，源码运行时位于 source/assets。
    resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    icon = resource_root / "assets" / "quickslice.ico"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    window = MainWindow()
    window.show()
    if len(sys.argv) > 1:
        # Windows 文件关联或命令行可一次传入多个路径，Qt 窗口就绪后全部入队。
        QTimer.singleShot(0, lambda: window.open_paths(sys.argv[1:]))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
