"""批量导出范围测试：只提取主窗口的纯业务决策方法，不启动 Qt。

通过模拟消息框选择执行生产函数，检查用户真实选择语义；这样不需要
Linux 测试机安装 PySide6，也能验证“否→仅导出已编辑”的逻辑路径。
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

MAIN = Path(__file__).resolve().parent.parent / "src" / "main.py"


def load_scope_method():
    """只编译业务决策方法，外部 QMessageBox 用可控替身注入。"""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    main_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow")
    method = next(n for n in main_class.body if isinstance(n, ast.FunctionDef)
                  and n.name == "_confirm_batch_export_scope")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              method], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, str(MAIN), "exec"), namespace)
    return namespace["_confirm_batch_export_scope"]


class FakeMessageBox:
    """记录每个询问，模拟 Windows QMessageBox 的是、否按钮。"""

    StandardButton = SimpleNamespace(Yes=1, No=2)
    answers = []
    questions = []
    notices = []

    @classmethod
    def reset(cls, answers):
        cls.answers = list(answers)
        cls.questions = []
        cls.notices = []

    @classmethod
    def question(cls, parent, title, content, choices, default):
        cls.questions.append((title, content))
        if not cls.answers:
            raise AssertionError("出现了未预期的额外询问")
        return cls.answers.pop(0)

    @classmethod
    def information(cls, parent, title, content):
        cls.notices.append((title, content))


class BatchScopeTests(unittest.TestCase):
    """覆盖所有编辑/未编辑组合和两层确认分支。"""

    @classmethod
    def setUpClass(cls):
        cls.func = staticmethod(load_scope_method())
        cls.func.__globals__["QMessageBox"] = FakeMessageBox

    @staticmethod
    def docs(edited_flags):
        return [SimpleNamespace(path=f"P{i:03}.png",
                                model=SimpleNamespace(guides=[1] if edited else []))
                for i, edited in enumerate(edited_flags)]

    def check(self, flags, answers, expected):
        FakeMessageBox.reset(answers)
        docs = self.docs(flags)
        result = self.func(SimpleNamespace(_documents=docs))
        if expected is None:
            self.assertIsNone(result)
        else:
            self.assertEqual(result, [docs[i] for i in expected])
        self.assertFalse(FakeMessageBox.answers)
        return FakeMessageBox

    def test_all_edited_exports_all_without_question(self):
        self.assertFalse(self.check([True, True], [], [0, 1]).questions)

    def test_yes_exports_both_edited_and_unedited(self):
        box = self.check([True, False, True], [1], [0, 1, 2])
        self.assertEqual(len(box.questions), 1)

    def test_no_then_yes_exports_only_edited(self):
        box = self.check([False, True, False, True], [2, 1], [1, 3])
        self.assertEqual(len(box.questions), 2)
        self.assertEqual(box.questions[1][0], "仅导出已编辑图片？")

    def test_no_then_no_aborts_everything(self):
        box = self.check([True, False], [2, 2], None)
        self.assertEqual(len(box.questions), 2)

    def test_no_with_no_edited_images_aborts_with_explanation(self):
        box = self.check([False, False], [2], None)
        self.assertEqual(len(box.questions), 1)
        self.assertEqual(len(box.notices), 1)

    def test_all_unedited_yes_exports_whole_images(self):
        self.check([False, False], [1], [0, 1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
