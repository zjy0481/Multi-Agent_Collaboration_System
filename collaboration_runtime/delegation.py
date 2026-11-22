"""Versioned negotiation and task registration in the collaboration transaction."""

import json
from typing import Annotated
from uuid import uuid4

from pydantic import Field

from .artifacts import ArtifactNotFound
from .collaboration import (
    CollaborationForbidden, CollaborationInvalid, Command, ObjectId, RequestId, Text, _now,
)
from .local_auth import Principal
from .teams import TeamConflict, TeamNotFound


Version = Annotated[int, Field(ge=1, strict=True)]
Members = Annotated[list[ObjectId], Field(max_length=100)]
TermsList = Annotated[list[Text], Field(min_length=1, max_length=32)]


class TaskTerms(Command):
    deliverables: TermsList
    acceptance_criteria: TermsList
    owner_id: ObjectId
    reviewer_id: ObjectId
    commitment_ids: Members = Field(default_factory=list)
    input_version_ids: Members = Field(default_factory=list)
    dependency_ids: Members = Field(default_factory=list)
    parent_task_id: ObjectId | None = None

    def normalized(self):
        terms = self.model_dump()
        for field in ("commitment_ids", "input_version_ids", "dependency_ids"):
            terms[field] = sorted(set(terms[field]))
        return terms


class CreateProposalInput(Command):
    request_id: RequestId
    goal_id: ObjectId
    scope_id: ObjectId | None = None
    task_id: ObjectId | None = None
    base_revision: Version | None = None
    terms: TaskTerms


class ProposalVersionInput(Command):
    request_id: RequestId
    expected_version: Version


class CounterProposalInput(ProposalVersionInput):
    base_revision: Version | None = None
    terms: TaskTerms


class ProposalReasonInput(ProposalVersionInput):
    reason: Text


class DelegationConflict(TeamConflict):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


DRAFT_OPERATIONS = {"create_proposal", "counter_proposal"}
REGISTRATION_OPERATIONS = {"assign_task", "update_task"}


class DelegationService:
    def __init__(self, collaboration, workspace):
        self.collaboration = collaboration
        self.db = collaboration.db
        self.artifacts = workspace.artifacts
        self.forks = None
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS delegation_tasks (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                goal_id TEXT NOT NULL REFERENCES team_goals(id),
                revision INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS delegation_tasks_team ON delegation_tasks(team_id);
            CREATE TABLE IF NOT EXISTS delegation_revisions (
                task_id TEXT NOT NULL REFERENCES delegation_tasks(id),
                revision INTEGER NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (task_id, revision)
            );
            CREATE TABLE IF NOT EXISTS delegation_proposals (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                goal_id TEXT NOT NULL REFERENCES team_goals(id),
                scope_id TEXT,
                task_id TEXT REFERENCES delegation_tasks(id),
                current_version INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS delegation_proposals_team ON delegation_proposals(team_id);
            CREATE UNIQUE INDEX IF NOT EXISTS delegation_initial_scope
                ON delegation_proposals(goal_id, scope_id) WHERE task_id IS NULL;
            CREATE TABLE IF NOT EXISTS delegation_versions (
                proposal_id TEXT NOT NULL REFERENCES delegation_proposals(id),
                version INTEGER NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (proposal_id, version)
            );
            CREATE TABLE IF NOT EXISTS delegation_events (
                seq INTEGER PRIMARY KEY REFERENCES collaboration_events(seq),
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delegation_notifications (
                message_id TEXT PRIMARY KEY REFERENCES team_messages(id),
                event_id INTEGER NOT NULL REFERENCES delegation_events(seq)
            );
        """)

    def proposal_team(self, proposal_id):
        row = self.db.execute("SELECT team_id FROM delegation_proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise TeamNotFound("Proposal not found")
        return row["team_id"]

    def _goal(self, team_id, actor, goal_id, *, active=False):
        row = self.db.execute(
            "SELECT * FROM team_goals WHERE team_id = ? AND id = ?", (team_id, goal_id),
        ).fetchone()
        if row is None:
            raise TeamNotFound("Goal not found")
        source = self.collaboration._message_for(team_id, actor, row["source_message_id"])
        if active and row["status"] != "active":
            raise DelegationConflict("Goal is no longer active")
        return row, source

    def _can_read(self, team_id, actor, record):
        if actor.id not in record["readers"]:
            return False
        try:
            self.collaboration._message_for(team_id, actor, record["source_message_id"])
            return all(
                actor.session_id in self.artifacts.reference_readers(team_id, version)
                for version in record["terms"]["input_version_ids"]
            )
        except (TeamNotFound, ArtifactNotFound):
            return False

    @staticmethod
    def _view(record):
        return {key: value for key, value in record.items()
                if key not in {"readers", "registration", "confirmation_events"}}

    def _proposal(self, team_id, proposal_id, version=None):
        row = self.db.execute("""
            SELECT v.data FROM delegation_proposals p JOIN delegation_versions v ON v.proposal_id = p.id
            WHERE p.team_id = ? AND p.id = ? AND v.version = COALESCE(?, p.current_version)
        """, (team_id, proposal_id, version)).fetchone()
        if row is None:
            raise TeamNotFound("Proposal not found")
        return json.loads(row["data"])

    def _task(self, team_id, task_id, revision=None):
        row = self.db.execute("""
            SELECT r.data FROM delegation_tasks t JOIN delegation_revisions r ON r.task_id = t.id
            WHERE t.team_id = ? AND t.id = ? AND r.revision = COALESCE(?, t.revision)
        """, (team_id, task_id, revision)).fetchone()
        if row is None:
            raise TeamNotFound("Task not found")
        return json.loads(row["data"])

    def _visible_proposal(self, team_id, actor, proposal_id, version=None):
        record = self._proposal(team_id, proposal_id, version)
        if not self._can_read(team_id, actor, record):
            raise TeamNotFound("Proposal not found")
        return record

    def _visible_task(self, team_id, actor, task_id, revision=None):
        record = self._task(team_id, task_id, revision)
        if not self._can_read(team_id, actor, record):
            raise TeamNotFound("Task not found")
        return record

    def get_proposal(self, team_id, actor, proposal_id, version=None):
        self.collaboration.team(team_id, actor)
        return self._view(self._visible_proposal(team_id, actor, proposal_id, version))

    def get_task(self, team_id, actor, task_id, revision=None):
        self.collaboration.team(team_id, actor)
        return self._view(self._visible_task(team_id, actor, task_id, revision))

    def snapshot(self, team_id, actor):
        self.collaboration.team(team_id, actor)
        result = {}
        for key, table, load in (
            ("proposals", "delegation_proposals", self._proposal),
            ("tasks", "delegation_tasks", self._task),
        ):
            records = (load(team_id, row["id"]) for row in self.db.execute(
                f"SELECT id FROM {table} WHERE team_id = ? ORDER BY rowid", (team_id,),
            ))
            result[key] = [self._view(record) for record in records if self._can_read(team_id, actor, record)]
        return result

    def active_goals(self, team_id, actor):
        self.collaboration.team(team_id, actor)
        goals = []
        for row in self.db.execute("SELECT id FROM team_goals WHERE team_id = ? AND status = 'active'", (team_id,)):
            try:
                goal, _ = self._goal(team_id, actor, row["id"])
            except TeamNotFound:
                continue
            goals.append(dict(goal))
        return goals

    def _event_record(self, team_id, seq):
        row = self.db.execute("""
            SELECT e.type, d.data FROM delegation_events d JOIN collaboration_events e ON e.seq = d.seq
            WHERE e.team_id = ? AND d.seq = ?
        """, (team_id, seq)).fetchone()
        return (row["type"], json.loads(row["data"])) if row else (None, None)

    def event(self, team_id, actor, event_type, seq):
        if not event_type.startswith(("proposal.", "task.")):
            return None
        actual, record = self._event_record(team_id, seq)
        if actual != event_type or not self._can_read(team_id, actor, record):
            return None
        key = "proposal" if event_type.startswith("proposal.") else "task"
        return {key: self._view(record)}

    def execution_context(self, message_id, actor):
        row = self.db.execute(
            "SELECT event_id FROM delegation_notifications WHERE message_id = ?", (message_id,),
        ).fetchone()
        if row is None:
            return None
        event_type, record = self._event_record(actor.team_id, row["event_id"])
        if record is None or not self._can_read(actor.team_id, actor, record):
            return None
        if event_type.startswith("proposal."):
            current = self._visible_proposal(actor.team_id, actor, record["id"], record["version"])
            return {"proposal": self._view(current)}
        return {"task": self._view(record)}

    def _emit(self, team_id, event_type, record):
        seq = self.collaboration._event(team_id, event_type, record["id"])
        self.db.execute("INSERT INTO delegation_events VALUES (?, ?)", (seq, json.dumps(record)))
        return seq

    def _notify(self, team_id, actor, event_id, *, recipient, targets, text, mentions, purpose):
        message = self.collaboration._create_message(
            team_id, actor, recipient=recipient, text=text, kind="progress", mentions=mentions, reply_to=None,
        )
        self.collaboration._deliver(message, targets, purpose)
        self.db.execute("INSERT INTO delegation_notifications VALUES (?, ?)", (message["message_id"], event_id))
        return message["message_id"]

    @staticmethod
    def _parties(dispatcher, terms):
        return {dispatcher, terms["owner_id"], terms["reviewer_id"], *terms["commitment_ids"]}

    def _validate_terms(self, team_id, actor, members, record):
        required = set(record["required_member_ids"])
        terms = record["terms"]
        if not required <= members.keys():
            raise CollaborationInvalid("Commitments must identify formal team members")
        participants = [members[member_id] for member_id in sorted(required | {actor.id})]
        for member in participants:
            try:
                self.collaboration._message_for(team_id, member, record["source_message_id"])
            except TeamNotFound as exc:
                raise CollaborationForbidden("A commitment party cannot read the goal source") from exc
        for version_id in terms["input_version_ids"]:
            readers = self.artifacts.reference_readers(team_id, version_id)
            if actor.session_id not in readers:
                raise TeamNotFound("Input version not found")
            if any(member.session_id not in readers for member in participants):
                raise CollaborationForbidden("An input version is not shared with every commitment party")
        self._validate_links(team_id, participants, record)

    @staticmethod
    def _check_assistant_scope(assistant, task_id, scope_id):
        if assistant.scope_id != (task_id or scope_id):
            raise CollaborationForbidden("Assistant is outside the authorized negotiation scope")

    def _check_assistant_draft(self, assistant, record):
        self._check_assistant_scope(assistant, record["task_id"], record["scope_id"])
        if not set(record["terms"]["input_version_ids"]) <= set(assistant.versions):
            raise CollaborationForbidden("Input version was not handed off to this assistant")
        if self.forks is not None and self.forks.exists(assistant.session_id):
            fork = self.forks.require_active(assistant.session_id)
            if record["goal_id"] != fork["goal_id"] or record["base_revision"] != fork["expected_revision"]:
                raise CollaborationForbidden("The draft is outside the assistant's goal or task revision")
            allowed_sessions = {fork["parent_session_id"], *fork["contact_session_ids"]}
            team = self.collaboration.teams.get(assistant.team_id)
            allowed_members = {member["member_id"] for member in team["members"]
                               if member["session_id"] in allowed_sessions}
            if not self._parties(record["dispatcher_id"], record["terms"]) <= allowed_members:
                raise CollaborationForbidden("Draft commitments include a party outside the assistant grant")

    def _validate_links(self, team_id, participants, record):
        for field in ("parent_task_id", "dependency_ids"):
            pending = list(self._links(record["terms"], field))
            for task_id in pending:
                target = self._task(team_id, task_id)
                if target["goal_id"] != record["goal_id"]:
                    raise CollaborationInvalid("Task links must stay within the same goal")
                if any(not self._can_read(team_id, member, target) for member in participants):
                    raise TeamNotFound("Linked task not found")
            visited = set()
            while pending:
                task_id = pending.pop()
                if task_id == record["task_id"]:
                    raise CollaborationInvalid(f"{field} would create a task cycle")
                if task_id in visited:
                    continue
                visited.add(task_id)
                pending.extend(self._links(self._task(team_id, task_id)["terms"], field))

    @staticmethod
    def _links(terms, field):
        value = terms[field]
        if field == "dependency_ids":
            return value
        return [value] if value is not None else []

    def _check_basis(self, team_id, actor, task_id, expected, *, show_changes=True):
        if task_id is None:
            return None
        current = self._task(team_id, task_id)
        if current["revision"] == expected:
            return current
        base = self._task(team_id, task_id, expected) if show_changes and expected < current["revision"] else None
        changes = {}
        if base and self._can_read(team_id, actor, base) and self._can_read(team_id, actor, current):
            changes = {
                key: {"before": value, "after": current["terms"][key]}
                for key, value in base["terms"].items() if value != current["terms"][key]
            }
        raise DelegationConflict("Task base revision changed", {
            "expected_revision": expected, "current_revision": current["revision"], "changes": changes,
        })

    def _save_version(self, record, *, new=False):
        encoded = json.dumps(record)
        if new:
            self.db.execute(
                "INSERT INTO delegation_versions VALUES (?, ?, ?)", (record["id"], record["version"], encoded),
            )
        else:
            self.db.execute(
                "UPDATE delegation_versions SET data = ? WHERE proposal_id = ? AND version = ?",
                (encoded, record["id"], record["version"]),
            )

    def _new_version(self, team_id, actor, lead, members, previous, terms, *, base_revision, assistant):
        if assistant is not None:
            self._check_assistant_draft(assistant, {**previous, "terms": terms, "base_revision": base_revision})
        base = self._check_basis(team_id, actor, previous["task_id"], base_revision, show_changes=assistant is None)
        required = self._parties(previous["dispatcher_id"], terms)
        if "terms" in previous:
            required.update(self._parties(previous["dispatcher_id"], previous["terms"]))
        if base is not None:
            required.update(self._parties(base["dispatcher_id"], base["terms"]))
        record = {
            **previous, "terms": terms, "base_revision": base_revision, "status": "open",
            "required_member_ids": sorted(required), "readers": sorted(required | {lead, actor.id}),
            "confirmation_ids": [], "confirmation_events": {}, "proposer_id": actor.id,
            "assistant_session_id": assistant.session_id if assistant is not None else None,
            "created_at": _now(),
        }
        self._validate_terms(team_id, actor, members, record)
        return record

    def _create(self, team_id, actor, lead, members, body, terms, assistant):
        if assistant is not None:
            self._check_assistant_scope(assistant, body.task_id, body.scope_id)
        goal, _ = self._goal(team_id, actor, body.goal_id, active=True)
        if body.task_id is not None:
            if body.base_revision is None or body.scope_id is not None:
                raise CollaborationInvalid("A task revision requires a base revision and uses the task scope")
            old = self._visible_task(team_id, actor, body.task_id)
            if old["goal_id"] != body.goal_id:
                raise CollaborationInvalid("Task belongs to another goal")
            dispatcher = old["dispatcher_id"]
        else:
            if body.scope_id is None or body.base_revision is not None:
                raise CollaborationInvalid("An initial proposal requires a negotiation scope")
            present = self.db.execute("""
                SELECT id FROM delegation_proposals WHERE goal_id = ? AND scope_id = ? AND task_id IS NULL
            """, (body.goal_id, body.scope_id)).fetchone()
            if present:
                self._visible_proposal(team_id, actor, present["id"])
                raise DelegationConflict("Negotiation scope already has a proposal")
            dispatcher = actor.id
        record = self._new_version(team_id, actor, lead, members, {
            "id": uuid4().hex, "team_id": team_id, "version": 1, "goal_id": body.goal_id, "task_id": body.task_id,
            "scope_id": body.scope_id, "dispatcher_id": dispatcher, "source_message_id": goal["source_message_id"],
        }, terms, base_revision=body.base_revision, assistant=assistant)
        self.db.execute("INSERT INTO delegation_proposals VALUES (?, ?, ?, ?, ?, 1)",
                        (record["id"], team_id, body.goal_id, body.scope_id, body.task_id))
        self._save_version(record, new=True)
        event_id = self._emit(team_id, "proposal.created", record)
        self._notify_proposal(team_id, actor, members, event_id, record, record["required_member_ids"])
        return self._proposal_receipt(record, event_id)

    def _counter(self, team_id, actor, lead, members, record, body, terms, assistant):
        if record["task_id"] is None and body.base_revision is not None:
            raise CollaborationInvalid("An initial proposal has no base revision")
        base_revision = body.base_revision if body.base_revision is not None else record["base_revision"]
        revised = self._new_version(team_id, actor, lead, members, {**record, "version": record["version"] + 1},
                                    terms, base_revision=base_revision, assistant=assistant)
        record["status"] = "superseded"
        self._save_version(record)
        self._save_version(revised, new=True)
        self.db.execute("UPDATE delegation_proposals SET current_version = ? WHERE id = ?",
                        (revised["version"], revised["id"]))
        event_id = self._emit(team_id, "proposal.countered", revised)
        self._notify_proposal(team_id, actor, members, event_id, revised, revised["required_member_ids"])
        return self._proposal_receipt(revised, event_id)

    @staticmethod
    def _proposal_receipt(record, event_id):
        pending = set(record["required_member_ids"]) - set(record["confirmation_ids"])
        return {"proposal_id": record["id"], "version": record["version"], "status": record["status"],
                "pending_member_ids": sorted(pending), "event_id": event_id}

    def _notify_proposal(self, team_id, actor, members, event_id, record, recipient_ids):
        for member_id in sorted(set(recipient_ids) - {actor.id}):
            member = members[member_id]
            if self._can_read(team_id, member, record):
                self._notify(team_id, actor, event_id, recipient=member.id, targets=[member.session_id],
                             text="委托草案有更新", mentions=[], purpose="delegation.proposal")

    def _confirm(self, team_id, actor, members, record):
        if actor.id not in record["required_member_ids"]:
            raise CollaborationForbidden("Only a commitment party can confirm this version")
        self._check_basis(team_id, actor, record["task_id"], record["base_revision"])
        existing = record["confirmation_events"].get(actor.id)
        if existing is not None:
            return self._proposal_receipt(record, existing)
        record["confirmation_ids"] = sorted({*record["confirmation_ids"], actor.id})
        event_id = self._emit(team_id, "proposal.confirmed", record)
        record["confirmation_events"][actor.id] = event_id
        self._save_version(record)
        receipt = self._proposal_receipt(record, event_id)
        if not receipt["pending_member_ids"]:
            self._notify_proposal(team_id, actor, members, event_id, record, [record["proposer_id"]])
        return receipt

    def _close(self, team_id, actor, members, record, operation, body):
        if operation == "reject_proposal":
            if actor.id not in record["required_member_ids"]:
                raise CollaborationForbidden("Only a commitment party can reject this version")
            status = "rejected"
        else:
            if actor.id != record["proposer_id"]:
                raise CollaborationForbidden("Only this version's proposer can withdraw it")
            status = "withdrawn"
        record.update(status=status, reason=body.reason, closed_by=actor.id, closed_at=_now())
        self._save_version(record)
        event_id = self._emit(team_id, f"proposal.{status}", record)
        self._notify_proposal(team_id, actor, members, event_id, record, record["required_member_ids"])
        return self._proposal_receipt(record, event_id)

    def _notify_task(self, team_id, actor, lead, members, task, source, event_id, event_type):
        title = "任务已登记" if event_type == "task.assigned" else "任务修订已登记"
        target_ids = list(dict.fromkeys([lead, task["terms"]["owner_id"]]))
        targets = [members[item] for item in target_ids if self._can_read(team_id, members[item], task)]
        if task["terms"]["input_version_ids"]:
            message_ids = [self._notify(
                team_id, actor, event_id, recipient=target.id, targets=[target.session_id], text=title,
                mentions=[lead] if target.id == lead else [], purpose=event_type,
            ) for target in targets]
            return message_ids[0]
        recipient = None
        if source["recipient_id"] is not None:
            recipient = source["recipient_id"] if source["sender_id"] == actor.id else source["sender_id"]
        return self._notify(team_id, actor, event_id, recipient=recipient,
                            targets=[target.session_id for target in targets], text=title, mentions=[lead],
                            purpose=event_type)

    def _register(self, team_id, actor, lead, members, record, source, operation):
        initial = record["task_id"] is None
        if initial != (operation == "assign_task"):
            raise CollaborationInvalid("Choose the registration command for the proposal type")
        missing = sorted(set(record["required_member_ids"]) - set(record["confirmation_ids"]))
        if missing:
            raise DelegationConflict("All commitment parties must confirm this version", {"pending_member_ids": missing})
        current = self._check_basis(team_id, actor, record["task_id"], record["base_revision"])
        self._validate_terms(team_id, actor, members, record)
        revision = 1 if initial else current["revision"] + 1
        task_id = uuid4().hex if initial else current["id"]
        terms = record["terms"]
        task = {
            "id": task_id, "team_id": team_id, "goal_id": record["goal_id"], "terms": terms,
            "revision": revision, "dispatcher_id": record["dispatcher_id"], "registrar_id": actor.id,
            "source_message_id": record["source_message_id"], "proposal_id": record["id"],
            "proposal_version": record["version"], "created_at": _now(),
            "status": "waiting_dependencies" if terms["dependency_ids"] else "ready",
            "readers": sorted(self._parties(record["dispatcher_id"], terms) | {lead}),
        }
        if initial:
            self.db.execute("INSERT INTO delegation_tasks VALUES (?, ?, ?, ?)",
                            (task_id, team_id, record["goal_id"], revision))
        else:
            self.db.execute("UPDATE delegation_tasks SET revision = ? WHERE id = ?", (revision, task_id))
        self.db.execute("INSERT INTO delegation_revisions VALUES (?, ?, ?)", (task_id, revision, json.dumps(task)))
        event_type = "task.assigned" if initial else "task.updated"
        event_id = self._emit(team_id, event_type, task)
        message_id = self._notify_task(team_id, actor, lead, members, task, source, event_id, event_type)
        receipt = {"proposal_id": record["id"], "version": record["version"], "task_id": task_id,
                   "revision": revision, "event_id": event_id, "notification_message_id": message_id}
        record.update(status="registered", registration=receipt, registered_task_id=task_id)
        self._save_version(record)
        return receipt

    def _change(self, team_id, actor, lead, members, operation, proposal_id, body, terms, assistant):
        record = self._visible_proposal(team_id, actor, proposal_id)
        if assistant is not None:
            self._check_assistant_scope(assistant, record["task_id"], record["scope_id"])
        if body.expected_version != record["version"]:
            raise DelegationConflict("Proposal version changed", {
                "expected_version": body.expected_version, "current_version": record["version"],
            })
        if record["status"] == "registered" and operation in REGISTRATION_OPERATIONS:
            same_kind = (record["task_id"] is None) == (operation == "assign_task")
            if same_kind:
                return record["registration"]
        if record["status"] != "open":
            raise DelegationConflict("Proposal version is closed")
        _, source = self._goal(team_id, actor, record["goal_id"], active=True)
        if operation == "counter_proposal":
            return self._counter(team_id, actor, lead, members, record, body, terms, assistant)
        if operation == "confirm_proposal":
            return self._confirm(team_id, actor, members, record)
        if operation in {"reject_proposal", "withdraw_proposal"}:
            return self._close(team_id, actor, members, record, operation, body)
        if operation in REGISTRATION_OPERATIONS:
            return self._register(team_id, actor, lead, members, record, source, operation)
        raise CollaborationInvalid("Unknown delegation operation")

    def command(self, team_id, actor, operation, body, *, proposal_id=None, assistant=None):
        payload = body.model_dump(exclude={"request_id"})
        terms = body.terms.normalized() if hasattr(body, "terms") else None
        if terms is not None:
            payload["terms"] = terms
        caller_id = actor.id if assistant is None else assistant.session_id
        key = (team_id, caller_id, operation, body.request_id)
        digest = self.collaboration._digest({"proposal_id": proposal_id, "body": payload})
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            team = self.collaboration.team(team_id, actor)
            members = {item["member_id"]: Principal(item["member_id"], team_id, item["session_id"])
                       for item in team["members"]}
            if members.get(actor.id) != actor:
                raise CollaborationForbidden("A formal member is required")
            if assistant is not None and (
                assistant.parent_session_id != actor.session_id or assistant.team_id != team_id
                or operation not in DRAFT_OPERATIONS or operation not in assistant.allowed_tools
            ):
                raise CollaborationForbidden("Assistant cannot perform this operation")
            replay = self.collaboration._replay(key, digest)
            if replay is not None:
                record = self._visible_proposal(team_id, actor, replay["proposal_id"], replay["version"])
                if assistant is not None:
                    self._check_assistant_draft(assistant, record)
                return replay
            lead = team["lead_member_id"]
            try:
                if operation == "create_proposal":
                    receipt = self._create(team_id, actor, lead, members, body, terms, assistant)
                else:
                    receipt = self._change(team_id, actor, lead, members, operation, proposal_id, body, terms, assistant)
            except ArtifactNotFound as exc:
                raise TeamNotFound("Input version not found") from exc
            self.collaboration._save_receipt(key, digest, receipt)
            return receipt
