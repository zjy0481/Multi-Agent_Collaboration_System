"""Exercise configured GitHub delivery with a local Git transport stand-in."""

import json
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.git_executor import GitActionError
from test_collaboration import OPERATOR, auth, bootstrap, send, setup_team
from test_delegation import action as proposal_action, confirm_all, context, create, ok, terms
from test_git_approvals import FileSandbox, GitModel, action, approvals, decision_payload, read_ref
from test_scheduler import current_delivery, tool, wait_until

TARGET = {"alias": "demo", "repository": "example/delivery", "ref": "refs/heads/main"}
URL = "https://github.com/example/delivery.git"


@pytest.fixture
def github(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    run = subprocess.run
    run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    calls = []
    observations = {"after_push": "actual", "push_result": "actual", "gh_available": True}
    monkeypatch.delenv("COLLABORATION_GIT_TARGETS_DIR", raising=False)
    monkeypatch.setenv("COLLABORATION_GITHUB_TARGET", json.dumps(TARGET))
    global_config = tmp_path / "gitconfig"
    global_config.write_text(f'[url "file://{remote.as_posix()}"]\n\tinsteadOf = {URL}\n', encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    which = shutil.which

    def find_cli(name):
        if name != "gh":
            return which(name)
        return str(tmp_path / "gh") if observations["gh_available"] else None

    monkeypatch.setattr(shutil, "which", find_cli)

    def route(args, *positional, **kwargs):
        if not any(str(arg).startswith("https://") for arg in args):
            return run(args, *positional, **kwargs)
        assert URL in args
        assert kwargs["env"]["GIT_CONFIG_KEY_0"] == "core.hooksPath"
        assert "credential.https://github.com.helper=" in " ".join(args)
        assert "http.followRedirects=false" in args
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        verb = next(verb for verb in ("ls-remote", "fetch", "push") if verb in args)
        if verb == "push":
            assert "--force" not in args and not args[-1].startswith("+")
            if observations.get("managed"):
                assert ":refs/heads/macs/task-" in args[-1]
            else:
                assert args[-1].endswith(":" + TARGET["ref"])
        calls.append(verb)
        if verb == "push" and isinstance(observations["push_result"], tuple):
            stdout, stderr = observations["push_result"]
            return subprocess.CompletedProcess(args, 1, stdout, stderr)
        if verb == "ls-remote" and "push" in calls:
            if observations["after_push"] == "different":
                return subprocess.CompletedProcess(args, 0, b"0123456789012345678901234567890123456789\trefs/heads/main\n", b"")
            if observations["after_push"] == "unknown":
                return subprocess.CompletedProcess(args, 128, b"", b"unavailable")
        adjusted = [str(remote) if arg == URL else arg for arg in args]
        adjusted[1:1] = ["-c", "protocol.file.allow=always"]
        result = run(adjusted, *positional, **kwargs)
        if verb == "push" and observations["push_result"] == "timeout_after_update":
            assert result.returncode == 0
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return result

    monkeypatch.setattr(subprocess, "run", route)
    return remote, calls, observations


def application(tmp_path, model, *, schedule=True):
    return create_app(model=model, sandbox=FileSandbox(), state_dir=tmp_path / "state",
                      bootstrap=bootstrap, operator_token=OPERATOR, schedule=schedule)


class TaskBranchModel:
    async def complete(self, messages, tools):
        message = current_delivery(messages)["message"]
        if message["kind"] == "approval":
            return {"role": "assistant", "content": "Push decision received"}
        schemas = {entry["function"]["name"]: entry["function"]["parameters"] for entry in tools}
        assert "prepare_git_branch" in schemas
        for name in ("create_git_commit", "request_push"):
            assert not {"repository", "ref"}.intersection(schemas[name]["properties"])
        results = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        prefix = message["id"]
        if not message["reply_to"]:
            if prefix + "-branch" not in results:
                return tool("prepare_git_branch", prefix + "-branch", {})
            assert results[prefix + "-branch"]["ok"]
        if prefix + "-commit" not in results:
            return tool("create_git_commit", prefix + "-commit", {"paths": ["demo.txt"], "message": message["text"]})
        commit = results[prefix + "-commit"]
        assert commit["ok"], commit
        return tool("request_push", prefix + "-push", {"request_id": prefix, "sha": commit["result"]["sha"]})


def test_task_branch_is_created_locally_then_published_and_reused(tmp_path, github, monkeypatch):
    remote, calls, observations = github
    observations["managed"] = True
    monkeypatch.setenv("COLLABORATION_GITHUB_TARGET", json.dumps({"alias": "demo", "repository": "example/delivery"}))

    def git(*args):
        return subprocess.run(["git", *args], check=True, capture_output=True).stdout.strip().decode()

    seed = tmp_path / "seed"
    git("init", "-b", "main", str(seed))
    (seed / "base.txt").write_text("base", encoding="utf-8")
    git("-C", str(seed), "add", "base.txt")
    git("-C", str(seed), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    git("-C", str(seed), "push", str(remote), "main")
    git("--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main")
    base_sha = read_ref(remote)

    def branch_tip(ref):
        return git("--git-dir", str(remote), "for-each-ref", "--format=%(objectname)", ref)

    def pending(client):
        return wait_until(lambda: next(iter(approvals(client, base)), None))

    def decide(client, approval, decision, expected):
        endpoint = f"/v1/approvals/{approval['approval_id']}/decision"
        body = decision_payload(approval, decision, approval["approval_id"])
        assert client.post(endpoint, json=body, headers=auth(member_token)).status_code == 403
        assert client.post(endpoint, json=body, headers=auth(OPERATOR)).is_success
        wait_until(lambda: action(client, base, member_token, approval["action_id"])["status"] == expected)

    with TestClient(application(tmp_path, TaskBranchModel())) as client:
        base, lead, _, tokens = setup_team(client)
        member_token = tokens[lead["member_id"]]
        first_message = send(client, base, OPERATOR, request_id="work", text="first change")
        first = pending(client)
        ref = "refs/heads/macs/task-" + first_message["message_id"]
        assert first["ref"] == ref and first["remote_repository"] == "example/delivery"
        assert "push" not in calls and not branch_tip(ref)
        local_repo, = (tmp_path / "state" / "git" / "git-objects").glob("*.git")
        assert git("--git-dir", str(local_repo), "rev-parse", ref) == first["sha"]
        assert git("--git-dir", str(local_repo), "rev-parse", ref + "^") == base_sha
        decide(client, first, "approve", "succeeded")
        assert branch_tip(ref) == first["sha"]

    with TestClient(application(tmp_path, TaskBranchModel())) as client:
        send(client, base, OPERATOR, request_id="follow-up", text="second change", reply_to=first_message["message_id"])
        second = pending(client)
        assert second["ref"] == ref and branch_tip(ref) == first["sha"]
        decide(client, second, "approve", "succeeded")
        assert branch_tip(ref) == second["sha"]
        assert git("--git-dir", str(remote), "rev-parse", ref + "^") == first["sha"]
        send(client, base, OPERATOR, request_id="other", text="independent work")
        other = pending(client)
        assert other["ref"] != ref
        decide(client, other, "reject", "rejected")
        assert not branch_tip(other["ref"])
        assert calls.count("push") == 2 and read_ref(remote) == base_sha


def test_delegated_tasks_derive_branches_from_task_id_across_revisions(tmp_path, github, monkeypatch):
    monkeypatch.setenv("COLLABORATION_GITHUB_TARGET", json.dumps({"alias": "demo", "repository": "example/delivery"}))
    with TestClient(application(tmp_path, TaskBranchModel(), schedule=False)) as client:
        base, lead, others, tokens, goal = context(client)
        lead_id = lead["member_id"]
        owner_id, reviewer_id = (member["member_id"] for member in others[:2])
        draft = terms(owner_id, reviewer_id)

        def register(operation, **fields):
            proposal = create(client, base, tokens[lead_id], goal, draft, **fields)
            proposal_id = proposal["proposal_id"]
            confirm_all(client, proposal_id, tokens)
            return ok(proposal_action(client, proposal_id, tokens[lead_id], operation))

        initial = register("assign")
        revised = register("update", task_id=initial["task_id"], base_revision=1)
        other = register("assign")
        service = client.app.state.runtime.git_approvals
        actor = client.portal.call(service.chat.auth.resolve, tokens[owner_id])

        def ref(receipt):
            return client.portal.call(service._task_target, actor, {"message_id": receipt["notification_message_id"]})["ref"]

        assert ref(initial) == ref(revised) == "refs/heads/macs/task-" + initial["task_id"]
        assert ref(other) == "refs/heads/macs/task-" + other["task_id"]


@pytest.mark.parametrize(
    "decision,readback,expected",
    [("approve", "actual", "succeeded"), ("reject", "actual", "rejected"),
     ("approve", "different", "pending_verification"), ("approve", "unknown", "pending_verification")],
)
def test_configured_github_approval_and_readback(tmp_path, github, decision, readback, expected):
    remote, calls, observations = github
    observations["after_push"] = readback
    model = GitModel()
    with TestClient(application(tmp_path, model)) as client:
        base, lead, _, tokens = setup_team(client)
        token = tokens[lead["member_id"]]
        send(client, base, OPERATOR, request_id="work", text="Commit and request push")
        pending = wait_until(lambda: next(iter(approvals(client, base)), None))
        assert pending["remote_repository"] == "example/delivery"
        assert calls == ["ls-remote"] and read_ref(remote) is None
        endpoint = f"/v1/approvals/{pending['approval_id']}/decision"
        payload = decision_payload(pending, decision, "decision-1")
        for field, bad in [("repository", "other"), ("ref", "refs/heads/other"), ("sha", "0" * 40)]:
            assert client.post(endpoint, headers=auth(OPERATOR), json={**payload, field: bad}).status_code in (403, 409)
        assert calls == ["ls-remote"] and read_ref(remote) is None
        response = client.post(endpoint, headers=auth(OPERATOR), json=payload)
        assert response.is_success
        assert client.post(endpoint, headers=auth(OPERATOR), json=payload).json() == response.json()
        wait_until(lambda: action(client, base, token, pending["action_id"])["status"] == expected)
        assert calls.count("push") == (decision == "approve")
        assert read_ref(remote) == (pending["sha"] if decision == "approve" else None)
    if expected == "pending_verification":
        observations["after_push"] = "actual"
        with TestClient(application(tmp_path, model)) as client:
            wait_until(lambda: action(client, base, token, pending["action_id"])["status"] == "succeeded")
            assert calls.count("push") == 1


@pytest.mark.parametrize("target", [
    {**TARGET, "repository": "https://example.test/owner/repo"},
    {**TARGET, "ref": "refs/heads/../escape"},
    {**TARGET, "alias": "../demo"},
    {**TARGET, "credential": "unsupported-field"},
])
def test_invalid_github_configuration_fails_startup(tmp_path, monkeypatch, target):
    monkeypatch.delenv("COLLABORATION_GIT_TARGETS_DIR", raising=False)
    monkeypatch.setenv("COLLABORATION_GITHUB_TARGET", json.dumps(target))
    with pytest.raises(ValueError, match="COLLABORATION_GITHUB_TARGET"):
        with TestClient(application(tmp_path, GitModel())):
            pass


def test_github_target_rejects_model_url_and_other_ref_and_extends_branch(tmp_path, github):
    remote, calls, _ = github
    with TestClient(application(tmp_path, GitModel())) as client:
        service = client.app.state.runtime.git_approvals
        assert service is not None, "standard factory must enable GitHub tools"
        executor = service.executor
        for repo, ref in [(URL, TARGET["ref"]), (TARGET["alias"], "refs/heads/other")]:
            with pytest.raises(GitActionError):
                executor.create_commit("session", repo, ref, {"fixture.txt": b"x"}, "fixture")
        assert calls == []
        first = executor.create_commit("session", "demo", TARGET["ref"], {"fixture.txt": b"first"}, "first")
        assert executor.push("session", "demo", TARGET["ref"], first) == "succeeded"
        assert executor.observe("demo", TARGET["ref"], first) == "succeeded"
        second = executor.create_commit("session", "demo", TARGET["ref"], {"followup.txt": b"second"}, "second")
        assert executor.push("session", "demo", TARGET["ref"], second) == "succeeded"
        assert executor.observe("demo", TARGET["ref"], second) == "succeeded"
        assert read_ref(remote) == second
        assert subprocess.run(["git", "--git-dir", str(remote), "rev-parse", second + "^"],
                              check=True, capture_output=True).stdout.strip().decode() == first
        assert subprocess.run(["git", "--git-dir", str(remote), "show", second + ":fixture.txt"],
                              check=True, capture_output=True).stdout == b"first"


@pytest.mark.parametrize(("failure", "expected"), [
    ("missing_cli", "failed"),
    ("unavailable_credentials", "failed"),
    ("authentication", "failed"),
    ("permission", "failed"),
    ("protected_branch", "failed"),
    ("non_fast_forward", "failed"),
    ("remote_failure", "pending_verification"),
    ("timeout_after_update", "pending_verification"),
])
def test_github_push_failure_classification(tmp_path, github, failure, expected):
    remote, calls, observations = github
    model = GitModel()
    with TestClient(application(tmp_path, model)) as client:
        base, lead, _, tokens = setup_team(client)
        token = tokens[lead["member_id"]]
        send(client, base, OPERATOR, request_id="work", text="Commit and request push")
        pending = wait_until(lambda: next(iter(approvals(client, base)), None))
        refspec = f"{pending['sha']}:{TARGET['ref']}".encode()
        git_results = {
            "unavailable_credentials": (b"", b"fatal: could not read Username for 'https://github.com': terminal prompts disabled"),
            "authentication": (b"", b"fatal: Authentication failed for 'https://github.com/example/delivery.git/'"),
            "permission": (b"", b"fatal: unable to access 'https://github.com/example/delivery.git/': The requested URL returned error: 403"),
            "protected_branch": (b"!\t" + refspec + b"\t[remote rejected] (protected branch hook declined)\n", b"remote fixture stderr"),
            "non_fast_forward": (b"!\t" + refspec + b"\t[rejected] (fetch first)\n", b"remote fixture stderr"),
            "remote_failure": (b"!\t" + refspec + b"\t[remote failure] (remote failed to report status)\n", b"remote fixture stderr"),
        }
        observations["gh_available"] = failure != "missing_cli"
        observations["push_result"] = git_results.get(failure, failure)
        if failure == "timeout_after_update":
            observations["after_push"] = "unknown"
        response = client.post(f"/v1/approvals/{pending['approval_id']}/decision", headers=auth(OPERATOR),
                               json=decision_payload(pending, "approve", "decision-1"))
        assert response.is_success

        def terminal_action():
            value = action(client, base, token, pending["action_id"])
            if value["status"] in {"failed", "pending_verification", "succeeded"}:
                return value

        final = wait_until(terminal_action)
        assert final["status"] == expected
        assert calls == (["ls-remote"]
                         + (["push"] if failure != "missing_cli" else [])
                         + (["ls-remote"] if expected == "pending_verification" else []))
        notified = wait_until(lambda: model.notifications)[0]["message"]["text"]
        assert json.loads(notified.removeprefix("Git push outcome: "))["status"] == expected
        assert "remote fixture stderr" not in notified
        assert read_ref(remote) == (pending["sha"] if failure == "timeout_after_update" else None)
    if failure == "timeout_after_update":
        observations["after_push"] = "actual"
        with TestClient(application(tmp_path, model)) as client:
            wait_until(lambda: action(client, base, token, pending["action_id"])["status"] == "succeeded")
        assert calls.count("push") == 1
