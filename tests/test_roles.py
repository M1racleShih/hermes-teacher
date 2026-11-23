import base64
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from teacher.i18n import tr, use_language
from teacher.library import BusyError, file_lease
from teacher.models import ModelSettings, read_config
from teacher.records import Records
from teacher.role_runner import HermesRoleRunner, WEB_TOOLS, _research, _visual
from teacher.roles import Roles
from teacher.visuals import Preview
from teacher.visuals import validate_source


class RoleLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(use_language("en"))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.records = Records(self.root / "profile")
        self.vault = self.root / "vault"
        self.other_vault = self.root / "other"
        self.first = self.records.prepare(self.vault, "贝叶斯", new=True, create=True)
        self.second = self.records.prepare(self.other_vault, "同主题并行", new=True, create=True)
        self.settings = ModelSettings(self.root / "profile")
        self.profile_config("research-model")

    def profile_config(self, model):
        (self.root / "profile" / "config.yaml").write_text(
            json.dumps(
                {
                    "teacher": {"models": {"roles": {"researcher": {"provider": "custom", "model": model}}}},
                }
            ),
            encoding="utf-8",
        )

    def test_parallel_supplement_is_lesson_scoped(self):
        both_entered, release = threading.Barrier(3), threading.Event()
        seen = []

        def runner(run, work, control):
            seen.append((run["lesson_id"], list(run["tasks"]), run["route"]["model"]))
            if len(run["tasks"]) == 1:
                both_entered.wait(4)
                release.wait(4)
            return {"summary": "|".join(run["tasks"])}

        roles = Roles(self.root / "profile", runner=runner)
        self.addCleanup(release.set)
        first = roles.dispatch(self.first["id"], "start", role="researcher", task="甲课题")
        second = roles.dispatch(self.second["id"], "start", role="researcher", task="乙课题")
        both_entered.wait(4)
        self.assertTrue(roles.busy(self.first["id"]))
        for action in ("status", "resume", "supplement", "cancel"):
            with self.assertRaisesRegex(ValueError, "task not found"):
                roles.dispatch(self.second["id"], action, task_id=first["id"], task="串课")
        self.profile_config("changed-after-start")
        roles.dispatch(self.first["id"], "supplement", task_id=first["id"], task="只修订甲课")
        release.set()
        for lesson, task, expected in (
            (self.first, first, "甲课题|只修订甲课"),
            (self.second, second, "乙课题"),
        ):
            final = roles.dispatch(lesson["id"], "wait", task_id=task["id"], timeout=4)
            self.assertEqual(final["result"]["summary"], expected)
            self.assertEqual(final["status"], "completed")
            self.assertEqual(final["route"]["model"], "research-model")
            self.assertFalse(roles.busy(lesson["id"]))
        self.assertEqual(len(seen), 3)
        self.assertEqual(first["session_id"], roles.dispatch(self.first["id"], "status", task_id=first["id"])["session_id"])
        self.assertNotEqual(first["session_id"], second["session_id"])

    def test_role_worker_uses_its_profile_language(self):
        config_file = self.root / "profile" / "config.yaml"
        config = json.loads(config_file.read_text())
        config["teacher"]["language"] = "zh"
        config_file.write_text(json.dumps(config))

        def runner(run, work, control):
            self.assertEqual(run["tasks"], ["原样传递"])
            return {"summary": tr("Complete", "已完成")}

        roles = Roles(self.root / "profile", records=self.records, runner=runner)
        with patch.dict(os.environ):
            os.environ.pop("HERMES_TEACHER_LANGUAGE", None)
            with use_language("en"):
                lesson = self.first["id"]
                job = roles.dispatch(lesson, "start", role="researcher", task="原样传递")
                result = roles.dispatch(lesson, "wait", task_id=job["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["summary"], "已完成")

    def test_cancel_resume_and_cold_start(self):
        entered = threading.Event()
        seen = []

        class Agent:
            def __init__(self):
                self.called = threading.Event()

            def interrupt(self, *, hard_cancel):
                if hard_cancel:
                    self.called.set()

        def runner(run, work, control):
            seen.append(run["route"]["model"])
            if len(seen) == 1:
                agent = Agent()
                with control.watch(agent):
                    entered.set()
                    if not agent.called.wait(4):
                        raise ValueError("cancel not delivered")
                    control.check()
            return {"summary": run["tasks"][0]}

        lesson_id = self.first["id"]
        roles = Roles(self.root / "profile", runner=runner)
        task_id = roles.dispatch(lesson_id, "start", role="researcher", task="丙")["id"]
        self.assertTrue(entered.wait(4))
        self.assertEqual(roles.dispatch(lesson_id, "cancel", task_id=task_id)["status"], "cancelling")
        final = roles.dispatch(lesson_id, "wait", task_id=task_id, timeout=4)
        self.assertEqual(final["status"], "cancelled")
        self.profile_config("changed-before-resume")
        cold = Roles(self.root / "profile", runner=runner)
        self.assertEqual(cold.dispatch(lesson_id, "status", task_id=task_id)["status"], "cancelled")
        self.assertEqual(seen, ["research-model"])
        cold.dispatch(lesson_id, "resume", task_id=task_id)
        self.assertEqual(cold.dispatch(lesson_id, "wait", task_id=task_id, timeout=4)["status"], "completed")
        self.assertEqual(cold.dispatch(lesson_id, "resume", task_id=task_id)["status"], "completed")
        self.assertEqual(seen, ["research-model", "research-model"])

    def test_lost_lock_marks_interrupted_until_explicit_resume(self):
        roles = Roles(self.root / "profile", runner=lambda run, work, control: {"summary": "继续"})
        lesson = self.first["id"]
        task = roles.dispatch(lesson, "start", role="researcher", task="保持原任务")["id"]
        roles.dispatch(lesson, "wait", task_id=task, timeout=4)
        # A persisted running state with no owner lock is the cold-start case after process loss.
        with roles._state(roles.directory / task) as job:
            job.update(status="running", result=None)
        cold = Roles(self.root / "profile", runner=roles.runner)
        self.assertEqual(cold.dispatch(lesson, "status", task_id=task)["status"], "interrupted")
        cold.dispatch(lesson, "resume", task_id=task)
        self.assertEqual(cold.dispatch(lesson, "wait", task_id=task, timeout=4)["result"]["summary"], "继续")

    def test_image_event_retry_reuses_accepted_preview(self):
        base_records = self.records
        attempts, appends = [], []

        class RecordsWithLostResponse:
            get, lease = base_records.get, base_records.lease

            @staticmethod
            def append(*args, **kwargs):
                new = base_records.append(*args, **kwargs)
                appends.append(new)
                if len(appends) == 1:
                    raise OSError("lost acknowledgement")
                return new

        def runner(run, work, control):
            attempts.append(run["id"])
            source, image = work / "preview.svg", work / "preview.png"
            source.write_bytes(b"<svg/>")
            image.write_bytes(b"test image")
            return {"preview": Preview(source, image, 640, 480).as_dict(), "summary": "已目检"}

        roles = Roles(self.root / "profile", records=RecordsWithLostResponse(), runner=runner)
        lesson = self.first["id"]
        task = roles.dispatch(lesson, "start", role="svg", task="直角三角形")["id"]
        self.assertEqual(roles.dispatch(lesson, "wait", task_id=task, timeout=4)["status"], "failed")
        roles.dispatch(lesson, "resume", task_id=task)
        final = roles.dispatch(lesson, "wait", task_id=task, timeout=4)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(attempts, [task])
        self.assertEqual(appends, [True, False])
        self.assertEqual((self.vault / final["result"]["asset"]["image"]).read_bytes(), b"test image")
        record = Path(base_records.get(lesson)["record"]).read_text()
        self.assertEqual(record.count(final["result"]["asset"]["embed"]), 1)
        self.assertEqual(base_records.history(self.second["id"]), [])


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(use_language("en"))

    def test_import_dry_run_allowlist_and_independent_role(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary)
            source = profile / "source.json"
            cfg = profile / "config.yaml"
            cfg.write_text(
                json.dumps(
                    {
                        "model": {"default": "local", "provider": "custom"},
                        "teacher": {
                            "models": {
                                "allowed": ["custom/approved", "custom/special"],
                                "default": {"compression": {"protect_last_n": 12}},
                                "roles": {"researcher": {"provider": "special", "model": "research"}},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            source.write_text(
                json.dumps(
                    {
                        "model": {"default": "approved", "provider": "custom", "api_key": "sentinel"},
                        "agent": {"reasoning_effort": "high"},
                        "compression": {
                            "threshold_tokens": 8000,
                            "protect_last_n": 19,
                            "summary_api_key": "sentinel",
                        },
                        "allowed_tools": ["sentinel"],
                    }
                ),
                encoding="utf-8",
            )
            original = hashlib.sha256(source.read_bytes()).digest()
            models = ModelSettings(profile)
            saved = cfg.read_bytes()
            self.assertEqual(models.sync(source, dry_run=True)["model"], "approved")
            self.assertEqual(cfg.read_bytes(), saved)
            self.assertFalse((profile / "teacher").exists())
            models.sync(source)
            self.assertNotIn("sentinel", cfg.read_text())
            self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)
            self.assertEqual(
                read_config(cfg)["compression"], {"threshold_tokens": 8000, "protect_last_n": 12}
            )
            self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), original)
            first = models.lesson()
            self.assertEqual(
                first,
                {
                    "provider": "custom",
                    "model": "approved",
                    "reasoning_effort": "high",
                    "compression": {"threshold_tokens": 8000, "protect_last_n": 12},
                },
            )
            self.assertEqual(models.lesson(model="special", reasoning_effort="low")["model"], "special")
            self.assertEqual(models.role("researcher"), {"provider": "special", "model": "research"})
            self.assertEqual(first["model"], "approved")
            with self.assertRaisesRegex(ValueError, "missing from"):
                models.lesson(model="unapproved")
            source.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Cannot read model config"):
                models.sync(source)
            self.assertEqual(models.lesson(), first)

    def test_native_compression_updates_and_prunes_only_imported_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary)
            source, config = profile / "source.json", profile / "config.yaml"
            config.write_text(
                json.dumps(
                    {
                        "model": {"provider": "base", "default": "main"},
                        "plugins": {"enabled": ["teacher"]},
                        "compression": {"target_ratio": 0.3, "tail_mode": "legacy", "protect_last_n": 11},
                        "teacher": {"models": {"default": {"compression": {"protect_last_n": 12}}}},
                    }
                )
            )
            models = ModelSettings(profile)

            def sync(budget):
                source.write_text(json.dumps({"compression": budget}))
                models.sync(source)
                return read_config(config)

            first = sync(
                {
                    "target_ratio": 0.2,
                    "threshold": 0.45,
                    "threshold_tokens": 9000,
                    "enabled": True,
                    "protect_last_n": 23,
                    "tail_mode": "DROP",
                    "summary_api_key": "SENTINEL",
                }
            )
            self.assertEqual(
                first["compression"],
                {
                    "target_ratio": 0.3,
                    "tail_mode": "legacy",
                    "protect_last_n": 12,
                    "threshold": 0.45,
                    "threshold_tokens": 9000,
                    "enabled": True,
                },
            )
            self.assertNotIn("SENTINEL", config.read_text())
            self.assertNotIn("DROP", config.read_text())
            # An explicit edit of a previously imported setting becomes a local override.
            first["compression"]["threshold_tokens"] = 9500
            config.write_text(json.dumps(first))
            second = sync({"enabled": False, "threshold_tokens": 12000})
            self.assertEqual(
                second["compression"],
                {
                    "enabled": False,
                    "threshold_tokens": 9500,
                    "target_ratio": 0.3,
                    "tail_mode": "legacy",
                    "protect_last_n": 12,
                },
            )
            second["teacher"]["models"].pop("default")
            config.write_text(json.dumps(second))
            final = sync({})
            self.assertEqual(
                final["compression"],
                {"threshold_tokens": 9500, "target_ratio": 0.3, "tail_mode": "legacy", "protect_last_n": 11},
            )
            self.assertEqual(final["model"], {"provider": "base", "default": "main"})
            self.assertEqual(final["plugins"], {"enabled": ["teacher"]})
            self.assertEqual(
                models.lesson()["compression"],
                {"threshold_tokens": 9500, "target_ratio": 0.3, "protect_last_n": 11},
            )
            saved = config.stat()
            models.sync(source)
            self.assertEqual(
                (config.stat().st_ino, config.stat().st_mtime_ns), (saved.st_ino, saved.st_mtime_ns)
            )

    def test_atomic_sync_failure_lock_and_corrupt_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary)
            source, config = profile / "source.json", profile / "config.yaml"
            source.write_text('{"compression":{"threshold_tokens":7000}}')
            config.write_text('{"plugins":{"enabled":["teacher"]}}')
            saved = config.read_bytes()
            models = ModelSettings(profile)
            with file_lease(profile, "teacher/locks/models-import.lock"), self.assertRaises(BusyError):
                models.sync(source)
            self.assertEqual(config.read_bytes(), saved)
            with patch("teacher.library.os.replace", side_effect=OSError("fixture write failure")):
                with self.assertRaises(OSError):
                    models.sync(source)
            self.assertEqual(config.read_bytes(), saved)
            self.assertEqual(list(profile.glob(".teacher-*.tmp")), [])
            models.sync(source)
            self.assertEqual(read_config(config)["compression"], {"threshold_tokens": 7000})
            config.write_text("{invalid profile document")
            with self.assertRaises(ValueError):
                models.sync(source)
            self.assertEqual(config.read_text(), "{invalid profile document")


class RoleOutputTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(use_language("en"))

    def test_role_prompts_honor_teaching_language_across_interface_locales(self):
        agent_factory = MagicMock()
        sessions = MagicMock(
            get_session=lambda _: None,
            get_compression_tip=lambda _: None,
            get_messages_as_conversation=lambda *_args, **_kwargs: [],
        )
        modules = {
            "agent.delegation_context": SimpleNamespace(delegated_child_context=nullcontext),
            "agent.runtime_scope": SimpleNamespace(bind_profile_scope=lambda _: nullcontext()),
            "hermes_state": SimpleNamespace(SessionDB=lambda **_: sessions),
            "run_agent": SimpleNamespace(AIAgent=agent_factory),
        }
        runner = HermesRoleRunner(Path("."))
        cases = (
            ("en", "请用简体中文呈现结果。", "latest task instructions", "Default to English if none is specified"),
            ("zh", "Present the result in English.", "最新任务说明指定的授课语言", "未指定时使用简体中文"),
        )
        with patch.dict(sys.modules, modules), patch("teacher.role_runner.resolve_agent_route", return_value={}):
            for locale, brief, override, fallback in cases:
                for role, worker, index in (("researcher", "_research", 1), ("svg", "_visual", 2)):
                    with self.subTest(locale=locale, role=role), use_language(locale):
                        agent_factory.return_value.valid_tool_names = WEB_TOOLS if role == "researcher" else set()
                        run = {
                            "role": role, "topic": "Probability", "tasks": [brief], "session_id": "task",
                            "parent_session_id": None, "route": {}, "id": "task",
                        }
                        with patch(f"teacher.role_runner.{worker}") as perform:
                            runner(run, Path("."), MagicMock())
                        self.assertIn(brief, perform.call_args.args[index])
                        instructions = agent_factory.call_args.kwargs["ephemeral_system_prompt"]
                        self.assertIn(override, instructions)
                        self.assertIn(fallback, instructions)

    def test_search_evidence_comes_from_extractions(self):
        site = "https://example.org/definition"
        snippet_only = "https://example.net/never-opened"

        def call(name, args, result):
            return [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": name,
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args),
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": name, "content": json.dumps(result)},
            ]

        class Agent:
            def run_conversation(self, *_args, **_kwargs):
                return {
                    "final_response": f"定义见 {site} 和 {snippet_only}",
                    "messages": [
                        *call(
                            "web_search",
                            {"query": "定义"},
                            {"success": True, "data": {"web": [{"url": snippet_only}]}},
                        ),
                        *call(
                            "web_extract",
                            {"urls": [site]},
                            {"results": [{"url": site, "content": "定义原文。"}]},
                        ),
                    ],
                }

        for language, notice in (("en", "still needs verification"), ("zh", "证据仍待核对")):
            with self.subTest(language=language), use_language(language):
                result = _research(Agent(), "查证", "task", None)
                self.assertEqual([e["url"] for e in result["sources"]], [site])
                self.assertIn(site, result["summary"])
                self.assertIn("定义见", result["summary"])
                self.assertNotIn(snippet_only, result["summary"])
                self.assertIn(notice, result["summary"])
                self.assertEqual(result["searches"], [{"query": "定义", "ok": True, "result_count": 1}])

    def test_visual_approval_requires_rendered_image(self):
        class Agent:
            def run_conversation(self, *_args, **_kwargs):
                return {"final_response": '{"approved": true, "review": "盲目确认"}'}

        class Control:
            @staticmethod
            def check():
                pass

        with self.assertRaisesRegex(ValueError, "did not provide a source"):
            _visual(Agent(), {"role": "svg", "id": "task", "attempt": 1}, "绘图", Path("."), Control())

    def test_corrected_image_goes_back_to_role_before_approval(self):
        original = 'flowchart TD\n B["全概率"] --> A["条件概率"]'
        corrected = 'flowchart TD\n A["条件概率"] --> B["全概率"]'
        outputs, seen = [], []

        class Agent:
            def run_conversation(self, prompt, **_kwargs):
                seen.append(prompt)
                reply = [
                    f"```mermaid\n{original}\n```",
                    json.dumps({"source": corrected}),
                    '{"approved": true, "review": "条件概率指向全概率，标签清楚。"}',
                ][len(seen) - 1]
                return {"final_response": reply}

        class Control:
            @staticmethod
            def check():
                pass

            @staticmethod
            def preview(preview):
                outputs.append(preview)

        def make_preview(role, source, work, label):
            source_path, png = work / f"{label}.mmd", work / f"{label}.png"
            source_path.write_text(source, encoding="utf-8")
            png.write_bytes(f"PNG:{source}".encode())
            return Preview(source_path, png, 100, 200)

        with (
            tempfile.TemporaryDirectory() as work,
            patch("teacher.role_runner.render", side_effect=make_preview),
        ):
            result = _visual(
                Agent(),
                {"role": "mermaid", "id": "task", "attempt": 1},
                "按正确方向绘图",
                Path(work),
                Control(),
            )
            for prompt, source in zip(seen[1:], [original, corrected]):
                self.assertEqual(
                    base64.b64decode(prompt[1]["image_url"]["url"].split(",", 1)[1]), f"PNG:{source}".encode()
                )
            self.assertEqual(result["iterations"], 2)
            self.assertEqual(outputs[-1], result["preview"])

    def test_static_graph_sources_reject_external_loads_and_broken_xml(self):
        with self.assertRaisesRegex(ValueError, "static"):
            validate_source("mermaid", 'flowchart TD\n click A href "https://example.org"')
        with self.assertRaisesRegex(ValueError, "Invalid SVG source"):
            validate_source("svg", '<svg xmlns="http://www.w3.org/2000/svg"><')
        with self.assertRaisesRegex(ValueError, "unsupported element"):
            validate_source(
                "svg",
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><image href="file:///home/data"/></svg>',
            )


if __name__ == "__main__":
    unittest.main()
