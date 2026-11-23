import sys

from collaboration_runtime.sandbox import DockerSandbox


def test_run_timings_are_opt_in_and_measure_cli_wall_time(tmp_path, monkeypatch):
    sandbox = DockerSandbox(tmp_path)
    # Substitute a local process for Docker; no daemon or additional process is needed.
    monkeypatch.setattr(sandbox, "_base", lambda *args: [sys.executable, "-c", "print('ok')"])
    plain = sandbox.run("developer", "ignored")
    measured = sandbox.run("developer", "ignored", measure_timings=True)
    assert "timings" not in plain
    assert measured["stdout"].strip() == "ok"
    assert 0 <= measured["timings"]["docker_cli_seconds"] <= measured["timings"]["wall_seconds"]

import os
import subprocess
import pytest
from collaboration_runtime.sandbox import MUTATE_WORKSPACE_FILE, SandboxError


@pytest.mark.parametrize(("mode", "exit_code", "message"), [
    ("create", 41, "workspace file already exists"),
    ("update", 42, "workspace file not found"),
    ("create", 42, "cannot write workspace file"),
    ("update", 41, "cannot write workspace file"),
    ("create", 1, "cannot write workspace file"),
])
def test_write_failure_reason_is_fixed_and_scoped(tmp_path, monkeypatch, mode, exit_code, message):
    sandbox = DockerSandbox(tmp_path)
    monkeypatch.setattr(sandbox, "_execute", lambda *a, **kw: {
        "exit_code": exit_code, "timed_out": False, "stderr": "secret host path"})
    with pytest.raises(SandboxError) as exc:
        sandbox.write_bytes("developer", "file", b"content", mode=mode)
    assert str(exc.value) == message


@pytest.mark.skipif(os.name != "posix", reason="script requires POSIX dir_fd support")
def test_mutation_script_reserved_statuses_and_cleanup(tmp_path):
    script = MUTATE_WORKSPACE_FILE.replace("'/workspace'", repr(str(tmp_path)))
    def execute(mode, name):
        return subprocess.run([sys.executable, "-I", "-c", script, name, mode, "1", ".collab-write-test"],
                              input=b"x", capture_output=True)
    (tmp_path / "exists").write_bytes(b"original")
    exists = execute("create", "exists")
    missing = execute("update", "missing")
    assert (exists.returncode, missing.returncode) == (41, 42)
    assert not exists.stderr and not missing.stderr
    assert (tmp_path / "exists").read_bytes() == b"original"
    assert not (tmp_path / ".collab-write-test").exists()
