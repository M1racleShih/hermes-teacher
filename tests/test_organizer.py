from pathlib import Path
import json
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from teacher import organizer as organizer_module
from teacher.i18n import use_language
from teacher.library import BusyError, LibraryError, topic_vaults
from teacher.organizer import Organizer, OrganizerError, _planned_bundle
from teacher.organizer_plan import _native_plan, instructions
from teacher.records import Records


class OrganizerTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(use_language("zh"))
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "资料 总库"
        self.vault.mkdir()
        self.profile = self.root / "profile"
        self.records = Records(self.profile)
        self.organizer = Organizer(self.records)
        self.lesson = self.records.prepare(self.vault, "贝叶斯")
        self.key = self.lesson["id"]

    def sealed_record(self, suffix=""):
        self.records.append(self.key, "user", f"如何理解事前概率？{suffix}", f"user{suffix}")
        self.records.append(
            self.key,
            "assistant",
            f"先看 $P(A)$。{suffix}\n\n```python\nprint(1 / 2)\n```",
            f"teacher{suffix}",
        )
        receipt = self.records.seal(self.key)
        return Path(receipt["file"])

    def bundle(self, snapshot, slug="基础与先验"):
        return {
            "schema_version": 2,
            "topic": "贝叶斯",
            "chapters": [
                {
                    "slug": slug,
                    "title": "先验与证据",
                    "slices": [
                        {"id": item["id"], "start": 1, "end": item["lines"]} for item in snapshot["sources"]
                    ],
                }
            ],
            "concepts": [{"slug": f"{slug}知识卡", "title": "先验", "body": f"参见 [[lessons/{slug}]]。"}],
            "next_session": "下次用频率计算。",
        }

    def test_scan_snapshot_preview_and_apply_preserve_exact_source(self):
        sealed = self.sealed_record()
        original = sealed.read_bytes()
        self.records.append(self.key, "user", "课程继续写入新的 Markdown", "continuing")
        live = Path(self.records.get(self.key)["record"])
        live_before = live.read_bytes()
        self.assertEqual(len(self.organizer.scan(self.vault)), 1)
        snapshot = self.organizer.snapshot(self.vault)
        self.assertEqual((Path(snapshot["run"]) / "sources" / "s001.md").read_bytes(), original)
        bundle = self.bundle(snapshot)

        self.assertEqual(
            self.organizer.apply(self.vault, snapshot["run"], bundle, check=True)["status"], "ready"
        )
        self.assertTrue(sealed.exists())
        self.assertFalse((self.vault / "开始阅读.md").exists())
        with self.records.lease(self.key):
            # The organizer works on the prior receipt while the live learner still holds the classroom.
            concurrent = Organizer(Records(self.profile))
            result = concurrent.apply(self.vault, snapshot["run"], bundle)
        self.assertEqual(result["status"], "applied")
        self.assertFalse(sealed.exists())
        self.assertEqual((self.vault / result["archives"][0]["archive"]).read_bytes(), original)
        self.assertIn(original, (self.vault / "lessons/基础与先验.md").read_bytes())
        self.assertIn("[[lessons/基础与先验|先验与证据]]", (self.vault / "开始阅读.md").read_text())
        self.assertEqual(live.read_bytes(), live_before)
        self.assertEqual(self.organizer.apply(self.vault, snapshot["run"], bundle)["status"], "unchanged")
        with use_language("en"):
            self.assertEqual(self.organizer.apply(self.vault, snapshot["run"], bundle)["status"], "unchanged")
            self.assertFalse((self.vault / "Start Here.md").exists())
        with self.assertRaises(LibraryError):
            self.records.bind_record(self.key, "lessons/基础与先验.md")

    def test_english_plan_and_artifacts_keep_original_text(self):
        original = self.sealed_record().read_bytes()
        with use_language("en"):
            self.assertIn("Write in English", instructions())
            plan = _native_plan(
                "@@Chapter 1 Prior Probability\n"
                "@@Concept 1 洞察\nKeep both examples.\n"
                "@@Review 1 Ask again\nWhat was the prior?\n"
                "@@Next\nReview the evidence."
            )
            snapshot = self.organizer.snapshot(self.vault, topic="贝叶斯")
            source = snapshot["sources"][0]
            segments = [{"id": source["id"], "start": 1, "end": len(original.decode().splitlines())}]
            bundle = _planned_bundle("贝叶斯", segments, plan)
            result = self.organizer.apply(self.vault, snapshot["run"], bundle)
            self.assertIn("Start Here.md", result["outputs"])
            self.assertIn("# Start Here", (self.vault / "Start Here.md").read_text())
            self.assertIn("Sources [[lessons/Prior Probability]]", (self.vault / "concepts/洞察.md").read_text())
            self.assertIn("# Next Session\n\nReview the evidence.", (self.vault / "reviews/Next Session.md").read_text())
            text = (self.vault / "lessons/Prior Probability.md").read_bytes()
            self.assertIn(b"Original Record", text)
            self.assertIn(original, text)
        self.assertEqual(self.organizer.apply(self.vault, snapshot["run"], bundle)["status"], "unchanged")

    def test_chapter_resolves_moved_links_and_cannot_reuse_name(self):
        from urllib.parse import unquote

        asset = self.vault / "assets/图 一.svg"
        asset.parent.mkdir()
        asset.write_text("<svg/>")
        body = ('看[课图](<../../assets/图 一.svg#axis> "图注")。'
                '\n示例 `[文字](../../ignore)`\n\n```md\n[示意](../../ignore)\n```')
        for n in range(2):
            self.records.append(self.key, "assistant", body, f"asset-{n}")
            sealed = Path(self.records.seal(self.key)["file"])
            raw = sealed.read_bytes()
            snapshot = self.organizer.snapshot(self.vault)
            bundle = self.bundle(snapshot)
            if n:
                with self.assertRaisesRegex(OrganizerError, "新的章节文件名"):
                    self.organizer.apply(self.vault, snapshot["run"], bundle, check=True)
                self.assertEqual(sealed.read_bytes(), raw)
            else:
                applied = self.organizer.apply(self.vault, snapshot["run"], bundle)
                archived = self.vault / applied["archives"][0]["archive"]
                self.assertEqual(archived.read_bytes(), raw)
                chapter = self.vault / "lessons/基础与先验.md"
                text = unquote(chapter.read_text())
                self.assertIn('(<../assets/图 一.svg#axis> "图注")', text)
                self.assertIn('`[文字](../../ignore)`', text)
                self.assertIn('[示意](../../ignore)', text)
                self.assertTrue((chapter.parent / "../assets/图 一.svg").samefile(asset))

    def test_topic_migration_includes_handwritten_notes_exercises_and_shared_figures(self):
        material = {
            "lessons/贝叶斯/自写札记.md": "配套 exercises/数值实验/主程序.py",
            "exercises/数值实验/主程序.py": "print(1)",
            "exercises/数值实验/说明.txt": "同一练习项目的说明",
            "viz/共享图.svg": "<svg/>",
            "lessons/另一门课/札记.md": "另一个主题",
            "exercises/无关项目/文件.txt": "另一门课",
            "exercises/数值实验/.git/config": "版本控制元数据",
        }
        for name, value in material.items():
            path = self.vault / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        self.sealed_record()
        target = Path(self.organizer.migrate_topic(self.vault, "贝叶斯")["destination"])
        for name, value in material.items():
            self.assertEqual((self.vault / name).read_text(), value)
            if "另" in name or "无关项目" in name or "/.git/" in name:
                self.assertFalse((target / name).exists())
            else:
                self.assertEqual((target / name).read_text(), value)

    def test_rejects_inexact_coverage_fence_cuts_and_missing_links(self):
        self.sealed_record()
        snapshot = self.organizer.snapshot(self.vault)
        lines = snapshot["sources"][0]["lines"]
        data = (Path(snapshot["run"]) / "sources/s001.md").read_text().splitlines()
        fence = data.index("```python") + 1
        good = self.bundle(snapshot)
        variants = [
            [{"id": "s001", "start": 2, "end": lines}],
            [{"id": "s001", "start": 1, "end": 2}, {"id": "s001", "start": 2, "end": lines}],
            [{"id": "s001", "start": 1, "end": fence}, {"id": "s001", "start": fence + 1, "end": lines}],
        ]
        for slices in variants:
            bundle = self.bundle(snapshot)
            bundle["chapters"][0]["slices"] = slices
            with self.assertRaises(OrganizerError):
                self.organizer.apply(self.vault, snapshot["run"], bundle)
        summarized = self.bundle(snapshot)
        summarized["chapters"][0]["body"] = "用总结取代课堂原文"
        with self.assertRaises(OrganizerError):
            self.organizer.apply(self.vault, snapshot["run"], summarized)
        good["concepts"][0]["body"] = "引用 [[找不到的知识卡]]"
        with self.assertRaises(OrganizerError):
            self.organizer.apply(self.vault, snapshot["run"], good)
        self.assertFalse((self.vault / "开始阅读.md").exists())

    def test_incremental_preserves_manual_files_and_preflights_prior_edits(self):
        self.sealed_record()
        first = self.organizer.snapshot(self.vault)
        self.organizer.apply(self.vault, first["run"], self.bundle(first))
        first_chapter = self.vault / "lessons/基础与先验.md"
        original = first_chapter.read_bytes()
        manual = self.vault / "concepts/手写.md"
        manual.write_text("人工知识卡")
        source = self.sealed_record("-2")
        second = self.organizer.snapshot(self.vault)
        self.assertEqual(len(second["sources"]), 1)
        first_chapter.write_text("人工改写/旧章节")
        with self.assertRaises(OrganizerError):
            self.organizer.apply(self.vault, second["run"], self.bundle(second, "条件概率"))
        self.assertTrue(source.exists())
        self.assertFalse((self.vault / "lessons/条件概率.md").exists())
        self.assertEqual(manual.read_text(), "人工知识卡")
        first_chapter.write_bytes(original)
        self.organizer.apply(self.vault, second["run"], self.bundle(second, "条件概率"))
        self.assertEqual(first_chapter.read_bytes(), original)
        self.assertIn("条件概率", (self.vault / "开始阅读.md").read_text())

    def test_partial_commit_retries_after_restart_and_keeps_original(self):
        original = self.sealed_record()
        before = original.read_bytes()
        snapshot = self.organizer.snapshot(self.vault)
        bundle = self.bundle(snapshot)
        actual = organizer_module.atomic_write

        def failure(vault, relative, data, **kwargs):
            if relative == ".teacher-organizer/ledger.json":
                raise OSError("interrupted")
            return actual(vault, relative, data, **kwargs)

        with patch.object(organizer_module, "atomic_write", failure):
            with self.assertRaises(OSError):
                self.organizer.apply(self.vault, snapshot["run"], bundle)
        self.assertEqual(original.read_bytes(), before)
        self.assertTrue((self.vault / ".teacher-organizer/pending.json").is_file())
        fresh = Organizer(Records(self.profile))
        self.assertEqual(fresh.apply(self.vault, snapshot["run"], bundle, check=True)["status"], "pending")
        result = fresh.apply(self.vault, snapshot["run"], bundle)
        self.assertEqual((self.vault / result["archives"][0]["archive"]).read_bytes(), before)
        self.assertFalse(original.exists())
        self.assertFalse((self.vault / ".teacher-organizer/pending.json").exists())

    def test_migrate_topic_and_relocate_root_preserve_branch_assets_and_pause(self):
        assets = self.vault / "viz"
        assets.mkdir()
        (assets / "贝叶斯.svg").write_text("<svg/>")
        figure = f"teacher-svg-{'a' * 32}-1"
        (assets / f"{figure}.svg").write_text("<svg/>")
        (assets / f"{figure}.png").write_bytes(b"fixture image")
        practice = self.vault / "exercises/贝叶斯"
        practice.mkdir(parents=True)
        (practice / "练习.txt").write_text("题目")
        self.records.append(self.key, "assistant", "图例 ![[viz/贝叶斯.svg]]", "image", kind="image")
        self.records.append(self.key, "assistant", f"![[viz/{figure}.png]]", "figure", kind="image")
        self.records.bind_session(self.key, "child-session", parent_id=self.lesson["session_id"])
        self.records.append(self.key, "user", "学习者作答", "answer")
        self.records.append(
            self.key, "user", "仅在旧分支", "old-branch", session_id=self.lesson["session_id"]
        )
        self.records.checkpoint(self.key, "下一步核对结果。")
        self.records.set_recording(self.key, False)
        source_file = Path(self.lesson["record"])
        original = source_file.read_bytes()
        target = self.vault / "vaults/贝叶斯"
        with self.records.lease(self.key):
            with self.assertRaises(BusyError):
                Organizer(Records(self.profile)).migrate_topic(self.vault, "贝叶斯", check=True)
        self.assertEqual(self.organizer.migrate_topic(self.vault, "贝叶斯", check=True)["status"], "ready")
        self.assertFalse(target.exists())
        self.assertEqual(self.organizer.migrate_topic(self.vault, "贝叶斯")["status"], "moved")
        self.assertEqual(source_file.read_bytes(), original)
        self.assertEqual((target / "viz/贝叶斯.svg").read_text(), "<svg/>")
        self.assertEqual((target / "viz" / f"{figure}.png").read_bytes(), b"fixture image")
        self.assertEqual((target / "viz" / f"{figure}.svg").read_text(), "<svg/>")
        self.assertEqual((target / "exercises/贝叶斯/练习.txt").read_text(), "题目")
        resumed = self.records.prepare(self.vault, "贝叶斯")
        self.assertEqual(resumed["id"], self.key)
        self.assertEqual(resumed["session_id"], "child-session")
        self.assertFalse(resumed["recording"])
        self.assertEqual(self.records.read_checkpoint(self.key)["text"], "下一步核对结果。")
        self.assertEqual(
            [event["event_id"] for event in self.records.history(self.key)], ["image", "figure", "answer"]
        )
        self.assertEqual(
            self.records.history(self.key, session_id=self.lesson["session_id"])[-1]["event_id"], "old-branch"
        )
        self.assertEqual(topic_vaults(self.vault)["贝叶斯"], target)
        self.records.set_recording(self.key, True)
        self.records.append(self.key, "user", "新原文", "new")
        self.assertEqual(Path(self.records.get(self.key)["record"]).parent, target / "lessons/_待整理")

        relocated = self.root / "搬迁 学习库"
        (self.vault / "空目录").mkdir()
        self.assertEqual(self.organizer.relocate(self.vault, relocated, check=True)["status"], "ready")
        self.assertFalse(relocated.exists())
        self.assertEqual(self.organizer.relocate(self.vault, relocated)["status"], "moved")
        final = Records(self.profile).prepare(self.vault, "贝叶斯")
        self.assertEqual(final["id"], self.key)
        self.assertEqual(Path(final["vault"]), relocated / "vaults/贝叶斯")
        self.assertTrue((relocated / "空目录").is_dir())
        self.assertEqual(
            Path(final["record"]).read_text(),
            Path(resumed["vault"], Path(final["record"]).relative_to(final["vault"])).read_text(),
        )
        self.assertTrue(self.vault.is_dir())

    def test_move_recovery_and_conflicts_preserve_source(self):
        path = self.sealed_record()
        original = path.read_bytes()
        destination = self.vault / "vaults/贝叶斯"
        destination.mkdir(parents=True)
        (destination / "人工.txt").write_text("不可覆盖")
        with self.assertRaises(OrganizerError):
            self.organizer.migrate_topic(self.vault, "贝叶斯")
        self.assertEqual((destination / "人工.txt").read_text(), "不可覆盖")
        (destination / "人工.txt").unlink()
        destination.rmdir()
        path.write_bytes(original + b"manual")
        with self.assertRaises(OrganizerError):
            self.organizer.migrate_topic(self.vault, "贝叶斯", check=True)
        path.write_bytes(original)
        with patch("teacher.records.configure_topic_vault", side_effect=OSError("interrupted route")):
            with self.assertRaises(OSError):
                self.organizer.migrate_topic(self.vault, "贝叶斯")
        self.assertTrue(destination.exists())
        self.assertEqual(path.read_bytes(), original)
        replacement = Records(self.profile)
        self.assertEqual(Path(replacement.prepare(self.vault, "贝叶斯")["vault"]), destination)
        self.assertEqual(Organizer(replacement).migrate_topic(self.vault, "贝叶斯")["status"], "unchanged")

    def test_move_reuses_localized_fallback_destination_after_language_switch(self):
        lesson = self.records.prepare(self.vault, ".")
        self.records.append(lesson["id"], "user", "原文", "source")
        moved = self.organizer.migrate_topic(self.vault, ".")
        self.assertTrue(Path(moved["destination"]).name.startswith("学习主题--"))
        with use_language("en"):
            retried = self.organizer.migrate_topic(self.vault, ".")
        self.assertEqual(retried["status"], "unchanged")
        self.assertEqual(retried["destination"], moved["destination"])

    def test_one_click_organize_validates_and_repairs_chapter_plan(self):
        source = self.sealed_record()
        raw = source.read_bytes()
        requests = []

        def complete(**kwargs):
            payload = json.loads(kwargs["input"][0]["text"])
            requests.append(payload)
            return SimpleNamespace(
                provider="fixture",
                model="organizer",
                parsed={
                    "chapters": [
                        {"slug": "原文分章", "title": "先验概率", "start": 1 if len(requests) > 1 else 999}
                    ],
                    "concepts": [
                        {
                            "slug": "先验卡",
                            "title": "先验",
                            "body": "先验是在看到证据之前的概率。",
                            "chapter_start": 1,
                        }
                    ],
                    "reviews": [
                        {
                            "slug": "先验复习",
                            "title": "检索练习",
                            "body": "怎样判断一个概率是否为先验？",
                            "chapter_start": 1,
                        }
                    ],
                    "next_session": "下一步练习区分先验与后验。",
                },
            )

        result = self.organizer.organize(self.vault, complete, topic="贝叶斯")
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["calls"], 2)
        self.assertIn("章节起始编号", requests[1]["previous_plan_error"])
        self.assertEqual("".join(segment["text"] for segment in requests[0]["segments"]).encode(), raw)
        self.assertIn(raw, (self.vault / "lessons/原文分章.md").read_bytes())
        self.assertIn("来源 [[lessons/原文分章]]", (self.vault / "concepts/先验卡.md").read_text())
        self.assertIn("怎样判断", (self.vault / "reviews/先验复习.md").read_text())
        self.assertEqual(self.organizer.plan(self.vault, result["run"], complete)["calls"], 0)
        with self.assertRaises(OrganizerError):
            self.organizer.organize(self.vault, complete, topic="贝叶斯")
        self.assertEqual(len(requests), 2)

    def test_move_excludes_active_gateway_turn_lock_across_processes(self):
        child = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys; from pathlib import Path; from teacher.library import file_lease; "
                "lease = file_lease(Path(sys.argv[1]), f'turns/{sys.argv[2]}.lock'); "
                "lease.__enter__(); print('ready'); sys.stdin.read()",
                str(self.records.state),
                self.key,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            with self.assertRaises(BusyError):
                self.organizer.migrate_topic(self.vault, "贝叶斯", check=True)
        finally:
            child.kill()
            child.communicate(timeout=5)
        self.assertEqual(self.organizer.migrate_topic(self.vault, "贝叶斯", check=True)["status"], "ready")

    def test_no_model_for_oversize_snapshot_and_can_select_one_source(self):
        first = self.sealed_record()
        second = self.sealed_record("-second")
        one = self.organizer.snapshot(self.vault, topic="贝叶斯", source=first)
        self.assertEqual(len(one["sources"]), 1)
        self.assertEqual((Path(one["run"]) / "sources/s001.md").read_bytes(), first.read_bytes())
        all_sources = self.organizer.snapshot(self.vault)
        with patch.object(organizer_module, "MAX_PLANNING_BYTES", 40):
            with self.assertRaisesRegex(OrganizerError, "输入超过 40 字节"):
                self.organizer.plan(self.vault, all_sources["run"], lambda **kwargs: self.fail("model call"))
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_native_model_markers_preserve_markdown_in_auxiliary_notes(self):
        body = '被称为“灵敏度”，写作 $P(+\\mid D)$。\n\n例子保留 "原话"。'
        parsed = _native_plan(
            f"@@章 1 条件概率\n@@知识卡 1 灵敏度\n{body}\n"
            "@@复习卡 1 分母\n谁在分母中？\n@@下一步\n做一道迁移练习。"
        )
        self.assertEqual(parsed["concepts"][0]["body"], body)
        self.assertEqual(parsed["chapters"][0]["start"], 1)
        self.assertEqual(parsed["next_session"], "做一道迁移练习。")

    def test_knowledge_notes_link_original_evidence_across_chapters(self):
        plan = _native_plan(
            "@@章 1 条件概率\n@@章 2 自然频数\n"
            "@@知识卡 1 频数中的分母\n定义来自第一章，复测错题来自第二章。\n"
            "@@复习卡 2 回忆分母\n哪两项应加在一起？\n@@下一步\n复测。"
        )
        numbered = [
            {"id": "s001", "start": 1, "end": 2, "text": "定义\n解释"},
            {"id": "s001", "start": 3, "end": 4, "text": "作答\n反馈"},
        ]
        bundle = _planned_bundle("贝叶斯", numbered, plan)
        self.assertIn("来源 [[lessons/条件概率]] · [[lessons/自然频数]]", bundle["concepts"][0]["body"])
        self.assertIn("来源 [[lessons/自然频数]] · [[lessons/条件概率]]", bundle["reviews"][0]["body"])


if __name__ == "__main__":
    unittest.main()
