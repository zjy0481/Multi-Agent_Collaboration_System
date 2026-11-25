import pytest
from collaboration_runtime import atomic_move


def failure(code):
    error = PermissionError("private host path")
    error.winerror = code
    return error


def test_transient_windows_failure_then_atomic_rename(tmp_path, monkeypatch):
    source, target = tmp_path / "stage", tmp_path / "final"
    source.mkdir()
    original = atomic_move.os.rename
    calls = []
    def rename(src, dst):
        calls.append((src, dst))
        if len(calls) == 1:
            raise failure(5)
        original(src, dst)
    monkeypatch.setattr(atomic_move, "_WINDOWS", True)
    monkeypatch.setattr(atomic_move.os, "rename", rename)
    monkeypatch.setattr(atomic_move.time, "sleep", lambda _: None)
    atomic_move.move_artifact_directory(source, target)
    assert len(calls) == 2 and target.is_dir() and not source.exists()


@pytest.mark.parametrize("windows,code,expected", [(True, 32, 4), (True, 33, 4), (True, 5, 4), (True, 2, 1), (False, 5, 1)])
def test_failure_is_bounded_and_not_hidden(tmp_path, monkeypatch, windows, code, expected):
    source, target = tmp_path / "stage", tmp_path / "final"
    source.mkdir()
    calls = []
    def rename(*_):
        calls.append(1)
        raise failure(code)
    monkeypatch.setattr(atomic_move, "_WINDOWS", windows)
    monkeypatch.setattr(atomic_move.os, "rename", rename)
    monkeypatch.setattr(atomic_move.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError):
        atomic_move.move_artifact_directory(source, target)
    assert len(calls) == expected and source.exists() and not target.exists()


def test_existing_target_is_never_replaced(tmp_path, monkeypatch):
    source, target = tmp_path / "stage", tmp_path / "final"
    source.mkdir()
    target.mkdir()
    (target / "keep").write_text("keep")
    def unexpected(*_):
        pytest.fail("destination must not be replaced")
    monkeypatch.setattr(atomic_move.os, "rename", unexpected)
    with pytest.raises(FileExistsError):
        atomic_move.move_artifact_directory(source, target)
    assert (target / "keep").read_text() == "keep"
