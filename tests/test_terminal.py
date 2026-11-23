import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from teacher.i18n import use_profile
from teacher.terminal import Backend, TerminalControl, TerminalError, TerminalSelection, process_identity, store


class TerminalControlTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        (Path(work.name) / "config.yaml").write_text('{"teacher": {"language": "zh"}}', encoding="utf-8")
        self.control = TerminalControl(Path(work.name))
        self.lesson = "lesson-A"
        self.auth = {
            "token": "owner-token",
            "key": self.lesson,
            "lesson": self.lesson,
            "backend": "herdr",
            "socket": "/tmp/disposable-virtual.sock",
            "owner": "agent",
        }
        self.record = {
            "key": self.lesson,
            "lesson": self.lesson,
            "generation": "first-instance",
            "token": self.auth["token"],
            "terminal": "terminal-v1",
            "pane": "pane-v1",
            "server": [101],
            "pid": os.getpid(),
            "process": process_identity(os.getpid()),
            "state": "waiting",
        }
        store(self.control.path(self.auth["token"], "launcher"), self.auth)
        store(self.control.path(self.lesson, "run"), self.record)
        self.address = self.lesson + "@first-instance"
        identity = patch.object(Backend, "identity", return_value=[101])
        foreground = patch.object(Backend, "foreground", return_value=True)
        self.addCleanup(identity.stop)
        self.addCleanup(foreground.stop)
        identity.start()
        self.foreground = foreground.start()

    def test_default_english_terminal_error_preserves_protocol(self):
        (self.control.profile / "config.yaml").unlink()
        self.assertEqual(self.control.interact(self.address, "handoff")["owner"], "user")
        with self.assertRaisesRegex(TerminalError, "The user has taken over"):
            self.control.interact(self.address, "send", "still a draft")

    def test_handoff_is_persistent_and_prevents_later_automated_input(self):
        with patch.object(Backend, "send", return_value={"queued": True}) as sender:
            self.assertEqual(self.control.interact(self.address, "send", "中文\n$变量"), {"queued": True})
            sender.assert_called_once()
            handoff = self.control.interact(self.address, "handoff")
            self.assertEqual(handoff["owner"], "user")
            self.assertIn("terminal-v1 --takeover", handoff["attach"])
            reopened = TerminalControl(self.control.profile)
            with self.assertRaisesRegex(TerminalError, "用户已经接管"):
                reopened.interact(self.address, "send", "禁止再次输入")
            sender.assert_called_once()

    def test_old_handle_and_changed_foreground_are_rejected(self):
        with self.assertRaisesRegex(TerminalError, "失效"):
            self.control.interact(self.lesson + "@old", "keys", ["enter"])
        self.foreground.return_value = False
        with self.assertRaisesRegex(TerminalError, "前台身份已更换"):
            self.control.interact(self.address, "keys", ["enter"])
        self.assertEqual(self.control.list(), [])

    def test_control_sequences_are_kept_out_of_literal_paste(self):
        backend = Backend("core", "/tmp/disposable-virtual.sock")
        with use_profile(self.control.profile), patch.object(backend, "call", return_value={}) as call:
            with self.assertRaisesRegex(TerminalError, "控制键"):
                backend.send(self.record, "hi\x1b[201~")
            call.assert_not_called()
            raw = "第一行 $a\n第二行\t尾部"
            backend.send(self.record, raw)
            call.assert_called_once_with("terminal.send", terminal="terminal-v1", text=raw, force=True)

    def test_switch_preserves_previous_seat_until_acquired_and_committed(self):
        (self.control.profile / "teacher").mkdir()
        first, second = (TerminalSelection(self.control.profile) for _ in range(2))
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        with first.move("a"), second.move("b"):
            pass
        with self.assertRaisesRegex(TerminalError, "其他终端占用"), first.move("b"):
            self.fail("occupied lesson was admitted")
        with self.assertRaisesRegex(ValueError, "failed"), first.move("c"):
            raise ValueError("failed")
        with self.assertRaisesRegex(TerminalError, "其他终端占用"), second.move("a"):
            self.fail("original seat was released")
        with second.move("c"), first.move("d"):
            pass
        with second.move("a"):
            pass
