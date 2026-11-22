"""Explicitly configured Chat Completions client for the course model."""

from __future__ import annotations

import ipaddress
import math
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from dotenv import dotenv_values


PROJECT_ENV = Path(__file__).resolve().parents[1] / ".env"
COURSE_SETTINGS = ("COURSE_API_BASE_URL", "COURSE_API_MODEL", "COURSE_API_KEY")


class ModelError(Exception):
    """A model configuration, transport, or response error without secrets."""


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


class CourseModel:
    def __init__(self, base_url: str, model: str, api_key: str, *, timeout: float = 60):
        self._url = _endpoint(base_url)
        if not isinstance(model, str) or not model.strip():
            raise ModelError("Course API model is required")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ModelError("Course API key is required")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ModelError("Course API timeout must be a positive finite number")
        self._model = model.strip()
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> CourseModel:
        """Read course settings locally while keeping file secrets out of os.environ."""
        try:
            file_values = dotenv_values(
                PROJECT_ENV if env_file is None else env_file,
                encoding="utf-8-sig",
                interpolate=False,
            )
        except (OSError, UnicodeError):
            raise ModelError("Could not read the project .env as UTF-8") from None
        values = [
            (os.environ.get(name, file_values.get(name)) or "").strip()
            for name in COURSE_SETTINGS
        ]
        missing = [name for name, value in zip(COURSE_SETTINGS, values) if not value]
        if missing:
            raise ModelError(f"Set {', '.join(missing)} in the project .env or process environment")
        return cls(*values)

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        payload = {"model": self._model, "messages": messages}
        if tools:
            payload["tools"] = tools
        try:
            response = await self._client.post(self._url, json=payload)
        except httpx.TimeoutException:
            raise ModelError("Course API request timed out") from None
        except (httpx.RequestError, ValueError, TypeError):
            raise ModelError("Course API request failed") from None
        if not 200 <= response.status_code < 300:
            raise ModelError(f"Course API returned HTTP {response.status_code}")
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
            raise ModelError("Course API returned an invalid Chat Completions response") from None

    async def aclose(self) -> None:
        await self._client.aclose()
