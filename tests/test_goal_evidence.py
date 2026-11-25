"""Checked handoffs across author, reviewer, downstream owner and goal Lead."""
from fastapi.testclient import TestClient

from test_collaboration import app, auth
from test_delegation import context, key, ok, terms
from test_goal_lifecycle import acceptance, lifecycle, terminate
from test_task_results import publish, register, review, decision, submit, submission


def test_freeze_from_preview_and_reject_changed_approval(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        owner, qa = others[:2]
        lid, oid, qid = (x["member_id"] for x in (lead, owner, qa))
        task = register(client, base, lid, tokens, goal, terms(oid, qid))
        ep = f"{base}/tasks/{task}"
        ok(acceptance(client, base, tokens[lid], goal, 1, [("Build", task)]))
        version = publish(client, base, owner, [lead, qa], b"accepted")

        def approve(revision):
            sent = ok(submit(client, ep, tokens[oid], submission(version, revision)))
            ok(review(client, ep, tokens[qid], sent["submission_id"], decision(revision=revision)))

        approve(1)
        preview = lifecycle(client, base, tokens[lid], goal).get("close_preview")
        assert preview and preview["final_version_ids"] == [version["version_id"]]
        other = publish(client, base, owner, [lead, qa], b"later", version["artifact_id"])
        wrong = terminate(client, base, tokens[lid], goal, 2, "completed", final_version_ids=[other["version_id"]])
        assert wrong.status_code == 409 and version["version_id"] in wrong.text
        register(client, base, lid, tokens, goal, terms(oid, qid), task_id=task, base_revision=1)
        approve(2)
        stale = terminate(client, base, tokens[lid], goal, 2, "completed", confirmation=preview["confirmation"])
        assert stale.status_code == 409
        import json
        stale_tool = client.portal.call(client.app.state.collaboration.execute_tool, lead['session_id'], 'close_goal',
                                        json.dumps({'goal_id': goal, 'request_id': key(), 'expected_revision': 2,
                                                    'outcome': 'completed', 'confirmation': preview['confirmation']}))
        assert stale_tool['status'] == 409 and 'fresh close preview' in stale_tool['error']
        current = lifecycle(client, base, tokens[lid], goal)["close_preview"]
        receipt = ok(terminate(client, base, tokens[lid], goal, 2, "completed", confirmation=current["confirmation"]))
        assert receipt["frozen_summary"]["accepted_deliveries"] == 1


def test_review_compares_declared_evidence_with_approved_edition(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        qa, release = others[:2]
        lid, qid, rid = (x["member_id"] for x in (lead, qa, release))
        up = register(client, base, lid, tokens, goal, terms(qid, lid, output_grants={rid: ["result.txt"]}))
        report = publish(client, base, qa, [lead, release], b"report")
        newer = publish(client, base, qa, [lead, release], b"newer", report["artifact_id"])
        sent = ok(submit(client, f"{base}/tasks/{up}", tokens[qid], submission(report)))
        ok(review(client, f"{base}/tasks/{up}", tokens[lid], sent["submission_id"], decision()))
        down = register(client, base, lid, tokens, goal, terms(rid, lid))
        ep = f"{base}/tasks/{down}"
        notes = publish(client, base, release, [lead], b"release")
        wrong = {**submission(notes), "evidence_versions": {up: [newer["version_id"]]}}
        receipt = ok(submit(client, ep, tokens[rid], wrong))
        failed = review(client, ep, tokens[lid], receipt["submission_id"], decision())
        assert failed.status_code == 409 and report["version_id"] in failed.text and newer["version_id"] in failed.text
        ok(review(client, ep, tokens[lid], receipt["submission_id"], decision("returned")))
        revised = publish(client, base, release, [lead], b"corrected", notes["artifact_id"])
        sent = ok(submit(client, ep, tokens[rid], {**submission(revised), "evidence_versions": {up: [report["version_id"]]}}))
        ok(review(client, ep, tokens[lid], sent["submission_id"], decision()))
        ok(acceptance(client, base, tokens[lid], goal, 1, [(f"Compare with historical {newer['version_id']}", up)]))
        body = {"request_id": key(), "expected_revision": 2, "required_deliveries": [
            {"name": "QA report", "task_id": up, "expected_version_ids": [newer["version_id"]]}]}
        mismatched = client.post(f"{base}/goals/{goal}/acceptance", headers=auth(tokens[lid]), json=body)
        assert mismatched.status_code == 409 and report["version_id"] in mismatched.text
        required = body["required_deliveries"]
        required[0]["expected_version_ids"] = [report["version_id"]]
        required.append({"name": "Release", "task_id": down, "expected_version_ids": [revised["version_id"]]})
        ok(client.post(f"{base}/goals/{goal}/acceptance", headers=auth(tokens[lid]), json=body))
        current = lifecycle(client, base, tokens[lid], goal)["close_preview"]
        assert set(current["final_version_ids"]) == {report["version_id"], revised["version_id"]}
        ok(terminate(client, base, tokens[lid], goal, 3, "completed", confirmation=current["confirmation"]))
        frozen = lifecycle(client, base, tokens[lid], goal)["terminal"]["frozen"]
        assert frozen["acceptance_required_deliveries"] == required
        assert frozen["task_acceptance"][down]["evidence_versions"] == {up: [report["version_id"]]}
        assert newer["version_id"] not in str(frozen)


def test_restricted_submission_reports_action_without_leaking_reference(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        owner, qa = others[:2]
        lead_token = tokens[lead["member_id"]]
        task = register(client, base, lead["member_id"], tokens, goal, terms(owner["member_id"], qa["member_id"]))
        ok(acceptance(client, base, lead_token, goal, 1, [("Build", task)]))
        private = publish(client, base, owner, [qa], b"private attachment")
        endpoint = f"{base}/tasks/{task}"
        sent = ok(submit(client, endpoint, tokens[owner["member_id"]], submission(private)))
        ok(review(client, endpoint, tokens[qa["member_id"]], sent["submission_id"], decision()))
        masked = ok(client.get(endpoint, headers=auth(lead_token)))
        assert masked["result"].get("access") == "publisher_action_required"
        assert private["version_id"] not in str(masked) and sent["submission_id"] not in str(masked)
        preview = lifecycle(client, base, lead_token, goal)["close_preview"]
        assert preview["blockers"] and not preview["confirmation"]
        assert private["version_id"] not in str(preview)
        assert sent["handoff"] == "lead_access_required"


def test_close_tool_explains_completion_argument_choices(tmp_path):
    import json
    with TestClient(app(tmp_path)) as client:
        base, lead, _, tokens, goal = context(client)
        service = client.app.state.collaboration
        def call(**extra):
            args = {'goal_id': goal, 'request_id': key(), 'expected_revision': 1,
                    'outcome': 'completed', **extra}
            return client.portal.call(service.execute_tool, lead['session_id'], 'close_goal', json.dumps(args))
        secret_confirmation = 'a' * 64
        secret_version = 'b' * 32
        both = call(confirmation=secret_confirmation, final_version_ids=[secret_version])
        assert both['status'] == 422 and 'mutually exclusive' in both['error']
        assert secret_confirmation not in str(both) and secret_version not in str(both)
        missing = call()
        assert missing['status'] == 422 and 'get_goal.close_preview.confirmation' in missing['error']
        assert 'final_version_ids' in missing['error']
        unrelated = call(expected_revision='invalid')
        assert unrelated['error'] == 'Invalid goal tool arguments'
        assert 'invalid' not in str(unrelated)
        schema = next(t for t in client.portal.call(service.goal_lifecycle.tools_for, lead['session_id'])
                      if t['function']['name'] == 'close_goal')
        assert 'never both' in schema['function']['description']
        assert lifecycle(client, base, tokens[lead['member_id']], goal)['goal']['status'] == 'active'
