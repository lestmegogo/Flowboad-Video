"""HTTP routes for the multi-LLM provider Settings UI.

Endpoints:
  GET  /api/llm/providers           — list with state per provider
  PUT  /api/llm/providers/{name}    — set/clear API key
  POST /api/llm/providers/{name}/test — connection ping
  GET  /api/llm/config              — read active feature → provider mapping
  PUT  /api/llm/config              — update mapping

Frontend ↔ backend contract is documented in detail in
``.omc/plans/multi-llm-provider-legacy.md`` (UI Specification → Frontend
↔ backend contract section).

API keys are accepted only via PUT /providers/{name} and never echoed
back. The list endpoint reports `configured: true/false` instead.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from flowboard.services.llm import registry, secrets
from flowboard.services.llm.base import LLMError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/llm", tags=["llm"])


# ── request/response models ───────────────────────────────────────────


class _ApiKeyBody(BaseModel):
    """PUT /api/llm/providers/{name}: `apiKey: null` clears the key."""
    apiKey: Optional[str] = None


class _ModelBody(BaseModel):
    """PUT /api/llm/providers/{name}/model: `model: null` clears the model."""
    model: Optional[str] = None


class _ConfigBody(BaseModel):
    """PUT /api/llm/config: any subset of the three features."""
    auto_prompt: Optional[str] = None
    vision: Optional[str] = None
    planner: Optional[str] = None


# Whitelist for the writable feature → provider mapping. Hand-edited
# secrets.json with garbage values is tolerated by `read_active_providers`,
# but the HTTP surface must reject input that wouldn't route anywhere.
_VALID_PROVIDER_NAMES = {"claude", "gemini", "openai", "nine_router"}
_VALID_FEATURES = ("auto_prompt", "vision", "planner")


# ── GET /api/llm/providers ────────────────────────────────────────────


@router.post("/debug/reset-probe")
async def debug_reset_probe() -> dict:
    """Force re-probe (debug endpoint)."""
    for provider in registry.list_providers():
        if hasattr(provider, "reset_cache"):
            provider.reset_cache()
    return {"ok": True}


@router.get("/providers")
async def list_providers() -> list[dict]:
    """Snapshot per-provider state for the Settings panel.

    Each entry carries everything the UI needs to render the right row
    state without follow-up calls. `configured` reports whether the user
    has done setup (API: key present, regardless of test outcome).
    """
    out: list[dict] = []
    for provider in registry.list_providers():
        available = await provider.is_available()
        configured = bool(secrets.get_api_key(provider.name))

        out.append({
            "name": provider.name,
            "supportsVision": provider.supports_vision,
            "available": available,
            "configured": configured,
            "requiresKey": True,
            "mode": "api",
            "selectedModel": secrets.get_model(provider.name),
        })
    return out


# ── PUT /api/llm/providers/{name} ─────────────────────────────────────


@router.put("/providers/{name}")
async def set_provider_key(name: str, body: _ApiKeyBody) -> dict:
    """Save (or clear, when `apiKey: null`) a provider's API key."""
    if name not in _VALID_PROVIDER_NAMES:
        raise HTTPException(status_code=404, detail=f"unknown provider {name!r}")
    secrets.set_api_key(name, body.apiKey)
    # Bust the relevant provider's availability cache so the next /providers
    # poll reflects the change immediately rather than waiting up to 60s.
    provider = registry.get_provider(name)
    if provider is not None and hasattr(provider, "reset_cache"):
        provider.reset_cache()
    logger.info("llm: api key %s for %s", "set" if body.apiKey else "cleared", name)
    return {"ok": True}


# ── PUT /api/llm/providers/{name}/model ─────────────────────────────────


@router.put("/providers/{name}/model")
async def set_provider_model(name: str, body: _ModelBody) -> dict:
    """Save (or clear, when `model: null`) a provider's configured model."""
    if name not in _VALID_PROVIDER_NAMES:
        raise HTTPException(status_code=404, detail=f"unknown provider {name!r}")
    secrets.set_model(name, body.model)
    logger.info("llm: model set to %s for %s", body.model, name)
    return {"ok": True}


# ── GET /api/llm/providers/{name}/models ────────────────────────────────


@router.get("/providers/{name}/models")
async def get_provider_models(name: str) -> list[str]:
    """Retrieve list of available models for a provider by querying its API."""
    if name not in _VALID_PROVIDER_NAMES:
        raise HTTPException(status_code=404, detail=f"unknown provider {name!r}")
    provider = registry.get_provider(name)
    if provider is None:
        raise HTTPException(status_code=404, detail="provider not registered")

    # If the provider implements list_models, call it to fetch from its API
    if hasattr(provider, "list_models"):
        try:
            return await provider.list_models()
        except Exception as exc:
            logger.warning("Failed to fetch models for %s: %s", name, exc)

    # Default fallback models when key is missing or call fails
    if name == "claude":
        return ["claude-3-5-sonnet-latest", "claude-3-5-haiku-latest", "claude-3-opus-latest"]
    elif name == "gemini":
        return ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-flash", "gemini-1.5-pro"]
    elif name == "openai":
        return ["gpt-4o", "gpt-4o-mini", "o1-mini", "o3-mini"]
    elif name == "nine_router":
        return ["kr/claude-sonnet-4.5", "gemini/gemini-2.5-flash", "Codex-GPT", "GEMINI", "Kiro"]
    return []


# ── POST /api/llm/providers/{name}/test ───────────────────────────────


@router.post("/providers/{name}/test")
async def test_provider(name: str) -> dict:
    """Ping the provider with a tiny prompt and report success / latency."""
    if name not in _VALID_PROVIDER_NAMES:
        raise HTTPException(status_code=404, detail=f"unknown provider {name!r}")
    provider = registry.get_provider(name)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"provider {name!r} not registered")
    if not await provider.is_available():
        return {"ok": False, "error": "provider not configured"}

    started = time.monotonic()
    try:
        # Single-character prompt to keep cost minimal. We do NOT pass
        # max_tokens because some providers (Claude CLI) ignore it; we
        # accept the small overage as a one-shot cost.
        # Timeout aligned with the slowest production feature ceiling
        # (auto_prompt_batch + vision both at 120s). The test endpoint
        # used to time out at 30s while Vision dispatches succeeded
        # because the Test path was tighter than what the user actually
        # runs. 120s keeps Test honest — if Vision passes here, it'll
        # pass at dispatch time too.
        # Gemini retries with exponential backoff on quota exhaustion (429),
        # so it uses 180s to account for multiple retries.
        test_timeout = getattr(provider, "test_timeout_secs", 120.0)
        await provider.run(".", timeout=test_timeout)
    except LLMError as exc:
        return {"ok": False, "error": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001
        # Wrapped so the Test endpoint never 500s — UI can render the
        # error inline regardless of which exception type leaked through.
        logger.exception("llm: test endpoint hit unexpected error for %s", name)
        return {"ok": False, "error": f"unexpected: {type(exc).__name__}"}
    latency_ms = int((time.monotonic() - started) * 1000)
    return {"ok": True, "latencyMs": latency_ms}


# ── GET /api/llm/config ───────────────────────────────────────────────


@router.get("/config")
def get_config() -> dict:
    """Return the feature → provider mapping plus the ``configured`` flag.

    Per-feature values are ``str | null`` — null means the user hasn't
    pinned a provider for that feature yet. ``configured`` is True only
    when all three features are pinned at the same provider (single-
    provider UI invariant); the frontend uses this to gate the forced
    AI Provider setup dialog on first run.
    """
    saved = secrets.read_active_providers()
    out: dict = {f: saved.get(f) for f in _VALID_FEATURES}
    out["configured"] = secrets.is_active_providers_configured()
    return out


# ── PUT /api/llm/config ───────────────────────────────────────────────


@router.put("/config")
def set_config(body: _ConfigBody) -> dict:
    """Update one or more feature → provider assignments.

    Validates names against the whitelist + feature keys against the
    enum. Provider availability is NOT checked here — picking an
    unconfigured provider is allowed (the dispatch path will fail loud
    when invoked, surfacing the gap to the user). Lets the user pre-pin
    a provider before completing setup.
    """
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="no fields to update")

    for feature, provider_name in updates.items():
        if feature not in _VALID_FEATURES:
            raise HTTPException(status_code=400, detail=f"unknown feature {feature!r}")
        if provider_name not in _VALID_PROVIDER_NAMES:
            raise HTTPException(
                status_code=400, detail=f"unknown provider {provider_name!r}"
            )
    for feature, provider_name in updates.items():
        secrets.set_feature_provider(feature, provider_name)
    logger.info("llm: config updated providers=%s", updates)
    return {"ok": True}
