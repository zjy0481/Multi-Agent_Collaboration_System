"""Read-only projection of visible clarification waits for an assignment."""

from datetime import datetime

from .collaboration import READABLE_MESSAGE


def project_human_waits(collaboration, team, actor, assignment_id, frozen_at):
    """Return question intervals from persisted messages and scope closure facts."""
    # A threaded question follows its explicit assignment even when another goal
    # held the active slot when the question was asked.
    rows = collaboration.db.execute(f"""
        WITH RECURSIVE ancestry(question_id, message_id, reply_to, kind) AS (
            SELECT q.question_id, source.id, source.reply_to, source.kind
            FROM human_questions q JOIN team_messages source ON source.id = q.source_message_id
            WHERE source.team_id = :team
            UNION ALL
            SELECT a.question_id, parent.id, parent.reply_to, parent.kind
            FROM ancestry a JOIN team_messages parent ON parent.id = a.reply_to
            WHERE a.kind <> 'assignment'
        )
        SELECT q.question_id, m.created_at AS started_at,
               answer.created_at AS answered_at, q.closed_at
        FROM human_questions q
        JOIN team_messages m ON m.id = q.question_id
        LEFT JOIN team_messages answer ON answer.id = q.answer_message_id
        LEFT JOIN team_goals g ON g.id = q.goal_id AND g.team_id = m.team_id
        LEFT JOIN ancestry root ON root.question_id = q.question_id AND root.kind = 'assignment'
        WHERE m.team_id = :team AND {READABLE_MESSAGE}
          AND COALESCE(root.message_id, g.source_message_id) = :assignment
        ORDER BY m.seq
    """, {"team": team["team_id"], "actor": actor.id, "assignment": assignment_id})
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
