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

_DEFAULT_ENDPOINT = "http://localhost:20128/v1/chat/completions"
_DEFAULT_TEXT_MODEL = "kr/claude-sonnet-4.5"
_DEFAULT_VISION_MODEL = "kr/claude-sonnet-4.5"
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10MB limit

class NineRouterProvider:
    name: str = "nine_router"
    supports_vision: bool = True

    async def is_available(self) -> bool:
        """9Router is available if a key is stored.
        We don't ping here to prevent startup lag."""
        return bool(secrets.get_api_key("nine_router"))

    async def run(
        self,
        user_prompt: str,
        *,
        system_prompt: Optional[str] = None,
        attachments: Optional[list[str]] = None,
        timeout: float = 300.0,
    ) -> str:
        key = secrets.get_api_key("nine_router")
        if not key:
            raise LLMError("9Router API key not configured")

        endpoint = os.environ.get("FLOWBOARD_NINE_ROUTER_ENDPOINT") or _DEFAULT_ENDPOINT
        
        # Look up the model name configured for nine_router in secrets, fallback to env or default
        model = secrets.get_model("nine_router") or os.environ.get("FLOWBOARD_NINE_ROUTER_MODEL") or (
            _DEFAULT_VISION_MODEL if attachments else _DEFAULT_TEXT_MODEL
        )

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if attachments:
            content = [{"type": "text", "text": user_prompt}]
            for path in attachments:
                content.append(self._image_url_block(path))
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user_prompt})

        payload = {"model": model, "messages": messages, "stream": False}

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    endpoint,
                    headers={
                        "authorization": f"Bearer {key}",
                        "content-type": "application/json",
                    },
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise LLMError(f"9Router request timed out after {timeout}s") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"9Router transport error: {exc}") from exc

        if resp.status_code != 200:
            raise LLMError(
                f"9Router HTTP {resp.status_code}: {self._safe_error_message(resp)}"
            )

        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError(f"9Router response missing content: {exc}") from exc

    async def list_models(self) -> list[str]:
        """Fetch active model IDs and combos from 9Router proxy."""
        key = secrets.get_api_key("nine_router")
        if not key:
            return [_DEFAULT_TEXT_MODEL]

        endpoint = os.environ.get("FLOWBOARD_NINE_ROUTER_ENDPOINT") or _DEFAULT_ENDPOINT
        # Deduce the models endpoint from the completions endpoint (http://localhost:20128/v1/models)
        base_url = endpoint.replace("/chat/completions", "")
        models_url = f"{base_url}/models"

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    models_url,
                    headers={"authorization": f"Bearer {key}"}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict) and "data" in data:
                        models = [
                            m["id"]
                            for m in data["data"]
                            if isinstance(m, dict) and "id" in m
                        ]
                        if models:
                            return models
        except Exception as exc:
            logger.warning("Failed to fetch models from 9Router: %s", exc)

        return [_DEFAULT_TEXT_MODEL]

    def _image_url_block(self, path: str) -> dict:
        p = Path(path)
        if not p.exists():
            raise LLMError(f"Attachment file not found: {path}")
        size = p.stat().st_size
        if size > _MAX_ATTACHMENT_BYTES:
            raise LLMError(
                f"Attachment too large for 9Router: "
                f"{size // (1024 * 1024)}MB > 10MB limit"
            )
        mime = image_mime_type(p)
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        }

    def _safe_error_message(self, resp: httpx.Response) -> str:
        try:
            body = resp.json()
            if isinstance(body, dict):
                err = body.get("error")
                if isinstance(err, dict):
                    msg = err.get("message")
                    if isinstance(msg, str):
                        return msg[:200]
                elif isinstance(err, str):
                    return err[:200]
        except ValueError:
            pass
        return resp.text[:200]
