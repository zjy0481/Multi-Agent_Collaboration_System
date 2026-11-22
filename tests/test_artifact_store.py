import hashlib
import sqlite3

import pytest

from collaboration_runtime.artifacts import ArtifactError, ArtifactNotFound, ArtifactStore


@pytest.fixture
def store(tmp_path):
    db = sqlite3.connect(":memory:")
    return ArtifactStore(db, tmp_path / "artifacts")


def publish(store, content=b"first", **kwargs):
    values = dict(team_id="team", author_session_id="dev", source_path="file.txt",
                  content=content, reader_session_ids=["qa"])
    values.update(kwargs)
    return store.publish(**values)


def test_versions_immutable_and_acl_per_version(store):
    one = publish(store)
    two = publish(store, b"second", artifact_id=one["artifact_id"], reader_session_ids=["release"])
    assert one["version"] == 1 and two["version"] == 2
    assert one["version_id"] != two["version_id"]
    assert one["sha256"] == hashlib.sha256(b"first").hexdigest()
    assert store.resolve("team", "qa", one["version_id"])[1].read_bytes() == b"first"
    assert store.resolve_number("team", "release", one["artifact_id"], 2)[1].read_bytes() == b"second"
    assert len(store.list_for("team", "dev")) == 2
    assert store.list_for("team", "qa") == [one]
    for args in [("team", "qa", two["version_id"]), ("other", "dev", one["version_id"]),
                 ("team", "dev", "a" * 32)]:
        with pytest.raises(ArtifactNotFound):
            store.resolve(*args)


def test_revision_rejects_other_author_or_team(store):
    item = publish(store)
    for kwargs in ({"team_id": "other"}, {"author_session_id": "other"}):
        with pytest.raises(ArtifactError):
            publish(store, artifact_id=item["artifact_id"], **kwargs)
    with pytest.raises(ArtifactError):
        publish(store, artifact_id="a" * 32)
    assert len(store.list_for("team", "dev")) == 1


def test_tampering_and_unregistered_file(store):
    item = publish(store)
    path = store.resolve("team", "dev", item["version_id"])[1]
    path.write_bytes(b"tampered")
    with pytest.raises(ArtifactError, match="integrity"):
        store.resolve("team", "dev", item["version_id"])
    with pytest.raises(ArtifactNotFound):
        store.resolve("team", "qa", "b" * 32)


def test_insert_failure_removes_only_new_files(store):
    existing = publish(store)
    store.db.execute("CREATE TRIGGER reject_artifact BEFORE INSERT ON artifact_versions BEGIN SELECT RAISE(FAIL, 'denied'); END")
    before = set(store.root.iterdir())
    with pytest.raises(ArtifactError):
        publish(store)
    assert set(store.root.iterdir()) == before
    assert store.resolve("team", "dev", existing["version_id"])[1].read_bytes() == b"first"


def test_invalid_inputs(store):
    with pytest.raises(ArtifactError):
        publish(store, content="not bytes")
    with pytest.raises(ArtifactNotFound):
        store.resolve_number("team", "dev", "../x", 1)
