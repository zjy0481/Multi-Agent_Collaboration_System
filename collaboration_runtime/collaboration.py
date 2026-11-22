"""Persistent chat, clarification, wake-up deliveries and initial goal registration."""

from datetime import datetime, timezone
import hashlib
import hmac
import json
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .local_auth import OPERATOR_ID, Principal
from .teams import TeamConflict, TeamNotFound


RequestId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
ObjectId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12_000)]


class CollaborationForbidden(Exception):
    pass


class CollaborationInvalid(Exception):
    pass


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageInput(Command):
    request_id: RequestId
    text: Text
    recipient_id: ObjectId | None = None
    mentions: list[ObjectId] = Field(default_factory=list, max_length=100)
    reply_to: ObjectId | None = None
    kind: Literal["chat", "progress", "assignment", "supplement"] = "chat"


class GoalInput(Command):
    request_id: RequestId
    source_message_id: ObjectId


class ReadMessageInput(Command):
    message_id: ObjectId


class QuestionInput(Command):
    request_id: RequestId
    source_message_id: ObjectId
    text: Text
    private: bool = False


READABLE_MESSAGE = "(m.recipient_id IS NULL OR m.sender_id = :actor OR m.recipient_id = :actor)"
QUESTION_FIELDS = "q.question_id, q.asker_id, q.source_message_id, q.answer_message_id"
CURSOR_WINDOW = 512
EVENT_BATCH_SIZE = 100


def _now():
    return datetime.now(timezone.utc).isoformat()


def _message(row):
    fields = dict(row)
    fields["mentions"] = json.loads(fields["mentions"])
    return fields


class CollaborationService:
    def __init__(self, db, teams, auth):
        self.db = db
        self.teams = teams
        self.auth = auth
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS collaboration_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                type TEXT NOT NULL,
                object_id TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS collaboration_events_team ON collaboration_events(team_id, seq);
            CREATE TABLE IF NOT EXISTS team_messages (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                seq INTEGER NOT NULL UNIQUE REFERENCES collaboration_events(seq),
                sender_id TEXT NOT NULL,
                recipient_id TEXT,
                text TEXT NOT NULL,
                kind TEXT NOT NULL,
                mentions TEXT NOT NULL,
                reply_to TEXT REFERENCES team_messages(id),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS team_messages_order ON team_messages(team_id, seq);
            CREATE TABLE IF NOT EXISTS team_goals (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                source_message_id TEXT NOT NULL UNIQUE REFERENCES team_messages(id),
                status TEXT NOT NULL CHECK (status IN ('active', 'queued', 'completed', 'cancelled', 'failed', 'timed_out')),
                revision INTEGER NOT NULL,
                seq INTEGER NOT NULL UNIQUE REFERENCES collaboration_events(seq),
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_goal ON team_goals(team_id) WHERE status = 'active';
            CREATE INDEX IF NOT EXISTS team_goals_order ON team_goals(team_id, seq);
            CREATE TABLE IF NOT EXISTS message_deliveries (
                event_id INTEGER NOT NULL REFERENCES collaboration_events(seq),
                message_id TEXT NOT NULL REFERENCES team_messages(id),
                session_id TEXT NOT NULL REFERENCES members(session_id),
                purpose TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY (event_id, session_id, purpose)
            );
            CREATE INDEX IF NOT EXISTS message_deliveries_inbox ON message_deliveries(session_id, status, event_id);
            CREATE TABLE IF NOT EXISTS human_questions (
                question_id TEXT PRIMARY KEY REFERENCES team_messages(id),
                asker_id TEXT NOT NULL,
                source_message_id TEXT NOT NULL REFERENCES team_messages(id),
                work_event_id INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                work_purpose TEXT NOT NULL,
                answer_message_id TEXT UNIQUE REFERENCES team_messages(id),
                FOREIGN KEY (work_event_id, session_id, work_purpose)
                    REFERENCES message_deliveries(event_id, session_id, purpose)
            );
            CREATE INDEX IF NOT EXISTS human_questions_work
                ON human_questions(work_event_id, session_id, work_purpose, answer_message_id);
            CREATE TABLE IF NOT EXISTS clarification_corrections (
                message_id TEXT PRIMARY KEY REFERENCES team_messages(id),
                question_id TEXT NOT NULL REFERENCES human_questions(question_id)
            );
            CREATE TABLE IF NOT EXISTS collaboration_requests (
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                actor_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                request_id TEXT NOT NULL,
                digest TEXT NOT NULL,
                receipt TEXT NOT NULL,
                PRIMARY KEY (team_id, actor_id, operation, request_id)
            );
        """)

    def team(self, team_id: str, actor: Principal):
        if not actor.is_operator and actor.team_id != team_id:
            raise TeamNotFound("Team not found")
        return self.teams.get(team_id)

    def list_teams(self, actor: Principal):
        if not actor.is_operator:
            team = self.team(actor.team_id, actor)
            return [{"team_id": team["team_id"], "name": team["name"]}]
        return [
            {"team_id": row["team_id"], "name": json.loads(row["details"])["name"]}
            for row in self.db.execute("SELECT team_id, details FROM teams ORDER BY rowid")
        ]

    @staticmethod
    def participants(team):
        return [{"id": OPERATOR_ID, "name": "用户"}] + [
            {"id": member["member_id"], "name": member["name"]} for member in team["members"]
        ]

    def _event(self, team_id, event_type, object_id):
        return self.db.execute(
            "INSERT INTO collaboration_events(team_id, type, object_id) VALUES (?, ?, ?)",
            (team_id, event_type, object_id),
        ).lastrowid

    def _message_for(self, team_id, actor, message_id):
        row = self.db.execute(f"""
            SELECT m.* FROM team_messages m
            WHERE m.team_id = :team AND m.id = :message AND {READABLE_MESSAGE}
        """, {"team": team_id, "message": message_id, "actor": actor.id}).fetchone()
        if row is None:
            raise TeamNotFound("Message not found")
        return _message(row)

    def get_message(self, team_id, actor, message_id):
        self.team(team_id, actor)
        return self._message_for(team_id, actor, message_id)

    def _replay(self, key, digest):
        row = self.db.execute("""
            SELECT digest, receipt FROM collaboration_requests
            WHERE team_id = ? AND actor_id = ? AND operation = ? AND request_id = ?
        """, key).fetchone()
        if row is None:
            return None
        if row["digest"] != digest:
            raise TeamConflict("Request ID was already used with different parameters")
        return json.loads(row["receipt"])

    def _save_receipt(self, key, digest, receipt):
        self.db.execute("INSERT INTO collaboration_requests VALUES (?, ?, ?, ?, ?, ?)",
                        (*key, digest, json.dumps(receipt)))

    @staticmethod
    def _digest(payload):
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def _create_message(self, team_id, actor, *, recipient, text, kind, mentions, reply_to):
        message_id = uuid4().hex
        seq = self._event(team_id, "message.created", message_id)
        self.db.execute("""
            INSERT INTO team_messages(id, team_id, seq, sender_id, recipient_id, text, kind, mentions, reply_to, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (message_id, team_id, seq, actor.id, recipient, text, kind, json.dumps(mentions), reply_to, _now()))
        return {"message_id": message_id, "seq": seq, "event_id": seq}

    def _deliver(self, message, sessions, purpose):
        self.db.executemany("""
            INSERT INTO message_deliveries(event_id, message_id, session_id, purpose) VALUES (?, ?, ?, ?)
        """, [(message["event_id"], message["message_id"], session, purpose) for session in sessions])

    def ask_human(self, team_id, actor, body: QuestionInput):
        if actor.session_id is None:
            raise CollaborationForbidden("Only a formal member can request a clarification")
        key = (team_id, actor.id, "ask_human", body.request_id)
        digest = self._digest(body.model_dump(exclude={"request_id"}))
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.team(team_id, actor)
            replay = self._replay(key, digest)
            if replay is not None:
                return replay
            source = self._message_for(team_id, actor, body.source_message_id)
            if source["recipient_id"] and OPERATOR_ID not in {source["sender_id"], source["recipient_id"]}:
                raise CollaborationForbidden("The user cannot read this private work message")
            work = self.db.execute("""
                SELECT event_id, purpose FROM message_deliveries
                WHERE message_id = ? AND session_id = ? AND status IN ('pending', 'blocked')
            """, (source["id"], actor.session_id)).fetchone()
            if work is None:
                raise CollaborationInvalid("The clarification must reference work delivered to this session")

            recipient = OPERATOR_ID if body.private or source["recipient_id"] else None
            posted = self._create_message(team_id, actor, recipient=recipient, text=body.text,
                                          kind="question", mentions=[], reply_to=source["id"])
            question_id = posted["message_id"]
            work_key = (work["event_id"], actor.session_id, work["purpose"])
            self.db.execute("""
                INSERT INTO human_questions(question_id, asker_id, source_message_id, work_event_id, session_id, work_purpose)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (question_id, actor.id, source["id"], *work_key))
            self.db.execute("""
                UPDATE message_deliveries SET status = 'blocked'
                WHERE event_id = ? AND session_id = ? AND purpose = ?
            """, work_key)
            question = {"question_id": question_id, "asker_id": actor.id, "source_message_id": source["id"],
                        "answer_message_id": None}
            receipt = {"question": question, "message": self._message_for(team_id, actor, question_id)}
            self._save_receipt(key, digest, receipt)
            return receipt

    def question_waiting(self, question_id):
        return self.db.execute("""
            SELECT 1 FROM human_questions WHERE question_id = ? AND answer_message_id IS NULL
        """, (question_id,)).fetchone() is not None

    def _answer_question(self, question, answer):
        if question["answer_message_id"] is not None:
            raise TeamConflict("This question already has an answer. Reference the answer to send a correction.")
        self.db.execute("UPDATE human_questions SET answer_message_id = ? WHERE question_id = ?",
                        (answer["message_id"], question["question_id"]))
        work_key = (question["work_event_id"], question["session_id"], question["work_purpose"])
        still_blocked = self.db.execute("""
            SELECT 1 FROM human_questions
            WHERE work_event_id = ? AND session_id = ? AND work_purpose = ? AND answer_message_id IS NULL
        """, work_key).fetchone()
        if still_blocked is None:
            self.db.execute("""
                UPDATE message_deliveries SET status = 'resolved'
                WHERE event_id = ? AND session_id = ? AND purpose = ? AND status = 'blocked'
            """, work_key)
            self._deliver(answer, (question["session_id"],), "clarification.resume")

    def _correct_answer(self, source, question, correction, members):
        self.db.execute("INSERT INTO clarification_corrections VALUES (?, ?)",
                        (correction["message_id"], question["question_id"]))
        readers = self.db.execute("""
            SELECT DISTINCT sender_id FROM team_messages
            WHERE team_id = ? AND reply_to IN (?, ?)
        """, (source["team_id"], source["id"], question["answer_message_id"]))
        targets = {question["asker_id"]}
        if source["recipient_id"] is None:
            targets.update(row["sender_id"] for row in readers)
        self._deliver(correction, (members[target] for target in targets if target in members),
                      "clarification.correction")

    def send_message(self, team_id, actor, body: MessageInput):
        key = (team_id, actor.id, "send_message", body.request_id)
        mentions = sorted(set(body.mentions))
        digest = self._digest({**body.model_dump(exclude={"request_id"}), "mentions": mentions})
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            team = self.team(team_id, actor)
            replay = self._replay(key, digest)
            if replay is not None:
                self._message_for(team_id, actor, replay["message_id"])
                return replay

            members = {member["member_id"]: member["session_id"] for member in team["members"]}
            recipient = body.recipient_id
            source = self._message_for(team_id, actor, body.reply_to) if body.reply_to else None
            question = decision = None
            if source and actor.is_operator:
                question = self.db.execute("SELECT * FROM human_questions WHERE question_id = ?",
                                           (source["id"],)).fetchone()
                if question is None:
                    decision = self.db.execute("""
                        SELECT * FROM human_questions
                        WHERE answer_message_id = ? OR question_id = (
                            SELECT question_id FROM clarification_corrections WHERE message_id = ?
                        )
                    """, (source["id"], source["id"])).fetchone()
            if question is not None or decision is not None:
                if body.kind != "chat":
                    raise CollaborationInvalid("A clarification answer or correction must be a chat message")
                expected_recipient = (question or decision)["asker_id"] if source["recipient_id"] else None
                if recipient is not None and recipient != expected_recipient:
                    raise CollaborationForbidden("Reply in the same thread as the question or decision")
                recipient = expected_recipient
            if recipient == actor.id or (recipient is not None and recipient not in members and recipient != OPERATOR_ID):
                raise CollaborationInvalid("A private message requires a different team participant")
            if any(member_id not in members or member_id == actor.id for member_id in mentions):
                raise CollaborationInvalid("Mentions must identify other formal team members")
            if recipient is not None and (question is not None or decision is not None):
                mentions = [member_id for member_id in mentions if member_id == recipient]
            if recipient is not None and any(member_id != recipient for member_id in mentions):
                raise CollaborationInvalid("A private message may only mention its recipient")

            if source and source["recipient_id"] is not None:
                # Immutable references can only keep or narrow the source audience.
                private_readers = {source["sender_id"], source["recipient_id"]}
                if recipient is None or {actor.id, recipient} != private_readers:
                    raise CollaborationForbidden("This thread cannot include the referenced private message")
            if body.kind in {"assignment", "supplement"} and not actor.is_operator:
                raise CollaborationForbidden("Only the user can submit assignments or supplements")
            if body.kind == "assignment" and (source or recipient not in {None, team["lead_member_id"]}):
                raise CollaborationInvalid("An original assignment must be in the group or a private thread with the Lead")
            if body.kind == "supplement" and (not source or source["kind"] != "assignment"):
                raise CollaborationInvalid("A supplement must reference the original assignment")

            receipt = self._create_message(team_id, actor, recipient=recipient, text=body.text,
                                           kind=body.kind, mentions=mentions, reply_to=body.reply_to)
            if question is not None:
                self._answer_question(question, receipt)
            elif decision is not None:
                self._correct_answer(source, decision, receipt, members)
            else:
                targets = mentions
                if not targets and body.kind != "progress":
                    if recipient in members:
                        targets = [recipient]
                    elif recipient is None and actor.is_operator:
                        targets = [team["lead_member_id"]]
                self._deliver(receipt, (members[target] for target in targets), "message")
            self._save_receipt(key, digest, receipt)
            return receipt

    def pending_deliveries(self, team_id, actor):
        self.team(team_id, actor)
        deliveries = [dict(row) for row in self.db.execute(f"""
            SELECT d.* FROM message_deliveries d JOIN team_messages m ON m.id = d.message_id
            WHERE m.team_id = :team AND d.session_id = :session AND d.status = 'pending' AND {READABLE_MESSAGE}
            ORDER BY d.event_id
        """, {"team": team_id, "session": actor.session_id, "actor": actor.id})]
        for delivery in deliveries:
            if delivery["purpose"] != "clarification.resume":
                continue
            answers = self.db.execute("""
                SELECT q.source_message_id, q.answer_message_id
                FROM human_questions q JOIN human_questions last
                    ON q.work_event_id = last.work_event_id AND q.session_id = last.session_id
                    AND q.work_purpose = last.work_purpose
                JOIN team_messages m ON m.id = q.answer_message_id
                WHERE last.answer_message_id = ? ORDER BY m.seq
            """, (delivery["message_id"],)).fetchall()
            delivery["source_message_id"] = answers[0]["source_message_id"]
            delivery["answer_message_ids"] = [row["answer_message_id"] for row in answers]
        return deliveries

    def register_goal(self, team_id, actor, body: GoalInput):
        key = (team_id, actor.id, "register_goal", body.request_id)
        digest = self._digest({"source_message_id": body.source_message_id})
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            team = self.team(team_id, actor)
            source = self._message_for(team_id, actor, body.source_message_id)
            if actor.id != team["lead_member_id"]:
                raise CollaborationForbidden("Only the Lead can register a goal")
            if source["sender_id"] != OPERATOR_ID or source["kind"] != "assignment":
                raise CollaborationInvalid("A goal requires an original user assignment")
            replay = self._replay(key, digest)
            if replay is not None:
                return replay
            row = self.db.execute("""
                SELECT id AS goal_id, seq AS event_id, status, revision FROM team_goals WHERE source_message_id = ?
            """, (source["id"],)).fetchone()
            if row:
                receipt = dict(row)
            else:
                active = self.db.execute("SELECT 1 FROM team_goals WHERE team_id = ? AND status = 'active'",
                                         (team_id,)).fetchone()
                status = "queued" if active else "active"
                goal_id = uuid4().hex
                seq = self._event(team_id, "goal.registered", goal_id)
                self.db.execute("""
                    INSERT INTO team_goals(id, team_id, source_message_id, status, revision, seq, created_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                """, (goal_id, team_id, source["id"], status, seq, _now()))
                receipt = {"goal_id": goal_id, "event_id": seq, "status": status, "revision": 1}
            self._save_receipt(key, digest, receipt)
            return receipt

    def _head(self, team_id):
        return self.db.execute("SELECT COALESCE(MAX(seq), 0) FROM collaboration_events WHERE team_id = ?",
                               (team_id,)).fetchone()[0]

    def cursor(self, team_id, actor, seq):
        data = f"{team_id}:{actor.id}:{seq}".encode("utf-8")
        signature = hmac.digest(self.auth.cursor_key, data, "sha256").hex()
        return f"{seq}.{signature}"

    def resume_position(self, team_id, actor, token):
        self.team(team_id, actor)
        if not token or len(token) > 84 or not token.isascii():
            return None
        try:
            seq = int(token.partition(".")[0])
        except ValueError:
            return None
        if seq < 0 or not hmac.compare_digest(self.cursor(team_id, actor, seq), token):
            return None
        lower = self.db.execute("""
            SELECT seq FROM collaboration_events WHERE team_id = ? ORDER BY seq DESC LIMIT 1 OFFSET ?
        """, (team_id, CURSOR_WINDOW)).fetchone()
        if seq > self._head(team_id) or (lower and seq < lower["seq"]):
            return None
        return seq

    def snapshot(self, team_id, actor):
        parameters = {"team": team_id, "actor": actor.id}
        with self.db:
            self.db.execute("BEGIN")
            team = self.team(team_id, actor)
            messages = [_message(row) for row in self.db.execute(f"""
                SELECT m.* FROM team_messages m WHERE m.team_id = :team AND {READABLE_MESSAGE} ORDER BY m.seq
            """, parameters)]
            goals = [dict(row) for row in self.db.execute(f"""
                SELECT g.* FROM team_goals g JOIN team_messages m ON m.id = g.source_message_id
                WHERE g.team_id = :team AND {READABLE_MESSAGE} ORDER BY g.seq
            """, parameters)]
            questions = [dict(row) for row in self.db.execute(f"""
                SELECT {QUESTION_FIELDS} FROM human_questions q JOIN team_messages m ON m.id = q.question_id
                WHERE m.team_id = :team AND {READABLE_MESSAGE} ORDER BY m.seq
            """, parameters)]
            cursor = self.cursor(team_id, actor, self._head(team_id))
        return {"team_id": team_id, "actor_id": actor.id, "lead_id": team["lead_member_id"], "cursor": cursor,
                "participants": self.participants(team), "messages": messages, "goals": goals,
                "questions": questions}

    def events(self, team_id, actor, after):
        self.team(team_id, actor)
        rows = self.db.execute(f"""
            WITH batch AS (
                SELECT seq, type, object_id FROM collaboration_events
                WHERE team_id = :team AND seq > :after ORDER BY seq LIMIT :limit
            )
            SELECT e.seq AS event_seq, e.type, m.*, g.id AS goal_id, g.status AS goal_status,
                   g.revision AS goal_revision, g.created_at AS goal_created_at, {QUESTION_FIELDS}
            FROM batch e
            LEFT JOIN team_goals g ON e.type = 'goal.registered' AND g.id = e.object_id
            LEFT JOIN team_messages m ON m.id = COALESCE(g.source_message_id, e.object_id) AND {READABLE_MESSAGE}
            LEFT JOIN human_questions q ON q.question_id = m.id OR q.answer_message_id = m.id
            ORDER BY e.seq
        """, {"team": team_id, "after": after, "actor": actor.id, "limit": EVENT_BATCH_SIZE}).fetchall()
        visible = []
        for row in rows:
            if row["id"] is None:
                continue
            fields = dict(row)
            seq = fields.pop("event_seq")
            event_type = fields.pop("type")
            goal_id = fields.pop("goal_id")
            status = fields.pop("goal_status")
            revision = fields.pop("goal_revision")
            created_at = fields.pop("goal_created_at")
            question = {key: fields.pop(key) for key in
                        ("question_id", "asker_id", "source_message_id", "answer_message_id")}
            if goal_id is None:
                data = {"seq": seq, "message": _message(fields)}
                if question["question_id"] is not None:
                    if question["question_id"] == fields["id"]:
                        question["answer_message_id"] = None
                    data["question"] = question
            else:
                goal = {"id": goal_id, "team_id": team_id, "source_message_id": fields["id"], "status": status,
                        "revision": revision, "created_at": created_at, "seq": seq}
                data = {"seq": seq, "goal": goal}
            visible.append((event_type, seq, data))
        scanned_through = rows[-1]["event_seq"] if rows else after
        return visible, scanned_through, len(rows) == EVENT_BATCH_SIZE

    def tools_for(self, session_id):
        if self.auth.member(session_id) is None:
            return []
        return [{"type": "function", "function": {
            "name": name, "description": description, "parameters": schema.model_json_schema(),
        }} for name, (schema, description) in TOOLS.items()]

    def execute_tool(self, session_id, name, raw_arguments):
        actor = self.auth.member(session_id)
        if actor is None:
            return {"ok": False, "error": "Collaboration tools require a formal member session"}
        try:
            args = TOOLS[name][0].model_validate_json(raw_arguments)
            if name == "send_message":
                result = self.send_message(actor.team_id, actor, args)
            elif name == "register_goal":
                result = self.register_goal(actor.team_id, actor, args)
            elif name == "get_message":
                result = self.get_message(actor.team_id, actor, args.message_id)
            elif name == "ask_human":
                result = self.ask_human(actor.team_id, actor, args)
            else:
                result = self.participants(self.team(actor.team_id, actor))
            return {"ok": True, "result": result}
        except ValidationError:
            return {"ok": False, "error": "Invalid collaboration tool arguments", "status": 422}
        except (TeamNotFound, TeamConflict, CollaborationForbidden, CollaborationInvalid) as exc:
            status = {TeamNotFound: 404, TeamConflict: 409, CollaborationForbidden: 403, CollaborationInvalid: 422}
            return {"ok": False, "error": str(exc), "status": status[type(exc)]}

    @staticmethod
    def handles(name):
        return name in TOOLS


TOOLS = {
    "list_participants": (Command, "List the participant IDs and names in your team."),
    "get_message": (ReadMessageInput, "Read a visible message by ID."),
    "send_message": (MessageInput, "Send a group or bilateral team message. Supply a unique request ID."),
    "register_goal": (GoalInput, "As Lead, register the user's original assignment by message ID."),
    "ask_human": (QuestionInput, "Ask the user a blocking question about a message delivered to your session. "
                  "Pass its message ID. This pauses the turn until a quoted reply makes that work ready again."),
}
