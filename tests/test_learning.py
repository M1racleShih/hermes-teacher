"""Learner-visible interactions across shuffling, retries and a restarted classroom."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import os
import unittest
from unittest.mock import patch

from teacher.learning import Learning
from teacher.library import MATERIAL_PAGE_BYTES, configure_topic_vault, list_materials
from teacher.plugin import ClassroomPlugin, TOOLS
from teacher.questions import read_question


class QuizTests(unittest.TestCase):
    def setUp(self):
        language = patch.dict(os.environ, {"HERMES_TEACHER_LANGUAGE": "zh"})
        language.start()
        self.addCleanup(language.stop)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.now = 1
        self.core = Learning(self.path, clock=lambda: self.now, shuffle=lambda a: a.reverse())
        self.lesson = "lesson-1"
        self.core.receive(self.lesson, "学习条件概率", "open")
        self.private = dict(
            prompt="请选择质数",
            options=[dict(label=s, correct=s != "4") for s in ("2", "4", "7")],
            hint="从 2 开始尝试因数。",
            explanation="只有 1 和自身两个正因数。",
        )

    def open(self):
        return self.core.quiz(self.lesson, "质数判断", "multi", self.private, ttl=5)

    def test_shuffled_multi_score_and_feedback_confirmation(self):
        card = self.open()
        self.assertIn("A. 7\nB. 4\nC. 2", card["text"])
        for secret in ("correct", "explanation", self.private["explanation"], self.private["hint"]):
            self.assertNotIn(secret, json.dumps(card, ensure_ascii=False))
        result = self.core.receive(self.lesson, "3、1\n分两步\n逐项排除", "answer", expected_id=card["id"])
        self.assertIn("独立答对", result)
        self.assertIn(self.private["explanation"], result)
        self.assertIn("分两步\n逐项排除", result)
        self.assertEqual(self.core.receive(self.lesson, "3、1", "answer"), result)
        self.assertEqual(self.core.receive(self.lesson, "下一题？", "again"), result)
        self.assertEqual(len(self.core.state(self.lesson)["evidence"]), 1)
        restored = Learning(self.path)
        self.assertEqual(restored.pending(self.lesson)["status"], "feedback")
        self.assertIsNone(restored.receive(self.lesson, "继续", "ok"))
        self.assertIsNone(restored.pending(self.lesson))
        self.assertIn("结束", restored.receive(self.lesson, "1", "old", expected_id=card["id"]))

    def test_letter_answers_follow_display_order_and_accept_compact_multi(self):
        self.open()
        for i, answer in enumerate(("A A", "AD", "1B")):
            self.core.receive(self.lesson, answer, f"invalid-{i}")
            self.assertEqual(self.core.pending(self.lesson)["status"], "open")
        for i, answer in enumerate(("Ａｃ", "c、a", "1 3")):
            if i:
                self.open()
            reply = self.core.receive(self.lesson, answer + "\n保留 B 作为备注", f"graded-{i}")
            self.assertIn("独立答对", reply)
            self.assertIn("A. 7\nC. 2", reply)
            self.assertIn("保留 B 作为备注", reply)
            self.core.receive(self.lesson, "继续", f"continue-{i}")

    def test_quiz_controls_only_expose_information_already_revealed(self):
        card = self.open()
        restored = Learning(self.path, clock=lambda: self.now)
        view = restored.quiz_view(self.lesson)
        self.assertEqual(view["id"], card["id"])
        self.assertEqual([option["label"] for option in view["options"]], ["7", "4", "2"])
        self.assertTrue(all(set(option) == {"id", "label"} for option in view["options"]))
        for secret in ("correct", "hint", "explanation", self.private["hint"], self.private["explanation"]):
            self.assertNotIn(secret, json.dumps(view, ensure_ascii=False))
        self.assertIsNone(restored.quiz_view("unrelated lesson"))
        self.core.receive(self.lesson, "提示", "hint")
        self.assertEqual(restored.quiz_view(self.lesson)["hint"], self.private["hint"])
        self.core.receive(self.lesson, "1 3\n检查因数", "answer")
        feedback = restored.quiz_view(self.lesson)["feedback"]
        self.assertIn(self.private["explanation"], feedback)
        self.assertIn("检查因数", feedback)
        self.assertNotIn("读完请回复", feedback)
        self.core.receive(self.lesson, "继续", "ack", expected_id=card["id"])
        self.assertIsNone(restored.quiz_view(self.lesson))
        self.core.ask(self.lesson, "你想怎么学？", "text")
        self.assertIsNone(restored.quiz_view(self.lesson))

    def test_hint_unknown_wrong_and_invalid(self):
        card = self.open()
        self.assertIn("编号", self.core.receive(self.lesson, "4", "bad"))
        self.assertIn("不重复", self.core.receive(self.lesson, "1 1", "duplicate"))
        self.assertEqual(self.core.pending(self.lesson), card)
        self.assertIn(self.private["hint"], self.core.receive(self.lesson, "提示", "hint"))
        self.assertIn("提示后答对", self.core.receive(self.lesson, "1 3", "hinted"))
        self.core.receive(self.lesson, "确认", "confirmed")
        self.open()
        self.assertIn("答错", self.core.receive(self.lesson, "3", "wrong"))
        self.core.receive(self.lesson, "已读", "read")
        self.open()
        self.assertIn("不知道", self.core.receive(self.lesson, "不知道\n需要复习", "unknown"))
        self.assertEqual(
            [e["outcome"] for e in self.core.state(self.lesson)["evidence"]], ["hinted", "wrong", "unknown"]
        )

    def test_question_validation_after_display_order_changes(self):
        for field, reference in (
            ("hint", "先看选项 B"),
            ("explanation", "正确的是第 $2$ 项"),
            ("prompt", "B 项和 A 项相同吗？"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "重排"):
                self.core.quiz(self.lesson, "比例", "multi", {**self.private, field: reference})
        card = read_question(
            "@@题干 4:7 = x:28\n@@干扰 14\n@@正确 16\n@@提示 放大倍数。\n@@解析 4 乘 4 等于 16。"
        )
        q = self.core.quiz(self.lesson, "比例", "single", card)
        self.assertIn("A. 16", q["text"])
        self.assertIn("独立答对", self.core.receive(self.lesson, "A", "actual", expected_id=q["id"]))
        self.core.receive(self.lesson, "继续", "ack")
        ordered = self.core.quiz(self.lesson, "比例", "single", card, shuffle=False)
        self.assertIn("A. 14\nB. 16", ordered["text"])
        self.assertNotIn(card["explanation"], ordered["text"])
        self.assertIn("B. 16", self.core.receive(self.lesson, "B", "ordered", expected_id=ordered["id"]))

    def test_failed_question_hides_private_output_and_can_be_retried(self):
        class Model:
            def complete_structured(self, **kwargs):
                raise ValueError("私有命题载荷")

            def complete(self, *args, **kwargs):
                return type("Result", (), {"text": "不可公开的未成形解析"})()

        plugin = ClassroomPlugin(type("Context", (), {"llm": Model()})(), self.path)
        sid = plugin.records.prepare(self.path, "比例")["session_id"]
        args = {"objective": "比较比例", "mode": "single"}
        plugin.before(session_id=sid, turn_id="a", user_message="测试比例")
        self.assertIn("error", plugin.invoke("teacher_quiz", args, sid))
        reply = plugin.transform(session_id=sid, turn_id="a", response_text="老师临时猜测的题面")
        self.assertIn("重新出题", reply)
        self.assertNotIn("私有", reply)
        plugin.end(session_id=sid, turn_id="a")

        class Restored:
            def complete_structured(self, **kwargs):
                return type("Result", (), {"parsed": None})()

            def complete(self, *args, **kwargs):
                if kwargs["purpose"] == "private-review":
                    return type("Result", (), {"text": "通过"})()
                return type(
                    "Result",
                    (),
                    {
                        "text": "@@题干 2+2 等于多少？\n@@正确 4\n@@干扰 3\n@@解析 两个二相加为四。\n@@提示 计数。"
                    },
                )()

        plugin.ctx.llm = Restored()
        plugin.before(session_id=sid, turn_id="b", user_message="重新出题")
        self.assertEqual(plugin.invoke("teacher_quiz", args, sid)["kind"], "quiz")
        plugin.end(session_id=sid, turn_id="b")

    def test_review_rejects_wrong_rounding_before_publication(self):
        from teacher.questions import make_question

        reviews = []

        class Reviewer:
            def complete_structured(self, **kwargs):
                return SimpleNamespace(
                    parsed=dict(
                        prompt="8/55.5 等于百分之几？",
                        options=[dict(label="14.3%", correct=True), dict(label="20%", correct=False)],
                        hint="化为百分比",
                        explanation="约 14.4%。",
                    )
                )

            def complete(self, messages, **kwargs):
                if kwargs["purpose"] == "private-review":
                    reviews.append(messages[-1]["content"])
                    return SimpleNamespace(text="重出，按一位小数应为 14.4%" if len(reviews) == 1 else "通过")
                self_test.assertIn("14.4%", messages[-1]["content"])
                return SimpleNamespace(
                    text="@@题干 8/55.5 按一位小数是多少？\n@@正确 14.4%\n@@干扰 20.0%\n@@提示 化为百分比。\n@@解析 约 14.414%，保留一位为 14.4%。"
                )

        self_test = self
        card = make_question(
            Reviewer(),
            self.core,
            self.lesson,
            "比例",
            self.core.state(self.lesson),
            {"mode": "single", "objective": "百分比"},
        )
        self.assertEqual(len(reviews), 2)
        self.assertNotIn("14.3%", card["text"])
        self.assertIn("B. 14.4%", card["text"])

    def test_context_notice_uses_native_usage(self):
        plugin = ClassroomPlugin(None, self.path)
        vault = configure_topic_vault(self.path, "有理数", create=True)
        card = plugin.records.prepare(vault, "有理数")
        sid, key = card["session_id"], card["id"]
        plugin.before(session_id=sid, turn_id="turn", user_message="学习整数")
        plugin.usage(session_id=sid, usage={"prompt_tokens": 699}, context_length=1000)
        self.assertFalse(plugin.records.take_pressure_notice(key, session_id=sid))
        plugin.usage(session_id=sid, usage={"prompt_tokens": 700}, context_length=1000)
        reply = plugin.transform(session_id=sid, turn_id="turn", response_text="今天到这里。")
        self.assertIn("70%", reply)
        self.assertIn("/lesson-next", reply)
        plugin.after(session_id=sid, turn_id="turn", assistant_response=reply)
        plugin.end(session_id=sid, turn_id="turn")
        plugin.usage(session_id=sid, usage={"prompt_tokens": 800}, context_length=1000)
        self.assertFalse(plugin.records.take_pressure_notice(key, session_id=sid))

    def test_cancel_expire_and_cross_lesson_replay(self):
        q = self.open()
        self.assertIn("其他课堂", self.core.receive("other", "2", "other", expected_id=q["id"]))
        self.assertIn("取消", self.core.receive(self.lesson, "取消", "cancel"))
        q2 = self.open()
        self.assertNotEqual(q2["id"], q["id"])
        self.assertIn("已结束", self.core.receive(self.lesson, "1", "delayed", expected_id=q["id"]))
        self.now += 5
        self.assertIn("超时", self.core.receive(self.lesson, "1", "expired"))
        self.assertIsNone(self.core.pending(self.lesson))
        self.assertEqual([], self.core.state(self.lesson)["evidence"])

    def test_preferences_preserve_actual_values_and_cancel(self):
        for seq, (mode, text, expected) in enumerate(
            [
                ("single", "2", ["讲授"]),
                ("multi", "2 1", ["讲授", "推导"]),
                ("multi", "2 自填 用配图\n分两步", ["讲授", "用配图\n分两步"]),
                ("text", "第一行\n第二行", "第一行\n第二行"),
                ("single", "自填 先复习", "先复习"),
            ]
        ):
            self.core.ask(self.lesson, "怎么学？", mode, ["推导", "讲授"])
            self.assertEqual(self.core.interaction_view(self.lesson)["kind"], "ask")
            self.assertIsNone(self.core.receive(self.lesson, text, str(seq)))
            self.assertEqual(self.core.state(self.lesson)["preferences"][-1]["answer"], expected)
        self.core.ask(self.lesson, "怎么学？", "text")
        self.core.receive(self.lesson, "取消", "x")
        self.assertEqual(len(self.core.state(self.lesson)["preferences"]), 5)
        self.assertIsNone(self.core.interaction_view(self.lesson))

    def test_plan_revisions_and_node_progress_require_actual_checks(self):
        def plan(nodes, edges=None):
            return self.core.plan(self.lesson, nodes, "从分母的意义出发。", "已核实的定义与来源", edges=edges)

        def probe(strand, objective, event, answer, **kwargs):
            question = self.core.quiz(self.lesson, objective, "multi", self.private, strand=strand, **kwargs)
            self.core.receive(self.lesson, answer, event, expected_id=question["id"])
            self.core.receive(self.lesson, "继续", event + "-ack", expected_id=question["id"])
            return question["id"]

        with self.assertRaisesRegex(ValueError, "探测"):
            plan(["后验"])
        gap = probe("条件", "条件的分母", "q1", "不知道", strands=["条件", "频数"])
        with self.assertRaisesRegex(ValueError, "条件、频数"):
            plan(["后验"])
        probe("条件", "固定样本分母", "q2", "1 3", follows=gap)
        probe("频数", "频数计数", "q3", "1 3")
        self.assertIn("提高难度", self.core.probe_progress(self.core.state(self.lesson))["频数"]["direction"])
        with self.assertRaisesRegex(ValueError, "频数"):
            plan(["后验"])
        with self.assertRaisesRegex(ValueError, "同一主线"):
            self.core.quiz(self.lesson, "总频数", "multi", self.private, strand="频数", follows=gap)
        gap = probe("频数", "总频数", "q4", "2")
        probe("频数", "相邻频数划分", "q5", "1 3", follows=gap)
        first = plan(["后验"])
        self.assertIn("```mermaid", first["text"])
        self.core.receive(self.lesson, "先补条件概率", "edit")
        self.assertEqual(self.core.state(self.lesson)["stage"], "revise")
        nodes = ["条件概率", "自然频数", "后验"]
        edges = [dict(prerequisite=node, dependent="后验") for node in nodes[:2]]
        for invalid in (
            edges[:1],
            edges + [dict(prerequisite="后验", dependent="条件概率")],
            edges + [dict(prerequisite="未知节点", dependent="后验")],
        ):
            with self.assertRaises(ValueError):
                plan(nodes, invalid)
        second = plan(nodes, edges)
        self.assertIn("第 2 版", second["text"])
        self.assertIn("N0 --> N2\n  N1 --> N2", second["text"])
        self.assertNotIn("N0 --> N1", second["text"])
        self.assertEqual(Learning(self.path).state(self.lesson)["plan_edges"], edges)
        self.assertEqual(self.core.state(self.lesson)["stage"], "plan")
        self.core.receive(self.lesson, "批准", "approve")
        self.open()
        self.core.receive(self.lesson, "提示", "h")
        self.core.receive(self.lesson, "1 3", "v1")
        self.core.receive(self.lesson, "继续", "next1")
        self.assertEqual(self.core.state(self.lesson)["node"], 0)
        self.open()
        self.core.receive(self.lesson, "1 3", "v2")
        self.core.receive(self.lesson, "继续", "next2")
        self.assertEqual(self.core.state(self.lesson)["node"], 1)

    def test_setup_questions_keep_ready_and_unambiguous_goal_starts(self):
        other = "fresh"
        for number, question in enumerate((
            "开课前如何选择思考强度？",
            "请讲讲怎样切换模型。",
            "How do I use /reasoning?",
        )):
            help_text = self.core.receive(other, question, f"setup-{number}", topic="贝叶斯")
            self.assertIn("/model" if number == 1 else "/reasoning", help_text)
            self.assertEqual(self.core.state(other)["stage"], "ready")
            self.assertFalse(self.core.state(other)["goal"])
            with self.assertRaisesRegex(ValueError, "学习目标"):
                self.core.quiz(other, "划分样本", "multi", self.private)
        self.core.receive(other, "开始吧", "start", topic="贝叶斯")
        self.assertEqual(self.core.state(other)["stage"], "probe")
        self.assertEqual(self.core.state(other)["goal"], "贝叶斯")
        self.core.receive("direct", "现在开始学习怎样从频数推导后验。", "learn")
        self.assertEqual(self.core.state("direct")["stage"], "probe")
        prompt = self.core.ask("clarify", "你想解决什么问题？", "text", purpose="goal")
        self.core.receive("clarify", "条件概率和反向条件概率的区别", "goal", expected_id=prompt["id"])
        self.assertEqual(self.core.state("clarify")["goal"], "条件概率和反向条件概率的区别")
        self.assertEqual(self.core.state("clarify")["preferences"], [])

    def test_material_pages_read_bound_vault_and_resume_utf8(self):
        plugin = ClassroomPlugin(None, self.path)
        vaults = [self.path / name for name in ("第一库", "第二库")]
        for index, vault in enumerate(vaults):
            (vault / "materials").mkdir(parents=True)
            (vault / "materials/数据.csv").write_text(f"来源,测量\n资料库{index},2\n")
        course = plugin.records.prepare(vaults[1], "测量")
        def material(**args):
            return plugin.invoke("teacher_material", args, course["session_id"])
        self.assertEqual(list_materials(vaults[1]), ["materials/数据.csv"])
        self.assertEqual(material(path="materials/数据.csv")["text"], "来源,测量\n资料库1,2\n")
        path = vaults[1] / "materials/长课.md"
        path.write_text("开头\n" * 151_000 + "最后一行\n")
        self.assertGreater(path.stat().st_size, 1_000_000)
        self.assertEqual(material(path="materials/长课.md", start_line=151_001, line_count=2)["text"], "最后一行\n")
        long_line = "x" + "甲" * MATERIAL_PAGE_BYTES + "\n最后一行"
        path.write_text(long_line)
        page = material(path="materials/长课.md", line_count=1)
        self.assertEqual(page["next"]["start_line"], 1)
        pieces = [page["text"]]
        while page["next"]:
            page = material(path="materials/长课.md", line_count=1, **page["next"])
            pieces.append(page["text"])
        self.assertEqual("".join(pieces), long_line)
        (vaults[1] / "materials/link.csv").symlink_to(vaults[0] / "materials/数据.csv")
        with self.assertRaises(ValueError):
            material(path="materials/link.csv")

    def test_plugin_learner_boundary_and_turn_release(self):
        class PrivateModel:
            def complete(inner, *args, **kwargs):
                return type("Result", (), {"text": "通过"})()

            def complete_structured(inner, **kwargs):
                return type("Result", (), {"parsed": self.private})()

        ctx = type("Context", (), {"llm": PrivateModel()})()
        plugin = ClassroomPlugin(ctx, self.path)
        lesson = plugin.records.prepare(self.path, "质数")
        sid = lesson["session_id"]
        pre = plugin.before(session_id=sid, turn_id="intro", user_message="学习质数")
        self.assertIn("课程状态", pre["context"])
        card = plugin.invoke("teacher_quiz", {"objective": "质数", "mode": "multi"}, sid)
        public = plugin.transform(session_id=sid, response_text="自由回复", turn_id="intro")
        self.assertEqual(public, card["text"])
        self.assertNotIn("correct", str(TOOLS))
        plugin.after(session_id=sid, turn_id="intro", assistant_response=public)
        busy = plugin.before(session_id=sid, turn_id="concurrent", user_message="1")
        self.assertEqual(busy["action"], "block")
        self.assertIn("写者", busy["message"])
        plugin.end(session_id=sid, turn_id="intro")
        self.assertNotIn(self.private["explanation"], Path(lesson["record"]).read_text())
        submitted = plugin.before(
            session_id=sid,
            turn_id="answer",
            user_message="〔课堂作答 " + card["id"] + "〕\n不知道\n分母不清楚",
        )
        self.assertIn(self.private["explanation"], submitted["message"])
        self.assertIn("分母不清楚", Path(lesson["record"]).read_text())
        self.assertEqual(plugin.guard(session_id=sid, tool_name="terminal")["action"], "block")
        self.assertIsNone(plugin.guard(session_id="other", tool_name="terminal"))


class LanguageTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HERMES_TEACHER_LANGUAGE", None)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name)
        self.core = Learning(self.profile, shuffle=lambda options: None)
        self.private = read_question(
            "@@PROMPT Which number is prime?\n@@CORRECT 2\n@@DISTRACTOR 4\n"
            "@@HINT Check the factors.\n@@EXPLANATION A prime has two positive factors."
        )

    def test_default_english_goal_and_plan_with_bilingual_controls(self):
        self.assertIn("/model", self.core.receive("lesson", "How do I switch models?", "setup"))
        self.assertEqual(self.core.state("lesson")["stage"], "ready")
        self.assertIsNone(self.core.receive("lesson", "Start lesson", "start", topic="Prime numbers"))
        self.assertEqual(self.core.state("lesson")["goal"], "Prime numbers")
        first = self.core.quiz("lesson", "Recognize a prime", "single", self.private, strand="Factors")
        self.assertIn("Reply with one option letter", first["text"])
        self.assertNotIn(self.private["hint"], first["text"])
        answer = self.core.receive("lesson", "A", "independent", expected_id=first["id"])
        self.assertIn("Correct without a hint.", answer)
        self.assertIsNone(self.core.receive("lesson", "continue", "ack"))
        second = self.core.quiz("lesson", "Factor a product", "single", self.private, strand="Factors")
        unknown = self.core.receive("lesson", "I don’t know\nJe ne vois pas les facteurs.", "unknown")
        self.assertIn("I don't know.", unknown)
        self.assertIn("Je ne vois pas les facteurs.", unknown)
        self.assertIsNone(self.core.receive("lesson", "继续", "ack2"))
        self.core.quiz("lesson", "Distinguish prime and composite", "single", self.private,
                       strand="Factors", follows=second["id"])
        self.assertEqual(self.core.receive("lesson", "hint", "hint"), "Hint\n\n" + self.private["hint"])
        self.assertIn("Correct with a hint.", self.core.receive("lesson", "A", "hinted"))
        self.assertIsNone(self.core.receive("lesson", "Read", "ack3"))
        plan = self.core.plan("lesson", ["Prime numbers"], "Begin with examples.", "Verified source")
        self.assertIn("Teaching plan · Version 1", plan["text"])
        self.assertIn("Reply “approve”", plan["text"])
        self.assertIsNone(self.core.receive("lesson", "Approve", "approved"))
        self.assertEqual(self.core.state("lesson")["stage"], "teach")

    def test_existing_chinese_feedback_and_english_custom_preference(self):
        self.core.receive("lesson", "I’d like to learn number theory", "start")
        with patch.dict(os.environ, {"HERMES_TEACHER_LANGUAGE": "zh"}):
            quiz = self.core.quiz("lesson", "Recognize a prime", "single", self.private)
            self.assertIn("回复一个选项字母", quiz["text"])
            answer = self.core.receive("lesson", "A", "answer")
        restarted = Learning(self.profile)
        self.assertEqual(restarted.receive("lesson", "A", "answer"), answer)
        self.assertIn("独立答对", restarted.interaction_view("lesson")["feedback"])
        self.assertNotIn("读完请回复", restarted.interaction_view("lesson")["feedback"])
        self.assertIsNone(restarted.receive("lesson", "continue", "ack"))
        ask = restarted.ask("lesson", "How do you want to learn?", "multi", ["Examples", "Proofs"])
        self.assertIn("1 2 custom your answer", ask["text"])
        raw = "1 other Avec des schémas\net des exemples."
        self.assertIsNone(restarted.receive("lesson", raw, "preference"))
        self.assertEqual(restarted.state("lesson")["preferences"][-1]["answer"],
                         ["Examples", "Avec des schémas\net des exemples."])
        restarted.ask("lesson", "Another preference?", "text")
        self.assertEqual(restarted.receive("lesson", "cancel", "cancel"), "Interaction cancelled.")

    def test_profile_language_and_french_goal_are_independent(self):
        config = self.profile / "config.yaml"
        config.write_text(json.dumps({"teacher": {"language": "zh-CN"}}))
        calls = {}
        private = {
            "prompt": "Quel nombre est premier ?",
            "options": [{"label": "2", "correct": True}, {"label": "4", "correct": False}],
            "hint": "Comptez les diviseurs.",
            "explanation": "Deux a exactement deux diviseurs positifs.",
        }

        class Model:
            def complete_structured(self, *, instructions, input, **kwargs):
                calls["instructions"] = instructions
                calls["request"] = json.loads(input[0]["text"])
                return SimpleNamespace(parsed=private)

            def complete(self, messages, **kwargs):
                return SimpleNamespace(text="PASS")

        plugin = ClassroomPlugin(SimpleNamespace(llm=Model()), self.profile)
        info = plugin.records.prepare(self.profile, "Probabilités")
        session = info["session_id"]
        intent = "Je veux comprendre les probabilités conditionnelles."
        start = plugin.before(session_id=session, turn_id="open", user_message=intent)
        self.assertIn("Use the language the learner requests or uses", start["context"])
        self.assertIn("Simplified Chinese", start["context"])
        self.assertEqual(plugin.learning.state(info["id"])["stage"], "ready")
        card = plugin.invoke(
            "teacher_ask", {"mode": "text", "purpose": "goal", "prompt": "Que voulez-vous apprendre ?"}, session,
        )
        view = json.loads(plugin.command("interaction-state", session_id=session))
        self.assertEqual(view["language"], "zh")
        self.assertEqual(view["interaction"]["prompt"], "Que voulez-vous apprendre ?")
        plugin.end(session_id=session, turn_id="open")
        reply = plugin.before(
            session_id=session, turn_id="goal", user_message=f"[Classroom answer {card['id']}]\n{intent}",
        )
        self.assertIn(intent, reply["context"])
        self.assertEqual(plugin.learning.state(info["id"])["goal"], intent)
        self.assertEqual(json.loads(plugin.command("quiz-state", session_id=session)), {"quiz": None, "language": "zh"})
        objective = "Vérifier les prérequis en français."
        question = plugin.invoke("teacher_quiz", {"mode": "single", "objective": objective}, session)
        self.assertIn("Follow any learner language request", calls["instructions"])
        self.assertEqual(calls["request"]["goal"], intent)
        self.assertEqual(calls["request"]["objective"], objective)
        self.assertEqual(calls["request"]["classroom_language"], "Simplified Chinese")
        self.assertIn(private["prompt"], question["text"])
        self.assertIn("回复一个选项字母", question["text"])
        self.assertNotIn(private["explanation"], question["text"])
        self.assertIn(intent, Path(info["record"]).read_text())
        plugin.end(session_id=session, turn_id="goal")
