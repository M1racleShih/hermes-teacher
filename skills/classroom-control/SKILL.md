---
name: classroom-control
description: Open, inspect, and operate Teacher classrooms and hand control to the learner.
version: 0.2.0
---

# Classroom control

Use this workflow for a learner's request to open, resume, inspect, or take over a classroom. The classroom runs in a dedicated Hermes profile.

## Commands

Run these commands from the installed `plugins/hermes-teacher/` directory or the repository root. Use a Python environment compatible with the pinned Hermes version, an initialized Teacher profile, a lesson ID, and the learner's chosen local herdr or Core socket.

```sh
python -m teacher.terminal --profile "$profile" list
python -m teacher.terminal --profile "$profile" start "$lesson" --backend herdr --socket "$socket"
python -m teacher.terminal --profile "$profile" start --topic "$topic" --vault "$vault" --backend herdr --socket "$socket"
python -m teacher.terminal --profile "$profile" read "$handle"
printf '%s' "$user_message" | python -m teacher.terminal --profile "$profile" send "$handle"
python -m teacher.terminal --profile "$profile" keys "$handle" enter
python -m teacher.terminal --profile "$profile" handoff "$handle"
```

Use `--backend core` for Core. Starting by topic reuses an existing lesson. Add `--new` for a separate lesson on the same topic. Use `open "$lesson"` to open in the foreground of the existing terminal and return to its shell on exit.

## Workflow

1. List running classrooms and confirm the lesson ID, status, and controller. Use `python -m teacher --profile "$profile" prepare` to inspect lessons and resume or start one according to the learner's intent.
2. Start or reuse a background classroom. For a preparation request, confirm the model and topic and leave the lesson at setup.
3. Read the display before sending. Check the lesson, prompt, and input state. Send the learner's exact text over standard input, preserving their language, line breaks, and symbols. Use herdr `paste "$handle"` to leave input for learner confirmation, Core `send` to submit directly, and `keys` for individual keystrokes.
4. Relay questions and plans to the learner. Send their actual answer or approval when received. Use the option letters or numbers shown in the prompt.
5. After navigation, process recovery, or terminal changes, list classrooms again to obtain current handles. Lobby handles allow reading the display.
6. For a takeover request, run `handoff` and give the learner the returned `attach` command. Read the display after handoff when requested.
7. If an input command disconnects or reports an uncertain outcome, inspect the display and lesson state before retrying.

## Verification

Check background launch, terminal identity, literal input, handoff, and recovery after restart.
