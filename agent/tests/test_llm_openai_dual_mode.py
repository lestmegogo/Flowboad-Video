"""Tests for the OpenAI REST API provider.

The filename is retained to keep historical test paths stable; the provider
no longer has a CLI/API dual mode.
"""
from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from flowboard.services.llm import secrets
from flowboard.services.llm.base import LLMError
from flowboard.services.llm.openai import OpenAIProvider


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
    provider = OpenAIProvider()
    assert await provider.is_available() is False

    secrets.set_api_key("openai", "sk-test")
    assert await provider.is_available() is True


@pytest.mark.asyncio
async def test_run_requires_api_key(tmp_secrets_path):
    with pytest.raises(LLMError, match="API key not configured"):
        await OpenAIProvider().run("hello")


@pytest.mark.asyncio
async def test_run_builds_chat_completion_request(tmp_secrets_path):
    secrets.set_api_key("openai", "sk-test")
    secrets.set_model("openai", "gpt-4o-mini")

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=_completion("answer")),
    ) as post:
        result = await OpenAIProvider().run(
            "user prompt",
            system_prompt="system prompt",
            timeout=8.0,
        )

    assert result == "answer"
    call = post.await_args
    assert call.args[0] == "https://api.openai.com/v1/chat/completions"
    assert call.kwargs["headers"]["authorization"] == "Bearer sk-test"
    assert call.kwargs["json"] == {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "user prompt"},
        ],
    }


@pytest.mark.asyncio
async def test_explicit_model_overrides_saved_model(tmp_secrets_path):
    secrets.set_api_key("openai", "sk-test")
    secrets.set_model("openai", "saved-model")

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=_completion()),
    ) as post:
        await OpenAIProvider().run("hello", model="request-model")

    assert post.await_args.kwargs["json"]["model"] == "request-model"


@pytest.mark.asyncio
async def test_run_encodes_image_attachments(tmp_secrets_path, tmp_path):
    secrets.set_api_key("openai", "sk-test")
    image = tmp_path / "reference.webp"
    image.write_bytes(b"webp-bytes")

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=_completion()),
    ) as post:
        await OpenAIProvider().run("describe", attachments=[str(image)])

    content = post.await_args.kwargs["json"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "describe"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {
            "url": (
                "data:image/webp;base64,"
                + base64.b64encode(b"webp-bytes").decode("ascii")
            )
        },
    }


@pytest.mark.asyncio
async def test_run_rejects_missing_attachment(tmp_secrets_path, tmp_path):
    secrets.set_api_key("openai", "sk-test")

    with pytest.raises(LLMError, match="Attachment file not found"):
        await OpenAIProvider().run(
            "describe",
            attachments=[str(tmp_path / "missing.png")],
        )


@pytest.mark.asyncio
async def test_run_surfaces_api_error(tmp_secrets_path):
    secrets.set_api_key("openai", "sk-test")
    response = _Response(
        status_code=401,
        payload={"error": {"message": "invalid key"}},
    )

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=response),
    ):
        with pytest.raises(LLMError, match="OpenAI HTTP 401: invalid key"):
            await OpenAIProvider().run("hello")


@pytest.mark.asyncio
async def test_run_wraps_transport_error(tmp_secrets_path):
    secrets.set_api_key("openai", "sk-test")

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=httpx.ConnectError("offline")),
    ):
        with pytest.raises(LLMError, match="transport error"):
            await OpenAIProvider().run("hello")


@pytest.mark.asyncio
async def test_run_rejects_malformed_success_response(tmp_secrets_path):
    secrets.set_api_key("openai", "sk-test")

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=_Response(payload={"choices": []})),
    ):
        with pytest.raises(LLMError, match="response missing content"):
            await OpenAIProvider().run("hello")


@pytest.mark.asyncio
async def test_list_models_without_key_uses_default(tmp_secrets_path):
    assert await OpenAIProvider().list_models() == ["gpt-4o"]


@pytest.mark.asyncio
async def test_list_models_returns_sorted_gpt_models(tmp_secrets_path):
    secrets.set_api_key("openai", "sk-test")
    response = _Response(
        payload={
            "data": [
                {"id": "text-embedding-3-small"},
                {"id": "gpt-4o-mini"},
                {"id": "gpt-4o"},
            ]
        }
    )

    with patch(
        "httpx.AsyncClient.get",
        new=AsyncMock(return_value=response),
    ):
        models = await OpenAIProvider().list_models()

    assert models == ["gpt-4o", "gpt-4o-mini"]


@pytest.mark.asyncio
async def test_list_models_falls_back_on_transport_error(tmp_secrets_path):
    secrets.set_api_key("openai", "sk-test")

    with patch(
        "httpx.AsyncClient.get",
        new=AsyncMock(side_effect=httpx.ConnectError("offline")),
    ):
        models = await OpenAIProvider().list_models()

    assert models == ["gpt-4o", "gpt-4o-mini", "o1-mini", "o3-mini"]
