"""Host-managed immutable artifact snapshots with version-specific ACLs.

The root and the SQLite connection belong to the trusted host. Callers must
validate that team and session identifiers refer to authorized participants.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .atomic_move import move_artifact_directory


class ArtifactError(RuntimeError):
    """Invalid input, version conflict, or storage failure."""


class ArtifactNotFound(ArtifactError):
    """The requested version does not exist or is not visible to the reader."""


_HEX = re.compile(r"[a-f0-9]{32}\Z")


class ArtifactStore:
    def __init__(self, db: sqlite3.Connection, root: Path):
        self.db = db
        self.root = Path(root).resolve()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.db.execute("""CREATE TABLE IF NOT EXISTS artifact_versions (
                version_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL,
                team_id TEXT NOT NULL, author_session_id TEXT NOT NULL,
                source_path TEXT NOT NULL, version INTEGER NOT NULL,
                reader_session_ids TEXT NOT NULL, size INTEGER NOT NULL,
                sha256 TEXT NOT NULL, scope_id TEXT,
                UNIQUE (artifact_id, version)
            )""")
        except (OSError, sqlite3.Error) as exc:
            raise ArtifactError("could not initialize artifact storage") from exc

    @staticmethod
    def _text(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ArtifactError(f"invalid {name}")
        return value

    @staticmethod
    def _id(value: str, name: str) -> str:
        if not isinstance(value, str) or not _HEX.fullmatch(value):
            raise ArtifactError(f"invalid {name}")
        return value

    @staticmethod
    def _metadata(row: tuple) -> dict:
        keys = ("version_id", "artifact_id", "team_id", "author_session_id", "source_path",
                "version", "reader_session_ids", "size", "sha256", "scope_id")
        result = dict(zip(keys, row))
        result["reader_session_ids"] = json.loads(result["reader_session_ids"])
        return result

    def publish(self, *, team_id: str, author_session_id: str, source_path: str,
                content: bytes, reader_session_ids: list[str], artifact_id: str | None = None,
                scope_id: str | None = None) -> dict:
        team_id = self._text(team_id, "team_id")
        author_session_id = self._text(author_session_id, "author_session_id")
        source_path = self._text(source_path, "source_path")
        if scope_id is not None:
            self._text(scope_id, "scope_id")
        if not isinstance(content, bytes):
            raise ArtifactError("content must be bytes")
        if len(content) > 1024 * 1024:
            raise ArtifactError("artifact exceeds the 1 MiB publication limit")
        if not isinstance(reader_session_ids, list):
            raise ArtifactError("reader_session_ids must be a list")
        readers = list(dict.fromkeys(self._text(item, "reader_session_id") for item in reader_session_ids))
        is_revision = artifact_id is not None
        artifact_id = self._id(artifact_id, "artifact_id") if is_revision else uuid.uuid4().hex
        version_id = uuid.uuid4().hex
        stage = self.root / (".staging-" + version_id)
        final = self.root / version_id
        digest = hashlib.sha256(content).hexdigest()
        created_stage = created_final = False
        savepoint = "artifact_" + version_id
        try:
            stage.mkdir()
            created_stage = True
            with (stage / "artifact").open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if final.exists():
                raise ArtifactError("version storage already exists")
            move_artifact_directory(stage, final)
            created_stage, created_final = False, True
            self.db.execute(f"SAVEPOINT {savepoint}")
            existing = self.db.execute(
                "SELECT MAX(version) FROM artifact_versions "
                "WHERE artifact_id = ? AND team_id = ? AND author_session_id = ?",
                (artifact_id, team_id, author_session_id),
            ).fetchone()[0]
            if is_revision and existing is None:
                raise ArtifactNotFound("artifact not found")
            version = (existing or 0) + 1
            self.db.execute(
                "INSERT INTO artifact_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (version_id, artifact_id, team_id, author_session_id, source_path, version,
                 json.dumps(readers, ensure_ascii=False), len(content), digest, scope_id),
            )
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
        except (OSError, sqlite3.Error, ArtifactError) as exc:
            try:
                self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            except sqlite3.Error:
                pass
            cleanup = final if created_final else stage if created_stage else None
            if cleanup is not None:
                try:
                    (cleanup / "artifact").unlink(missing_ok=True)
                    cleanup.rmdir()
                except OSError:
                    pass
            if isinstance(exc, ArtifactError):
                raise
            raise ArtifactError("could not publish artifact") from None
        return {"version_id": version_id, "artifact_id": artifact_id, "team_id": team_id,
                "author_session_id": author_session_id, "source_path": source_path,
                "version": version, "reader_session_ids": readers, "size": len(content),
                "sha256": digest, "scope_id": scope_id}

    def _resolve_row(self, team_id: str, reader_session_id: str, row) -> tuple[dict, Path]:
        if row is None:
            raise ArtifactNotFound("artifact version not found")
        try:
            metadata = self._metadata(row)
        except (TypeError, ValueError) as exc:
            raise ArtifactError("invalid artifact metadata") from exc
        if metadata["team_id"] != team_id or (
            reader_session_id != metadata["author_session_id"]
            and reader_session_id not in metadata["reader_session_ids"]
        ):
            raise ArtifactNotFound("artifact version not found")
        path = self.root / metadata["version_id"] / "artifact"
        try:
            if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
                raise ArtifactError("artifact file missing or invalid")
            data = path.read_bytes()
        except OSError as exc:
            raise ArtifactError("could not read artifact") from exc
        if len(data) != metadata["size"] or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise ArtifactError("artifact integrity check failed")
        return metadata, path

    def resolve(self, team_id: str, reader_session_id: str, version_id: str) -> tuple[dict, Path]:
        if not isinstance(version_id, str) or not _HEX.fullmatch(version_id):
            raise ArtifactNotFound("artifact version not found")
        try:
            row = self.db.execute("SELECT * FROM artifact_versions WHERE version_id = ?", (version_id,)).fetchone()
            return self._resolve_row(team_id, reader_session_id, row)
        except sqlite3.Error as exc:
            raise ArtifactError("could not query artifact") from exc

    def reference(self, team_id: str, version_id: str) -> dict:
        """Read immutable version metadata inside a business transaction without opening the file."""
        if not isinstance(version_id, str) or not _HEX.fullmatch(version_id):
            raise ArtifactNotFound("artifact version not found")
        row = self.db.execute(
            "SELECT * FROM artifact_versions WHERE team_id = ? AND version_id = ?", (team_id, version_id),
        ).fetchone()
        if row is None:
            raise ArtifactNotFound("artifact version not found")
        return self._metadata(row)

    def reference_readers(self, team_id: str, version_id: str) -> set[str]:
        metadata = self.reference(team_id, version_id)
        return {metadata["author_session_id"], *metadata["reader_session_ids"]}

    def resolve_number(self, team_id: str, reader_session_id: str, artifact_id: str, version: int) -> tuple[dict, Path]:
        if not isinstance(artifact_id, str) or not _HEX.fullmatch(artifact_id) or type(version) is not int or version < 1:
            raise ArtifactNotFound("artifact version not found")
        try:
            row = self.db.execute("SELECT * FROM artifact_versions WHERE artifact_id = ? AND version = ?", (artifact_id, version)).fetchone()
            return self._resolve_row(team_id, reader_session_id, row)
        except sqlite3.Error as exc:
            raise ArtifactError("could not query artifact") from exc

    def list_for(self, team_id: str, reader_session_id: str) -> list[dict]:
        try:
            rows = self.db.execute("SELECT * FROM artifact_versions WHERE team_id = ? ORDER BY rowid", (team_id,)).fetchall()
            result = []
            for row in rows:
                data = self._metadata(row)
                if reader_session_id == data["author_session_id"] or reader_session_id in data["reader_session_ids"]:
                    result.append(data)
            return result
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise ArtifactError("could not list artifacts") from exc
