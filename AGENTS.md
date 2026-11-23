# Working on Hermes Teacher

The plugin entry point is `__init__.py`. Classroom behavior lives in `teacher/`, client components in `desktop/` and `tui/`, and the teaching guides shipped to Hermes in `skills/`. See the [README](README.md) for installation and a first lesson.

Use temporary profiles and vaults for tests. Keep student material, credentials and machine-specific settings out of commits. Preserve existing lesson identities, notes and pending answers when changing storage or message handling.

English is the default interface language. Keep Chinese available for learner-facing strings, and preserve the original text of learner and teacher messages. Exercise both languages when changing a classroom interaction.

Run the Python suite from the repository root:

```sh
python3 -m unittest discover -s tests -v
```
