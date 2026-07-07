from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

from .base import LLMError, image_mime_type
from . import secrets

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-2.5-flash"
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10MB limit

class GeminiProvider:
    """Conforms to ``LLMProvider`` (structural typing)."""

    name: str = "gemini"
    supports_vision: bool = True
    test_timeout_secs: float = 120.0

    async def is_available(self) -> bool:
        return bool(secrets.get_api_key("gemini"))

    async def run(
        self,
        user_prompt: str,
        *,
        system_prompt: Optional[str] = None,
        attachments: Optional[list[str]] = None,
        timeout: float = 90.0,
    ) -> str:
        key = secrets.get_api_key("gemini")
        if not key:
            raise LLMError("Gemini API key not configured")

        model = secrets.get_model("gemini") or os.environ.get("FLOWBOARD_GEMINI_MODEL") or _DEFAULT_MODEL
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

        parts = []
        if attachments:
            for path in attachments:
                parts.append(self._image_part(path))
        parts.append({"text": user_prompt})

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": parts
                }
            ]
        }

        if system_prompt:
            payload["systemInstruction"] = {
                "parts": [
                    {"text": system_prompt}
                ]
            }

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    url,
                    headers={"content-type": "application/json"},
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise LLMError(f"Gemini request timed out after {timeout}s") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Gemini transport error: {exc}") from exc

        if resp.status_code != 200:
            raise LLMError(
                f"Gemini HTTP {resp.status_code}: {self._safe_error_message(resp)}"
            )

        try:
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError(f"Gemini response structure invalid: {exc}") from exc

    def _image_part(self, path: str) -> dict:
        p = Path(path)
        if not p.exists():
            raise LLMError(f"Attachment file not found: {path}")
        size = p.stat().st_size
        if size > _MAX_ATTACHMENT_BYTES:
            raise LLMError(
                f"Attachment too large for Gemini: "
                f"{size // (1024 * 1024)}MB > 10MB limit"
            )
        mime = image_mime_type(p)
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        return {
            "inlineData": {
                "mimeType": mime,
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
