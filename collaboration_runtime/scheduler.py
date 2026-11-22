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
                SELECT d.event_id, d.message_id, d.session_id, d.purpose, d.status,
                       m.team_id, s.turn_id
                FROM message_deliveries d
                JOIN team_messages m ON m.id = d.message_id
                LEFT JOIN scheduled_turns s ON s.event_id = d.event_id
                    AND s.session_id = d.session_id AND s.purpose = d.purpose
                LEFT JOIN turns t ON t.id = s.turn_id
                WHERE (t.status = 'recoverable' OR (s.turn_id IS NULL AND d.status = 'pending'))
                  AND NOT EXISTS (
                      SELECT 1 FROM turns live WHERE live.role = d.session_id
                      AND live.status IN ('running', 'recoverable')
                      AND (s.turn_id IS NULL OR live.id != s.turn_id)
                  )
                ORDER BY CASE WHEN t.status = 'recoverable' THEN 0 ELSE 1 END, d.event_id
            ''').fetchall()
            for row in candidates:
                delivery = dict(row)
                session = delivery['session_id']
                if session in self.runtime.active:
                    continue
                fork_message_id = None
                try:
                    fork_message_id = self.collaboration.fork_message_for_delivery(delivery)
                    principal = self.collaboration.auth.member(session)
                    if principal is None or principal.team_id != delivery['team_id']:
                        raise WorkspaceNotFound('Delivery identity unavailable')
                    actor = self.workspace.actor(session)
                    if actor.team_id != delivery['team_id']:
                        raise WorkspaceNotFound('Workspace identity unavailable')
                    version_ids = None
                    if fork_message_id is not None:
                        version_ids = self.runtime.fork_messages.authorize_message(session, fork_message_id)
                    if delivery['turn_id'] is not None:
                        self.collaboration.get_message(delivery['team_id'], principal, delivery['message_id'])
                        self.runtime.reopen_turn(delivery['turn_id'])
                        turn_id = delivery['turn_id']
                    else:
                        payload = self.collaboration.execution_input(delivery, principal)
                        if version_ids is not None:
                            self.runtime.fork_messages.add_delivery_versions(payload, fork_message_id, version_ids)
                        text = json.dumps(payload, ensure_ascii=False)
                        turn_id = self.runtime.create_turn(session, text)
                        self.db.execute('''INSERT INTO scheduled_turns(turn_id, event_id, session_id, purpose)
                                           VALUES (?, ?, ?, ?)''',
                                        (turn_id, delivery['event_id'], session, delivery['purpose']))
                        self.db.execute('''UPDATE message_deliveries SET status = 'running'
                                           WHERE event_id = ? AND session_id = ? AND purpose = ?
                                             AND status = 'pending' ''',
                                        (delivery['event_id'], session, delivery['purpose']))
                    return session, turn_id, actor.prompt, actor.workspace_id
                except (TeamNotFound, WorkspaceNotFound, CollaborationForbidden, CollaborationInvalid) as exc:
                    # These failures cannot be resolved by retrying the same work.
                    logger.warning('Invalid delivery %s: %s', delivery['event_id'], type(exc).__name__)
                    self.db.execute('''UPDATE message_deliveries SET status = 'failed'
                                       WHERE event_id = ? AND session_id = ? AND purpose = ?''',
                                    (delivery['event_id'], session, delivery['purpose']))
                    if delivery['turn_id'] is not None:
                        if fork_message_id is not None:
                            self.runtime._write_finish(delivery['turn_id'], 'interrupted', error='Fork delivery scope ended')
                        else:
                            self.db.execute("UPDATE turns SET status = 'interrupted', error = ? WHERE id = ? AND status = 'recoverable'",
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
                while not self.runtime.closing and len(self.runtime.active) < self.max_parallel:
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
