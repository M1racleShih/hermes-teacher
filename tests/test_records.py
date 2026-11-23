from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from teacher.i18n import use_language
from teacher.library import (
    BusyError,
    LibraryError,
    configure_topic_vault,
    list_materials,
    read_material,
    safe_path,
    topic_vaults,
)
from teacher.plugin import ClassroomPlugin
from teacher.records import Records, RecordsError


class RecordHookTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"HERMES_TEACHER_LANGUAGE": "zh"}))
        sandbox = TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name)
        self.vault = configure_topic_vault(self.root, "组合", create=True)
        self.plugin = ClassroomPlugin(None, self.root / "profile")
        self.lesson = self.plugin.records.prepare(self.vault, "组合")
        self.session_id = self.lesson["session_id"]
        self.messages = []
        self.enterContext(patch("teacher.plugin.native_messages", side_effect=self.native_messages))

    def native_messages(self, profile, session_id, *, after_id=0, limit=None, latest=False, **options):
        messages = [row for row in self.messages if row["id"] > after_id and row["session_id"] == session_id]
        return messages[-limit:] if latest and limit else messages

    def message(self, role, content="", **fields):
        self.messages.append({
            "id": len(self.messages) + 1, "session_id": self.session_id, "role": role,
            "content": content, **fields,
        })

    def before(self, turn_id):
        reply = self.plugin.before(session_id=self.session_id, turn_id=turn_id, user_message="同一条学习请求")
        self.assertIn("context", reply)
        self.message("user", "会带有注入内容的原生消息")

    def tool(self, name, text, turn_id, *, sibling=False):
        calls = [{"id": name, "function": {"name": name, "arguments": "raw tool arguments"}}]
        if sibling:
            calls.append({"id": "teacher_material", "function": {"name": "teacher_material"}})
        self.message("assistant", text, tool_calls=calls, display_kind="hidden", reasoning="private reasoning")
        self.assertIsNone(self.plugin.guard(tool_name=name, session_id=self.session_id, turn_id=turn_id))

    def checkpoint(self, turn_id, *, sibling=False, failed_result=False):
        self.tool("teacher_checkpoint", "", turn_id, sibling=sibling)
        payload = self.plugin.invoke(
            "teacher_checkpoint",
            dict(covered="已讲合并", evidence="会分别计数", gaps="未比较次序", next_step="练习三组"),
            self.session_id,
        )
        self.assertTrue(payload["saved"])
        self.message(
            "tool", json.dumps({"error": "失败"} if failed_result else payload),
            tool_name="teacher_checkpoint", tool_call_id="teacher_checkpoint",
        )

    def finish(self, turn_id, *, failed=False, interrupted=False, final=True):
        if final:
            self.message("assistant", "原始模型末句", finish_reason="stop")
            self.plugin.after(session_id=self.session_id, turn_id=turn_id, assistant_response="显示后的末句")
        self.plugin.end(
            session_id=self.session_id, turn_id=turn_id, completed=not (failed or interrupted),
            failed=failed, interrupted=interrupted,
        )

    def test_tool_prose_repeat_input_and_transformed_reply_are_recorded_in_order(self):
        for turn_id in ("first", "second"):
            self.before(turn_id)
            self.tool("teacher_material", "公式 $n+m$\n\n```python\nprint(1)\n```", turn_id)
            self.plugin.guard(tool_name="teacher_material", session_id=self.session_id, turn_id=turn_id)
            self.message("tool", "raw tool output", tool_name="teacher_material")
            self.finish(turn_id)
        lesson_id = self.lesson["id"]
        self.assertEqual(
            [row["text"] for row in self.plugin.records.history(lesson_id)],
            ["同一条学习请求", "公式 $n+m$\n\n```python\nprint(1)\n```", "显示后的末句"] * 2,
        )
        document = Path(self.lesson["record"]).read_text()
        for hidden in ("raw tool", "private reasoning", "注入内容", "原始模型末句"):
            self.assertNotIn(hidden, document)

    def test_checkpoint_needs_exact_successful_tool_and_final_turn(self):
        key = self.lesson["id"]
        scenarios = ("sibling", "later", "failed", "interrupted", "failed-result", "missing-final", "valid")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                self.before(scenario)
                self.checkpoint(
                    scenario, sibling=scenario == "sibling", failed_result=scenario == "failed-result"
                )
                with self.assertRaisesRegex(RecordsError, "尚未生效"):
                    self.plugin.records.read_checkpoint(key)
                if scenario == "later":
                    self.tool("teacher_material", "查阅读书笔记", scenario)
                    self.message("tool", "资料列表", tool_name="teacher_material")
                self.finish(
                    scenario,
                    failed=scenario == "failed",
                    interrupted=scenario == "interrupted",
                    final=scenario not in {"missing-final", "interrupted"},
                )
                if scenario != "valid":
                    with self.assertRaisesRegex(RecordsError, "尚未生效"):
                        self.plugin.records.read_checkpoint(key)
        self.assertIn("练习三组", self.plugin.records.read_checkpoint(key)["text"])
        target = self.plugin.records.handoff(key, confirmed=True)
        self.assertEqual(self.plugin.records.history(target["id"]), [])
        self.assertNotEqual(target["record"], self.lesson["record"])


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(use_language("zh"))
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "资料库 A"
        self.vault.mkdir()
        self.profile = self.root / "profile"
        self.records = Records(self.profile)

    def test_prepare_resume_and_scope_isolate_same_topic(self):
        other = self.root / "资料库 B"
        with self.assertRaises(LibraryError):
            self.records.prepare(other, "贝叶斯")
        self.assertFalse(other.exists())
        self.assertEqual(self.records.search(self.vault, "贝叶斯"), [])
        self.assertEqual(list(self.vault.iterdir()), [])

        lesson = self.records.prepare(self.vault, "贝叶斯")
        original = Path(lesson["record"])
        self.assertFalse(original.exists())
        self.records.append(lesson["id"], "user", "学习先验概率", "u-1")
        self.assertEqual(self.records.prepare(self.vault, "贝叶斯")["id"], lesson["id"])
        self.assertEqual(self.records.search(self.vault, "先验")[0]["id"], lesson["id"])

        independent = self.records.prepare(self.vault, "贝叶斯", new=True)
        other_lesson = self.records.prepare(other, "贝叶斯", create=True)
        self.assertNotEqual(lesson["session_id"], independent["session_id"])
        self.assertNotEqual(lesson["record"], independent["record"])
        self.assertEqual(self.records.history(independent["id"]), [])
        self.assertNotIn(other_lesson["id"], {item["id"] for item in self.records.recent(self.vault)})
        self.assertIn("学习先验概率", original.read_text())
        self.assertEqual(Path(other_lesson["vault"]), other)

    def test_temporary_discussion_starts_without_topic_or_markdown(self):
        with self.assertRaises(RecordsError):
            self.records.prepare(self.vault, None)
        discussion = self.records.prepare(self.vault, None, temporary=True)
        self.assertTrue(discussion["temporary"])
        self.assertFalse(Path(discussion["record"]).exists())
        self.records.append(discussion["id"], "user", "为什么 GPU 适合并行？", "discussion")
        self.assertEqual(self.records.recent(self.vault)[0]["id"], discussion["id"])
        self.assertEqual(self.records.prepare(self.vault, None, temporary=True)["id"], discussion["id"])

    def test_english_record_and_handoff_interoperate_with_legacy_files(self):
        old = self.records.prepare(self.vault, "旧课堂")
        old_path = Path(old["record"])
        self.records.append(old["id"], "user", "原文 $P(A)$", "one")
        previous = old_path.read_bytes()
        checkpoint = self.records.checkpoint(old["id"], "旧进度")
        temporary = self.records.prepare(self.vault, None, temporary=True)

        with use_language("en"):
            self.assertEqual(self.records.prepare(self.vault, None, temporary=True)["id"], temporary["id"])
            self.records.append(old["id"], "assistant", "Still original.", "two", kind="feedback")
            self.assertEqual(old_path.read_bytes(), previous + b"> [!note] Feedback\n\nStill original.\n\n")
            updated = self.records.checkpoint(old["id"], "Updated notes.")
            self.assertEqual(updated["file"], checkpoint["file"])
            self.assertEqual(self.records.read_checkpoint(old["id"])["text"], "Updated notes.")
            self.assertEqual(self.records.handoff(old["id"], confirmed=True)["handoff"], "Updated notes.")

            configure_topic_vault(self.vault, "New topic", create=True)
            lesson = self.records.prepare(self.vault, "New topic")
            path = Path(lesson["record"])
            self.assertEqual(path.parent.name, "_pending")
            self.records.append(lesson["id"], "user", "仍保留中文原句。", "three")
            self.assertIn("> [!note] Learner\n\n仍保留中文原句。", path.read_text())
            self.assertTrue(Path(self.records.checkpoint(lesson["id"], "Continue.")["file"]).name.startswith(
                "Teacher Handoff-"
            ))
            self.assertEqual(
                self.records.prepare(self.vault, None, new=True, temporary=True)["topic"], "Temporary Discussion"
            )

    def test_materials_and_topic_routing_reject_bad_mappings(self):
        left = self.vault
        right = self.root / "独立 空格"
        for root, text in ((left, "错误 A"), (right, "错误 B")):
            (root / "concepts").mkdir(parents=True)
            (root / "concepts" / "先验.md").write_text(text)
        self.assertEqual(list_materials(left), ["concepts/先验.md"])
        self.assertEqual(read_material(left, "concepts/先验.md"), "错误 A")
        self.assertEqual(read_material(right, "concepts/先验.md"), "错误 B")
        (left / "concepts" / "链接.md").symlink_to(right / "concepts" / "先验.md")
        for path in ("concepts/链接.md", "concepts/../../../独立 空格/concepts/先验.md"):
            with self.assertRaises(LibraryError):
                read_material(left, path)
        (left / "concepts" / "链接.md").unlink()

        target = configure_topic_vault(left, "贝叶斯", create=True)
        lesson = self.records.prepare(left, "贝叶斯")
        self.assertEqual(Path(lesson["vault"]), target)
        self.assertEqual(Path(lesson["record"]).parent, target / "lessons" / "_待整理")
        self.assertEqual(self.records.prepare(target, "贝叶斯")["id"], lesson["id"])
        another = self.records.prepare(left, "概率论")
        self.assertEqual(topic_vaults(left)["概率论"], Path(another["vault"]))
        registry = left / ".teacher-vaults.json"
        registry.write_text('{"version":1,"vaults":{"越界":"../独立 空格"}}')
        with self.assertRaises(LibraryError):
            self.records.prepare(left, "越界")
        self.assertEqual(sorted(right.iterdir()), [right / "concepts"])

    def test_events_replay_and_branch_export(self):
        lesson = self.records.prepare(self.vault, "统计")
        key, initial = lesson["id"], lesson["session_id"]
        contents = "贝叶斯公式：$P(A\\mid B)$\n\n```python\nprint(1)\n```\n\n![[viz/示例.svg]]"
        self.assertTrue(self.records.append(key, "assistant", contents, "explain"))
        self.assertFalse(self.records.append(key, "assistant", contents, "explain"))
        with self.assertRaises(RecordsError):
            self.records.append(key, "assistant", "different", "explain")
        self.records.append(key, "tool", "raw role process", "tool")
        self.records.append(key, "assistant", "题目原文", "question", kind="question")
        self.records.append(key, "user", "答案与理由", "answer", kind="answer")
        self.records.bind_session(key, "compressed", parent_id=initial)
        self.records.append(key, "assistant", "教师反馈", "feedback", kind="feedback")
        self.records.append(key, "user", "其他分支", "old-tail", session_id=initial)
        self.records.append(key, "note", "教学备注", "note", kind="note")
        self.assertEqual(self.records.by_session(initial)["id"], key)
        self.assertEqual(self.records.by_session("compressed")["id"], key)
        self.assertEqual(
            [event["event_id"] for event in self.records.history(key)],
            ["explain", "question", "answer", "feedback", "note"],
        )
        self.assertIn("tool", {event["event_id"] for event in self.records.history(key, include_hidden=True)})
        self.assertNotIn(
            "feedback", {event["event_id"] for event in self.records.history(key, session_id=initial)}
        )
        note = Path(lesson["record"]).read_text()
        self.assertIn(contents, note)
        self.assertIn("教师反馈", note)
        self.assertNotIn("raw role process", note)
        self.assertNotIn("其他分支", note)

        self.records.set_recording(key, False)
        path = Path(self.records.get(key)["record"])
        paused = path.read_bytes()
        self.records.append(key, "user", "停录时提问", "unrecorded")
        restarted = Records(self.profile)
        self.assertFalse(restarted.prepare(self.vault, "统计")["recording"])
        self.assertEqual(paused, path.read_bytes())
        exported = self.vault / "专用导出.md"
        exported.write_text("旧占位")
        restarted.bind_record(key, exported)
        output = exported.read_text()
        self.assertIn("停录时提问", output)
        self.assertIn(contents, output)
        self.assertNotIn("其他分支", output)
        self.assertNotIn("旧占位", output)

    def test_seal_is_atomic_with_receipt_and_rotates_same_session(self):
        lesson = self.records.prepare(self.vault, "研究")
        key = lesson["id"]
        path = Path(lesson["record"])
        self.assertEqual(self.records.seal(key), {"status": "empty"})
        self.assertEqual(self.records.get(key)["record"], str(path))
        self.records.append(key, "user", "课文原样", "first")
        before = path.read_bytes()
        with self.assertRaises(BusyError):
            self.records.seal(key, busy=True)
        with patch.object(self.records, "_active_path", side_effect=OSError("receipt transaction failure")):
            with self.assertRaises(OSError):
                self.records.seal(key)
        self.assertEqual(self.records.get(key)["record"], str(path))
        self.assertEqual(self.records.sealed(self.vault), [])
        self.assertEqual(path.read_bytes(), before)

        receipt = self.records.seal(key)
        self.assertEqual(receipt["status"], "sealed")
        self.assertEqual(receipt["file"], str(path))
        self.assertEqual(self.records.get(key)["session_id"], lesson["session_id"])
        with self.assertRaises(RecordsError):
            self.records.bind_record(key, path)
        self.records.append(key, "assistant", "教师继续从原会话回答", "continued")
        current = Path(self.records.get(key)["record"])
        self.assertIn("从原会话回答", current.read_text())
        self.assertEqual(path.read_bytes(), before)

        self.records.set_recording(key, False)
        self.assertEqual(self.records.seal(key)["status"], "sealed")
        self.assertFalse(self.records.get(key)["recording"])
        self.assertIsNone(self.records.get(key)["record"])

    def test_interrupted_append_recovers_without_replay_duplication(self):
        lesson = self.records.prepare(self.vault, "恢复")
        key = lesson["id"]
        path = Path(lesson["record"])
        with patch("teacher.records.atomic_write", side_effect=OSError("interrupted projection")):
            with self.assertRaises(OSError):
                self.records.append(key, "user", "原文只记录一次", "once")
        self.assertFalse(path.exists())
        fresh = Records(self.profile)
        self.assertFalse(fresh.append(key, "user", "原文只记录一次", "once"))
        self.assertEqual(path.read_text().count("原文只记录一次"), 1)

    def test_checkpoint_and_handoff_validate_the_current_reply_and_file(self):
        lesson = self.records.prepare(self.vault, "Rust")
        key = lesson["id"]
        self.records.append(key, "user", "请保存进度", "save")
        checkpoint = self.records.checkpoint(
            key, "已讲所有权，借用尚未独立答对。下一步复测。", turn_id="save"
        )
        with self.assertRaises(RecordsError):
            self.records.read_checkpoint(key)
        self.records.append(
            key, "assistant", "进度已保存", "reply:save", completed_checkpoint_turn="save"
        )
        self.assertEqual(self.records.read_checkpoint(key)["text"], checkpoint["text"])
        with self.assertRaises(RecordsError):
            self.records.handoff(key)
        self.assertEqual(self.records.sealed(self.vault), [])

        original = Path(lesson["record"]).read_bytes()
        Path(lesson["record"]).write_text("课堂原文被改动")
        with self.assertRaisesRegex(RecordsError, "当前课堂原文已修改"):
            self.records.read_checkpoint(key)
        Path(lesson["record"]).write_bytes(original)
        Path(checkpoint["file"]).write_text("人工改写")
        with self.assertRaises(RecordsError):
            self.records.handoff(key, confirmed=True)
        self.records.checkpoint(key, checkpoint["text"])
        self.records.append(key, "user", "补问一题", "later")
        with self.assertRaises(RecordsError):
            self.records.handoff(key, confirmed=True)
        self.records.checkpoint(key, checkpoint["text"])
        self.records.set_recording(key, False)
        target = self.records.handoff(key, confirmed=True, request_id="single")
        self.assertNotEqual(target["session_id"], lesson["session_id"])
        self.assertEqual(target["topic"], lesson["topic"])
        self.assertEqual(target["handoff"], checkpoint["text"])
        self.assertFalse(target["recording"])
        self.assertEqual(self.records.handoff(key, confirmed=True, request_id="single")["id"], target["id"])
        self.assertEqual(self.records.prepare(self.vault, "Rust")["id"], target["id"])

    def test_existing_handoff_table_upgrades_for_new_progress_and_legacy_lookup(self):
        from teacher.learning import Learning

        profile = self.root / "legacy-profile"
        directory = profile / "teacher"
        directory.mkdir(parents=True)
        with closing(sqlite3.connect(directory / "records.sqlite3")) as database, database:
            database.execute(
                """CREATE TABLE handoffs (lesson_id TEXT NOT NULL, request_id TEXT NOT NULL,
                   target_id TEXT NOT NULL, PRIMARY KEY (lesson_id, request_id))"""
            )
        records = Records(profile)
        lesson_id = records.prepare(self.vault, "原有课堂")["id"]
        records.checkpoint(lesson_id, "先复习第一步")
        legacy = records.handoff(lesson_id, confirmed=True, request_id="legacy")
        self.assertIsNone(records.incoming_handoff(legacy["id"])["progress"])
        learning = Learning(profile, incoming_handoff=records.incoming_handoff)
        learning.update(lesson_id, goal="原有证据", stage="teach")
        self.assertEqual(learning.state(legacy["id"])["goal"], "原有证据")

        target = records.handoff(
            lesson_id,
            confirmed=True,
            request_id="current",
            capture_progress=lambda: learning.handoff_progress(lesson_id),
        )
        self.assertEqual(records.incoming_handoff(target["id"])["progress"]["goal"], "原有证据")

    def test_default_handoff_validates_checkpoint_before_retry_and_distinguishes_updates(self):
        key = self.records.prepare(self.vault, "研读交接")["id"]
        self.records.append(key, "user", "首轮练习", "start")
        self.records.checkpoint(key, "接下来复测分母。")
        first = self.records.handoff(key, confirmed=True)
        self.assertEqual(self.records.handoff(key, confirmed=True)["id"], first["id"])

        self.records.append(key, "user", "我又解了一道同类题", "later")
        with self.assertRaisesRegex(RecordsError, "已过期"):
            self.records.handoff(key, confirmed=True)
        self.records.checkpoint(key, "接下来复测分母。")
        second = self.records.handoff(key, confirmed=True)
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(self.records.handoff(key, confirmed=True)["id"], second["id"])

        self.records.checkpoint(key, "接下来比较两个条件事件。")
        third = self.records.handoff(key, confirmed=True)
        self.assertNotIn(third["id"], {first["id"], second["id"]})
        self.assertEqual(third["handoff"], "接下来比较两个条件事件。")

    def test_legacy_session_key_reuses_current_checkpoint_and_explicit_exact_retry(self):
        lesson = self.records.prepare(self.vault, "旧交接")
        key = lesson["id"]
        self.records.checkpoint(key, "下一步复盘。")
        legacy = self.records.handoff(key, confirmed=True, request_id=lesson["session_id"])
        self.assertEqual(self.records.handoff(key, confirmed=True)["id"], legacy["id"])

        self.records.append(key, "user", "后来继续练习", "later")
        with self.assertRaisesRegex(RecordsError, "已过期"):
            self.records.handoff(key, confirmed=True)
        self.assertEqual(
            self.records.handoff(key, confirmed=True, request_id=lesson["session_id"])["id"], legacy["id"]
        )
        self.records.checkpoint(key, "下一步复盘。")
        current = self.records.handoff(key, confirmed=True)
        self.assertNotEqual(current["id"], legacy["id"])
        self.assertEqual(self.records.handoff(key, confirmed=True)["id"], current["id"])

    def test_pressure_once_and_session_association(self):
        configure_topic_vault(self.vault, "压力", create=True)
        lesson = self.records.prepare(self.vault, "压力")
        key = lesson["id"]
        self.assertFalse(self.records.pressure(key, used=699, limit=1000))
        self.assertTrue(self.records.pressure(key, used=700, limit=1000))
        self.assertFalse(Records(self.profile).pressure(key, compressed=True))
        self.records.bind_session(key, "new-session", parent_id=lesson["session_id"])
        self.assertFalse(self.records.pressure(key, compressed=True))
        self.assertFalse(self.records.pressure(key, session_id=lesson["session_id"], compressed=True))
        self.assertFalse(self.records.take_pressure_notice(key, session_id=lesson["session_id"]))
        with self.records._connect(write=True) as connection:
            connection.execute(
                "UPDATE sessions SET notice = ? WHERE id = ?", ("上下文已达 70%", lesson["session_id"])
            )
        with use_language("en"):
            self.assertIn(
                "Context usage reached 70%. Ask the teacher",
                Records(self.profile).take_pressure_notice(key, session_id="new-session"),
            )
        self.assertFalse(self.records.take_pressure_notice(key, session_id="new-session"))
        other = self.records.prepare(self.vault, "压力", new=True)
        self.assertTrue(self.records.pressure(other["id"], compressed=True))
        with self.assertRaises(RecordsError):
            self.records.bind_session(other["id"], "new-session")

    def test_exclusive_lease_recovers_after_process_exit_and_rejects_links(self):
        lesson = self.records.prepare(self.vault, "锁")
        key = lesson["id"]
        child = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys; from pathlib import Path; from teacher.records import Records; "
                "records = Records(Path(sys.argv[1])); lock = records.lease(sys.argv[2]); "
                "lock.__enter__(); print('ready'); sys.stdin.read()",
                str(self.profile),
                key,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            with self.assertRaises(BusyError):
                self.records.append(key, "user", "并发写入", "blocked")
        finally:
            child.kill()
            child.communicate(timeout=5)
        self.assertTrue(self.records.append(key, "user", "接管", "recovered"))
        outside = self.root / "外部.md"
        outside.write_text("保留")
        (self.vault / "引用.md").symlink_to(outside)
        os.link(outside, self.vault / "硬链接.md")
        for path in ("引用.md", "硬链接.md"):
            with self.assertRaises(LibraryError):
                self.records.bind_record(key, path)
        self.assertEqual(outside.read_text(), "保留")
        with self.assertRaises(LibraryError):
            safe_path(self.vault, "../外部.md")


if __name__ == "__main__":
    unittest.main()
