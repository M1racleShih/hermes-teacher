"""Run and resume classroom specialists under their original lesson and role."""

from contextlib import contextmanager
from contextvars import copy_context
import fcntl
import json
from pathlib import Path
import re
import threading
import time
import uuid

from .i18n import get_language, tr, use_profile
from .models import ModelSettings, ROLES, write_private
from .role_runner import HermesRoleRunner
from .visuals import Preview, publish


ACTIVE = {"running", "cancelling"}
RECOVERABLE = {"cancelled", "interrupted", "failed"}


class _Cancelled(Exception):
    pass


class _Control:
    def __init__(self, owner, directory):
        self.owner, self.directory = owner, directory
        self.language = get_language()
        self.included = 0
        self._queued = {}
        self._steer_lock = threading.Lock()

    def started(self, task_count):
        self.included = task_count
        self._queued.clear()

    def forward_supplements(self, agent):
        job = self.owner._read(self.directory)
        if job["status"] != "running":
            return
        with self._steer_lock:
            for index, task in enumerate(job["tasks"][self.included:], self.included + 1):
                if index not in self._queued:
                    ticket = f"teacher-supplement-{job['id']}-{index}"
                    instruction = tr(
                        "The learner provided a follow-up. Update your response using it.\n",
                        "学习者补充本角色任务，请按此更新结论：\n",
                        language=self.language,
                    )
                    if agent.steer(instruction + task, ticket=ticket):
                        self._queued[index] = ticket

    def settle_supplements(self, agent):
        with self._steer_lock:
            for index, ticket in self._queued.items():
                if not agent.withdraw_steer(ticket) and index == self.included + 1:
                    self.included = index

    def check(self):
        if self.owner._read(self.directory)["status"] == "cancelling":
            raise _Cancelled

    def preview(self, value):
        with self.owner._state(self.directory) as job:
            job["preview"] = value

    def endpoint(self, identity):
        with self.owner._state(self.directory) as job:
            original = job.get("endpoint")
            if original is not None and original != identity:
                raise ValueError(tr(
                    "The original role model endpoint changed. Start a new role task.",
                    "角色原模型端点已变化，请开始新的角色任务",
                ))
            job["endpoint"] = identity

    @contextmanager
    def watch(self, agent):
        done = threading.Event()

        def interrupt_if_requested():
            while not done.wait(0.25):
                try:
                    self.check()
                except _Cancelled:
                    agent.interrupt(hard_cancel=True)
                    return

        self.check()
        thread = threading.Thread(target=interrupt_if_requested, daemon=True)
        thread.start()
        try:
            yield
        finally:
            done.set()
            thread.join()


class Roles:
    def __init__(self, profile: Path, *, records=None, runner=None, settings=None):
        self.profile = Path(profile).resolve()
        self.directory = self.profile / "teacher" / "roles"
        if records is None:
            from .records import Records

            records = Records(self.profile)
        self.records = records
        self.runner = runner or HermesRoleRunner(self.profile)
        self.settings = settings or ModelSettings(self.profile)

    @staticmethod
    def _read(directory: Path) -> dict:
        return json.loads((directory / "task.json").read_text(encoding="utf-8"))

    @staticmethod
    def _write(directory: Path, job: dict) -> None:
        write_private(directory / "task.json", json.dumps(job, ensure_ascii=False, indent=2).encode())

    @staticmethod
    def _claim(directory: Path):
        handle = (directory / "worker.lock").open("ab")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        return handle

    @contextmanager
    def _state(self, directory: Path):
        with (directory / "state.lock").open("ab") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            job = self._read(directory)
            before = job.copy()
            yield job
            if job != before:
                self._write(directory, job)

    def _launch(self, directory: Path, handle):
        try:
            with use_profile(self.profile):
                context = copy_context()
            threading.Thread(
                target=context.run,
                args=(self._work, directory, handle),
                name=f"teacher-role-{directory.name[:8]}",
                daemon=True,
            ).start()
        except Exception:
            with self._state(directory) as job:
                self._stop(job, "failed", tr("Could not start the background role thread.", "无法启动后台角色线程"))
                handle.close()
            raise

    @staticmethod
    def _stop(job, status, error=None):
        job["status"], job["error"] = status, error

    def _finish_locked(self, directory: Path, job: dict, pending: dict) -> None:
        about = pending["result"]
        if job["role"] != "researcher":
            preview = about["preview"]
            prepared = Preview(
                Path(preview["source"]), Path(preview["image"]), preview["width"], preview["height"]
            )
            if not prepared.source.is_relative_to(directory) or not prepared.image.is_relative_to(directory):
                raise ValueError(tr(
                    "The diagram preview path does not belong to this role task.",
                    "图示预览路径与角色任务不一致",
                ))
            with self.records.lease(job["lesson_id"]):
                lesson = self.records.get(job["lesson_id"])
                if not lesson:
                    raise ValueError(tr("Could not find the lesson while publishing.", "发布时找不到课堂"))
                asset = publish(Path(lesson["vault"]), job["role"], job["id"], pending["revision"], prepared)
                event_id = f"teacher-role:{job['id']}:{pending['revision']}"
                self.records.append(job["lesson_id"], "assistant", asset["embed"], event_id, kind="image")
            about = {**about, "asset": asset}
        job.update(result=about, revision=pending["revision"], pending=None, context_result=None)
        if about.get("failure"):
            self._stop(job, "failed", about["failure"])
        else:
            self._stop(job, "completed")

    def _work(self, directory: Path, handle):
        control = _Control(self, directory)
        finished = None
        with handle:
            try:
                while True:
                    control.check()
                    with self._state(directory) as job:
                        if job["status"] == "cancelling":
                            raise _Cancelled
                        pending = job.get("pending")
                        if pending and pending["revision"] == len(job["tasks"]):
                            self._finish_locked(directory, job, pending)
                            if job.get("notify_token") and job["status"] == "completed":
                                finished = job.copy()
                            handle.close()
                            break
                        job["pending"] = None
                        if "session_id" not in job:
                            job["session_id"] = (
                                f"teacher-role-{job['id']}-{job['attempt']}"
                                if job["attempt"] else f"teacher-role-{job['id']}"
                            )
                        job["attempt"] += 1
                        run = job.copy()
                    control.started(len(run["tasks"]))
                    outcome = self.runner(run, directory, control)
                    if not isinstance(outcome, dict):
                        raise ValueError(tr("Invalid role response format.", "角色回复格式无效"))
                    control.check()
                    with self._state(directory) as job:
                        if job["status"] == "cancelling":
                            raise _Cancelled
                        if len(job["tasks"]) == control.included:
                            job["pending"] = {"revision": control.included, "result": outcome}
                        elif not outcome.get("failure"):
                            job["context_result"] = outcome
                    # The candidate is durable before publishing files or appending a lesson event.
            except _Cancelled:
                with self._state(directory) as job:
                    self._stop(job, "cancelled")
                    handle.close()
            except Exception as exc:
                with self._state(directory) as job:
                    if job["status"] == "cancelling":
                        self._stop(job, "cancelled")
                    else:
                        error = (
                            str(exc)
                            if isinstance(exc, ValueError)
                            else tr(f"Role failed ({type(exc).__name__}).", f"角色运行失败（{type(exc).__name__}）")
                        )
                        self._stop(job, "failed", error)
                    handle.close()
        if finished:
            from .role_notice import offer

            offer(self, directory, finished)

    @staticmethod
    def _task(task):
        if not isinstance(task, str) or not task.strip() or len(task) > 5000:
            raise ValueError(tr("Role tasks must contain 1–5,000 characters.", "角色任务须为 1–5000 个字符"))
        return task.strip()

    def _find(self, lesson_id: str, task_id: str | None) -> Path:
        if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise ValueError(tr("Lesson role task not found.", "课堂角色任务不存在"))
        directory = self.directory / task_id
        try:
            job = self._read(directory)
        except FileNotFoundError:
            raise ValueError(tr("Lesson role task not found.", "课堂角色任务不存在")) from None
        if job["lesson_id"] != lesson_id:
            raise ValueError(tr("Lesson role task not found.", "课堂角色任务不存在"))
        return directory

    def _recover_status(self, directory: Path, job: dict) -> None:
        if job["status"] in ACTIVE:
            handle = self._claim(directory)
            if handle is not None:
                handle.close()
                final = "cancelled" if job["status"] == "cancelling" else "interrupted"
                self._stop(job, final, tr(
                    "The role worker exited. You can explicitly resume the task.",
                    "运行进程已退出，可明确续接",
                ) if final == "interrupted" else None)

    @staticmethod
    def _public(job: dict) -> dict:
        return {
            key: job[key]
            for key in (
                "id",
                "session_id",
                "role",
                "status",
                "lesson_id",
                "revision",
                "result",
                "error",
                "preview",
                "route",
            )
            if key in job
        }

    def busy(self, lesson_id: str) -> bool:
        for directory in self.directory.glob("*/task.json"):
            if self._read(directory.parent)["lesson_id"] != lesson_id:
                continue
            with self._state(directory.parent) as job:
                self._recover_status(directory.parent, job)
                if job["status"] in ACTIVE:
                    return True
        return False

    def list_for_lesson(self, lesson_id: str) -> list[dict]:
        jobs = []
        for path in self.directory.glob("*/task.json"):
            if self._read(path.parent)["lesson_id"] != lesson_id:
                continue
            with self._state(path.parent) as job:
                self._recover_status(path.parent, job)
                jobs.append((path.stat().st_mtime_ns, self._public(job)))
        return [job for _, job in sorted(jobs, key=lambda row: row[0], reverse=True)]

    @staticmethod
    def unread(job):
        return (job.get("notify_token") and job["status"] == "completed"
                and job.get("read_revision", 0) < job["revision"])

    def read_notice(self, directory, session_id, *, revision=None, token=None):
        from .session_links import native_compression_path

        with self._state(directory) as job:
            if not self.unread(job):
                return None
            if revision is not None and (job["revision"] != revision or job["notify_token"] != token):
                return None
            origin = job["parent_session_id"]
            if origin != session_id and session_id not in (
                native_compression_path(self.profile, origin, forward=True) or []
            ):
                return None
            job["read_revision"] = job["revision"]
            return self._public(job)

    def take_notices(self, lesson_id, session_id, envelope=None):
        if envelope:
            task_id, revision, token = envelope
            directory = self._find(lesson_id, task_id)
            notice = self.read_notice(directory, session_id, revision=revision, token=token)
            return [notice] if notice else []
        result = []
        for path in self.directory.glob("*/task.json"):
            job = self._read(path.parent)
            if job["lesson_id"] == lesson_id and self.unread(job):
                notice = self.read_notice(path.parent, session_id)
                if notice:
                    result.append(notice)
        return result

    def dispatch(
        self, lesson_id: str, action: str, *, role=None, task=None, task_id=None, timeout=25,
        notify=False, read=False,
    ) -> dict:
        if action == "start":
            if role not in ROLES:
                raise ValueError(tr(
                    "Choose researcher, mermaid, or svg as the lesson role.",
                    "课堂角色须为 researcher、mermaid 或 svg",
                ))
            task = self._task(task)
            lesson = self.records.get(lesson_id)
            if not lesson:
                raise ValueError(tr("Lesson not found.", "课堂不存在"))
            route = self.settings.role(role)
            task_id = uuid.uuid4().hex
            directory = self.directory / task_id
            directory.mkdir(parents=True, mode=0o700)
            handle = self._claim(directory)
            job = {
                "id": task_id,
                "session_id": f"teacher-role-{task_id}",
                "lesson_id": lesson_id,
                "role": role,
                "route": route,
                "parent_session_id": lesson.get("session_id"),
                "topic": lesson["topic"],
                "tasks": [task],
                "status": "running",
                "attempt": 0,
                "revision": 0,
                "preview": None,
                "error": None,
                "result": None,
                "pending": None,
                "context_result": None,
            }
            if notify:
                job["notify_token"] = uuid.uuid4().hex
            try:
                self._write(directory, job)
            except Exception:
                handle.close()
                raise
            self._launch(directory, handle)
            return self._public(job)
        if action not in {"status", "wait", "supplement", "cancel", "resume"}:
            raise ValueError(tr("Unknown lesson role action.", "未知课堂角色操作"))
        directory = self._find(lesson_id, task_id)
        if action == "wait":
            if type(timeout) not in {int, float} or not 0 <= timeout <= 40:
                raise ValueError(tr("Wait time must be between 0 and 40 seconds.", "等待时长须为 0–40 秒"))
            deadline = time.monotonic() + timeout
            while True:
                result = self.dispatch(lesson_id, "status", task_id=task_id, read=read)
                remaining = deadline - time.monotonic()
                if result["status"] not in ACTIVE or remaining <= 0:
                    return result
                time.sleep(min(0.25, remaining))
        handle = None
        with self._state(directory) as job:
            self._recover_status(directory, job)
            if read and action == "status" and self.unread(job):
                job["read_revision"] = job["revision"]
            if action == "cancel" and job["status"] == "running":
                self._stop(job, "cancelling")
            if action == "supplement":
                if job["status"] == "cancelling":
                    raise ValueError(tr(
                        "The role is cancelling. You can add a follow-up after it stops.",
                        "角色正在取消，待取消后可补充任务",
                    ))
                if len(job["tasks"]) >= 12:
                    raise ValueError(tr(
                        "This role reached its follow-up limit. Start a new role task.",
                        "此角色已达到补充数上限，请开始新的角色任务",
                    ))
                job["tasks"] = [*job["tasks"], self._task(task)]
                if job["status"] == "running":
                    return self._public(job)
            if action == "supplement" or (action == "resume" and job["status"] in RECOVERABLE):
                handle = self._claim(directory)
                if handle is None:
                    raise ValueError(tr(
                        "The role is finishing its previous task. Try resuming shortly.",
                        "角色正在结束上一轮任务，请稍后续接",
                    ))
                self._stop(job, "running")
            result = self._public(job)
        if handle is not None:
            self._launch(directory, handle)
        return result
