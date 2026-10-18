"""界面变更的结构检查；不要求 Linux 测试机实际安装 PySide6。

测试通过解析 Python 语法树检查重复入口是否清理。这里不模拟 Qt
事件循环，因此按钮尺寸、DPI 布局和实际鼠标操作仍以 Windows 实机验收为准。
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parent.parent
MAIN = SOURCE / "src" / "main.py"
QUEUE = SOURCE / "src" / "queue_panel.py"
MANUAL = SOURCE / "assets" / "软件使用说明.md"


class UiStructureTests(unittest.TestCase):
    """确认界面删减没有误删除对应的菜单入口。"""

    def test_toolbar_only_has_fit_and_actual(self):
        """工具栏只接入两项缩放动作，打开/导出/加线仍由菜单或右栏提供。"""
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        sources = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "addAction" and isinstance(node.func.value, ast.Name):
                    if node.func.value.id == "toolbar":
                        sources.append(ast.unparse(node.args[0]))
        self.assertEqual(sources, ["action"])
        text = MAIN.read_text(encoding="utf-8")
        self.assertIn("for action in (self.act_fit, self.act_original)", text)
        self.assertIn("menu_file.addAction(self.act_auto_next)", text)
        self.assertIn("menu_edit.addAction(action)", text)
        self.assertNotIn("toolbar.addAction(self.act_open)", text)
        self.assertNotIn("toolbar.addAction(self.act_export)", text)

    def test_help_is_top_right_and_uses_bundled_document(self):
        """完整帮助内容来自源文件，编译后仍通过打包资源路径访问。"""
        text = MAIN.read_text(encoding="utf-8")
        self.assertIn('self.help_button = QPushButton("?", self.menuBar())', text)
        self.assertIn('self.help_button.move(x, y)', text)
        self.assertIn('bar.width() - self.help_button.width() - 8', text)
        self.assertNotIn('setCornerWidget(self.help_button', text)
        self.assertIn('close_button.setText("关闭")', text)
        self.assertIn('min(1000, max(520, available.width() - 100))', text)
        self.assertIn('resource_root / "assets" / "软件使用说明.md"', text)
        self.assertIn("browser.setMarkdown(content)", text)
        self.assertTrue(MANUAL.is_file())
        document = MANUAL.read_text(encoding="utf-8")
        for heading in ("打开图片", "分断线段", "保存当前图片", "导出全部图片队列", "常见问题"):
            self.assertIn(heading, document)

    def test_sidebar_uses_existing_maximum_once(self):
        """宽度上限不增长，默认最大展开也不能重置后续手动拖动。"""
        text = MAIN.read_text(encoding="utf-8")
        self.assertIn("panel.setMaximumWidth(360)", text)
        self.assertIn("_sidebar_initial_width_done = True", text)
        self.assertIn("self.right_panel.maximumWidth()", text)
        self.assertIn("QTimer.singleShot(0, self._maximize_initial_sidebar)", text)

    def test_queue_pin_is_small_icon(self):
        """固定状态使用图标与悬停提示，不显示占位的大文字按钮。"""
        text = QUEUE.read_text(encoding="utf-8")
        self.assertIn("self.pin_button.setFixedSize(30, 30)", text)
        self.assertIn("setIcon(create_pin_icon(pinned))", text)
        self.assertNotIn('self.pin_button.setText("取消固定"', text)

    def test_pin_pen_uses_valid_qt_enum_names(self):
        """用 Qt6 实际枚举名称绘制图钉，避免窗口初始化时直接抛异常。"""
        text = QUEUE.read_text(encoding="utf-8")
        tree = ast.parse(text)
        enum_names = {"PenCapStyle": set(), "PenJoinStyle": set()}
        # 遍历图钉的全部属性读取，不只检查第一处报错的枚举。
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Attribute):
                continue
            base = node.value
            if isinstance(base.value, ast.Name) and base.value.id == "Qt" and base.attr in enum_names:
                enum_names[base.attr].add(node.attr)
        self.assertEqual(enum_names["PenCapStyle"], {"RoundCap"})
        self.assertEqual(enum_names["PenJoinStyle"], {"RoundJoin"})
        # 显式配置浮点线宽，避免 QPen(QColor, float, ...) 重载选择的不确定性。
        for method in ("setWidthF(2.3)", "setCapStyle(Qt.PenCapStyle.RoundCap)",
                       "setJoinStyle(Qt.PenJoinStyle.RoundJoin)"):
            self.assertIn(method, text)

    def test_auto_hide_queue_is_a_small_overlay_and_no_horizontal_scroll(self):
        """取消固定后不应出现占满画布高度的左边框，图片列表也不应水平滚动。"""
        main = MAIN.read_text(encoding="utf-8")
        queue = QUEUE.read_text(encoding="utf-8")
        self.assertIn('self.queue_tab.setFixedSize(30, 30)', main)
        self.assertNotIn('layout.addWidget(self.queue_tab)', main)
        self.assertIn('self.queue_tab.move(8, 10)', main)
        self.assertIn('min(640, available_height)', main)
        self.assertIn('self.workspace_layout.insertWidget(0, self.queue_panel)', main)
        self.assertIn('self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)', queue)
        self.assertIn('header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)', queue)

    def test_batch_export_has_second_confirmation(self):
        """先问是否将未编辑图片整张导出；拒绝后应保留仅导出已编辑图的机会。"""
        source = MAIN.read_text(encoding="utf-8")
        self.assertIn('def _confirm_batch_export_scope', source)
        self.assertIn('"仅导出已编辑图片？"', source)
        method = source[source.index('    def start_batch_export(self)'):source.index('    @staticmethod\n    def _existing_outputs',source.index('    def start_batch_export(self)'))]
        self.assertLess(method.index('self._confirm_batch_export_scope()'),
                        method.index('QFileDialog.getExistingDirectory'))
        self.assertIn('for doc in documents:', method)

    def test_right_panel_keeps_current_export_but_not_duplicate_switch(self):
        """右侧保留当前图导出，批量导出继续由左侧队列管理。"""
        text = MAIN.read_text(encoding="utf-8")
        self.assertNotIn("QCheckBox", text)
        self.assertNotIn('QLabel("操作说明', text)
        self.assertIn("self.button_export.clicked.connect(self.start_export)", text)
        self.assertIn("self.queue_panel.exportAll.connect(self.start_batch_export)", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
