"""Interactive classroom, profile preparation, and vault operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import unicodedata

from .i18n import get_language, profile_argument, tr, use_language, use_profile


def show(value):
    if value is not None:
        print(
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str),
            flush=True,
        )


def quiz_input(text, status):
    first = unicodedata.normalize("NFKC", text.strip("\r\n").split("\n", 1)[0].strip()).casefold()
    if status == "feedback":
        return first in {"继续", "确认", "已读", "continue", "confirm", "read"}
    return (
        first[:1].isdigit()
        or bool(re.fullmatch(r"[A-La-l]+(?:[\s,、]+[A-La-l]+)*", first))
        or first in {"不知道", "我不知道", "不确定", "i don't know", "i don’t know", "unsure"}
        or first in {"提示", "/hint", "hint", "取消", "/cancel", "cancel"}
    )


HELP_EN = """Classroom navigation
/topic TOPIC    Resume or prepare a lesson   /new TOPIC      Start a separate lesson
/list KEYWORD   Search recent lessons        /open ID        Open a lesson by number or ID
/lobby         Return to the lobby          /resume         Resume the most recent lesson
/workspace PATH   Switch libraries          /vaults         Show recent libraries
/browse PATH      Browse directories        /route TOPIC    Create a separate topic library
Lesson commands
/model [PROVIDER/MODEL]   /reasoning LEVEL   /material [PATH]   /status   /session
/record [on|off|bind PATH]   /checkpoint   /seal   /handoff   /handoff-retry   /organize
/role start ROLE TASK       /role supplement TASK_ID DETAILS
/role status|cancel|resume TASK_ID   /models-import [--dry-run] FILE
/help   /quit
Send plain text to the teacher. Questions and feedback include response instructions.
In an interactive terminal, press Tab to select options, Ctrl+J for a new line in an answer or note,
and Enter to continue after reading feedback."""

HELP_ZH = """课堂导航
/topic 主题      续接或准备课堂       /new 主题       开同名独立课堂
/list 关键词     搜索最近课堂         /open 序号或ID   打开选中的课堂
/lobby          返回大厅             /resume        续接最近讨论
/workspace 路径  切换资料库           /vaults        最近资料库
/browse 路径     浏览目录             /route 主题     创建并关联独立主题库
课堂操作
/model [服务/模型]   /reasoning 强度   /material [路径]   /status   /session
/record [on|off|bind 路径]   /checkpoint   /seal   /handoff   /handoff-retry   /organize
/role start 角色 任务         /role supplement 任务ID 补充说明
/role status|cancel|resume 任务ID   /models-import [--dry-run] 配置文件
/help   /quit
普通文字提交给教师。题面和反馈显示作答方式。
交互终端支持 Tab 选项、Ctrl+J 换行写备注或补充偏好，读完解析后按 Enter 继续。"""


def chat(tutor, args, claim):
    from .extensions import Extensions
    from .models import read_config
    from .navigation import Navigator
    from .role_notice import recorded_replies, watch_replies
    from .runtime import feedback_confirmation
    from .terminal import report_status

    vault = args.vault or Path(read_config(tutor.profile / "config.yaml")["teacher"]["vault"])
    nav = Navigator(tutor, vault, args.lesson, claim=claim, tool_limits=tool_limits(args))
    if args.topic:
        nav.choose(topic=args.topic, new=args.new)
    elif args.resume:
        nav.action("resume", "")
    ext = Extensions(
        tutor.profile,
        compact=tutor.compact,
        rename=tutor.rename,
        turn=tutor.turn,
        pending=tutor.learning.pending,
    )
    prompt = confirmation_key = None
    if sys.stdin.isatty() and sys.stdout.isatty():
        from .terminal_quiz import CONFIRM_FEEDBACK, TerminalInput

        suggestions = (lambda text: ext.complete(nav.lesson, text)) if "search" in ext.flags else None
        prompt, confirmation_key = TerminalInput(file_suggestions=suggestions), CONFIRM_FEEDBACK

    def read(label, default="", *, quiz=None, watch=None):
        if prompt:
            return prompt.read(label, default, quiz=quiz, watch=watch)
        if default:
            show(tr("Draft (Enter to submit) ", "草稿（回车提交） ") + default)
        return input(label) or default

    def report(state):
        report_status(state, nav.lesson["id"] if nav.lesson else None)

    lobby = tr("Classroom lobby", "课堂大厅")
    show(f"{nav.lesson['topic'] if nav.lesson else lobby} · {nav.vault}")
    show(tr("Enter /help to see commands.", "输入 /help 查看操作。"))
    if not nav.lesson:
        show(nav.action("list", ""))
    visible_quiz = None
    last_output = None
    seen_roles = {}

    def observe(lesson):
        key = lesson["id"]
        if key not in seen_roles:
            seen_roles[key] = {identity for identity, _ in recorded_replies(tutor.records, key)}

        async def display(reply):
            nonlocal visible_quiz, last_output
            card = tutor.learning.interaction_view(key)
            visible_quiz = (key, card["id"], card["status"]) if card else None
            last_output = (key, reply)
            await prompt.announce(lambda: show(reply), card)
            report("blocked" if tutor.learning.pending(key) else "waiting")

        return lambda: watch_replies(tutor, key, seen_roles[key], display)

    if nav.lesson:
        pending = tutor.learning.pending(nav.lesson["id"])
        if pending:
            show(pending["text"])
            last_output = (nav.lesson["id"], pending["text"])
    try:
        while True:
            ready = nav.lesson
            if ready:
                nav.lesson = ready = tutor.lesson(ready["id"])
            try:
                pending = tutor.learning.pending(ready["id"]) if ready else None
                quiz = None
                if prompt and pending and pending["kind"] in {"quiz", "ask"}:
                    card = tutor.learning.interaction_view(ready["id"])
                    if card and (card["id"], card["status"]) == (pending["id"], pending["status"]):
                        quiz = card
                if quiz:
                    shown = (ready["id"], quiz["id"], quiz["status"])
                    if shown != visible_quiz and (
                        last_output is None
                        or last_output[0] != ready["id"]
                        or pending["text"] not in last_output[1]
                    ):
                        show(pending["text"])
                    visible_quiz = shown
                else:
                    visible_quiz = None

                report("blocked" if pending else "waiting")
                text = read(
                    f"{ready['topic'] if ready else lobby} › ",
                    nav.draft,
                    quiz=quiz if not nav.selecting else None,
                    watch=observe(ready) if prompt and ready and not nav.selecting else None,
                )
                if prompt:
                    quiz = prompt.quiz
                confirm_feedback = text is confirmation_key
                if confirm_feedback:
                    text = tr("continue", "继续")
                cancel_list = nav.selecting and text.strip().casefold() in ("cancel", "取消", "/cancel")
                if not text.strip():
                    continue
                if text.strip() in {"/quit", "/exit"}:
                    return
                if cancel_list:
                    show(nav.action("cancel", ""))
                    continue
                nav.selecting = False
                report("working")
                cmd, _, arg = text.strip().partition(" ")
                name = cmd.removeprefix("/")
                if text.startswith("/") and name not in {"answer", "hint", "cancel"}:
                    if name == "help":
                        show(tr(HELP_EN, HELP_ZH))
                        commands = [
                            "/model-info" if k == "model" else "/" + k for k in sorted(ext.flags - {"ui"})
                        ]
                        if commands:
                            show(tr("Enabled extensions · ", "已启用的个人增强 · ") + " ".join(commands))
                        if "search" in ext.flags:
                            show(tr(
                                "Type @KEYWORD or /material PARTIAL_PATH and press Tab to complete the path.",
                                "资料路径输入 @关键词 或 /material 路径片段，按 Tab 补全。",
                            ))
                    elif name in {"workspace", "vault"}:
                        if not arg:
                            show(nav.action("vaults", ""))
                            continue
                        target = nav.workspace_path(arg)
                        if not target.exists():
                            report("blocked")
                        if (
                            not target.exists()
                            and read(tr(f"Create library {target}? Enter create to confirm › ",
                                        f"新建资料库 {target}？输入「创建」确认 › ")).strip().casefold()
                            not in {"create", "创建"}
                        ):
                            show(tr("Cancelled. Still in ", "已取消，仍在 ") + str(nav.vault))
                            continue
                        nav.switch(target, create=not target.exists())
                        show(f"{nav.lesson['topic'] if nav.lesson else lobby} · {nav.vault}")
                    elif name in {"handoff", "lesson-next"}:
                        report("blocked")
                        decision = tr("confirm", "确认") if name == "lesson-next" else arg or read(tr(
                            "Archive this lesson and start a new session with a handoff? Enter confirm › ",
                            "封存本课并带说明换会话？输入「确认」 › ",
                        ))
                        show(nav.action("handoff", decision))
                    elif name == "lesson-end":
                        show(nav.action("seal", ""))
                    elif name == "classroom":
                        show(nav.action("list", arg))
                    elif name == "organize" and ready:
                        show(tutor.organize(ready["id"]))
                    else:
                        value = None
                        if ready and name not in {"model", "list"}:
                            value = ext.invoke(ready, name, arg)
                        if value is None:
                            value = nav.action(name, arg)
                        if isinstance(value, dict) and {"id", "topic", "vault"} <= value.keys():
                            show(f"{value['topic']} · {value['id']}\n{value['vault']}")
                        else:
                            show(value)
                    continue
                if nav.prose(text):
                    if nav.draft:
                        show(tr("The lesson is ready. Edit your goal and press Enter to submit.",
                                "课堂已准备好，编辑目标后回车提交。"))
                    elif nav.lesson:
                        show(f"{nav.lesson['topic']} · {nav.lesson['id']}")
                    else:
                        show(nav.action("list", ""))
                    continue
                nav.draft = ""
                current = nav.lesson
                if name == "answer" and text.startswith("/"):
                    qid, sep, answer = arg.partition(" ")
                    if not sep:
                        raise ValueError(tr("Use /answer QUESTION_ID ANSWER.", "用 /answer 题目ID 实际作答。"))
                    response = tutor.turn(current["id"], answer, expected_id=qid)
                elif confirm_feedback:
                    confirmation, event_id = feedback_confirmation(tutor.learning, current["id"], quiz["id"])
                    response = tutor.turn(
                        current["id"], confirmation, expected_id=quiz["id"], event_id=event_id,
                    )
                elif quiz and (quiz["kind"] == "ask" or quiz_input(text, quiz["status"])):
                    response = tutor.turn(current["id"], text, expected_id=quiz["id"])
                else:
                    response = tutor.turn(current["id"], text)
                show(response)
                last_output = (current["id"], response)
                current = tutor.lesson(current["id"])
                ext.auto_title(current, text)
                for more in ext.continue_goal(current, response):
                    show(more)
                show(ext.display(current))
            except KeyboardInterrupt:
                show(tr("Input interrupted. Use /status to check the lesson or /quit to leave.",
                        "当前输入已中断。可用 /status 核对原课，或用 /quit 离开。"))
            except EOFError:
                return
            except (ValueError, RuntimeError, OSError) as exc:
                show(str(exc))
    finally:
        report("exited")


def tool_limits(args):
    if args.tools or args.exclude_tools or args.no_tools:
        return args.tools, args.exclude_tools, args.no_tools
    return None


def parser():
    p = argparse.ArgumentParser(description=tr("Hermes Teacher classroom", "Hermes Teacher 课堂"))
    p.add_argument("-p", "--profile", type=Path, default=Path.home() / ".hermes/profiles/teacher")
    sub = p.add_subparsers(dest="command")
    init = sub.add_parser("init", help=tr("Install the plugin and set up a classroom in an empty directory.",
                                          "在空目录安装插件并创建课堂配置"))
    init.add_argument("--language", choices=("en", "zh"), help=tr("Classroom language (default: en).",
                                                                 "课堂语言（默认 en）"))
    init.add_argument("--vault", type=Path, required=True)
    init.add_argument("--source-profile", type=Path)
    init.add_argument("--provider", default="newapi")
    init.add_argument("--model", default="Qwen3.8-Flash-Next")
    for name in ("chat", "prepare"):
        control = sub.add_parser(name)
        control.add_argument("--vault", type=Path)
        group = control.add_mutually_exclusive_group()
        group.add_argument("--lesson")
        group.add_argument("--topic")
        group.add_argument("--resume", action="store_true")
        control.add_argument("--new", action="store_true")
        control.add_argument("--tools", action="append", help=tr("Comma-separated tools allowed in this lesson.",
                                                                 "本课允许的工具名，用逗号分隔"))
        control.add_argument("--exclude-tools", action="append", help=tr(
            "Comma-separated tool names or patterns to exclude from this lesson.", "从本课排除以逗号分隔的工具名或通配模式",
        ))
        control.add_argument("--no-tools", "-nt", action="store_true", help=tr("Disable model tools for this lesson.",
                                                                              "关闭本课模型工具"))
    ops = sub.add_parser("assets", help=tr("Organize archived records and move libraries.",
                                          "整理封存记录和迁移资料库"))
    ops.add_argument("action", choices=["scan", "snapshot", "check", "apply", "move-topic", "move-root"])
    ops.add_argument("--vault", type=Path, required=True)
    ops.add_argument("--topic")
    ops.add_argument("--source", type=Path, help=tr("Create an organization snapshot for one archived transcript.",
                                                    "为单份封存原文建立整理快照"))
    ops.add_argument("--run", type=Path)
    ops.add_argument("--bundle", type=Path)
    ops.add_argument("--destination", type=Path)
    ops.add_argument("--check", action="store_true")
    op = sub.add_parser("session", help=tr("Prepare the lesson's session for the Hermes App.",
                                           "准备供 Hermes App 打开的同课会话"))
    op.add_argument("--lesson", required=True)
    return p


def main(argv=None):
    profile = profile_argument(argv, default=Path.home() / ".hermes/profiles/teacher", short=True)
    with use_profile(profile):
        p = parser()
        args = p.parse_args(argv)
        if not args.command:
            args = p.parse_args(["--profile", str(args.profile), "chat"])
        with use_language(getattr(args, "language", None) or get_language()):
            run(p, args)


def run(p, args):
    from .profile import initialize

    if args.command == "init":
        show(
            str(
                initialize(
                    args.profile,
                    args.vault,
                    source=args.source_profile,
                    provider=args.provider,
                    model=args.model,
                    language=args.language,
                )
            )
        )
        return
    from .runtime import Tutor

    tutor = Tutor(args.profile)
    if args.command == "chat":
        from contextlib import closing
        from .terminal import TerminalSelection

        with closing(TerminalSelection(tutor.profile)) as seat:
            chat(tutor, args, seat.move)
    elif args.command == "prepare":
        from .models import read_config
        from .navigation import Navigator

        nav = Navigator(
            tutor,
            args.vault or Path(read_config(tutor.profile / "config.yaml")["teacher"]["vault"]),
            args.lesson,
            tool_limits=tool_limits(args),
        )
        if args.topic:
            show(nav.choose(topic=args.topic, new=args.new))
        elif args.resume:
            show(nav.action("resume", ""))
        else:
            show(nav.lesson or nav.catalog())
    elif args.command == "session":
        import asyncio

        show(asyncio.run(tutor.open(args.lesson)))
    elif args.command == "assets":
        from .organizer import Organizer
        from .roles import Roles

        organizer = Organizer(tutor.records, roles_busy=Roles(tutor.profile).busy)
        if args.action == "snapshot":
            show(organizer.snapshot(args.vault, topic=args.topic, source=args.source))
        elif args.action == "scan":
            show(organizer.scan(args.vault, topic=args.topic))
        elif args.action in ("check", "apply") and args.run and args.bundle:
            show(organizer.apply(args.vault, args.run, args.bundle, check=args.action == "check"))
        elif args.action == "move-topic" and args.topic:
            show(organizer.migrate_topic(args.vault, args.topic, args.destination, check=args.check))
        elif args.action == "move-root" and args.destination:
            show(organizer.relocate(args.vault, args.destination, check=args.check))
        else:
            p.error(tr("Organization requires --run and --bundle. Migration requires --topic or --destination.",
                       "整理需要 --run 与 --bundle，迁移需要 --topic 或 --destination"))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        sys.exit(str(error))
