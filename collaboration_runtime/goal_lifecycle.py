"""Goal acceptance revisions, terminal facts, scope closure and queue activation."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import uuid4

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
from .local_auth import OPERATOR_ID, Principal
from .teams import TeamConflict, TeamNotFound


Version = Annotated[int, Field(ge=1, strict=True)]
Evidence = Annotated[list[Text], Field(min_length=1, max_length=64)]
Notes = Annotated[list[Text], Field(max_length=64)]
Versions = Annotated[list[ObjectId], Field(max_length=100)]


class RequiredDeliveryInput(Command):
    name: Text
    task_id: ObjectId


class AcceptanceUpdateInput(Command):
    request_id: RequestId
    expected_revision: Version
    required_deliveries: Annotated[list[RequiredDeliveryInput], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique_tasks(self):
        task_ids = [item.task_id for item in self.required_deliveries]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("Each required delivery must name a distinct task")
        return self


class TerminalInput(Command):
    request_id: RequestId
    expected_revision: Version
    outcome: Literal["completed", "failed", "cancelled"]
    reason: Text | None = None
    evidence: Notes = Field(default_factory=list)
    final_version_ids: Versions = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_outcome(self):
        if self.outcome == "completed" and not self.final_version_ids:
            raise ValueError("A completed goal requires readable final versions")
        if self.outcome == "failed" and (self.reason is None or not self.evidence):
            raise ValueError("A failed goal requires a reason and evidence")
        return self


class CloseGoalTool(TerminalInput):
    goal_id: ObjectId
    outcome: Literal["completed", "failed"]
    reason: Text | None = None
    evidence: Notes = Field(default_factory=list)


class UpdateGoalAcceptanceTool(AcceptanceUpdateInput):
    goal_id: ObjectId


class ActivateGoalInput(Command):
    request_id: RequestId
    expected_revision: Version


class ActivateGoalTool(ActivateGoalInput):
    goal_id: ObjectId


class PreRegistrationTerminationInput(Command):
    request_id: RequestId
    reason: Text | None = None


class HostTerminalEventInput(Command):
    event_key: RequestId
    expected_revision: Version
    outcome: Literal["failed", "timed_out"]
    reason: Text
    evidence: Evidence


class GoalTool(Command):
    goal_id: ObjectId


TOOLS = {
    "get_goal": (GoalTool, "Read the current goal acceptance agreement, terminal fact and frozen evidence."),
    "update_goal_acceptance": (
        UpdateGoalAcceptanceTool,
        "As Lead, publish a new required-delivery revision for an active goal.",
    ),
    "close_goal": (
        CloseGoalTool,
        "As Lead, complete a goal after all required current reviews and blockers pass, or fail it with evidence.",
    ),
    "activate_goal": (
        ActivateGoalTool,
        "As Lead, atomically activate the selected queued goal when it is the earliest eligible one.",
    ),
}


class GoalLifecycle:
    def __init__(self, collaboration, delegation, runtime, *, goal_timeout_seconds=None):
        if goal_timeout_seconds is not None and goal_timeout_seconds <= 0:
            raise ValueError("goal_timeout_seconds must be positive")
        self.collaboration = collaboration
        self.delegation = delegation
        self.runtime = runtime
        self.goal_timeout_seconds = goal_timeout_seconds
        self.db = collaboration.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS goal_acceptances (
                goal_id TEXT NOT NULL REFERENCES team_goals(id),
                revision INTEGER NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (goal_id, revision)
            );
            CREATE TABLE IF NOT EXISTS goal_prerequisites (
                goal_id TEXT NOT NULL REFERENCES team_goals(id),
                prerequisite_goal_id TEXT NOT NULL REFERENCES team_goals(id),
                PRIMARY KEY (goal_id, prerequisite_goal_id),
                CHECK (goal_id <> prerequisite_goal_id)
            );
            CREATE TABLE IF NOT EXISTS goal_deadlines (
                goal_id TEXT PRIMARY KEY REFERENCES team_goals(id),
                deadline_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'host_policy',
                configured_seconds REAL
            );
            CREATE TABLE IF NOT EXISTS goal_terminal_facts (
                terminal_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                goal_id TEXT UNIQUE REFERENCES team_goals(id),
                source_message_id TEXT NOT NULL UNIQUE REFERENCES team_messages(id),
                outcome TEXT NOT NULL CHECK (outcome IN ('completed', 'failed', 'timed_out', 'cancelled')),
                actor_id TEXT NOT NULL,
                actor_kind TEXT NOT NULL CHECK (actor_kind IN ('lead', 'user', 'host')),
                reason TEXT NOT NULL,
                evidence TEXT NOT NULL,
                frozen TEXT NOT NULL,
                server_time TEXT NOT NULL,
                event_id INTEGER NOT NULL REFERENCES collaboration_events(seq),
                host_event_key TEXT
            );
            CREATE TABLE IF NOT EXISTS goal_final_versions (
                terminal_id TEXT NOT NULL REFERENCES goal_terminal_facts(terminal_id),
                goal_id TEXT NOT NULL REFERENCES team_goals(id),
                version_id TEXT NOT NULL REFERENCES artifact_versions(version_id),
                PRIMARY KEY (terminal_id, version_id)
            );
            CREATE INDEX IF NOT EXISTS goal_final_versions_version ON goal_final_versions(version_id);
        """)
        terminal_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(goal_terminal_facts)")
        }
        if "host_event_key" not in terminal_columns:
            self.db.execute("ALTER TABLE goal_terminal_facts ADD COLUMN host_event_key TEXT")
        deadline_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(goal_deadlines)")
        }
        if "source" not in deadline_columns:
            self.db.execute("ALTER TABLE goal_deadlines ADD COLUMN source TEXT NOT NULL DEFAULT 'host_policy'")
        if "configured_seconds" not in deadline_columns:
            self.db.execute("ALTER TABLE goal_deadlines ADD COLUMN configured_seconds REAL")
        self.db.execute("DROP INDEX IF EXISTS goal_terminal_host_event")
        self.db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS goal_terminal_host_event
            ON goal_terminal_facts(team_id, host_event_key) WHERE host_event_key IS NOT NULL
        """)
        for row in self.db.execute("SELECT id, revision FROM team_goals"):
            if self.db.execute(
                "SELECT 1 FROM goal_acceptances WHERE goal_id = ? AND revision = ?",
                (row["id"], row["revision"]),
            ).fetchone() is None:
                self.db.execute(
                    "INSERT INTO goal_acceptances VALUES (?, ?, ?)",
                    (row["id"], row["revision"], json.dumps({
                        "required_deliveries": [],
                        "created_at": _now(),
                        "created_by": "migration",
                    })),
                )

    @staticmethod
    def _decode(row, field):
        if row is None:
            return None
        value = dict(row)
        value[field] = json.loads(value[field])
        return value

    def _goal_row(self, team_id, actor, goal_id):
        row = self.db.execute(
            "SELECT * FROM team_goals WHERE team_id = ? AND id = ?", (team_id, goal_id),
        ).fetchone()
        if row is None:
            raise TeamNotFound("Goal not found")
        self.collaboration._message_for(team_id, actor, row["source_message_id"])
        return row

    def _goal_by_source(self, team_id, actor, source_message_id):
        source = self.collaboration._message_for(team_id, actor, source_message_id)
        row = self.db.execute(
            "SELECT * FROM team_goals WHERE team_id = ? AND source_message_id = ?",
            (team_id, source["id"]),
        ).fetchone()
        return row, source

    def _lead_principal(self, team):
        lead = next(member for member in team["members"] if member["member_id"] == team["lead_member_id"])
        return Principal(lead["member_id"], team["team_id"], lead["session_id"])

    @staticmethod
    def _is_lead(team, actor):
        return actor.id == team["lead_member_id"]

    def _is_full_reader(self, team, actor):
        return actor.is_operator or self._is_lead(team, actor)

    def _acceptance(self, goal_id, revision=None):
        row = self.db.execute("""
            SELECT a.revision, a.data FROM goal_acceptances a
            WHERE a.goal_id = ? AND a.revision = COALESCE(
                ?, (SELECT revision FROM team_goals WHERE id = ?)
            )
        """, (goal_id, revision, goal_id)).fetchone()
        if row is None:
            return None
        return {"revision": row["revision"], **json.loads(row["data"])}

    def _prerequisites(self, goal_id):
        return [row["prerequisite_goal_id"] for row in self.db.execute("""
            SELECT prerequisite_goal_id FROM goal_prerequisites
            WHERE goal_id = ? ORDER BY rowid
        """, (goal_id,))]

    def _deadline(self, goal_id):
        row = self.db.execute(
            "SELECT deadline_at FROM goal_deadlines WHERE goal_id = ?", (goal_id,),
        ).fetchone()
        return row["deadline_at"] if row is not None else None

    def _terminal(self, *, goal_id=None, source_message_id=None):
        if goal_id is not None:
            row = self.db.execute(
                "SELECT * FROM goal_terminal_facts WHERE goal_id = ?", (goal_id,),
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT * FROM goal_terminal_facts WHERE source_message_id = ?", (source_message_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["evidence"] = json.loads(result["evidence"])
        result["frozen"] = json.loads(result["frozen"])
        return result

    def _compact_terminal(self, terminal):
        if terminal is None:
            return None
        return {key: terminal[key] for key in
                ("terminal_id", "goal_id", "source_message_id", "outcome", "actor_kind",
                 "reason", "server_time")}

    def _can_read_task_acceptance(self, team_id, actor, task_id, acceptance):
        if acceptance is None:
            return False
        try:
            self.delegation._visible_task(team_id, actor, task_id)
        except TeamNotFound:
            return False
        return self.delegation.results._can_read_versions(team_id, actor, acceptance)

    def _can_read_final_version(self, team_id, actor, version_id):
        if actor.is_operator:
            return self.operator_can_read_version(version_id)
        if actor.session_id is None:
            return False
        try:
            return actor.session_id in self.delegation.artifacts.reference_readers(team_id, version_id)
        except ArtifactNotFound:
            return False

    def _project_frozen(self, team_id, actor, frozen, outcome):
        authorized_acceptances = {}
        required = []
        for item in frozen["required_deliveries"]:
            projected = {key: item[key] for key in ("name", "task_id", "accepted")}
            acceptance = item.get("acceptance")
            if self._can_read_task_acceptance(team_id, actor, item["task_id"], acceptance):
                projected.update({
                    "acceptance": acceptance,
                    "versions": item.get("versions", []),
                    "submission_id": item.get("submission_id"),
                    "review_id": item.get("review_id"),
                })
            required.append(projected)
        for task_id, acceptance in frozen.get("task_acceptance", {}).items():
            if self._can_read_task_acceptance(team_id, actor, task_id, acceptance):
                authorized_acceptances[task_id] = acceptance
        final_versions = []
        if outcome == "completed":
            final_versions = [
                version for version in frozen.get("final_versions", [])
                if self._can_read_final_version(team_id, actor, version["version_id"])
            ]
        return {
            "metric_version": frozen["metric_version"],
            "goal_id": frozen["goal_id"],
            "source_message_id": frozen["source_message_id"],
            "acceptance_revision": frozen["acceptance_revision"],
            "acceptance_required_deliveries": [
                {key: item[key] for key in ("name", "task_id")}
                for item in frozen.get("acceptance_required_deliveries", [])
            ],
            "required_deliveries": required,
            "effective_task_ids": frozen["effective_task_ids"],
            "accepted_task_ids": frozen["accepted_task_ids"],
            "task_acceptance": authorized_acceptances,
            "final_versions": final_versions,
            "open_question_ids": frozen["open_question_ids"],
            "closed_proposal_ids": frozen["closed_proposal_ids"],
            "evidence": frozen["evidence"],
            "raw_counts": frozen["raw_counts"],
        }

    def _terminal_view(self, team, actor, terminal):
        if terminal is None:
            return None
        if not self._is_full_reader(team, actor):
            return self._compact_terminal(terminal)
        return {
            **terminal,
            "frozen": self._project_frozen(team["team_id"], actor, terminal["frozen"], terminal["outcome"]),
        }

    def enrich_goal(self, team_id, actor, goal):
        acceptance = self._acceptance(goal["id"])
        terminal = self._terminal(goal_id=goal["id"])
        team = self.collaboration.teams.get(team_id)
        return {
            **goal,
            "acceptance": acceptance,
            "prerequisites": self._prerequisites(goal["id"]),
            "deadline_at": self._deadline(goal["id"]),
            "terminal": self._terminal_view(team, actor, terminal),
        }

    def get_goal(self, team_id, actor, goal_id):
        self.collaboration.team(team_id, actor)
        row = self._goal_row(team_id, actor, goal_id)
        enriched = self.enrich_goal(team_id, actor, dict(row))
        return {
            "goal": {key: value for key, value in enriched.items()
                     if key not in {"acceptance", "prerequisites", "deadline_at", "terminal"}},
            "acceptance": enriched["acceptance"],
            "prerequisites": enriched["prerequisites"],
            "deadline_at": enriched["deadline_at"],
            "terminal": enriched["terminal"],
        }

    def get_acceptance(self, team_id, actor, goal_id, revision=None):
        self.collaboration.team(team_id, actor)
        self._goal_row(team_id, actor, goal_id)
        acceptance = self._acceptance(goal_id, revision)
        if acceptance is None:
            raise TeamNotFound("Goal acceptance revision not found")
        return acceptance

    def assignment_lifecycle(self, team_id, actor, source_message_id):
        team = self.collaboration.team(team_id, actor)
        row, source = self._goal_by_source(team_id, actor, source_message_id)
        terminal = self._terminal(source_message_id=source["id"])
        if row is not None:
            goal = self.enrich_goal(team_id, actor, dict(row))
        else:
            goal = None
        return {"goal": goal, "terminal": self._terminal_view(team, actor, terminal)}

    def initialize_goal(self, team_id, goal_id, actor_id):
        record = {"required_deliveries": [], "created_at": _now(), "created_by": actor_id}
        self.db.execute(
            "INSERT INTO goal_acceptances VALUES (?, 1, ?)",
            (goal_id, json.dumps(record, ensure_ascii=False)),
        )
        if self.goal_timeout_seconds is not None:
            deadline_at = (
                datetime.now(timezone.utc) + timedelta(seconds=self.goal_timeout_seconds)
            ).isoformat()
            self.db.execute(
                """INSERT INTO goal_deadlines
                   (goal_id, deadline_at, source, configured_seconds) VALUES (?, ?, ?, ?)""",
                (goal_id, deadline_at, "host_policy", self.goal_timeout_seconds),
            )

    def terminal_for_source(self, team_id, source_message_id):
        return self._terminal(source_message_id=source_message_id)

    def question_scope_ended(self, question):
        if question["goal_id"] is not None:
            return self._terminal(goal_id=question["goal_id"]) is not None
        return self._terminal(source_message_id=question["source_message_id"]) is not None

    def validate_prerequisites(self, team_id, actor, goal_ids):
        normalized = list(dict.fromkeys(goal_ids))
        for prerequisite_id in normalized:
            row = self.db.execute(
                "SELECT source_message_id FROM team_goals WHERE team_id = ? AND id = ?",
                (team_id, prerequisite_id),
            ).fetchone()
            if row is None:
                raise TeamNotFound("Prerequisite goal not found")
            self.collaboration._message_for(team_id, actor, row["source_message_id"])
        return normalized

    def store_prerequisites(self, goal_id, prerequisite_ids):
        self.db.executemany(
            "INSERT INTO goal_prerequisites(goal_id, prerequisite_goal_id) VALUES (?, ?)",
            [(goal_id, prerequisite_id) for prerequisite_id in prerequisite_ids],
        )

    def prerequisites_completed(self, goal_id):
        rows = self.db.execute("""
            SELECT g.status FROM goal_prerequisites p JOIN team_goals g ON g.id = p.prerequisite_goal_id
            WHERE p.goal_id = ?
        """, (goal_id,))
        return all(row["status"] == "completed" for row in rows)

    def update_acceptance(self, team_id, actor, goal_id, body: AcceptanceUpdateInput):
        key = (team_id, actor.id, "update_goal_acceptance", body.request_id)
        payload = body.model_dump(exclude={"request_id"})
        digest = self.collaboration._digest({"goal_id": goal_id, **payload})
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            team = self.collaboration.team(team_id, actor)
            row = self._goal_row(team_id, actor, goal_id)
            if not self._is_lead(team, actor):
                raise CollaborationForbidden("Only the Lead can revise goal acceptance")
            replay = self.collaboration._replay(key, digest)
            if replay is not None:
                return replay
            if row["status"] != "active":
                raise TeamConflict("Only an active goal can revise its acceptance agreement")
            if body.expected_revision != row["revision"]:
                raise TeamConflict("Goal acceptance revision changed")
            for item in body.required_deliveries:
                try:
                    task = self.delegation._visible_task(team_id, actor, item.task_id)
                except TeamNotFound as exc:
                    raise CollaborationInvalid("A required delivery task is not visible to the Lead") from exc
                if task["goal_id"] != goal_id:
                    raise CollaborationInvalid("A required delivery must belong to this goal")
            revision = row["revision"] + 1
            record = {
                "required_deliveries": [item.model_dump() for item in body.required_deliveries],
                "created_at": _now(),
                "created_by": actor.id,
            }
            self.db.execute("INSERT INTO goal_acceptances VALUES (?, ?, ?)",
                            (goal_id, revision, json.dumps(record, ensure_ascii=False)))
            self.db.execute("UPDATE team_goals SET revision = ? WHERE id = ?", (revision, goal_id))
            event_id = self.collaboration._event(team_id, "goal.acceptance_updated", goal_id)
            receipt = {"goal_id": goal_id, "revision": revision, "event_id": event_id,
                       "required_deliveries": len(body.required_deliveries)}
            self.collaboration._save_receipt(key, digest, receipt)
            return receipt

    def _version(self, team_id, version_id):
        try:
            return self.delegation.artifacts.reference(team_id, version_id)
        except ArtifactNotFound as exc:
            raise TeamNotFound("Final version not found") from exc

    @staticmethod
    def _version_view(version):
        return {**{key: version[key] for key in
                   ("version_id", "artifact_id", "version", "source_path", "sha256")},
                "download_url": f"/v1/artifacts/{version['artifact_id']}/versions/{version['version']}"}

    def _thread_message_ids(self, team_id, seeds):
        message_ids = list(dict.fromkeys(seeds))
        seen = set(message_ids)
        frontier = message_ids
        while frontier:
            placeholders = ",".join("?" for _ in frontier)
            children = [
                row["id"] for row in self.db.execute(f"""
                    SELECT id FROM team_messages
                    WHERE team_id = ? AND reply_to IN ({placeholders})
                    ORDER BY seq
                """, (team_id, *frontier))
            ]
            frontier = [message_id for message_id in children if message_id not in seen]
            seen.update(frontier)
            message_ids.extend(frontier)
        return message_ids

    def _scope_objects(self, team_id, goal_id, source_message_id):
        questions = [dict(row) for row in self.db.execute("""
            SELECT q.question_id, q.source_message_id, q.answer_message_id, q.closed_at, q.session_id,
                   q.work_event_id, q.work_purpose
            FROM human_questions q
            WHERE q.goal_id = ? OR (q.goal_id IS NULL AND q.source_message_id = ?)
            ORDER BY q.rowid
        """, (goal_id, source_message_id))]
        corrections = []
        if questions:
            placeholders = ",".join("?" for _ in questions)
            corrections = [
                row["message_id"] for row in self.db.execute(
                    f"SELECT message_id FROM clarification_corrections WHERE question_id IN ({placeholders})",
                    tuple(question["question_id"] for question in questions),
                )
            ]
        proposal_ids = []
        assistant_ids = set()
        notification_messages = []
        if goal_id is not None:
            for row in self.db.execute("""
                SELECT p.id, v.data FROM delegation_proposals p
                JOIN delegation_versions v ON v.proposal_id = p.id AND v.version = p.current_version
                WHERE p.team_id = ? AND p.goal_id = ?
            """, (team_id, goal_id)):
                record = json.loads(row["data"])
                if record["status"] == "open":
                    proposal_ids.append(row["id"])
                if record.get("assistant_session_id"):
                    assistant_ids.add(record["assistant_session_id"])
            for row in self.db.execute("""
                SELECT n.message_id, d.data FROM delegation_notifications n
                JOIN delegation_events d ON d.seq = n.event_id
            """):
                if json.loads(row["data"]).get("goal_id") == goal_id:
                    notification_messages.append(row["message_id"])
        forks = []
        if goal_id is not None:
            for row in self.db.execute("SELECT session_id, details, status FROM assistant_forks"):
                if json.loads(row["details"]).get("goal_id") == goal_id:
                    forks.append(dict(row))
                    assistant_ids.add(row["session_id"])
        assistant_messages = []
        if forks:
            placeholders = ",".join("?" for _ in forks)
            assistant_messages = [
                row["message_id"] for row in self.db.execute(
                    f"SELECT message_id FROM fork_messages WHERE assistant_id IN ({placeholders})",
                    tuple(item["session_id"] for item in forks),
                )
            ]
        message_ids = list(dict.fromkeys([
            source_message_id,
            *notification_messages,
            *[question["question_id"] for question in questions],
            *[question["source_message_id"] for question in questions],
            *[question["answer_message_id"] for question in questions if question["answer_message_id"]],
            *corrections,
            *assistant_messages,
        ]))
        return {
            "questions": questions,
            "proposal_ids": proposal_ids,
            "forks": forks,
            "assistant_ids": sorted(assistant_ids),
            "message_ids": self._thread_message_ids(team_id, message_ids),
        }

    def _freeze(self, team, goal, acceptance, evidence, final_version_ids, scope):
        goal_id = goal["id"] if goal is not None else None
        task_ids = [row["id"] for row in self.db.execute(
            "SELECT id FROM delegation_tasks WHERE team_id = ? AND goal_id = ? ORDER BY rowid",
            (team["team_id"], goal_id),
        )] if goal_id is not None else []
        task_acceptance = {}
        for task_id in task_ids:
            task_acceptance[task_id] = self.delegation.results.current_acceptance_fact(
                team["team_id"], goal_id, task_id,
            )
        accepted_task_ids = [task_id for task_id in task_ids if task_acceptance[task_id] is not None]
        required = []
        for item in (acceptance or {}).get("required_deliveries", []):
            fact = task_acceptance.get(item["task_id"])
            entry = {
                "name": item["name"],
                "task_id": item["task_id"],
                "accepted": fact is not None,
                "acceptance": fact,
            }
            if fact is not None:
                entry.update({
                    "versions": fact["versions"],
                    "submission_id": fact["id"],
                    "review_id": fact["review"]["id"],
                })
            required.append(entry)
        final_versions = [self._version_view(self._version(team["team_id"], version_id))
                          for version_id in sorted(set(final_version_ids))]
        return {
            "metric_version": "T009-v1",
            "goal_id": goal_id,
            "source_message_id": goal["source_message_id"] if goal is not None else scope["source_message_id"],
            "acceptance_revision": acceptance["revision"] if acceptance is not None else None,
            "acceptance_required_deliveries": (acceptance or {}).get("required_deliveries", []),
            "required_deliveries": required,
            "effective_task_ids": task_ids,
            "accepted_task_ids": accepted_task_ids,
            "task_acceptance": task_acceptance,
            "final_versions": final_versions,
            "open_question_ids": [
                question["question_id"] for question in scope["questions"]
                if question["answer_message_id"] is None and question["closed_at"] is None
            ],
            "closed_proposal_ids": scope["proposal_ids"],
            "assistant_session_ids": scope["assistant_ids"],
            "evidence": evidence,
            "raw_counts": {
                "registered_tasks": len(task_ids),
                "accepted_tasks": len(accepted_task_ids),
                "required_deliveries": len(required),
                "accepted_required_deliveries": sum(item["accepted"] for item in required),
            },
        }

    def _close_scope(self, team_id, actor_id, goal_id, source_message_id, scope):
        now = _now()
        active_tasks = []
        for question in scope["questions"]:
            self.db.execute("""
                UPDATE human_questions SET closed_at = ?
                WHERE question_id = ? AND answer_message_id IS NULL AND closed_at IS NULL
            """, (now, question["question_id"]))
            self.db.execute("""
                UPDATE message_deliveries SET status = 'cancelled'
                WHERE event_id = ? AND session_id = ? AND purpose = ? AND status = 'blocked'
            """, (question["work_event_id"], question["session_id"], question["work_purpose"]))
        for proposal_id in scope["proposal_ids"]:
            record = self.delegation._proposal(team_id, proposal_id)
            record.update(status="scope_closed", closed_at=now, closed_by=actor_id,
                          reason="Goal ended before this proposal was registered")
            self.delegation._save_version(record)
            self.delegation._emit(team_id, "proposal.scope_closed", record)
        if scope["message_ids"]:
            placeholders = ",".join("?" for _ in scope["message_ids"])
            self.db.execute(f"""
                UPDATE message_deliveries SET status = 'cancelled'
                WHERE message_id IN ({placeholders})
                  AND status IN ('pending', 'running', 'blocked', 'recoverable',
                                 'waiting_human', 'waiting_approval')
            """, tuple(scope["message_ids"]))
            scheduled = self.db.execute(f"""
                SELECT d.event_id, d.session_id, d.purpose, s.turn_id, t.status AS turn_status
                FROM message_deliveries d
                JOIN scheduled_turns s USING(event_id, session_id, purpose)
                JOIN turns t ON t.id = s.turn_id
                WHERE d.message_id IN ({placeholders})
                  AND t.status IN ('running', 'recoverable', 'waiting_human', 'waiting_approval')
            """, tuple(scope["message_ids"])).fetchall()
            for delivery in scheduled:
                self.runtime.journal.next_generation(delivery["turn_id"])
                self.runtime._write_finish(delivery["turn_id"], "interrupted", error="Goal ended")
                active = self.runtime.active.get(delivery["session_id"])
                if active is not None and active.id == delivery["turn_id"] and active.task not in active_tasks:
                    active_tasks.append(active.task)

        for fork in scope["forks"]:
            if fork["status"] == "active":
                self.db.execute("UPDATE assistant_forks SET status = 'revoked' WHERE session_id = ?",
                                (fork["session_id"],))
            self.db.execute("UPDATE assistant_workspaces SET enabled = 0 WHERE session_id = ?",
                            (fork["session_id"],))
            jobs = self.db.execute("SELECT job_id, status, turn_id FROM assistant_jobs WHERE session_id = ?",
                                   (fork["session_id"],)).fetchall()
            for job in jobs:
                if job["status"] == "pending":
                    self.db.execute("UPDATE assistant_jobs SET status = 'cancelled' WHERE job_id = ?",
                                    (job["job_id"],))
                elif job["turn_id"] is not None:
                    turn = self.db.execute("SELECT status FROM turns WHERE id = ?", (job["turn_id"],)).fetchone()
                    if turn is not None and turn["status"] in {
                        "running", "recoverable", "waiting_human", "waiting_approval",
                    }:
                        self.runtime.journal.next_generation(job["turn_id"])
                        self.runtime._write_finish(job["turn_id"], "interrupted", error="Goal ended")
                        active = self.runtime.active.get(fork["session_id"])
                        if active is not None and active.id == job["turn_id"]:
                            active_tasks.append(active.task)
        for assistant_id in scope["assistant_ids"]:
            self.db.execute("UPDATE assistant_workspaces SET enabled = 0 WHERE session_id = ?",
                            (assistant_id,))
        return active_tasks

    def _notify_terminal(self, team, actor, goal, outcome, reason, event_id):
        lead_id = team["lead_member_id"]
        lead = next(member for member in team["members"] if member["member_id"] == lead_id)
        label = {"completed": "已成功交付", "failed": "已失败", "timed_out": "已超时",
                 "cancelled": "已终止"}[outcome]
        target = (f"目标 {goal['id']} {label}" if goal is not None
                  else "原始布置在目标登记前已终止")
        text = f"{target}。原因：{reason}。请按队列顺序激活最早满足前置条件的目标。"
        message = self.collaboration._create_message(
            team["team_id"], actor, recipient=lead_id, text=text, kind="progress",
            mentions=[lead_id], reply_to=None,
        )
        self.collaboration._deliver(message, (lead["session_id"],), "goal.terminal")
        return message["message_id"]

    def _insert_terminal(self, team_id, goal, source_message_id, outcome, actor_id, actor_kind,
                         reason, evidence, frozen, event_id, host_event_key=None):
        terminal_id = uuid4().hex
        server_time = _now()
        self.db.execute("""
            INSERT INTO goal_terminal_facts
            (terminal_id, team_id, goal_id, source_message_id, outcome, actor_id, actor_kind,
             reason, evidence, frozen, server_time, event_id, host_event_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (terminal_id, team_id, goal["id"] if goal is not None else None, source_message_id,
              outcome, actor_id, actor_kind, reason,
              json.dumps(evidence, ensure_ascii=False),
              json.dumps(frozen, ensure_ascii=False), server_time, event_id, host_event_key))
        return terminal_id, server_time

    @staticmethod
    def _receipt(goal_id, terminal_id, source_message_id, outcome, server_time, frozen,
                 message_id, event_id, actor_id, actor_kind, host_event_key=None):
        receipt = {
            "goal_id": goal_id,
            "status": outcome,
            "terminal_id": terminal_id,
            "source_message_id": source_message_id,
            "actor_id": actor_id,
            "actor_kind": actor_kind,
            "server_time": server_time,
            "event_id": event_id,
            "notification_message_id": message_id,
            "frozen_summary": {
                "required_deliveries": frozen["raw_counts"]["required_deliveries"],
                "accepted_deliveries": frozen["raw_counts"]["accepted_required_deliveries"],
                "registered_tasks": frozen["raw_counts"]["registered_tasks"],
                "accepted_tasks": frozen["raw_counts"]["accepted_tasks"],
            },
        }
        if host_event_key is not None:
            receipt["host_event_key"] = host_event_key
        return receipt

    def _check_terminal_permission(self, team, actor, outcome):
        if outcome in {"completed", "failed"} and not self._is_lead(team, actor):
            raise CollaborationForbidden("Only the Lead can close or fail a goal")
        if outcome == "cancelled" and not actor.is_operator:
            raise CollaborationForbidden("Only the user can cancel a goal")

    def _validate_completed(self, team, acceptance, frozen, scope, final_version_ids):
        if acceptance is None or not acceptance["required_deliveries"]:
            raise TeamConflict("Goal acceptance has no required deliverables")
        missing = [item["name"] for item in frozen["required_deliveries"] if not item["accepted"]]
        if missing:
            raise TeamConflict("Required deliverables do not have current readable acceptance: "
                               + ", ".join(missing))
        if any(question["answer_message_id"] is None and question["closed_at"] is None
               for question in scope["questions"]):
            raise TeamConflict("Related human questions remain open")
        required_versions = {
            version["version_id"] for item in frozen["required_deliveries"]
            for version in (item.get("versions") or [])
        }
        if not required_versions <= set(final_version_ids):
            raise TeamConflict("Final versions must include every currently accepted required deliverable")
        for version_id in final_version_ids:
            try:
                self.delegation.artifacts.resolve(team["team_id"], self._lead_principal(team).session_id, version_id)
            except (ArtifactError, ArtifactNotFound) as exc:
                raise TeamNotFound("Final version is not readable") from exc

    def terminate_goal(self, team_id, actor, goal_id, body: TerminalInput):
        key = (team_id, actor.id, "terminate_goal", body.request_id)
        payload = body.model_dump(exclude={"request_id"})
        payload["final_version_ids"] = sorted(set(payload["final_version_ids"]))
        digest = self.collaboration._digest({"goal_id": goal_id, **payload})
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            team = self.collaboration.team(team_id, actor)
            goal = self._goal_row(team_id, actor, goal_id)
            self._check_terminal_permission(team, actor, body.outcome)
            replay = self.collaboration._replay(key, digest)
            if replay is not None:
                return replay
            if goal["status"] not in {"active", "queued"}:
                raise TeamConflict("Goal is already terminal")
            if body.expected_revision != goal["revision"]:
                raise TeamConflict("Goal acceptance revision changed")
            if body.outcome == "completed" and goal["status"] != "active":
                raise TeamConflict("Only the active goal can be completed")
            acceptance = self._acceptance(goal_id, goal["revision"])
            scope = self._scope_objects(team_id, goal_id, goal["source_message_id"])
            scope["source_message_id"] = goal["source_message_id"]
            reason = body.reason or ("用户主动终止" if body.outcome == "cancelled" else "必需交付已通过")
            evidence = body.evidence
            final_version_ids = sorted(set(body.final_version_ids))
            frozen = self._freeze(team, dict(goal), acceptance, evidence, final_version_ids, scope)
            if body.outcome == "completed":
                self._validate_completed(team, acceptance, frozen, scope, final_version_ids)
            for version_id in final_version_ids:
                try:
                    self.delegation.artifacts.resolve(
                        team_id, self._lead_principal(team).session_id, version_id,
                    )
                except (ArtifactError, ArtifactNotFound) as exc:
                    raise TeamNotFound("Final version is not readable") from exc
            scope["source_message_id"] = goal["source_message_id"]
            active_tasks = self._close_scope(team_id, actor.id, goal_id, goal["source_message_id"], scope)
            self.db.execute("UPDATE team_goals SET status = ? WHERE id = ?", (body.outcome, goal_id))
            event_id = self.collaboration._event(team_id, f"goal.{body.outcome}", goal_id)
            terminal_id, server_time = self._insert_terminal(
                team_id, dict(goal), goal["source_message_id"], body.outcome, actor.id,
                "lead" if body.outcome in {"completed", "failed"} else "user",
                reason, evidence, frozen, event_id,
            )
            if body.outcome == "completed":
                self.db.executemany(
                    "INSERT INTO goal_final_versions VALUES (?, ?, ?)",
                    [(terminal_id, goal_id, version_id) for version_id in final_version_ids],
                )
            message_id = self._notify_terminal(team, actor, dict(goal), body.outcome, reason, event_id)
            receipt = self._receipt(goal_id, terminal_id, goal["source_message_id"], body.outcome,
                                    server_time, frozen, message_id, event_id, actor.id,
                                    "lead" if body.outcome in {"completed", "failed"} else "user")
            self.collaboration._save_receipt(key, digest, receipt)
        for active in active_tasks:
            if active is not None:
                active.cancel()
        return receipt

    def terminate_before_registration(self, team_id, actor, source_message_id,
                                      body: PreRegistrationTerminationInput):
        key = (team_id, actor.id, "terminate_assignment", body.request_id)
        digest = self.collaboration._digest({"source_message_id": source_message_id,
                                             "reason": body.reason})
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            team = self.collaboration.team(team_id, actor)
            if not actor.is_operator:
                raise CollaborationForbidden("Only the user can terminate an unregistered assignment")
            goal, source = self._goal_by_source(team_id, actor, source_message_id)
            replay = self.collaboration._replay(key, digest)
            if replay is not None:
                return replay
            if source["sender_id"] != OPERATOR_ID or source["kind"] != "assignment":
                raise CollaborationInvalid("Pre-registration termination requires the original user assignment")
            if goal is not None:
                raise TeamConflict("Goal is already registered")
            if self._terminal(source_message_id=source["id"]) is not None:
                raise TeamConflict("Original assignment is already terminal")
            scope = self._scope_objects(team_id, None, source["id"])
            scope["source_message_id"] = source["id"]
            reason = body.reason or "用户主动终止"
            frozen = self._freeze(team, None, None, [], [], scope)
            active_tasks = self._close_scope(team_id, actor.id, None, source["id"], scope)
            event_id = self.collaboration._event(team_id, "goal.cancelled_before_registration", source["id"])
            terminal_id, server_time = self._insert_terminal(
                team_id, None, source["id"], "cancelled", actor.id, "user",
                reason, [], frozen, event_id,
            )
            message_id = self._notify_terminal(team, actor, None, "cancelled", reason, event_id)
            receipt = self._receipt(None, terminal_id, source["id"], "cancelled", server_time,
                                    frozen, message_id, event_id, actor.id, "user")
            self.collaboration._save_receipt(key, digest, receipt)
        for active in active_tasks:
            if active is not None:
                active.cancel()
        return receipt

    def _host_terminal(self, team_id, goal_id, expected_revision, outcome, event_key,
                       reason, evidence):
        if outcome not in {"failed", "timed_out"}:
            raise CollaborationInvalid("Host terminal outcome must be failed or timed_out")
        team = self.collaboration.teams.get(team_id)
        actor = Principal("runtime-host", team_id, None)
        key = (team_id, actor.id, "host_goal_terminal", event_key)
        digest = self.collaboration._digest({
            "goal_id": goal_id, "expected_revision": expected_revision, "outcome": outcome,
            "reason": reason, "evidence": evidence, "final_version_ids": [],
        })
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            goal = self._goal_row(team_id, self._lead_principal(team), goal_id)
            replay = self.collaboration._replay(key, digest)
            if replay is not None:
                return replay
            if goal["status"] not in {"active", "queued"}:
                raise TeamConflict("Goal is already terminal")
            if expected_revision != goal["revision"]:
                raise TeamConflict("Goal acceptance revision changed")
            acceptance = self._acceptance(goal_id, goal["revision"])
            scope = self._scope_objects(team_id, goal_id, goal["source_message_id"])
            scope["source_message_id"] = goal["source_message_id"]
            frozen = self._freeze(team, dict(goal), acceptance, evidence, [], scope)
            active_tasks = self._close_scope(team_id, actor.id, goal_id, goal["source_message_id"], scope)
            self.db.execute("UPDATE team_goals SET status = ? WHERE id = ?", (outcome, goal_id))
            event_id = self.collaboration._event(team_id, f"goal.{outcome}", goal_id)
            terminal_id, server_time = self._insert_terminal(
                team_id, dict(goal), goal["source_message_id"], outcome, actor.id, "host",
                reason, evidence, frozen, event_id, host_event_key=event_key,
            )
            message_id = self._notify_terminal(team, actor, dict(goal), outcome, reason, event_id)
            receipt = self._receipt(goal_id, terminal_id, goal["source_message_id"], outcome,
                                    server_time, frozen, message_id, event_id, actor.id, "host",
                                    host_event_key=event_key)
            self.collaboration._save_receipt(key, digest, receipt)
        for active in active_tasks:
            if active is not None:
                active.cancel()
        return receipt

    def record_host_event(self, team_id, goal_id, body: HostTerminalEventInput):
        return self._host_terminal(
            team_id, goal_id, body.expected_revision, body.outcome, body.event_key,
            body.reason, body.evidence,
        )

    def expire_due_goals(self, now=None):
        current = now or datetime.now(timezone.utc)
        receipts = []
        rows = self.db.execute("""
            SELECT g.id AS goal_id, g.team_id, g.revision, d.deadline_at,
                   d.source, d.configured_seconds
            FROM goal_deadlines d JOIN team_goals g ON g.id = d.goal_id
            WHERE g.status IN ('active', 'queued')
        """).fetchall()
        for row in rows:
            deadline = datetime.fromisoformat(row["deadline_at"].replace("Z", "+00:00"))
            if deadline > current:
                continue
            event_key = f"deadline:{row['goal_id']}:{row['deadline_at']}"
            try:
                receipts.append(self._host_terminal(
                    row["team_id"], row["goal_id"], row["revision"], "timed_out", event_key,
                    f"目标在 {row['deadline_at']} 前未完成",
                    [
                        f"宿主策略 {row['source']} 配置 {row['configured_seconds']} 秒；"
                        f"持久化截止时间 {row['deadline_at']} 已到；宿主复核时间 {current.isoformat()}",
                    ],
                ))
            except TeamConflict:
                continue
        return receipts

    def activate_goal(self, team_id, actor, goal_id, body: ActivateGoalInput):
        key = (team_id, actor.id, "activate_goal", body.request_id)
        digest = self.collaboration._digest({"goal_id": goal_id, "expected_revision": body.expected_revision})
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            team = self.collaboration.team(team_id, actor)
            goal = self._goal_row(team_id, actor, goal_id)
            if not self._is_lead(team, actor):
                raise CollaborationForbidden("Only the Lead can activate a goal")
            replay = self.collaboration._replay(key, digest)
            if replay is not None:
                return replay
            if goal["status"] != "queued":
                raise TeamConflict("Only a queued goal can be activated")
            if body.expected_revision != goal["revision"]:
                raise TeamConflict("Goal acceptance revision changed")
            active = self.db.execute(
                "SELECT id FROM team_goals WHERE team_id = ? AND status = 'active'", (team_id,),
            ).fetchone()
            if active is not None:
                raise TeamConflict("The team already has an active goal")
            if not self.prerequisites_completed(goal_id):
                raise TeamConflict("Goal prerequisites are not complete")
            earlier = self.db.execute("""
                SELECT id FROM team_goals WHERE team_id = ? AND status = 'queued' AND seq < ? ORDER BY seq
            """, (team_id, goal["seq"]))
            for row in earlier:
                if self.prerequisites_completed(row["id"]):
                    raise TeamConflict("An earlier eligible goal must be activated first")
            self.db.execute("UPDATE team_goals SET status = 'active' WHERE id = ?", (goal_id,))
            event_id = self.collaboration._event(team_id, "goal.activated", goal_id)
            receipt = {"goal_id": goal_id, "status": "active", "revision": goal["revision"],
                       "event_id": event_id}
            self.collaboration._save_receipt(key, digest, receipt)
            return receipt

    def scope_is_open(self, goal_id):
        row = self.db.execute("SELECT status FROM team_goals WHERE id = ?", (goal_id,)).fetchone()
        return row is not None and row["status"] == "active" and self._terminal(goal_id=goal_id) is None

    def operator_can_read_version(self, version_id):
        return self.db.execute("""
            SELECT 1 FROM goal_final_versions f JOIN goal_terminal_facts t USING(terminal_id)
            WHERE f.version_id = ? AND t.outcome = 'completed'
        """, (version_id,)).fetchone() is not None

    def tools_for(self, session_id):
        if self.collaboration.auth.member(session_id) is None:
            return []
        return [{"type": "function", "function": {
            "name": name, "description": description, "parameters": schema.model_json_schema(),
        }} for name, (schema, description) in TOOLS.items()]

    def handles(self, name):
        return name in TOOLS

    def execute(self, session_id, name, raw_arguments):
        actor = self.collaboration.auth.member(session_id)
        if actor is None:
            return {"ok": False, "error": "Goal tools require a formal member session"}
        try:
            args = TOOLS[name][0].model_validate_json(raw_arguments)
            if name == "get_goal":
                result = self.get_goal(actor.team_id, actor, args.goal_id)
            elif name == "update_goal_acceptance":
                result = self.update_acceptance(
                    actor.team_id, actor, args.goal_id,
                    AcceptanceUpdateInput(**args.model_dump(exclude={"goal_id"})),
                )
            elif name == "close_goal":
                result = self.terminate_goal(
                    actor.team_id, actor, args.goal_id,
                    TerminalInput(**args.model_dump(exclude={"goal_id"})),
                )
            else:
                result = self.activate_goal(
                    actor.team_id, actor, args.goal_id,
                    ActivateGoalInput(**args.model_dump(exclude={"goal_id"})),
                )
            return {"ok": True, "result": result}
        except ValidationError:
            return {"ok": False, "error": "Invalid goal tool arguments", "status": 422}
        except (TeamNotFound, TeamConflict, CollaborationForbidden, CollaborationInvalid) as exc:
            status = {TeamNotFound: 404, TeamConflict: 409,
                      CollaborationForbidden: 403, CollaborationInvalid: 422}
            return {"ok": False, "error": str(exc), "status": status[type(exc)]}
