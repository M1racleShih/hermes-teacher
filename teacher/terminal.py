"""Verified classroom control over an explicitly selected local terminal server."""

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import sys
import tempfile
import time
import uuid

from .i18n import localized, profile_argument, profile_language, tr, use_profile

class TerminalError(RuntimeError):
    pass


class StaleTerminal(TerminalError):
    pass


def expired_handle():
    return StaleTerminal(tr("The classroom terminal handle has expired. List the lessons again.",
                            "课堂终端句柄已失效，请重新列出课堂"))


PROJECT = Path(__file__).resolve().parents[1]
KEY = re.compile(
    r"(?:enter|esc|tab|backspace|delete|home|end|pageup|pagedown|up|down|left|right|f(?:[1-9]|1[0-2])|ctrl\+[a-z])\Z"
)
_RESUME = "import base64,os,sys;a=[base64.urlsafe_b64decode(x).decode() for x in sys.argv[1:]];os.chdir(a[0]);os.execv(a[1],a[1:])"


def process_identity(pid):
    fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    if fields[0] == "Z":
        raise StaleTerminal(tr("The classroom process has exited.", "课堂进程已经结束"))
    return [int(fields[2]), fields[19], Path("/proc/sys/kernel/random/boot_id").read_text().strip()]


def metadata(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise TerminalError(tr("The classroom metadata has invalid permissions or file type.", "课堂元数据权限或类型错误"))
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TerminalError(tr("The classroom metadata has an invalid format.", "课堂元数据格式错误"))
    return value


def store(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as stream:
        staging = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(staging, path)
        finally:
            staging.unlink(missing_ok=True)


class Backend:
    def __init__(self, name, address):
        if name not in {"herdr", "core"} or not Path(address).is_absolute():
            raise TerminalError(tr("Specify herdr or Core and the absolute path to the server socket.", "请指定 herdr/Core 和服务器的绝对 socket 路径"))
        self.name = name
        self.address = str(address)

    def fingerprint(self):
        info = Path(self.address).lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise TerminalError(tr("The terminal server socket has an invalid type or owner.", "终端服务器 socket 的类型或所有者错误"))
        return [info.st_dev, info.st_ino]

    def call(self, method, **params):
        self.fingerprint()
        nonce = uuid.uuid4().hex
        request = {"id": nonce, "method": method, "params": params}
        if self.name == "core":
            request["jsonrpc"] = "2.0"
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(4)
                connection.connect(self.address)
                connection.sendall(json.dumps(request, ensure_ascii=False).encode() + b"\n")
                with connection.makefile("rb") as stream:
                    for _ in range(60):
                        line = stream.readline(8 * 1024 * 1024)
                        if not line.endswith(b"\n"):
                            raise TerminalError(tr(f"{method} received an incomplete response. Check the target status.",
                                                   f"{method} 未收到完整答复，请核对目标状态"))
                        answer = json.loads(line)
                        if "id" not in answer:
                            continue
                        if answer.get("id") != nonce or (
                            self.name == "core" and answer.get("jsonrpc") != "2.0"
                        ):
                            raise TerminalError(tr(f"{method} received a response with an invalid ID.", f"{method} 答复身份错误"))
                        if "error" in answer:
                            code = answer["error"].get("data", {}).get("kind") or answer["error"].get("code")
                            raise TerminalError(tr(f"{method} failed ({code}).", f"{method} 失败（{code}）"))
                        if not isinstance(answer.get("result"), dict):
                            raise TerminalError(tr(f"{method} received a response without a result.", f"{method} 答复缺少结果"))
                        return answer["result"]
        except (OSError, ValueError) as exc:
            raise TerminalError(tr(
                f"{method} lost its connection or received an invalid response. Check the target status.",
                f"{method} 连接中断或答复异常，请核对目标状态",
            )) from exc
        raise TerminalError(tr(f"{method} received no response to its request.", f"{method} 未收到请求的答复"))

    def identity(self):
        address = self.fingerprint()
        if self.name == "core":
            reply = self.call("hello", generation=1)
            if reply.get("generation") != 1 or not reply.get("core", {}).get("id"):
                raise TerminalError(tr("The Herdr Core generation 1 terminal interface is required.", "需要 Herdr Core generation 1 终端接口"))
            return [*address, reply["core"]["id"]]
        protocol = self.call("ping").get("protocol", 0)
        if protocol < 22:
            raise TerminalError(tr("The herdr socket protocol version 22 or later is required.", "需要 herdr socket 协议 22 或更新版本"))
        return address

    def foreground(self, pane, terminal, pid, group):
        if self.name == "core":
            actual = self.call("terminal.get", terminal=terminal)
            return (
                actual.get("address") == terminal
                and actual.get("state") == "live"
                and (actual.get("foreground") or {}).get("pid") == pid
            )
        actual = self.call("pane.current", caller_pane_id=pane)["pane"]
        processes = self.call("pane.process_info", pane_id=actual["pane_id"])["process_info"]
        return (
            actual["terminal_id"] == terminal
            and processes.get("foreground_process_group_id") == group
            and any(proc.get("pid") == pid for proc in processes.get("foreground_processes", []))
        )

    def start(self, argv, lesson):
        if self.name == "core":
            return self.call(
                "terminal.run",
                argv=argv,
                cwd=str(PROJECT),
                keep=True,
                no_parent=True,
                no_notify=True,
                labels={"hermes-teacher": lesson},
            )
        created = self.call("workspace.create", cwd=str(PROJECT), label=f"Teacher {lesson[:12]}", focus=False)
        pane = created["root_pane"]
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if self.call("pane.get", pane_id=pane["pane_id"])["pane"]["terminal_id"] != pane["terminal_id"]:
                raise TerminalError(tr("The terminal identity changed before the lesson could start.", "启动课堂前终端身份已变更"))
            process = self.call("pane.process_info", pane_id=pane["pane_id"])["process_info"]
            if process.get("foreground_process_group_id") == process.get("shell_pid") and process.get(
                "shell_pid"
            ):
                return self.call(
                    "pane.send_input", pane_id=pane["pane_id"], text=shlex.join(argv), keys=["enter"]
                )
            time.sleep(0.05)
        raise TerminalError(tr("The shell in the new terminal is not ready.", "新终端的 shell 未就绪"))

    def read(self, run, lines):
        if self.name == "core":
            return self.call("terminal.read", terminal=run["terminal"], lines=lines)["text"]
        return self.call("pane.read", pane_id=run["pane"], source="visible", lines=lines)["read"]["text"]

    def keys(self, run, keys):
        if not keys or any(KEY.fullmatch(key) is None for key in keys):
            raise TerminalError(tr("Unsupported key name.", "按键名称不受支持"))
        if self.name == "core":
            return self.call("terminal.keys", terminal=run["terminal"], keys=keys)
        return self.call("pane.send_keys", pane_id=run["pane"], keys=keys)

    def send(self, run, text, *, submit=True):
        if not text.strip() or any(
            ord(char) < 32 and char not in "\t\n" or ord(char) == 127 for char in text
        ):
            raise TerminalError(tr("Provide the user's text. Send control keys through keys.", "请提供用户原文，控制键通过 keys 发送"))
        if self.name == "core":
            if not submit:
                raise TerminalError(tr(
                    "Core submits text immediately. To compose a draft, take over the terminal and use its input field.",
                    "Core 的文字控制会立即提交。请在接管后的编辑框中填写草稿。",
                ))
            return self.call("terminal.send", terminal=run["terminal"], text=text, force=True)
        return self.call("pane.send_input", pane_id=run["pane"], text=text, keys=["enter"] if submit else [])

    def attach(self, run):
        if self.name == "core":
            return shlex.join(
                [
                    "env",
                    "-u",
                    "HC_SESSION",
                    "-u",
                    "HC_TERMINAL_SOCKET",
                    "-u",
                    "HC_TERMINAL_ID",
                    f"HC_SOCKET={self.address}",
                    "hc",
                    "attach",
                    run["terminal"],
                ]
            )
        return shlex.join(
            [
                "env",
                f"HERDR_SOCKET_PATH={self.address}",
                "herdr",
                "terminal",
                "attach",
                run["terminal"],
                "--takeover",
            ]
        )


class TerminalControl:
    def __init__(self, profile):
        self.profile = Path(profile).resolve(strict=True)
        self.directory = self.profile / "classroom-terminals"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise TerminalError(tr("The classroom terminal directory must be private to the current user.",
                                   "课堂终端目录必须由当前用户独占", language=profile_language(self.profile)))

    def path(self, lesson, kind):
        name = hashlib.sha256(lesson.encode()).hexdigest()
        return self.directory / f"{name}.{kind}"

    @contextlib.contextmanager
    def lock(self, lesson):
        descriptor = os.open(self.path(lesson, "lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def verified(self, key):
        run = metadata(self.path(key, "run"))
        launch = metadata(self.path(run["token"], "launcher"))
        if run["key"] != key or launch["key"] != key or run["state"] == "exited":
            raise expired_handle()
        if process_identity(run["pid"]) != run["process"]:
            raise StaleTerminal(tr("The classroom process identity changed. List the lessons again.", "课堂进程身份已更换，请重新列出课堂"))
        backend = Backend(launch["backend"], launch["socket"])
        if backend.identity() != run["server"] or not backend.foreground(
            run["pane"], run["terminal"], run["pid"], run["process"][0]
        ):
            raise TerminalError(tr("The server or foreground terminal identity changed. List the lessons again.", "服务器或终端前台身份已更换，请重新列出课堂"))
        run["owner"] = self.owner(launch, run["lesson"])
        return backend, run

    def owner(self, launch, lesson):
        if lesson is None or launch["owner"] == "user":
            return "user"
        try:
            return metadata(self.path(lesson, "owner"))["owner"]
        except FileNotFoundError:
            return "agent"

    @localized
    def list(self):
        result = []
        for path in sorted(self.directory.glob("*.run")):
            try:
                key = metadata(path)["key"]
                backend, run = self.verified(key)
            except (OSError, TerminalError, KeyError, ValueError):
                continue
            result.append(self.summary(backend, run))
        return result

    def summary(self, backend, run):
        from .records import Records

        context = {}
        if run["lesson"] and (self.profile / "teacher/records.sqlite3").is_file():
            lesson = Records(self.profile).get(run["lesson"])
            if lesson:
                context = {field: lesson[field] for field in ("topic", "vault", "session_id", "record", "recording")}
        return {
            **context,
            "lesson": run["lesson"],
            "state": run["state"],
            "owner": run["owner"],
            "handle": f"{run['key']}@{run['generation']}",
            "backend": backend.name,
            "terminal": run["terminal"],
            "attach": backend.attach(run),
        }

    @localized
    def start(self, lesson, backend=None, *, foreground=False, timeout=15):
        with self.lock("start:" + lesson):
            with self.lock("registry"):
                try:
                    active_backend, run = self.verified(lesson)
                except (FileNotFoundError, ProcessLookupError, StaleTerminal):
                    run = None
                if run:
                    if (
                        foreground
                        or backend
                        and (active_backend.name, active_backend.address) != (backend.name, backend.address)
                    ):
                        raise TerminalError(tr("This lesson is already running in another terminal.", "这节课已经在另一个终端运行"))
                    return self.summary(active_backend, run)
                if backend is None:
                    raise TerminalError(tr("Specify a terminal server to start the lesson.", "启动时需要明确的终端服务器"))
                server = backend.identity()
                try:
                    previous = metadata(self.path(lesson, "launch"))
                    old = metadata(self.path(previous["token"], "launcher"))
                except FileNotFoundError:
                    old = None
                if old and not old.get("ready") and old["server"] == server:
                    raise TerminalError(tr("The previous launch needs verification. Check the terminal server.", "上次启动结果待核对，请检查这台终端服务器"))
                token = uuid.uuid4().hex
                record = {
                    "token": token,
                    "lesson": lesson,
                    "key": lesson,
                    "owner": "user" if foreground else "agent",
                    "backend": backend.name,
                    "socket": backend.address,
                    "server": server,
                    "ready": False,
                }
                store(self.path(token, "launcher"), record)
                store(self.path(lesson, "launch"), {"token": token})
            argv = self.argv(lesson, token)
            if foreground:
                return argv
            backend.start(argv, lesson)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    actual, run = self.verified(lesson)
                    if run["token"] == token and run["state"] in {"waiting", "blocked"}:
                        return self.summary(actual, run)
                except (OSError, TerminalError):
                    pass
                time.sleep(0.10)
        raise TerminalError(tr("The lesson launch was submitted. Check the terminal for its status.", "课堂启动已提交但未报告就绪，请核对终端状态"))

    def argv(self, lesson, token):
        stub = [
            str(PROJECT),
            sys.executable,
            "-m",
            "teacher.terminal",
            "--profile",
            str(self.profile),
            "_launch",
            lesson,
            token,
        ]
        # The working directory was set to the project for both backends. Encoding
        # each argument keeps spaces and shell metacharacters intact in herdr's resume argv.
        return [
            sys.executable,
            "-c",
            _RESUME,
            *[base64.urlsafe_b64encode(arg.encode()).decode() for arg in stub],
        ]

    @localized
    def interact(self, handle, action, value=None):
        lesson, sep, generation = handle.rpartition("@")
        if not sep:
            raise TerminalError(tr("Use a classroom handle returned by list.", "请使用 list 返回的课堂句柄"))
        with self.lock("registry"):
            try:
                backend, run = self.verified(lesson)
            except FileNotFoundError as exc:
                raise expired_handle() from exc
            if generation != run["generation"]:
                raise expired_handle()
            if action == "read":
                return backend.read(run, max(1, min(value, 300)))
            if action == "handoff":
                path = self.path(run["token"], "launcher")
                launch = metadata(path)
                store(path, {**launch, "owner": "user"})
                if run["lesson"] is not None:
                    store(self.path(lesson, "owner"), {"owner": "user"})
                return self.summary(backend, {**run, "owner": "user"})
            if run["owner"] != "agent":
                raise TerminalError(tr("The user has taken over. Classroom control can only read the screen.", "用户已经接管，课堂控制仅允许读屏"))
            if action in {"paste", "send"}:
                if run["state"] not in {"waiting", "blocked"}:
                    raise TerminalError(tr("The classroom is not waiting for input.", "课堂当前未等待输入"))
                return backend.send(run, value, submit=action == "send")
            if run["state"] == "working" and value != ["ctrl+c"]:
                raise TerminalError(tr("The classroom is working and only accepts interrupt keys.", "课堂正在工作，只接受中断键"))
            return backend.keys(run, value)


_RUNTIME = None


class TerminalSelection:
    """Keep the old terminal seat until the target lesson has been admitted."""

    def __init__(self, profile):
        self.profile = profile
        self.current = None
        self.held = contextlib.ExitStack()

    def close(self):
        self.held.close()

    @contextlib.contextmanager
    def move(self, lesson):
        from .library import BusyError, file_lease

        if lesson == self.current:
            yield
            return
        with contextlib.ExitStack() as target:
            if lesson:
                try:
                    target.enter_context(file_lease(self.profile / "teacher", f"seats/{lesson}.lock"))
                except BusyError:
                    raise TerminalError(occupied_lesson_message(self.profile)) from None
            yield
            report_status("working", lesson)
            self.held.close()
            self.held = target.pop_all()
            self.current = lesson


def occupied_lesson_message(profile):
    return tr("Another terminal is using that lesson. The current lesson is still open.",
              "这节课已由其他终端占用，仍留在原课堂。", language=profile_language(profile))


def report_status(state, lesson_id=None):
    """Publish the actual displayed lesson and foreground state of a managed terminal."""
    global _RUNTIME
    token = os.environ.get("HERMES_TEACHER_TERMINAL_TOKEN")
    profile = os.environ.get("HERMES_TEACHER_TERMINAL_PROFILE")
    if not token or not profile or state not in {"waiting", "working", "blocked", "idle", "exited"}:
        return
    control = TerminalControl(profile)
    launch_path = control.path(token, "launcher")
    launch = metadata(launch_path)
    backend = Backend(launch["backend"], launch["socket"])
    if _RUNTIME is None:
        terminal = os.environ["HC_TERMINAL_ID"] if backend.name == "core" else None
        pane = os.environ["HERDR_PANE_ID"] if backend.name == "herdr" else None
        if pane:
            info = backend.call("pane.current", caller_pane_id=pane)["pane"]
            pane = info["pane_id"]
            terminal = info["terminal_id"]
        identity = process_identity(os.getpid())
        deadline = time.monotonic() + 3
        while not backend.foreground(pane, terminal, os.getpid(), identity[0]):
            if time.monotonic() >= deadline:
                raise TerminalError(tr("The classroom process is not in the declared foreground terminal.", "课堂进程未占用声明的前台终端"))
            time.sleep(0.05)
        _RUNTIME = {
            "token": token,
            "terminal": terminal,
            "pane": pane,
            "server": backend.identity(),
            "pid": os.getpid(),
            "process": identity,
            "key": None,
            "generation": None,
        }
    key = lesson_id or "lobby-" + token
    with control.lock("registry"):
        launch = metadata(launch_path)
        visible_lesson = lesson_id
        if state == "exited" and _RUNTIME["key"] is not None:
            key = _RUNTIME["key"]
        elif lesson_id and key != _RUNTIME["key"]:
            try:
                _, other = control.verified(key)
            except (OSError, StaleTerminal):
                other = None
            if other and other["token"] != token:
                raise TerminalError(occupied_lesson_message(profile))
        if key != _RUNTIME["key"] or launch["lesson"] != lesson_id:
            old_key = _RUNTIME["key"]
            if old_key and old_key != key:
                old_path = control.path(old_key, "run")
                old = metadata(old_path)
                if old["token"] == token:
                    store(old_path, {**old, "state": "exited"})
            _RUNTIME.update(key=key, generation=uuid.uuid4().hex)
            launch.update(lesson=lesson_id, key=key, ready=True)
            store(launch_path, launch)
        if backend.name == "herdr":
            if state == "exited":
                backend.call("pane.release_agent", pane_id=_RUNTIME["pane"], source="hermes-teacher")
            else:
                mapping = {"waiting": "idle", "idle": "idle", "working": "working", "blocked": "blocked"}
                recovery_args = control.argv(lesson_id or "", token)
                recovery_args[0] = "python3"
                backend.call(
                    "pane.report_agent",
                    pane_id=_RUNTIME["pane"],
                    source="hermes-teacher",
                    agent="hermes",
                    state=mapping[state],
                    seq=time.time_ns() // 1_000_000,
                    agent_session_id=lesson_id or token,
                    resume_argv=recovery_args,
                )
        else:
            status = {
                "state": {"waiting": "idle", "exited": "done"}.get(state, state),
                "app": "hermes-teacher",
            }
            if state == "blocked":
                status["kind"] = "question"
            sys.stdout.write("\033]7501;" + json.dumps(status, separators=(",", ":")) + "\a")
            sys.stdout.flush()
        store(control.path(key, "run"), {**_RUNTIME, "state": state, "lesson": visible_lesson})


def main(argv=None):
    profile = profile_argument(argv)
    try:
        with use_profile(profile) if profile is not None else contextlib.nullcontext():
            return run(argv)
    except (OSError, ValueError) as exc:
        print(f"Teacher: {exc}", file=sys.stderr)
        return 1


def run(argv):
    parser = argparse.ArgumentParser(description=tr("Control a terminal classroom in a dedicated Teacher profile.",
                                                    "控制专用 Teacher profile 的终端课堂"))
    parser.add_argument("--profile", required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list")
    for action in ("start", "open"):
        cmd = commands.add_parser(action)
        target = cmd.add_mutually_exclusive_group(required=True)
        target.add_argument("lesson", nargs="?")
        target.add_argument("--topic", help=tr("Prepare or reuse a lesson by topic.", "按主题准备并复用课堂"))
        cmd.add_argument("--vault", type=Path)
        cmd.add_argument("--new", action="store_true", help=tr("Prepare a separate lesson for this topic.", "为该主题准备独立新课"))
        cmd.add_argument("--backend", choices=["herdr", "core"])
        cmd.add_argument("--socket")
    for action in ("read", "send", "paste", "keys", "handoff"):
        cmd = commands.add_parser(action)
        cmd.add_argument("handle")
        if action == "read":
            cmd.add_argument("--lines", default=80, type=int)
        if action == "keys":
            cmd.add_argument("keys", nargs="+")
    child = commands.add_parser("_launch", help=argparse.SUPPRESS)
    child.add_argument("lesson")
    child.add_argument("token")
    args = parser.parse_args(argv)
    try:
        control = TerminalControl(args.profile)
        if args.action == "_launch":
            launch = metadata(control.path(args.token, "launcher"))
            if launch["token"] != args.token:
                raise TerminalError(tr("The launch token has expired.", "启动令牌已失效"))
            os.environ.update(
                HERMES_TEACHER_TERMINAL_TOKEN=args.token, HERMES_TEACHER_TERMINAL_PROFILE=str(control.profile)
            )
            os.chdir(PROJECT)
            argv = [sys.executable, "-m", "teacher", "--profile", str(control.profile), "chat"]
            if launch["lesson"] is not None:
                argv.extend(["--lesson", launch["lesson"]])
            os.execv(sys.executable, argv)
        if args.action in {"open", "start"}:
            if args.topic:
                from .models import read_config
                from .records import Records

                vault = args.vault or Path(read_config(control.profile / "config.yaml")["teacher"]["vault"])
                args.lesson = Records(control.profile).prepare(vault, args.topic, new=args.new)["id"]
            elif args.new or args.vault:
                parser.error(tr("--vault and --new require --topic.", "--vault 和 --new 用于按 --topic 开课"))
            if args.action == "open" and not args.backend and not args.socket:
                if os.environ.get("HC_TERMINAL_ID"):
                    args.backend = "core"
                    args.socket = os.environ.get("HC_TERMINAL_SOCKET")
                elif os.environ.get("HERDR_PANE_ID"):
                    args.backend = "herdr"
                    args.socket = os.environ.get("HERDR_SOCKET_PATH")
            if args.action == "open" and not args.backend and not args.socket:
                os.chdir(PROJECT)
                os.execv(
                    sys.executable,
                    [
                        sys.executable,
                        "-m",
                        "teacher",
                        "--profile",
                        str(control.profile),
                        "chat",
                        "--lesson",
                        args.lesson,
                    ],
                )
            if not args.backend or not args.socket:
                raise TerminalError(tr("Specify both --backend and --socket.", "同时指定 --backend 与 --socket"))
            backend = Backend(args.backend, args.socket)
            result = control.start(args.lesson, backend, foreground=args.action == "open")
            if args.action == "open":
                os.chdir(PROJECT)
                os.execv(result[0], result)
        elif args.action == "list":
            result = control.list()
        else:
            argument = {"read": lambda: args.lines, "send": sys.stdin.read, "paste": sys.stdin.read, "keys": lambda: args.keys}
            result = control.interact(
                args.handle, args.action, argument[args.action]() if args.action in argument else None
            )
        print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))
    except (TerminalError, KeyError, OSError, ValueError) as exc:
        print(f"Teacher: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
