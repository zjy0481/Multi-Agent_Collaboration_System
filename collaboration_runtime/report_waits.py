"""Read-only projection of visible clarification waits for an assignment."""

from datetime import datetime

from .collaboration import QUESTION_ASSIGNMENTS, READABLE_MESSAGE


def project_human_waits(collaboration, team, actor, assignment_id, frozen_at):
    """Return question intervals from persisted messages and scope closure facts."""
    rows = collaboration.db.execute(f"""
        {QUESTION_ASSIGNMENTS}
        SELECT q.question_id, m.created_at AS started_at,
               answer.created_at AS answered_at, q.closed_at
        FROM question_assignments a JOIN human_questions q USING (question_id)
        JOIN team_messages m ON m.id = q.question_id
        LEFT JOIN team_messages answer ON answer.id = q.answer_message_id
        WHERE m.team_id = :team AND {READABLE_MESSAGE}
          AND a.assignment_id = :assignment
        ORDER BY m.seq
    """, {"team": team["team_id"], "actor": actor.id, "question_id": None, "assignment": assignment_id})
    cutoff = datetime.fromisoformat(frozen_at) if frozen_at else None
    waits = []
    for row in rows:
        start = datetime.fromisoformat(row["started_at"])
        if cutoff is not None and start > cutoff:
            continue
        ends = [(datetime.fromisoformat(value), value) for value in
                (row["answered_at"], row["closed_at"], frozen_at) if value is not None]
        end, ended_at = min(ends, key=lambda item: item[0]) if ends else (None, None)
        waits.append({"question_id": row["question_id"], "started_at": row["started_at"],
                      "ended_at": ended_at,
                      "duration_seconds": (end - start).total_seconds() if end is not None else None})
    return waits
