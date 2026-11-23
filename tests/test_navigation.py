"""Normal launcher paths with no inference backend, including a switched but unsent handoff."""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from teacher.__main__ import main
from teacher.navigation import Navigator
from teacher.library import configure_topic_vault
from teacher.plugin import ClassroomPlugin
from teacher.profile import initialize
from teacher.runtime import Tutor


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.base = TemporaryDirectory()
        self.addCleanup(self.base.cleanup)
        self.basepath = Path(self.base.name)
        self.a, self.b = self.basepath / "A", self.basepath / "中文 B 库"
        env = patch.dict(os.environ, {"HERMES_TEACHER_LANGUAGE": "zh"})
        env.start()
        self.addCleanup(env.stop)
        self.profile = initialize(self.basepath / "profile", self.a, language="zh")
        self.t = Tutor(self.profile)
        self.nav = Navigator(self.t, self.a)

    def test_visit_group_navigation_and_goal_draft_without_inference(self):
        algebra = configure_topic_vault(self.a, "代数", create=True)
        probability = configure_topic_vault(self.a, "概率", create=True)
        with patch.object(self.t, "turn", side_effect=AssertionError("导航不应调用模型")):
            first = self.nav.choose(topic="代数")
            self.nav.choose(topic="概率")
            self.nav.choose(lesson_id=first["id"])
            self.assertEqual([row["topic"] for row in self.nav.catalog()], ["代数", "概率"])
            self.assertTrue(self.nav.prose("返回学习大厅"))
            self.assertEqual(self.nav.vault, self.a)
            self.assertTrue(self.nav.prose("继续上次"))
            self.assertEqual(self.nav.vault, algebra)
            self.assertTrue(self.nav.prose("切换主题到概率！"))
            self.assertEqual(self.nav.vault, probability)
            self.assertTrue(self.nav.prose("今天想学代数"))
            self.assertEqual(self.nav.draft, "今天想学代数")
            self.assertFalse(self.nav.prose("今天想学代数"))
            self.assertEqual(self.nav.draft, "")
            self.nav.switch(".")
            self.assertEqual(self.nav.active(), first["id"])
            self.nav.switch("../代数")
            self.assertEqual(self.nav.active(), first["id"])
            self.nav.switch("../概率")
            self.assertEqual(self.nav.vault, probability)
            self.assertIsNone(self.nav.lesson)
            self.assertFalse(self.nav.prose("临时讨论一个问题"))
            self.assertTrue(self.nav.lesson["temporary"])
            self.assertEqual(self.nav.vault, self.a)
        self.assertFalse((self.profile / "state.db").exists())

    def test_english_navigation_and_multilingual_free_text(self):
        with patch.dict(os.environ, {"HERMES_TEACHER_LANGUAGE": "en"}):
            with patch.object(self.t, "turn", side_effect=AssertionError("Navigation started a model turn")):
                self.assertEqual(self.nav.action("list", "missing"), "No matching lessons.")
                self.assertTrue(self.nav.prose("Today I want to learn probability."))
                first = self.nav.active()
                self.assertEqual(self.nav.lesson["topic"], "probability")
                self.assertEqual(self.nav.draft, "Today I want to learn probability.")
                self.assertFalse(self.nav.prose(self.nav.draft))
                self.assertTrue(self.nav.prose("Return to the classroom lobby"))
                self.assertTrue(self.nav.prose("Resume the last lesson"))
                self.assertEqual(self.nav.active(), first)
                self.assertTrue(self.nav.prose("Switch topics to Algebra"))
                self.assertEqual(self.nav.lesson["topic"], "Algebra")
                self.assertFalse(self.nav.prose("Je veux apprendre le calcul"))
                self.assertEqual(self.nav.lesson["topic"], "Algebra")
        self.assertFalse((self.profile / "state.db").exists())

    def test_desktop_reports_profile_language_without_an_open_lesson(self):
        plugin = ClassroomPlugin(None, self.profile)
        hermes = SimpleNamespace(get_hermes_home=lambda: self.profile)
        with patch.dict(sys.modules, {"hermes_constants": hermes}):
            with patch.dict(os.environ):
                os.environ.pop("HERMES_TEACHER_LANGUAGE", None)
                state = json.loads(plugin.desktop_command('{"action":"state"}'))
                self.assertEqual(state, {"lesson": None, "vault": str(self.a), "language": "zh"})
                os.environ["HERMES_TEACHER_LANGUAGE"] = "en"
                state = json.loads(plugin.desktop_command('{"action":"state"}'))
                self.assertEqual(state, {"lesson": None, "vault": str(self.a), "language": "en"})
                error = json.loads(plugin.desktop_command('{"action":"checkpoint"}'))
                self.assertEqual(error, {"error": "Open a lesson first", "language": "en"})

    def test_catalog_prefers_registered_topic_and_rejects_foreign_id(self):
        old = self.nav.choose(topic="同名主题")
        configure_topic_vault(self.a, "同名主题", create=True)
        new = self.nav.choose(topic="同名主题")
        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual([item["id"] for item in self.nav.catalog()], [new["id"]])
        with self.assertRaisesRegex(ValueError, "资料库范围"):
            self.nav.action("open", old["id"])
        self.assertEqual(self.nav.active(), new["id"])

    def test_no_calls_draft_narrow_selection_and_real_vault_switch(self):
        self.assertEqual(self.nav.catalog(), [])
        self.assertFalse((self.a / "lessons").exists())
        with patch("teacher.runtime.Tutor.turn", side_effect=AssertionError("导航发起了推理")):
            self.assertTrue(self.nav.prose("今天想学概率论"))
            a = self.nav.lesson
            self.assertEqual(self.t.learning.state(a["id"])["stage"], "ready")
            self.t.model(a["id"], model="fixture-test", effort="low")
            self.assertEqual(self.t.learning.state(a["id"])["stage"], "ready")
            self.nav.switch(self.b, create=True)
            self.assertEqual(self.nav.draft, "今天想学概率论")
            b = self.nav.choose(topic="概率论")
            self.assertNotEqual(a["id"], b["id"])
            for v, s in ((self.a, "分母是所有阳性者"), (self.b, "我认为分母是所有患病者")):
                (v / "materials").mkdir()
                (v / "materials/卡.md").write_text(s)
            self.assertIn("所有患病者", self.nav.action("material", "materials/卡.md"))
            self.assertIn("1. 概率论", self.nav.action("list", "概率"))
            self.assertEqual(self.nav.action("open", "1")["id"], b["id"])
            self.assertFalse(self.nav.prose("插问一下，GPU 是什么？"))
            self.assertEqual(self.nav.active(), b["id"])
            self.assertFalse((self.profile / "state.db").exists())
        self.assertEqual(
            json.loads((self.profile / "config.yaml").read_text())["teacher"]["vault"], str(self.a)
        )

    def test_handoff_reports_loss_after_switch_and_retries_same_admission(self):
        old = self.nav.choose(topic="测验")
        self.t.records.append(old["id"], "assistant", "这是已讲的节点", "teacher")
        self.t.records.checkpoint(old["id"], "下次先检查条件概率")
        with self.assertRaisesRegex(ValueError, "已取消"):
            self.nav.action("handoff", "取消")
        with patch.object(self.t, "turn", side_effect=RuntimeError("连接中断")) as first:
            self.assertIn("交接发送尚待确认", self.nav.action("handoff", "确认"))
            self.assertNotEqual(self.nav.active(), old["id"])
        with patch.dict(os.environ, {"HERMES_TEACHER_LANGUAGE": "en"}), patch.object(
            self.t, "turn", return_value="下节课先复测分母",
        ) as retry:
            self.assertIn("先复测分母", self.nav.action("handoff-retry", ""))
            self.assertEqual(first.call_args, retry.call_args)

    def test_open_after_interrupted_handoff_restores_frozen_learning_progress(self):
        old = self.nav.choose(topic="概率分母")
        key = old["id"]
        saved = [{"question": "分母是什么？", "outcome": "wrong"}]
        self.t.learning.update(
            key,
            goal="分清条件概率",
            stage="plan",
            evidence=saved,
            plan=["看自然频数"],
            revision=2,
            node=0,
            model={"model": "handoff-model"},
            canonical="old-native",
        )
        self.t.records.append(key, "assistant", "先看条件概率", "old-explanation")
        self.t.records.checkpoint(key, "下次先复测分母。")

        with patch.object(self.t.learning, "seed_handoff", side_effect=RuntimeError("进程在交接落盘后退出")):
            with self.assertRaisesRegex(RuntimeError, "交接落盘后退出"):
                self.nav.action("handoff", "确认")
        recovered = Tutor(self.profile)
        with recovered.learning._db() as db:
            self.assertIsNone(db.execute("SELECT state FROM courses WHERE id != ?", (key,)).fetchone())
        self.t.learning.update(key, goal="后来修改的旧课目标", evidence=[], revision=3)

        new = Navigator(recovered, self.a).choose(topic=old["topic"])
        self.assertNotEqual(new["id"], key)
        state = recovered.learning.state(new["id"])
        self.assertEqual(state["goal"], "分清条件概率")
        self.assertEqual(state["evidence"], saved)
        self.assertEqual(state["plan"], ["看自然频数"])
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["stage"], "revise")
        self.assertEqual(state["model"], {"model": "handoff-model"})
        self.assertNotIn("canonical", state)
        self.assertEqual(recovered.records.incoming_handoff(new["id"])["source_id"], key)

    def test_replay_from_old_client_never_overwrites_later_learning(self):
        old = self.nav.choose(topic="条件概率")
        source = old["id"]
        inherited = [{"question": "旧课探测", "outcome": "wrong"}]
        self.t.learning.update(source, stage="teach", plan=["分母"], evidence=inherited)
        self.t.records.checkpoint(source, "下一次复测。")
        with patch.object(self.t, "turn", side_effect=RuntimeError("发送中断")):
            self.nav.action("handoff", "确认")
        target = self.nav.active()
        card = self.t.learning.quiz(
            target,
            "复测分母",
            "single",
            {
                "prompt": "后验概率的分母是什么？",
                "options": [{"label": "所有阳性", "correct": True}, {"label": "所有患病", "correct": False}],
                "hint": "想想条件事件。",
                "explanation": "条件是检测阳性。",
            },
        )
        lines = card["text"].splitlines()
        matches = [letter for letter in ("A", "B") if f"{letter}. 所有阳性" in lines]
        self.assertEqual(len(matches), 1, "题面应为「所有阳性」标明唯一选项字母")
        answer = matches[0]
        self.t.records.append(target, "user", answer, "new-answer")
        self.assertIn(
            "独立答对", self.t.learning.receive(target, answer, "new-answer", expected_id=card["id"])
        )
        expected = self.t.learning.state(target)
        self.assertEqual(len(expected["evidence"]), 2)
        self.t.learning.update(source, goal="旧客户端改变了目标", evidence=[])

        stale_tutor = Tutor(self.profile)
        stale = Navigator(stale_tutor, self.a, lesson=source)
        with patch.object(stale_tutor, "turn") as retry:
            self.assertIn("已恢复这节课的当前进度", stale.action("handoff", "确认"))
        self.assertEqual(stale.active(), target)
        retry.assert_not_called()
        self.assertEqual(stale_tutor.learning.state(target), expected)
        self.assertEqual(stale_tutor.learning.pending(target)["id"], card["id"])

        plugin = ClassroomPlugin(None, self.profile)
        result = plugin.before(
            session_id=stale.lesson["session_id"],
            turn_id="late-handoff",
            user_message="〔已确认的续课交接〕\n过期的起点。",
        )
        self.assertEqual(result["action"], "block")
        self.assertIn("已恢复当前进度", result["message"])
        self.assertEqual(stale_tutor.learning.state(target), expected)

    def test_lobby_keeps_empty_vault_and_cancelled_create(self):
        absent = self.basepath / "尚未创建"
        text = f"/list 贝叶斯\n/workspace {absent}\n取消\n/list\n/quit\n"
        result = subprocess.run(
            [sys.executable, "-m", "teacher", "-p", str(self.profile), "chat"],
            input=text,
            capture_output=True,
            text=True,
            timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("已取消", result.stdout)
        self.assertFalse(absent.exists())
        self.assertEqual(list(self.a.iterdir()), [])

    def test_profile_imports_only_the_selected_route_and_vision_declaration(self):
        source = self.basepath / "source"
        source.mkdir()
        fake = "synthetic-private-value"
        entry = dict(
            api="http://127.0.0.1/v1",
            key_env="TRIAL_KEY",
            api_key=fake,
            extra_headers={"Authorization": fake},
            models={"trial": {"context_length": 60000}},
        )
        original = {
            "model": {"provider": "fixture", "default": "trial", "supports_vision": True},
            "providers": {"fixture": entry, "unselected": {"api_key": fake}},
        }
        config = source / "config.yaml"
        config.write_text(json.dumps(original))
        target = initialize(self.basepath / "own", self.b, source=source, provider="fixture", model="trial")
        destination = (target / "config.yaml").read_text()
        self.assertTrue(json.loads(destination)["model"]["supports_vision"])
        self.assertNotIn(fake, destination)
        self.assertNotIn("unselected", destination)
        self.assertEqual(json.loads(config.read_text()), original)
        entry["api"] += "?api_key=" + fake
        config.write_text(json.dumps(original))
        with self.assertRaisesRegex(ValueError, "地址含凭据"):
            initialize(self.basepath / "other", self.b, source=source, provider="fixture", model="trial")
        self.assertFalse((self.basepath / "other").exists())

    def test_installed_launcher_finds_parent_profile_from_another_directory(self):
        launcher = self.profile / "plugins/hermes-teacher/bin/hermes-teacher"
        package = launcher.parents[1]
        self.assertTrue((package / "desktop/plugin.js").is_file())
        self.assertEqual(
            (package / "tui/hermes-teacher-quiz.mjs").read_bytes(),
            (self.profile / "tui-widgets/hermes-teacher-quiz.mjs").read_bytes(),
        )
        result = subprocess.run(
            [str(launcher), "prepare", "--topic", "新安装"],
            cwd=self.basepath,
            env={**os.environ, "HERMES_TEACHER_PYTHON": sys.executable},
            capture_output=True,
            text=True,
            timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["vault"], str(self.a))
        self.assertFalse((self.profile / "state.db").exists())

    def test_cancel_chinese_search_without_creating_history(self):
        lesson = self.nav.choose(topic="概率论")
        output = subprocess.run(
            [sys.executable, "-m", "teacher", "-p", str(self.profile), "chat"],
            input="/list 概率\n取消\n/list 概率\n/open 1\n/quit\n",
            env={**os.environ, "COLUMNS": "28"},
            capture_output=True,
            text=True,
            timeout=12,
        )
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertIn("已退出课程列表", output.stdout)
        self.assertIn(lesson["id"], output.stdout)
        self.assertFalse(Path(lesson["record"]).exists())
        self.assertFalse((self.profile / "state.db").exists())

    def test_resume_and_explicit_new_through_cli(self):
        prefix = ["--profile", str(self.profile), "prepare"]

        def prepare(args):
            with redirect_stdout(out := StringIO()):
                main(prefix + args)
            return json.loads(out.getvalue())

        old = prepare(["--topic", "A"])
        self.assertEqual(prepare(["--resume"])["session_id"], old["session_id"])
        new = prepare(["--topic", "A", "--new"])
        self.assertNotEqual(new["session_id"], old["session_id"])
        self.assertNotEqual(new["record"], old["record"])
