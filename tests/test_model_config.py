"""Course configuration loading uses an isolated file and environment."""
import os
import asyncio

import httpx

import pytest

from collaboration_runtime.model import CourseModel, ModelError, load_model


@pytest.fixture(autouse=True)
def clear_course_environment(monkeypatch):
    for name in ("COURSE_API_BASE_URL", "COURSE_API_MODEL", "COURSE_API_KEY", "COLLABORATION_MODEL_MODE"):
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


def test_mode_is_explicit_and_process_override_wins(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("COLLABORATION_MODEL_MODE=mock\n", encoding="utf-8")
    model = load_model(env_file)
    assert model.identity == {"mode": "mock", "endpoint": None, "model": "offline-mock"}
    assert "离线" in asyncio.run(model.complete([], []))["content"]
    bootstrap = asyncio.run(model.complete([], [{"function": {"name": "finish_team_setup"}}]))
    assert bootstrap["tool_calls"][0]["function"]["name"] == "finish_team_setup"
    asyncio.run(model.aclose())
    assert "COLLABORATION_MODEL_MODE" not in os.environ
    monkeypatch.setenv("COLLABORATION_MODEL_MODE", "course")
    with pytest.raises(ModelError, match="COURSE_API_KEY"):
        load_model(env_file)
    monkeypatch.setenv("COLLABORATION_MODEL_MODE", "invalid")
    with pytest.raises(ModelError, match="COLLABORATION_MODEL_MODE"):
        load_model(env_file)


def test_course_identity_and_auth_failure_do_not_fallback(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("COURSE_API_BASE_URL=https://example.invalid/v1\nCOURSE_API_MODEL=course-model\nCOURSE_API_KEY=test-secret\n", encoding="utf-8")

    async def check():
        model = load_model(env_file)
        assert model.identity == {"mode": "course", "endpoint": "https://example.invalid", "model": "course-model"}
        await model._client.aclose()
        model._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401, text="test-secret")))
        try:
            with pytest.raises(ModelError, match="401") as error:
                await model.complete([], [])
            assert "test-secret" not in str(error.value)
            assert model.identity["mode"] == "course"
        finally:
            await model.aclose()
    asyncio.run(check())
