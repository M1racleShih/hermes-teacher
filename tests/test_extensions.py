import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from teacher.extensions import Extensions, extensions_config
from teacher.learning import Learning
from teacher.library import configure_topic_vault
from teacher.models import ModelSettings
from teacher.plugin import ClassroomPlugin, TOOLS
from teacher.records import Records


class ExtensionsTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        self.profile, self.vault = root / "profile", root / "vault"
        self.profile.mkdir()
        self.vault.mkdir()
        self.records = Records(self.profile)
        self.lesson = self.records.prepare(self.vault, "原主题")

    def settings(self, *, language="zh", **flags):
        (self.profile / "config.yaml").write_text(
            json.dumps(
                {
                    "model": {"provider": "teacher-route", "default": "main"},
                    "teacher": {
                        "language": language,
                        "extensions": flags,
                        "models": {"roles": {"mermaid": {"provider": "figure-route", "model": "diagram"}}},
                    },
                }
            )
        )

    def test_english_extensions_keep_task_and_goal_text(self):
        self.settings(language="en", todo=True, goal=True, ui=True)
        extras = Extensions(self.profile)
        self.assertEqual(extras.invoke(self.lesson, "/todo"), "This session branch has no tasks.")
        self.assertEqual(extras.invoke(self.lesson, "/todo", "add 练习"), "1. [ ] 练习")
        self.assertEqual(extras.invoke(self.lesson, "/goal", "set 比较"), "Active · 比较")
        self.assertIn("Tasks 1/1", extras.display(self.lesson))

    def test_explicit_task_switch_and_branch_cut(self):
        disabled = Extensions(self.profile)
        self.assertIsNone(disabled.invoke(self.lesson, "/todo", "add invisible"))
        self.assertIsNone(disabled.display(self.lesson))
        self.settings(todo=True, ui=True)
        extras = Extensions(self.profile)
        extras.invoke(self.lesson, "/todo", "add 练习 1")
        child = self.records.bind_session(self.lesson["id"], "branch-a", parent_id=self.lesson["session_id"])
        self.assertIn("1. [ ] 练习 1", extras.invoke(child, "/todo"))
        extras.invoke(child, "/todo", "done 1")
        sibling = self.records.bind_session(
            self.lesson["id"], "branch-b", parent_id=self.lesson["session_id"]
        )
        self.assertIn("1. [ ] 练习 1", extras.invoke(sibling, "/todo"))
        self.assertIn("待办 1/1", extras.display(sibling))
        self.assertEqual(self.records.history(self.lesson["id"]), [])
        other = self.records.prepare(self.vault, "原主题", new=True)
        self.assertIn("没有待办", extras.invoke(other, "/todo"))
        self.settings(todo=False, ui=True)
        self.assertNotIn("待办", Extensions(self.profile).display(sibling))

    def test_search_title_and_native_compress(self):
        self.settings(search=True, title=True, compact=True)
        source = self.vault / "概念.md"
        source.write_text("后验 融合 证据", encoding="utf-8")
        (self.vault / "linked.md").symlink_to(source)
        calls = []
        extras = Extensions(
            self.profile,
            rename=lambda lesson, title: calls.append(("rename", lesson, title)),
            compact=lambda lesson, focus: calls.append(("compact", lesson, focus)) or "压缩预检",
            derive_title=lambda opener: opener[:12],
        )
        hits = extras.invoke(self.lesson, "/search", "融合 后验")
        self.assertIn("概念.md", hits)
        self.assertNotIn("linked.md", hits)
        extras.auto_title(self.lesson, "贝叶斯证据的解释")
        extras.auto_title(self.lesson, "不会覆盖")
        self.assertEqual(calls[0], ("rename", self.lesson["id"], "贝叶斯证据的解释"))
        self.assertEqual(extras.invoke(self.lesson, "/title"), "贝叶斯证据的解释")
        self.assertEqual(self.records.get(self.lesson["id"])["topic"], "原主题")
        self.assertEqual(extras.invoke(self.lesson, "/compact", "--preview"), "压缩预检")
        self.assertEqual(calls[1], ("compact", self.lesson["id"], "--preview"))

    def test_goal_explicit_activation_continuation_and_pending_guard(self):
        self.settings(goal=True)
        calls = []
        outcomes = iter([("continue", "先比较", False, None, False), ("done", "已比较", False, None, False)])
        extras = Extensions(
            self.profile,
            judge=lambda *_: next(outcomes),
            turn=lambda lesson, prompt, **kw: calls.append((lesson, prompt, kw)) or "目标已解释",
        )
        self.assertEqual(extras.continue_goal(self.lesson, "普通课堂", pending=lambda _: None), [])
        self.assertIn("进行中", extras.invoke(self.lesson, "/goal", "set 比较"))
        self.assertEqual(extras.continue_goal(self.lesson, "题面", pending=lambda _: {"id": "quiz"}), [])
        responses = extras.continue_goal(self.lesson, "说明开头", pending=lambda _: None)
        self.assertTrue(any("完成" in response for response in responses))
        self.assertEqual(calls[0][2], {"generated": True})
        self.assertEqual(len(calls), 1)

    def test_model_metadata_preserves_role_choice_and_disabled_command(self):
        self.settings(model=True)
        lesson_choice = {"provider": "different", "model": "current", "reasoning_effort": "high"}
        Learning(self.profile).update(self.lesson["id"], model=lesson_choice)
        extras = Extensions(self.profile)
        self.assertIn("different/current", extras.invoke(self.lesson, "/model-info"))
        self.assertIn("high", extras.invoke(self.lesson, "/model-info"))
        self.assertIsNone(extras.invoke(self.lesson, "/model", "another"))
        self.assertEqual(
            ModelSettings(self.profile).role("mermaid"), {"provider": "figure-route", "model": "diagram"}
        )
        self.settings(model=False)
        self.assertIsNone(Extensions(self.profile).invoke(self.lesson, "/model-info"))
        self.settings(typo=True)
        with self.assertRaises(ValueError):
            extensions_config(self.profile)

    def test_model_tools_follow_opt_ins_and_reuse_current_branch_state(self):
        sid = self.lesson["session_id"]
        disabled = ClassroomPlugin(None, self.profile)
        self.assertEqual(disabled.tools, TOOLS)
        self.assertEqual(disabled.guard(tool_name="teacher_todo", session_id=sid)["action"], "block")
        with self.assertRaisesRegex(ValueError, "Enable the corresponding classroom extension"):
            disabled.invoke("teacher_todo", {"action": "list"}, sid)
        self.settings(todo=True, search=True, goal=False)
        plugin = ClassroomPlugin(None, self.profile)
        self.assertEqual(set(plugin.tools) - set(TOOLS), {"teacher_todo", "teacher_find", "teacher_grep"})
        self.assertIsNone(plugin.guard(tool_name="teacher_todo", session_id=sid))
        self.assertIn("1. [ ] 提炼公式", plugin.extensions.todo(self.lesson, "add 提炼公式"))
        result = plugin.invoke("teacher_todo", {"action": "done", "number": 1}, sid)
        self.assertEqual(result, [{"text": "提炼公式", "done": True}])
        self.assertIn("1. [x]", plugin.extensions.todo(self.lesson, "list"))
        self.assertEqual(self.records.history(self.lesson["id"]), [])
        with self.assertRaisesRegex(ValueError, "打开课堂"):
            plugin.invoke("teacher_todo", {"action": "list"}, "ordinary")

    def test_model_file_tools_and_completion_stay_in_bound_library(self):
        self.settings(search=True)
        other = configure_topic_vault(self.vault, "另一主题", "child", create=True)
        for vault, marker in ((self.vault, "本课"), (other, "另一课")):
            (vault / "materials").mkdir()
            (vault / "materials/条件 概率.csv").write_text(f"名称,值\n概率标记,{marker}\n")
        (self.vault / "materials/入口.csv").symlink_to(other / "materials/条件 概率.csv")
        sid = self.lesson["session_id"]
        plugin = ClassroomPlugin(None, self.profile)
        self.assertEqual(plugin.invoke("teacher_find", {"query": "概率"}, sid), ["materials/条件 概率.csv"])
        self.assertEqual(
            plugin.invoke("teacher_grep", {"query": "概率标记"}, sid),
            [{"path": "materials/条件 概率.csv", "line": 2, "text": "概率标记,本课"}],
        )
        self.assertEqual(plugin.extensions.complete(self.lesson, "请读 @概率"), [("materials/条件 概率.csv", -2)])
        self.assertEqual(plugin.extensions.complete(self.lesson, "/material materials/入口"), [])
        other_lesson = self.records.prepare(other, "另一主题")
        self.assertEqual(
            plugin.invoke("teacher_grep", {"query": "概率标记"}, other_lesson["session_id"])[0]["text"],
            "概率标记,另一课",
        )

    def test_goal_report_requires_current_explicit_goal_and_no_pending_card(self):
        self.settings(goal=True)
        plugin = ClassroomPlugin(None, self.profile)
        sid = self.lesson["session_id"]
        args = {"action": "wait", "goal_id": "forged", "reason": "等待演算"}
        with self.assertRaisesRegex(ValueError, "当前会话开启"):
            plugin.invoke("teacher_goal", args, sid)
        plugin.extensions.goal(self.lesson, "set 比较分母")
        args["goal_id"] = plugin.extensions.active_goal(self.lesson)["id"]
        answer = plugin.learning.ask(self.lesson["id"], "理解了吗？", "text")
        with self.assertRaisesRegex(ValueError, "等待学习者"):
            plugin.invoke("teacher_goal", args, sid)
        plugin.learning.receive(self.lesson["id"], "已理解", "confirmed", expected_id=answer["id"])
        self.assertIn("等待", plugin.invoke("teacher_goal", args, sid))
        self.assertIsNone(plugin.extensions.active_goal(self.lesson))
        plugin.extensions.goal(self.lesson, "resume")
        self.assertIn("受阻", plugin.invoke("teacher_goal", {**args, "action": "blocked"}, sid))
        plugin.extensions.goal(self.lesson, "set 复核另一问题")
        with self.assertRaisesRegex(ValueError, "当前会话开启"):
            plugin.invoke("teacher_goal", {**args, "action": "complete"}, sid)
