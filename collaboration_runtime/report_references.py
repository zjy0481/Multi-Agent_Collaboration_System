"""Permission-filtered references to durable evidence for evaluation reports."""

import json

from .collaboration import READABLE_MESSAGE, _message


def project_report_references(collaboration, team, actor, assignment, goal, terminal, task_ids, human_waits):
    db = collaboration.db
    team_id = team["team_id"]
    goal_id = goal["id"] if goal is not None else None
    dialogue = [_message(row) for row in db.execute(f"""
        WITH RECURSIVE thread(id) AS (
            SELECT :assignment
            UNION SELECT value FROM json_each(:questions)
            UNION SELECT m.id FROM team_messages m JOIN thread t ON m.reply_to = t.id
                  WHERE m.team_id = :team
        )
        SELECT m.* FROM thread t JOIN team_messages m USING(id)
        WHERE m.team_id = :team AND {READABLE_MESSAGE} ORDER BY m.seq
    """, {"assignment": assignment["id"], "actor": actor.id, "team": team_id,
          "questions": json.dumps([wait["question_id"] for wait in human_waits])})]

    versions, reviews = [], []
    for task_id in task_ids:
        task = collaboration.delegation.get_task(team_id, actor, task_id)
        for submission in task["result"]["submissions"]:
            basis = {"task_id": task_id, "submission_id": submission["id"],
                     "task_revision": submission["task_revision"]}
            versions.extend({**basis, **version} for version in submission["versions"])
            if review := submission["review"]:
                reviews.append({**basis, "review_id": review["id"], "decision": review["decision"]})
    if terminal is not None and terminal["outcome"] == "completed":
        versions.extend(
            {"final": True, **version} for version in terminal["frozen"].get("final_versions", [])
            if collaboration.goal_lifecycle._can_read_final_version(team_id, actor, version["version_id"])
        )

    model_calls = []
    for row in db.execute("""
        SELECT e.seq, e.turn_id, e.payload, d.message_id, s.session_id FROM events e
        JOIN scheduled_turns s ON s.turn_id = e.turn_id
        JOIN message_deliveries d USING(event_id, session_id, purpose)
        JOIN json_each(?) visible ON visible.value = d.message_id
        WHERE e.type = 'model.started' AND (? OR s.session_id = ?) ORDER BY e.seq
    """, (json.dumps([message["id"] for message in dialogue]), actor.is_operator, actor.session_id)):
        payload = json.loads(row["payload"])
        reference = {"message_id": row["message_id"],
                     **{key: payload.get(key) for key in ("mode", "endpoint", "model")}}
        if row["session_id"] == actor.session_id:
            reference.update(turn_id=row["turn_id"], event_seq=row["seq"])
        model_calls.append(reference)

    uses = []
    if goal_id is not None and actor.session_id is not None:
        uses = [dict(row) for row in db.execute("""
            SELECT id AS use_id, retrieval_id, created_at FROM experience_uses
            WHERE team_id = ? AND target_goal_id = ? AND actor_session_id = ? ORDER BY rowid
        """, (team_id, goal_id, actor.session_id))]
    return {"dialogue": dialogue, "versions": versions, "reviews": reviews,
            "model_calls": model_calls, "experience_uses": uses}
