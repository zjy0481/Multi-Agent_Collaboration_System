"""T011 approval acceptance using a local bare Git repository, never GitHub."""
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from test_collaboration import OPERATOR, auth, bootstrap, send, setup_team
from test_scheduler import Sandbox, current_delivery, tool, turns, wait_until


class FileSandbox(Sandbox):
    def read_bytes(self, workspace_id, path):
        assert path == "demo.txt"
        return b"T011 test content\n"


class GitModel:
    def __init__(self):
        self.sha = None
        self.approval = None
        self.notifications = []

    async def complete(self, messages, tools):
        payload = current_delivery(messages)
        if payload["delivery"]["purpose"] != "message":
            self.notifications.append(payload)
            return {"role": "assistant", "content": "Push decision received"}
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        commit = next((m for m in tool_messages if m.get("tool_call_id") == "commit-call"), None)
        push = next((m for m in tool_messages if m.get("tool_call_id") == "push-call"), None)
        if commit is None:
            return tool("create_git_commit", "commit-call", {
                "repository": "demo", "ref": "refs/heads/main", "paths": ["demo.txt"], "message": "test commit",
            })
        commit_result = json.loads(commit["content"])
        assert commit_result["ok"], commit_result
        self.sha = commit_result["result"]["sha"]
        if push is None:
            return tool("request_push", "push-call", {
                "request_id": "push-request", "repository": "demo", "ref": "refs/heads/main", "sha": self.sha,
            })
        push_result = json.loads(push["content"])
        assert push_result["ok"], push_result
        self.approval = push_result["result"]
        return {"role": "assistant", "content": "Awaiting approval"}


def create_app_for_test(tmp_path, model, sandbox, executor=None):
    targets = tmp_path / "targets"
    targets.mkdir(exist_ok=True)
    return create_app(model=model, sandbox=sandbox, state_dir=tmp_path / "state", bootstrap=bootstrap,
                      operator_token=OPERATOR, schedule=True, git_targets_dir=targets, git_executor=executor)


def prepare_target(tmp_path):
    targets = tmp_path / "targets"
    targets.mkdir()
    subprocess.run(["git", "init", "--bare", str(targets / "demo.git")], check=True, capture_output=True)
    return targets / "demo.git"


def read_ref(repo):
    result = subprocess.run(["git", "--git-dir", str(repo), "rev-parse", "--verify", "refs/heads/main"],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def approvals(client, base):
    response = client.get(f"{base}/approvals", headers=auth(OPERATOR))
    assert response.status_code == 200, response.text
    return response.json()


def action(client, base, member_token, action_id):
    response = client.get(f"{base}/git-actions/{action_id}", headers=auth(member_token))
    assert response.status_code == 200, response.text
    return response.json()


def decision_payload(approval, decision, request_id):
    return {"request_id": request_id, "decision": decision, **{
        key: approval[key] for key in ("action", "repository", "ref", "sha", "digest")
    }}


@pytest.mark.parametrize("decision, expected", [("approve", "succeeded"), ("reject", "rejected")])
def test_bound_approval_replay_and_real_local_git(tmp_path, decision, expected):
    repo = prepare_target(tmp_path)
    model = GitModel()
    with TestClient(create_app_for_test(tmp_path, model, FileSandbox())) as client:
        base, lead, _, tokens = setup_team(client)
        member_token = tokens[lead["member_id"]]
        send(client, base, OPERATOR, request_id="work", text="Commit and request push")
        approval = wait_until(lambda: next(iter(approvals(client, base)), None))
        assert approval["status"] == "awaiting_approval"
        assert approval["action"] == "push" and approval["repository"] == "demo"
        assert approval["ref"] == "refs/heads/main" and len(approval["sha"]) == 40
        assert read_ref(repo) is None
        turn = wait_until(lambda: next((t for t in turns(client, lead["session_id"], member_token)
                                        if t["status"] == "waiting_approval"), None))
        assert turn
        endpoint = f"/v1/approvals/{approval['approval_id']}/decision"
        payload = decision_payload(approval, decision, "decision-1")
        denied = client.post(endpoint, headers=auth(member_token), json=payload)
        assert denied.status_code == 403, denied.text
        assert read_ref(repo) is None
        assert action(client, base, member_token, approval["action_id"])["call_id"] == "push-call"
        for field, wrong in [("action", "commit"), ("repository", "other"), ("ref", "refs/heads/other"),
                             ("sha", "0" * 40), ("digest", "wrong")]:
            response = client.post(endpoint, headers=auth(OPERATOR), json={**payload, field: wrong})
            assert response.status_code in (403, 409, 422), response.text
            assert read_ref(repo) is None
        response = client.post(endpoint, headers=auth(OPERATOR), json=payload)
        assert response.is_success, response.text
        replay = client.post(endpoint, headers=auth(OPERATOR), json=payload)
        assert replay.is_success and replay.json() == response.json()
        final = wait_until(lambda: (lambda a: a if a["status"] == expected else None)(
            action(client, base, member_token, approval["action_id"])))
        assert final["status"] == expected
        after_completion = client.post(endpoint, headers=auth(OPERATOR), json=payload)
        assert after_completion.is_success and after_completion.json() == response.json()
        assert final["call_id"] == "push-call"
        assert read_ref(repo) == (approval["sha"] if decision == "approve" else None)
        assert wait_until(lambda: model.notifications)


def test_unknown_push_is_observed_after_restart_without_repeating(tmp_path):
    from collaboration_runtime.git_executor import GitExecutor

    class UnknownOnce(GitExecutor):
        def __init__(self, state_dir, targets_dir):
            super().__init__(state_dir, targets_dir)
            self.push_calls = 0
            self.allow_observe = False

        def push(self, session_id, repository, ref, sha):
            self.push_calls += 1
            assert super().push(session_id, repository, ref, sha) == "succeeded"
            return "pending_verification"

        def observe(self, repository, ref, sha):
            if not self.allow_observe:
                return "pending_verification"
            return super().observe(repository, ref, sha)

    repo = prepare_target(tmp_path)
    executor = UnknownOnce(tmp_path / "state", tmp_path / "targets")
    model = GitModel()
    with TestClient(create_app_for_test(tmp_path, model, FileSandbox(), executor)) as client:
        base, lead, _, tokens = setup_team(client)
        member_token = tokens[lead["member_id"]]
        send(client, base, OPERATOR, request_id="work", text="Commit and request push")
        approval = wait_until(lambda: next(iter(approvals(client, base)), None))
        response = client.post(f"/v1/approvals/{approval['approval_id']}/decision", headers=auth(OPERATOR),
                               json=decision_payload(approval, "approve", "decision-1"))
        assert response.is_success, response.text
        wait_until(lambda: action(client, base, member_token, approval["action_id"])["status"] == "pending_verification")
        assert read_ref(repo) == approval["sha"]
        assert executor.push_calls == 1
    executor.allow_observe = True
    with TestClient(create_app_for_test(tmp_path, model, FileSandbox(), executor)) as client:
        wait_until(lambda: action(client, base, member_token, approval["action_id"])["status"] == "succeeded")
        assert executor.push_calls == 1
        assert read_ref(repo) == approval["sha"]
