import json
import queue
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.scheduler import _delivery_guidance
from collaboration_runtime.context_window import _compact_delivery
from collaboration_runtime.delegation import DelegationService
from collaboration_runtime.teams import LeadDecision
from test_collaboration import OPERATOR, ROOT, Sandbox, auth, send


def test_stage_guidance_is_scoped():
    def payload(purpose, **extra):
        return {"delivery": {"purpose": purpose}, **extra}
    assignment = payload('message', message={'kind': 'assignment'})
    assert 'output_grants' in _delivery_guidance(assignment, 'lead')['assignment']
    assert _delivery_guidance(payload('message', message={'kind': 'chat'}), 'lead') is None
    task = {'id': 't', 'revision': 1, 'terms': {'owner_id': 'owner'}}
    for purpose in ('task.assigned', 'task.ready', 'task.updated', 'task.returned'):
        assert 'submit_result' in _delivery_guidance(payload(purpose, task=task), 'owner')['delivery']
        assert _delivery_guidance(payload(purpose, task=task), 'reviewer') is None
    assert 'evidence' in _delivery_guidance(payload('task.submitted'))['review']
    assert _delivery_guidance(payload('task.approved'), 'lead') is None
    assert 'close_goal' in _delivery_guidance(payload('task.approved', goal_close_ready=True), 'lead')['closure']
    assert 'assign_task' in _delivery_guidance(payload('delegation.proposal', proposal_action='assign_task'))['proposal']
    assert _delivery_guidance(payload('delegation.proposal')) is None


def test_compaction_retains_stage_guidance():
    payload = {'delivery': {'purpose': 'message'}, 'message': {'kind': 'assignment'},
               'assignment_guidance': 'operator intent', 'platform_guidance': {'assignment': 'act'},
               'proposal_action': 'assign_task', 'goal_close_ready': True}
    result = json.loads(_compact_delivery({'role': 'user', 'content': json.dumps(payload)})['content'])
    for key in ('assignment_guidance', 'platform_guidance', 'proposal_action', 'goal_close_ready'):
        assert result[key] == payload[key]


def test_proposal_action_uses_current_version_and_authorization():
    service = object.__new__(DelegationService)
    class DB:
        def execute(self, *_):
            return self
        def fetchone(self):
            return {'event_id': 1}
    service.db = DB()
    record = {'id': 'p', 'version': 1}
    current = {'id': 'p', 'version': 1, 'status': 'open', 'proposer_id': 'p1',
               'required_member_ids': ['p1', 'p2'], 'confirmation_ids': ['p1', 'p2'], 'task_id': None}
    service._event_record = lambda *_: ('proposal.confirmed', record)
    service._can_read = lambda *_: True
    service._visible_proposal = lambda *_: current
    latest = {'version': 1}
    service._proposal = lambda *_: latest
    actor = SimpleNamespace(team_id='team', id='p1')
    assert service.execution_context('m', actor)['proposal_action'] == 'assign_task'
    assert service._proposal_receipt(current, 1, actor_id='p1')['proposal_action'] == 'assign_task'
    assert 'proposal_action' not in service._proposal_receipt(current, 1, actor_id='p2')
    current['task_id'] = 't'
    assert service.execution_context('m', actor)['proposal_action'] == 'update_task'
    latest['version'] = 2
    assert 'proposal_action' not in service.execution_context('m', actor)
    assert service.execution_context('m', actor)['proposal']['version'] == 1
    latest['version'] = 1
    current['confirmation_ids'] = ['p1']
    assert 'proposal_action' not in service.execution_context('m', actor)
    current['confirmation_ids'].append('p2')
    actor.id = 'p2'
    assert 'proposal_action' not in service.execution_context('m', actor)
    service._can_read = lambda *_: False
    assert service.execution_context('m', actor) is None


def test_close_guidance_only_when_lead_preview_ready():
    service = object.__new__(DelegationService)
    class DB:
        def execute(self, *_):
            return self
        def fetchone(self):
            return {'event_id': 1}
    service.db = DB()
    task = {'id': 'task', 'goal_id': 'goal', 'terms': {'reviewer_id': 'reviewer'}}
    service._event_record = lambda *_: ('task.approved', task)
    service._can_read = lambda *_: True
    service.results = SimpleNamespace(event_reference=lambda *_: {})
    preview = {'confirmation': None}
    lifecycle = SimpleNamespace(get_goal=lambda *_: {'close_preview': preview})
    service.collaboration = SimpleNamespace(goal_lifecycle=lifecycle,
                                            teams=SimpleNamespace(get=lambda *_: {'lead_member_id': 'lead'}))
    actor = SimpleNamespace(team_id='team', id='lead')
    assert 'goal_close_ready' not in service.execution_context('m', actor)
    preview['confirmation'] = 'confirmed'
    assert service.execution_context('m', actor)['goal_close_ready'] is True
    actor.id = 'owner'
    assert 'goal_close_ready' not in service.execution_context('m', actor)


@pytest.mark.parametrize(("sample_prefix", "lead_role", "owner_role", "brief"), [
    ("scenario_2", "chief_editor", "reporter", "Start with a short draft with verifiable sources."),
    ("scenario_3", "fund_manager", "research", "Start with a draft allocation and documented assumptions."),
])
def test_first_delivery_guides_other_course_teams(tmp_path, sample_prefix, lead_role, owner_role, brief):
    sample = next((ROOT / "test samples").glob(f"{sample_prefix}*"))
    roster = (sample / "01_role_initialization.md").read_text(encoding="utf-8")
    raw_task = (sample / "02_task_1.md").read_text(encoding="utf-8")
    official = re.search(r"```text\s*\n(.*?)\n```", raw_task, re.S).group(1)
    deliveries = queue.SimpleQueue()

    class Capture:
        async def complete(self, messages, tools):
            deliveries.put(json.loads(messages[-1]["content"]))
            return {"role": "assistant", "content": "Ready"}

    async def bootstrap(roles):
        return LeadDecision(role_id=lead_role, reason="Coordinates")

    with TestClient(create_app(model=Capture(), sandbox=Sandbox(), state_dir=tmp_path,
                               bootstrap=bootstrap, operator_token=OPERATOR, schedule=True)) as client:
        created = client.post("/v1/teams", headers=auth(OPERATOR), json={"request_id": "team", "roster": roster})
        assert created.status_code == 201, created.text
        team = created.json()
        owner = next(member for member in team["members"] if member["role_id"] == owner_role)
        send(client, f"/v1/teams/{team['team_id']}", OPERATOR, request_id="assignment", kind="assignment",
             text=official, assignment_guidance=brief)
        payload = deliveries.get(timeout=5)
        assert payload["message"]["text"] == official
        assert payload["assignment_guidance"] == brief
        assert "one primary deliverable" in payload["platform_guidance"]["assignment"]

        delivery = {"delivery": {"purpose": "task.ready"}, "task": {"terms": {"owner_id": owner["member_id"]}}}
        guidance = _delivery_guidance(delivery, owner["member_id"])["delivery"]
        assert "publish the exact result and evidence" in guidance and "submit_result" in guidance
