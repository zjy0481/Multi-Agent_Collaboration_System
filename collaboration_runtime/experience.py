"""Owner-scoped lessons, explicit cross-goal handoffs, and adoption evidence.

This module is deliberately self-contained.  It reads team membership, goals,
artifact ACLs and assistant forks through the existing services, but owns its
queries, receipts, HTTP router factory and model-tool adapter.  ``create_app``
constructs the service, registers :func:`build_router` with the application's
existing authentication dependency, and exposes the model tools through
``Runtime``.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field, ValidationError, model_validator

from .artifacts import ArtifactError, ArtifactNotFound
from .collaboration import (
    CollaborationForbidden,
    CollaborationInvalid,
    Command,
    ObjectId,
    RequestId,
    Text,
    _now,
)
from .local_auth import Principal
from .teams import TeamConflict, TeamNotFound
from .workspace import ASSISTANT_EXPERIENCE_TOOLS, WorkspaceNotFound


SourceList = Annotated[list["LessonSource"], Field(min_length=1, max_length=32)]
EvidenceText = Annotated[list[Text], Field(min_length=1, max_length=32)]
OptionalSources = Annotated[list["LessonSource"], Field(max_length=32)]


class LessonSource(Command):
    """A durable pointer to the evidence used to form a lesson."""

    kind: Literal["message", "artifact"]
    source_id: ObjectId


class RecordLessonInput(Command):
    request_id: RequestId
    goal_id: ObjectId
    title: Text | None = None
    text: Text
    sources: SourceList
    applies_when: Text
    invalid_when: Text


class RetrieveLessonsInput(Command):
    request_id: RequestId
    target_goal_id: ObjectId
    query: Annotated[str, Field(max_length=400)] = ""
    limit: Annotated[int, Field(ge=1, le=50)] = 20


class HandoffLessonsInput(Command):
    request_id: RequestId
    source_goal_id: ObjectId
    target_goal_id: ObjectId
    lesson_ids: Annotated[list[ObjectId], Field(max_length=100)] = Field(default_factory=list)
    purpose: Text
    recipient_session_id: ObjectId | None = None
    recipient_id: ObjectId | None = None
    artifact_version_ids: Annotated[list[ObjectId], Field(max_length=100)] = Field(default_factory=list)

    @model_validator(mode="after")
    def exactly_one_recipient(self):
        if (self.recipient_session_id is None) == (self.recipient_id is None):
            raise ValueError("Specify exactly one recipient_session_id or recipient_id")
        if not self.lesson_ids and not self.artifact_version_ids:
            raise ValueError("A handoff requires at least one lesson or artifact version")
        return self


class RecordLessonUseInput(Command):
    request_id: RequestId
    lesson_id: ObjectId
    action: Text
    evidence: EvidenceText
    sources: OptionalSources = Field(default_factory=list)


class ObserveLessonInput(Command):
    request_id: RequestId
    outcome: Literal["behavior_changed", "no_change", "invalidated"]
    note: Text
    evidence: EvidenceText
    sources: OptionalSources = Field(default_factory=list)
    recheck_items: Annotated[list[Text], Field(max_length=32)] = Field(default_factory=list)


class ReadLessonTool(Command):
    lesson_id: ObjectId
    target_goal_id: ObjectId


class ExperienceGoalTool(Command):
    target_goal_id: ObjectId


class ReadHandoffTool(Command):
    handoff_id: ObjectId


@dataclass(frozen=True)
class Caller:
    kind: Literal["member", "assistant"]
    team_id: str
    session_id: str
    member_id: str | None = None
    parent_session_id: str | None = None
    goal_id: str | None = None
    scope_id: str | None = None
    versions: frozenset[str] = frozenset()
    allowed_tools: frozenset[str] = frozenset()

    @property
    def key(self):
        return f"{self.kind}:{self.session_id}"


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class ExperienceService:
    """Persist lessons and their authorization/usage trail.

    The first SQLite query in a search selects candidates for the caller and
    only then does content matching run.  No query text is sent to an index or
    an unauthorized candidate.
    """

    def __init__(self, collaboration, workspace, forks=None, *,
                 assistant_read_tools=ASSISTANT_EXPERIENCE_TOOLS):
        self.collaboration = collaboration
        self.workspace = workspace
        self.forks = forks
        self.db = collaboration.db
        self.artifacts = workspace.artifacts
        self.assistant_read_tools = frozenset(assistant_read_tools)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS experience_lessons (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                owner_id TEXT NOT NULL,
                owner_session_id TEXT NOT NULL,
                goal_id TEXT NOT NULL REFERENCES team_goals(id),
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                sources TEXT NOT NULL,
                applies_when TEXT NOT NULL,
                invalid_when TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS experience_lessons_owner
                ON experience_lessons(team_id, owner_id, goal_id);
            CREATE TABLE IF NOT EXISTS experience_handoffs (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                source_goal_id TEXT NOT NULL REFERENCES team_goals(id),
                target_goal_id TEXT NOT NULL REFERENCES team_goals(id),
                sender_id TEXT NOT NULL,
                sender_session_id TEXT NOT NULL,
                recipient_session_id TEXT NOT NULL,
                recipient_id TEXT,
                purpose TEXT NOT NULL,
                lesson_ids TEXT NOT NULL,
                artifact_version_ids TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS experience_handoffs_recipient
                ON experience_handoffs(team_id, target_goal_id, recipient_session_id);
            CREATE TABLE IF NOT EXISTS experience_handoff_lessons (
                handoff_id TEXT NOT NULL REFERENCES experience_handoffs(id),
                lesson_id TEXT NOT NULL REFERENCES experience_lessons(id),
                PRIMARY KEY (handoff_id, lesson_id)
            );
            CREATE TABLE IF NOT EXISTS experience_retrievals (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                caller_key TEXT NOT NULL,
                caller_session_id TEXT NOT NULL,
                target_goal_id TEXT NOT NULL REFERENCES team_goals(id),
                query TEXT NOT NULL,
                visible_lesson_ids TEXT NOT NULL,
                matched_lesson_ids TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS experience_retrievals_caller
                ON experience_retrievals(team_id, caller_key, target_goal_id);
            CREATE TABLE IF NOT EXISTS experience_uses (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                retrieval_id TEXT NOT NULL REFERENCES experience_retrievals(id),
                lesson_id TEXT NOT NULL REFERENCES experience_lessons(id),
                target_goal_id TEXT NOT NULL REFERENCES team_goals(id),
                actor_id TEXT NOT NULL,
                actor_session_id TEXT NOT NULL,
                action TEXT NOT NULL,
                evidence TEXT NOT NULL,
                sources TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS experience_uses_retrieval
                ON experience_uses(retrieval_id, lesson_id);
            CREATE TABLE IF NOT EXISTS experience_observations (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                use_id TEXT NOT NULL REFERENCES experience_uses(id),
                actor_id TEXT NOT NULL,
                actor_session_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                note TEXT NOT NULL,
                evidence TEXT NOT NULL,
                sources TEXT NOT NULL,
                recheck_items TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS experience_observations_use
                ON experience_observations(use_id);
            CREATE TABLE IF NOT EXISTS experience_requests (
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                caller_key TEXT NOT NULL,
                operation TEXT NOT NULL,
                request_id TEXT NOT NULL,
                digest TEXT NOT NULL,
                receipt TEXT NOT NULL,
                PRIMARY KEY (team_id, caller_key, operation, request_id)
            );
        """)

    # ---------- identity, goals and source projection ----------

    def _member(self, team_id: str, session_id: str) -> Principal:
        member = self.collaboration.auth.member(session_id)
        if member is None or member.team_id != team_id or member.id is None:
            raise TeamNotFound("Team member not found")
        return member

    def _caller(self, team_id: str, session_id: str) -> Caller:
        member = self.collaboration.auth.member(session_id)
        if member is not None:
            if member.team_id != team_id:
                raise TeamNotFound("Team not found")
            return Caller("member", team_id, session_id, member_id=member.id)
        if self.forks is None or not self.forks.exists(session_id):
            raise TeamNotFound("Execution session not found")
        fork = self.forks.require_active(session_id)
        if fork["team_id"] != team_id:
            raise TeamNotFound("Execution session not found")
        if not set(self.assistant_read_tools).intersection(fork["allowed_tools"]):
            raise CollaborationForbidden("The assistant fork does not allow lesson retrieval")
        actor = self.workspace.actor(session_id)
        return Caller(
            "assistant", team_id, session_id, parent_session_id=fork["parent_session_id"],
            goal_id=fork["goal_id"], scope_id=fork["scope_id"],
            versions=frozenset(actor.versions), allowed_tools=frozenset(fork["allowed_tools"]),
        )

    @staticmethod
    def _require_member(caller: Caller) -> Principal:
        if caller.kind != "member" or caller.member_id is None:
            raise CollaborationForbidden("Only a formal member may change personal experience records")
        return Principal(caller.member_id, caller.team_id, caller.session_id)

    def _goal(self, team_id: str, goal_id: str, caller: Caller | None = None):
        row = self.db.execute(
            "SELECT * FROM team_goals WHERE team_id = ? AND id = ?", (team_id, goal_id),
        ).fetchone()
        if row is None:
            raise TeamNotFound("Goal not found")
        if caller is not None and caller.kind == "assistant" and caller.goal_id != goal_id:
            raise TeamNotFound("Goal not found")
        if caller is not None and caller.kind == "member":
            self.collaboration._message_for(team_id, self._require_member(caller), row["source_message_id"])
        return dict(row)

    def _require_active_adoption(self, team_id, goal_id, caller):
        goal = self._goal(team_id, goal_id, caller)
        if goal["status"] != "active":
            raise TeamConflict("Lesson adoption requires an active goal")
        return goal

    def _check_source(self, caller: Caller, source: dict):
        try:
            kind, source_id = source["kind"], source["source_id"]
        except (KeyError, TypeError):
            raise CollaborationInvalid("Invalid lesson source") from None
        if kind == "message":
            if caller.kind == "assistant":
                row = self.db.execute(
                    "SELECT recipient_id FROM team_messages WHERE team_id = ? AND id = ?",
                    (caller.team_id, source_id),
                ).fetchone()
                if row is None or row["recipient_id"] is not None:
                    raise TeamNotFound("Lesson source not found")
            else:
                self.collaboration._message_for(
                    caller.team_id, self._require_member(caller), source_id,
                )
            return
        if kind != "artifact":
            raise CollaborationInvalid("Invalid lesson source kind")
        if caller.kind == "assistant":
            if source_id not in caller.versions:
                raise TeamNotFound("Lesson source not found")
            readers = self.artifacts.reference_readers(caller.team_id, source_id)
            if caller.parent_session_id not in readers:
                raise TeamNotFound("Lesson source not found")
            return
        readers = self.artifacts.reference_readers(caller.team_id, source_id)
        if caller.session_id not in readers:
            raise TeamNotFound("Lesson source not found")

    def _check_version(self, caller: Caller, version_id: str):
        self._check_source(caller, {"kind": "artifact", "source_id": version_id})

    @staticmethod
    def _sources(row):
        try:
            value = json.loads(row["sources"])
        except (TypeError, ValueError, KeyError) as exc:
            raise ArtifactError("Invalid lesson evidence metadata") from exc
        if not isinstance(value, list):
            raise ArtifactError("Invalid lesson evidence metadata")
        return value

    def _lesson_row(self, team_id, lesson_id):
        row = self.db.execute(
            "SELECT * FROM experience_lessons WHERE team_id = ? AND id = ?", (team_id, lesson_id),
        ).fetchone()
        if row is None:
            raise TeamNotFound("Lesson not found")
        return row

    def _handoff_row(self, team_id, handoff_id):
        row = self.db.execute(
            "SELECT * FROM experience_handoffs WHERE team_id = ? AND id = ?", (team_id, handoff_id),
        ).fetchone()
        if row is None:
            raise TeamNotFound("Lesson handoff not found")
        return row

    @staticmethod
    def _handoff_ids(handoff):
        try:
            lessons = json.loads(handoff["lesson_ids"])
            versions = json.loads(handoff["artifact_version_ids"])
        except (TypeError, ValueError, KeyError) as exc:
            raise ArtifactError("Invalid lesson handoff metadata") from exc
        if (
            not isinstance(lessons, list)
            or not all(isinstance(item, str) for item in lessons)
            or not isinstance(versions, list)
            or not all(isinstance(item, str) for item in versions)
        ):
            raise ArtifactError("Invalid lesson handoff metadata")
        return lessons, versions

    def _owned_lesson(self, team_id, lesson_id, caller, source_goal_id):
        lesson = self._lesson_row(team_id, lesson_id)
        if lesson["owner_id"] != caller.member_id:
            raise TeamNotFound("Lesson not found")
        if lesson["goal_id"] != source_goal_id:
            raise CollaborationInvalid("A handoff must cite lessons from its source goal")
        return lesson

    def _check_handoff(self, caller: Caller, handoff, lesson_row, target_goal_id):
        if handoff["target_goal_id"] != target_goal_id or handoff["recipient_session_id"] != caller.session_id:
            raise TeamNotFound("Lesson not found")
        lesson_ids, versions = self._handoff_ids(handoff)
        if lesson_row["id"] not in lesson_ids:
            raise TeamNotFound("Lesson not found")
        for source in self._sources(lesson_row):
            self._check_source(caller, source)
        for version_id in versions:
            self._check_version(caller, version_id)

    def _handoff_view(self, caller, handoff):
        if caller.session_id not in {
            handoff["sender_session_id"], handoff["recipient_session_id"],
        }:
            raise TeamNotFound("Lesson handoff not found")
        try:
            self._goal(handoff["team_id"], handoff["target_goal_id"], caller)
            lesson_ids, versions = self._handoff_ids(handoff)
            for version_id in versions:
                self._check_version(caller, version_id)
            lessons = []
            for lesson_id in lesson_ids:
                lesson = self._lesson_row(handoff["team_id"], lesson_id)
                if caller.session_id == handoff["recipient_session_id"]:
                    self._check_handoff(caller, handoff, lesson, handoff["target_goal_id"])
                else:
                    if lesson["owner_id"] != handoff["sender_id"]:
                        raise TeamNotFound("Lesson not found")
                    for source in self._sources(lesson):
                        self._check_source(caller, source)
                lessons.append(self._lesson_view(caller, lesson, handoff))
        except (TeamNotFound, ArtifactNotFound):
            raise TeamNotFound("Lesson handoff not found") from None
        return {
            "handoff_id": handoff["id"],
            "source_goal_id": handoff["source_goal_id"],
            "target_goal_id": handoff["target_goal_id"],
            "sender_id": handoff["sender_id"],
            "recipient_session_id": handoff["recipient_session_id"],
            "recipient_id": handoff["recipient_id"],
            "lesson_ids": lesson_ids,
            "artifact_version_ids": versions,
            "artifact_versions": [
                self._version_metadata(handoff["team_id"], version_id) for version_id in versions
            ],
            "lessons": lessons,
            "purpose": handoff["purpose"],
            "created_at": handoff["created_at"],
        }

    def _version_metadata(self, team_id, version_id):
        row = self.artifacts.reference(team_id, version_id)
        return {
            key: row[key] for key in (
                "version_id", "artifact_id", "version", "source_path", "sha256",
                "author_session_id", "scope_id",
            )
        }

    def _lesson_view(self, caller, lesson_row, handoff=None):
        for source in self._sources(lesson_row):
            self._check_source(caller, source)
        result = {
            "id": lesson_row["id"],
            "team_id": lesson_row["team_id"],
            "owner_id": lesson_row["owner_id"],
            "goal_id": lesson_row["goal_id"],
            "title": lesson_row["title"],
            "text": lesson_row["text"],
            "sources": self._sources(lesson_row),
            "applies_when": lesson_row["applies_when"],
            "invalid_when": lesson_row["invalid_when"],
            "created_at": lesson_row["created_at"],
        }
        if handoff is None:
            result["access"] = "owner"
            return result
        _, versions = self._handoff_ids(handoff)
        result.update(
            access="handoff",
            handoff={
                "id": handoff["id"],
                "source_goal_id": handoff["source_goal_id"],
                "target_goal_id": handoff["target_goal_id"],
                "sender_id": handoff["sender_id"],
                "purpose": handoff["purpose"],
                "artifact_version_ids": versions,
                "artifact_versions": [
                    self._version_metadata(handoff["team_id"], version_id) for version_id in versions
                ],
                "created_at": handoff["created_at"],
            },
        )
        return result

    def _candidates(self, caller: Caller, target_goal_id: str):
        """Return authorized and source-readable candidates before matching."""
        result = []
        rows = self.db.execute(
            "SELECT * FROM experience_lessons WHERE team_id = ? ORDER BY rowid", (caller.team_id,),
        ).fetchall()
        for lesson_row in rows:
            if caller.kind == "member" and lesson_row["owner_id"] == caller.member_id:
                try:
                    for source in self._sources(lesson_row):
                        self._check_source(caller, source)
                except (TeamNotFound, ArtifactError):
                    continue
                result.append((lesson_row, None))
                continue
            handoffs = self.db.execute("""
                SELECT * FROM experience_handoffs
                WHERE team_id = ? AND target_goal_id = ? AND recipient_session_id = ?
                ORDER BY rowid DESC
            """, (caller.team_id, target_goal_id, caller.session_id)).fetchall()
            for handoff in handoffs:
                try:
                    self._check_handoff(caller, handoff, lesson_row, target_goal_id)
                except (TeamNotFound, ArtifactError):
                    continue
                result.append((lesson_row, handoff))
                break
        return result

    @staticmethod
    def _matches(lesson_row, query: str):
        normalized = query.strip().casefold()
        if not normalized:
            return True
        haystack = " ".join((
            lesson_row["title"], lesson_row["text"], lesson_row["applies_when"], lesson_row["invalid_when"],
        )).casefold()
        if normalized in haystack:
            return True
        tokens = re.findall(r"[\w\u3400-\u9fff]+", normalized)
        return bool(tokens) and all(token in haystack for token in tokens)

    # ---------- request receipts ----------

    def _replay(self, team_id, caller_key, operation, body, *, scope=None):
        payload = body.model_dump(exclude={"request_id"})
        if scope is not None:
            payload = {**payload, "scope": scope}
        digest = self.collaboration._digest(payload)
        row = self.db.execute("""
            SELECT digest, receipt FROM experience_requests
            WHERE team_id = ? AND caller_key = ? AND operation = ? AND request_id = ?
        """, (team_id, caller_key, operation, body.request_id)).fetchone()
        if row is None:
            return digest, None
        if row["digest"] != digest:
            raise ExperienceConflict("Request ID was already used with different parameters")
        return digest, json.loads(row["receipt"])

    def _save_receipt(self, team_id, caller_key, operation, request_id, digest, receipt):
        self.db.execute(
            "INSERT INTO experience_requests VALUES (?, ?, ?, ?, ?, ?)",
            (team_id, caller_key, operation, request_id, digest, _encode(receipt)),
        )

    # ---------- writes ----------

    def record_lesson(self, team_id, caller_session_id, body: RecordLessonInput):
        if not isinstance(body, RecordLessonInput):
            body = RecordLessonInput.model_validate(body)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            caller = self._caller(team_id, caller_session_id)
            self._require_member(caller)
            digest, replay = self._replay(team_id, caller.key, "record_lesson", body)
            if replay is not None:
                lesson = self._lesson_row(team_id, replay["lesson_id"])
                self._lesson_view(caller, lesson)
                return replay
            goal = self._goal(team_id, body.goal_id, caller)
            for source in body.sources:
                self._check_source(caller, source.model_dump())
            lesson_id = uuid4().hex
            title = (body.title or body.text.splitlines()[0])[:120]
            record = {
                "id": lesson_id,
                "team_id": team_id,
                "owner_id": caller.member_id,
                "owner_session_id": caller.session_id,
                "goal_id": goal["id"],
                "title": title or "Lesson",
                "text": body.text,
                "sources": [source.model_dump() for source in body.sources],
                "applies_when": body.applies_when,
                "invalid_when": body.invalid_when,
                "created_at": _now(),
            }
            self.db.execute("""
                INSERT INTO experience_lessons
                (id, team_id, owner_id, owner_session_id, goal_id, title, text,
                 sources, applies_when, invalid_when, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record["id"], team_id, record["owner_id"], record["owner_session_id"], record["goal_id"],
                record["title"], record["text"], _encode(record["sources"]), record["applies_when"],
                record["invalid_when"], record["created_at"],
            ))
            receipt = {
                "lesson_id": lesson_id,
                "goal_id": goal["id"],
                "owner_id": caller.member_id,
                "created_at": record["created_at"],
            }
            self._save_receipt(team_id, caller.key, "record_lesson", body.request_id, digest, receipt)
            return receipt

    def handoff_lessons(self, team_id, caller_session_id, body: HandoffLessonsInput):
        if not isinstance(body, HandoffLessonsInput):
            body = HandoffLessonsInput.model_validate(body)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            caller = self._caller(team_id, caller_session_id)
            self._require_member(caller)
            self._goal(team_id, body.source_goal_id, caller)
            self._goal(team_id, body.target_goal_id, caller)
            digest, replay = self._replay(team_id, caller.key, "handoff_lessons", body)
            if replay is not None:
                self._handoff_view(caller, self._handoff_row(team_id, replay["handoff_id"]))
                return replay
            lessons = [
                self._owned_lesson(team_id, lesson_id, caller, body.source_goal_id)
                for lesson_id in sorted(set(body.lesson_ids))
            ]
            for lesson in lessons:
                for source in self._sources(lesson):
                    self._check_source(caller, source)
            versions = sorted(set(body.artifact_version_ids))
            for version_id in versions:
                self._check_version(caller, version_id)
            recipient = self._recipient(team_id, body)
            self._goal(team_id, body.target_goal_id, recipient["caller"])
            for lesson in lessons:
                for source in self._sources(lesson):
                    self._check_source(recipient["caller"], source)
            for version_id in versions:
                self._check_version(recipient["caller"], version_id)
            handoff_id = uuid4().hex
            record = {
                "handoff_id": handoff_id,
                "source_goal_id": body.source_goal_id,
                "target_goal_id": body.target_goal_id,
                "lesson_ids": [lesson["id"] for lesson in lessons],
                "artifact_version_ids": versions,
                "recipient_session_id": recipient["session_id"],
                "recipient_id": recipient["recipient_id"],
                "purpose": body.purpose,
                "created_at": _now(),
            }
            self.db.execute("""
                INSERT INTO experience_handoffs
                (id, team_id, source_goal_id, target_goal_id, sender_id, sender_session_id,
                 recipient_session_id, recipient_id, purpose, lesson_ids,
                 artifact_version_ids, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                handoff_id, team_id, body.source_goal_id, body.target_goal_id, caller.member_id,
                caller.session_id, recipient["session_id"], recipient["recipient_id"], body.purpose,
                _encode(record["lesson_ids"]), _encode(versions), record["created_at"],
            ))
            self.db.executemany(
                "INSERT INTO experience_handoff_lessons VALUES (?, ?)",
                [(handoff_id, lesson_id) for lesson_id in record["lesson_ids"]],
            )
            receipt = {
                **record,
                "lesson_id": record["lesson_ids"][0] if len(record["lesson_ids"]) == 1 else None,
            }
            self._save_receipt(team_id, caller.key, "handoff_lessons", body.request_id, digest, receipt)
            return receipt

    def _recipient(self, team_id, body: HandoffLessonsInput):
        if body.recipient_id is not None:
            team = self.collaboration.teams.get(team_id)
            member = next(
                (item for item in team["members"] if item["member_id"] == body.recipient_id), None,
            )
            if member is None:
                raise TeamNotFound("Recipient not found")
            session_id = member["session_id"]
        else:
            session_id = body.recipient_session_id
        member = self.collaboration.auth.member(session_id)
        if member is not None:
            if member.team_id != team_id:
                raise TeamNotFound("Recipient not found")
            return {
                "session_id": session_id, "recipient_id": member.id,
                "caller": Caller("member", team_id, session_id, member_id=member.id),
            }
        if self.forks is None or not self.forks.exists(session_id):
            raise TeamNotFound("Recipient not found")
        try:
            fork = self.forks.require_active(session_id)
        except (TeamConflict, TeamNotFound):
            raise TeamNotFound("Recipient not found") from None
        if fork["team_id"] != team_id or fork["goal_id"] != body.target_goal_id:
            raise TeamNotFound("Recipient not found")
        if not set(self.assistant_read_tools).intersection(fork["allowed_tools"]):
            raise TeamNotFound("Recipient not found")
        actor = self.workspace.actor(session_id)
        return {
            "session_id": session_id, "recipient_id": None,
            "caller": Caller(
                "assistant", team_id, session_id, parent_session_id=fork["parent_session_id"],
                goal_id=fork["goal_id"], scope_id=fork["scope_id"],
                versions=frozenset(actor.versions), allowed_tools=frozenset(fork["allowed_tools"]),
            ),
        }

    def retrieve_lessons(self, team_id, caller_session_id, body: RetrieveLessonsInput):
        if not isinstance(body, RetrieveLessonsInput):
            body = RetrieveLessonsInput.model_validate(body)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            caller = self._caller(team_id, caller_session_id)
            digest, replay = self._replay(team_id, caller.key, "retrieve_lessons", body)
            if replay is not None:
                return self._project_retrieval(team_id, caller, replay["retrieval_id"])
            self._goal(team_id, body.target_goal_id, caller)
            if caller.kind == "assistant" and caller.goal_id != body.target_goal_id:
                raise TeamNotFound("Goal not found")
            candidates = self._candidates(caller, body.target_goal_id)
            matched = [
                (lesson, handoff) for lesson, handoff in candidates
                if self._matches(lesson, body.query)
            ][:body.limit]
            retrieval_id = uuid4().hex
            created_at = _now()
            visible_ids = [lesson["id"] for lesson, _ in candidates]
            matched_ids = [lesson["id"] for lesson, _ in matched]
            self.db.execute("""
                INSERT INTO experience_retrievals
                (id, team_id, caller_key, caller_session_id, target_goal_id, query,
                 visible_lesson_ids, matched_lesson_ids, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                retrieval_id, team_id, caller.key, caller.session_id, body.target_goal_id,
                body.query, _encode(visible_ids), _encode(matched_ids), created_at,
            ))
            receipt = {
                "retrieval_id": retrieval_id,
                "target_goal_id": body.target_goal_id,
                "query": body.query,
                "lessons": [self._lesson_view(caller, lesson, handoff) for lesson, handoff in matched],
                "visible_lesson_ids": visible_ids,
                "created_at": created_at,
            }
            self._save_receipt(team_id, caller.key, "retrieve_lessons", body.request_id, digest, receipt)
            return receipt

    def _project_retrieval(self, team_id, caller, retrieval_id):
        row = self.db.execute("""
            SELECT * FROM experience_retrievals
            WHERE team_id = ? AND id = ? AND caller_key = ?
        """, (team_id, retrieval_id, caller.key)).fetchone()
        if row is None:
            raise TeamNotFound("Lesson retrieval not found")
        candidates = {
            lesson["id"]: (lesson, handoff)
            for lesson, handoff in self._candidates(caller, row["target_goal_id"])
        }
        matched_ids = json.loads(row["matched_lesson_ids"])
        lessons = [
            self._lesson_view(caller, *candidates[lesson_id])
            for lesson_id in matched_ids if lesson_id in candidates
        ]
        return {
            "retrieval_id": row["id"],
            "target_goal_id": row["target_goal_id"],
            "query": row["query"],
            "lessons": lessons,
            "visible_lesson_ids": [
                lesson_id for lesson_id in json.loads(row["visible_lesson_ids"]) if lesson_id in candidates
            ],
            "created_at": row["created_at"],
        }

    def get_retrieval(self, team_id, caller_session_id, retrieval_id):
        caller = self._caller(team_id, caller_session_id)
        return self._project_retrieval(team_id, caller, retrieval_id)

    def get_handoff(self, team_id, caller_session_id, handoff_id):
        caller = self._caller(team_id, caller_session_id)
        return self._handoff_view(caller, self._handoff_row(team_id, handoff_id))

    def record_lesson_use(self, team_id, caller_session_id, retrieval_id, body: RecordLessonUseInput):
        if not isinstance(body, RecordLessonUseInput):
            body = RecordLessonUseInput.model_validate(body)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            caller = self._caller(team_id, caller_session_id)
            self._require_member(caller)
            digest, replay = self._replay(
                team_id, caller.key, "record_lesson_use", body, scope={"retrieval_id": retrieval_id},
            )
            if replay is not None:
                retrieval = self.db.execute("""
                    SELECT * FROM experience_retrievals
                    WHERE team_id = ? AND id = ? AND caller_key = ?
                """, (team_id, retrieval_id, caller.key)).fetchone()
                if retrieval is None or body.lesson_id not in json.loads(retrieval["matched_lesson_ids"]):
                    raise TeamNotFound("Lesson not found")
                current = {
                    lesson["id"] for lesson, _ in self._candidates(caller, retrieval["target_goal_id"])
                }
                if body.lesson_id not in current:
                    raise TeamNotFound("Lesson not found")
                return replay
            retrieval = self.db.execute("""
                SELECT * FROM experience_retrievals
                WHERE team_id = ? AND id = ? AND caller_key = ?
            """, (team_id, retrieval_id, caller.key)).fetchone()
            if retrieval is None:
                raise TeamNotFound("Lesson retrieval not found")
            if body.lesson_id not in json.loads(retrieval["matched_lesson_ids"]):
                raise CollaborationInvalid("The selected lesson was not returned by this retrieval")
            self._require_active_adoption(team_id, retrieval["target_goal_id"], caller)
            candidates = {
                lesson["id"]: (lesson, handoff)
                for lesson, handoff in self._candidates(caller, retrieval["target_goal_id"])
            }
            if body.lesson_id not in candidates:
                raise TeamNotFound("Lesson not found")
            for source in body.sources:
                self._check_source(caller, source.model_dump())
            use_id = uuid4().hex
            created_at = _now()
            self.db.execute("""
                INSERT INTO experience_uses
                (id, team_id, retrieval_id, lesson_id, target_goal_id, actor_id,
                 actor_session_id, action, evidence, sources, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                use_id, team_id, retrieval_id, body.lesson_id, retrieval["target_goal_id"],
                caller.member_id, caller.session_id, body.action,
                _encode(body.evidence), _encode([source.model_dump() for source in body.sources]), created_at,
            ))
            receipt = {
                "use_id": use_id,
                "retrieval_id": retrieval_id,
                "lesson_id": body.lesson_id,
                "target_goal_id": retrieval["target_goal_id"],
                "action": body.action,
                "created_at": created_at,
            }
            self._save_receipt(team_id, caller.key, "record_lesson_use", body.request_id, digest, receipt)
            return receipt

    def observe_lesson(self, team_id, caller_session_id, use_id, body: ObserveLessonInput):
        if not isinstance(body, ObserveLessonInput):
            body = ObserveLessonInput.model_validate(body)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            caller = self._caller(team_id, caller_session_id)
            self._require_member(caller)
            digest, replay = self._replay(
                team_id, caller.key, "observe_lesson", body, scope={"use_id": use_id},
            )
            if replay is not None:
                use = self.db.execute("""
                    SELECT * FROM experience_uses
                    WHERE team_id = ? AND id = ? AND actor_session_id = ?
                """, (team_id, use_id, caller.session_id)).fetchone()
                if use is None:
                    raise TeamNotFound("Lesson use not found")
                current = {
                    lesson["id"] for lesson, _ in self._candidates(caller, use["target_goal_id"])
                }
                if use["lesson_id"] not in current:
                    raise TeamNotFound("Lesson not found")
                return replay
            use = self.db.execute("""
                SELECT * FROM experience_uses
                WHERE team_id = ? AND id = ? AND actor_session_id = ?
            """, (team_id, use_id, caller.session_id)).fetchone()
            if use is None:
                raise TeamNotFound("Lesson use not found")
            self._require_active_adoption(team_id, use["target_goal_id"], caller)
            candidates = {
                lesson["id"]: (lesson, handoff)
                for lesson, handoff in self._candidates(caller, use["target_goal_id"])
            }
            if use["lesson_id"] not in candidates:
                raise TeamNotFound("Lesson not found")
            for source in body.sources:
                self._check_source(caller, source.model_dump())
            observation_id = uuid4().hex
            created_at = _now()
            self.db.execute("""
                INSERT INTO experience_observations
                (id, team_id, use_id, actor_id, actor_session_id, outcome, note,
                 evidence, sources, recheck_items, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                observation_id, team_id, use_id, caller.member_id, caller.session_id,
                body.outcome, body.note, _encode(body.evidence),
                _encode([source.model_dump() for source in body.sources]),
                _encode(body.recheck_items), created_at,
            ))
            receipt = {
                "observation_id": observation_id,
                "use_id": use_id,
                "outcome": body.outcome,
                "created_at": created_at,
            }
            self._save_receipt(team_id, caller.key, "observe_lesson", body.request_id, digest, receipt)
            return receipt

    def get_lesson(self, team_id, caller_session_id, lesson_id, target_goal_id=None):
        caller = self._caller(team_id, caller_session_id)
        lesson = self._lesson_row(team_id, lesson_id)
        if caller.kind == "member" and lesson["owner_id"] == caller.member_id:
            return self._lesson_view(caller, lesson)
        if target_goal_id is None:
            raise TeamNotFound("Lesson not found")
        self._goal(team_id, target_goal_id, caller)
        candidates = {
            item["id"]: (item, handoff)
            for item, handoff in self._candidates(caller, target_goal_id)
        }
        if lesson_id not in candidates:
            raise TeamNotFound("Lesson not found")
        return self._lesson_view(caller, *candidates[lesson_id])

    def experience_snapshot(self, team_id, caller_session_id, target_goal_id):
        caller = self._caller(team_id, caller_session_id)
        self._goal(team_id, target_goal_id, caller)
        candidates = self._candidates(caller, target_goal_id)
        retrievals = [
            {
                "retrieval_id": row["id"],
                "query": row["query"],
                "matched_lesson_ids": json.loads(row["matched_lesson_ids"]),
                "created_at": row["created_at"],
            }
            for row in self.db.execute("""
                SELECT * FROM experience_retrievals
                WHERE team_id = ? AND caller_key = ? AND target_goal_id = ?
                ORDER BY rowid
            """, (team_id, caller.key, target_goal_id))
        ]
        uses = [
            {
                "use_id": row["id"],
                "retrieval_id": row["retrieval_id"],
                "lesson_id": row["lesson_id"],
                "action": row["action"],
                "evidence": json.loads(row["evidence"]),
                "sources": json.loads(row["sources"]),
                "created_at": row["created_at"],
            }
            for row in self.db.execute("""
                SELECT * FROM experience_uses
                WHERE team_id = ? AND actor_session_id = ? AND target_goal_id = ?
                ORDER BY rowid
            """, (team_id, caller.session_id, target_goal_id))
        ]
        observations = [
            {
                "observation_id": row["id"],
                "use_id": row["use_id"],
                "outcome": row["outcome"],
                "note": row["note"],
                "evidence": json.loads(row["evidence"]),
                "sources": json.loads(row["sources"]),
                "recheck_items": json.loads(row["recheck_items"]),
                "created_at": row["created_at"],
            }
            for row in self.db.execute("""
                SELECT o.* FROM experience_observations o JOIN experience_uses u ON u.id = o.use_id
                WHERE o.team_id = ? AND o.actor_session_id = ? AND u.target_goal_id = ?
                ORDER BY o.rowid
            """, (team_id, caller.session_id, target_goal_id))
        ]
        handoffs = []
        for row in self.db.execute("""
            SELECT * FROM experience_handoffs
            WHERE team_id = ? AND target_goal_id = ?
              AND (sender_session_id = ? OR recipient_session_id = ?)
            ORDER BY rowid
        """, (team_id, target_goal_id, caller.session_id, caller.session_id)):
            try:
                handoffs.append(self._handoff_view(caller, row))
            except (TeamNotFound, ArtifactNotFound):
                continue
        return {
            "target_goal_id": target_goal_id,
            "lessons": [self._lesson_view(caller, lesson, handoff) for lesson, handoff in candidates],
            "retrievals": retrievals,
            "uses": uses,
            "observations": observations,
            "handoffs": handoffs,
            "counts": {
                "lessons": len(candidates),
                "retrievals": len(retrievals),
                "uses": len(uses),
                "observations": len(observations),
                "handoffs": len(handoffs),
            },
        }


class ExperienceConflict(TeamConflict):
    pass


class RecordLessonUseTool(RecordLessonUseInput):
    retrieval_id: ObjectId


class ObserveLessonTool(ObserveLessonInput):
    use_id: ObjectId


class ExperienceTools:
    """Model-tool adapter for the same service used by HTTP."""

    READ_TOOLS = {
        "retrieve_lessons": (RetrieveLessonsInput, "Retrieve only lessons owned by you or explicitly handed "
                                                  "off to this execution identity and target goal."),
        "read_lesson": (ReadLessonTool, "Read one lesson by ID when it is owned by you or explicitly handed off."),
        "read_handoff": (ReadHandoffTool, "Read one handoff addressed to you or sent by you, including only "
                                         "artifact versions that remain readable."),
    }
    MEMBER_TOOLS = {
        **READ_TOOLS,
        "record_lesson": (RecordLessonInput, "Record a personal lesson with evidence references, applicability "
                                            "and invalidation conditions."),
        "handoff_lessons": (HandoffLessonsInput, "Hand off owned lessons and/or authorized artifact versions to "
                                                "one member or assistant for a target goal."),
        "record_lesson_use": (RecordLessonUseTool, "Record a concrete action after a retrieval. This is distinct "
                                                  "from the retrieval and from the observation."),
        "observe_lesson": (ObserveLessonTool, "Record whether the adopted lesson changed behavior, produced no "
                                              "change, or was invalidated. Cite evidence."),
        "get_experience": (ExperienceGoalTool, "Read your lessons and your retrieval/use/observation trail for a "
                                              "target goal."),
    }

    def __init__(self, service, auth, workspace):
        self.service = service
        self.auth = auth
        self.workspace = workspace

    @staticmethod
    def handles(name):
        return name in ExperienceTools.MEMBER_TOOLS

    def _member(self, session_id):
        return self.auth.member(session_id)

    def _assistant(self, session_id):
        if self.service.forks is None:
            return None
        try:
            actor = self.workspace.actor(session_id)
            fork = self.service.forks.require_active(session_id)
        except (WorkspaceNotFound, TeamConflict, TeamNotFound):
            return None
        return actor, fork

    def tools_for(self, session_id):
        member = self._member(session_id)
        if member is not None:
            available = self.MEMBER_TOOLS
        else:
            assistant = self._assistant(session_id)
            if assistant is None:
                return []
            _, fork = assistant
            available = {
                name: item for name, item in self.READ_TOOLS.items()
                if name in self.service.assistant_read_tools and name in fork["allowed_tools"]
            }
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": model.model_json_schema(),
                },
            }
            for name, (model, description) in available.items()
        ]

    def execute(self, session_id, name, raw_arguments):
        if name not in self.MEMBER_TOOLS:
            return {"ok": False, "error": "Unknown experience tool", "status": 422}
        member = self._member(session_id)
        if member is None:
            assistant = self._assistant(session_id)
            if (
                assistant is None
                or name not in self.READ_TOOLS
                or name not in self.service.assistant_read_tools
                or name not in assistant[1]["allowed_tools"]
            ):
                return {"ok": False, "error": "This execution identity cannot use the experience tool", "status": 403}
            actor = assistant[0]
        else:
            actor = member
        try:
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError
            if name == "read_lesson":
                body = ReadLessonTool.model_validate(arguments)
                result = self.service.get_lesson(
                    actor.team_id, session_id, body.lesson_id, body.target_goal_id,
                )
            elif name == "read_handoff":
                body = ReadHandoffTool.model_validate(arguments)
                result = self.service.get_handoff(
                    actor.team_id, session_id, body.handoff_id,
                )
            elif name == "get_experience":
                body = ExperienceGoalTool.model_validate(arguments)
                result = self.service.experience_snapshot(
                    actor.team_id, session_id, body.target_goal_id,
                )
            else:
                model = self.MEMBER_TOOLS[name][0]
                body = model.model_validate(arguments)
                if name == "record_lesson":
                    result = self.service.record_lesson(actor.team_id, session_id, body)
                elif name == "retrieve_lessons":
                    result = self.service.retrieve_lessons(actor.team_id, session_id, body)
                elif name == "handoff_lessons":
                    result = self.service.handoff_lessons(actor.team_id, session_id, body)
                elif name == "record_lesson_use":
                    command = RecordLessonUseInput.model_validate(
                        body.model_dump(exclude={"retrieval_id"}),
                    )
                    result = self.service.record_lesson_use(actor.team_id, session_id, body.retrieval_id, command)
                elif name == "observe_lesson":
                    command = ObserveLessonInput.model_validate(
                        body.model_dump(exclude={"use_id"}),
                    )
                    result = self.service.observe_lesson(actor.team_id, session_id, body.use_id, command)
                else:
                    raise CollaborationInvalid("Unknown experience tool")
            return {"ok": True, "result": result}
        except (KeyError, TypeError, ValueError, ValidationError):
            return {"ok": False, "error": "Invalid experience tool arguments", "status": 422}
        except (TeamNotFound, ArtifactNotFound, ArtifactError, WorkspaceNotFound) as exc:
            return {"ok": False, "error": str(exc), "status": 404}
        except ExperienceConflict as exc:
            return {"ok": False, "error": str(exc), "status": 409}
        except TeamConflict as exc:
            return {"ok": False, "error": str(exc), "status": 409}
        except CollaborationForbidden as exc:
            return {"ok": False, "error": str(exc), "status": 403}
        except CollaborationInvalid as exc:
            return {"ok": False, "error": str(exc), "status": 422}


def _service(request: Request) -> ExperienceService:
    service = getattr(request.app.state, "experience", None)
    if service is None:
        raise HTTPException(
            status_code=503, detail="Personal experience service is not assembled",
            headers={"Cache-Control": "no-store"},
        )
    return service


def _error(exc):
    headers = {"Cache-Control": "no-store"}
    if isinstance(exc, ExperienceConflict):
        return HTTPException(status_code=409, detail=str(exc), headers=headers)
    if isinstance(exc, TeamConflict):
        return HTTPException(status_code=409, detail=str(exc), headers=headers)
    if isinstance(exc, CollaborationForbidden):
        return HTTPException(status_code=403, detail=str(exc), headers=headers)
    if isinstance(exc, CollaborationInvalid):
        return HTTPException(status_code=422, detail=str(exc), headers=headers)
    if isinstance(exc, (TeamNotFound, ArtifactNotFound)):
        return HTTPException(status_code=404, detail=str(exc), headers=headers)
    if isinstance(exc, ArtifactError):
        return HTTPException(status_code=404, detail=str(exc), headers=headers)
    return exc


def build_router(authenticated):
    """Build the T012 router on top of the application's existing auth boundary."""
    api_router = APIRouter()

    async def member_actor(actor: Principal = Depends(authenticated)) -> Principal:
        if actor.is_operator or actor.session_id is None:
            raise HTTPException(
                status_code=403, detail="This action requires a team member",
                headers={"Cache-Control": "no-store"},
            )
        return actor

    @api_router.post("/v1/teams/{team_id}/lessons", status_code=201)
    async def record_lesson(
        team_id: str, body: RecordLessonInput, request: Request,
        actor: Principal = Depends(member_actor),
    ):
        try:
            return _service(request).record_lesson(team_id, actor.session_id, body)
        except Exception as exc:
            raise _error(exc) from exc

    @api_router.post("/v1/teams/{team_id}/lessons/search", status_code=201)
    async def retrieve_lessons(
        team_id: str, body: RetrieveLessonsInput, request: Request,
        actor: Principal = Depends(member_actor),
    ):
        try:
            return _service(request).retrieve_lessons(team_id, actor.session_id, body)
        except Exception as exc:
            raise _error(exc) from exc

    @api_router.get("/v1/teams/{team_id}/lessons/{lesson_id}")
    async def read_lesson(
        team_id: str, lesson_id: str, request: Request,
        actor: Principal = Depends(member_actor),
        target_goal_id: Annotated[str | None, Query()] = None,
    ):
        try:
            return _service(request).get_lesson(team_id, actor.session_id, lesson_id, target_goal_id)
        except Exception as exc:
            raise _error(exc) from exc

    @api_router.post("/v1/teams/{team_id}/lesson-handoffs", status_code=201)
    async def handoff_lessons(
        team_id: str, body: HandoffLessonsInput, request: Request,
        actor: Principal = Depends(member_actor),
    ):
        try:
            return _service(request).handoff_lessons(team_id, actor.session_id, body)
        except Exception as exc:
            raise _error(exc) from exc

    @api_router.get("/v1/teams/{team_id}/lesson-handoffs/{handoff_id}")
    async def read_handoff(
        team_id: str, handoff_id: str, request: Request,
        actor: Principal = Depends(member_actor),
    ):
        try:
            return _service(request).get_handoff(team_id, actor.session_id, handoff_id)
        except Exception as exc:
            raise _error(exc) from exc

    @api_router.get("/v1/teams/{team_id}/lesson-retrievals/{retrieval_id}")
    async def read_retrieval(
        team_id: str, retrieval_id: str, request: Request,
        actor: Principal = Depends(member_actor),
    ):
        try:
            return _service(request).get_retrieval(team_id, actor.session_id, retrieval_id)
        except Exception as exc:
            raise _error(exc) from exc

    @api_router.post("/v1/teams/{team_id}/lesson-retrievals/{retrieval_id}/uses", status_code=201)
    async def record_lesson_use(
        team_id: str, retrieval_id: str, body: RecordLessonUseInput, request: Request,
        actor: Principal = Depends(member_actor),
    ):
        try:
            return _service(request).record_lesson_use(team_id, actor.session_id, retrieval_id, body)
        except Exception as exc:
            raise _error(exc) from exc

    @api_router.post("/v1/teams/{team_id}/lesson-uses/{use_id}/observations", status_code=201)
    async def observe_lesson(
        team_id: str, use_id: str, body: ObserveLessonInput, request: Request,
        actor: Principal = Depends(member_actor),
    ):
        try:
            return _service(request).observe_lesson(team_id, actor.session_id, use_id, body)
        except Exception as exc:
            raise _error(exc) from exc

    @api_router.get("/v1/teams/{team_id}/experience")
    async def read_experience(
        team_id: str, request: Request,
        target_goal_id: Annotated[str, Query()],
        actor: Principal = Depends(member_actor),
    ):
        try:
            return _service(request).experience_snapshot(team_id, actor.session_id, target_goal_id)
        except Exception as exc:
            raise _error(exc) from exc

    return api_router
