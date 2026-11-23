"""Configured model providers that use Chat Completions."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import tomllib
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from dotenv import dotenv_values


PROJECT_ENV = Path(__file__).resolve().parents[1] / ".env"
COURSE_SETTINGS = ("COURSE_API_BASE_URL", "COURSE_API_MODEL", "COURSE_API_KEY")
PROVIDER_SETTINGS = {
    "deepseek": ("https://api.deepseek.com", "deepseek-flash", "DEEPSEEK_API_KEY"),
    "opencode": ("https://opencode.ai/inference/go/openai/v1", "deepseek-v4.1-flash", "OPENCODE_API_KEY"),
}
PROVIDER_LABELS = {"course": "Course API", "deepseek": "DeepSeek API", "opencode": "OpenCode Go API"}
logger = logging.getLogger(__name__)


class ModelError(Exception):
    """A model configuration, transport, or response error without secrets."""


class ModelUnavailable(ModelError):
    """A transient provider failure after the request retry limit."""


def _endpoint(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ModelError("Course API base URL is required")
    try:
        parts = urlsplit(base_url.strip())
        host = parts.hostname
        port = parts.port  # Validate malformed ports before creating a client.
        del port
        if not host or parts.username is not None or parts.password is not None:
            raise ValueError
        if parts.query or parts.fragment:
            raise ValueError
        if parts.scheme == "http":
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host.lower() == "localhost"
            if not loopback:
                raise ValueError
        elif parts.scheme != "https":
            raise ValueError
        path = parts.path.rstrip("/")
        if not path.endswith("/chat/completions"):
            path += "/chat/completions"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    except ValueError:
        raise ModelError("Course API URL must be HTTPS or loopback HTTP, without credentials, query, or fragment") from None


def _env_values(env_file: Path) -> dict:
    try:
        return dotenv_values(env_file, encoding="utf-8-sig", interpolate=False)
    except (OSError, UnicodeError):
        raise ModelError("Could not read the project .env as UTF-8") from None


def _required_settings(names, values):
    settings = [(os.environ.get(name, values.get(name)) or "").strip() for name in names]
    missing = [name for name, value in zip(names, settings) if not value]
    if missing:
        raise ModelError(f"Set {', '.join(missing)} in the project .env or process environment")
    return settings


def _provider_config(config_file: Path) -> tuple[str, dict]:
    try:
        config = tomllib.loads(config_file.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return "hkgai", {}
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ModelError("Could not read llm.toml as UTF-8 TOML") from None
    if config.keys() - {"provider", *PROVIDER_SETTINGS}:
        raise ModelError("llm.toml supports only provider, [deepseek] and [opencode]; put API keys in .env")
    provider = config.get("provider", "hkgai")
    if not isinstance(provider, str) or (provider != "hkgai" and provider not in PROVIDER_SETTINGS):
        raise ModelError("llm.toml provider must be hkgai, deepseek or opencode")
    models = {}
    for name in PROVIDER_SETTINGS.keys() & config.keys():
        section = config[name]
        if not isinstance(section, dict) or section.keys() - {"model"}:
            raise ModelError(f"llm.toml [{name}] supports only model; put API keys in .env")
        if "model" in section:
            model = section["model"]
            if not isinstance(model, str) or not model.strip():
                raise ModelError(f"llm.toml {name}.model must be a non-empty string")
            models[name] = model.strip()
    return provider, models


class ChatCompletionsModel:
    def __init__(self, base_url: str, model: str, api_key: str, *, mode: str, timeout: float = 60):
        self._label = PROVIDER_LABELS[mode]
        self._url = _endpoint(base_url)
        if not isinstance(model, str) or not model.strip():
            raise ModelError(f"{self._label} model is required")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ModelError(f"{self._label} key is required")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ModelError(f"{self._label} timeout must be a positive finite number")
        self._model = model.strip()
        self._disable_thinking = mode == "deepseek" or (mode == "opencode" and self._model.startswith("deepseek-"))
        parts = urlsplit(self._url)
        self.identity = {"mode": mode, "endpoint": urlunsplit((parts.scheme, parts.netloc, "", "", "")), "model": self._model}
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        return await self._complete(messages, tools)

    async def _complete(self, messages: list[dict], tools: list[dict], *, headers=None) -> dict:
        payload = {"model": self._model, "messages": messages}
        if tools:
            payload["tools"] = tools
        if self._disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        for attempt in range(3):
            try:
                response = await self._client.post(self._url, json=payload, headers=headers)
                if 200 <= response.status_code < 300:
                    break
                error = f"{self._label} returned HTTP {response.status_code}"
                retryable = response.status_code in (408, 429, 500, 502, 503, 504)
            except httpx.TimeoutException:
                error = f"{self._label} request timed out"
                retryable = True
            except httpx.RequestError:
                error = f"{self._label} request failed"
                retryable = True
            except (ValueError, TypeError):
                raise ModelError(f"{self._label} request failed") from None
            if not retryable or attempt == 2:
                raise (ModelUnavailable(error) if retryable else ModelError(error)) from None
            delay = 0.5 * (2 ** attempt)
            logger.warning("Course model request retry %s after %s; waiting %.1f seconds", attempt + 1, error, delay)
            await asyncio.sleep(delay)
        try:
            data = response.json()
            message = data["choices"][0]["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError
            calls = message.get("tool_calls")
            if calls is not None:
                if not isinstance(calls, list):
                    raise ValueError
                ids = set()
                for call in calls:
                    if not isinstance(call, dict) or call.get("type") != "function":
                        raise ValueError
                    call_id, function = call.get("id"), call.get("function")
                    if not isinstance(call_id, str) or not call_id or call_id in ids or not isinstance(function, dict):
                        raise ValueError
                    if not isinstance(function.get("name"), str) or not function["name"] or not isinstance(function.get("arguments"), str):
                        raise ValueError
                    ids.add(call_id)
            if "reasoning_content" in message and message["reasoning_content"] is not None and not isinstance(message["reasoning_content"], str):
                raise ValueError
            return {**message, "content": content}
        except (ValueError, TypeError, KeyError, IndexError):
            raise ModelError(f"{self._label} returned an invalid Chat Completions response") from None

    async def aclose(self) -> None:
        await self._client.aclose()


class CourseModel(ChatCompletionsModel):
    def __init__(self, base_url: str, model: str, api_key: str, *, timeout: float = 60):
        super().__init__(base_url, model, api_key, mode="course", timeout=timeout)

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> CourseModel:
        values = _env_values(PROJECT_ENV if env_file is None else env_file)
        return cls(*_required_settings(COURSE_SETTINGS, values))


class OpenCodeModel(ChatCompletionsModel):
    def __init__(self, base_url: str, model: str, api_key: str):
        super().__init__(base_url, model, api_key, mode="opencode")
        self._standalone_session = uuid4().hex

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        return await self.complete_for_session(messages, tools, session_id=self._standalone_session)

    async def complete_for_session(self, messages: list[dict], tools: list[dict], *, session_id: str) -> dict:
        return await self._complete(messages, tools, headers={
            "User-Agent": "multi-agent-collaboration/1.0",
            "X-OpenCode-Session": f"ses_{uuid5(NAMESPACE_URL, session_id).hex}",
        })


async def complete_model(model, messages: list[dict], tools: list[dict], *, session_id: str) -> dict:
    complete_for_session = getattr(model, "complete_for_session", None)
    if complete_for_session is not None:
        return await complete_for_session(messages, tools, session_id=session_id)
    return await model.complete(messages, tools)


class MockModel:
    """Explicit offline substitute; it does not contact a model provider."""

    def __init__(self):
        self.identity = {"mode": "mock", "endpoint": None, "model": "offline-mock"}

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        if any(tool["function"]["name"] == "finish_team_setup" for tool in tools):
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "offline-team-setup", "type": "function", "function": {
                    "name": "finish_team_setup",
                    "arguments": json.dumps({"role_id": None, "reason": "Offline mock uses the default Lead"}),
                },
            }]}
        return {"role": "assistant", "content": "离线模拟回复：未调用课程模型。"}

    async def aclose(self) -> None:
        pass


def load_model(env_file: Path | None = None) -> ChatCompletionsModel | MockModel:
    """Read the selected provider and only its credentials; never fall back on errors."""
    env_file = PROJECT_ENV if env_file is None else env_file
    values = _env_values(env_file)
    configured = os.environ.get("COLLABORATION_MODEL_MODE", values.get("COLLABORATION_MODEL_MODE"))
    mode = "course" if configured is None else configured.strip()
    if mode == "mock":
        return MockModel()
    if mode != "course":
        raise ModelError("COLLABORATION_MODEL_MODE must be course or mock")
    provider, models = _provider_config(env_file.with_name("llm.toml"))
    if provider == "hkgai":
        return CourseModel(*_required_settings(COURSE_SETTINGS, values))
    base_url, default_model, key_name = PROVIDER_SETTINGS[provider]
    api_key, = _required_settings((key_name,), values)
    selected_model = models.get(provider, default_model)
    if provider == "opencode":
        return OpenCodeModel(base_url, selected_model, api_key)
    return ChatCompletionsModel(base_url, selected_model, api_key, mode=provider)
