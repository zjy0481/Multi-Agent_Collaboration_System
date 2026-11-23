"""Recompute evaluation reports from durable assignment and acceptance facts."""

from datetime import datetime

from .local_auth import OPERATOR_ID
from .report_references import project_report_references
from .report_waits import project_human_waits
from .teams import TeamNotFound


OUTCOME_COUNTS = {
    "completed": "completed_goals",
    "failed": "failed_goals",
    "timed_out": "timed_out_goals",
    "cancelled": "cancelled_goals",
}


class EvaluationReports:
    def __init__(self, collaboration):
        self.collaboration = collaboration
        self.lifecycle = collaboration.goal_lifecycle
        self.delegation = collaboration.delegation
        self.db = collaboration.db

    def goal(self, actor, goal_id):
        with self.db:
            self.db.execute("BEGIN")
            goal = self.db.execute("SELECT * FROM team_goals WHERE id = ?", (goal_id,)).fetchone()
            if goal is None:
                raise TeamNotFound("Goal report not found")
            try:
                team = self.collaboration.team(goal["team_id"], actor)
                assignment = self.collaboration._message_for(goal["team_id"], actor, goal["source_message_id"])
            except TeamNotFound:
                raise TeamNotFound("Goal report not found") from None
            return self._report(team, actor, assignment, goal)

    def assignments(self, team_id, actor, assignment_ids):
        with self.db:
            self.db.execute("BEGIN")
            team = self.collaboration.team(team_id, actor)
            goals = []
            counts = dict.fromkeys((*OUTCOME_COUNTS.values(), "unfinished_goals"), 0)
            unique_ids = list(dict.fromkeys(assignment_ids))
            for assignment_id in unique_ids:
                try:
                    goal, assignment = self.lifecycle._goal_by_source(team_id, actor, assignment_id)
                except TeamNotFound:
                    raise TeamNotFound("Assignment not found") from None
                if assignment["kind"] != "assignment" or assignment["sender_id"] != OPERATOR_ID:
                    raise TeamNotFound("Assignment not found")
                report = self._report(team, actor, assignment, goal)
                goals.append(report)
                counts[OUTCOME_COUNTS.get(report["status"], "unfinished_goals")] += 1
            counts["assigned_goals"] = len(unique_ids)
            return {
                "team_id": team_id,
                "assignment_ids": unique_ids,
                "counts": counts,
                "goal_completion_rate": counts["completed_goals"] / len(unique_ids) if unique_ids else None,
                "goals": goals,
            }

    def _report(self, team, actor, assignment, goal):
        team_id = team["team_id"]
        assignment_id = assignment["id"]
        goal_id = goal["id"] if goal is not None else None
        terminal = self.lifecycle._terminal(source_message_id=assignment_id)
        if terminal is None:
            task_ids = [row["id"] for row in self.db.execute(
                "SELECT id FROM delegation_tasks WHERE team_id = ? AND goal_id = ? ORDER BY rowid",
                (team_id, goal_id),
            )]
            accepted_ids = {
                task_id for task_id in task_ids
                if self.delegation.results.current_acceptance_fact(team_id, goal_id, task_id) is not None
            }
        else:
            frozen = terminal["frozen"]
            task_ids = list(dict.fromkeys(frozen["effective_task_ids"]))
            accepted_ids = set(task_ids).intersection(frozen["accepted_task_ids"])

        visible_tasks = []
        for task_id in task_ids:
            try:
                self.delegation._visible_task(team_id, actor, task_id)
            except TeamNotFound:
                continue
            visible_tasks.append(task_id)
        visible_accepted = [task_id for task_id in visible_tasks if task_id in accepted_ids]
        report_all_tasks = self.lifecycle._is_full_reader(team, actor)
        registered_count = len(task_ids) if report_all_tasks else len(visible_tasks)
        accepted_count = len(accepted_ids) if report_all_tasks else len(visible_accepted)

        elapsed = ended_at = terminal_summary = metric_version = None
        if terminal is not None:
            ended_at = terminal["server_time"]
            elapsed = (datetime.fromisoformat(ended_at) - datetime.fromisoformat(assignment["created_at"])).total_seconds()
            status = terminal["outcome"]
            metric_version = terminal["frozen"]["metric_version"]
            terminal_summary = self.lifecycle._compact_terminal(terminal)
            terminal_summary.update({"event_id": terminal["event_id"], "actor_id": terminal["actor_id"]})
            if report_all_tasks:
                terminal_summary["evidence"] = terminal["evidence"]
        else:
            status = goal["status"] if goal is not None else "pending_registration"
        human_waits = project_human_waits(self.collaboration, team, actor, assignment_id, ended_at)
        references = project_report_references(
            self.collaboration, team, actor, assignment, goal, terminal, visible_tasks, human_waits,
        )
        return {
            "team_id": team_id,
            "assignment_id": assignment_id,
            "goal_id": goal_id,
            "status": status,
            "assigned_at": assignment["created_at"],
            "registered_at": goal["created_at"] if goal is not None else None,
            "terminal": terminal_summary,
            "metric_version": metric_version,
            "acceptance_revision": terminal["frozen"].get("acceptance_revision") if terminal is not None else None,
            "elapsed_seconds": elapsed,
            "human_waits": human_waits,
            "counts": {"registered_tasks": registered_count, "accepted_tasks": accepted_count},
            "counts_scope": "goal" if report_all_tasks else "visible_tasks",
            "task_ids": visible_tasks,
            "accepted_task_ids": visible_accepted,
            "subtask_completion_rate": accepted_count / registered_count if registered_count else None,
            "run_modes": sorted({call["mode"] for call in references["model_calls"] if call["mode"] is not None}),
            "references": references,
        }
