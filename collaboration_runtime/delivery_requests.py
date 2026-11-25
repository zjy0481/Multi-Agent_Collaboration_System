"""Operator requests for an owner's current candidate, without changing task ACLs."""
import json
from typing import Annotated
from uuid import uuid4

from pydantic import Field, StringConstraints, field_validator
from .collaboration import Command, RequestId, CollaborationForbidden, CollaborationInvalid
from .local_auth import OPERATOR_ID, Principal
from .teams import TeamConflict, TeamNotFound


class DeliveryRequestInput(Command):
    request_id: RequestId
    expected_revision: Annotated[int, Field(ge=1, strict=True)]
    paths: Annotated[list[Annotated[str, StringConstraints(min_length=1, max_length=1024)]], Field(max_length=8)]
    note: Annotated[str, StringConstraints(max_length=4000)] | None = None


    @field_validator('paths')
    @classmethod
    def canonical_paths(cls, paths):
        if len(paths) != len(set(paths)):
            raise ValueError('Paths must be unique')
        for path in paths:
            if (path != path.strip() or path.startswith('/') or '\\' in path or ':' in path
                    or any(ord(c) < 32 or ord(c) == 127 for c in path)
                    or any(part in ('', '.', '..') for part in path.split('/'))):
                raise ValueError('Use canonical relative POSIX paths')
        return paths


class DeliveryRequests:
    def __init__(self, collaboration):
        self.collaboration = collaboration
        self.db = collaboration.db
        self.delegation = collaboration.delegation
        self.db.execute('''CREATE TABLE IF NOT EXISTS candidate_delivery_requests (
            id TEXT PRIMARY KEY, team_id TEXT NOT NULL, task_id TEXT NOT NULL, goal_id TEXT NOT NULL,
            revision INTEGER NOT NULL, owner_id TEXT NOT NULL, reviewer_id TEXT NOT NULL,
            message_id TEXT NOT NULL UNIQUE, event_id INTEGER NOT NULL, paths TEXT NOT NULL, note TEXT,
            finished_event_id INTEGER, reason TEXT
        )''')
        self.db.execute('CREATE INDEX IF NOT EXISTS candidate_delivery_task_revision ON candidate_delivery_requests(task_id, revision)')

    def _operator(self, team_id, actor):
        self.collaboration.team(team_id, actor)
        if not actor.is_operator:
            raise CollaborationForbidden('Only the user can request candidate delivery')

    def _task(self, team_id, task_id):
        task = self.delegation._task(team_id, task_id)
        goal = self.db.execute('SELECT * FROM team_goals WHERE id=? AND team_id=?', (task['goal_id'], team_id)).fetchone()
        source = self.db.execute('SELECT sender_id,kind FROM team_messages WHERE id=?', (goal['source_message_id'],)).fetchone() if goal else None
        if not source or source['sender_id'] != OPERATOR_ID or source['kind'] != 'assignment':
            raise TeamNotFound('Task not found')
        return task, goal

    def _members(self, team):
        return {m['member_id']: Principal(m['member_id'], team['team_id'], m['session_id']) for m in team['members']}

    def _submission_after_request(self, row):
        return self.db.execute("""SELECT 1 FROM task_submissions
            WHERE task_id=? AND task_revision=? AND json_extract(data, '$.event_id') > ? LIMIT 1""",
            (row['task_id'], row['revision'], row['event_id'])).fetchone()

    def _projection(self, row):
        task, goal = self._task(row['team_id'], row['task_id'])
        result = {'id': row['id'], 'status': 'queued'}
        if task['revision'] != row['revision']:
            return {'id': row['id'], 'status': 'stale'}
        submission = self.db.execute("""SELECT s.data, r.data AS review
            FROM scheduled_turns st JOIN execution_receipts e ON e.turn_id=st.turn_id
            JOIN task_submissions s ON s.id=json_extract(e.result, '$.result.submission_id')
            LEFT JOIN task_reviews r ON r.submission_id=s.id
            WHERE st.event_id=? AND st.purpose='task.delivery_requested'
              AND e.name='submit_result' AND e.state='completed' AND json_extract(e.result, '$.ok')=1
              AND s.task_id=? AND s.task_revision=? AND json_extract(s.data, '$.event_id') > ?
            LIMIT 1""", (row['event_id'], row['task_id'], row['revision'], row['event_id'])).fetchone()
        if submission:
            submission_data = json.loads(submission['data'])
            result.update(status='submitted', submission_id=submission_data['id'])
            if submission['review']:
                review = json.loads(submission['review'])
                result.update(status=review['decision'], review_id=review['id'], review_decision=review['decision'])
            return result
        if self._submission_after_request(row):
            return {'id': row['id'], 'status': 'stale',
                    'reason': 'The task received a submission outside this delivery request.'}
        if goal['status'] != 'active':
            result['status'] = 'cancelled'
        else:
            delivery = self.db.execute('''SELECT d.status AS delivery_status, t.status AS turn_status FROM message_deliveries d
                LEFT JOIN scheduled_turns st ON (st.event_id,st.session_id,st.purpose)=(d.event_id,d.session_id,d.purpose)
                LEFT JOIN turns t ON t.id=st.turn_id WHERE d.event_id=? AND d.purpose='task.delivery_requested' ''', (row['event_id'],)).fetchone()
            if delivery and delivery['turn_status'] in ('running', 'recoverable', 'yielded'):
                result['status'] = 'running'
            elif row['finished_event_id'] or (delivery and (delivery['delivery_status'] != 'pending'
                    or delivery['turn_status'] in ('failed', 'interrupted', 'needs_reconciliation'))):
                result['status'] = 'blocked'
        if row['reason'] and result['status'] == 'blocked':
            result['reason'] = row['reason']
        return result

    def list(self, team_id, actor):
        self._operator(team_id, actor)
        tasks = []
        for item in self.db.execute('SELECT id FROM delegation_tasks WHERE team_id=? ORDER BY rowid', (team_id,)):
            try:
                task, goal = self._task(team_id, item['id'])
            except TeamNotFound:
                continue
            row = self.db.execute('SELECT * FROM candidate_delivery_requests WHERE task_id=? ORDER BY rowid DESC LIMIT 1', (task['id'],)).fetchone()
            if goal['status'] != 'active' and row is None:
                continue
            tasks.append({'id': task['id'], 'goal_id': task['goal_id'], 'revision': task['revision'],
                          'goal_status': goal['status'],
                          'owner_id': task['terms']['owner_id'], 'reviewer_id': task['terms']['reviewer_id'],
                          'status': self.delegation.results.status(task), 'request': self._projection(row) if row else None})
        return {'tasks': tasks}

    def create(self, team_id, actor, task_id, body):
        key = (team_id, actor.id, 'candidate_delivery', body.request_id)
        digest = self.collaboration._digest({'task_id':task_id, **body.model_dump(exclude={'request_id'})})
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._operator(team_id, actor)
            replay = self.collaboration._replay(key, digest)
            if replay is not None:
                return replay
            task, goal = self._task(team_id, task_id)
            members = self._members(self.collaboration.team(team_id, actor))
            owner = members.get(task['terms']['owner_id'])
            if goal['status'] != 'active' or task['revision'] != body.expected_revision:
                raise TeamConflict('Task or goal changed; refresh before requesting delivery')
            if owner is None or not self.delegation._can_read(team_id, owner, task):
                raise TeamConflict('Task owner cannot currently read the task and its inputs')
            if self.delegation.results.status(task) not in {'assigned', 'ready', 'returned'}:
                raise TeamConflict('Task is not ready for a new submission')
            prior = self.db.execute('SELECT * FROM candidate_delivery_requests WHERE task_id=? ORDER BY rowid DESC LIMIT 1', (task_id,)).fetchone()
            if prior and self._projection(prior)['status'] in {'queued','running'}:
                raise TeamConflict('A candidate delivery request is already in progress')
            msg = self.collaboration._create_message(team_id, actor, recipient=owner.id, text='请将当前任务稿件准备送审。', kind='chat', mentions=[], reply_to=None)
            self.collaboration._deliver(msg, [owner.session_id], 'task.delivery_requested')
            self.db.execute('INSERT INTO message_goal_scopes VALUES (?,?)', (msg['message_id'],task['goal_id']))
            rid = uuid4().hex
            self.db.execute('INSERT INTO candidate_delivery_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)',
                (rid, team_id, task_id, task['goal_id'], task['revision'], owner.id, task['terms']['reviewer_id'], msg['message_id'], msg['event_id'], json.dumps(body.paths), body.note))
            self.collaboration._event(team_id, 'delivery.requested', rid)
            receipt = {'id':rid,'status':'queued'}
            self.collaboration._save_receipt(key, digest, receipt)
            return receipt

    def context(self, message_id, actor):
        row = self.db.execute('SELECT * FROM candidate_delivery_requests WHERE message_id=?', (message_id,)).fetchone()
        if row is None or actor is None or actor.id != row['owner_id'] or actor.team_id != row['team_id']:
            raise TeamNotFound('Delivery request not found')
        team = self.collaboration.team(row['team_id'], actor)
        if self._members(team).get(actor.id) != actor:
            raise TeamNotFound('Delivery request not found')
        self.collaboration._message_for(row['team_id'], actor, message_id)
        task, goal = self._task(row['team_id'], row['task_id'])
        if goal['status'] != 'active' or task['revision'] != row['revision'] or task['terms']['owner_id'] != actor.id or not self.delegation._can_read(row['team_id'], actor, task):
            raise CollaborationInvalid('Delivery request no longer applies to the current task')
        if self.delegation.results.status(task) not in {'assigned', 'ready', 'returned'}:
            raise CollaborationInvalid('Task is no longer ready for a new submission')
        if self._submission_after_request(row):
            raise CollaborationInvalid('The task received a submission outside this delivery request')
        members = self._members(self.collaboration.team(row['team_id'], actor))
        reviewer = members.get(row['reviewer_id'])
        if reviewer is None:
            raise CollaborationInvalid('Task reviewer unavailable')
        return {'id':row['id'], 'task_id':row['task_id'], 'goal_id':row['goal_id'], 'revision':row['revision'],
                'owner_id':row['owner_id'], 'reviewer_id':row['reviewer_id'], 'reviewer_session_id':reviewer.session_id,
                'lead_session_id': members[team['lead_member_id']].session_id,
                'paths':json.loads(row['paths']), 'note':row['note']}

    def finish(self, message_id, actor, reason=None):
        row = self.db.execute('SELECT * FROM candidate_delivery_requests WHERE message_id=?', (message_id,)).fetchone()
        if row is None or actor is None or actor.team_id != row['team_id'] or actor.id != row['owner_id']:
            raise TeamNotFound('Delivery request not found')
        team = self.collaboration.team(row['team_id'], actor)
        if self._members(team).get(actor.id) != actor:
            raise TeamNotFound('Delivery request not found')
        if reason is not None:
            reason = str(reason)[:1000]
        if row['finished_event_id'] is None:
            event = self.collaboration._event(row['team_id'], 'delivery.finished', row['id'])
            self.db.execute('UPDATE candidate_delivery_requests SET finished_event_id=?, reason=? WHERE id=?', (event, reason, row['id']))
        return self._projection(self.db.execute('SELECT * FROM candidate_delivery_requests WHERE id=?', (row['id'],)).fetchone())

    def started(self, message_id, team_id):
        row = self.db.execute('SELECT id FROM candidate_delivery_requests WHERE message_id=? AND team_id=?',
                              (message_id, team_id)).fetchone()
        if row:
            self.collaboration._event(team_id, 'delivery.started', row['id'])

    def submitted_handoff(self, turn_id, actor, task):
        row = self.db.execute("""SELECT r.* FROM events e
            JOIN candidate_delivery_requests r ON r.event_id = json_extract(e.payload, '$.event_id')
            WHERE e.turn_id=? AND e.type='turn.delivery_yielded'
              AND json_extract(e.payload, '$.purpose')='task.delivery_requested'
              AND r.team_id=? AND r.owner_id=? AND r.task_id=? AND r.revision=?""",
            (turn_id, actor.team_id, actor.id, task['id'], task['revision'])).fetchone()
        return bool(row and self._projection(row).get('submission_id'))

    def event(self, team_id, actor, event_type, seq):
        if not actor.is_operator:
            return None
        if event_type in {'task.assigned', 'task.updated', 'task.ready', 'task.submitted',
                          'task.approved', 'task.returned', 'task.needs_recheck'}:
            actual, record = self.delegation._event_record(team_id, seq)
            if actual != event_type or not record:
                return None
            try:
                self._task(team_id, record['id'])
            except TeamNotFound:
                return None
            return {'task_id': record['id']}
        if event_type not in {'delivery.requested', 'delivery.started', 'delivery.finished'}:
            return None
        row = self.db.execute('''SELECT r.* FROM collaboration_events e JOIN candidate_delivery_requests r ON r.id=e.object_id
                                WHERE e.seq=? AND e.team_id=?''', (seq, team_id)).fetchone()
        return {'task_id':row['task_id'], 'request':self._projection(row)} if row else None
