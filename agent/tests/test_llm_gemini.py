"""Tests for the Gemini REST API provider."""
from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from flowboard.services.llm import secrets
from flowboard.services.llm.base import LLMError
from flowboard.services.llm.gemini import GeminiProvider


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


def _success(text: str = "ok") -> _Response:
    return _Response(
        payload={"candidates": [{"content": {"parts": [{"text": text}]}}]}
    )


@pytest.mark.asyncio
async def test_is_available_tracks_api_key(tmp_secrets_path):
    provider = GeminiProvider()
    assert await provider.is_available() is False

    secrets.set_api_key("gemini", "gemini-key")
    assert await provider.is_available() is True


@pytest.mark.asyncio
async def test_run_requires_api_key(tmp_secrets_path):
    with pytest.raises(LLMError, match="API key not configured"):
        await GeminiProvider().run("hello")


@pytest.mark.asyncio
async def test_run_builds_generate_content_request(tmp_secrets_path):
    secrets.set_api_key("gemini", "secret-key")
    secrets.set_model("gemini", "gemini-2.5-pro")

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=_success("answer")),
    ) as post:
        result = await GeminiProvider().run(
            "user prompt",
            system_prompt="system prompt",
            timeout=12.0,
        )

    assert result == "answer"
    call = post.await_args
    assert (
        call.args[0]
        == "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-pro:generateContent?key=secret-key"
    )
    assert call.kwargs["json"] == {
        "contents": [{"role": "user", "parts": [{"text": "user prompt"}]}],
        "systemInstruction": {"parts": [{"text": "system prompt"}]},
    }


@pytest.mark.asyncio
async def test_run_uses_env_model_when_no_saved_model(
    tmp_secrets_path, monkeypatch
):
    secrets.set_api_key("gemini", "key")
    monkeypatch.setenv("FLOWBOARD_GEMINI_MODEL", "gemini-custom")

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=_success()),
    ) as post:
        await GeminiProvider().run("hello")

    assert "/models/gemini-custom:generateContent" in post.await_args.args[0]


@pytest.mark.asyncio
async def test_saved_model_wins_over_environment(tmp_secrets_path, monkeypatch):
    secrets.set_api_key("gemini", "key")
    secrets.set_model("gemini", "saved-model")
    monkeypatch.setenv("FLOWBOARD_GEMINI_MODEL", "env-model")

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=_success()),
    ) as post:
        await GeminiProvider().run("hello")

    assert "/models/saved-model:generateContent" in post.await_args.args[0]


@pytest.mark.asyncio
async def test_run_encodes_image_attachments(tmp_secrets_path, tmp_path):
    secrets.set_api_key("gemini", "key")
    image = tmp_path / "reference.png"
    image.write_bytes(b"image-bytes")

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=_success()),
    ) as post:
        await GeminiProvider().run("describe", attachments=[str(image)])

    parts = post.await_args.kwargs["json"]["contents"][0]["parts"]
    assert parts[0] == {
        "inlineData": {
            "mimeType": "image/png",
            "data": base64.b64encode(b"image-bytes").decode("ascii"),
        }
    }
    assert parts[1] == {"text": "describe"}


@pytest.mark.asyncio
async def test_run_rejects_missing_attachment(tmp_secrets_path, tmp_path):
    secrets.set_api_key("gemini", "key")
    missing = tmp_path / "missing.jpg"

    with pytest.raises(LLMError, match="Attachment file not found"):
        await GeminiProvider().run("describe", attachments=[str(missing)])


@pytest.mark.asyncio
async def test_run_surfaces_api_error_without_leaking_response_shape(
    tmp_secrets_path,
):
    secrets.set_api_key("gemini", "key")
    response = _Response(
        status_code=429,
        payload={"error": {"message": "quota exhausted"}},
    )

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=response),
    ):
        with pytest.raises(LLMError, match="Gemini HTTP 429: quota exhausted"):
            await GeminiProvider().run("hello")


@pytest.mark.asyncio
async def test_run_wraps_timeout(tmp_secrets_path):
    secrets.set_api_key("gemini", "key")

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=httpx.TimeoutException("late")),
    ):
        with pytest.raises(LLMError, match="timed out after 3.0s"):
            await GeminiProvider().run("hello", timeout=3.0)


@pytest.mark.asyncio
async def test_run_rejects_malformed_success_response(tmp_secrets_path):
    secrets.set_api_key("gemini", "key")

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=_Response(payload={"candidates": []})),
    ):
        with pytest.raises(LLMError, match="response structure invalid"):
            await GeminiProvider().run("hello")
