from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from teacher.i18n import use_language
from teacher.records import Records
from teacher.session_links import lesson_for_session


class SessionLinkTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(use_language("en"))
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name)
        self.records = Records(self.path / "profile")
        self.lesson = self.records.prepare(self.path / "vault", "压缩恢复", create=True, new=True)
        self.course = self.lesson["id"]
        self.records.bind_session(self.course, "canonical", parent_id=self.lesson["session_id"])
        self.records.append(self.course, "user", "压缩前的原文", "before-compression", session_id="canonical")

    def test_missing_notifications_repair_multiple_hops_once(self):
        path = ["canonical", "middle", "tip"]
        with patch("teacher.session_links.native_compression_path", return_value=path) as native:
            after_restart = Records(self.records.profile)
            self.assertEqual(lesson_for_session(after_restart, "tip")["id"], self.course)
            self.assertEqual(lesson_for_session(after_restart, "tip")["id"], self.course)
            self.assertEqual(native.call_count, 1)
        self.assertEqual(self.records.by_session("middle")["id"], self.course)
        self.assertEqual(self.records.get(self.course)["session_id"], "tip")
        self.records.append(self.course, "user", "压缩后的原文", "after-compression", session_id="tip")
        self.assertEqual(
            [row["text"] for row in self.records.history(self.course)], ["压缩前的原文", "压缩后的原文"]
        )

    def test_known_desktop_anchor_can_follow_new_tip_under_lesson_lease(self):
        with (
            patch(
                "teacher.session_links.native_compression_path", return_value=["canonical", "tip"]
            ) as native,
            self.records.lease(self.course),
        ):
            self.assertEqual(lesson_for_session(self.records, "canonical", follow=True)["session_id"], "tip")
        native.assert_called_once_with(self.records.profile, "canonical", forward=True)
        self.assertEqual(lesson_for_session(self.records, "canonical")["id"], self.course)

    def test_explicit_forks_and_replaced_course_session_do_not_inherit(self):
        with patch("teacher.session_links.native_compression_path", side_effect=lambda _, sid, **kw: [sid]):
            self.assertIsNone(lesson_for_session(self.records, "branch"))
            self.assertIsNone(lesson_for_session(self.records, "role"))
        self.records.bind_session(self.course, "separately-bound")
        with patch("teacher.session_links.native_compression_path", return_value=["canonical", "stale-tip"]):
            self.assertIsNone(lesson_for_session(self.records, "stale-tip"))
        self.assertEqual(self.records.get(self.course)["session_id"], "separately-bound")

    def test_conflicting_stored_courses_cannot_be_combined(self):
        second = self.records.prepare(self.path / "vault", "其他课程", new=True)
        self.records.bind_session(second["id"], "other-anchor", parent_id=second["session_id"])
        with patch(
            "teacher.session_links.native_compression_path", return_value=["canonical", "other-anchor", "tip"]
        ):
            with self.assertRaisesRegex(ValueError, "multiple lessons"):
                lesson_for_session(self.records, "tip")
        self.assertIsNone(self.records.by_session("tip"))


if __name__ == "__main__":
    unittest.main()
