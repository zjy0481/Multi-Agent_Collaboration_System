"""Bind a human decision to a single Git push and reconcile uncertain results."""

import asyncio
import json
import logging
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, StringConstraints, ValidationError

from .collaboration import CollaborationForbidden, CollaborationInvalid, Command, ObjectId, RequestId
from .git_executor import GitActionError
from .local_auth import OPERATOR_ID, Principal
from .sandbox import SandboxError
from .teams import TeamConflict, TeamNotFound
from .workspace import WorkspaceNotFound

logger = logging.getLogger(__name__)
Repository = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")]
GitRef = Annotated[str, StringConstraints(min_length=12, max_length=200)]
CommitId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
GitPath = Annotated[str, StringConstraints(min_length=1, max_length=240)]


class CommitInput(Command):
    repository: Repository
    ref: GitRef
    paths: list[GitPath] = Field(min_length=1, max_length=32)
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class PushInput(Command):
    request_id: RequestId
    repository: Repository
    ref: GitRef
    sha: CommitId


class ApprovalInput(PushInput):
    action: Literal["push"]
    digest: Digest
    decision: Literal["approve", "reject"]


class ActionInput(Command):
    action_id: ObjectId


TOOLS = {
    "create_git_commit": (CommitInput, "Create a Git commit for a configured repository/ref using selected "
                          "relative paths in the active private workspace. This does not push."),
    "request_push": (PushInput, "Request the user's approval to push a commit from this work to the exact "
                     "configured repository and full refs/heads/ ref. Supply a stable request_id."),
    "get_git_action": (ActionInput, "Read the push or approval status for one of your Git actions."),
}
BINDING_FIELDS = ("action", "repository", "ref", "sha", "digest")


class GitApprovals:
    def __init__(self, runtime, collaboration, workspace, executor):
        self.runtime = runtime
        self.db = runtime.db
        self.chat = collaboration
        self.workspace = workspace
        self.executor = executor
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS git_commits (
                turn_id TEXT NOT NULL REFERENCES scheduled_turns(turn_id),
                repository TEXT NOT NULL,
                ref TEXT NOT NULL,
                sha TEXT NOT NULL,
                target_identity TEXT NOT NULL,
                PRIMARY KEY (turn_id, repository, ref, sha)
            );
            CREATE TABLE IF NOT EXISTS git_actions (
                action_id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL UNIQUE,
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                member_id TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES members(session_id),
                turn_id TEXT NOT NULL REFERENCES scheduled_turns(turn_id),
                call_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK (action = 'push'),
                repository TEXT NOT NULL,
                ref TEXT NOT NULL,
                sha TEXT NOT NULL,
                target_identity TEXT NOT NULL,
                digest TEXT NOT NULL,
                status TEXT NOT NULL,
                notice_id TEXT NOT NULL REFERENCES team_messages(id),
                decision TEXT,
                decision_request_id TEXT,
                attempt TEXT,
                UNIQUE (session_id, request_id)
            );
            CREATE INDEX IF NOT EXISTS git_actions_status ON git_actions(status);
        """)

    def _view(self, row):
        fields = ("action_id", "approval_id", "member_id", "turn_id", "call_id", "status", *BINDING_FIELDS)
        view = {key: row[key] for key in fields}
        remote = self.executor.remote_repository(row["repository"], row["target_identity"])
        if remote is not None:
            view["remote_repository"] = remote
        return view

    def list_pending(self, team_id, actor):
        self.chat.team(team_id, actor)
        return [self._view(row) for row in self.db.execute("""
            SELECT * FROM git_actions WHERE team_id = ? AND status = 'awaiting_approval' ORDER BY rowid
        """, (team_id,))]

    def get_action(self, team_id, actor, action_id):
        self.chat.team(team_id, actor)
        row = self.db.execute("""
            SELECT * FROM git_actions WHERE team_id = ? AND action_id = ? AND (? OR session_id = ?)
        """, (team_id, action_id, actor.is_operator, actor.session_id)).fetchone()
        if row is None:
            raise TeamNotFound("Git action not found")
        return self._view(row)

    def tools_for(self, role, scheduled):
        if self.chat.auth.member(role) is None:
            return []
        return [{"type": "function", "function": {
            "name": name, "description": description, "parameters": schema.model_json_schema(),
        }} for name, (schema, description) in TOOLS.items() if scheduled or name == "get_git_action"]

    @staticmethod
    def handles(name):
        return name in TOOLS

    def _delivery(self, turn_id):
        return self.db.execute("""
            SELECT d.* FROM scheduled_turns s JOIN message_deliveries d USING (event_id, session_id, purpose)
            WHERE s.turn_id = ?
        """, (turn_id,)).fetchone()

    async def execute(self, role, turn_id, generation, call_id, name, raw_arguments):
        actor = self.chat.auth.member(role)
        if actor is None:
            return {"ok": False, "error": "Git actions require a formal member"}
        try:
            args = TOOLS[name][0].model_validate_json(raw_arguments)
            if name == "get_git_action":
                result = self.get_action(actor.team_id, actor, args.action_id)
            else:
                delivery = self._delivery(turn_id)
                if delivery is None or delivery["session_id"] != role:
                    raise CollaborationForbidden("Git writes require currently scheduled work")
                if name == "request_push":
                    result = self._request(actor, turn_id, generation, call_id, delivery, args)
                else:
                    result = await self._create_commit(actor, turn_id, generation, delivery, args)
            return {"ok": True, "result": result}
        except ValidationError:
            return {"ok": False, "error": "Invalid Git tool arguments"}
        except (CollaborationForbidden, CollaborationInvalid, TeamConflict, TeamNotFound,
                WorkspaceNotFound, GitActionError) as exc:
            return {"ok": False, "error": str(exc)}

    async def _create_commit(self, actor, turn_id, generation, delivery, args):
        if delivery["status"] != "running" or not self.runtime.journal.current(turn_id, generation):
            raise CollaborationForbidden("The work is no longer active")
        workspace = self.workspace.actor(actor.session_id)
        if len(set(args.paths)) != len(args.paths):
            raise CollaborationInvalid("Duplicate commit paths")
        files = {}
        for path in args.paths:
            try:
                files[path] = await asyncio.to_thread(self.workspace.sandbox.read_bytes, workspace.workspace_id, path)
            except (ValueError, OSError, SandboxError) as exc:
                raise GitActionError("Could not read the selected workspace file") from exc
        if not self.runtime.journal.current(turn_id, generation):
            raise CollaborationForbidden("The execution generation changed")
        target = self.executor.target_identity(args.repository)
        sha = await asyncio.to_thread(self.executor.create_commit, actor.session_id, args.repository, args.ref,
                                      files, args.message)
        if self.executor.target_identity(args.repository) != target:
            raise GitActionError("The configured Git repository changed during commit creation")
        self.workspace.actor(actor.session_id)
        with self.db:
            if not self.runtime.journal.current(turn_id, generation):
                raise CollaborationForbidden("The execution generation changed")
            self.db.execute("INSERT OR IGNORE INTO git_commits VALUES (?, ?, ?, ?, ?)",
                            (turn_id, args.repository, args.ref, sha, target))
        return {"repository": args.repository, "ref": args.ref, "sha": sha}

    def _request(self, actor, turn_id, generation, call_id, delivery, args):
        binding = {"action": "push", **args.model_dump(exclude={"request_id"})}
        digest = self.chat._digest(binding)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not self.runtime.journal.current(turn_id, generation):
                raise CollaborationForbidden("The execution generation changed")
            existing = self.db.execute("SELECT * FROM git_actions WHERE session_id = ? AND request_id = ?",
                                       (actor.session_id, args.request_id)).fetchone()
            if existing:
                if existing["digest"] != digest:
                    raise TeamConflict("The request ID already binds different Git parameters")
                return {**self._view(existing), "pause": existing["turn_id"] == turn_id}
            commit = self.db.execute("""
                SELECT target_identity FROM git_commits WHERE turn_id = ? AND repository = ? AND ref = ? AND sha = ?
            """, (turn_id, args.repository, args.ref, args.sha)).fetchone()
            if delivery["status"] != "running" or commit is None:
                raise CollaborationForbidden("Use a commit created for this repository and ref in the active work")

            action_id, approval_id = uuid4().hex, uuid4().hex
            notice = self.chat._create_message(
                actor.team_id, actor, recipient=OPERATOR_ID, text=f"Git push needs approval ({approval_id}).",
                kind="approval", mentions=[], reply_to=None,
            )
            self.db.execute("""
                INSERT INTO git_actions(action_id, approval_id, team_id, member_id, session_id, turn_id, call_id,
                    request_id, action, repository, ref, sha, target_identity, digest, status, notice_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'push', ?, ?, ?, ?, ?, 'awaiting_approval', ?)
            """, (action_id, approval_id, actor.team_id, actor.id, actor.session_id, turn_id, call_id, args.request_id,
                  args.repository, args.ref, args.sha, commit["target_identity"], digest, notice["message_id"]))
            self.db.execute("""UPDATE message_deliveries SET status = 'blocked'
                               WHERE (event_id, session_id, purpose) = (?, ?, ?)""",
                            (delivery["event_id"], delivery["session_id"], delivery["purpose"]))
            return {**self._view(self._action(action_id)), "pause": True}

    def _action(self, action_id):
        return self.db.execute("SELECT * FROM git_actions WHERE action_id = ?", (action_id,)).fetchone()

    def decide(self, approval_id, args):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM git_actions WHERE approval_id = ?", (approval_id,)).fetchone()
            if row is None:
                raise TeamNotFound("Push approval not found")
            if any(row[key] != getattr(args, key) for key in BINDING_FIELDS):
                raise TeamConflict("The decision must match the original action, repository, ref, SHA and digest")
            if row["decision"] is not None:
                if row["decision"] != args.decision:
                    raise TeamConflict("This push already has a different decision")
                return {**self._view(row), "status": "approved" if args.decision == "approve" else "rejected"}
            # Validate the scope again immediately before this approval becomes executable.
            delivery = self._delivery(row["turn_id"])
            if delivery is None or delivery["status"] != "blocked":
                raise TeamConflict("This work is no longer waiting for approval")
            status = "approved" if args.decision == "approve" else "rejected"
            self.db.execute("""UPDATE git_actions SET status = ?, decision = ?, decision_request_id = ?
                               WHERE action_id = ?""", (status, args.decision, args.request_id, row["action_id"]))
            if status == "rejected":
                self._notify(row, status)
            return self._view(self._action(row["action_id"]))

    def _notify(self, row, status):
        principal = self.chat.auth.member(row["session_id"])
        if principal is None or principal.team_id != row["team_id"]:
            return
        delivery = self._delivery(row["turn_id"])
        if delivery is None or delivery["status"] not in {"blocked", "resolved"}:
            return
        data = {"action_id": row["action_id"], "status": status, "repository": row["repository"],
                "ref": row["ref"], "sha": row["sha"]}
        notice = self.chat._create_message(row["team_id"], Principal(OPERATOR_ID), recipient=principal.id,
                                           text="Git push outcome: " + json.dumps(data), kind="approval",
                                           mentions=[], reply_to=row["notice_id"])
        self.chat._deliver(notice, (row["session_id"],), "git.result")
        self.db.execute("""UPDATE message_deliveries SET status = 'resolved'
                           WHERE (event_id, session_id, purpose) = (?, ?, ?) AND status = 'blocked'""",
                        (delivery["event_id"], delivery["session_id"], delivery["purpose"]))

    async def _process(self, row, *, observe):
        action_id = row["action_id"]
        attempt = uuid4().hex
        with self.db:
            claim = self.db.execute("""UPDATE git_actions SET status = 'executing', attempt = ?
                                       WHERE action_id = ? AND status = ? AND attempt IS ?""",
                                    (attempt, action_id, row["status"], row["attempt"]))
            if claim.rowcount != 1:
                return
        status = "failed"
        try:
            same_target = self.executor.target_identity(row["repository"]) == row["target_identity"]
            if not same_target:
                status = "pending_verification" if observe else "failed"
            elif observe:
                status = await asyncio.to_thread(self.executor.observe, row["repository"], row["ref"], row["sha"])
            else:
                delivery = self._delivery(row["turn_id"])
                actor = self.workspace.actor(row["session_id"])
                if delivery and delivery["status"] == "blocked" and actor.team_id == row["team_id"]:
                    status = await asyncio.to_thread(self.executor.push, row["session_id"], row["repository"],
                                                     row["ref"], row["sha"])
                    if status in {"succeeded", "pending_verification"}:
                        status = await asyncio.to_thread(self.executor.observe, row["repository"], row["ref"], row["sha"])
        except WorkspaceNotFound:
            pass
        except (GitActionError, OSError):
            status = "pending_verification"
        if status not in {"failed", "succeeded", "pending_verification"}:
            status = "pending_verification"
        with self.db:
            updated = self.db.execute("""UPDATE git_actions SET status = ?
                                        WHERE action_id = ? AND status = 'executing' AND attempt = ?""",
                                     (status, action_id, attempt))
            if updated.rowcount == 1 and status != row["status"]:
                self._notify(row, status)

    async def run(self):
        # After restart verify actions that might have reached the remote. Never push them again.
        uncertain = self.db.execute("""SELECT * FROM git_actions
                                      WHERE status IN ('executing', 'pending_verification') ORDER BY rowid""").fetchall()
        for row in uncertain:
            await self._process(row, observe=True)
        while True:
            row = self.db.execute("SELECT * FROM git_actions WHERE status = 'approved' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                await asyncio.sleep(0.10)
            else:
                await self._process(row, observe=False)
