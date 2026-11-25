"""Run the unmodified official assignments against the local platform."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.collaboration import QUESTION_ASSIGNMENTS
from collaboration_runtime.model import load_model
from collaboration_runtime.sandbox import DockerSandbox
from collaboration_runtime.search import SearchClient
from handoff import prepare_software_state
from recovery import retry_key
from software_handoff import audit_t1_handoff, audit_t2_handoff


SCENARIOS = {"software": "scenario_1*", "newsroom": "scenario_2*", "fund": "scenario_3*"}
END_STATES = {"completed", "failed", "cancelled", "timed_out"}
TASK_DELIVERIES = {"task.assigned", "task.updated", "task.ready", "task.returned", "task.submitted", "clarification.resume", "clarification.correction"}
RECOVERABLE_ERRORS = {"Turn exceeded its model step limit", "Course API request timed out"}


def sequential_plan():
    return [('software', (1, 2)), ('fund', (1,)), ('newsroom', (1,))]


def run_sequential(run_scenario):
    outcomes = {}
    for name, phases in sequential_plan():
        outcomes[name] = run_scenario(name, phases)
        if outcomes[name] != 'finished':
            break
    return outcomes


TABLES = ("team_messages", "team_goals", "goal_terminal_facts", "human_questions",
          "message_deliveries", "turns", "scheduled_turns", "execution_receipts", "events",
          "artifact_versions", "delegation_tasks", "delegation_revisions", "delegation_proposals",
          "delegation_notifications", "delegation_events", "goal_acceptances", "workspace_sources",
          "task_submissions", "task_reviews", "workspace_searches", "experience_lessons",
          "experience_handoffs", "experience_retrievals", "experience_uses", "experience_observations")


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prompt_text(path):
    raw = path.read_text(encoding="utf-8")
    return re.search(r"```text\s*\n(.*?)\n```", raw, re.S).group(1)


def now():
    return datetime.now(timezone.utc).isoformat()


def operator_answer(scenario, phase, question):
    if scenario == "software":
        if phase == 1:
            return ("For this course exercise the app should run locally without external dependencies. Use three rooms "
                    "and Hong Kong local time. Bookings cannot overlap in the same room, and cancellation releases the slot. "
                    "Demo data is fine if entered through the app and labeled. Record any other assumptions.")
        return ("Please use the actual booking records from Task 1. State how duration, occupancy, boundaries, "
                "and cancellations are counted. For this run, count active reservations in usage and display "
                "cancellations separately. Use Hong Kong local time and document any other assumptions. "
                "Have the developer and reviewer use the same field names and counting rules when comparing tests.")
    if scenario == "newsroom":
        return ("The audience is HKUST students and faculty. Use English for the sample. Choose the editorial angle "
                "and channels yourselves. Use independently traceable sources; primary evidence is preferred. "
                "Handle incomplete evidence transparently and prepare publication-ready drafts without publishing externally.")
    if phase == 1:
        return ("This is a fictional course simulation only. Assume a 5–10 year horizon, moderate risk tolerance, "
                "no leverage, and no near-term need for the invested money. Discuss HKD/USD and concentration risk. "
                "State assumptions and risks; no real orders or financial transactions are authorized.")
    return ("The presentation is an internal fund-team course simulation. Make end-of-2026 judgments forward-looking "
            "from the evidence available today. Cite dated evidence, separate forecasts from observed facts, and "
            "deliver a browser-presentable HTML deck and a Markdown copy. Choose the analysis structure yourselves.")


NUDGES = (
    "Please continue the work on this assignment and share your current progress with the group. Coordinate the "
    "team, keep evidence in the workspace, and flag any blocker you cannot resolve.",
    "Please finish the actual deliverables, exchange verified artifacts with the appropriate team members, "
    "and arrange review. Prefer a small working result supported by evidence over a larger description. "
    "If work remains, name exactly what is missing.",
    "Please reconcile the current task and review states, publish the available final versions, and record the "
    "actual final outcome if the acceptance conditions permit it. An incomplete result must be identified as such.",
)


class Scenario:
    def __init__(self, name, directory, args):
        self.name, self.directory = name, directory
        self.stage_seconds, self.grace_seconds = args.stage_seconds, args.grace_seconds
        self.idle_seconds = args.idle_seconds
        self.sample = next((ROOT / "test samples").glob(SCENARIOS[name]))
        self.state = directory / "state"
        self.fixture = None
        self.sequential = getattr(args, "sequential_four", False)
        self.fresh_software = (args.fresh_software or self.sequential) and name == "software"
        self.t1_handoff = None
        if name == "software" and not self.fresh_software:
            self.fixture = prepare_software_state(args.software_baseline, self.state, directory / "fixture")
        else:
            self.state.mkdir(parents=True, exist_ok=False)
        self.token = secrets.token_urlsafe(32)
        self.model = load_model()
        if self.model.identity.get("mode") not in {"course", "deepseek", "opencode"}:
            raise RuntimeError("T015 requires a real configured model provider")
        self.sandbox = DockerSandbox(self.state / "sandbox")
        self.app = create_app(model=self.model, sandbox=self.sandbox, state_dir=self.state,
                              search=SearchClient.from_env(), operator_token=self.token, schedule=True)
        self.history = []
        self.assignment_ids = []

    async def publish_fixture(self):
        members = self.team['members']
        producer = next(member for member in members if member['role_id'] == 'release')
        path = 't015-booking-snapshot.db'
        self.sandbox.prepare(producer['workspace_id'])
        self.sandbox.write_bytes(producer['workspace_id'], path, Path(self.fixture['fixture']).read_bytes(), mode='create')
        eligible = await self.app.state.workspace.execute(producer['session_id'], 'list_workspace_readers', '{}')
        if not eligible['ok']:
            raise RuntimeError('Could not resolve eligible readers for the fixture')
        result = await self.app.state.workspace.execute(producer['session_id'], 'publish_artifact', json.dumps({
            'path': path, 'reader_session_ids': [reader['session_id'] for reader in eligible['result']],
        }))
        if not result['ok']:
            raise RuntimeError(f"Fixture publication failed: {result['error']}")
        return result['result']

    def log(self, event, **values):
        item = {"scenario": self.name, "at": now(), "event": event, **values}
        self.history.append(item)
        with (self.directory / "operator-events.jsonl").open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(item, ensure_ascii=False) + "\n")
        headline = {key: value for key, value in values.items() if key not in {"question", "answer", "text"}}
        print(json.dumps({"scenario": self.name, "event": event, **headline}, ensure_ascii=True), flush=True)

    def post(self, client, path, body, *, allow_failure=False):
        response = client.post(path, headers={"Authorization": f"Bearer {self.token}"}, json=body)
        if not response.is_success and not allow_failure:
            raise RuntimeError(f"{path} returned HTTP {response.status_code}: {response.text[:500]}")
        return response

    def send(self, client, text, **extra):
        return self.post(client, f"{self.base}/messages", {"request_id": uuid4().hex, "text": text, **extra}).json()

    def collect_status(self, source=None):
        db = self.app.state.runtime.db
        source_row = db.execute("SELECT * FROM team_goals WHERE source_message_id=?", (source,)).fetchone()
        term = db.execute("SELECT outcome FROM goal_terminal_facts WHERE source_message_id=?", (source,)).fetchone()
        pending = [dict(row) for row in db.execute(f"""
            {QUESTION_ASSIGNMENTS}
            SELECT q.question_id, m.text FROM question_assignments a JOIN human_questions q USING(question_id)
            JOIN team_messages m ON m.id=q.question_id
            WHERE a.assignment_id=:source AND q.answer_message_id IS NULL AND q.closed_at IS NULL ORDER BY m.seq
        """, {"team": self.team["team_id"], "question_id": None, "source": source})]
        turns = [dict(row) for row in db.execute("""
            SELECT t.id, t.role, t.status, t.error, d.purpose, d.message_id, m.role_id
            FROM turns t LEFT JOIN scheduled_turns st ON st.turn_id=t.id
            LEFT JOIN message_deliveries d USING (event_id, session_id, purpose)
            LEFT JOIN members m ON m.session_id=t.role ORDER BY t.rowid
        """)]
        for turn in turns:
            if turn['status'] == 'failed':
                checkpoint = self.app.state.runtime.journal.checkpoint(turn['id'])
                failure_type = 'model.step_limit' if turn['error'] == 'Turn exceeded its model step limit' else 'model.failed'
                failure = db.execute("SELECT payload FROM events WHERE turn_id=? AND type=? ORDER BY rowid DESC LIMIT 1",
                                     (turn['id'], failure_type)).fetchone()
                turn['generation'] = checkpoint['generation'] if checkpoint else None
                turn['failure'] = {**json.loads(failure['payload']), 'type': failure_type} if failure else None
        current_messages = {source}
        if source_row:
            scope = self.app.state.goal_lifecycle._scope_objects(self.team['team_id'], source_row['id'], source)
            current_messages.update(scope['message_ids'])
        active = [row for row in turns if row['message_id'] in current_messages and row['status'] == 'running']
        counts = {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in (
            "team_messages", "execution_receipts", "artifact_versions", "delegation_tasks", "task_submissions",
            "task_reviews", "workspace_searches", "workspace_sources",
        )}
        return {"goal": dict(source_row) if source_row else None,
                "terminal": term["outcome"] if term else None, "questions": pending,
                "active": active, "turns": turns, "counts": counts, "current_messages": current_messages,
                "latest_event": db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]}

    def dump(self, client, name):
        def records():
            db = self.app.state.runtime.db
            existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {table: [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
                    for table in TABLES if table in existing}

        write_json(self.directory / f"{name}-trace.json", client.portal.call(records))
        if self.assignment_ids:
            report = client.get(f"{self.base}/evaluation/report", headers={"Authorization": f"Bearer {self.token}"},
                                params=[("assignment_ids", item) for item in self.assignment_ids])
            write_json(self.directory / f"{name}-reports.json", {"http_status": report.status_code,
                       "body": report.json() if report.is_success else report.text[:1000]})

    def phase(self, client, number):
        filename = "02_task_1.md" if number == 1 else "03_task_2.md"
        text = prompt_text(self.sample / filename)
        before = client.portal.call(self.collect_status)
        observed_failures = {turn["id"] for turn in before["turns"]
                             if turn["status"] in {"failed", "needs_reconciliation"}}
        observed_failures_at_start = observed_failures.copy()
        required_inputs = []
        if self.fixture:
            versions = [self.fixture['accepted_app_version_id'], self.fixture['published_version']['version_id']]
            required_inputs = [{'owner_id': member['member_id'], 'version_ids': versions}
                               for member in self.team['members'] if member['role_id'] in {'dev', 'qa'}]
            if len(required_inputs) != 2:
                raise RuntimeError('Software dev and qa members are required for the handoff')
        if self.fresh_software and number == 2 and self.t1_handoff:
            versions = [self.t1_handoff.get('app_version_id'), self.t1_handoff.get('database_version_id')]
            if all(versions):
                required_inputs = [{'owner_id': member['member_id'], 'version_ids': versions}
                                   for member in self.team['members'] if member['role_id'] in {'dev', 'qa'}]
                if len(required_inputs) != 2:
                    raise RuntimeError('Software dev and qa members are required for the handoff')
        extra = {'required_inputs': required_inputs} if required_inputs else {}
        if self.fresh_software or (self.sequential and number == 1):
            minutes = [round(self.stage_seconds * f / 60, 2) for f in (.15, .4, .6, .8, 1)]
            guidance = (f"Time budget: {self.stage_seconds} seconds. Minute {minutes[0]}: register the primary task; "
                        f"{minutes[1]}: publish a reviewable candidate and the result of one representative check; "
                        f"{minutes[2]}: first review; {minutes[3]}: any rework; {minutes[4]}: close if accepted. "
                        "Assign production and review early. Share the same candidate with the reviewer. "
                        "Publish and submit the first checkable outcome before optional refinements. ")
            if self.fresh_software and number == 1:
                guidance += (
                    "Publish the runnable candidate as t1-app.zip with launch instructions for Lead, Developer, QA and Release. "
                    "Use a persistent workspace SQLite database from the first demonstration. Make several real calls "
                    "to the running app, including a successful reservation and cancellation. Before final review "
                    "use sqlite3.Connection.backup and publish t1-bookings.sqlite3 and t1-data-provenance.json. "
                    "Print T015_SNAPSHOT_SHA256=<snapshot hash> in the successful backup command. The provenance "
                    "JSON includes app_version_id, app_sha256, database_sha256, relative source_database_path, "
                    "backup_method (sqlite3.Connection.backup), operations (method and HTTP status), and schema "
                    "(table, room, date, start, end, status, active_values, cancelled_values). Include approved app "
                    "and snapshot versions in the final evidence."
                )
            elif self.fresh_software:
                handoff = self.t1_handoff or {}
                guidance += (
                    f"T1 application: {handoff.get('app_version_id') or 'unavailable'}. "
                    f"T1 booking snapshot: {handoff.get('database_version_id') or 'unavailable'}. "
                    f"T1 formal handoff evidence ready: {bool(handoff.get('strict_ready'))}. "
                    "Use any available fixed versions read-only and report what is missing or unapproved. "
                    "Extend that build and validate the actual dashboard against the same snapshot. "
                    "Publish t2-dashboard-check.json with database_version_id, range_start, range_end and "
                    "observed metrics: total_count, total_minutes, cancelled_count, rooms (count, minutes, "
                    "cancelled_count), busiest_dates and busiest_periods. Count valid active bookings in usage "
                    "and cancellations separately. Use inclusive YYYY-MM-DD dates and HH:MM hours with ties."
                )
            extra['assignment_guidance'] = guidance
        sent = self.send(client, text, kind="assignment", **extra)
        source = sent["message_id"]
        self.assignment_ids.append(source)
        self.log("assignment", phase=number, message_id=source,
                 official_sha256=hashlib.sha256(text.encode()).hexdigest())
        if self.fixture:
            snapshot = self.fixture['published_version']['version_id']
            app_version = self.fixture['accepted_app_version_id']
            self.send(client, f"For the test handoff, start from accepted app.py version {app_version}. "
                      f"The released booking snapshot version is {snapshot}, from records entered through "
                      "that exact Task 1 application in the isolated exercise. Use both fixed versions, "
                      "keep the snapshot read-only, and cite it in the verification. Count active reservations "
                      "in usage, with cancellations separately. Agree on the API fields and test counts before implementation.",
                      kind='supplement', reply_to=source)
            self.log('controlled_handoff', phase=number, app_version_id=app_version, database_version_id=snapshot,
                     database_sha256=self.fixture['fixture_sha256'])
        began = last_change = time.monotonic()
        material_change = began
        last_material = None
        deadline = began + self.stage_seconds
        grace_used = False
        previous = None
        answered = set()
        attempted_resumes = set()
        resume_counts = {}
        reminders = 0
        outcome = None
        milestone_fractions = iter((.15, .4, .6, .8, 1.0)) if (self.fresh_software or self.sequential) else iter(())
        next_milestone = next(milestone_fractions, None)
        delivery_seconds = None
        while True:
            status = client.portal.call(self.collect_status, source)
            current_time = time.monotonic()
            while next_milestone is not None and current_time - began >= self.stage_seconds * next_milestone:
                self.log('milestone', phase=number, fraction=next_milestone,
                         elapsed=round(current_time-began, 2), assignment_id=source,
                         goal_id=status['goal']['id'] if status['goal'] else None,
                         since_assignment={key: value - before['counts'][key]
                                           for key, value in status['counts'].items()})
                next_milestone = next(milestone_fractions, None)
            with (self.directory / f'task{number}-active-samples.jsonl').open('a', encoding='utf-8') as samples:
                samples.write(json.dumps({'elapsed': round(current_time-began, 3), 'active': status['active']})+'\n')
            goal_status = status["goal"]["status"] if status["goal"] else "pending_registration"
            latest = (status["latest_event"], tuple(status["counts"].values()), goal_status)
            material = tuple(status['counts'][name] for name in ('artifact_versions','task_submissions','task_reviews'))
            if material != last_material:
                last_material, material_change = material, current_time
            if previous != latest:
                last_change = time.monotonic()
                if previous is None or previous[1:] != latest[1:]:
                    self.log("progress", phase=number, goal_status=goal_status, **status["counts"])
                previous = latest
            for turn in status["turns"]:
                if turn["status"] in {"failed", "needs_reconciliation"} and turn["id"] not in observed_failures:
                    self.log("turn_failure", phase=number, turn_id=turn["id"], status=turn["status"], error=turn["error"])
                    observed_failures.add(turn["id"])
            outcome = status["terminal"] or (goal_status if goal_status in END_STATES else None)
            if outcome:
                # close_goal records the terminal fact before its confirmation model call finishes.
                delivery_seconds = round(current_time - began, 2)
                settle_deadline = time.monotonic() + 15
                while status['active'] and time.monotonic() < settle_deadline:
                    time.sleep(0.5)
                    status = client.portal.call(self.collect_status, source)
                self.log('terminal_confirmation_wait', phase=number, remaining_active=len(status['active']),
                         delivery_seconds=delivery_seconds)
                break
            if getattr(self.model, "paused_reason", None):
                outcome = "provider_paused"
                break
            if current_time >= deadline:
                active_delivery = any(turn['purpose'] in TASK_DELIVERIES for turn in status['active'])
                if self.grace_seconds > 0 and not grace_used and (active_delivery or current_time-material_change <= 45):
                    deadline += self.grace_seconds
                    grace_used = True
                    self.log('grace_window', phase=number, seconds=self.grace_seconds,
                             active_delivery=active_delivery, recent_material=current_time-material_change <= 45)
                else:
                    break
            for turn in status['turns']:
                resume_key = retry_key(turn, source, status['current_messages'], observed_failures_at_start,
                                       attempted_resumes, resume_counts, deadline-current_time)
                if resume_key is not None:
                    attempted_resumes.add((turn['id'], turn['generation']))
                    resume_counts[resume_key] = resume_counts.get(resume_key, 0) + 1
                    reply = self.post(client, f"/v1/sessions/{turn['role']}/turns/{turn['id']}/retry", {},
                                      allow_failure=True)
                    self.log('operator_resume', phase=number, turn_id=turn['id'], role=turn['role_id'],
                             generation=turn['generation'], purpose=turn['purpose'], cause=turn['error'],
                             attempt=resume_counts[resume_key], message_id=turn['message_id'], http_status=reply.status_code)
            for question in status["questions"]:
                qid = question["question_id"]
                if qid not in answered:
                    answer = operator_answer(self.name, number, question["text"])
                    self.send(client, answer, reply_to=qid)
                    self.log("clarification_answer", phase=number, question_id=qid, question=question["text"], answer=answer)
                    answered.add(qid)
                    last_change = time.monotonic()
            idle = time.monotonic() - last_change
            if not status["active"] and idle >= self.idle_seconds and reminders < len(NUDGES):
                nudge = NUDGES[reminders]
                self.send(client, nudge, kind="supplement", reply_to=source)
                reminders += 1
                last_change = time.monotonic()
                self.log("operator_followup", phase=number, number=reminders, text=nudge)
            time.sleep(2)
        status = client.portal.call(self.collect_status, source)
        outcome = status["terminal"] or outcome
        if outcome is None and getattr(self.model, "paused_reason", None):
            outcome = "provider_paused"
        if outcome == "provider_paused":
            self.dump(client, f"task{number}")
            self.log("task_outcome", phase=number, outcome=outcome, team_id=self.team["team_id"],
                     source_message_id=source, goal_id=status["goal"]["id"] if status["goal"] else None,
                     reason=self.model.paused_reason, seconds=round(time.monotonic() - began, 2))
            return outcome
        if outcome is None:
            self.dump(client, f"task{number}-before-operator-stop")
            goal = status["goal"]
            reason = "The T015 run ended before this task produced a verified terminal outcome. Preserve the observed work."
            if goal is None:
                path = f"{self.base}/assignments/{source}/terminate"
                body = {"request_id": uuid4().hex, "reason": reason}
            else:
                path = f"{self.base}/goals/{goal['id']}/terminate"
                body = {"request_id": uuid4().hex, "expected_revision": goal["revision"], "outcome": "cancelled", "reason": reason}
            result = self.post(client, path, body, allow_failure=True)
            self.log("operator_stop", phase=number, original_status=goal["status"] if goal else "pending_registration",
                     http_status=result.status_code)
            outcome = "incomplete_operator_stopped" if result.is_success else "incomplete"
        self.dump(client, f"task{number}")
        self.log("task_outcome", phase=number, outcome=outcome, seconds=delivery_seconds if delivery_seconds is not None else round(time.monotonic() - began, 2),
                 reminders=reminders, human_answers=len(answered), resume_attempts=len(attempted_resumes), grace=grace_used)
        return outcome

    def run(self, phases=None):
        with TestClient(self.app, raise_server_exceptions=False) as client:
            if self.fixture:
                response = client.get(f"/v1/teams/{self.fixture['team_id']}", headers={"Authorization": f"Bearer {self.token}"})
            else:
                roster = (self.sample / "01_role_initialization.md").read_text(encoding="utf-8")
                response = self.post(client, "/v1/teams", {"request_id": uuid4().hex, "roster": roster}, allow_failure=True)
            if not response.is_success:
                if getattr(self.model, "paused_reason", None):
                    self.log("provider_paused_during_bootstrap", reason=self.model.paused_reason,
                             http_status=response.status_code)
                    return "provider_paused"
                self.log("bootstrap_failed", http_status=response.status_code, error=response.text[:500])
                return "error"
            self.team = response.json()
            self.base = f"/v1/teams/{self.team['team_id']}"
            members = self.team["members"]
            lead = next(member for member in members if member["member_id"] == self.team["lead_member_id"])
            write_json(self.directory / "team.json", self.team)
            self.log("team", team_id=self.team["team_id"], lead_role=lead["role_id"], members=len(members), **self.model.identity)
            if self.fixture:
                self.fixture['published_version'] = client.portal.call(self.publish_fixture)
                write_json(self.directory / 'handoff-fixture.json', self.fixture)
            self.dump(client, 'initial')
            try:
                for phase in (phases if phases is not None else ((2,) if self.fixture else (1, 2))):
                    if getattr(self.model, "paused_reason", None):
                        self.log("provider_paused_before_phase", phase=phase, team_id=self.team["team_id"],
                                 reason=self.model.paused_reason)
                        return "provider_paused"
                    if self.phase(client, phase) == "provider_paused":
                        return "provider_paused"
                    if self.fresh_software:
                        if phase == 1:
                            self.t1_handoff = client.portal.call(audit_t1_handoff, self.app, self.team, self.assignment_ids[-1])
                            write_json(self.directory / 't1-handoff-audit.json', self.t1_handoff)
                            self.log('handoff_audit', phase=1, assignment_id=self.assignment_ids[-1], **self.t1_handoff)
                        else:
                            audit = client.portal.call(audit_t2_handoff, self.app, self.team, self.assignment_ids[-1], self.t1_handoff)
                            write_json(self.directory / 't2-handoff-audit.json', audit)
                            self.log('handoff_audit', phase=2, assignment_id=self.assignment_ids[-1], audit=audit)
            finally:
                self.dump(client, "final")
        return "finished"


def main():
    parser = argparse.ArgumentParser()
    parser.set_defaults(scenarios=list(SCENARIOS))
    parser.add_argument("--run", required=True)
    parser.add_argument("--stage-seconds", type=int, default=1200)
    parser.add_argument("--grace-seconds", type=int, help="Optional extension after the stage deadline")
    parser.add_argument("--idle-seconds", type=float, default=45)
    parser.add_argument("--jobs", type=int, default=1)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--software-baseline', type=Path, help='Original completed software T1 scenario directory')
    group.add_argument('--fresh-software', action='store_true', help='Run only software T1 and T2 in one fresh team')
    group.add_argument('--sequential-four', action='store_true', help='Fresh software T1/T2, fund T1, newsroom T1 in order')
    args = parser.parse_args()
    if args.grace_seconds is None:
        args.grace_seconds = 0 if (args.fresh_software or args.sequential_four) else 75
    if args.sequential_four:
        if args.jobs != 1 or args.stage_seconds != 1200 or args.grace_seconds != 0:
            parser.error('--sequential-four requires --jobs 1, --stage-seconds 1200 and --grace-seconds 0')
        args.scenarios = [name for name, _ in sequential_plan()]
    if args.fresh_software:
        args.scenarios = ["software"]
        if args.jobs != 1:
            parser.error("--fresh-software requires --jobs 1")
    if re.fullmatch(r"[a-zA-Z0-9_-]+", args.run) is None:
        raise ValueError("Invalid run directory name")
    os.environ.pop("COLLABORATION_GIT_TARGETS_DIR", None)
    os.environ.pop("COLLABORATION_GOAL_TIMEOUT_SECONDS", None)
    parent = ROOT / ".tmp" / "t015" / args.run
    parent.mkdir(parents=True, exist_ok=False)
    class DiagnosticFormatter(logging.Formatter):
        def format(self, record):
            fields = ("goal_id", "turn_id", "attempt", "duration_ms", "status", "http_status", "usage")
            return json.dumps({"at": now(), "message": record.getMessage(),
                               **{key: getattr(record, key) for key in fields if hasattr(record, key)}},
                              ensure_ascii=False, default=str)
    diagnostic = logging.FileHandler(parent / "model-diagnostics.jsonl", encoding="utf-8")
    diagnostic.setFormatter(DiagnosticFormatter())
    model_logger = logging.getLogger("collaboration_runtime.model")
    model_logger.setLevel(logging.INFO)
    model_logger.addHandler(diagnostic)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    for script in ('run_five.py', 'recovery.py', 'handoff.py', 'create_fixture.py', 'software_handoff.py'):
        shutil.copyfile(Path(__file__).with_name(script), parent / script)
    runtime_sources = {str(path.relative_to(ROOT)).replace(os.sep, "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in sorted((ROOT / "collaboration_runtime").rglob("*.py"))}
    write_json(parent / "run.json", {"revision": revision, "scenarios": args.scenarios, "started_at": now(),
                                     "stage_seconds": args.stage_seconds, "grace_seconds": args.grace_seconds,
                                     "idle_seconds": args.idle_seconds,
                                     "jobs": args.jobs, "fresh_software": args.fresh_software, "sequential_four": args.sequential_four,
                                     "phase_plan": sequential_plan() if args.sequential_four else None, "runtime_source_sha256": runtime_sources})

    def run_scenario(scenario, phases=None):
        try:
            return Scenario(scenario, parent / scenario, args).run(phases)
        except Exception as exc:
            print(json.dumps({"scenario": scenario, "event": "runner_error", "error": str(exc)}, ensure_ascii=True), flush=True)
            return "error"

    if args.sequential_four:
        outcomes = run_sequential(run_scenario)
    else:
        with ThreadPoolExecutor(max_workers=args.jobs) as executor:
            futures = {executor.submit(run_scenario, scenario): scenario for scenario in args.scenarios}
            outcomes = {futures[future]: future.result() for future in as_completed(futures)}
    write_json(parent / "runner-result.json", {"scenarios": outcomes,
               "blocked": any(value == "provider_paused" for value in outcomes.values())})
    if any(value != "finished" for value in outcomes.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
