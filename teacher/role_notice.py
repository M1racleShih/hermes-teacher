"""Return a teacher-delegated role result through the parent gateway."""

import asyncio
from contextlib import ExitStack
import re
import threading

from .i18n import use_profile
from .library import BusyError, file_lease


ENVELOPE = re.compile(r"〔课堂角色完成 ([0-9a-f]{32}) ([1-9]|1[0-2]) ([0-9a-f]{32})〕")


def read_envelope(text):
    match = ENVELOPE.fullmatch(text)
    if match:
        task_id, revision, token = match.groups()
        return task_id, int(revision), token
    return None


def recorded_replies(records, lesson):
    """Visible response belonging to each role admission on the current branch."""
    pending = {}
    replies = []
    for row in records.history(lesson, include_hidden=True):
        event = row["event_id"]
        if row["role"] == "system" and event.startswith("user:"):
            envelope = read_envelope(row["text"])
            if envelope:
                pending["reply:" + event.removeprefix("user:")] = envelope[:2]
        if row["role"] == "assistant" and event in pending:
            replies.append((pending.pop(event), row["text"]))
    return replies


async def watch_replies(tutor, lesson, seen, display):
    from hermes_cli.gateway_client import connect_gateway

    session = tutor.learning.state(lesson).get("canonical")
    if not session:
        return
    try:
        async with connect_gateway() as client:
            await client.rpc("session.resume", session_id=session, omit_messages=True)
            while True:
                for identity, reply in recorded_replies(tutor.records, lesson):
                    if identity not in seen:
                        await display(reply)
                        seen.add(identity)
                while True:
                    frame = await client.events.get()
                    if isinstance(frame, Exception):
                        return
                    params = frame.get("params", {})
                    if params.get("session_id") == session and params.get("type") in {
                        "message.complete", "session.replay_gap",
                    }:
                        break
    except (OSError, RuntimeError, ValueError):
        return


def offer(roles, directory, finished):
    """Offer a result when idle. The turn-end hook retries an offer deferred by a writer."""
    lesson, task, revision = finished["lesson_id"], finished["id"], finished["revision"]
    envelope = f"〔课堂角色完成 {task} {revision} {finished['notify_token']}〕"
    event = f"teacher-role-completion-{task}-{revision}"
    try:
        from agent.runtime_scope import bind_profile_scope

        from .runtime import Tutor

        with use_profile(roles.profile), bind_profile_scope(roles.profile), ExitStack() as delivery:
            tutor = Tutor(roles.profile, activate_profile=False)
            with roles.records.turn_lease(lesson):
                delivery.enter_context(file_lease(roles.directory, f"{task}/notice.lock"))
                job = roles._read(directory)
                if not roles.unread(job) or job["revision"] != revision or tutor.learning.pending(lesson):
                    return
                if not tutor.learning.state(lesson).get("canonical"):
                    return

            def admission(session, identity):
                with roles._state(directory) as current:
                    current["notice_receipt"] = {
                        "revision": revision, "session_id": session, "admission_id": identity,
                    }

            asyncio.run(tutor._submit(lesson, envelope, event, (), admission))
    except BusyError:
        return
    except (ImportError, OSError, ValueError, RuntimeError):
        # A retained unread result can accompany the next real learner turn.
        return


def resume_offers(roles, lesson):
    for path in roles.directory.glob("*/task.json"):
        job = roles._read(path.parent)
        if job["lesson_id"] == lesson and roles.unread(job):
            threading.Thread(target=offer, args=(roles, path.parent, job), daemon=True).start()
