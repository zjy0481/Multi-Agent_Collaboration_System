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
    evidence_versions: dict[ObjectId, Annotated[list[ObjectId], Field(min_length=1, max_length=100)]] = Field(
        default_factory=dict, max_length=100, description="Source tasks and exact output versions cited as evidence.")


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
            CREATE TABLE IF NOT EXISTS task_dependency_basis (
                submission_id TEXT PRIMARY KEY REFERENCES task_submissions(id), outputs TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_ready_notices (
                task_id TEXT NOT NULL, revision INTEGER NOT NULL, basis TEXT NOT NULL,
                PRIMARY KEY(task_id, revision, basis)
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

    def visible_outputs(self, source, actor, versions):
        grants = source["terms"].get("output_grants", {}).get(actor.id, [])
        full = self.delegation._can_read(source["team_id"], actor, source)
        return [v for v in versions if (full or v["source_path"] in grants)
                and actor.session_id in self.artifacts.reference_readers(source["team_id"], v["version_id"])]

    def dependency_handoff(self, task, actor):
        outputs, blockers, guidance = {}, {}, {}
        for dependency in task["terms"]["dependency_ids"]:
            source = self.delegation._task(task["team_id"], dependency)
            paths = source["terms"].get("output_grants", {}).get(actor.id)
            if paths is None and not self.delegation._can_read(task["team_id"], actor, source):
                blockers[dependency] = "handoff_unavailable"
                continue
            fact = self.current_acceptance_fact(task["team_id"], task["goal_id"], dependency)
            if fact:
                visible = self.visible_outputs(source, actor, fact["versions"])
                complete = (len(visible) == len(fact["versions"]) if paths is None
                            else set(paths) <= {v["source_path"] for v in visible})
                outputs[dependency] = sorted(v["version_id"] for v in visible
                                             if paths is None or v["source_path"] in paths)
                if not complete:
                    blockers[dependency] = "publisher_action_required"
                    guidance[dependency] = ("Ask the dispatcher to compare exact output_grants paths with "
                                            "approved source paths and correct the grant if needed; "
                                            "republish only when reader access is missing.")
                    # Only source-task readers may see approved paths, and only when
                    # the artifact itself is readable. Basenames never authorize access.
                    if paths is not None and self.delegation._can_read(task["team_id"], actor, source):
                        matches = [(grant, v["source_path"]) for grant in paths for v in fact["versions"]
                                   if grant != v["source_path"]
                                   and grant.rsplit("/", 1)[-1] == v["source_path"].rsplit("/", 1)[-1]
                                   and actor.session_id in self.artifacts.reference_readers(
                                       task["team_id"], v["version_id"])]
                        if matches:
                            guidance[dependency] += " Exact path candidates: " + ", ".join(
                                f"{grant} -> {actual}" for grant, actual in sorted(set(matches))[:5]) + "."
            else:
                blockers[dependency] = "awaiting_approval"
        result = {"dependency_outputs": outputs, "dependency_blockers": blockers}
        if guidance:
            result["dependency_guidance"] = guidance
        return result

    def _owner_outputs(self, task):
        if not task["terms"]["dependency_ids"]:
            return {}
        team = self.collaboration.teams.get(task["team_id"])
        owner = next(m for m in team["members"] if m["member_id"] == task["terms"]["owner_id"])
        actor = Principal(owner["member_id"], task["team_id"], owner["session_id"])
        handoff = self.dependency_handoff(task, actor)
        return {dep: versions for dep, versions in handoff["dependency_outputs"].items()
                if dep not in handoff["dependency_blockers"]}

    def _dependencies_ready(self, task, submission=None):
        if not task["terms"]["dependency_ids"]:
            return True
        outputs = self._owner_outputs(task)
        if set(task["terms"]["dependency_ids"]) != outputs.keys():
            return False
        if submission is not None:
            basis = self.db.execute("SELECT outputs FROM task_dependency_basis WHERE submission_id=?",
                                    (submission["id"],)).fetchone()
            if basis is None or json.loads(basis["outputs"]) != outputs:
                return False
        return True

    def mark_ready_notice(self, task):
        basis = self.collaboration._digest(self._owner_outputs(task))
        row = self.db.execute("INSERT OR IGNORE INTO task_ready_notices VALUES (?, ?, ?)",
                              (task["id"], task["revision"], basis))
        return bool(row.rowcount)

    def _status(self, task, latest, stale):
        if stale:
            return "needs_recheck"
        if not self._dependencies_ready(task, latest):
            return "waiting_dependencies" if latest is None else "needs_recheck"
        if latest is None:
            return "ready" if task["terms"]["dependency_ids"] else task["status"]
        if latest["review"] is None:
            return "pending_review"
        return "accepted" if latest["review"]["decision"] == "approved" else "returned"

    def status(self, task):
        return self._status(task, self._latest(task["id"], task["revision"]), self._stale_inputs(task))

    def _can_read_versions(self, team_id, actor, submission):
        try:
            for source_id, ids in submission.get("evidence_versions", {}).items():
                source = self.delegation._task(team_id, source_id)
                versions = [self.artifacts.reference(team_id, vid) for vid in ids]
                if len(self.visible_outputs(source, actor, versions)) != len(ids):
                    return False
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
        access = "available" if current_id else "no_current_submission"
        if latest and latest not in visible:
            access = "publisher_action_required"
        return {
            "status": status, **self.dependency_handoff(task, actor),
            "result": {
                "current_submission_id": current_id, "access": access,
                "stale_input_versions": [self._version_view(self.artifacts.reference(team_id, v)) for v in stale],
                "submissions": submissions,
            },
        }

    def current_acceptance(self, team_id, actor, goal_id, task_id):
        """Return usable current acceptance or None; closure checks other blockers in its transaction."""
        try:
            self.collaboration.team(team_id, actor)
            task = self.delegation._visible_task(team_id, actor, task_id)
            if task["goal_id"] != goal_id or self._stale_inputs(task):
                return None
            latest = self._latest(task_id, task["revision"])
            if self._status(task, latest, []) != "accepted" or not self._can_read_versions(team_id, actor, latest):
                return None
            for version in latest["versions"]:
                self.artifacts.resolve(team_id, actor.session_id, version["version_id"])
            return {key: value for key, value in latest.items() if key != "previous_return_id"}
        except (ArtifactError, TeamNotFound):
            return None

    def current_acceptance_fact(self, team_id, goal_id, task_id):
        """Return current acceptance without applying one reader's artifact ACL."""
        try:
            task = self.delegation._task(team_id, task_id)
            if task["goal_id"] != goal_id or self._stale_inputs(task):
                return None
            latest = self._latest(task_id, task["revision"])
            if self._status(task, latest, []) != "accepted":
                return None
            for version in latest["versions"]:
                metadata = self.artifacts.reference(team_id, version["version_id"])
                self.artifacts.resolve(team_id, metadata["author_session_id"], version["version_id"])
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

    def evidence_conflicts(self, task, submission, actor):
        conflicts = []
        for source_id, cited in submission.get("evidence_versions", {}).items():
            source = self.delegation._task(task["team_id"], source_id)
            current = self.current_acceptance_fact(task["team_id"], task["goal_id"], source_id)
            if current is None:
                conflicts.append("Evidence source needs current approval")
                continue
            missing = set(cited) - {v["version_id"] for v in current["versions"]}
            if missing:
                if self._can_read_versions(task["team_id"], actor, submission):
                    visible = [v["version_id"] for v in self.visible_outputs(source, actor, current["versions"])]
                    conflicts.append(f"Cited {sorted(missing)}; current approved versions {visible}")
                else:
                    conflicts.append("Evidence edition requires publisher action")
        return conflicts

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
        if body.evidence_versions:
            for source_id in body.evidence_versions:
                try:
                    source = self.delegation._task(task["team_id"], source_id)
                except TeamNotFound:
                    raise TeamNotFound("Evidence source unavailable") from None
                if source_id == task["id"] or source["goal_id"] != task["goal_id"]:
                    raise TeamNotFound("Evidence source unavailable")
            record["evidence_versions"] = body.evidence_versions
            if not all(self._can_read_versions(task["team_id"], reader, record) for reader in (actor, members[reviewer_id])):
                raise TeamNotFound("Evidence source unavailable")
        self.db.execute("INSERT INTO task_submissions VALUES (?, ?, ?, ?)",
                        (record["id"], task["id"], task["revision"], json.dumps(record)))
        if task["terms"]["dependency_ids"]:
            self.db.execute("INSERT INTO task_dependency_basis VALUES (?, ?)",
                            (submission_id, json.dumps(self._owner_outputs(task), sort_keys=True)))
        notifications = self._notify(task, actor, members, (reviewer_id, lead, task["dispatcher_id"]),
                                     event_id, kind, "任务结果已提交")
        self._invalidate(task, previous_ids - new_ids)
        receipt = {"submission_id": record["id"], "event_id": event_id, "notification_message_ids": notifications}
        agreement = self.collaboration.goal_lifecycle._acceptance(task["goal_id"])
        if agreement and any(item["task_id"] == task["id"] for item in agreement["required_deliveries"]):
            receipt["handoff"] = "ready" if self._can_read_versions(task["team_id"], members[lead], record) else "lead_access_required"
        return receipt

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
        if approved:
            conflicts = self.evidence_conflicts(task, latest, actor)
            if conflicts:
                raise TeamConflict("Evidence version mismatch: " + "; ".join(conflicts))
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
        if approved:
            rows = self.db.execute("SELECT id FROM delegation_tasks WHERE team_id = ? AND goal_id = ? AND id <> ?",
                                   (task["team_id"], task["goal_id"], task["id"])).fetchall()
            for row in rows:
                downstream = self.delegation._task(task["team_id"], row["id"])
                if (task["id"] not in downstream["terms"]["dependency_ids"]
                        or self._latest(downstream["id"], downstream["revision"]) is not None
                        or self.status(downstream) != "ready"):
                    continue
                owner = members[downstream["terms"]["owner_id"]]
                try:
                    for version_id in downstream["terms"]["input_version_ids"]:
                        self.artifacts.resolve(task["team_id"], owner.session_id, version_id)
                except ArtifactError:
                    continue
                if not self.mark_ready_notice(downstream):
                    continue
                ready_event = self._event(downstream, "ready", "task.ready")
                self._notify(downstream, actor, members, [owner.id], ready_event, "task.ready", "前置任务已通过，任务可以继续")
        return {"submission_id": submission_id, "review_id": record["id"], "event_id": event_id,
                "notification_message_ids": notifications}

    def command(self, team_id, actor, task_id, body, *, submission_id=None):
        reviewing = isinstance(body, ReviewResultInput)
        operation = "review_result" if reviewing else "submit_result"
        key = (team_id, actor.id, operation, body.request_id)
        payload = body.model_dump(exclude={"request_id"})
        if not payload.get("evidence_versions"):
            payload.pop("evidence_versions", None)
        digest = self.collaboration._digest({"task_id": task_id, "submission_id": submission_id, "body": payload})
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
            pending = self._latest(task_id, task["revision"]) if reviewing else None
            if (not reviewing or body.decision == "approved") and not self._dependencies_ready(task, pending):
                raise TeamConflict("Upstream dependencies need current approval and readable granted outputs. "
                                   "For a submission with a missing or changed dependency basis, return it for recheck.")
            if reviewing:
                receipt = self._review(task, actor, members, team["lead_member_id"], submission_id, body)
            else:
                receipt = self._submit(task, actor, members, team["lead_member_id"], body)
            self.collaboration._save_receipt(key, digest, receipt)
            return receipt
