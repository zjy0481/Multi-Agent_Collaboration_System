"""T001: roster import, idempotent provisioning, and membership readiness."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.sandbox import SandboxError
from collaboration_runtime.teams import LeadDecision


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = [
    ("scenario_1_software_engineering 2", "lead"),
    ("scenario_2_newsroom 2", "chief_editor"),
    ("scenario_3_securities_fund 2", "fund_manager"),
]


class Sandbox:
    def __init__(self):
        self.prepared = []
        self.fail_next = False

    def prepare(self, workspace_id):
        self.prepared.append(workspace_id)
        if self.fail_next:
            self.fail_next = False
            raise SandboxError("workspace unavailable")


class Model:
    async def complete(self, messages, tools):
        raise AssertionError("bootstrap must use the injected decision callable")


def sample(directory):
    return (ROOT / "test samples" / directory / "01_role_initialization.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("directory,lead_role", SAMPLES)
def test_three_course_rosters_preserve_roles_and_lead(tmp_path, directory, lead_role):
    seen = []

    async def bootstrap(roles):
        assert isinstance(roles, tuple)
        seen.extend(roles)
        return LeadDecision(role_id=lead_role, reason="Coordinates the team's work")

    sandbox = Sandbox()
    with TestClient(create_app(model=Model(), sandbox=sandbox, state_dir=tmp_path, bootstrap=bootstrap)) as client:
        roster = sample(directory)
        response = client.post("/v1/teams", json={"request_id": "create-1", "roster": roster})
        assert response.status_code == 201, response.text
        team = response.json()
        assert len(team["members"]) == len(seen) == 4
        lead = next(m for m in team["members"] if m["member_id"] == team["lead_member_id"])
        assert lead["role_id"] == lead_role
        assert team["lead_reason"] == "Coordinates the team's work"
        assert team["generated_lead"] is False
        assert team["bootstrap"]["status"] == "completed"
        assert team["bootstrap"]["session_id"]
        assert len({m["workspace_id"] for m in team["members"]}) == 4
        assert len({m["session_id"] for m in team["members"]}) == 4
        for member in team["members"]:
            assert member["prompt"] and member["prompt"] in roster
        assert set(sandbox.prepared) == {m["workspace_id"] for m in team["members"]}
        snapshot = client.get(f"/v1/teams/{team['team_id']}")
        assert snapshot.status_code == 200
        assert snapshot.json()["team_id"] == team["team_id"]
        assert snapshot.json()["contract"]["version"] == team["contract"]["version"]


def test_fallback_lead_replay_and_team_isolation(tmp_path):
    class BootstrapModel:
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            supplied_roles = json.loads(messages[-1]["content"])
            assert {role["role_id"] for role in supplied_roles} == {f"specialist_{i}" for i in range(3)}
            assert [tool["function"]["name"] for tool in tools] == ["finish_team_setup"]
            return {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "bootstrap-test", "type": "function", "function": {
                        "name": "finish_team_setup",
                        "arguments": json.dumps({"role_id": None, "reason": "No coordinating role"}),
                    },
                }],
            }

    model = BootstrapModel()
    sandbox = Sandbox()
    table = "\n".join(["| Agent ID | Role |", "|---|---|", *(
        f"| `specialist_{i}` | Specialist {i} |" for i in range(3)
    )])
    roster = table + "\n\n" + "\n\n".join(
        f"## Specialist {i} (specialist_{i})\n\n```text\nInvestigate subject {i}.\n```"
        for i in range(3)
    )
    with TestClient(create_app(model=model, sandbox=sandbox, state_dir=tmp_path)) as client:
        payload = {"request_id": "one", "name": "Research", "roster": roster}
        first = client.post("/v1/teams", json=payload)
        assert first.status_code == 201, first.text
        team = first.json()
        assert team["generated_lead"] is True
        assert len(team["members"]) == 4
        assert sum(m["member_id"] == team["lead_member_id"] for m in team["members"]) == 1
        bootstrap_path = f"/v1/sessions/{team['bootstrap']['session_id']}/turns"
        assert client.post(bootstrap_path, json={"text": "Keep going"}).status_code == 404
        before = len(sandbox.prepared)
        replay = client.post("/v1/teams", json=payload)
        assert replay.status_code == 201
        assert replay.json() == team
        assert len(sandbox.prepared) == before
        conflict = client.post("/v1/teams", json={**payload, "name": "Different"})
        assert conflict.status_code == 409
        other = client.post("/v1/teams", json={**payload, "request_id": "two"})
        assert other.status_code == 201
        assert model.calls == 2
        other_team = other.json()
        assert other_team["team_id"] != team["team_id"]
        for role_id in ("specialist_0", "specialist_1", "specialist_2"):
            left = next(m for m in team["members"] if m["role_id"] == role_id)
            right = next(m for m in other_team["members"] if m["role_id"] == role_id)
            assert left["member_id"] != right["member_id"]
            assert left["workspace_id"] != right["workspace_id"]
            assert left["session_id"] != right["session_id"]


def test_member_preparation_failure_retry_and_revision_conflicts(tmp_path):
    async def bootstrap(roles):
        return LeadDecision(role_id="lead", reason="Coordinates")

    sandbox = Sandbox()
    with TestClient(create_app(model=Model(), sandbox=sandbox, state_dir=tmp_path, bootstrap=bootstrap)) as client:
        created = client.post("/v1/teams", json={"request_id": "create", "roster": sample(SAMPLES[0][0])})
        assert created.status_code == 201, created.text
        team = created.json()
        url = f"/v1/teams/{team['team_id']}/members"
        version = team["contract"]["version"]
        payload = {"request_id": "add-1", "expected_version": version, "role_id": "designer", "name": "Designer", "prompt": "Design the interface."}
        sandbox.fail_next = True
        failed = client.post(url, json=payload)
        assert failed.status_code == 503
        failed_workspace = sandbox.prepared[-1]
        snapshot = client.get(f"/v1/teams/{team['team_id']}").json()
        assert snapshot["contract"]["version"] == version
        assert all(m["role_id"] != "designer" for m in snapshot["members"])
        result = client.post(url, json=payload)
        assert result.status_code == 201, result.text
        updated = result.json()
        assert sandbox.prepared[-1] == failed_workspace
        assert updated["contract"]["version"] == version + 1
        old_contract = client.get(f"/v1/teams/{team['team_id']}/contracts/{version}")
        assert old_contract.json() == team["contract"]
        assert sum(m["role_id"] == "designer" for m in updated["members"]) == 1
        assert client.post(url, json=payload).json() == updated
        assert client.post(url, json={**payload, "prompt": "Different"}).status_code == 409
        assert client.post(url, json={**payload, "request_id": "stale", "role_id": "other"}).status_code == 409


def test_bad_roster_reports_line(tmp_path):
    async def bootstrap(roles):
        raise AssertionError("invalid roster must not reach bootstrap")

    with TestClient(create_app(model=Model(), sandbox=Sandbox(), state_dir=tmp_path, bootstrap=bootstrap)) as client:
        response = client.post("/v1/teams", json={"request_id": "bad", "roster": "## Broken (bad)\n\nNo fenced prompt here."})
        assert response.status_code == 422
        assert "line" in str(response.json()["detail"]).lower()


def test_concurrent_import_reuses_the_same_job(tmp_path):
    started, release = threading.Event(), threading.Event()
    decisions = []

    async def bootstrap(roles):
        decisions.append(roles)
        started.set()
        assert await asyncio.to_thread(release.wait, 10)
        return LeadDecision(role_id="lead", reason="Coordinates")

    sandbox = Sandbox()
    payload = {"request_id": "same", "roster": sample(SAMPLES[0][0])}
    with TestClient(create_app(model=Model(), sandbox=sandbox, state_dir=tmp_path, bootstrap=bootstrap)) as client:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(client.post, "/v1/teams", json=payload)
            try:
                assert started.wait(5)
                second = workers.submit(client.post, "/v1/teams", json=payload)
                conflict = client.post("/v1/teams", json={**payload, "name": "Different parameters"})
                assert conflict.status_code == 409
            finally:
                release.set()
            first_response, second_response = first.result(), second.result()
            assert first_response.status_code == second_response.status_code == 201
            assert first_response.json() == second_response.json()
            assert len(decisions) == 1
            assert len(sandbox.prepared) == len(set(sandbox.prepared)) == 4


def test_failed_initial_preparation_resumes_after_restart(tmp_path):
    decisions = []

    async def bootstrap(roles):
        decisions.append(roles)
        return LeadDecision(role_id="lead", reason="Coordinates")

    sandbox = Sandbox()
    sandbox.fail_next = True
    payload = {"request_id": "restart", "roster": sample(SAMPLES[0][0])}
    with TestClient(create_app(model=Model(), sandbox=sandbox, state_dir=tmp_path, bootstrap=bootstrap)) as client:
        assert client.post("/v1/teams", json=payload).status_code == 503
    failed_workspace = sandbox.prepared[0]
    with TestClient(create_app(model=Model(), sandbox=sandbox, state_dir=tmp_path, bootstrap=bootstrap)) as client:
        result = client.post("/v1/teams", json=payload)
        assert result.status_code == 201, result.text
        assert len(decisions) == 1
        assert sandbox.prepared[1] == failed_workspace
        assert any(member["workspace_id"] == failed_workspace for member in result.json()["members"])
