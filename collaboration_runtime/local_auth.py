"""Credentials for the loopback workbench and its HTTP clients."""

from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import secrets

from .teams import TeamNotFound


OPERATOR_ID = "local-operator"
SESSION_COOKIE = "collaboration_session"


@dataclass(frozen=True)
class Principal:
    id: str
    team_id: str | None = None
    session_id: str | None = None

    @property
    def is_operator(self):
        return self.id == OPERATOR_ID


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def operator_credential(state: Path) -> str:
    path = state / "operator-token"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(token + "\n")
    return token


class LocalAuth:
    def __init__(self, db, teams, operator_token: str):
        if not operator_token or any(char.isspace() for char in operator_token):
            raise ValueError("The local operator credential must be nonempty and contain no whitespace")
        self.db = db
        self.teams = teams
        self._operator_digest = _digest(operator_token)
        self.cursor_key = hmac.digest(operator_token.encode("utf-8"), b"team-event-cursor", "sha256")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS member_credentials (
                session_id TEXT PRIMARY KEY REFERENCES members(session_id),
                token_digest TEXT NOT NULL UNIQUE
            )
        """)

    def resolve(self, token: str | None) -> Principal | None:
        if not token or len(token) > 512:
            return None
        digest = _digest(token)
        if hmac.compare_digest(self._operator_digest, digest):
            return Principal(OPERATOR_ID)
        row = self.db.execute(
            "SELECT session_id FROM member_credentials WHERE token_digest = ?", (digest,),
        ).fetchone()
        return self.member(row["session_id"]) if row else None

    def member(self, session_id: str) -> Principal | None:
        member = self.teams.session(session_id)
        if member is None:
            return None
        return Principal(member["member_id"], member["team_id"], member["session_id"])

    def issue(self, team_id: str, member_id: str) -> str:
        member = next((item for item in self.teams.get(team_id)["members"] if item["member_id"] == member_id), None)
        if member is None:
            raise TeamNotFound("Team member not found")
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("""
                INSERT INTO member_credentials(session_id, token_digest) VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET token_digest = excluded.token_digest
            """, (member["session_id"], _digest(token)))
        return token
