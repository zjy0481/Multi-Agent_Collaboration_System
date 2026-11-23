"""Model provider configuration and requests use isolated files and transport."""
import asyncio
import json
import os

import httpx

import pytest
from fastapi.testclient import TestClient

from collaboration_runtime.api import create_app
from collaboration_runtime.model import CourseModel, ModelError, load_model
from test_runtime import wait_for_terminal
from test_teams import sample


@pytest.fixture(autouse=True)
def clear_course_environment(monkeypatch):
    for name in ("COURSE_API_BASE_URL", "COURSE_API_MODEL", "COURSE_API_KEY", "COLLABORATION_MODEL_MODE",
                 "DEEPSEEK_API_KEY", "OPENCODE_API_KEY"):
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


@pytest.mark.parametrize(("provider", "url", "model_id", "key_name"), [
    ("deepseek", "https://api.deepseek.com/chat/completions", "deepseek-v4-pro", "DEEPSEEK_API_KEY"),
    ("opencode", "https://opencode.ai/inference/go/openai/v1/chat/completions", "deepseek-v4.1-flash", "OPENCODE_API_KEY"),
])
def test_provider_config_routes_the_runtime_tool_loop(tmp_path, monkeypatch, provider, url, model_id, key_name):
    env_file = tmp_path / ".env"
    env_file.write_text(f"COLLABORATION_MODEL_MODE=course\nCOURSE_API_KEY=course-secret\n{key_name}=provider-key\n",
                        encoding="utf-8")
    (tmp_path / "llm.toml").write_text(f'provider = "{provider}"\n[{provider}]\nmodel = "{model_id}"\n',
                                      encoding="utf-8-sig")
    monkeypatch.setattr("collaboration_runtime.model.PROJECT_ENV", env_file)
    monkeypatch.setattr("collaboration_runtime.search.PROJECT_ENV", env_file)
    monkeypatch.setenv("COURSE_SEARCH_URL", "")
    if provider == "opencode":
        monkeypatch.setenv(key_name, "environment-key")
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append((request, payload))
        if len(requests) == 4:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "bootstrap-1", "type": "function", "function": {
                    "name": "finish_team_setup", "arguments": '{"role_id":"lead","reason":"Coordinates work"}',
                },
            }]}
        elif len(requests) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "tool-1", "type": "function", "function": {
                    "name": "run_command", "arguments": '{"command":"echo hello"}',
                },
            }]}
        else:
            message = {"role": "assistant", "content": "Finished"}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    class Sandbox:
        def prepare(self, workspace_id):
            pass

        def run(self, *args, **kwargs):
            return {"exit_code": 0, "stdout": "tool-result", "stderr": ""}

    client_factory = httpx.AsyncClient
    monkeypatch.setattr("collaboration_runtime.model.httpx.AsyncClient", lambda **kwargs: client_factory(
        transport=httpx.MockTransport(respond), **kwargs))
    app = create_app(sandbox=Sandbox(), state_dir=tmp_path / "state", operator_token="test-operator-token")
    with TestClient(app, headers={"Authorization": "Bearer test-operator-token"}) as client:
        for role in ("developer", "tester"):
            started = client.post(f"/v1/sessions/{role}/turns", json={"text": "Try the model"})
            assert started.status_code == 202
            turn_id = started.json()["turn_id"]
            result = wait_for_terminal(client, role, turn_id)
            assert result["status"] == "completed", result
            events = client.get(f"/v1/sessions/{role}/turns/{turn_id}/events").text
            assert f'"mode":"{provider}"' in events.replace(" ", "")
            assert "provider-key" not in events and "environment-key" not in events
        team = client.post("/v1/teams", json={"request_id": "provider-bootstrap",
                                             "roster": sample("scenario_1_software_engineering 2")})
        assert team.status_code == 201, team.text
        assert team.json()["lead_reason"] == "Coordinates work"

    assert len(requests) == 4
    expected_key = "environment-key" if provider == "opencode" else "provider-key"
    for request, payload in requests:
        assert str(request.url) == url
        assert payload["model"] == model_id
        assert payload["thinking"] == {"type": "disabled"}
        assert request.headers["Authorization"] == f"Bearer {expected_key}"
    assert any(tool["function"]["name"] == "run_command" for tool in requests[0][1]["tools"])
    tool_message, = [message for message in requests[1][1]["messages"] if message["role"] == "tool"]
    assert tool_message["tool_call_id"] == "tool-1"
    assert json.loads(tool_message["content"])["result"]["stdout"] == "tool-result"
    sessions = [request.headers.get("x-opencode-session") for request, _ in requests]
    if provider == "opencode":
        assert all(sessions) and sessions[0] == sessions[1] and len(set(sessions)) == 3
        assert all(request.headers["user-agent"].startswith("multi-agent-collaboration/") for request, _ in requests)
    else:
        assert sessions == [None] * 4


def test_provider_key_does_not_fall_back_to_hkgai(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("COURSE_API_KEY=course-secret\n", encoding="utf-8")
    (tmp_path / "llm.toml").write_text('provider = "deepseek"\n', encoding="utf-8")
    with pytest.raises(ModelError, match="DEEPSEEK_API_KEY") as error:
        load_model(env_file)
    assert "course-secret" not in str(error.value)


@pytest.mark.parametrize(("config", "error"), [
    ('provider = "unknown-provider"', "provider"),
    ('provider = ["deepseek"]', "provider"),
    ('provider = "deepseek"\n[deepseek]\nmodel = " "', "deepseek.model"),
    ('provider = "deepseek"\n[deepseek]\napi_key = "do-not-log"', "llm.toml"),
    ('provider = "do-not-log', "llm.toml"),
])
def test_invalid_llm_config_fails_without_exposing_values(tmp_path, config, error):
    env_file = tmp_path / ".env"
    env_file.write_text("COURSE_API_BASE_URL=https://example.invalid/v1\nCOURSE_API_MODEL=course-model\n"
                        "COURSE_API_KEY=course-secret\n", encoding="utf-8")
    (tmp_path / "llm.toml").write_text(config, encoding="utf-8")
    with pytest.raises(ModelError, match=error) as failure:
        load_model(env_file)
    assert "do-not-log" not in str(failure.value)


def test_explicit_mock_mode_does_not_require_selected_provider_key(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("COLLABORATION_MODEL_MODE=mock\n", encoding="utf-8")
    (tmp_path / "llm.toml").write_text('provider = "opencode"\n', encoding="utf-8")
    assert load_model(env_file).identity["mode"] == "mock"
