from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from teacher.learning import Learning
from teacher.profile import initialize
from teacher.quiz_widget import interact
from teacher.records import Records
from teacher.runtime import Tutor, feedback_confirmation


class FixtureTutor:
    def __init__(self, profile):
        self.records = Records(profile)
        self.now = 100
        self.learning = Learning(profile, clock=lambda: self.now, shuffle=lambda options: None)
        self.calls = []

    def turn(self, lesson, text, *, expected_id, event_id):
        self.calls.append((lesson, text, expected_id, event_id))
        return self.learning.receive(lesson, text, event_id, expected_id=expected_id)


class QuizWidgetTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HERMES_TEACHER_LANGUAGE", None)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.tutor = FixtureTutor(self.root / "profile")
        self.marker = self.root / "active.json"
        self.configure_language("zh")
        self.course = self.lesson("canonical")
        self.switch("canonical")

    def configure_language(self, language):
        (self.tutor.records.profile / "config.yaml").write_text(
            json.dumps({"teacher": {"language": language}}), encoding="utf-8",
        )

    def lesson(self, session):
        vault = self.root / session
        records = self.tutor.records
        lesson = records.prepare(vault, session, create=True)
        records.bind_session(lesson["id"], session, parent_id=lesson["session_id"])
        self.tutor.learning.update(lesson["id"], canonical=session, stage="probe")
        return lesson["id"]

    def quiz(self, course, *, ttl=60):
        return self.tutor.learning.quiz(course, "质数", "multi", {
            "prompt": "选出质数。",
            "options": [{"label": value, "correct": value != "4"} for value in ("4", "5", "7")],
            "explanation": "找出只有两个正因数的整数。",
            "hint": "检查因数。",
        }, ttl=ttl)

    def switch(self, session):
        self.marker.write_text(json.dumps({"session_id": session}), encoding="utf-8")

    def request(self, action="read", *, session="canonical", **kwargs):
        return interact(self.tutor, self.marker, {
            "session_id": session, "action": action, "request_id": str(uuid.uuid4()), **kwargs,
        })

    def test_switch_exposes_only_the_active_course(self):
        first = self.quiz(self.course)
        second = self.quiz(self.lesson("other"))
        self.assertEqual(self.request()["card"]["id"], first["id"])
        self.switch("other")
        with self.assertRaisesRegex(ValueError, "当前会话已变化"):
            self.request()
        self.assertEqual(self.request(session="other")["card"]["id"], second["id"])
        with self.assertRaisesRegex(ValueError, "这道题已结束"):
            self.request("hint", session="other", question_id=first["id"])
        self.switch("ordinary")
        self.assertEqual(self.request(session="ordinary"), {"language": "zh", "classroom": False, "card": None})
        self.assertEqual(self.tutor.calls, [])

    def test_english_labels_and_answers_preserve_question_and_option_text(self):
        self.configure_language("en")
        question = self.quiz(self.course)
        read = self.request()
        self.assertEqual(read["language"], "en")
        self.assertEqual(read["card"]["prompt"], "选出质数。")
        result = self.request("unknown", question_id=question["id"], note="自选备注")
        self.assertEqual(self.tutor.calls[0][:3], (self.course, "I don't know\n自选备注", question["id"]))
        self.assertEqual(result["card"]["status"], "feedback")
        self.assertIsNone(self.request("continue", question_id=question["id"])["card"])
        self.assertEqual(self.tutor.calls[-1][1], "continue")

    def test_lost_native_retry_and_shared_confirmation_keep_the_admitted_wire_language(self):
        profile = initialize(self.root / "native-profile", self.root / "native-vault", language="en")
        self.tutor = Tutor(profile)
        self.course = self.lesson("wire-session")
        self.switch("wire-session")
        question = self.quiz(self.course)

        class Gateway:
            def __init__(self):
                self.admitted = {}
                self.submissions = []

            async def rpc(self, method, **params):
                if method == "session.resume":
                    return {}
                if method == "prompt.submit":
                    self.submissions.append(params)
                    identity = (params["session_id"], params["input_id"])
                    if identity not in self.admitted:
                        self.admitted[identity] = params
                        raise TimeoutError("The submit reply was lost")
                    if self.admitted[identity] != params:
                        raise ValueError("admission_conflict")
                    return {"admission_id": params["input_id"]}
                if method == "prompt.receipt":
                    return {"status": "terminal", "result": {"final_response": "Recovered receipt"}}
                raise AssertionError(method)

        gateway = Gateway()

        @asynccontextmanager
        async def connect_gateway():
            yield gateway

        native = SimpleNamespace(connect_gateway=connect_gateway)
        request = {"session": "wire-session", "question_id": question["id"], "request_id": str(uuid.uuid4())}
        with patch.dict(sys.modules, {"hermes_cli.gateway_client": native}), patch.object(
            Tutor, "_session", return_value="wire-session",
        ):
            with self.assertRaises(TimeoutError):
                self.request("hint", **request)
            self.configure_language("zh")
            self.tutor = Tutor(profile)
            recovered = self.request("hint", **request)
            self.assertEqual(recovered["language"], "zh")
            self.assertEqual(recovered["message"], "Recovered receipt")
            self.assertEqual(gateway.submissions[0], gateway.submissions[1])
            self.assertEqual(gateway.submissions[0]["text"], f"[Classroom answer {question['id']}]\nhint")
            with self.assertRaisesRegex(ValueError, "admission_conflict"):
                self.request("unknown", **request, note="Nouvelle réponse")
            self.assertEqual(
                gateway.submissions[2]["text"], f"[Classroom answer {question['id']}]\nI don't know\nNouvelle réponse",
            )

            self.tutor.learning.receive(self.course, "不知道", "synthetic-answer", expected_id=question["id"])
            with self.assertRaises(TimeoutError):
                self.request("continue", **request)
            self.configure_language("en")
            self.tutor = Tutor(profile)
            text, identity = feedback_confirmation(self.tutor.learning, self.course, question["id"])
            recovered = self.tutor.turn(self.course, text, expected_id=question["id"], event_id=identity)
            self.assertEqual(recovered, "Recovered receipt")
            self.assertEqual(gateway.submissions[-2], gateway.submissions[-1])
            self.assertEqual(gateway.submissions[-1]["input_id"], "teacher-confirm-" + question["id"])
            self.assertEqual(gateway.submissions[-1]["text"], f"〔课堂作答 {question['id']}〕\n继续")

    def test_timeout_and_stale_action_leave_the_replacement_open(self):
        previous = self.quiz(self.course, ttl=2)
        self.tutor.now += 3
        expired = self.request("hint", question_id=previous["id"])
        self.assertIn("超时", expired["message"])
        self.assertIsNone(expired["card"])
        replacement = self.quiz(self.course)
        with self.assertRaisesRegex(ValueError, "这道题已结束"):
            self.request("continue", question_id=previous["id"])
        self.assertEqual(self.request()["card"]["id"], replacement["id"])
        self.assertEqual(len(self.tutor.calls), 1)

    def test_twelfth_option_submits_letter_l(self):
        question = self.tutor.learning.quiz(self.course, "辨认数值", "single", {
            "prompt": "选择最大的数。",
            "options": [{"label": str(number), "correct": number == 31} for number in range(20, 32)],
            "explanation": "三十一最大。",
            "hint": "按从小到大比较。",
        })
        option = self.request()["card"]["options"][-1]
        response = self.request("answer", question_id=question["id"], options=[option["id"]])
        self.assertIn("独立答对", response["card"]["feedback"])
        self.assertEqual(self.tutor.calls[0][:3], (self.course, "L", question["id"]))

    def test_preference_options_and_custom_text_share_the_current_card(self):
        question = self.tutor.learning.ask(self.course, "怎么学？", "multi", ["推导", "讲授"])
        card = self.request()["card"]
        self.assertEqual(card["kind"], "ask")
        with self.assertRaisesRegex(ValueError, "课堂状态"):
            self.request("hint", question_id=question["id"])
        result = self.request(
            "answer", question_id=question["id"], options=[card["options"][1]["id"]], note="配图\n和例题"
        )
        self.assertIsNone(result["card"])
        self.assertEqual(
            self.tutor.learning.state(self.course)["preferences"],
            [{"question": "怎么学？", "answer": ["讲授", "配图\n和例题"], "custom": "配图\n和例题"}],
        )

    def test_canonical_and_current_linked_tip_allow_actions(self):
        records = self.tutor.records
        records.bind_session(self.course, "middle", parent_id="canonical")
        records.bind_session(self.course, "tip", parent_id="middle")
        question = self.quiz(self.course)
        with patch("teacher.quiz_widget.native_compression_path", return_value=["canonical", "middle", "tip"]):
            self.assertEqual(self.request()["card"]["id"], question["id"])
            self.switch("middle")
            self.assertFalse(self.request("hint", session="middle", question_id=question["id"])["classroom"])
            self.switch("tip")
            card = self.request(session="tip")["card"]
            picked = [option["id"] for option in card["options"][1:]]
            result = self.request(
                "answer", session="tip", question_id=question["id"], options=picked, note="第一行\n第二行"
            )
            self.assertIn("独立答对", result["card"]["feedback"])
            self.assertIsNone(self.request("continue", session="tip", question_id=question["id"])["card"])
        self.assertEqual(self.tutor.calls[0][:3], (self.course, "B C\n第一行\n第二行", question["id"]))
        self.assertEqual(self.tutor.calls[1], (self.course, "继续", question["id"], "teacher-confirm-" + question["id"]))

    def test_unbound_tip_and_explicit_branch_do_not_change_links(self):
        records = self.tutor.records
        records.bind_session(self.course, "previous-tip", parent_id="canonical")
        chain = ["canonical", "previous-tip", "unbound-tip"]
        with patch("teacher.quiz_widget.native_compression_path", return_value=chain):
            self.switch("previous-tip")
            self.assertFalse(self.request(session="previous-tip")["classroom"])
            self.switch("unbound-tip")
            self.assertFalse(self.request(session="unbound-tip")["classroom"])
            self.assertIsNone(records.by_session("unbound-tip"))
            self.assertEqual(records.get(self.course)["session_id"], "previous-tip")
            records.bind_session(self.course, "explicit-branch", parent_id="canonical")
            self.switch("explicit-branch")
            self.assertFalse(self.request(session="explicit-branch")["classroom"])
            self.switch("canonical")
            self.assertFalse(self.request()["classroom"])
        self.assertEqual(records.get(self.course)["session_id"], "explicit-branch")
        self.assertEqual(self.tutor.calls, [])

    def test_tip_with_another_classroom_in_the_chain_is_refused(self):
        self.lesson("other")
        self.tutor.records.bind_session(self.course, "tip", parent_id="canonical")
        with patch("teacher.quiz_widget.native_compression_path", return_value=["canonical", "other", "tip"]):
            self.assertFalse(self.request()["classroom"])
            self.switch("tip")
            self.assertFalse(self.request(session="tip")["classroom"])
        self.assertEqual(self.tutor.calls, [])
