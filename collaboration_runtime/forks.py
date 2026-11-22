"""Persist bounded assistant forks and atomically enqueue their first work."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Annotated
from uuid import UUID, uuid5

from pydantic import Field, ValidationError, model_validator

from .artifacts import ArtifactError
from .collaboration import CollaborationInvalid, Command, ObjectId, RequestId, Text
from .fork_history import project_history
from .teams import TeamConflict, TeamNotFound
from .workspace import WorkspaceNotFound


class ForkInput(Command):
    request_id: RequestId
    from_turn_id: ObjectId
    goal_id: ObjectId
    purpose: Text
    scope_id: ObjectId | None = None
    task_id: ObjectId | None = None
    expected_revision: Annotated[int, Field(ge=1, strict=True)] | None = None
    history_turn_ids: list[ObjectId] = Field(default_factory=list, max_length=32)
    version_ids: list[ObjectId] = Field(default_factory=list, max_length=32)
    allowed_tools: list[str] = Field(default_factory=list, max_length=32)
    contact_session_ids: list[ObjectId] = Field(default_factory=list, max_length=32)

    @model_validator(mode='after')
    def validate_scope(self):
        if (self.scope_id is None) == (self.task_id is None):
            raise ValueError('Exactly one scope_id or task_id is required')
        if (self.task_id is None) != (self.expected_revision is None):
            raise ValueError('Task scope requires an expected revision')
        return self


class ForkHandoffInput(Command):
    request_id: RequestId
    version_ids: list[ObjectId] = Field(min_length=1, max_length=32)
    note: Text


class ForkService:
    def __init__(self, runtime, collaboration, workspace, delegation):
        self.runtime = runtime
        self.db = runtime.db
        self.collaboration = collaboration
        self.workspace = workspace
        self.delegation = delegation
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS assistant_forks (
                session_id TEXT PRIMARY KEY,
                parent_session_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                digest TEXT NOT NULL,
                details TEXT NOT NULL,
                status TEXT NOT NULL,
                UNIQUE(parent_session_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS assistant_jobs (
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES assistant_forks(session_id),
                input TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                turn_id TEXT UNIQUE REFERENCES turns(id)
            );
            CREATE INDEX IF NOT EXISTS assistant_jobs_status ON assistant_jobs(status, job_id);
            CREATE TABLE IF NOT EXISTS assistant_handoffs (
                assistant_id TEXT NOT NULL REFERENCES assistant_forks(session_id),
                request_id TEXT NOT NULL, digest TEXT NOT NULL,
                job_id INTEGER NOT NULL REFERENCES assistant_jobs(job_id),
                PRIMARY KEY (assistant_id, request_id)
            );
        ''')

    @staticmethod
    def _encode(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    def get(self, assistant_id):
        row = self.db.execute('SELECT details, status FROM assistant_forks WHERE session_id = ?',
                              (assistant_id,)).fetchone()
        if row is None:
            raise TeamNotFound('Assistant fork not found')
        result = json.loads(row['details'])
        result['status'] = row['status']
        return result

    def exists(self, assistant_id):
        return self.db.execute('SELECT 1 FROM assistant_forks WHERE session_id = ?', (assistant_id,)).fetchone() is not None

    def authorize_workspace(self, assistant_id):
        if self.exists(assistant_id):
            try:
                self.require_active(assistant_id)
            except (TeamConflict, TeamNotFound) as exc:
                raise WorkspaceNotFound("Assistant scope is no longer available") from exc

    def material_readers(self, assistant_id):
        return self.require_active(assistant_id)['contact_session_ids'] if self.exists(assistant_id) else []

    def get_for(self, assistant_id, actor):
        record = self.get(assistant_id)
        if actor.session_id not in {record['parent_session_id'], *record['contact_session_ids']}:
            raise TeamNotFound('Assistant fork not found')
        if actor.team_id != record['team_id']:
            raise TeamNotFound('Assistant fork not found')
        return record

    def _check(self, record, parent):
        if parent is None or parent.session_id != record['parent_session_id'] or parent.team_id != record['team_id']:
            raise TeamNotFound('Parent session not found')
        self.delegation._goal(record['team_id'], parent, record['goal_id'], active=True)
        team = self.collaboration.teams.get(record['team_id'])
        members = {m['session_id']: m['member_id'] for m in team['members']}
        if parent.session_id not in members or members[parent.session_id] != record['parent_member_id']:
            raise TeamNotFound('Parent session not found')
        contacts = set(record['contact_session_ids'])
        if parent.session_id in contacts or not contacts <= members.keys():
            raise TeamConflict('Contact is no longer a team member')
        if record['task_id']:
            task = self.delegation._visible_task(record['team_id'], parent, record['task_id'])
            if task['goal_id'] != record['goal_id'] or task['revision'] != record['expected_revision']:
                raise TeamConflict('Task revision or goal has changed')
            active_statuses = {'ready', 'waiting_dependencies', 'in_progress', 'waiting_human'}
            if task['status'] not in active_statuses or self.delegation.results.status(task) == 'accepted':
                raise TeamConflict('Task is no longer active')
            parties = self.delegation._parties(task['dispatcher_id'], task['terms'])
            if parent.id not in parties or not {members[s] for s in contacts} <= parties:
                raise TeamConflict('Task participation has changed')
        else:
            row = self.db.execute('''SELECT id FROM delegation_proposals
                WHERE goal_id = ? AND scope_id = ? AND task_id IS NULL''',
                (record['goal_id'], record['scope_id'])).fetchone()
            if row is not None:
                proposal = self.delegation._visible_proposal(record['team_id'], parent, row['id'])
                if proposal['status'] != 'open':
                    raise TeamConflict('Negotiation scope is no longer open')
                parties = set(proposal['required_member_ids']) | {proposal['dispatcher_id'], proposal['proposer_id']}
                if parent.id not in parties or not {members[s] for s in contacts} <= parties:
                    raise TeamConflict('Negotiation participation has changed')
        for version_id in record['version_ids']:
            try:
                if parent.session_id not in self.workspace.artifacts.reference_readers(record['team_id'], version_id):
                    raise TeamConflict('Parent can no longer access an input version')
            except ArtifactError as exc:
                raise TeamConflict('Parent can no longer access an input version') from exc
        # Every inherited turn must remain a completed turn of this parent and
        # no later than the selected anchor; never inherit a sibling's history.
        anchor = self.db.execute('SELECT rowid, role, status FROM turns WHERE id = ?',
                                 (record['from_turn_id'],)).fetchone()
        if anchor is None or anchor['role'] != parent.session_id or anchor['status'] != 'completed':
            raise TeamNotFound('Completed parent turn not found')
        for turn_id in record['history_turn_ids']:
            row = self.db.execute('SELECT rowid, role, status FROM turns WHERE id = ?', (turn_id,)).fetchone()
            if row is None or row['role'] != parent.session_id or row['status'] != 'completed' or row['rowid'] > anchor['rowid']:
                raise TeamNotFound('Completed parent turn not found')

    def require_active(self, assistant_id):
        record = self.get(assistant_id)
        if record['status'] != 'active':
            raise TeamConflict('Assistant fork is no longer active')
        parent = self.collaboration.auth.member(record['parent_session_id'])
        self._check(record, parent)
        return record

    async def create(self, parent, session_id, body: ForkInput):
        if parent.session_id != session_id or parent.is_operator:
            raise TeamNotFound('Parent session not found')
        if not isinstance(body, ForkInput):
            body = ForkInput.model_validate(body)
        history = list(dict.fromkeys(body.history_turn_ids or [body.from_turn_id]))
        if body.from_turn_id not in history:
            raise CollaborationInvalid('history_turn_ids must contain the anchor turn')
        data = body.model_dump()
        data['history_turn_ids'] = history
        for name in ('version_ids', 'allowed_tools', 'contact_session_ids'):
            data[name] = sorted(set(data[name]))
        digest = hashlib.sha256(self._encode(data).encode('utf-8')).hexdigest()
        assistant_id = uuid5(UUID(session_id), 'fork:' + body.request_id).hex
        row = self.db.execute('SELECT digest, session_id FROM assistant_forks WHERE parent_session_id = ? AND request_id = ?',
                              (session_id, body.request_id)).fetchone()
        if row is not None:
            if row['digest'] != digest:
                raise TeamConflict('Request ID reused with different input')
            return self.get(row['session_id'])
        record = {'session_id': assistant_id, 'parent_session_id': session_id,
                  'parent_member_id': parent.id, 'team_id': parent.team_id, 'goal_id': body.goal_id,
                  'scope_id': body.scope_id or body.task_id, 'task_id': body.task_id,
                  'expected_revision': body.expected_revision, 'history_turn_ids': history,
                  'from_turn_id': body.from_turn_id, 'version_ids': data['version_ids'],
                  'history_version_ids': data['version_ids'],
                  'allowed_tools': data['allowed_tools'], 'contact_session_ids': data['contact_session_ids'],
                  'purpose': body.purpose}
        self._check(record, parent)
        try:
            details = self.workspace.assistant_details(parent_session_id=session_id, session_id=assistant_id,
                        scope_id=record['scope_id'], prompt=body.purpose, version_ids=record['version_ids'],
                        allowed_tools=record['allowed_tools'])
        except ValueError as exc:
            raise CollaborationInvalid(str(exc)) from exc
        await asyncio.to_thread(self.workspace.sandbox.prepare, details['workspace_id'])
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT digest, session_id FROM assistant_forks WHERE parent_session_id = ? AND request_id = ?',
                                  (session_id, body.request_id)).fetchone()
            if row is not None:
                if row['digest'] != digest:
                    raise TeamConflict('Request ID reused with different input')
                return self.get(row['session_id'])
            self._check(record, self.collaboration.auth.member(session_id))
            self.workspace.install_assistant(details)
            self.db.execute('INSERT INTO assistant_forks VALUES (?, ?, ?, ?, ?, ?)',
                            (assistant_id, session_id, body.request_id, digest, self._encode(record), 'active'))
            self.db.execute('INSERT INTO assistant_jobs(session_id, input, status, turn_id) VALUES (?, ?, ?, NULL)',
                            (assistant_id, self._encode({'type': 'fork.initial', 'purpose': body.purpose,
                                                        'goal_id': record['goal_id'], 'scope_id': record['scope_id'],
                                                        'task_id': record['task_id'],
                                                        'expected_revision': record['expected_revision'],
                                                        'version_ids': record['version_ids'],
                                                        'parent_session_id': session_id,
                                                        'contact_session_ids': record['contact_session_ids']}), 'pending'))
        return self.get(assistant_id)

    def revoke(self, assistant_id, actor):
        self.require_parent(assistant_id, actor)
        with self.db:
            self.db.execute("UPDATE assistant_forks SET status = 'revoked' WHERE session_id = ?", (assistant_id,))
            self.db.execute('UPDATE assistant_workspaces SET enabled = 0 WHERE session_id = ?', (assistant_id,))
            running = self.db.execute("""
                SELECT turn_id FROM assistant_jobs WHERE session_id = ? AND status = 'running'
            """, (assistant_id,)).fetchall()
            for row in running:
                self.runtime.journal.next_generation(row['turn_id'])
                self.runtime._write_finish(row['turn_id'], 'interrupted', error='Assistant scope revoked')
            self.db.execute("UPDATE assistant_jobs SET status = 'cancelled' WHERE session_id = ? AND status = 'pending'", (assistant_id,))
        active = self.runtime.active.get(assistant_id)
        if active is not None:
            active.task.cancel()
        return self.get(assistant_id)

    def require_parent(self, assistant_id, actor):
        fork = self.get(assistant_id)
        if actor.session_id != fork['parent_session_id'] or actor.team_id != fork['team_id']:
            raise TeamNotFound('Assistant fork not found')
        return fork

    def _turn_peer(self, assistant_id, turn_id, default):
        row = self.db.execute('SELECT input FROM assistant_jobs WHERE session_id = ? AND turn_id = ?',
                              (assistant_id, turn_id)).fetchone()
        if row is None:
            return None
        return json.loads(row['input']).get('sender_session_id', default)

    def observable_turns(self, assistant_id, actor):
        self.require_parent(assistant_id, actor)
        self.require_active(assistant_id)
        return [{**turn, "reply": None, "error": None} for turn in self.runtime.list_turns(assistant_id)
                if self._turn_peer(assistant_id, turn['turn_id'], actor.session_id) == actor.session_id]

    def model_history(self, assistant_id, turn_id):
        fork = self.require_active(assistant_id)
        parent = fork['parent_session_id']
        peer = self._turn_peer(assistant_id, turn_id, parent)
        inherited = project_history(self, assistant_id)
        rows = self.db.execute("""
            SELECT t.history, j.input FROM assistant_jobs j JOIN turns t ON t.id = j.turn_id
            WHERE j.session_id = ? AND t.status = 'completed' ORDER BY j.job_id
        """, (assistant_id,))
        prior = [message for row in rows
                 if json.loads(row['input']).get('sender_session_id', parent) == peer
                 for message in json.loads(row['history'])]
        return inherited + prior

    def handoff(self, assistant_id, actor, body: ForkHandoffInput):
        self.require_parent(assistant_id, actor)
        versions = sorted(set(body.version_ids))
        data = {'version_ids': versions, 'note': body.note}
        digest = self.collaboration._digest(data)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            fork = self.require_active(assistant_id)
            existing = self.db.execute("""
                SELECT digest, job_id FROM assistant_handoffs WHERE assistant_id = ? AND request_id = ?
            """, (assistant_id, body.request_id)).fetchone()
            if existing:
                if existing['digest'] != digest:
                    raise TeamConflict('Request ID reused with different material')
                return {'job_id': existing['job_id'], **data}

            for version in versions:
                self.workspace.artifacts.resolve(fork['team_id'], actor.session_id, version)
            fork['version_ids'] = sorted(set(fork['version_ids']) | set(versions))
            details = self.workspace.actor(assistant_id)
            serialized = self._encode({**vars(details), 'versions': fork['version_ids']})
            self.db.execute('UPDATE assistant_workspaces SET details = ? WHERE session_id = ?', (serialized, assistant_id))
            self.db.execute('UPDATE assistant_forks SET details = ? WHERE session_id = ?',
                            (self._encode({k: v for k, v in fork.items() if k != 'status'}), assistant_id))
            job = {'type': 'fork.handoff', 'sender_session_id': actor.session_id, **data}
            queued = self.db.execute('INSERT INTO assistant_jobs(session_id, input) VALUES (?, ?)',
                                     (assistant_id, self._encode(job))).lastrowid
            self.db.execute('INSERT INTO assistant_handoffs VALUES (?, ?, ?, ?)',
                            (assistant_id, body.request_id, digest, queued))
            return {'job_id': queued, **data}

    def tools_for(self, role):
        if self.collaboration.auth.member(role) is None:
            return []
        return [{'type': 'function', 'function': {
            'name': 'fork_session',
            'description': 'Delegate bounded work to an assistant from your own completed turn. Specify the active '
                           'goal and negotiation scope or task revision, authorized history, materials and contacts.',
            'parameters': ForkInput.model_json_schema(),
        }}]

    async def execute_create(self, role, arguments):
        parent = self.collaboration.auth.member(role)
        if parent is None:
            return {'ok': False, 'error': 'Only a formal member may create an assistant'}
        try:
            body = ForkInput.model_validate_json(arguments)
            return {'ok': True, 'result': await self.create(parent, role, body)}
        except ValidationError:
            return {'ok': False, 'error': 'Invalid assistant fork arguments'}
        except (TeamConflict, TeamNotFound, CollaborationInvalid, ArtifactError, WorkspaceNotFound) as exc:
            return {'ok': False, 'error': str(exc)}
