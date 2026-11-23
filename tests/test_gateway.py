import base64
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import unquote, urlparse
import unittest
from unittest.mock import patch

from teacher.gateway import (
    GatewayError,
    MobileClassroom,
    MobileSettings,
    app_artifact,
    capture_media,
    capture_product,
)
from teacher.i18n import use_language
from teacher.learning import Learning
from teacher.library import BusyError
from teacher.records import Records


class ClassroomFixture:
    def __init__(self, records, learning):
        self.records = records
        self.learning = learning
        self.calls = []

    def handoff_progress(self, lesson_id):
        return self.learning.handoff_progress(lesson_id)

    def turn(self, lesson_id, text, *, event_id, expected_id=None, attachments=()):
        self.calls.append((lesson_id, text, event_id, expected_id, attachments))
        self.records.append(lesson_id, "user", text, event_id)
        result = self.learning.receive(lesson_id, text or "附件", event_id, expected_id=expected_id)
        return result or "当前课堂原文：" + "；".join(
            item["text"] for item in self.records.history(lesson_id) if item["role"] == "user"
        )


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.profile, self.vault, self.cache = (root / name for name in ("profile", "vault", "cache"))
        for directory in (self.profile, self.vault, self.cache):
            directory.mkdir()
        self.configure_language("zh")
        self.records = Records(self.profile)
        self.learning = Learning(self.profile, shuffle=lambda values: None)
        self.tutor = ClassroomFixture(self.records, self.learning)
        self.settings = MobileSettings("authorized", "private-chat", self.vault)
        self.role_jobs = set()
        self.mobile = self.build()
        self.output = []
        self.lesson = self.records.prepare(self.vault, "普通统计")["id"]

    def configure_language(self, language):
        (self.profile / "config.yaml").write_text(
            json.dumps({"teacher": {"language": language}}), encoding="utf-8",
        )

    def build(self):
        return MobileClassroom(
            self.profile,
            self.settings,
            self.tutor,
            pending=self.learning.pending,
            roles_busy=self.role_jobs.__contains__,
            media_roots=(self.cache,),
        )

    def submit(
        self,
        identifier,
        text,
        *,
        sender="authorized",
        chat="private-chat",
        kind="dm",
        media=None,
        platform="feishu",
        reply=None,
    ):
        event = SimpleNamespace(
            text=text,
            message_id=identifier,
            media_urls=media or [],
            reply_to_message_id=reply,
            source=SimpleNamespace(
                platform=SimpleNamespace(value=platform),
                chat_type=kind,
                chat_id=chat,
                is_bot=False,
                thread_id=None,
                user_id=sender,
                user_id_alt=None,
            ),
        )
        return self.mobile.submit(event)

    async def send(self, chat, source, part):
        self.assertEqual(chat, self.settings.chat)
        receipt = f"platform-{len(self.output)}"
        self.output.append((source, part, receipt))
        return receipt

    async def drain(self):
        while await self.mobile.step(self.send):
            pass

    async def test_english_commands_and_previously_sent_chinese_cards(self):
        self.submit("use", f"/course use {self.lesson}")
        self.learning.update(self.lesson, stage="probe")
        question = self.learning.quiz(self.lesson, "选标签", "single", dict(
            prompt="选一项", options=[dict(label="对", correct=True), dict(label="错", correct=False)],
            explanation="解析", hint="提示",
        ))
        self.submit("answer", "1")
        await self.drain()
        _, card, receipt = self.output[-1]
        self.assertEqual(card["choices"], [["继续", "继续"]])

        self.configure_language("en")
        accepted = self.mobile.card_click(SimpleNamespace(
            context=SimpleNamespace(open_chat_id=self.settings.chat, open_message_id=receipt),
            operator=SimpleNamespace(open_id=self.settings.sender),
            action=SimpleNamespace(value={"question": question["id"], "phase": "feedback", "answer": "继续"}),
        ))
        self.assertTrue(accepted)
        await self.drain()
        self.assertIsNone(self.learning.pending(self.lesson))

        with use_language("zh"):
            self.records.checkpoint(self.lesson, "下节继续。")
        self.submit("handoff-file", "/course file handoff")
        self.submit("help", "/help")
        self.submit("list", "/course list")
        await self.drain()
        self.assertTrue(self.output[-3][1]["name"].startswith("教师续课说明-"))
        self.assertEqual(base64.b64decode(self.output[-3][1]["data"]).decode(), "下节继续。")
        self.assertIn("/course file record", self.output[-2][1]["text"])
        self.assertIn("普通统计", self.output[-1][1]["text"])
        self.assertIn("(current)", self.output[-1][1]["text"])

    async def test_private_admission_scope_and_shared_classroom(self):
        self.assertFalse(self.submit("intruder", "secret", sender="other"))
        self.assertFalse(self.submit("group", "/", kind="group"))
        self.assertFalse(self.submit("alien-chat", "/", chat="other"))
        self.assertFalse(self.submit("alien-platform", "/", platform="relay"))
        self.assertIsNone(self.mobile.row("intruder"))
        self.assertTrue(self.submit("use", f"/course use {self.lesson}"))
        self.records.append(self.lesson, "user", "电脑输入的上下文", "desktop-1")
        self.assertTrue(self.submit("turn", "手机接着问"))
        self.assertFalse(self.submit("turn", "重复配送"))
        self.assertTrue(self.submit("shell", "/exec echo x"))
        self.assertTrue(self.submit("shell2", "!whoami"))
        await self.drain()
        self.assertTrue(any("电脑输入的上下文" in item[1]["text"] for item in self.output))
        self.assertEqual(len(self.tutor.calls), 1)
        self.assertEqual(self.tutor.calls[0][0:2], (self.lesson, "手机接着问"))
        self.role_jobs.add(self.lesson)
        candidate = self.records.prepare(self.vault, "另一节课", new=True)["id"]
        self.submit("busy", f"/course use {candidate}")
        await self.drain()
        self.assertIn("正在工作", self.output[-1][1]["text"])
        self.assertEqual(self.mobile.row("busy")["lesson"], self.lesson)
        self.role_jobs.clear()
        self.submit("new", "/course new 条件概率")
        await self.drain()
        other = self.mobile.row("new")["lesson"]
        self.assertNotEqual(other, self.lesson)
        self.submit("back", f"/course use {self.lesson}")
        self.submit("again", "电脑的上下文还在")
        await self.drain()
        self.assertEqual(self.tutor.calls[-1][0], self.lesson)
        self.assertIn("电脑输入的上下文", self.output[-1][1]["text"])
        self.submit("stop", "/course stop")
        await self.drain()
        self.assertTrue(self.records.history(self.lesson))
        self.submit("after-stop", "still recording?")
        self.assertIsNone(self.mobile.row("after-stop")["lesson"])

    async def test_quiz_number_notes_feedback_and_stale_prompt(self):
        self.submit("use", f"/course use {self.lesson}")
        self.learning.update(self.lesson, stage="probe")
        first = self.learning.quiz(
            self.lesson,
            "稳定编号",
            "multi",
            dict(
                prompt="请选择两项",
                options=[dict(label=s, correct=s != "x, y") for s in ("x, y", "4", "label / separator")],
                explanation="验证解析",
                hint="算法",
            ),
        )
        self.submit("incorrect", f"/answer {first['id']} 9")
        await self.drain()
        self.assertIn("编号超出", self.output[-1][1]["text"])
        first_receipt = self.output[-1][2]
        self.submit("answer", f"/answer {first['id']} 2，3\n备注有 | 和逗号,")
        await self.drain()
        self.assertIn("独立答对", self.output[-1][1]["text"])
        self.assertIn("备注有 | 和逗号,", self.output[-1][1]["text"])
        self.assertEqual(self.learning.pending(self.lesson)["status"], "feedback")
        evidence = self.learning.state(self.lesson)["evidence"][-1]
        self.assertEqual(evidence["answer"], ["4", "label / separator"])
        self.submit("feedback", "继续", reply=self.output[-1][2])
        await self.drain()
        self.assertIsNone(self.learning.pending(self.lesson))
        second = self.learning.ask(self.lesson, "还想解释什么？", "text")
        self.submit("stale", "上一条回复", reply=first_receipt)
        await self.drain()
        self.assertIn("题目已结束", self.output[-1][1]["text"])
        self.assertEqual(self.learning.pending(self.lesson)["id"], second["id"])
        self.submit("cancel", "取消")
        await self.drain()
        self.assertIn("已取消", self.output[-1][1]["text"])

    async def test_referenced_prompt_remains_bound_to_its_lesson_after_switch(self):
        self.submit("use-first", f"/course use {self.lesson}")
        self.learning.update(self.lesson, stage="probe")
        question = self.learning.quiz(
            self.lesson,
            "编号",
            "single",
            dict(
                prompt="选一个",
                options=[dict(label="乙", correct=False), dict(label="4", correct=True)],
                explanation="比较内容",
                hint="核对标签",
            ),
        )
        self.submit("invalid-first", f"/answer {question['id']} 9")
        await self.drain()
        receipt = self.output[-1][2]
        second = self.records.prepare(self.vault, "另一课堂", new=True)["id"]
        second_question = self.learning.ask(second, "第二节课的待回答问题", "text")
        self.submit("use-second", f"/course use {second}")
        self.submit("first-quoted-answer", "2", reply=receipt)
        await self.drain()
        self.assertEqual(self.tutor.calls[-1][0], self.lesson)
        self.assertIn("独立答对", self.output[-1][1]["text"])
        self.assertEqual(self.learning.pending(second)["id"], second_question["id"])
        self.submit("second-answer", "这是第二节课的答案")
        await self.drain()
        self.assertEqual(self.tutor.calls[-1][0], second)

    async def test_attachment_parts_and_immutable_published_snapshot(self):
        self.submit("use", f"/course use {self.lesson}")
        png = self.cache / "fixture.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        note = self.cache / "输入.md"
        note.write_text("# 中文资料\n概率", encoding="utf-8")
        self.submit("media", "请查看附件", media=[str(png), str(note)])
        note.unlink()
        await self.drain()
        parts = self.tutor.calls[-1][4]
        self.assertEqual(parts[0]["type"], "image_url")
        self.assertEqual(base64.b64decode(parts[0]["image_url"]["url"].split(",")[1]), png.read_bytes())
        self.assertIn("# 中文资料", parts[1]["text"])
        directory = self.vault / "viz"
        directory.mkdir()
        product = directory / "figure.png"
        data = png.read_bytes()
        product.write_bytes(data)
        self.submit("file", "/course file viz/figure.png")
        product.write_bytes(b"new version")
        await self.drain()
        self.assertEqual(base64.b64decode(self.output[-1][1]["data"]), data)
        self.assertEqual(self.output[-1][1]["type"], "image")
        outside = self.vault / "outside.md"
        outside.touch()
        with self.assertRaises(GatewayError):
            capture_product(self.vault, "viz/../outside.md")
        (directory / "symlink").symlink_to(outside)
        with self.assertRaises(GatewayError):
            capture_product(self.vault, "viz/symlink")
        (directory / "hardlink").hardlink_to(outside)
        with self.assertRaises(GatewayError):
            capture_product(self.vault, "viz/hardlink")
        with self.assertRaises(GatewayError):
            capture_media([str(png)] * 11, [self.cache])
        big = self.cache / "big.txt"
        with big.open("wb") as stream:
            stream.truncate(20 * 1024 * 1024 + 1)
        with self.assertRaises(GatewayError):
            capture_media([str(big)], [self.cache])

    async def test_app_artifact_directive_points_to_scoped_snapshot(self):
        folder = self.vault / "viz"
        folder.mkdir()
        product = folder / "复习 图.pdf"
        product.write_bytes(b"original PDF fixture")
        first = app_artifact(self.profile, self.vault, "viz/复习 图.pdf")
        self.assertEqual(app_artifact(self.profile, self.vault, "viz/复习 图.pdf"), first)
        path = Path(unquote(urlparse(first.removeprefix("MEDIA:")).path))
        self.assertTrue(path.is_relative_to(self.profile))
        self.assertEqual(path.name, product.name)
        product.write_bytes(b"new fixture")
        self.assertEqual(path.read_bytes(), b"original PDF fixture")
        self.assertNotEqual(app_artifact(self.profile, self.vault, "viz/复习 图.pdf"), first)
        with self.assertRaises(GatewayError):
            app_artifact(self.profile, self.vault, "../config.yaml")

    async def test_card_identity_full_labels_and_inline_notes(self):
        self.submit("open", f"/switch {self.lesson}")
        self.learning.update(self.lesson, stage="probe")
        question = self.learning.quiz(self.lesson, "选标签", "single", dict(
            prompt="选一项", options=[dict(label=s, correct=(n == 0)) for n, s in enumerate(("x, y | z", "4"))],
            explanation="私有解析", hint="提示",
        ))
        self.submit("invalid", f"/answer {question['id']} 9")
        await self.drain()
        _, card, receipt = self.output[-1]
        self.assertEqual(card["choices"][:2], [["x, y | z", "1"], ["4", "2"]])
        self.assertNotIn("私有解析", json.dumps(card, ensure_ascii=False))
        self.submit("label-and-note", "x, y | z | 我的理由")
        await self.drain()
        self.assertIn("独立答对", self.output[-1][1]["text"])
        self.assertEqual(self.learning.state(self.lesson)["evidence"][-1]["note"], "我的理由")

        def click(source, phase, answer, user="authorized", chat="private-chat"):
            return self.mobile.card_click(SimpleNamespace(
                operator=SimpleNamespace(open_id=user),
                context=SimpleNamespace(open_message_id=source, open_chat_id=chat),
                action=SimpleNamespace(value=dict(question=question["id"], phase=phase, answer=answer)),
            ))
        feedback = self.output[-1][2]
        self.assertFalse(click(receipt, "open", "2"))
        self.assertFalse(click(feedback, "feedback", "继续", user="stranger"))
        self.assertFalse(click(feedback, "feedback", "继续", chat="elsewhere"))
        self.assertFalse(click("unknown", "feedback", "继续"))
        self.assertTrue(click(feedback, "feedback", "继续"))
        self.assertFalse(click(feedback, "feedback", "继续"))
        await self.drain()
        self.assertEqual(self.output[-1][0], feedback)
        self.assertIsNone(self.learning.pending(self.lesson))

    async def test_private_lesson_commands_and_exported_files(self):
        self.submit("list", "/list")
        self.submit("use", "/switch 1")
        self.assertEqual(self.mobile.row("use")["lesson"], self.lesson)
        self.records.append(self.lesson, "assistant", "已经完成求导。", "manual")
        self.records.checkpoint(self.lesson, "下次验证二阶导数。")
        self.submit("record", "/course file 记录")
        self.submit("checkpoint", "/course file 续课说明")
        exported = lambda name: base64.b64decode(json.loads(self.mobile.row(name)["output"])[0]["data"]).decode()
        self.assertIn("已经完成求导", exported("record"))
        self.assertEqual(exported("checkpoint"), "下次验证二阶导数。")
        self.submit("seal", "/lesson-end")
        self.assertEqual(self.mobile.row("seal")["lesson"], self.lesson)
        self.assertEqual(len(self.records.sealed(self.vault)), 1)
        self.records.checkpoint(self.lesson, "下次验证二阶导数。")
        self.learning.update(self.lesson, model={"model": "原课设置"}, tool_allowlist=[])
        current_model = {"provider": "fixture", "model": "当前模型", "reasoning_effort": "low"}
        self.tutor.handoff_progress = lambda key: {**self.learning.handoff_progress(key), "model": current_model}
        self.submit("next", "/lesson-next")
        target = self.mobile.row("next")["lesson"]
        self.assertNotEqual(self.lesson, target)
        self.assertEqual(self.learning.state(target)["model"], current_model)
        self.assertEqual(self.learning.state(target)["tool_allowlist"], [])
        self.submit("another", "复习第二个例子")
        await self.drain()
        self.assertEqual([row[0] for row in self.tutor.calls], [target, target])
        self.assertIn("下次验证二阶导数", self.tutor.calls[0][1])
        stopped = []
        async def interrupt(lesson):
            stopped.append(lesson)
            return "已停止"
        self.tutor.interrupt = interrupt
        await asyncio.to_thread(self.submit, "stop-turn", "/stop")
        self.assertEqual(stopped, [target])
        self.submit("after-stop", "仍在新课堂")
        self.assertEqual(self.mobile.row("after-stop")["lesson"], target)

    async def test_resume_delivers_existing_question_and_role_work_holds_input(self):
        question = self.learning.ask(self.lesson, "怎么讲解？", "single", ["举例", "推导"])
        self.submit("open", f"/switch {self.lesson}")
        await self.drain()
        self.assertEqual(len(self.output), 2)
        self.assertEqual(self.output[-1][1]["prompt_id"], question["id"])
        with self.mobile.database() as db:
            self.assertIsNone(self.mobile.referenced_prompt(db, self.output[0][2]))
            self.assertEqual(self.mobile.referenced_prompt(db, self.output[1][2]), (self.lesson, question["id"]))
        self.role_jobs.add(self.lesson)
        self.submit("student", "想看配图")
        self.assertFalse(await self.mobile.step(self.send))
        self.assertEqual(self.mobile.row("student")["state"], "queued")
        self.role_jobs.clear()
        await self.drain()
        self.assertEqual(self.tutor.calls[-1][1], "自填 想看配图")

    async def test_mobile_retry_keeps_the_custom_answer_wire_language(self):
        self.configure_language("en")
        self.learning.ask(self.lesson, "How should we learn?", "single", ["Examples", "Proofs"])
        self.submit("open", f"/switch {self.lesson}")
        await self.drain()
        original = "Par un schéma\net deux exemples."
        self.submit("answer", original)
        with patch.object(self.tutor, "turn", side_effect=BusyError("writer busy")) as first:
            self.assertTrue(await self.mobile.step(self.send))
            self.assertEqual(self.mobile.row("answer")["state"], "queued")
        self.configure_language("zh")
        self.mobile = self.build()
        with patch.object(self.tutor, "turn", wraps=self.tutor.turn) as retry:
            await self.drain()
            self.assertEqual(first.call_args, retry.call_args)
        self.assertEqual(self.tutor.calls[-1][1], "other " + original)
        self.assertEqual(self.learning.state(self.lesson)["preferences"][-1]["answer"], original)

    async def test_interrupted_execution_and_media_delivery_recover(self):
        self.submit("use", f"/course use {self.lesson}")
        await self.drain()
        self.submit("queued", "waiting for worker")
        fresh = self.build()
        with fresh.worker():
            await fresh.step(self.send)
        self.assertEqual(fresh.row("queued")["state"], "ready")
        self.mobile = self.build()
        self.submit("lost-execution", "execution may have started")
        with self.mobile.database(write=True) as db:
            db.execute("UPDATE queue SET state='running' WHERE id='lost-execution'")
        self.mobile = self.build()
        with self.mobile.worker():
            await self.mobile.step(self.send)
        self.assertEqual(self.mobile.row("queued")["state"], "completed")
        self.assertEqual(len(self.tutor.calls), 1)
        self.assertEqual(self.mobile.review()[0]["reason"], "interrupted-execution")
        with self.assertRaises(GatewayError):
            self.mobile.review("lost-execution", "retry")
        self.submit("held", "following same-classroom turn")
        self.assertFalse(await self.mobile.step(self.send))
        self.mobile.review("lost-execution", "discard")
        await self.drain()
        self.assertEqual(len(self.tutor.calls), 2)
        self.submit("send-interrupted", "possible platform delivery")
        await self.mobile.step(self.send)
        with self.mobile.database(write=True) as db:
            db.execute("UPDATE queue SET state='sending' WHERE id='send-interrupted'")
        self.mobile = self.build()
        with self.mobile.worker():
            self.assertEqual(self.mobile.review()[0]["reason"], "interrupted-delivery")
        self.mobile.review("send-interrupted", "retry")
        await self.drain()
        self.assertEqual(len(self.tutor.calls), 3)
        self.assertEqual(self.mobile.row("send-interrupted")["state"], "completed")

    async def test_durable_created_admission_is_never_replayed(self):
        self.submit("use", f"/course use {self.lesson}")
        self.mobile.create_route = lambda *_: None
        self.assertTrue(self.submit("created-or-not", "/course new 无法确认的创建"))
        self.assertEqual(self.mobile.row("created-or-not")["state"], "routing")
        self.mobile = self.build()
        with self.mobile.worker():
            self.assertEqual(self.mobile.review()[0]["id"], "created-or-not")
        self.assertFalse(self.submit("created-or-not", "/course new 无法确认的创建"))
        self.assertEqual(len(self.records.recent()), 1)
