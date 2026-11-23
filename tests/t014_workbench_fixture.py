"""Serve isolated task-review data for the T014 browser check."""

from argparse import ArgumentParser
import json
from pathlib import Path
import sys
from tempfile import mkdtemp

from fastapi.testclient import TestClient
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_collaboration import app
from test_delegation import context, ok, terms
from test_task_results import decision, publish, register, review, submission, submit


def prepare(state_dir):
    application = app(state_dir)
    with TestClient(application) as client:
        base, lead, others, tokens, goal = context(client)
        owner, reviewer, outsider = others
        task = register(client, base, lead["member_id"], tokens, goal, terms(
            owner["member_id"], reviewer["member_id"], deliverables=["T014 review fixture"],
        ))
        task_url = f"{base}/tasks/{task}"
        owner_token = tokens[owner["member_id"]]
        reviewer_token = tokens[reviewer["member_id"]]
        first = publish(client, base, owner, [lead, reviewer], b"T014 version one\n")
        submitted = ok(submit(client, task_url, owner_token, submission(first)))
        returned = ok(review(client, task_url, reviewer_token, submitted["submission_id"], decision("returned")))
        second = publish(client, base, owner, [lead, reviewer], b"T014 version two\n", first["artifact_id"])
        resubmitted = ok(submit(client, task_url, owner_token, submission(second)))
        passed = ok(review(client, task_url, reviewer_token, resubmitted["submission_id"], decision()))
        info = {
            "task_url": task_url,
            "reviewer_token": reviewer_token,
            "outsider_token": tokens[outsider["member_id"]],
            "returned_review_id": returned["review_id"],
            "approved_review_id": passed["review_id"],
            "final_version_id": second["version_id"],
            "final_download_url": f"/v1/artifacts/{second['artifact_id']}/versions/{second['version']}",
        }
    return application, info


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    temp_root = ROOT / ".tmp"
    temp_root.mkdir(exist_ok=True)
    temp_dir = Path(mkdtemp(prefix="t014-workbench-", dir=temp_root))
    application, info = prepare(temp_dir / "state")
    fixture = temp_dir / "fixture.json"
    fixture.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Fixture details: {fixture}", flush=True)
    uvicorn.run(application, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
