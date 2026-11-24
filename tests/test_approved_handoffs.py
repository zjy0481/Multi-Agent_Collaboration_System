"""Checked handoffs across author, reviewer, downstream owner and goal Lead."""
from fastapi.testclient import TestClient

from test_collaboration import app, auth
from test_delegation import context, ok, terms
from test_task_results import publish, register, review, decision, submit, submission


def test_private_upstream_can_grant_only_a_named_output(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        dev, qa, release = others[:3]
        lid, did, qid, rid = (x["member_id"] for x in (lead, dev, qa, release))
        upstream = register(client, base, lid, tokens, goal, terms(did, qid, output_grants={rid: ["result.txt"]}))
        downstream = register(client, base, lid, tokens, goal, terms(rid, lid, dependency_ids=[upstream]))
        up, down = (f"{base}/tasks/{i}" for i in (upstream, downstream))
        assert client.get(up, headers=auth(tokens[rid])).status_code == 404
        assert ok(client.get(down, headers=auth(tokens[rid])))["status"] == "waiting_dependencies"
        item = publish(client, base, dev, [qa, release], b"shared output")
        sent = ok(submit(client, up, tokens[did], submission(item)))
        ok(review(client, up, tokens[qid], sent["submission_id"], decision()))
        ready = ok(client.get(down, headers=auth(tokens[rid])))
        assert ready["status"] == "ready"
        assert ready["dependency_outputs"][upstream] == [item["version_id"]]
        assert client.get(up, headers=auth(tokens[rid])).status_code == 404
        blocked = register(client, base, lid, tokens, goal, terms(lid, qid, dependency_ids=[upstream]))
        assert ok(client.get(f"{base}/tasks/{blocked}", headers=auth(tokens[lid])))["status"] == "waiting_dependencies"
        register(client, base, lid, tokens, goal, terms(did, qid, output_grants={rid: ["result.txt"]}),
                 task_id=upstream, base_revision=1)
        sent = ok(submit(client, up, tokens[did], submission(item, 2)))
        ok(review(client, up, tokens[qid], sent["submission_id"], decision(revision=2)))
        assert len([d for d in ok(client.get(f"{base}/deliveries", headers=auth(tokens[rid]))) if d["purpose"] == "task.ready"]) == 1


def test_partially_shared_dependency_exposes_only_usable_outputs(tmp_path):
    from functools import partial

    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        dev, qa, release = others[:3]
        lid, did, qid, rid = (x["member_id"] for x in (lead, dev, qa, release))
        up = register(client, base, lid, tokens, goal, terms(did, qid, output_grants={rid: ["result.txt", "private-helper"]}))
        down = register(client, base, lid, tokens, goal, terms(rid, lid, dependency_ids=[up]))
        visible = publish(client, base, dev, [lead, qa, release], b"app")
        hidden = client.portal.call(partial(client.app.state.workspace.artifacts.publish,
            team_id=base.rsplit("/", 1)[-1], author_session_id=dev["session_id"], source_path="private-helper",
            content=b"private", reader_session_ids=[qa["session_id"]]))
        body = submission(visible)
        body["version_ids"].append(hidden["version_id"])
        sent = ok(submit(client, f"{base}/tasks/{up}", tokens[did], body))
        ok(review(client, f"{base}/tasks/{up}", tokens[qid], sent["submission_id"], decision()))
        view = ok(client.get(f"{base}/tasks/{down}", headers=auth(tokens[rid])))
        assert view["status"] == "waiting_dependencies"
        assert view["dependency_outputs"].get(up) == [visible["version_id"]]
        assert view["dependency_blockers"][up] == "publisher_action_required"
        assert hidden["version_id"] not in str(view) and hidden["artifact_id"] not in str(view)
        assert "private-helper" not in str(view)
        draft = publish(client, base, release, [lead], b"release draft")
        assert submit(client, f"{base}/tasks/{down}", tokens[rid], submission(draft)).status_code == 409
        assert all(d["purpose"] != "task.ready"
                   for d in ok(client.get(f"{base}/deliveries", headers=auth(tokens[rid]))))
        assert client.get(f"{base}/tasks/{up}", headers=auth(tokens[rid])).status_code == 404


def test_waiting_dependency_blocks_formal_submission(tmp_path):
    with TestClient(app(tmp_path)) as client:
        base, lead, others, tokens, goal = context(client)
        owner, qa = others[:2]
        lid, oid, qid = (x["member_id"] for x in (lead, owner, qa))
        up = register(client, base, lid, tokens, goal, terms(oid, qid))
        down = register(client, base, lid, tokens, goal, terms(qid, lid, dependency_ids=[up]))
        draft = publish(client, base, qa, [lead], b"preparation")
        request = submission(draft)
        early = submit(client, f"{base}/tasks/{down}", tokens[qid], request)
        assert early.status_code == 409 and "dependenc" in early.text.lower()
        source = publish(client, base, owner, [qa, lead], b"source")
        sent = ok(submit(client, f"{base}/tasks/{up}", tokens[oid], submission(source)))
        ok(review(client, f"{base}/tasks/{up}", tokens[qid], sent["submission_id"], decision()))
        ok(submit(client, f"{base}/tasks/{down}", tokens[qid], request))
