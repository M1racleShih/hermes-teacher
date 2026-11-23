"""Classroom question keyboard controls for the Teacher terminal composer."""

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app, run_in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings

from .i18n import tr
from .learning import QUIZ_LETTERS


CONFIRM_FEEDBACK = object()


class FileCompleter(Completer):
    def __init__(self, suggestions):
        self.suggestions = suggestions

    def get_completions(self, document, complete_event):
        for path, start in self.suggestions(document.text_before_cursor):
            yield Completion(path, start_position=start)


class TerminalInput:
    def __init__(self, *, file_suggestions=None):
        keys = KeyBindings()

        @Condition
        def can_choose():
            return self.open_question and self.labels and not get_app().current_buffer.text

        @Condition
        def picking():
            return self.picking

        @Condition
        def can_confirm():
            return self.allow_confirm and not get_app().current_buffer.text

        @Condition
        def can_add_note():
            return self.open_question and (self.ask or bool(get_app().current_buffer.text))

        @keys.add("tab", filter=can_choose)
        def toggle_picker(event):
            self.picking = not self.picking

        @keys.add("escape", filter=picking)
        def leave_picker(event):
            self.picking = False

        @keys.add("up", filter=picking)
        def move_up(event):
            self.cursor = (self.cursor - 1) % len(self.labels)

        @keys.add("down", filter=picking)
        def move_down(event):
            self.cursor = (self.cursor + 1) % len(self.labels)

        @keys.add(" ", filter=picking)
        def toggle_choice(event):
            if self.quiz["mode"] == "multi":
                self.selected.symmetric_difference_update({self.cursor})

        @keys.add("enter", filter=picking)
        def stage_answer(event):
            chosen = sorted(self.selected) if self.quiz["mode"] == "multi" else [self.cursor]
            if not chosen:
                return
            if self.ask:
                values = [str(index + 1) if index < len(self.labels) - 1 else tr("other ", "自填 ")
                          for index in chosen]
            else:
                values = [QUIZ_LETTERS[index] for index in chosen]
            event.current_buffer.insert_text(" ".join(values))

        @keys.add("c-j", filter=can_add_note)
        def newline(event):
            event.current_buffer.insert_text("\n")

        @keys.add("enter", filter=can_confirm)
        def confirm_feedback(event):
            event.app.exit(result=CONFIRM_FEEDBACK)

        self.session = PromptSession(
            key_bindings=keys,
            bottom_toolbar=self.toolbar,
            completer=FileCompleter(file_suggestions) if file_suggestions else None,
            complete_while_typing=False,
        )
        self.session.default_buffer.on_text_changed += self.composer_changed

    def composer_changed(self, buffer):
        if buffer.text:
            self.picking = False

    def set_card(self, quiz):
        self.quiz = quiz
        self.ask = bool(quiz and quiz.get("kind") == "ask")
        self.labels = [option["label"] for option in quiz["options"]] if quiz else []
        if self.ask and self.labels:
            self.labels.append(tr("Other (custom response)", "其他（自填）"))
        self.open_question = bool(not self.default and quiz and quiz["status"] == "open")
        self.allow_confirm = bool(not self.default and quiz and quiz["status"] == "feedback")
        self.picking, self.cursor, self.selected = False, 0, set()

    async def announce(self, display, quiz):
        if self.quiz != quiz:
            self.set_card(quiz)
        await run_in_terminal(display)

    def read(self, label, default="", *, quiz=None, watch=None):
        self.label, self.default = label, default
        self.set_card(quiz)

        def pre_run():
            if watch:
                get_app().create_background_task(watch())

        return self.session.prompt(self.message, default=default, pre_run=pre_run)

    def message(self):
        if not self.picking:
            return self.label
        multi = self.quiz["mode"] == "multi"
        lines = []
        for index, label in enumerate(self.labels):
            cursor = "›" if index == self.cursor else " "
            selected = ("[x] " if index in self.selected else "[ ] ") if multi else ""
            number = index + 1 if self.ask else QUIZ_LETTERS[index]
            lines.append(f"{cursor} {selected}{number}. {label}")
        return "\n".join([*lines, self.label])

    def toolbar(self):
        if self.allow_confirm:
            return tr("With the input empty, press Enter to continue after reading the feedback.",
                      "输入框为空时，读完解析按 Enter 继续。")
        fill = tr("Enter to insert choices", "Enter 填入选择") if self.ask else tr("Enter to insert letters", "Enter 填入字母")
        if self.picking and self.quiz["mode"] == "multi":
            return tr(f"↑↓ to move · Space to select · {fill} · Esc to go back",
                      f"↑↓ 移动 · 空格勾选 · {fill} · Esc 返回")
        if self.picking:
            return tr(f"↑↓ to move · {fill} · Esc to go back", f"↑↓ 移动 · {fill} · Esc 返回")
        if self.open_question and self.ask:
            return (tr("Tab for options · ", "Tab 选项 · ") if self.labels else "") + tr(
                "Ctrl+J for a new line · Enter to submit", "Ctrl+J 换行 · Enter 提交",
            )
        if self.open_question:
            return tr("Tab for options · Ctrl+J for a new line in your note · Enter to submit",
                      "Tab 选项 · Ctrl+J 换行写备注 · Enter 提交")
        return ""
