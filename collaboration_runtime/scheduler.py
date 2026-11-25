"""Claim persisted deliveries and launch role turns without holding DB locks.

All database work stays on the event-loop thread and uses the runtime's SQLite
connection. Model calls and tool execution are owned by Runtime after launch.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

from .collaboration import CollaborationForbidden, CollaborationInvalid
from .teams import TeamNotFound
from .workspace import WorkspaceNotFound


logger = logging.getLogger(__name__)


def _delivery_guidance(payload, actor_id=None):
    """Give short, event-specific suggestions without making acceptance decisions."""
    purpose = payload['delivery']['purpose']
    if purpose == 'goal.terminal':
        return {'activation': ('Use next_goal_activation to activate the earliest eligible queued goal. '
                               'Finish this notification after the tool receipt so the next delivery can run.')}
    if purpose == 'message.correction':
        return {'correction': ('Apply the verified change to the referenced work, then yield for the pending task. '
                               'Use the existing candidate and record any effect on its review.')}
    if purpose == 'task.submitted':
        request = payload.get('review_request')
        if not request:
            return {'notice': ('This is a submission update. Let the designated reviewer decide. Continue only '
                               'independent work that needs this update.')}
        submission = request['submission']
        return {'review': {
            'tool': 'review_result',
            'arguments': {'task_id': submission['task_id'], 'expected_revision': submission['task_revision'],
                          'submission_id': submission['id']},
            'guidance': ('Review the exact versions and check each numbered criterion once. Read only the passages '
                         'needed for the decision. Record one evidence-backed finding per criterion. Approve when all '
                         'pass. For a return, give the specific gap, expected correction and recheck items. Make this '
                         'first decision promptly, before optional research or rewriting the candidate.'),
        }}
    if purpose == 'message' and payload.get('message', {}).get('kind') == 'assignment':
        return {'assignment': ('Register the goal and propose the smallest complete outcome a reviewer can check. '
                               'Clarify blockers and start independent work before expanding the plan. '
                               'Name one primary deliverable, its owner, reviewer and readers. '
                               'Let that owner publish and submit the primary candidate while others prepare checks. '
                               'Pin exact input_version_ids for checks of a published draft. For work that needs an '
                               'approved output, use dependency_ids and set output_grants on the producing task. '
                               'If ownership must change, register the change before work shifts. '
                               'End this turn when waiting for confirmations.')}
    if purpose == 'delegation.proposal':
        if action := payload.get('proposal_action'):
            return {'proposal': (f'All parties confirmed the current open version. Call {action} '
                                 'with this proposal and exact version; yield the turn when waiting on others.')}
        return {'proposal': ('This turn is for deciding on the proposal. If the terms are clear, confirm the exact '
                             'version and yield unless the receipt asks you to register it. Wait for the formal task '
                             'delivery to start work.')}
    if purpose in ('task.assigned', 'task.ready', 'task.updated', 'task.returned'):
        task = payload.get('task', {})
        if actor_id is not None and task.get('terms', {}).get('owner_id') == actor_id:
            return {'delivery': ('Choose a representative acceptance check for the smallest reviewable version. '
                                 'Report a missing input to its dispatcher once and continue independent work. '
                                 'For research, draft from available evidence and search only for specific gaps. '
                                 'When the representative check passes, '
                                 'publish the exact result and evidence to the required readers and call submit_result '
                                 'for this task and revision before running optional checks or polishing. '
                                 'Disclose any remaining acceptance gaps and yield to the reviewer.')}
    if purpose == 'task.approved' and payload.get('goal_close_ready'):
        return {'closure': ('Use get_goal.close_preview; if still ready, call close_goal with its confirmation. '
                            'Yield the turn while waiting on others.')}
    return None


class DeliveryScheduler:
    def __init__(self, runtime, collaboration, workspace, *, max_parallel=4):
        if type(max_parallel) is not int or max_parallel < 1:
            raise ValueError('max_parallel must be a positive integer')
        self.runtime = runtime
        self.collaboration = collaboration
        self.workspace = workspace
        self.db = runtime.db
        self.max_parallel = max_parallel
        self.assistant_first = False

    def _claim_one(self):
        """Claim one eligible record; no asynchronous work runs in transaction."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            candidates = self.db.execute('''
                WITH waiting AS (
                    SELECT t.role, json_extract(e.payload, '$.queued_through') AS queued_through,
                           json_extract(e.payload, '$.event_id') AS target_event, ym.kind AS yielded_kind
                    FROM turns t JOIN events e ON e.turn_id = t.id
                    JOIN scheduled_turns ys ON ys.turn_id = t.id
                    JOIN message_deliveries yd USING (event_id, session_id, purpose)
                    JOIN team_messages ym ON ym.id = yd.message_id
                    WHERE t.status = 'yielded' AND e.type = 'turn.delivery_yielded'
                )
                SELECT d.event_id, d.message_id, d.session_id, d.purpose, d.status,
                       m.team_id, s.turn_id
                FROM message_deliveries d
                JOIN team_messages m ON m.id = d.message_id
                LEFT JOIN scheduled_turns s ON s.event_id = d.event_id
                    AND s.session_id = d.session_id AND s.purpose = d.purpose
                LEFT JOIN turns t ON t.id = s.turn_id
                LEFT JOIN waiting w ON w.role = d.session_id
                WHERE ((t.status IN ('recoverable', 'yielded') AND d.status = 'running')
                       OR (s.turn_id IS NULL AND d.status = 'pending'))
                  AND NOT EXISTS (
                      SELECT 1 FROM turns live WHERE live.role = d.session_id
                      AND live.status IN ('running', 'recoverable')
                      AND (s.turn_id IS NULL OR live.id != s.turn_id)
                  )
                ORDER BY CASE WHEN t.status = 'recoverable' THEN 0
                              WHEN d.purpose = 'task.delivery_requested' AND (w.queued_through IS NULL
                                   OR d.event_id <= w.queued_through) THEN 1
                              WHEN w.yielded_kind = 'assignment' AND s.turn_id IS NULL AND d.event_id = w.target_event THEN 1
                              WHEN t.status = 'yielded' THEN 4
                              WHEN m.kind = 'assignment' THEN 1
                              WHEN s.turn_id IS NULL AND d.event_id > w.queued_through
                               AND m.kind <> 'supplement'
                               AND d.purpose NOT IN ('clarification.resume', 'clarification.correction') THEN 5
                              WHEN m.kind = 'supplement' OR d.purpose IN (
                                  'task.submitted', 'task.approved', 'message.correction',
                                  'clarification.resume', 'clarification.correction') THEN 2
                              WHEN d.purpose IN ('task.assigned', 'task.updated', 'task.ready', 'task.returned') THEN 3
                              ELSE 5 END, d.event_id
            ''').fetchall()
            for row in candidates:
                delivery = dict(row)
                session = delivery['session_id']
                if session in self.runtime.active:
                    continue
                fork_message_id = None
                principal = None
                try:
                    fork_message_id = self.collaboration.fork_message_for_delivery(delivery)
                    principal = self.collaboration.auth.member(session)
                    if principal is None or principal.team_id != delivery['team_id']:
                        raise WorkspaceNotFound('Delivery identity unavailable')
                    actor = self.workspace.actor(session)
                    if actor.team_id != delivery['team_id']:
                        raise WorkspaceNotFound('Workspace identity unavailable')
                    version_ids = None
                    confirming = delivery["turn_id"] is not None and self.runtime.journal.confirming_goal(delivery["turn_id"])
                    if fork_message_id is not None and not confirming:
                        version_ids = self.runtime.fork_messages.authorize_message(session, fork_message_id)
                    if delivery['turn_id'] is not None:
                        self.collaboration.get_message(delivery['team_id'], principal, delivery['message_id'])
                        self.runtime.reopen_turn(delivery['turn_id'])
                        turn_id = delivery['turn_id']
                    else:
                        payload = self.collaboration.execution_input(delivery, principal)
                        if delivery['purpose'] == 'goal.terminal':
                            activation = self.collaboration.goal_lifecycle.next_eligible_queued_goal(delivery['team_id'])
                            if activation is None:
                                self.db.execute("""
                                    UPDATE message_deliveries SET status = 'completed'
                                    WHERE event_id = ? AND session_id = ? AND purpose = ? AND status = 'pending'
                                """, (delivery['event_id'], session, delivery['purpose']))
                                continue
                            payload['next_goal_activation'] = activation
                        if version_ids is not None:
                            self.runtime.fork_messages.add_delivery_versions(payload, fork_message_id, version_ids)
                        guidance = _delivery_guidance(payload, principal.id)
                        if guidance is not None:
                            payload['platform_guidance'] = guidance
                        text = json.dumps(payload, ensure_ascii=False)
                        turn_id = self.runtime.create_turn(session, text)
                        self.db.execute('''INSERT INTO scheduled_turns(turn_id, event_id, session_id, purpose)
                                           VALUES (?, ?, ?, ?)''',
                                        (turn_id, delivery['event_id'], session, delivery['purpose']))
                        self.db.execute('''UPDATE message_deliveries SET status = 'running'
                                           WHERE event_id = ? AND session_id = ? AND purpose = ?
                                             AND status = 'pending' ''',
                                        (delivery['event_id'], session, delivery['purpose']))
                        if delivery['purpose'] == 'task.delivery_requested':
                            self.collaboration.delivery_requests.started(delivery['message_id'], principal.team_id)
                    return session, turn_id, actor.prompt, actor.workspace_id
                except (TeamNotFound, WorkspaceNotFound, CollaborationForbidden, CollaborationInvalid) as exc:
                    # These failures cannot be resolved by retrying the same work.
                    logger.warning('Invalid delivery %s: %s', delivery['event_id'], type(exc).__name__)
                    self.db.execute('''UPDATE message_deliveries SET status = 'failed'
                                       WHERE event_id = ? AND session_id = ? AND purpose = ?''',
                                    (delivery['event_id'], session, delivery['purpose']))
                    if delivery['purpose'] == 'task.delivery_requested' and principal is not None:
                        try:
                            self.collaboration.delivery_requests.finish(delivery['message_id'], principal, reason=str(exc))
                        except (TeamNotFound, CollaborationForbidden):
                            pass
                    if delivery['turn_id'] is not None:
                        if fork_message_id is not None:
                            self.runtime._write_finish(delivery['turn_id'], 'interrupted', error='Fork delivery scope ended')
                        else:
                            self.db.execute("UPDATE turns SET status = 'interrupted', error = ? WHERE id = ? AND status IN ('recoverable', 'yielded')",
                                            ('Delivery identity or input unavailable', delivery['turn_id']))
            return None

    def _claim_assistant(self):
        if self.runtime.forks is None:
            return None
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute("""
                SELECT j.*, t.status AS turn_status FROM assistant_jobs j LEFT JOIN turns t ON t.id = j.turn_id
                WHERE (j.status = 'pending' OR (j.status = 'running' AND t.status = 'recoverable'))
                  AND NOT EXISTS (
                      SELECT 1 FROM turns live WHERE live.role = j.session_id
                      AND live.status IN ('running', 'recoverable') AND (j.turn_id IS NULL OR live.id != j.turn_id)
                  )
                ORDER BY j.turn_id IS NULL, j.job_id
            """).fetchall()
            for row in rows:
                session = row["session_id"]
                if session in self.runtime.active:
                    continue
                try:
                    actor = self.workspace.actor(session)
                    self.runtime.fork_messages.authorize_job(session, row["input"])
                except WorkspaceNotFound:
                    self.db.execute("UPDATE assistant_jobs SET status = 'cancelled' WHERE job_id = ?", (row["job_id"],))
                    if row["turn_id"]:
                        self.runtime._write_finish(row["turn_id"], "interrupted", error="Assistant scope ended")
                    continue
                if row["turn_id"]:
                    turn_id = row["turn_id"]
                    self.runtime.reopen_turn(turn_id)
                else:
                    turn_id = self.runtime.create_turn(session, row["input"])
                    self.db.execute("UPDATE assistant_jobs SET status = 'running', turn_id = ? WHERE job_id = ?",
                                    (turn_id, row["job_id"]))
                return session, turn_id, actor.prompt, actor.workspace_id
        return None

    async def run(self):
        """Poll until cancelled or runtime shutdown; independent sessions can run together."""
        while not self.runtime.closing:
            try:
                if self.collaboration.goal_lifecycle is not None:
                    self.collaboration.goal_lifecycle.expire_due_goals()
                while (not self.runtime.closing and len(self.runtime.active) < self.max_parallel
                       and getattr(self.runtime.model, "paused_reason", None) is None):
                    claimed = ((self._claim_assistant() or self._claim_one()) if self.assistant_first
                               else (self._claim_one() or self._claim_assistant()))
                    self.assistant_first = not self.assistant_first
                    if claimed is None:
                        break
                    session, turn_id, prompt, workspace_id = claimed
                    try:
                        self.runtime.launch(session, turn_id, prompt, workspace_id)
                    except Exception:
                        # A committed claim must not be left silently running if launch fails.
                        logger.exception('Could not launch scheduled turn %s', turn_id)
                        with self.db:
                            self.db.execute("UPDATE turns SET status = 'recoverable', error = ? WHERE id = ? AND status = 'running'",
                                            ('Scheduler could not launch turn', turn_id))
                        break
            except sqlite3.Error:
                logger.exception('Unable to claim delivery; will retry')
            await asyncio.sleep(0.08)
