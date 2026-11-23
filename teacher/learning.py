"""Durable teaching checkpoints and the learner-only text interaction channel."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import random
import re
import sqlite3
import time
import unicodedata
import uuid

from .i18n import get_language, localized, tr
from .library import safe_path, vault_directory


def _encode(value):
    return json.dumps(value, ensure_ascii=False)


def _required(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(tr(f"Missing {label}", f"缺少{label}"))
    return value.strip()


QUIZ_LETTERS = "ABCDEFGHIJKL"


def _labels(options):
    if not isinstance(options, list) or not 2 <= len(options) <= len(QUIZ_LETTERS):
        raise ValueError(tr("Provide 2 to 12 options", "请选择 2 至 12 个选项"))
    labels = [_required(option, tr("option", "选项")) for option in options]
    if len(set(labels)) != len(labels):
        raise ValueError(tr("Options must have distinct text", "选项需要有不同的文字"))
    return labels


_OPTION_REFERENCE = re.compile(
    r"选项\s*[A-L0-9](?![A-Z0-9])|"
    r"第\s*[一二三四五六七八九十两0-9]+\s*(?:个\s*选项|项)|"
    r"(?<![A-Za-z])[A-L]\s*(?:选项|项)|\boption\s+[a-l0-9]\b",
    re.IGNORECASE,
)
_TEXT_CONFIRMATION_EN = "\n\nReply “continue” once you've read the feedback."
_TEXT_CONFIRMATION_ZH = "\n\n读完请回复「继续」。"
_START = re.compile(
    r"^(?:(?:老师[，,：:\s]*)?我?(?:现在|今天)?(?:想|希望|要|请|打算)?(?:先|开始|继续)?"
    r"(?:学习|学一下|学一学|学会|了解|理解|讲解|讲讲|教我|测试|检查|复习|探测|练习)"
    r"|(?:我)?(?:想|希望|要|打算)学"
    r"|(?:请)?(?:先)?(?:用\s*teacher_quiz\s*)?出(?:一|几)?道[^。！？\n]*探测题"
    r"|(?:today\s+)?(?:I (?:want|would like) to|I['’]d like to) (?:learn|study|understand)\b"
    r"|(?:please )?(?:teach me|let['’]s learn|(?:start|continue) learning|learn|study|review)\b)",
    re.IGNORECASE,
)
_START_NOW = re.compile(
    r"^(?:(?:(?:请|现在|可以|我们)\s*)*(?:开课|开始(?:上课|学习|讲课)?)(?:吧|了)?"
    r"|(?:please )?(?:start(?: (?:the )?lesson)?|begin(?: (?:the )?lesson)?|let['’]s (?:begin|start)))[。！!.]*$",
    re.IGNORECASE,
)
_REASONING_SETUP = re.compile(r"(?:思考|推理)(?:强度|级别)|(?:/reasoning|reasoning effort|thinking level)\b", re.I)
_MODEL_SETUP = re.compile(
    r"(?:切换|选择|设置|更换|换|调整)(?:课堂)?模型|模型.{0,8}(?:切换|选择|设置|换|选)"
    r"|(?:/model|switch models?|select a model)\b",
    re.I,
)
_SETUP_QUESTION = re.compile(
    r"如何|怎么|怎样|哪里|能否|能不能|是否|可以先|开课前|开始前|[吗？?]"
    r"|^(?:how|can I|could I|where)\b|\bbefore (?:starting|the lesson)\b", re.I,
)
_PROBE_GAPS = {"wrong", "unknown"}
_CUSTOM_ANSWER = re.compile(
    r"(?:^|(?<=[\s，、,]))(?:自填|其他|custom|other)(?:[ \t：:]+(.*)|$)$", re.DOTALL | re.IGNORECASE,
)
_HANDOFF_FIELDS = (
    "goal",
    "evidence",
    "plan",
    "plan_edges",
    "probe_strands",
    "node",
    "revision",
    "research",
    "model",
    "tool_allowlist",
    "preferences",
    "stage",
    "change",
)


def _probe_strands(strands):
    if not isinstance(strands, list) or not 1 <= len(strands) <= 12:
        raise ValueError(tr("Provide 1–12 names for the lesson's prerequisite strands", "请用 1–12 个名称列出本课依赖的前置主线"))
    labels = [_required(strand, tr("prerequisite strand name", "前置主线名称")) for strand in strands]
    if len(set(labels)) != len(labels):
        raise ValueError(tr("Prerequisite strand names must be unique", "前置主线名称不能重复"))
    return labels


def _setup_help(text):
    if not _SETUP_QUESTION.search(text):
        return ""
    lines = []
    if _MODEL_SETUP.search(text):
        lines.append(tr("Use /model in the input box to choose the classroom model.", "在输入框使用 /model 选择课堂模型。"))
    if _REASONING_SETUP.search(text):
        lines.append(tr(
            "Use /reasoning in the input box to adjust reasoning effort, for example /reasoning low.",
            "在输入框使用 /reasoning 调整思考强度，例如 /reasoning low。",
        ))
    if lines:
        lines.append(tr(
            "When ready, tell me your learning goal or reply “Start lesson”.", "准备好后，请告诉我学习目标或回复「开始吧」。",
        ))
    return " ".join(lines)


def _requested_goal(text, topic):
    if _START_NOW.fullmatch(text.strip()):
        return topic or text
    for sentence in re.split(r"[。！？!?\n]+", text.strip()):
        sentence = sentence.strip()
        if _setup_help(sentence + text.strip()[-1:]):
            continue
        if _START.match(sentence):
            return text
    return ""


def _plan_edges(nodes, edges):
    if len(set(nodes)) != len(nodes):
        raise ValueError(tr("Plan node names must be unique", "计划节点名称不能重复"))
    if not isinstance(edges, list) or len(edges) > 100:
        raise ValueError(tr("Provide the plan's direct dependency edges", "请列出计划中直接依赖的边"))
    position = {node: index for index, node in enumerate(nodes)}
    retained = set()
    origins = set()
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) != {"prerequisite", "dependent"}:
            raise ValueError(tr("Each edge needs a prerequisite and a dependent node", "每条边需要指定前置节点 prerequisite 和后继节点 dependent"))
        source = _required(edge["prerequisite"], tr("prerequisite node", "前置节点"))
        target = _required(edge["dependent"], tr("dependent node", "后继节点"))
        if source not in position or target not in position:
            raise ValueError(tr("Dependency edges must reference nodes in the plan", "依赖边只能引用计划中已有的节点"))
        if position[source] >= position[target]:
            raise ValueError(tr("Prerequisites must come before their dependents in an acyclic graph", "前置节点必须排在后继节点之前，依赖图不能成环"))
        if (source, target) in retained:
            raise ValueError(tr("Dependency edges must be unique", "计划依赖边不能重复"))
        retained.add((source, target))
        origins.add(source)
    if len(origins) != len(nodes) - 1:
        raise ValueError(tr("Each prerequisite node needs a path to the final learning goal", "每个前置节点都需要有一条路径通向最后的学习目标"))
    return [dict(prerequisite=source, dependent=target) for source, target in sorted(
        retained, key=lambda edge: (position[edge[1]], position[edge[0]])
    )]


class Learning:
    """Only receive() interprets an answer. No model-facing tool exposes it."""

    def __init__(self, profile, *, clock=time.time, shuffle=None, incoming_handoff=None):
        self.profile = Path(profile)
        self.directory = vault_directory(self.profile / "teacher", create=True)
        self.file = safe_path(self.directory, "learning.sqlite3")
        self.clock = clock
        self.shuffle = shuffle or random.SystemRandom().shuffle
        self.incoming_handoff = incoming_handoff
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS courses(id TEXT PRIMARY KEY, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS questions(
                    id TEXT PRIMARY KEY, course TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL, deadline REAL NOT NULL, feedback TEXT NOT NULL DEFAULT '');
                CREATE UNIQUE INDEX IF NOT EXISTS waiting ON questions(course)
                    WHERE status IN ('open','feedback');
                CREATE TABLE IF NOT EXISTS receipts(course TEXT, event TEXT, reply TEXT,
                    PRIMARY KEY(course,event));
                CREATE TABLE IF NOT EXISTS request_locales(course TEXT, event TEXT, language TEXT NOT NULL,
                    PRIMARY KEY(course,event));
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(safe_path(self.directory, self.file.name), timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @staticmethod
    def _defaults():
        return dict(
            stage="ready",
            goal="",
            evidence=[],
            plan=[],
            plan_edges=[],
            probe_strands=[],
            revision=0,
            node=0,
            research="",
            model=None,
            change="",
            notice="",
            preferences=[],
        )

    @staticmethod
    def _handoff_fields(state):
        progress = {key: state[key] for key in _HANDOFF_FIELDS if key in state}
        if progress.get("stage") == "plan":
            progress["stage"] = "revise"
        return progress

    def _state(self, db, lesson):
        row = db.execute("SELECT state FROM courses WHERE id=?", (lesson,)).fetchone()
        if row:
            return json.loads(row[0])
        state = self._defaults()
        handoff = self.incoming_handoff(lesson) if self.incoming_handoff is not None else None
        if handoff is not None:
            progress = handoff["progress"]
            if progress is None:
                progress = self._state(db, handoff["source_id"])
            state.update(self._handoff_fields(progress))
            db.execute("INSERT INTO courses VALUES (?,?)", (lesson, _encode(state)))
        return state

    @staticmethod
    def _save(db, lesson, state):
        db.execute("INSERT OR REPLACE INTO courses VALUES (?,?)", (lesson, _encode(state)))

    def state(self, lesson):
        with self._db() as db:
            return self._state(db, lesson)

    def handoff_progress(self, lesson):
        return self._handoff_fields(self.state(lesson))

    def seed_handoff(self, lesson):
        """Persist the captured start only for a target without a learning row."""
        with self._db() as db:
            self._state(db, lesson)

    def update(self, lesson, **changes):
        with self._db() as db:
            state = self._state(db, lesson)
            state.update(changes)
            self._save(db, lesson, state)
            return state

    def take_notice(self, lesson):
        with self._db() as db:
            state = self._state(db, lesson)
            notice = state.get("notice", "")
            if notice:
                state["notice"] = ""
                self._save(db, lesson, state)
            return notice

    @localized
    def request_language(self, lesson, event_id):
        """Keep generated prompt fragments stable for this Hermes input ID."""
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO request_locales VALUES (?, ?, ?)", (lesson, event_id, get_language()),
            )
            return db.execute(
                "SELECT language FROM request_locales WHERE course=? AND event=?", (lesson, event_id),
            ).fetchone()[0]

    @staticmethod
    def _active(db, lesson):
        return db.execute(
            "SELECT * FROM questions WHERE course=? AND status IN ('open','feedback')", (lesson,)
        ).fetchone()

    @staticmethod
    def _public(row):
        if not row:
            return None
        item = json.loads(row["payload"])
        lines = [item.get("intro", ""), item["prompt"]]
        options = item.get("options", [])
        if item["kind"] == "quiz":
            lines.extend(f"{letter}. {o['label']}" for letter, o in zip(QUIZ_LETTERS, options))
            selection = (
                tr("option letters, such as AB", "多个选项字母，如 AB") if item["mode"] == "multi"
                else tr("one option letter", "一个选项字母")
            )
            lines.append(tr(
                f"Reply with {selection}, or enter “I don't know”, “hint”, or “cancel”. "
                "Add any reasoning on a new line.",
                f"回复{selection}，也可回复「不知道」「提示」或「取消」。另起一行可填写推理备注。",
            ))
        elif item["kind"] == "ask":
            lines.extend(f"{n}. {o['label']}" for n, o in enumerate(options, 1))
            lines.append(
                {
                    "single": tr("Reply with a number or “custom your answer”. ", "回复编号或「自填 内容」。"),
                    "multi": tr(
                        "Reply with option numbers. To add your own response, use “1 2 custom your answer”. ",
                        "回复多个编号，可加「自填 内容」，例如「1 2 自填 补充内容」。",
                    ),
                    "text": tr("Reply in your own words. You can use multiple lines. ", "请直接回复，可写多行。"),
                }[item["mode"]]
                + tr("Reply “cancel” to exit.", "回复「取消」可退出。")
            )
        elif item["kind"] == "plan":
            lines.append(tr(
                "Reply “approve” to begin this version, send your revisions, or reply “cancel”.",
                "回复「批准」开始这一版，也可直接写修改意见或回复「取消」。",
            ))
        text = (
            row["feedback"]
            if row["status"] == "feedback"
            else "\n\n".join(
                block for block in (lines[0], lines[1], "\n".join(lines[2:-1]), lines[-1]) if block
            )
        )
        return {"id": row["id"], "kind": item["kind"], "status": row["status"], "text": text}

    @localized
    def pending(self, lesson):
        with self._db() as db:
            return self._public(self._active(db, lesson))

    def quiz_view(self, lesson):
        """Public fields for an interactive client, including restored text-only quizzes."""
        view = self.interaction_view(lesson)
        if view and view.pop("kind") == "quiz":
            return view
        return None

    def interaction_view(self, lesson):
        """Questions and preferences available to the learner in the current classroom."""
        with self._db() as db:
            row = self._active(db, lesson)
            if not row:
                return None
            item = json.loads(row["payload"])
            if item["kind"] not in {"quiz", "ask"}:
                return None
            view = {
                "id": row["id"],
                "kind": item["kind"],
                "status": row["status"],
                "mode": item["mode"],
                "intro": item.get("intro", ""),
                "prompt": item["prompt"],
                "options": [{"id": o["id"], "label": o["label"]} for o in item["options"]],
            }
            if item.get("hinted"):
                view["hint"] = item["hint"]
            if row["status"] == "feedback":
                view["feedback"] = row["feedback"].removesuffix(_TEXT_CONFIRMATION_EN).removesuffix(_TEXT_CONFIRMATION_ZH)
            return view

    def _open(self, db, lesson, item, ttl):
        if self._active(db, lesson):
            raise ValueError(tr("Complete the current interaction first", "请先完成当前交互"))
        if ttl <= 0:
            raise ValueError(tr("The interaction duration must be positive", "有效时长必须为正数"))
        qid = uuid.uuid4().hex
        db.execute(
            "INSERT INTO questions(id,course,payload,status,deadline) VALUES(?,?,?,'open',?)",
            (qid, lesson, _encode(item), self.clock() + ttl),
        )
        return self._public(self._active(db, lesson))

    @staticmethod
    def quiz_scope(state, objective, strand=None, strands=None, follows=None):
        if state["stage"] not in {"probe", "teach", "review"}:
            raise ValueError(tr("Set your learning goal and finish any pending plan approval first", "请先提交学习目标并完成当前计划确认"))
        if state["stage"] != "probe":
            return None, [], None
        strand = _required(strand if strand is not None else objective, tr("prerequisite strand", "前置主线"))
        registered = state.get("probe_strands", [])
        scope = list(dict.fromkeys([*registered, *(_probe_strands(strands) if strands is not None else []), strand]))
        _probe_strands(scope)
        if follows is not None and not any(
            event.get("id") == follows
            and event.get("phase") == "probe"
            and event.get("strand") == strand
            and event["outcome"] in _PROBE_GAPS
            and event["objective"] != objective
            for event in state["evidence"]
        ):
            raise ValueError(tr(
                "A follow-up must reference a missed or unknown question in the same strand "
                "and check a different objective",
                "相邻复测须引用同一主线中答错或不知道的题目 ID，并使用不同的检查目标",
            ))
        return strand, scope, follows

    @localized
    def quiz(
        self, lesson, objective, mode, private, *, intro="", shuffle=True, strand=None,
        strands=None, follows=None, ttl=86400,
    ):
        objective = _required(objective, tr("objective", "检查目标"))
        if mode not in {"single", "multi"} or not isinstance(private, dict):
            raise ValueError(tr("Invalid question mode or draft", "题型或出题结果无效"))
        if type(shuffle) is not bool:
            raise ValueError(tr("shuffle must be a Boolean", "shuffle 须为布尔值"))
        answers = private.get("options")
        if not isinstance(answers, list) or any(
            not isinstance(a, dict) or type(a.get("correct")) is not bool for a in answers
        ):
            raise ValueError(tr("Each option needs its own correctness flag", "每个选项都需要独立的评分标记"))
        labels = _labels([a.get("label") for a in answers])
        count = sum(a["correct"] for a in answers)
        if not count or mode == "single" and count != 1:
            raise ValueError(tr("The draft has an invalid answer key. Generate a new question", "出题侧返回了无效评分键，请重新出题"))
        prompt = _required(private.get("prompt"), tr("question prompt", "题干"))
        explanation = _required(private.get("explanation"), tr("explanation", "解析"))
        hint = _required(private.get("hint"), tr("hint", "提示"))
        if any(
            _OPTION_REFERENCE.search(value.translate(str.maketrans("", "", "$*`")))
            for value in [prompt, explanation, hint, *labels]
        ):
            raise ValueError(tr(
                "Options can be shuffled. Refer to options by their text in the prompt, hint, and explanation",
                "测验支持选项重排，请用选项正文说明题干、提示和解析",
            ))
        options = [dict(id=uuid.uuid4().hex, label=s) for s in labels]
        correct = [option["id"] for option, answer in zip(options, answers) if answer["correct"]]
        if shuffle:
            self.shuffle(options)
        item = dict(
            kind="quiz",
            mode=mode,
            prompt=prompt,
            options=options,
            correct=correct,
            explanation=explanation,
            hint=hint,
            hinted=False,
            objective=objective,
            intro=intro,
        )
        with self._db() as db:
            state = self._state(db, lesson)
            strand, scope, follows = self.quiz_scope(state, objective, strand, strands, follows)
            if strand is not None:
                item.update(strand=strand, follows=follows)
            item["phase"], item["node"] = state["stage"], state["node"]
            card = self._open(db, lesson, item, ttl)
            if strand is not None and scope != state.get("probe_strands"):
                state["probe_strands"] = scope
                self._save(db, lesson, state)
            return card

    @staticmethod
    def probe_progress(state):
        strands = {strand: [] for strand in state.get("probe_strands", [])}
        for event in state["evidence"]:
            strand = event.get("strand")
            if strand and event.get("phase") == "probe":
                strands.setdefault(strand, []).append(event)
        progress = {}
        for strand, events in strands.items():
            floor = [event for event in events if event["outcome"] == "independent"]
            gaps = [event for event in events if event["outcome"] in _PROBE_GAPS]
            follows = {event.get("follows") for event in events}
            adjacent = [gap for gap in gaps if gap["id"] in follows]
            gap = (adjacent or gaps)[-1] if gaps else None
            if floor and adjacent:
                direction = tr(
                    "The strand has independent success, a gap, and a follow-up check.",
                    "边界已取得独立答对、困难与相邻复测证据。",
                )
            elif gap and not adjacent:
                direction = tr(
                    "Use a different follow-up question to distinguish a misconception from a calculation slip. "
                    "Set follows to the gap ID.",
                    "换一道相邻题，填 follows 引用缺口 ID，区分误解与计算疏漏。",
                )
            elif not floor:
                direction = tr(
                    "Lower the difficulty to find a prerequisite the learner can answer independently.",
                    "降低难度，找出这一主线上能独立答对的前置内容。",
                )
            else:
                direction = tr(
                    "Raise the difficulty with a transfer or derivation question to find the upper bound.",
                    "提高难度，用需要迁移或推导的题寻找上界。",
                )
            progress[strand] = {
                "floor": floor[-1]["objective"] if floor else None,
                "gap": {"id": gap["id"], "objective": gap["objective"], "outcome": gap["outcome"]} if gap else None,
                "followed_up": bool(adjacent),
                "direction": direction,
            }
        return progress

    @localized
    def ask(self, lesson, prompt, mode, options=None, *, purpose="preference", ttl=86400):
        if mode not in {"single", "multi", "text"}:
            raise ValueError(tr("Unknown question mode", "未知问询类型"))
        if purpose not in {"preference", "goal"} or purpose == "goal" and mode != "text":
            raise ValueError(tr("Use a free-text question to confirm the learning goal", "学习目标用自由文字确认"))
        labels = [] if mode == "text" else _labels(options)
        item = dict(
            kind="ask",
            prompt=_required(prompt, tr("question", "问题")),
            mode=mode,
            purpose=purpose,
            options=[dict(id=str(i), label=x) for i, x in enumerate(labels)],
        )
        with self._db() as db:
            if purpose == "goal" and self._state(db, lesson)["stage"] != "ready":
                raise ValueError(tr("This lesson already has a learning goal", "本课已收到学习目标"))
            return self._open(db, lesson, item, ttl)

    @localized
    def plan(self, lesson, nodes, rationale, research, *, edges=None, ttl=86400):
        if not isinstance(nodes, list) or not 1 <= len(nodes) <= 30:
            raise ValueError(tr("The plan needs an ordered list of learning nodes", "计划需要有序的知识节点"))
        nodes = [_required(n, tr("node", "节点")) for n in nodes]
        edges = _plan_edges(nodes, [] if edges is None else edges)
        reason = _required(rationale, tr("rationale for scope and sequence", "范围与顺序理由"))
        source = _required(research, tr("this lesson's verified research findings", "本课研究角色的核查结果"))
        with self._db() as db:
            state = self._state(db, lesson)
            if state["stage"] not in {"probe", "revise"}:
                raise ValueError(tr("Confirm the learning goal and complete the prerequisite assessment first", "请先明确学习目标并完成探测"))
            progress = self.probe_progress(state)
            # Older courses can still revise their already submitted plan.
            legacy_revision = state["revision"] > 0 and not state.get("probe_strands")
            missing = [name for name, row in progress.items() if not (row["floor"] and row["followed_up"])]
            if not legacy_revision and (not progress or missing):
                message = tr(
                    "Complete the prerequisite assessment with independent success, a gap, "
                    "and a follow-up check for every strand",
                    "请完成前置主线探测的独立答对、困难与相邻复测证据",
                )
                if missing:
                    message += tr(": ", "：") + tr(", ", "、").join(missing)
                raise ValueError(message)
            graph = ["flowchart TD"]
            indices = {node: i for i, node in enumerate(nodes)}
            for i, n in enumerate(nodes):
                label = n.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
                label = " ".join(label.splitlines()).replace("`", "&#96;")
                graph.append(f'  N{i}["{label}"]')
            for edge in edges:
                graph.append(f"  N{indices[edge['prerequisite']]} --> N{indices[edge['dependent']]}")
            version = state["revision"] + 1
            text = (
                tr(f"## Teaching plan · Version {version}\n\n{reason}\n\n", f"## 教学计划 · 第 {version} 版\n\n{reason}\n\n")
                + "\n".join(f"{i}. {n}" for i, n in enumerate(nodes, 1))
                + "\n\n```mermaid\n"
                + "\n".join(graph)
                + tr("\n```\n\nResearch supporting the plan\n\n", "\n```\n\n规划依据\n\n")
                + source
            )
            card = self._open(db, lesson, dict(kind="plan", prompt=text), ttl)
            state.update(stage="plan", plan=nodes, plan_edges=edges, revision=version, research=source, node=0)
            self._save(db, lesson, state)
            return card

    @staticmethod
    def _selection(raw, item):
        quiz = item["kind"] == "quiz"
        raw = unicodedata.normalize("NFKC", raw)
        fields = re.split(r"[\s,，、]+", raw.strip())
        if quiz and all(re.fullmatch(r"[A-Za-z]+", part) for part in fields):
            indices = [ord(ch) - ord("A") + 1 for ch in "".join(fields).upper()]
        elif all(re.fullmatch(r"[0-9]{1,2}", part) for part in fields):
            indices = [int(n) for n in fields]
        else:
            raise ValueError(
                tr("Reply with the option letters shown", "请按当前显示的选项字母作答") if quiz
                else tr("Reply with the option numbers shown", "请按当前显示的编号作答")
            )
        if len(set(indices)) != len(indices) or (item["mode"] == "single" and len(indices) != 1):
            raise ValueError(tr("Choose valid options for this question without duplicates", "请按题型选择有效且不重复的编号"))
        if any(i < 1 or i > len(item["options"]) for i in indices):
            raise ValueError(tr("Your selection is outside the displayed options", "编号超出当前题目范围"))
        return [item["options"][i - 1] for i in indices]

    def _answer(self, db, row, text, state):
        item, status, qid = json.loads(row["payload"]), row["status"], row["id"]
        stripped = text.strip()
        command = stripped.casefold().replace("’", "'")
        first_command = command.partition("\n")[0].strip().rstrip("。！!.")
        command = command.rstrip("。！!.")

        def finish(value):
            db.execute("UPDATE questions SET status=? WHERE id=?", (value, qid))

        if status == "feedback":
            if first_command not in {"继续", "确认", "已读", "continue", "confirm", "read"}:
                return row["feedback"]
            finish("confirmed")
            if item["phase"] == "teach" and item["outcome"] == "independent":
                state["node"] += 1
                if state["node"] >= len(state["plan"]):
                    state["stage"] = "review"
            return None
        if self.clock() >= row["deadline"]:
            finish("expired")
            if item["kind"] == "plan":
                state["stage"] = "probe"
            return tr("This interaction expired. Ask for a new question or plan.", "这次交互已超时，请重新出题或提交计划。")
        if command in {"取消", "cancel", "/cancel"}:
            finish("cancelled")
            if item["kind"] == "plan":
                state["stage"] = "probe"
            return tr("Interaction cancelled.", "已取消本次交互。")
        if item["kind"] == "plan":
            if command in {"批准", "同意", "确认", "按这个计划开始", "approve", "approved", "confirm", "accept"}:
                state["stage"], state["change"] = "teach", ""
                finish("approved")
            else:
                state["stage"], state["change"] = "revise", text
                finish("revision_requested")
            return None
        if item["kind"] == "ask":
            custom = None
            if item["mode"] == "text":
                value = text
            else:
                entered = _CUSTOM_ANSWER.search(stripped)
                numbered = stripped if entered is None else stripped[:entered.start()].rstrip(" \t\n，、,")
                if entered:
                    custom = _required(entered.group(1), tr("custom answer", "自填内容"))
                if numbered:
                    value = [o["label"] for o in self._selection(numbered, item)]
                    if custom:
                        if item["mode"] == "single":
                            raise ValueError(tr("Choose one option or enter one custom answer", "单选只接受一个已有选项或一段自填内容"))
                        value.append(custom)
                else:
                    value = custom
            if item.get("purpose") == "goal":
                state.update(stage="probe", goal=value)
            else:
                preference = dict(question=item["prompt"], answer=value)
                if custom and isinstance(value, list):
                    preference["custom"] = custom
                state["preferences"].append(preference)
            finish("submitted")
            return None
        if command in {"提示", "hint", "/hint"}:
            item["hinted"] = True
            db.execute("UPDATE questions SET payload=? WHERE id=?", (_encode(item), qid))
            return tr("Hint\n\n", "提示\n\n") + item["hint"]
        first, _, note = text.strip("\r\n").partition("\n")
        unknown = first_command in {
            "不知道", "不确定", "i don't know", "don't know", "not sure", "i'm not sure", "unknown",
        }
        chosen = [] if unknown else self._selection(first, item)
        correct = set(item["correct"])
        passed = set(o["id"] for o in chosen) == correct
        outcome = (
            "unknown" if unknown else ("hinted" if item["hinted"] else "independent") if passed else "wrong"
        )
        names = {
            "independent": tr("Correct without a hint.", "独立答对。"),
            "hinted": tr("Correct with a hint.", "提示后答对。"),
            "wrong": tr("Incorrect.", "答错。"),
            "unknown": tr("I don't know.", "不知道。"),
        }
        feedback = (
            names[outcome] + tr("\n\nCorrect options\n", "\n\n正确选项\n")
            + "\n".join(f"{letter}. {o['label']}" for letter, o in zip(QUIZ_LETTERS, item["options"]) if o["id"] in correct)
            + "\n\n"
            + item["explanation"]
        )
        if note:
            feedback += tr("\n\nYour notes\n", "\n\n你的备注\n") + note
        feedback += tr(_TEXT_CONFIRMATION_EN, _TEXT_CONFIRMATION_ZH)
        state["evidence"].append(
            dict(
                id=qid,
                question=item["prompt"],
                objective=item["objective"],
                phase=item["phase"],
                node=item["node"],
                strand=item.get("strand"),
                follows=item.get("follows"),
                outcome=outcome,
                note=note,
                answer=[o["label"] for o in chosen],
            )
        )
        item["outcome"] = outcome
        db.execute(
            "UPDATE questions SET status='feedback',feedback=?,payload=? WHERE id=?",
            (feedback, _encode(item), qid),
        )
        return feedback

    @localized
    def receive(self, lesson, text, event_id, *, expected_id=None, topic=""):
        """None permits inference. Any string is a complete, zero-inference turn."""
        _required(text, tr("learner input", "学习者输入"))
        _required(event_id, tr("input event ID", "输入事件身份"))
        with self._db() as db:
            prior = db.execute(
                "SELECT reply FROM receipts WHERE course=? AND event=?", (lesson, event_id)
            ).fetchone()
            if prior:
                return prior[0]
            state, row = self._state(db, lesson), self._active(db, lesson)
            if expected_id and (not row or expected_id != row["id"]):
                reply = tr("This question has ended or belongs to another classroom.", "这次作答关联的题目已结束或属于其他课堂。")
            elif row:
                try:
                    reply = self._answer(db, row, text, state)
                except ValueError as exc:
                    reply = str(exc)
            elif state["stage"] == "ready":
                goal = _requested_goal(text, topic)
                if goal:
                    state.update(stage="probe", goal=goal)
                reply = None if goal else _setup_help(text) or None
            else:
                reply = None
            self._save(db, lesson, state)
            db.execute("INSERT INTO receipts VALUES (?,?,?)", (lesson, event_id, reply))
            return reply
