"""Profile language survives installation, resumption and concurrent callbacks."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from teacher.i18n import get_language, localized, profile_language, tr, use_language
from teacher.profile import initialize


class ProfileLanguageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        override = patch.dict(os.environ, {"HERMES_TEACHER_LANGUAGE": ""})
        override.start()
        self.addCleanup(override.stop)

    def test_new_profiles_and_installed_runtime(self):
        for name, requested, expected in (("default", None, "en"), ("chinese", "zh-CN", "zh")):
            with self.subTest(name=name):
                home = initialize(self.root / name, self.root / (name + "-notes"), language=requested)
                self.assertEqual(profile_language(home), expected)
                config = json.loads((home / "config.yaml").read_text())
                self.assertEqual(config["teacher"]["language"], expected)
                plugin = home / "plugins/hermes-teacher"
                for filename in ("teacher/i18n.py", "LICENSE", "skills/teach/SKILL.md"):
                    self.assertTrue((plugin / filename).is_file(), filename)

    def test_legacy_chinese_profile_and_environment_override(self):
        (self.root / "config.yaml").write_text('{"teacher":{"vault":"/legacy"}}')
        self.assertEqual(profile_language(self.root), "zh")
        with patch.dict(os.environ, {"HERMES_TEACHER_LANGUAGE": "en-US"}):
            self.assertEqual(profile_language(self.root), "en")
        (self.root / "config.yaml").write_text('{"plugins":{"enabled":["hermes-teacher"]}}')
        self.assertEqual(profile_language(self.root), "en")

    def test_async_profiles_do_not_change_another_classroom(self):
        class Callback:
            def __init__(self, profile):
                self.profile = profile

            @localized
            async def label(self):
                await asyncio.sleep(0)
                return tr("Continue", "继续")

        english = initialize(self.root / "en", self.root / "notes-en")
        chinese = initialize(self.root / "zh", self.root / "notes-zh", language="zh")

        async def both():
            return await asyncio.gather(Callback(english).label(), Callback(chinese).label())

        with use_language("zh"):
            self.assertEqual(asyncio.run(both()), ["Continue", "继续"])
            self.assertEqual(get_language(), "zh")
        self.assertEqual(get_language(), "en")


if __name__ == "__main__":
    unittest.main()
