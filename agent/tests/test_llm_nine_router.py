"""Tests for the local 9Router OpenAI-compatible provider."""
from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from flowboard.services.llm import secrets
from flowboard.services.llm.base import LLMError
from flowboard.services.llm.nine_router import NineRouterProvider


@pytest.fixture
def tmp_secrets_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    path = tmp_path / "secrets.json"
    monkeypatch.setenv("FLOWBOARD_SECRETS_PATH", str(path))
    return path


class _Response:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _completion(text: str = "ok") -> _Response:
    return _Response(payload={"choices": [{"message": {"content": text}}]})


@pytest.mark.asyncio
async def test_is_available_tracks_api_key(tmp_secrets_path):
    provider = NineRouterProvider()
    assert await provider.is_available() is False

    secrets.set_api_key("nine_router", "local-key")
    assert await provider.is_available() is True


@pytest.mark.asyncio
async def test_run_requires_api_key(tmp_secrets_path):
    with pytest.raises(LLMError, match="API key not configured"):
        await NineRouterProvider().run("hello")


@pytest.mark.asyncio
async def test_run_uses_configured_endpoint_and_model(
    tmp_secrets_path, monkeypatch
):
    secrets.set_api_key("nine_router", "local-key")
    secrets.set_model("nine_router", "GEMINI")
    monkeypatch.setenv(
        "FLOWBOARD_NINE_ROUTER_ENDPOINT",
        "http://router.test/v1/chat/completions",
    )

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=_completion("answer")),
    ) as post:
        result = await NineRouterProvider().run(
            "user prompt",
            system_prompt="system prompt",
        )

    assert result == "answer"
    call = post.await_args
    assert call.args[0] == "http://router.test/v1/chat/completions"
    assert call.kwargs["headers"]["authorization"] == "Bearer local-key"
    assert call.kwargs["json"] == {
        "model": "GEMINI",
        "messages": [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "user prompt"},
        ],
        "stream": False,
    }


@pytest.mark.asyncio
async def test_run_encodes_image_attachments(tmp_secrets_path, tmp_path):
    secrets.set_api_key("nine_router", "local-key")
    image = tmp_path / "reference.jpeg"
    image.write_bytes(b"jpeg-bytes")

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=_completion()),
    ) as post:
        await NineRouterProvider().run("describe", attachments=[str(image)])

    content = post.await_args.kwargs["json"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "describe"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {
            "url": (
                "data:image/jpeg;base64,"
                + base64.b64encode(b"jpeg-bytes").decode("ascii")
            )
        },
    }


@pytest.mark.asyncio
async def test_run_surfaces_proxy_error(tmp_secrets_path):
    secrets.set_api_key("nine_router", "local-key")
    response = _Response(
        status_code=502,
        payload={"error": {"message": "all routes unavailable"}},
    )

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=response),
    ):
        with pytest.raises(
            LLMError,
            match="9Router HTTP 502: all routes unavailable",
        ):
            await NineRouterProvider().run("hello")


@pytest.mark.asyncio
async def test_run_wraps_timeout(tmp_secrets_path):
    secrets.set_api_key("nine_router", "local-key")

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=httpx.TimeoutException("late")),
    ):
        with pytest.raises(LLMError, match="timed out after 4.0s"):
            await NineRouterProvider().run("hello", timeout=4.0)


@pytest.mark.asyncio
async def test_list_models_without_key_uses_default(tmp_secrets_path):
    assert await NineRouterProvider().list_models() == [
        "kr/claude-sonnet-4.5"
    ]


@pytest.mark.asyncio
async def test_list_models_uses_models_endpoint(
    tmp_secrets_path, monkeypatch
):
    secrets.set_api_key("nine_router", "local-key")
    monkeypatch.setenv(
        "FLOWBOARD_NINE_ROUTER_ENDPOINT",
        "http://router.test/v1/chat/completions",
    )
    response = _Response(
        payload={"data": [{"id": "GEMINI"}, {"id": "Codex-GPT"}]}
    )

    with patch(
        "httpx.AsyncClient.get",
        new=AsyncMock(return_value=response),
    ) as get:
        models = await NineRouterProvider().list_models()

    assert models == ["GEMINI", "Codex-GPT"]
    assert get.await_args.args[0] == "http://router.test/v1/models"


@pytest.mark.asyncio
async def test_list_models_falls_back_when_proxy_is_offline(
    tmp_secrets_path,
):
    secrets.set_api_key("nine_router", "local-key")

    with patch(
        "httpx.AsyncClient.get",
        new=AsyncMock(side_effect=httpx.ConnectError("offline")),
    ):
        models = await NineRouterProvider().list_models()

    assert models == ["kr/claude-sonnet-4.5"]
