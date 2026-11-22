"""Immutable task submissions, decisions, and current acceptance facts."""

import json
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, StrictBool

from .artifacts import ArtifactError, ArtifactNotFound
from .collaboration import CollaborationForbidden, CollaborationInvalid, Command, ObjectId, RequestId, Text, _now
from .local_auth import Principal
from .teams import TeamConflict, TeamNotFound


Positive = Annotated[int, Field(ge=1, strict=True)]
Evidence = Annotated[list[Text], Field(min_length=1, max_length=32)]
Notes = Annotated[list[Text], Field(max_length=32)]


class SubmitResultInput(Command):
    request_id: RequestId
    expected_revision: Positive
    version_ids: Annotated[list[ObjectId], Field(min_length=1, max_length=100)]
    evidence: Evidence
    open_issues: Notes = Field(default_factory=list)


class ReviewFinding(Command):
    criterion: Positive
    met: StrictBool
    evidence: Text


class ReviewResultInput(Command):
    request_id: RequestId
    expected_revision: Positive
    decision: Literal["approved", "returned"]
    findings: Annotated[list[ReviewFinding], Field(min_length=1, max_length=32)]
    expected: Text | None = None
    recheck_items: Notes = Field(default_factory=list)


class TaskResults:
    def __init__(self, delegation):
        self.delegation = delegation
        self.collaboration = delegation.collaboration
        self.artifacts = delegation.artifacts
        self.db = delegation.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS task_submissions (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                task_revision INTEGER NOT NULL,
                data TEXT NOT NULL,
                FOREIGN KEY (task_id, task_revision) REFERENCES delegation_revisions(task_id, revision)
            );
            CREATE INDEX IF NOT EXISTS task_submissions_task ON task_submissions(task_id);
            CREATE TABLE IF NOT EXISTS task_reviews (
                submission_id TEXT PRIMARY KEY REFERENCES task_submissions(id),
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_superseded_versions (
                task_id TEXT NOT NULL REFERENCES delegation_tasks(id),
                version_id TEXT NOT NULL REFERENCES artifact_versions(version_id),
                PRIMARY KEY (task_id, version_id)
            );
        """)

    @staticmethod
    def _record(row):
        return {**json.loads(row["submission"]), "review": json.loads(row["review"]) if row["review"] else None}

    def _latest(self, task_id, revision=None):
        row = self.db.execute("""
            SELECT s.data AS submission, r.data AS review FROM task_submissions s
            LEFT JOIN task_reviews r ON r.submission_id = s.id
            WHERE s.task_id = ? AND s.task_revision = COALESCE(?, s.task_revision)
            ORDER BY s.rowid DESC LIMIT 1
        """, (task_id, revision)).fetchone()
        return self._record(row) if row else None

    def _stale_inputs(self, task):
        pinned = set(task["terms"]["input_version_ids"])
        if not pinned:
            return []
        rows = self.db.execute("""
            SELECT s.version_id FROM task_superseded_versions s JOIN delegation_tasks t ON s.task_id = t.id
            WHERE t.goal_id = ? AND t.team_id = ? AND t.id <> ?
        """, (task["goal_id"], task["team_id"], task["id"]))
        return sorted(pinned.intersection(row["version_id"] for row in rows))

    @staticmethod
    def _status(task, latest, stale):
        if stale:
            return "needs_recheck"
        if latest is None:
            return task["status"]
        if latest["review"] is None:
            return "pending_review"
        return "accepted" if latest["review"]["decision"] == "approved" else "returned"

    def status(self, task):
        return self._status(task, self._latest(task["id"], task["revision"]), self._stale_inputs(task))

    def _can_read_versions(self, team_id, actor, submission):
        try:
            return all(actor.session_id in self.artifacts.reference_readers(team_id, version["version_id"])
                       for version in submission["versions"])
        except ArtifactNotFound:
            return False

    def event_reference(self, actor, task):
        reference = task.get("result_reference")
        if reference is None:
            return {}
        row = self.db.execute("SELECT data FROM task_submissions WHERE task_id = ? AND id = ?",
                              (task["id"], reference["submission_id"])).fetchone()
        if row and self._can_read_versions(task["team_id"], actor, json.loads(row["data"])):
            return {"result_reference": reference}
        return {}

    def view(self, actor, task, revision):
        team_id, task_id = task["team_id"], task["id"]
        latest = self._latest(task_id, task["revision"])
        stale = self._stale_inputs(task)
        status = self._status(task, latest, stale)
        rows = self.db.execute("""
            SELECT s.data AS submission, r.data AS review FROM task_submissions s
            LEFT JOIN task_reviews r ON r.submission_id = s.id
            WHERE s.task_id = ? ORDER BY s.rowid
        """, (task_id,))
        visible = []
        for row in rows:
            record = self._record(row)
            basis = self.delegation._task(team_id, task_id, record["task_revision"])
            if self.delegation._can_read(team_id, actor, basis) and self._can_read_versions(team_id, actor, record):
                visible.append(record)
        active = revision is None or task["revision"] == self.delegation._task(team_id, task_id)["revision"]
        current_id = latest["id"] if active and latest in visible else None
        readable_reviews = {record["review"]["id"] for record in visible if record["review"] is not None}
        submissions = [record if record["previous_return_id"] in readable_reviews else {**record, "previous_return_id": None}
                       for record in visible if revision is None or record["task_revision"] == revision]
        return {"status": status, "result": {"current_submission_id": current_id,
                                              "stale_input_versions": [self._version_view(self.artifacts.reference(team_id, version_id))
                                                                       for version_id in stale],
                                              "submissions": submissions}}

    def current_acceptance(self, team_id, actor, goal_id, task_id):
        """Return usable current acceptance or None; closure checks other blockers in its transaction."""
        try:
            self.collaboration.team(team_id, actor)
            task = self.delegation._visible_task(team_id, actor, task_id)
            if task["goal_id"] != goal_id or self._stale_inputs(task):
                return None
            latest = self._latest(task_id, task["revision"])
            if self._status(task, latest, []) != "accepted":
                return None
            for version in latest["versions"]:
                self.artifacts.resolve(team_id, actor.session_id, version["version_id"])
            return {key: value for key, value in latest.items() if key != "previous_return_id"}
        except (ArtifactError, TeamNotFound):
            return None

    def _notify(self, task, actor, members, recipients, event_id, kind, text):
        messages = []
        team_id = task["team_id"]
        for recipient_id in dict.fromkeys(recipients):
            try:
                self.collaboration._message_for(team_id, members[recipient_id], task["source_message_id"])
            except TeamNotFound:
                continue
            messages.append(self.delegation._notify(
                team_id, actor, event_id, recipient=recipient_id, targets=[members[recipient_id].session_id],
                text=text, mentions=[], purpose=kind,
            ))
        return messages

    def _event(self, task, status, kind, **reference):
        record = {**task, "status": status}
        if reference:
            record["result_reference"] = reference
        return self.delegation._emit(task["team_id"], kind, record)

    def _invalidate(self, task, retired):
        if not retired:
            return
        for version_id in sorted(retired):
            self.db.execute("INSERT OR IGNORE INTO task_superseded_versions VALUES (?, ?)", (task["id"], version_id))
        rows = self.db.execute("SELECT id FROM delegation_tasks WHERE team_id = ? AND goal_id = ? AND id <> ? ORDER BY rowid",
                               (task["team_id"], task["goal_id"], task["id"]))
        for row in rows:
            downstream = self.delegation._task(task["team_id"], row["id"])
            if retired.intersection(downstream["terms"]["input_version_ids"]):
                self._event(downstream, "needs_recheck", "task.needs_recheck")

    def _versions(self, team_id, actor, reviewer, version_ids):
        versions = []
        for version_id in sorted(set(version_ids)):
            try:
                version = self.artifacts.reference(team_id, version_id)
                readers = {version["author_session_id"], *version["reader_session_ids"]}
                if actor.session_id not in readers:
                    raise ArtifactNotFound("Artifact not visible")
            except ArtifactNotFound as exc:
                raise TeamNotFound("Result version not found") from exc
            if reviewer.session_id not in readers:
                raise CollaborationForbidden("The designated reviewer cannot read a result version")
            versions.append(self._version_view(version))
        if len({version['artifact_id'] for version in versions}) != len(versions):
            raise CollaborationInvalid("Choose one version of each artifact")
        return versions

    @staticmethod
    def _version_view(version):
        return {**{key: version[key] for key in ("version_id", "artifact_id", "version", "source_path", "sha256")},
                "download_url": f"/v1/artifacts/{version['artifact_id']}/versions/{version['version']}"}

    def _submit(self, task, actor, members, lead, body):
        latest = self._latest(task["id"])
        review = latest["review"] if latest else None
        previous_ids = {version["version_id"] for version in latest["versions"]} if latest else set()
        new_ids = set(body.version_ids)
        if latest and latest["task_revision"] == task["revision"]:
            if review is None or review["decision"] != "returned":
                raise TeamConflict("Only a returned submission can be replaced on the same task revision")
        if review and review["decision"] == "returned" and not new_ids - previous_ids:
            raise TeamConflict("A replacement must include a new result version")
        retired = self.db.execute("SELECT version_id FROM task_superseded_versions WHERE task_id = ?", (task["id"],))
        if new_ids.intersection(row["version_id"] for row in retired):
            raise TeamConflict("A superseded result version cannot become current again")
        reviewer_id = task["terms"]["reviewer_id"]
        versions = self._versions(task["team_id"], actor, members[reviewer_id], body.version_ids)
        kind = "task.submitted"
        submission_id = uuid4().hex
        event_id = self._event(task, "pending_review", kind, submission_id=submission_id)
        record = {"id": submission_id, "task_id": task["id"], "task_revision": task["revision"],
                  "owner_id": actor.id, "created_at": _now(), "versions": versions, "event_id": event_id,
                  "evidence": body.evidence, "open_issues": body.open_issues,
                  "previous_return_id": review["id"] if review and review["decision"] == "returned" else None}
        self.db.execute("INSERT INTO task_submissions VALUES (?, ?, ?, ?)",
                        (record["id"], task["id"], task["revision"], json.dumps(record)))
        notifications = self._notify(task, actor, members, (reviewer_id, lead, task["dispatcher_id"]),
                                     event_id, kind, "任务结果已提交")
        self._invalidate(task, previous_ids - new_ids)
        return {"submission_id": record["id"], "event_id": event_id, "notification_message_ids": notifications}

    def _review(self, task, actor, members, lead, submission_id, body):
        latest = self._latest(task["id"], task["revision"])
        if latest is None or latest["id"] != submission_id or latest["review"] is not None:
            raise TeamConflict("The submission is no longer pending review")
        if not self._can_read_versions(task["team_id"], actor, latest):
            raise TeamNotFound("Result version not found")
        criteria = task["terms"]["acceptance_criteria"]
        findings = sorted(body.findings, key=lambda finding: finding.criterion)
        if [finding.criterion for finding in findings] != list(range(1, len(criteria) + 1)):
            raise CollaborationInvalid("Provide exactly one finding for each acceptance criterion")
        all_met = all(finding.met for finding in findings)
        if body.decision == "returned":
            if all_met or not body.expected or not body.recheck_items:
                raise CollaborationInvalid("A return requires an evidenced gap, expectations, and recheck items")
        elif not all_met or body.expected is not None or body.recheck_items:
            raise CollaborationInvalid("Approval requires all criteria to pass and no return instructions")
        approved = body.decision == "approved"
        kind = "task.approved" if approved else "task.returned"
        review_id = uuid4().hex
        event_id = self._event(task, "accepted" if approved else "returned", kind,
                               submission_id=submission_id, review_id=review_id)
        record = {"id": review_id, "submission_id": submission_id, "reviewer_id": actor.id, "event_id": event_id,
                  "created_at": _now(), "decision": body.decision, "expected": body.expected,
                  "recheck_items": body.recheck_items,
                  "findings": [{**finding.model_dump(), "requirement": criteria[finding.criterion - 1]}
                               for finding in findings]}
        self.db.execute("INSERT INTO task_reviews VALUES (?, ?)", (submission_id, json.dumps(record)))
        notifications = self._notify(task, actor, members, (task["terms"]["owner_id"], lead, task["dispatcher_id"]),
                                     event_id, kind, "任务审查通过" if approved else "任务审查退回")
        return {"submission_id": submission_id, "review_id": record["id"], "event_id": event_id,
                "notification_message_ids": notifications}

    def command(self, team_id, actor, task_id, body, *, submission_id=None):
        reviewing = isinstance(body, ReviewResultInput)
        operation = "review_result" if reviewing else "submit_result"
        key = (team_id, actor.id, operation, body.request_id)
        digest = self.collaboration._digest({"task_id": task_id, "submission_id": submission_id,
                                             "body": body.model_dump(exclude={"request_id"})})
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            team = self.collaboration.team(team_id, actor)
            members = {item["member_id"]: Principal(item["member_id"], team_id, item["session_id"])
                       for item in team["members"]}
            if members.get(actor.id) != actor:
                raise CollaborationForbidden("A formal member is required")
            task = self.delegation._visible_task(team_id, actor, task_id)
            replay = self.collaboration._replay(key, digest)
            if replay is not None:
                return replay
            role = "reviewer_id" if reviewing else "owner_id"
            if actor.id != task["terms"][role]:
                raise CollaborationForbidden("Only the designated reviewer may review" if reviewing else
                                             "Only the current owner may submit")
            self.delegation._goal(team_id, actor, task["goal_id"], active=True)
            if body.expected_revision != task["revision"]:
                raise TeamConflict("Task revision changed")
            if self._stale_inputs(task):
                raise TeamConflict("Task inputs require rechecking before submitting or reviewing")
            if reviewing:
                receipt = self._review(task, actor, members, team["lead_member_id"], submission_id, body)
            else:
                receipt = self._submit(task, actor, members, team["lead_member_id"], body)
            self.collaboration._save_receipt(key, digest, receipt)
            return receipt
