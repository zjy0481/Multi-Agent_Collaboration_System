"""Run against the Docker Engine when RUN_DOCKER_TESTS=1."""

import hashlib
import json
import os
from pathlib import Path
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.sandbox import DockerSandbox, SandboxError
from collaboration_runtime.teams import LeadDecision

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_TESTS") != "1",
    reason="Set RUN_DOCKER_TESTS=1 after building the sandbox image",
)


@pytest.fixture
def sandbox(tmp_path):
    instance = DockerSandbox(tmp_path)
    try:
        yield instance
    finally:
        instance.cleanup()


def run_ok(sandbox, role, command):
    result = sandbox.run(role, command)
    assert result["exit_code"] == 0 and not result["timed_out"], result
    return result["stdout"]


def test_isolated_workspaces_and_immutable_handoff(sandbox, monkeypatch):
    monkeypatch.setenv("COURSE_API_KEY", "dummy-host-credential")
    run_ok(sandbox, "developer", "printf draft > private.txt; printf v1 > handoff.txt")
    run_ok(sandbox, "tester", 'test ! -e private.txt && test ! -e handoff.txt && test -z "$COURSE_API_KEY"')
    run_ok(sandbox, "tester", 'test "$(id -u)" = 10001 && printf spec > qa.txt')
    assert sandbox.list_artifacts("tester") == []

    first = sandbox.publish("developer", "handoff.txt", "tester")
    assert first["sha256"] == hashlib.sha256(b"v1").hexdigest()
    assert sandbox.list_artifacts("tester") == [first]
    assert sandbox.list_artifacts("developer") == []
    assert run_ok(sandbox, "tester", f"cat {first['path']}") == "v1"
    run_ok(sandbox, "developer", f"test ! -e qa.txt && test ! -e {first['path']}")
    attempt = sandbox.run("tester", f"printf overwrite > {first['path']}")
    assert attempt["exit_code"] != 0 and not attempt["timed_out"], attempt

    run_ok(sandbox, "developer", "printf v2 > handoff.txt; ln -s handoff.txt link.txt")
    second = sandbox.publish("developer", "handoff.txt", "tester")
    assert second["id"] != first["id"]
    assert run_ok(sandbox, "tester", f"cat {first['path']} {second['path']}") == "v1v2"
    with pytest.raises(SandboxError):
        sandbox.publish("developer", "link.txt", "tester")
    with pytest.raises(SandboxError):
        sandbox.publish("developer", "../handoff.txt", "tester")


def test_no_egress_and_timeout_removes_container(sandbox):
    interfaces = run_ok(sandbox, "developer", "ls /sys/class/net").splitlines()
    assert interfaces == ["lo"]
    sandbox.timeout = 3
    timed = sandbox.run("developer", "sleep 30")
    assert timed["timed_out"]
    assert "cleanup_error" not in timed
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={sandbox.prefix}-run-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert not result.stdout.strip(), result.stdout


def test_two_roles_handoff_through_runtime_api(sandbox, tmp_path, monkeypatch):
    def completion(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-only"
        payload = json.loads(request.content)
        assert {tool["function"]["name"] for tool in payload["tools"]} == {
            "run_command", "list_artifacts", "publish_artifact",
        }
        messages = payload["messages"]
        is_developer = messages[0]["content"].startswith("You are the developer.")
        results = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
        assert all(item["ok"] for item in results)
        if len(results) == 2:
            if is_developer:
                assert results[-1]["result"]["author"] == "developer"
            else:
                assert results[-1]["result"]["stdout"] == "from dev"
            message = {"role": "assistant", "content": "Published." if is_developer else "Verified."}
        else:
            if is_developer and not results:
                name, arguments = "run_command", {"command": "printf 'from dev' > check.txt"}
            elif is_developer:
                name, arguments = "publish_artifact", {"path": "check.txt", "recipient": "tester"}
            elif not results:
                name, arguments = "list_artifacts", {}
            else:
                artifact = results[-1]["result"][0]
                name, arguments = "run_command", {"command": f"test ! -e check.txt && cat {artifact['path']}"}
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call-{len(results)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            }
        return httpx.Response(200, json={"choices": [{"message": message}]})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(completion), **kwargs,
    ))
    monkeypatch.setenv("COURSE_API_BASE_URL", "https://course.example/v1")
    monkeypatch.setenv("COURSE_API_MODEL", "course-test")
    monkeypatch.setenv("COURSE_API_KEY", "test-only")

    with TestClient(create_app(sandbox=sandbox, state_dir=tmp_path)) as client:
        for role, reply in (("developer", "Published."), ("tester", "Verified.")):
            endpoint = f"/v1/sessions/{role}/turns"
            response = client.post(endpoint, json={"text": "Run the sample task"})
            assert response.status_code == 202
            turn = f"{endpoint}/{response.json()['turn_id']}"
            events = client.get(f"{turn}/events")
            assert events.text.count("event: tool.completed") == 2
            assert client.get(turn).json()["reply"] == reply


def test_registered_member_sessions_use_independent_docker_workspaces(sandbox, tmp_path):
    sample_root = Path(__file__).resolve().parents[1] / "test samples"
    samples = [
        ("scenario_1_software_engineering 2", "lead"),
        ("scenario_2_newsroom 2", "chief_editor"),
        ("scenario_3_securities_fund 2", "fund_manager"),
    ]
    lead_role = None
    expected_prompt = None

    async def bootstrap(_roles):
        return LeadDecision(lead_role, "Controlled T001 Lead decision")

    class Model:
        async def complete(self, messages, tools):
            assert messages[0]["content"] == expected_prompt
            assert [tool["function"]["name"] for tool in tools] == ["run_command"]
            if messages[-1]["role"] == "tool":
                execution = json.loads(messages[-1]["content"])
                assert execution["ok"] and execution["result"]["exit_code"] == 0
                return {"role": "assistant", "content": "Workspace write succeeded"}
            return {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "write-file", "type": "function",
                    "function": {"name": "run_command", "arguments": json.dumps({
                        "command": "printf t001 > owner.txt",
                    })},
                }],
            }

    workspaces = []
    try:
        with TestClient(create_app(model=Model(), sandbox=sandbox, state_dir=tmp_path, bootstrap=bootstrap)) as client:
            teams = []
            for index, (directory, lead_role) in enumerate(samples):
                roster = (sample_root / directory / "01_role_initialization.md").read_text(encoding="utf-8")
                response = client.post("/v1/teams", json={"request_id": f"course-{index}", "roster": roster})
                assert response.status_code == 201, response.text
                team = response.json()
                workspaces.extend(member["workspace_id"] for member in team["members"])
                assert len(team["members"]) == 4
                teams.append(team)

            lead_role = None
            fallback = client.post("/v1/teams", json={
                "request_id": "fallback", "roster": "| Agent ID | Role |\n|---|---|\n| `dev` | Developer |\n"
                "\n## Developer (dev)\n\n```text\nWrite the product.\n```\n",
            })
            assert fallback.status_code == 201, fallback.text
            generated = fallback.json()
            workspaces.extend(member["workspace_id"] for member in generated["members"])
            assert generated["generated_lead"] and len(generated["members"]) == 2
            assert len(workspaces) == len(set(workspaces)) == 14

            developer = next(member for member in teams[0]["members"] if member["role_id"] == "dev")
            same_role_elsewhere = next(member for member in generated["members"] if member["role_id"] == "dev")
            expected_prompt = developer["prompt"]
            path = f"/v1/sessions/{developer['session_id']}/turns"
            turn = client.post(path, json={"text": "Run your workspace smoke check"}).json()["turn_id"]
            client.get(f"{path}/{turn}/events")
            assert client.get(f"{path}/{turn}").json()["reply"] == "Workspace write succeeded"
            assert run_ok(sandbox, developer["workspace_id"], "cat owner.txt") == "t001"
            run_ok(sandbox, same_role_elsewhere["workspace_id"], "test ! -e owner.txt")
            added = client.post(f"/v1/teams/{teams[0]['team_id']}/members", json={
                "request_id": "add-qa-auditor", "expected_version": 1, "role_id": "auditor",
                "name": "Auditor", "prompt": "Check acceptance evidence.",
            })
            assert added.status_code == 201, added.text
            auditor = next(member for member in added.json()["members"] if member["role_id"] == "auditor")
            workspaces.append(auditor["workspace_id"])
            assert added.json()["contract"]["version"] == 2
            run_ok(sandbox, auditor["workspace_id"], "test -w /workspace && test ! -e owner.txt")
    finally:
        if workspaces:
            sandbox.cleanup(*workspaces)
