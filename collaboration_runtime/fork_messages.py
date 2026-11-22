"""Scoped assistant conversations and parent decisions on returned results."""
import json
from typing import Literal
from uuid import uuid4

from pydantic import Field, ValidationError

from .artifacts import ArtifactError
from .collaboration import Command, ObjectId, RequestId, Text, CollaborationForbidden, CollaborationInvalid
from .local_auth import Principal
from .teams import TeamConflict, TeamNotFound
from .workspace import WorkspaceNotFound


class ForkMessageInput(Command):
    request_id: RequestId
    text: Text
    recipient_session_id: ObjectId | None = None
    version_ids: list[ObjectId] = Field(default_factory=list, max_length=16)


class ForkDecisionInput(Command):
    request_id: RequestId
    message_id: ObjectId
    decision: Literal["adopt", "return"]
    reason: str | None = Field(default=None, max_length=12000)


class SendToolInput(ForkMessageInput):
    assistant_id: ObjectId


class DecideToolInput(ForkDecisionInput):
    assistant_id: ObjectId


TOOLS = {
    "send_fork_message": (SendToolInput, "Send a private message within an authorized assistant delegation."),
    "decide_fork_result": (DecideToolInput, "As the parent, adopt or return an assistant result addressed to you."),
}


class ForkMessages:
    def __init__(self, forks, collaboration, runtime):
        self.forks = forks
        self.chat = collaboration
        self.runtime = runtime
        self.db = runtime.db
        self.workspace = runtime.workspace_tools
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS fork_messages (
                message_id TEXT PRIMARY KEY, assistant_id TEXT NOT NULL,
                sender_session_id TEXT NOT NULL, recipient_session_id TEXT NOT NULL,
                text TEXT NOT NULL, version_ids TEXT NOT NULL, event_id INTEGER NOT NULL,
                request_id TEXT NOT NULL, digest TEXT NOT NULL,
                UNIQUE(assistant_id, sender_session_id, request_id)
            );
            CREATE INDEX IF NOT EXISTS fork_messages_conversation ON fork_messages(assistant_id, sender_session_id, recipient_session_id);
            CREATE TABLE IF NOT EXISTS fork_result_decisions (
                decision_id TEXT PRIMARY KEY, assistant_id TEXT NOT NULL,
                parent_session_id TEXT NOT NULL, message_id TEXT NOT NULL REFERENCES fork_messages(message_id),
                decision TEXT NOT NULL, reason TEXT, event_id INTEGER NOT NULL,
                request_id TEXT NOT NULL, digest TEXT NOT NULL,
                UNIQUE(assistant_id, parent_session_id, request_id)
            );
        """)

    def _scope(self, assistant_id):
        return self.forks.require_active(assistant_id)

    def _member(self, session_id, team_id):
        principal = self.chat.auth.member(session_id)
        if principal is None or principal.team_id != team_id:
            raise TeamNotFound("Participant not found")
        return principal

    def _resolve_pair(self, fork, sender, recipient):
        assistant = fork["session_id"]
        eligible = {fork["parent_session_id"], *fork["contact_session_ids"]}
        if sender == assistant:
            target = recipient or fork["parent_session_id"]
            if target not in eligible:
                other = self._scope(target)
                if (other["session_id"] == assistant or
                    any(other[field] != fork[field] for field in ("team_id", "goal_id", "scope_id", "expected_revision")) or
                    other["parent_session_id"] == fork["parent_session_id"] or
                    other["parent_session_id"] not in fork["contact_session_ids"] or
                    fork["parent_session_id"] not in other["contact_session_ids"]):
                    raise CollaborationForbidden("Assistant contact is outside the mutually authorized scope")
                return target, Principal(assistant, fork["team_id"], assistant), None
            return target, Principal(assistant, fork["team_id"], assistant), self._member(target, fork["team_id"])
        if sender not in eligible or (recipient is not None and recipient != assistant):
            raise CollaborationForbidden("Sender or recipient is outside this delegation")
        return assistant, self._member(sender, fork["team_id"]), None

    def _check_versions(self, sender, recipient, version_ids):
        if len(set(version_ids)) != len(version_ids):
            raise CollaborationInvalid("Duplicate artifact version")
        try:
            for session in (sender, recipient):
                actor = self.workspace.actor(session)
                for version_id in version_ids:
                    self.workspace._resolve(actor, version_id)
        except (WorkspaceNotFound, ArtifactError) as exc:
            raise TeamNotFound("Artifact version not accessible to both participants") from exc

    def authorize_job(self, recipient, encoded):
        data = json.loads(encoded)
        if data.get("type") == "fork.message":
            self.authorize_message(recipient, data.get("message_id"))

    def authorize_message(self, recipient, message_id):
        message = self.db.execute("""
            SELECT * FROM fork_messages WHERE message_id = ? AND recipient_session_id = ?
        """, (message_id, recipient)).fetchone()
        if message is None:
            raise WorkspaceNotFound("Assistant message is unavailable")
        try:
            fork = self._scope(message["assistant_id"])
            target, _, _ = self._resolve_pair(fork, message["sender_session_id"], recipient)
            if target != recipient:
                raise CollaborationForbidden("Assistant recipient changed")
            version_ids = json.loads(message["version_ids"])
            self._check_versions(message["sender_session_id"], recipient, version_ids)
            return version_ids
        except (TeamNotFound, TeamConflict, CollaborationForbidden, CollaborationInvalid) as exc:
            raise WorkspaceNotFound("Assistant message scope is no longer available") from exc

    @staticmethod
    def add_delivery_versions(payload, message_id, version_ids):
        if payload["message"]["id"] == message_id:
            payload["message"]["version_ids"] = version_ids
        else:
            payload["fork_message"] = {"id": message_id, "version_ids": version_ids}

    def post(self, assistant_id, sender_session_id, body: ForkMessageInput):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            fork = self._scope(assistant_id)
            recipient, sender, target = self._resolve_pair(fork, sender_session_id, body.recipient_session_id)
            self._check_versions(sender_session_id, recipient, body.version_ids)
            digest = self.chat._digest({"text": body.text, "recipient": recipient, "version_ids": body.version_ids})
            old = self.db.execute("SELECT * FROM fork_messages WHERE assistant_id=? AND sender_session_id=? AND request_id=?",
                                  (assistant_id, sender_session_id, body.request_id)).fetchone()
            if old:
                if old["digest"] != digest:
                    raise TeamConflict("The request ID already binds different message parameters")
                return {"message_id": old["message_id"], "event_id": old["event_id"]}
            recipient_id = target.id if target else recipient
            receipt = self.chat._create_message(fork["team_id"], sender, recipient=recipient_id,
                                                text=body.text, kind="chat", mentions=[], reply_to=None)
            self.db.execute("INSERT INTO fork_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (receipt["message_id"], assistant_id, sender_session_id, recipient, body.text,
                             json.dumps(body.version_ids), receipt["event_id"], body.request_id, digest))
            if target:
                self.chat._deliver(receipt, (recipient,), "fork.message")
            else:
                job = {"type": "fork.message", "message_id": receipt["message_id"],
                       "sender_session_id": sender_session_id, "text": body.text, "version_ids": body.version_ids}
                self.db.execute("INSERT INTO assistant_jobs(session_id, input, status) VALUES (?, ?, 'pending')",
                                (recipient, json.dumps(job, ensure_ascii=False)))
            return {"message_id": receipt["message_id"], "event_id": receipt["event_id"]}

    def list_for(self, assistant_id, actor_session_id):
        fork = self._scope(assistant_id)
        if actor_session_id != assistant_id:
            self._resolve_pair(fork, actor_session_id, assistant_id)
        if actor_session_id == assistant_id:
            rows = self.db.execute("""SELECT message_id, sender_session_id, recipient_session_id, text, version_ids, event_id
                FROM fork_messages WHERE sender_session_id=? OR recipient_session_id=? ORDER BY event_id""",
                (assistant_id, assistant_id)).fetchall()
        else:
            rows = self.db.execute("""SELECT message_id, sender_session_id, recipient_session_id, text, version_ids, event_id
                FROM fork_messages WHERE assistant_id=? AND (sender_session_id=? OR recipient_session_id=?) ORDER BY event_id""",
                (assistant_id, actor_session_id, actor_session_id)).fetchall()
        result = []
        for row in rows:
            versions = json.loads(row["version_ids"])
            try:
                if self.chat.auth.member(row["sender_session_id"]) is None:
                    source_fork = self._scope(row["sender_session_id"])
                    self._resolve_pair(source_fork, row["sender_session_id"], row["recipient_session_id"])
                self._check_versions(row["sender_session_id"], row["recipient_session_id"], versions)
            except (TeamNotFound, TeamConflict, CollaborationForbidden, WorkspaceNotFound):
                continue
            result.append({**dict(row), "version_ids": versions})
        return result

    def decide(self, assistant_id, parent_session_id, body: ForkDecisionInput):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            fork = self._scope(assistant_id)
            if parent_session_id != fork["parent_session_id"]:
                raise CollaborationForbidden("Only the parent may decide")
            parent = self._member(parent_session_id, fork["team_id"])
            message = self.db.execute("""SELECT * FROM fork_messages WHERE message_id=? AND assistant_id=?
                AND sender_session_id=? AND recipient_session_id=?""",
                (body.message_id, assistant_id, assistant_id, parent_session_id)).fetchone()
            if not message:
                raise TeamNotFound("Assistant result not found")
            self._check_versions(assistant_id, parent_session_id, json.loads(message["version_ids"]))
            digest = self.chat._digest(body.model_dump(exclude={"request_id"}))
            old = self.db.execute("SELECT * FROM fork_result_decisions WHERE assistant_id=? AND parent_session_id=? AND request_id=?",
                                  (assistant_id, parent_session_id, body.request_id)).fetchone()
            if old:
                if old["digest"] != digest:
                    raise TeamConflict("The request ID already binds a different decision")
                return {"decision_id": old["decision_id"], "event_id": old["event_id"], "message_id": old["message_id"], "decision": old["decision"]}
            receipt = self.chat._create_message(fork["team_id"], parent, recipient=assistant_id,
                text=f"Result {body.decision}: {body.reason or ''}", kind="chat", mentions=[], reply_to=body.message_id)
            decision_id = uuid4().hex
            self.db.execute("INSERT INTO fork_result_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (decision_id, assistant_id, parent_session_id, body.message_id, body.decision,
                             body.reason, receipt["event_id"], body.request_id, digest))
            return {"decision_id": decision_id, "event_id": receipt["event_id"], "message_id": body.message_id, "decision": body.decision}

    def tools_for(self, role):
        if self.chat.auth.member(role) is None:
            try:
                actor = self.workspace.actor(role)
                if actor.parent_session_id is None or "send_fork_message" not in actor.allowed_tools:
                    return []
                self._scope(role)
            except (WorkspaceNotFound, TeamNotFound, TeamConflict, CollaborationForbidden):
                return []
            names = ["send_fork_message"]
        else:
            names = list(TOOLS)
        return [{"type": "function", "function": {"name": name, "description": TOOLS[name][1],
                 "parameters": TOOLS[name][0].model_json_schema()}} for name in names]

    def execute(self, role, name, args):
        if name not in TOOLS:
            return {"ok": False, "error": "Unknown fork tool"}
        try:
            data = TOOLS[name][0].model_validate_json(args)
            assistant_id = data.assistant_id
            if self.chat.auth.member(role) is None:
                actor = self.workspace.actor(role)
                if (name != "send_fork_message" or actor.parent_session_id is None or
                    "send_fork_message" not in actor.allowed_tools or assistant_id != role):
                    raise CollaborationForbidden("Assistant tool is not authorized for this scope")
            fields = data.model_dump(exclude={"assistant_id"})
            result = (self.post(assistant_id, role, ForkMessageInput(**fields)) if name == "send_fork_message"
                      else self.decide(assistant_id, role, ForkDecisionInput(**fields)))
            return {"ok": True, "result": result}
        except ValidationError:
            return {"ok": False, "error": "Invalid fork tool arguments"}
        except (TeamNotFound, TeamConflict, CollaborationForbidden, CollaborationInvalid, WorkspaceNotFound, ArtifactError) as exc:
            return {"ok": False, "error": str(exc)}
