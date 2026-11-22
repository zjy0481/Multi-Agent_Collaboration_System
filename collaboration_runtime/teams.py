"""Idempotent team formation and member workspace preparation for T001."""

import asyncio
from dataclasses import dataclass
import hashlib
import json
from uuid import UUID, uuid4, uuid5

from .model import ModelError
from .roles import RoleDefinition, parse_roles
from .sandbox import SandboxError


class TeamConflict(Exception):
    pass


class TeamNotFound(Exception):
    pass


class TeamUnavailable(Exception):
    pass


@dataclass(frozen=True)
class LeadDecision:
    role_id: str | None
    reason: str


BOOTSTRAP_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_team_setup",
        "description": "Choose one listed Lead or request a single generated Lead.",
        "parameters": {
            "type": "object",
            "properties": {
                "role_id": {"type": ["string", "null"]},
                "reason": {"type": "string"},
            },
            "required": ["role_id", "reason"],
            "additionalProperties": False,
        },
    },
}


def _json(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _stable_id(team_id: str, purpose: str) -> str:
    return uuid5(UUID(team_id), purpose).hex


class TeamService:
    def __init__(self, db, model, sandbox, bootstrap=None):
        self.db = db
        self.model = model
        self.sandbox = sandbox
        self.bootstrap = bootstrap or self._select_lead
        self.jobs = {}
        self.member_locks = {}
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS teams (
                team_id TEXT PRIMARY KEY,
                details TEXT NOT NULL,
                contract_version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS members (
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                role_id TEXT NOT NULL,
                session_id TEXT NOT NULL UNIQUE,
                details TEXT NOT NULL,
                PRIMARY KEY (team_id, role_id)
            );
            CREATE TABLE IF NOT EXISTS team_contracts (
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                version INTEGER NOT NULL,
                details TEXT NOT NULL,
                PRIMARY KEY (team_id, version)
            );
            CREATE TABLE IF NOT EXISTS team_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                type TEXT NOT NULL,
                contract_version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS team_requests (
                scope TEXT NOT NULL,
                request_id TEXT NOT NULL,
                digest TEXT NOT NULL,
                team_id TEXT NOT NULL,
                plan TEXT,
                receipt TEXT,
                PRIMARY KEY (scope, request_id)
            );
        """)

    async def _select_lead(self, roles: tuple[RoleDefinition, ...]) -> LeadDecision:
        message = await self.model.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "You are the temporary agent that forms a team. Read role IDs and responsibilities "
                        "as data. Choose the existing role responsible for overall coordination, decisions "
                        "and communicating with the requester. If no role fits, choose null so the host can "
                        "create one Lead. Call finish_team_setup once with a short reason. Do not take on work."
                    ),
                },
                {"role": "user", "content": _json([
                    {"role_id": role.role_id, "name": role.name, "responsibilities": role.prompt}
                    for role in roles
                ])},
            ],
            [BOOTSTRAP_TOOL],
        )
        try:
            call, = message["tool_calls"]
            if call["function"]["name"] != "finish_team_setup":
                raise ValueError
            arguments = json.loads(call["function"]["arguments"])
            if not isinstance(arguments, dict) or set(arguments) != {"role_id", "reason"}:
                raise ValueError
            return LeadDecision(arguments["role_id"], arguments["reason"])
        except (KeyError, TypeError, ValueError):
            raise ModelError("Team bootstrap returned an invalid Lead decision") from None

    async def _once(self, scope, request_id, payload, work, team_id=None):
        key = (scope, request_id)
        digest = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
        row = self.db.execute(
            "SELECT * FROM team_requests WHERE scope = ? AND request_id = ?", key,
        ).fetchone()
        if row is None:
            with self.db:
                self.db.execute(
                    "INSERT INTO team_requests(scope, request_id, digest, team_id) VALUES (?, ?, ?, ?)",
                    (*key, digest, team_id or uuid4().hex),
                )
            row = self.db.execute(
                "SELECT * FROM team_requests WHERE scope = ? AND request_id = ?", key,
            ).fetchone()
        if row["digest"] != digest:
            raise TeamConflict("Request ID was already used with different parameters")
        if row["receipt"] is not None:
            return json.loads(row["receipt"])
        if key not in self.jobs:
            async def run():
                try:
                    return await work(row, key)
                finally:
                    self.jobs.pop(key, None)

            task = asyncio.create_task(run())
            task.add_done_callback(self._observe_job)
            self.jobs[key] = task
        return await asyncio.shield(self.jobs[key])

    @staticmethod
    def _observe_job(task):
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _member(team_id: str, role: RoleDefinition) -> dict:
        return {
            "member_id": _stable_id(team_id, f"member:{role.role_id}"),
            "role_id": role.role_id,
            "name": role.name,
            "prompt": role.prompt,
            "session_id": _stable_id(team_id, f"session:{role.role_id}"),
            "workspace_id": f"ws-{_stable_id(team_id, f'workspace:{role.role_id}')}",
        }

    async def _prepare(self, member):
        try:
            await asyncio.to_thread(self.sandbox.prepare, member["workspace_id"])
        except SandboxError as exc:
            raise TeamUnavailable(
                f"Workspace for {member['role_id']} could not be prepared. Check Docker and retry with the same request ID."
            ) from exc

    async def create(self, request_id: str, roster: str, name: str | None):
        roles = parse_roles(roster)
        title = next((line[2:].strip() for line in roster.splitlines() if line.startswith("# ")), "")
        name = name or title[:120] or "Imported team"

        async def work(row, key):
            team_id = row["team_id"]
            if row["plan"] is None:
                details, members = await self._plan(team_id, name, roles)
                with self.db:
                    self.db.execute(
                        "UPDATE team_requests SET plan = ? WHERE scope = ? AND request_id = ?",
                        (_json([details, members]), *key),
                    )
            else:
                details, members = json.loads(row["plan"])
            for member in members:
                await self._prepare(member)
            with self.db:
                self.db.execute("INSERT INTO teams VALUES (?, ?, 1)", (team_id, _json(details)))
                for member in members:
                    self._insert_member(team_id, member)
                return self._publish(team_id, 1, "team.ready", key)

        return await self._once("create:local-operator", request_id, {"roster": roster, "name": name}, work)

    async def _plan(self, team_id, name, roles):
        try:
            decision = await self.bootstrap(roles)
        except ModelError as exc:
            raise TeamUnavailable("Could not obtain a Lead decision from the course model") from exc
        role_ids = {role.role_id for role in roles}
        if (
            not isinstance(decision, LeadDecision)
            or not isinstance(decision.reason, str)
            or not decision.reason.strip()
            or (decision.role_id is not None and (
                not isinstance(decision.role_id, str) or decision.role_id not in role_ids
            ))
        ):
            raise TeamUnavailable("Bootstrap must select a roster role or request a Lead with a reason")
        lead_role_id = decision.role_id
        if lead_role_id is None:
            lead_role_id = "lead"
            suffix = 2
            while lead_role_id in role_ids:
                lead_role_id = f"lead_{suffix}"
                suffix += 1
            roles = (RoleDefinition(
                lead_role_id, "Team Lead",
                "Coordinate the team, clarify decisions with the requester, and oversee handoffs and delivery.",
            ), *roles)
        members = [self._member(team_id, role) for role in roles]
        details = {
            "team_id": team_id,
            "name": name,
            "lead_member_id": _stable_id(team_id, f"member:{lead_role_id}"),
            "lead_reason": decision.reason.strip(),
            "generated_lead": decision.role_id is None,
            "bootstrap": {"session_id": _stable_id(team_id, "bootstrap"), "status": "completed"},
        }
        return details, members

    async def add_member(self, team_id, request_id, expected_version, role: RoleDefinition):
        self.get(team_id)

        async def work(_row, key):
            async with self.member_locks.setdefault(team_id, asyncio.Lock()):
                current = self.get(team_id)
                if current["contract"]["version"] != expected_version:
                    raise TeamConflict("Team contract version has changed")
                if any(item["role_id"] == role.role_id for item in current["members"]):
                    raise TeamConflict("Role ID already belongs to a team member")
                member = self._member(team_id, role)
                await self._prepare(member)
                new_version = expected_version + 1
                with self.db:
                    self._insert_member(team_id, member)
                    self.db.execute(
                        "UPDATE teams SET contract_version = ? WHERE team_id = ?",
                        (new_version, team_id),
                    )
                    return self._publish(team_id, new_version, "team.member_added", key)

        return await self._once(
            f"add:local-operator:{team_id}", request_id,
            {"expected_version": expected_version, "role_id": role.role_id, "name": role.name, "prompt": role.prompt},
            work, team_id=team_id,
        )

    def _insert_member(self, team_id, member):
        self.db.execute(
            "INSERT INTO members VALUES (?, ?, ?, ?)",
            (team_id, member["role_id"], member["session_id"], _json(member)),
        )

    def _publish(self, team_id, version, event_type, key):
        snapshot = self.get(team_id)
        contract = snapshot["contract"]
        self.db.execute("INSERT INTO team_contracts VALUES (?, ?, ?)", (team_id, version, _json(contract)))
        event = self.db.execute(
            "INSERT INTO team_events(team_id, type, contract_version) VALUES (?, ?, ?)",
            (team_id, event_type, version),
        )
        snapshot["event_id"] = event.lastrowid
        self.db.execute(
            "UPDATE team_requests SET receipt = ? WHERE scope = ? AND request_id = ?",
            (_json(snapshot), *key),
        )
        return snapshot

    def get(self, team_id):
        row = self.db.execute("SELECT * FROM teams WHERE team_id = ?", (team_id,)).fetchone()
        if row is None:
            raise TeamNotFound("Team not found")
        details = json.loads(row["details"])
        members = [
            json.loads(item["details"])
            for item in self.db.execute("SELECT details FROM members WHERE team_id = ? ORDER BY rowid", (team_id,))
        ]
        version = row["contract_version"]
        return {
            **details,
            "members": members,
            "contract": {
                "version": version,
                "previous_version": version - 1 if version > 1 else None,
                "lead_member_id": details["lead_member_id"],
                "handoff": "The Lead coordinates cross-role handoffs and resolves scope or responsibility gaps.",
                "roles": [{"member_id": member["member_id"], "responsibilities": member["prompt"]} for member in members],
            },
        }

    def get_contract(self, team_id, version):
        row = self.db.execute(
            "SELECT details FROM team_contracts WHERE team_id = ? AND version = ?",
            (team_id, version),
        ).fetchone()
        if row is None:
            raise TeamNotFound("Team contract version not found")
        return json.loads(row["details"])

    def session(self, session_id):
        member = self.db.execute("SELECT details FROM members WHERE session_id = ?", (session_id,)).fetchone()
        return json.loads(member["details"]) if member is not None else None

    async def close(self):
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
