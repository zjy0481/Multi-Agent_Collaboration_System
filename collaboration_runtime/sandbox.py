"""Docker-backed role workspaces and immutable, recipient-scoped handoffs.

The Docker daemon and this state directory must be trusted and host-controlled.
The sandbox is a resource boundary, not a guarantee against container escapes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

READ_WORKSPACE_FILE = """\
import base64
import os
import stat
import sys

parts = sys.argv[1].split("/")
directory = os.open("/workspace", os.O_RDONLY | os.O_DIRECTORY)
try:
    for part in parts[:-1]:
        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        os.close(directory)
        directory = child
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(parts[-1], flags, dir_fd=directory), "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("not a regular file")
        maximum = int(sys.argv[2])
        data = source.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("file exceeds the publish limit")
    sys.stdout.write(base64.b64encode(data).decode("ascii"))
finally:
    os.close(directory)
"""


class SandboxError(RuntimeError):
    """The sandbox could not safely perform the requested operation."""


class DockerSandbox:
    ROLES = frozenset({"developer", "tester"})
    MAX_FILE = 1024 * 1024
    MAX_OUTPUT = 64 * 1024
    ID_RE = re.compile(r"^[a-f0-9]{32}$")
    WORKSPACE_RE = re.compile(r"ws-[a-f0-9]{32}")

    def __init__(self, state_dir: Path, image: str = "collaboration-sandbox:local", timeout: float = 20):
        if timeout <= 0:
            raise SandboxError("timeout must be positive")
        self.state_dir = Path(state_dir).resolve()
        self.image = image
        self.timeout = timeout
        self.prefix = "collab-" + hashlib.sha256(str(self.state_dir).encode()).hexdigest()[:20]
        try:
            (self.state_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SandboxError(f"cannot initialize state directory: {exc}") from exc

    @classmethod
    def _role(cls, role: str) -> str:
        if not isinstance(role, str) or (role not in cls.ROLES and not cls.WORKSPACE_RE.fullmatch(role)):
            raise SandboxError("invalid role or workspace ID")
        return role

    @staticmethod
    def _path(path: str) -> str:
        if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
            raise SandboxError("path must be a relative POSIX path")
        p = PurePosixPath(path)
        if p.is_absolute() or any(x in ("", ".", "..") for x in path.split("/")):
            raise SandboxError("path must stay inside the workspace")
        return str(p)

    def _volume(self, role: str) -> str:
        return f"{self.prefix}-{self._role(role)}"

    def _artifacts(self) -> list[dict[str, Any]]:
        result = []
        root = self.state_dir / "artifacts"
        try:
            for directory in sorted(root.iterdir()):
                if not directory.is_dir() or directory.is_symlink() or not self.ID_RE.fullmatch(directory.name):
                    continue
                metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
                if metadata.get("id") != directory.name:
                    raise SandboxError("artifact metadata mismatch")
                result.append(metadata)
        except (OSError, ValueError, TypeError) as exc:
            raise SandboxError(f"cannot read artifact metadata: {exc}") from exc
        return result

    def list_artifacts(self, role: str) -> list[dict]:
        self._role(role)
        return [dict(a) for a in self._artifacts() if a.get("recipient") == role]

    def _base(self, role: str, name: str, readonly_workspace: bool) -> list[str]:
        workspace = f"type=volume,source={self._volume(role)},target=/workspace"
        if readonly_workspace:
            workspace += ",readonly"
        args = ["docker", "run", "--pull=never", "--rm", "--name", name, "--network", "none",
                "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "64", "--memory", "256m", "--cpus", "1", "--read-only",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m", "--workdir", "/workspace",
                "--mount", workspace]
        if not readonly_workspace:
            for artifact in self.list_artifacts(role):
                artifact_id = artifact["id"]
                directory = (self.state_dir / "artifacts" / artifact_id / "content").resolve()
                # --mount requires an absolute daemon-visible path; Docker Desktop translates Windows paths.
                if "," in str(directory):
                    raise SandboxError("state directory path cannot contain a comma")
                args += ["--mount", f"type=bind,source={directory},target=/inputs/{artifact_id},readonly"]
        return args

    def _execute(
        self, role: str, command: list[str], *, readonly_workspace: bool = False,
        output_limit: int | None = None,
    ) -> dict:
        self._role(role)
        name = self.prefix + "-run-" + uuid.uuid4().hex
        args = self._base(role, name, readonly_workspace) + [self.image] + command
        limit = output_limit if output_limit is not None else self.MAX_OUTPUT
        out_buffer, err_buffer = bytearray(), bytearray()
        def drain(stream, buffer, cap):
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                if len(buffer) <= cap:
                    buffer.extend(chunk[:cap + 1 - len(buffer)])

        try:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       stdin=subprocess.DEVNULL)
            workers = [threading.Thread(target=drain, args=(process.stdout, out_buffer, limit), daemon=True),
                       threading.Thread(target=drain, args=(process.stderr, err_buffer, self.MAX_OUTPUT), daemon=True)]
            for worker in workers:
                worker.start()
            timed_out = False
            cleanup_error = None
            try:
                process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                # Remove the named container, not only the Docker CLI process.
                process.kill()
                process.wait()
                try:
                    removed = subprocess.run(
                        ["docker", "rm", "-f", name], capture_output=True, timeout=10, check=False,
                    )
                    if removed.returncode and b"no such container" not in removed.stderr.lower():
                        cleanup_error = "Docker did not confirm removal of the timed-out container"
                except (OSError, subprocess.TimeoutExpired):
                    cleanup_error = "Docker did not confirm removal of the timed-out container"
            for worker in workers:
                worker.join(timeout=2)
            out, err = bytes(out_buffer), bytes(err_buffer)
        except OSError as exc:
            raise SandboxError(f"unable to run Docker: {exc}") from exc
        decode = lambda b, n: b[:n].decode("utf-8", "replace") + ("\n[output truncated]" if len(b) > n else "")
        result = {"exit_code": process.returncode, "stdout": decode(out, limit),
                  "stderr": decode(err, self.MAX_OUTPUT), "timed_out": timed_out}
        if cleanup_error:
            result["cleanup_error"] = cleanup_error
        if not timed_out and process.returncode in (125, 126, 127):
            raise SandboxError(f"Docker or command startup failed (exit {process.returncode}): {result['stderr'][:2048]}")
        return result

    def run(self, role: str, command: str) -> dict:
        self._role(role)
        if not isinstance(command, str) or not command or "\x00" in command:
            raise SandboxError("command must be a nonempty string")
        return self._execute(role, ["/bin/sh", "-c", command])

    def prepare(self, workspace_id: str) -> None:
        """Mount a workspace and verify the container user can write to it.

        Docker reuses the named volume on subsequent calls. The temporary probe
        is removed before the container exits and is not a draft artifact.
        """
        if not isinstance(workspace_id, str) or not self.WORKSPACE_RE.fullmatch(workspace_id):
            raise SandboxError("invalid workspace ID")
        result = self._execute(workspace_id, [
            "python3", "-I", "-c",
            "import tempfile; f = tempfile.TemporaryFile(dir='/workspace'); f.write(b'probe'); f.close()",
        ])
        if result["timed_out"] or result["exit_code"] != 0:
            raise SandboxError("cannot prepare workspace: " + (result["stderr"][:2048] or "execution failed or timed out"))

    def publish(self, role: str, path: str, recipient: str) -> dict:
        self._role(role)
        self._role(recipient)
        relative = self._path(path)
        # Isolated Python ignores modules supplied by the workspace.
        result = self._execute(
            role, ["python3", "-I", "-c", READ_WORKSPACE_FILE, relative, str(self.MAX_FILE)],
            readonly_workspace=True, output_limit=((self.MAX_FILE + 2) // 3) * 4 + 8,
        )
        if result["timed_out"] or result["exit_code"] != 0:
            raise SandboxError(f"cannot publish file: {result['stderr'][:2048] or 'execution timed out'}")
        try:
            content = base64.b64decode(result["stdout"], validate=True)
        except ValueError as exc:
            raise SandboxError("invalid file transfer") from exc
        if len(content) > self.MAX_FILE:
            raise SandboxError("file too large")
        artifact_id = uuid.uuid4().hex
        artifacts_root = self.state_dir / "artifacts"
        metadata = {"id": artifact_id, "author": role, "recipient": recipient,
                    "path": f"/inputs/{artifact_id}/artifact", "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content)}
        try:
            with tempfile.TemporaryDirectory(dir=artifacts_root) as temp_dir:
                staged = Path(temp_dir) / "version"
                exposed = staged / "content"
                exposed.mkdir(parents=True)
                (exposed / "artifact").write_bytes(content)
                (staged / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
                staged.replace(artifacts_root / artifact_id)
        except OSError as exc:
            raise SandboxError(f"cannot store artifact: {exc}") from exc
        return dict(metadata)

    def cleanup(self, *workspace_ids: str) -> None:
        """Remove specified workspace volumes, or only demo volumes if omitted."""
        for workspace_id in workspace_ids:
            if not isinstance(workspace_id, str) or not self.WORKSPACE_RE.fullmatch(workspace_id):
                raise SandboxError("invalid workspace ID")
        for role in sorted(set(workspace_ids) if workspace_ids else self.ROLES):
            try:
                result = subprocess.run(["docker", "volume", "rm", self._volume(role)], capture_output=True,
                                        text=True, timeout=10, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SandboxError(f"cannot remove sandbox volume: {exc}") from exc
            if result.returncode and "no such volume" not in result.stderr.lower():
                raise SandboxError(f"cannot remove sandbox volume: {result.stderr[:2048]}")
