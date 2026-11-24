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

    def client_with_test_model(**kwargs):
        if (kwargs.get("headers") or {}).get("Authorization") == "Bearer test-only":
            kwargs["transport"] = httpx.MockTransport(completion)
        return client_class(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_with_test_model)
    monkeypatch.setenv("COLLABORATION_MODEL_MODE", "course")
    monkeypatch.setenv("COURSE_API_BASE_URL", "https://course.example/v1")
    monkeypatch.setenv("COURSE_API_MODEL", "course-test")
    monkeypatch.setenv("COURSE_API_KEY", "test-only")

    with TestClient(create_app(sandbox=sandbox, state_dir=tmp_path, operator_token='test-operator-token')) as client:
        client.headers.update({"Authorization": "Bearer test-operator-token"})
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
            system_prompt = messages[0]["content"]
            assert system_prompt.startswith(expected_prompt)
            assert "Return at most 8 tool calls" in system_prompt[len(expected_prompt):]
            assert {"run_command", "read_file", "publish_artifact"} <= {tool["function"]["name"] for tool in tools}
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
        with TestClient(create_app(model=Model(), sandbox=sandbox, state_dir=tmp_path, operator_token='test-operator-token', bootstrap=bootstrap)) as client:
            client.headers.update({"Authorization": "Bearer test-operator-token"})
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
            credential = client.post(f"/v1/teams/{teams[0]['team_id']}/members/{developer['member_id']}/credential").json()["token"]
            member_auth = {"Authorization": f"Bearer {credential}"}
            turn = client.post(path, json={"text": "Run your workspace smoke check"}, headers=member_auth).json()["turn_id"]
            client.get(f"{path}/{turn}/events", headers=member_auth)
            assert client.get(f"{path}/{turn}", headers=member_auth).json()["reply"] == "Workspace write succeeded"
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


def test_registered_members_file_tools_and_version_handoff(sandbox, tmp_path, monkeypatch):
    class Model:
        async def complete(self, _messages, _tools):
            raise AssertionError("Workspace tests call the registered tool handlers directly")

    async def bootstrap(_roles):
        return LeadDecision("lead", "Coordinates the software roster")

    monkeypatch.setenv("COURSE_API_KEY", "host-only-t005-canary")
    sample = Path(__file__).resolve().parents[1] / "test samples" / "scenario_1_software_engineering 2"
    roster = (sample / "01_role_initialization.md").read_text(encoding="utf-8")
    workspaces = []
    try:
        app = create_app(model=Model(), sandbox=sandbox, state_dir=tmp_path, operator_token='test-operator-token', bootstrap=bootstrap)
        with TestClient(app) as client:
            client.headers.update({"Authorization": "Bearer test-operator-token"})
            response = client.post("/v1/teams", json={"request_id": "t005-docker", "roster": roster})
            assert response.status_code == 201, response.text
            members = {item["role_id"]: item for item in response.json()["members"]}
            workspaces.extend(item["workspace_id"] for item in members.values())
            dev, tester, outsider = (members[role]["session_id"] for role in ("dev", "qa", "release"))

            def tool(session_id, name, **arguments):
                return client.portal.call(app.state.workspace.execute, session_id, name, json.dumps(arguments))

            def ok(session_id, name, **arguments):
                result = tool(session_id, name, **arguments)
                assert result["ok"], result
                return result["result"]

            path = "package/booking.py"
            old = "def slots():\n    return 1\n"
            new = "def slots():\n    return 2\n"
            ok(dev, "create_file", path=path, content=old)
            ok(dev, "create_file", path="private.txt", content="private")
            assert ok(dev, "read_file", path=path)["content"] == old
            assert not tool(dev, "create_file", path=path, content="overwrite")["ok"]
            assert not tool(dev, "read_file", path="../private.txt")["ok"]
            version1 = ok(dev, "publish_artifact", path=path, reader_session_ids=[tester])
            ok(dev, "update_file", path=path, content=new)
            version2 = ok(dev, "publish_artifact", path=path, reader_session_ids=[tester], artifact_id=version1["artifact_id"])
            assert (version1["version"], version2["version"]) == (1, 2)
            assert ok(tester, "read_artifact", version_id=version1["version_id"])["content"] == old
            assert ok(tester, "read_artifact", version_id=version2["version_id"])["content"] == new

            old_path = version1["path"]
            result = ok(tester, "run_command", command=(
                "test ! -e private.txt && python -I -c "
                f"\"data = {{}}; exec(open('{old_path}').read(), data); assert data['slots']() == 1\""
            ), artifact_version_ids=[version1["version_id"]])
            assert result["exit_code"] == 0 and not result["timed_out"], result
            denied = ok(tester, "run_command", command=f"printf changed > {old_path}", artifact_version_ids=[version1["version_id"]])
            assert denied["exit_code"] != 0, denied
            assert not tool(outsider, "read_artifact", version_id=version1["version_id"])["ok"]
            assert not tool(outsider, "run_command", command="true", artifact_version_ids=[version1["version_id"]])["ok"]
            assert not tool(outsider, "read_file", path="private.txt")["ok"]
            assert ok(outsider, "run_command", command='test -z "$COURSE_API_KEY" && test ! -e private.txt')["exit_code"] == 0

            tokens = {role: client.post(f"/v1/teams/{response.json()['team_id']}/members/{members[role]['member_id']}/credential").json()["token"] for role in ("qa", "release")}
            endpoint = f"/v1/artifacts/{version1['artifact_id']}/versions/1"
            assert client.get(endpoint, headers={"Authorization": f"Bearer {tokens['qa']}"}).text == old
            assert client.get(endpoint, headers={"Authorization": f"Bearer {tokens['release']}"}).status_code == 404
            assert ok(dev, "run_command", command="ln -s package nested-link; ln -s private.txt unsafe.txt")["exit_code"] == 0
            for unsafe in ("nested-link/booking.py", "unsafe.txt"):
                assert not tool(dev, "read_file", path=unsafe)["ok"]
                assert not tool(dev, "update_file", path=unsafe, content="changed")["ok"]
                assert not tool(dev, "delete_file", path=unsafe)["ok"]
            ok(dev, "delete_file", path=path)
            assert not tool(dev, "read_file", path=path)["ok"]
            assert ok(dev, "read_file", path="private.txt")["content"] == "private"
            assert client.get(endpoint, headers={"Authorization": f"Bearer {tokens['qa']}"}).text == old
    finally:
        if workspaces:
            sandbox.cleanup(*workspaces)
