"""Course configuration loading uses an isolated file and environment."""
import os

import pytest

from collaboration_runtime.model import CourseModel, ModelError


@pytest.fixture(autouse=True)
def clear_course_environment(monkeypatch):
    for name in ("COURSE_API_BASE_URL", "COURSE_API_MODEL", "COURSE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


class RecordingModel(CourseModel):
    def __init__(self, base_url, model, api_key, **kwargs):
        self.values = (base_url, model, api_key)


def test_dotenv_bom_literal_values_and_no_environment_mutation(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'COURSE_API_BASE_URL=https://example.invalid/v1\n'
        'COURSE_API_MODEL=course-model\n'
        'COURSE_API_KEY=${DO_NOT_EXPAND}\n',
        encoding="utf-8-sig",
    )
    monkeypatch.setattr("collaboration_runtime.model.PROJECT_ENV", env_file)
    model = RecordingModel.from_env()
    assert model.values == ("https://example.invalid/v1", "course-model", "${DO_NOT_EXPAND}")
    assert all(name not in os.environ for name in ("COURSE_API_BASE_URL", "COURSE_API_MODEL", "COURSE_API_KEY"))


def test_process_environment_takes_precedence(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "COURSE_API_BASE_URL=https://file.invalid\nCOURSE_API_MODEL=file-model\nCOURSE_API_KEY=file-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COURSE_API_MODEL", "environment-model")
    monkeypatch.setenv("COURSE_API_KEY", "environment-key")
    model = RecordingModel.from_env(env_file)
    assert model.values == ("https://file.invalid", "environment-model", "environment-key")
    assert "COURSE_API_BASE_URL" not in os.environ


def test_missing_key_reports_configuration_error(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("COURSE_API_BASE_URL=https://example.invalid\nCOURSE_API_MODEL=course-model\n", encoding="utf-8")
    with pytest.raises(ModelError, match="COURSE_API_KEY"):
        CourseModel.from_env(env_file)
