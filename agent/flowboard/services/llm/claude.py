from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional

import httpx

from .base import LLMError, image_mime_type
from . import secrets

logger = logging.getLogger(__name__)

_DEFAULT_TEXT_MODEL = "claude-3-5-sonnet-latest"
_DEFAULT_VISION_MODEL = "claude-3-5-sonnet-latest"
_MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # 5MB limit

class ClaudeProvider:
    """Conforms to ``LLMProvider`` (structural typing — no inheritance)."""

    name: str = "claude"
    supports_vision: bool = True

    async def run(
        self,
        user_prompt: str,
        *,
        system_prompt: Optional[str] = None,
        attachments: Optional[list[str]] = None,
        timeout: float = 90.0,
    ) -> str:
        key = secrets.get_api_key("claude")
        if not key:
            raise LLMError("Claude API key not configured")

        model = secrets.get_model("claude") or _DEFAULT_TEXT_MODEL

        # Anthropic messages API payload
        content = []
        if attachments:
            for path in attachments:
                content.append(self._image_block(path))
        content.append({"type": "text", "text": user_prompt})

        payload = {
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": content}],
        }
        if system_prompt:
            payload["system"] = system_prompt

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise LLMError(f"Claude request timed out after {timeout}s") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Claude transport error: {exc}") from exc

        if resp.status_code != 200:
            raise LLMError(
                f"Claude HTTP {resp.status_code}: {self._safe_error_message(resp)}"
            )

        try:
            data = resp.json()
            return data["content"][0]["text"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError(f"Claude response structure invalid: {exc}") from exc

    async def is_available(self) -> bool:
        return bool(secrets.get_api_key("claude"))

    def _image_block(self, path: str) -> dict:
        p = Path(path)
        if not p.exists():
            raise LLMError(f"Attachment file not found: {path}")
        size = p.stat().st_size
        if size > _MAX_ATTACHMENT_BYTES:
            raise LLMError(
                f"Attachment too large for Claude: "
                f"{size // (1024 * 1024)}MB > 5MB limit"
            )
        mime = image_mime_type(p)
        # Anthropic expects image/jpeg, image/png, image/gif, or image/webp
        # Convert mime dynamically (if not supported, fallback to image/jpeg)
        supported_mimes = {"image/jpeg", "image/png", "image/gif", "image/webp"}
        if mime not in supported_mimes:
            mime = "image/jpeg"
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": b64
            }
        }

    def _safe_error_message(self, resp: httpx.Response) -> str:
        try:
            body = resp.json()
            if isinstance(body, dict) and "error" in body:
                err = body["error"]
                if isinstance(err, dict) and "message" in err:
                    return err["message"]
        except ValueError:
            pass
        return resp.text[:200]
