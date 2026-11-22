"""Parse the course role-list Markdown without changing role prompts."""
from __future__ import annotations

from dataclasses import dataclass
import re

ROLE_ID_PATTERN = r"[a-z][a-z0-9_-]{0,63}"
_ID = re.compile(rf"{ROLE_ID_PATTERN}\Z")
_TABLE_HEADER = re.compile(r"^\s*\|\s*Agent ID\s*\|\s*Role\s*\|\s*$")
_SEPARATOR = re.compile(r"^\s*\|\s*:?-{3,}:?\s*\|\s*:?-{3,}:?\s*\|\s*$")
_ROW = re.compile(r"^\s*\|\s*`([^`]*)`\s*\|\s*([^|]*?)\s*\|\s*$")
_HEADING = re.compile(r"^##\s+(.+?)\s+\(([^()]*)\)\s*$")


@dataclass(frozen=True)
class RoleDefinition:
    role_id: str
    name: str
    prompt: str


class RoleFileError(ValueError):
    def __init__(self, line: int, message: str):
        self.line = line
        super().__init__(f"Line {line}: {message}")


def parse_roles(markdown: str) -> tuple[RoleDefinition, ...]:
    """Read one ID/name table and matching level-two fenced role prompts.

    Prompt is the exact text between fence lines, including its line endings.
    Lines in errors are one-based; missing content is reported at EOF + 1.
    """
    lines = markdown.splitlines(keepends=True)
    content = [line.rstrip("\r\n") for line in lines]
    eof = len(lines) + 1
    headers = [i for i, line in enumerate(content) if _TABLE_HEADER.fullmatch(line)]
    if not headers:
        raise RoleFileError(eof, "missing Agent ID / Role table")
    if len(headers) > 1:
        raise RoleFileError(headers[1] + 1, "duplicate role table")
    index = headers[0] + 1
    if index >= len(lines) or not _SEPARATOR.fullmatch(content[index]):
        raise RoleFileError(index + 1, "expected role table separator")
    index += 1
    entries: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    while index < len(lines) and content[index].lstrip().startswith("|"):
        match = _ROW.fullmatch(content[index])
        if not match:
            raise RoleFileError(index + 1, "invalid role table row")
        role_id, name = match.group(1), match.group(2).strip()
        if not _ID.fullmatch(role_id):
            raise RoleFileError(index + 1, f"invalid role ID: {role_id!r}")
        if not name:
            raise RoleFileError(index + 1, "role name is empty")
        if role_id in seen:
            raise RoleFileError(index + 1, f"duplicate role ID: {role_id}")
        entries.append((role_id, name, index + 1))
        seen.add(role_id)
        index += 1
    if not entries:
        raise RoleFileError(index + 1, "role table has no members")
    expected = {role_id: name for role_id, name, _ in entries}
    prompts: dict[str, str] = {}
    while index < len(lines):
        line = content[index]
        if not line.strip():
            index += 1
            continue
        match = _HEADING.fullmatch(line)
        if not match:
            raise RoleFileError(index + 1, "expected role heading '## Name (id)'")
        name, role_id = match.group(1).strip(), match.group(2)
        if not _ID.fullmatch(role_id):
            raise RoleFileError(index + 1, f"invalid role ID: {role_id!r}")
        if role_id not in expected:
            raise RoleFileError(index + 1, f"role ID {role_id!r} is not in the table")
        if role_id in prompts:
            raise RoleFileError(index + 1, f"duplicate role section: {role_id}")
        if name != expected[role_id]:
            raise RoleFileError(index + 1, f"name does not match table for {role_id}")
        index += 1
        while index < len(lines) and not content[index].strip():
            index += 1
        if index >= len(lines) or content[index].strip() != "```text":
            raise RoleFileError(index + 1, "expected opening ```text fence")
        index += 1
        start = index
        while index < len(lines) and content[index].strip() != "```":
            index += 1
        if index >= len(lines):
            raise RoleFileError(eof, "missing closing fence")
        prompt = "".join(lines[start:index])
        if not prompt.strip():
            raise RoleFileError(start + 1, f"empty prompt for {role_id}")
        prompts[role_id] = prompt
        index += 1
    for role_id, _, _ in entries:
        if role_id not in prompts:
            raise RoleFileError(eof, f"missing role section: {role_id}")
    return tuple(RoleDefinition(role_id, name, prompts[role_id]) for role_id, name, _ in entries)
