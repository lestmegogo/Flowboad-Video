"""In-process worker that drains queued generation requests.

Scope for Run 3 (Phase 2 bridge): a single handler type `"proxy"` that
forwards `params = {url, method?, headers?, body?}` through the extension
via ``flow_client.api_request``. Further types (gen_image, gen_video,
upload_image, etc.) land in later runs once the full Flow protocol + captcha
round-trip is ported.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from flowboard.db import get_session
from flowboard.db.models import Request
from flowboard.services import media as media_service
from flowboard.services.flow_client import flow_client
from flowboard.services.flow_sdk import get_flow_sdk

logger = logging.getLogger(__name__)


# type → coroutine(params) → (result_dict, error_or_None)
Handler = Callable[[dict], Awaitable[tuple[dict, Optional[str]]]]


_ALLOWED_URL_PREFIXES: tuple[str, ...] = (
    "https://aisandbox-pa.googleapis.com/",
)


async def _handle_proxy(params: dict) -> tuple[dict, Optional[str]]:
    url = params.get("url")
    method = params.get("method", "POST")
    if not isinstance(url, str) or not url:
        return {}, "missing_url"
    # Defense-in-depth: refuse to proxy URLs outside the expected allowlist
    # even if the extension's own check was somehow bypassed.
    if not any(url.startswith(p) for p in _ALLOWED_URL_PREFIXES):
        return {}, "url_not_allowed"
    resp = await flow_client.api_request(
        url=url,
        method=method,
        headers=params.get("headers") or {},
        body=params.get("body"),
    )
    if not isinstance(resp, dict):
        return {"value": resp}, None
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    status = resp.get("status")
    if isinstance(status, int) and status >= 400:
        return resp, f"API_{status}"
    return resp, None


async def _handle_create_project(params: dict) -> tuple[dict, Optional[str]]:
    name = params.get("name") or params.get("title") or "Untitled"
    if not isinstance(name, str) or not name.strip():
        return {}, "missing_name"
    tool = params.get("tool", "PINHOLE")
    resp = await get_flow_sdk().create_project(name.strip(), tool)
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    return resp, None


async def _handle_gen_image(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.flow_sdk import is_valid_project_id

    prompt = params.get("prompt")
    project_id = params.get("project_id")
    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "missing_prompt"
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"
    aspect = params.get("aspect_ratio") or "IMAGE_ASPECT_RATIO_LANDSCAPE"
    # Tier resolution: caller-stamped value first (set at dispatch time),
    # then the live value from `flow_client` (resolved authoritatively
    # via /v1/credits on token capture). NO silent default — if both
    # are absent we fail loud with `paygate_tier_unknown`. The old
    # behaviour (default `PAYGATE_TIER_ONE`) silently downgraded Ultra
    # users to Pro and stamped the wrong tier into request.params, which
    # then fed back through `_last_observed_paygate_tier_from_db()` and
    # corrupted /api/auth/me responses for the rest of the session.
    tier = params.get("paygate_tier") or flow_client.paygate_tier
    if tier is None:
        return {}, "paygate_tier_unknown"

    # `ref_media_ids` is the broader name (any upstream image / character /
    # visual_asset feeds in as IMAGE_INPUT_TYPE_REFERENCE). Older callers used
    # `character_media_ids` — accept both.
    raw_ref_ids = params.get("ref_media_ids")
    if not isinstance(raw_ref_ids, list):
        raw_ref_ids = params.get("character_media_ids")
    ref_media_ids: Optional[list[str]] = None
    if isinstance(raw_ref_ids, list):
        cleaned = [m for m in raw_ref_ids if isinstance(m, str) and m]
        ref_media_ids = cleaned or None
    raw_count = params.get("variant_count")
    variant_count = 1
    if isinstance(raw_count, int) and raw_count > 0:
        variant_count = raw_count
    # Per-variant prompts (optional). When provided, each variant gets its
    # own text — used by auto-prompt batch mode so variants don't collapse
    # to the same stance.
    raw_prompts = params.get("prompts")
    per_variant_prompts: Optional[list[str]] = None
    if isinstance(raw_prompts, list):
        cleaned = [p for p in raw_prompts if isinstance(p, str) and p.strip()]
        per_variant_prompts = cleaned or None
    image_model = params.get("image_model")
    if not isinstance(image_model, str) or not image_model.strip():
        image_model = None
    resp = await get_flow_sdk().gen_image(
        prompt=prompt.strip(),
        project_id=project_id,
        aspect_ratio=aspect,
        paygate_tier=tier,
        ref_media_ids=ref_media_ids,
        variant_count=variant_count,
        prompts=per_variant_prompts,
        image_model=image_model,
    )
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    # Flow returns signed fifeUrls directly in the response — persist them
    # immediately so `/media/:id` can serve bytes without any extra round-trip.
    entries_with_urls = [
        e for e in (resp.get("media_entries") or []) if isinstance(e, dict) and e.get("url")
    ]
    if entries_with_urls:
        try:
            media_service.ingest_urls(entries_with_urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest from gen_image response failed")
    from flowboard.services.media_project_sync import (
        record_media_project_identity,
    )

    record_media_project_identity(resp.get("media_ids") or [], project_id)
    return resp, None


# Video polling knobs — overridable in tests. 5-minute hard deadline
# (30 cycles × 10s). When the budget runs out without all ops finishing
# the handler returns the ``timeout_waiting_video`` sentinel and the
# worker stamps the row as ``status='timeout'`` (distinct from
# ``failed``) so the UI can render it as a soft auto-cancel rather than
# a generation error.
VIDEO_POLL_INTERVAL_S = 10.0
VIDEO_POLL_MAX_CYCLES = 90
OMNI_FAILURE_CONFIRM_CYCLES = 3
# Accepted generation requests can consume credits independently, so only
# transport failures before acceptance are eligible for automatic retries.
OMNI_GENERATION_MAX_ATTEMPTS = 1
OMNI_DISPATCH_MAX_ATTEMPTS = 3
OMNI_GENERATION_RETRY_DELAY_S = 5.0


def _is_request_canceled(rid: Optional[int]) -> bool:
    """Return True iff the cancel endpoint flipped this row to canceled.

    Long-running handlers call this between polls so a user-initiated
    cancel takes effect mid-flight (we can't abort the Flow HTTP calls
    themselves, but we can stop polling and let _process_one keep the
    canceled status intact).
    """
    if not isinstance(rid, int):
        return False
    with get_session() as s:
        req = s.get(Request, rid)
        if req is None:
            return True
        return req.status == "canceled"


def _set_request_progress(
    rid: Optional[int],
    stage: str,
    **extra: object,
) -> None:
    """Best-effort progress metadata for long-running activity rows.

    The Activity panel already has the Request row while a video is running;
    updating ``result.progress`` lets us see whether time is being spent in
    reference sync, Flow dispatch/captcha, or render polling without changing
    the visible node layout.
    """
    if not isinstance(rid, int):
        return
    try:
        with get_session() as s:
            req = s.get(Request, rid)
            if req is None or req.status == "canceled":
                return
            result = dict(req.result or {})
            progress = dict(result.get("progress") or {})
            progress.update(
                {
                    "stage": stage,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    **extra,
                }
            )
            result["progress"] = progress
            req.result = result
            s.add(req)
            s.commit()
    except Exception:  # noqa: BLE001
        logger.debug("request progress update failed for rid=%s", rid)


def _merge_request_result(rid: Optional[int], **extra: object) -> None:
    """Best-effort merge into Request.result while a long job is running."""
    if not isinstance(rid, int):
        return
    try:
        with get_session() as s:
            req = s.get(Request, rid)
            if req is None or req.status == "canceled":
                return
            result = dict(req.result or {})
            result.update(extra)
            req.result = result
            s.add(req)
            s.commit()
    except Exception:  # noqa: BLE001
        logger.debug("request result merge failed for rid=%s", rid)


async def _handle_gen_video(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.flow_sdk import is_valid_project_id

    prompt = params.get("prompt")
    project_id = params.get("project_id")
    start_media_id = params.get("start_media_id") or params.get("startMediaId")
    raw_starts = params.get("start_media_ids")
    start_media_ids: Optional[list[str]] = None
    if isinstance(raw_starts, list):
        cleaned = [m for m in raw_starts if isinstance(m, str) and m.strip()]
        start_media_ids = [m.strip() for m in cleaned] or None

    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "missing_prompt"
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"
    # Either a single start_media_id OR a non-empty start_media_ids list.
    if start_media_ids is None and (
        not isinstance(start_media_id, str) or not start_media_id.strip()
    ):
        return {}, "missing_start_media_id"
    aspect = params.get("aspect_ratio") or "VIDEO_ASPECT_RATIO_LANDSCAPE"
    # Tier resolution — see the matching block in _handle_gen_image for
    # the rationale. No silent default; missing tier is a hard error so
    # we never dispatch an Ultra user's video at the Pro checkpoint.
    tier = params.get("paygate_tier") or flow_client.paygate_tier
    if tier is None:
        return {}, "paygate_tier_unknown"
    video_quality = params.get("video_quality")
    if not isinstance(video_quality, str) or not video_quality.strip():
        video_quality = None

    sdk = get_flow_sdk()
    dispatch = await sdk.gen_video(
        prompt=prompt.strip(),
        project_id=project_id,
        start_media_id=start_media_id.strip()
        if isinstance(start_media_id, str) and start_media_id.strip()
        else None,
        start_media_ids=start_media_ids,
        aspect_ratio=aspect,
        paygate_tier=tier,
        video_quality=video_quality,
    )
    if dispatch.get("error"):
        return dispatch, str(dispatch["error"])[:200]

    op_names = dispatch.get("operation_names") or []
    if not op_names:
        return dispatch, "no_operations_returned"
    # NEW low-priority models return workflows (`{name, primary_media_id}`)
    # instead of operations; the SDK surfaces them on `dispatch["workflows"]`
    # so we can resolve media through Flow's authenticated redirect endpoint
    # instead of batchCheckAsync.
    workflows = dispatch.get("workflows") or None

    poll_attempts = 0
    last_poll: dict = {}
    done_by_name: dict[str, bool] = {name: False for name in op_names}
    entry_by_name: dict[str, dict] = {}
    op_errors: dict[str, str] = {}
    rid = params.get("__request_id")

    # Per-op resolution: each operation in the batch resolves
    # independently (success, content-filter rejection, or timeout). We
    # used to break the whole loop on the first per-op error, which
    # collapsed a 4-variant gen into a hard failure even when 3/4 clips
    # had already rendered. Now we let every op terminate on its own
    # and aggregate the outcome at the end so partial batches still
    # surface the variants that did succeed.
    while (
        poll_attempts < VIDEO_POLL_MAX_CYCLES
        and not all(done_by_name.values())
    ):
        await asyncio.sleep(VIDEO_POLL_INTERVAL_S)
        poll_attempts += 1
        if _is_request_canceled(rid):
            # User canceled mid-poll. Bail with the special error code
            # so _process_one knows to leave the row's canceled status
            # intact (the cancel endpoint already stamped finished_at +
            # error='canceled'). Any partial state we collected is
            # preserved on `result` for the detail viewer.
            return (
                {
                    "raw_dispatch": dispatch,
                    "last_poll": last_poll,
                    "operation_names": op_names,
                    "done": done_by_name,
                    "canceled": True,
                },
                "canceled",
            )
        last_poll = await sdk.check_async(op_names, workflows=workflows)
        if last_poll.get("error"):
            continue
        for op in last_poll.get("operations") or []:
            if not isinstance(op, dict):
                continue
            name = op.get("name")
            if not isinstance(name, str) or done_by_name.get(name, False):
                continue
            # Per-op terminal failure (e.g. content filter
            # PUBLIC_ERROR_UNSAFE_GENERATION / PUBLIC_ERROR_AUDIO_FILTERED).
            # Mark this op resolved-with-error and keep polling the rest.
            err = op.get("error")
            if isinstance(err, str) and err:
                done_by_name[name] = True
                op_errors[name] = err
                continue
            if op.get("done"):
                done_by_name[name] = True
                # Each op is expected to yield exactly one media entry
                # on success; capture the first valid one.
                for e in op.get("media_entries") or []:
                    if isinstance(e, dict) and e.get("media_id"):
                        entry_by_name[name] = e
                        break

    # Slots still unresolved after the max cycles — record as timeout
    # so the partial summary names them alongside any filter failures.
    for name in op_names:
        if not done_by_name.get(name) and name not in op_errors:
            op_errors[name] = "timeout_waiting_video"

    # Build positional outcome aligned to dispatch order. Slot i in
    # `media_ids` corresponds to slot i in the original
    # `start_media_ids` array, so the frontend can keep upstream-image
    # variant ↔ video-variant alignment even when middle slots fail.
    # `slot_errors` mirrors the same indexing — `None` for succeeded
    # slots, error code for blocked ones — so the detail viewer can
    # render the exact filter reason on the blocked tile without
    # having to know the internal Flow op-name keys.
    positional_ids: list[Optional[str]] = []
    slot_errors: list[Optional[str]] = []
    succeeded_entries: list[dict] = []
    for name in op_names:
        e = entry_by_name.get(name)
        if isinstance(e, dict) and isinstance(e.get("media_id"), str):
            positional_ids.append(e["media_id"])
            succeeded_entries.append(e)
            slot_errors.append(None)
        else:
            positional_ids.append(None)
            slot_errors.append(op_errors.get(name))

    success_count = sum(1 for x in positional_ids if x)
    total = len(op_names)

    if success_count == 0:
        # No op produced a clip — surface the first error verbatim.
        # When all errors are "timeout_waiting_video" this matches the
        # legacy single-op timeout contract; tests rely on it.
        first_err = next(iter(op_errors.values()), "timeout_waiting_video")
        return (
            {
                "raw_dispatch": dispatch,
                "last_poll": last_poll,
                "operation_names": op_names,
                "done": done_by_name,
                "op_errors": op_errors,
            },
            first_err,
        )

    # ≥1 op succeeded — ingest only the bytes we actually have.
    entries_with_urls = [
        e for e in succeeded_entries if isinstance(e, dict) and e.get("url")
    ]
    if entries_with_urls:
        try:
            media_service.ingest_urls(entries_with_urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest from gen_video response failed")
    for entry in succeeded_entries:
        if not isinstance(entry, dict):
            continue
        encoded = entry.get("encoded_video")
        mid = entry.get("media_id")
        if not isinstance(encoded, str) or not isinstance(mid, str):
            continue
        try:
            import base64 as _b64

            media_service.ingest_inline_bytes(
                mid,
                _b64.b64decode(encoded, validate=False),
                kind="video",
                mime="video/mp4",
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "inline ingest from workflow-mode poll failed for %s",
                mid,
            )
    partial_error: Optional[str] = None
    if op_errors:
        # De-dup distinct error codes for a compact one-line summary
        # (e.g. "1/4 variants blocked: PUBLIC_ERROR_UNSAFE_GENERATION").
        unique_errs = sorted({err for err in op_errors.values()})
        partial_error = (
            f"{len(op_errors)}/{total} variants blocked: {', '.join(unique_errs)}"
        )

    return (
        {
            "raw_dispatch": dispatch,
            "last_poll": last_poll,
            "operation_names": op_names,
            "media_ids": positional_ids,
            "media_entries": succeeded_entries,
            "op_errors": op_errors,
            "slot_errors": slot_errors,
            "partial_error": partial_error,
        },
        None,
    )


async def _handle_edit_image(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.flow_sdk import is_valid_project_id

    prompt = params.get("prompt")
    project_id = params.get("project_id")
    source_media_id = params.get("source_media_id") or params.get("sourceMediaId")
    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "missing_prompt"
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"
    if not isinstance(source_media_id, str) or not source_media_id.strip():
        return {}, "missing_source_media_id"
    aspect = params.get("aspect_ratio") or "IMAGE_ASPECT_RATIO_LANDSCAPE"
    # Tier resolution — see _handle_gen_image for rationale. Fail loud,
    # no silent fallback to Pro.
    tier = params.get("paygate_tier") or flow_client.paygate_tier
    if tier is None:
        return {}, "paygate_tier_unknown"
    raw_refs = params.get("ref_media_ids")
    ref_ids: Optional[list[str]] = None
    if isinstance(raw_refs, list):
        cleaned = [m for m in raw_refs if isinstance(m, str) and m]
        ref_ids = cleaned or None
    image_model = params.get("image_model")
    if not isinstance(image_model, str) or not image_model.strip():
        image_model = None

    resp = await get_flow_sdk().edit_image(
        prompt=prompt.strip(),
        project_id=project_id,
        source_media_id=source_media_id.strip(),
        ref_media_ids=ref_ids,
        aspect_ratio=aspect,
        paygate_tier=tier,
        image_model=image_model,
    )
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    entries_with_urls = [
        e for e in (resp.get("media_entries") or []) if isinstance(e, dict) and e.get("url")
    ]
    if entries_with_urls:
        try:
            media_service.ingest_urls(entries_with_urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest from edit_image response failed")
    return resp, None



# ── Omni Flash r2v ────────────────────────────────────────────────────────
# Variable-duration video model with a distinct endpoint + body shape from
# Veo i2v. See agent/flowboard/services/flow_sdk.py::gen_video_omni for the
# request assembly. Single operation per request (no multi-source batching
# like Veo's start_media_ids), so the polling logic collapses to a single
# op + first-error-wins, simpler than _handle_gen_video.

async def _handle_gen_video_omni(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.flow_sdk import is_valid_project_id
    from flowboard.services.media_project_sync import (
        MediaSyncError,
        ensure_media_ids_in_project,
    )

    prompt = params.get("prompt")
    project_id = params.get("project_id")
    raw_refs = params.get("ref_media_ids")
    if not isinstance(raw_refs, list):
        # Also accept the legacy single-source field for symmetry with
        # Veo's start_media_id, so the same upstream-walk on the frontend
        # works without a special-case.
        raw_refs = (
            [params.get("start_media_id")]
            if isinstance(params.get("start_media_id"), str)
            else []
        )
    ref_media_ids = [m for m in raw_refs if isinstance(m, str) and m.strip()]
    duration_s = params.get("duration_s")
    if isinstance(duration_s, str) and duration_s.strip().isdigit():
        duration_s = int(duration_s.strip())

    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "missing_prompt"
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"
    if not ref_media_ids:
        return {}, "missing_ref_media_ids"
    if not isinstance(duration_s, int) or duration_s not in (4, 6, 8, 10):
        return {}, "invalid_duration_s"
    aspect = params.get("aspect_ratio") or "VIDEO_ASPECT_RATIO_PORTRAIT"
    tier = params.get("paygate_tier") or flow_client.paygate_tier
    if tier is None:
        return {}, "paygate_tier_unknown"

    # ── Cross-project ref sync ────────────────────────────────────────
    # Flow scopes mediaIds to the project they were uploaded in. When
    # the user references media generated under another board's project
    # (the cross-board Reference library case), Flow returns 404 because
    # the asset is unknown in this project. Re-upload bytes from the
    # local cache and substitute the project-local id before dispatch.
    # First sync hits the Flow upload endpoint per ref; subsequent
    # syncs use the MediaProjectMapping cache and are free.
    rid = params.get("__request_id")
    handler_started = time.perf_counter()
    dispatch_attempt_timings: list[dict[str, object]] = []
    timings: dict[str, object] = {
        "ref_count": len(ref_media_ids),
        "dispatch_attempts_s": dispatch_attempt_timings,
    }

    def _final_timings() -> dict[str, object]:
        return {
            **timings,
            "total_handler_s": round(
                time.perf_counter() - handler_started,
                3,
            ),
        }

    _set_request_progress(
        rid,
        "syncing_references",
        ref_count=len(ref_media_ids),
    )
    sync_started = time.perf_counter()
    try:
        synced_refs, sync_failures = await ensure_media_ids_in_project(
            ref_media_ids, project_id
        )
    except MediaSyncError as exc:
        timings["sync_refs_s"] = round(
            time.perf_counter() - sync_started,
            3,
        )
        return {"timings": _final_timings()}, f"sync_failed: {exc}"[:200]
    sync_elapsed_s = round(time.perf_counter() - sync_started, 3)
    timings["sync_refs_s"] = sync_elapsed_s
    timings["synced_ref_count"] = len(synced_refs)
    _set_request_progress(
        rid,
        "references_ready",
        ref_count=len(ref_media_ids),
        synced_ref_count=len(synced_refs),
        sync_refs_s=sync_elapsed_s,
    )
    if not synced_refs:
        # Every ref failed to sync — surface the first reason.
        first = sync_failures[0][1] if sync_failures else "no_refs_synced"
        return (
            {"sync_failures": sync_failures, "timings": _final_timings()},
            f"sync_failed: {first}"[:200],
        )
    if sync_failures:
        # Partial sync — log; proceed with the refs that worked.
        logger.warning(
            "gen_video_omni: %d ref(s) failed to sync, proceeding with %d",
            len(sync_failures), len(synced_refs),
        )

    sdk = get_flow_sdk()
    attempt_summaries: list[dict] = []
    dispatch_summaries: list[dict] = []
    last_failure_result: dict = {}
    last_failure = "unknown_video_generation_error"

    for generation_attempt in range(1, OMNI_GENERATION_MAX_ATTEMPTS + 1):
        if _is_request_canceled(rid):
            return (
                {
                    "canceled": True,
                    "generation_attempt": generation_attempt,
                    "attempts": attempt_summaries,
                    "dispatch_attempts": dispatch_summaries,
                    "timings": _final_timings(),
                },
                "canceled",
            )

        dispatch: dict = {}
        dispatch_failure = "unknown_dispatch_error"
        for dispatch_attempt in range(1, OMNI_DISPATCH_MAX_ATTEMPTS + 1):
            if _is_request_canceled(rid):
                return (
                    {
                        "canceled": True,
                        "generation_attempt": generation_attempt,
                        "attempts": attempt_summaries,
                        "dispatch_attempts": dispatch_summaries,
                        "timings": _final_timings(),
                    },
                    "canceled",
                )
            _set_request_progress(
                rid,
                "submitting_to_flow",
                generation_attempt=generation_attempt,
                dispatch_attempt=dispatch_attempt,
                elapsed_s=round(
                    time.perf_counter() - handler_started,
                    3,
                ),
            )
            dispatch_started = time.perf_counter()
            dispatch = await sdk.gen_video_omni(
                prompt=prompt.strip(),
                project_id=project_id,
                ref_media_ids=synced_refs,
                duration_s=duration_s,
                aspect_ratio=aspect,
                paygate_tier=tier,
            )
            dispatch_elapsed_s = round(
                time.perf_counter() - dispatch_started,
                3,
            )
            dispatch_ops = dispatch.get("operation_names") or []
            _merge_request_result(
                rid,
                raw_dispatch=dispatch,
                generation_attempt=generation_attempt,
                dispatch_attempt=dispatch_attempt,
                dispatch_s=dispatch_elapsed_s,
                operation_names=dispatch_ops,
            )
            dispatch_attempt_timings.append(
                {
                    "generation_attempt": generation_attempt,
                    "dispatch_attempt": dispatch_attempt,
                    "elapsed_s": dispatch_elapsed_s,
                }
            )
            dispatch_error = dispatch.get("error")
            if not dispatch_error and dispatch_ops:
                timings["last_dispatch_s"] = dispatch_elapsed_s
                _set_request_progress(
                    rid,
                    "flow_submitted",
                    generation_attempt=generation_attempt,
                    dispatch_s=dispatch_elapsed_s,
                    operation_names=dispatch_ops,
                    elapsed_s=round(
                        time.perf_counter() - handler_started,
                        3,
                    ),
                )
                break
            dispatch_failure = (
                str(dispatch_error)[:200]
                if dispatch_error
                else "no_operations_returned"
            )
            dispatch_summaries.append(
                {
                    "generation_attempt": generation_attempt,
                    "dispatch_attempt": dispatch_attempt,
                    "error": dispatch_failure,
                }
            )
            if dispatch_attempt < OMNI_DISPATCH_MAX_ATTEMPTS:
                await asyncio.sleep(OMNI_GENERATION_RETRY_DELAY_S)
        else:
            return (
                {
                    "raw_dispatch": dispatch,
                    "generation_attempt": generation_attempt - 1,
                    "attempts": attempt_summaries,
                    "dispatch_attempts": dispatch_summaries,
                    "timings": _final_timings(),
                },
                (
                    "Could not submit video to Google Flow after "
                    f"{OMNI_DISPATCH_MAX_ATTEMPTS} attempts: "
                    f"{dispatch_failure}"
                ),
            )

        if dispatch.get("error"):
            last_failure = str(dispatch["error"])[:200]
            attempt_summaries.append(
                {
                    "attempt": generation_attempt,
                    "error": last_failure,
                    "poll_attempts": 0,
                }
            )
            last_failure_result = {"raw_dispatch": dispatch}
        else:
            op_names = dispatch.get("operation_names") or []
            if not op_names:
                last_failure = "no_operations_returned"
                attempt_summaries.append(
                    {
                        "attempt": generation_attempt,
                        "error": last_failure,
                        "poll_attempts": 0,
                    }
                )
                last_failure_result = {"raw_dispatch": dispatch}
            else:
                workflows = dispatch.get("workflows") or None
                poll_attempts = 0
                last_poll: dict = {}
                done_by_name: dict[str, bool] = {
                    name: False for name in op_names
                }
                entry_by_name: dict[str, dict] = {}
                op_errors: dict[str, str] = {}
                failure_candidates: dict[str, tuple[str, int]] = {}
                poll_started = time.perf_counter()

                while (
                    poll_attempts < VIDEO_POLL_MAX_CYCLES
                    and not all(done_by_name.values())
                ):
                    await asyncio.sleep(VIDEO_POLL_INTERVAL_S)
                    poll_attempts += 1
                    if _is_request_canceled(rid):
                        attempt_summaries.append(
                            {
                                "attempt": generation_attempt,
                                "operation_names": op_names,
                                "error": "canceled",
                                "poll_attempts": poll_attempts,
                            }
                        )
                        return (
                            {
                                "raw_dispatch": dispatch,
                                "last_poll": last_poll,
                                "operation_names": op_names,
                                "done": done_by_name,
                                "canceled": True,
                                "generation_attempt": generation_attempt,
                                "attempts": attempt_summaries,
                                "dispatch_attempts": dispatch_summaries,
                                "timings": _final_timings(),
                            },
                            "canceled",
                        )
                    last_poll = await sdk.check_async(
                        op_names, workflows=workflows
                    )
                    timings["last_poll_attempts"] = poll_attempts
                    timings["last_poll_s"] = round(
                        time.perf_counter() - poll_started,
                        3,
                    )
                    _set_request_progress(
                        rid,
                        "waiting_for_flow_video",
                        generation_attempt=generation_attempt,
                        poll_attempts=poll_attempts,
                        poll_max=VIDEO_POLL_MAX_CYCLES,
                        poll_elapsed_s=timings["last_poll_s"],
                        operation_names=op_names,
                        elapsed_s=round(
                            time.perf_counter() - handler_started,
                            3,
                        ),
                    )
                    if last_poll.get("error"):
                        continue
                    for op in last_poll.get("operations") or []:
                        if not isinstance(op, dict):
                            continue
                        name = op.get("name")
                        if (
                            not isinstance(name, str)
                            or done_by_name.get(name, False)
                        ):
                            continue
                        candidate = op.get("failure_candidate")
                        if isinstance(candidate, str) and candidate:
                            previous, count = failure_candidates.get(
                                name,
                                (candidate, 0),
                            )
                            next_count = (
                                count + 1 if previous == candidate else 1
                            )
                            failure_candidates[name] = (
                                candidate,
                                next_count,
                            )
                            if next_count >= OMNI_FAILURE_CONFIRM_CYCLES:
                                done_by_name[name] = True
                                op_errors[name] = (
                                    "Flow video failed before media was "
                                    f"produced: {candidate}"
                            )
                            continue
                        failure_candidates.pop(name, None)
                        err = op.get("error")
                        if isinstance(err, str) and err:
                            done_by_name[name] = True
                            op_errors[name] = err
                            continue
                        if op.get("done"):
                            done_by_name[name] = True
                            for e in op.get("media_entries") or []:
                                if (
                                    isinstance(e, dict)
                                    and e.get("media_id")
                                ):
                                    entry_by_name[name] = e
                                    break

                for name in op_names:
                    if (
                        not done_by_name.get(name)
                        and name not in op_errors
                    ):
                        op_errors[name] = "timeout_waiting_video"

                positional_ids: list[Optional[str]] = []
                slot_errors: list[Optional[str]] = []
                succeeded_entries: list[dict] = []
                for name in op_names:
                    entry = entry_by_name.get(name)
                    if (
                        isinstance(entry, dict)
                        and isinstance(entry.get("media_id"), str)
                    ):
                        positional_ids.append(entry["media_id"])
                        succeeded_entries.append(entry)
                        slot_errors.append(None)
                    else:
                        positional_ids.append(None)
                        slot_errors.append(op_errors.get(name))

                if any(positional_ids):
                    attempt_summaries.append(
                        {
                            "attempt": generation_attempt,
                            "operation_names": op_names,
                            "error": None,
                            "poll_attempts": poll_attempts,
                        }
                    )
                    entries_with_urls = [
                        entry
                        for entry in succeeded_entries
                        if isinstance(entry, dict) and entry.get("url")
                    ]
                    if entries_with_urls:
                        try:
                            media_service.ingest_urls(entries_with_urls)
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "auto-ingest from gen_video_omni "
                                "response failed"
                            )
                    for entry in succeeded_entries:
                        if not isinstance(entry, dict):
                            continue
                        encoded = entry.get("encoded_video")
                        mid = entry.get("media_id")
                        if not isinstance(encoded, str) or not isinstance(mid, str):
                            continue
                        try:
                            import base64 as _b64

                            media_service.ingest_inline_bytes(
                                mid,
                                _b64.b64decode(encoded, validate=False),
                                kind="video",
                                mime="video/mp4",
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "inline ingest from omni workflow poll "
                                "failed for %s",
                                mid,
                            )
                    return (
                        {
                            "raw_dispatch": dispatch,
                            "last_poll": last_poll,
                            "operation_names": op_names,
                            "media_ids": positional_ids,
                            "media_entries": succeeded_entries,
                            "op_errors": op_errors,
                            "slot_errors": slot_errors,
                            "duration_s": duration_s,
                            "generation_attempt": generation_attempt,
                            "attempts": attempt_summaries,
                            "dispatch_attempts": dispatch_summaries,
                            "timings": _final_timings(),
                        },
                        None,
                    )

                last_failure = next(
                    iter(op_errors.values()),
                    "timeout_waiting_video",
                )
                attempt_summaries.append(
                    {
                        "attempt": generation_attempt,
                        "operation_names": op_names,
                        "error": last_failure,
                        "poll_attempts": poll_attempts,
                    }
                )
                last_failure_result = {
                    "raw_dispatch": dispatch,
                    "last_poll": last_poll,
                    "operation_names": op_names,
                    "done": done_by_name,
                    "op_errors": op_errors,
                }
                if op_errors and all(
                    err == "timeout_waiting_video"
                    for err in op_errors.values()
                ):
                    last_failure_result.update(
                        {
                            "generation_attempt": generation_attempt,
                            "attempts": attempt_summaries,
                            "dispatch_attempts": dispatch_summaries,
                            "timings": _final_timings(),
                        }
                    )
                    return last_failure_result, "timeout_waiting_video"

        if generation_attempt < OMNI_GENERATION_MAX_ATTEMPTS:
            await asyncio.sleep(OMNI_GENERATION_RETRY_DELAY_S)

    last_failure_result.update(
        {
            "generation_attempt": OMNI_GENERATION_MAX_ATTEMPTS,
            "attempts": attempt_summaries,
            "dispatch_attempts": dispatch_summaries,
            "timings": _final_timings(),
        }
    )
    return (
        last_failure_result,
        (
            "Flow video failed after "
            f"{OMNI_GENERATION_MAX_ATTEMPTS} "
            f"{'attempt' if OMNI_GENERATION_MAX_ATTEMPTS == 1 else 'attempts'}: "
            f"{last_failure}"
        ),
    )


async def _handle_compose_video(
    params: dict,
) -> tuple[dict, Optional[str]]:
    import math
    import uuid
    from pathlib import Path

    from sqlmodel import select

    from flowboard.db.models import Asset, Edge, Node
    from flowboard.services.video_composer import (
        ComposerCanceled,
        compose_video,
    )

    node_id = params.get("__node_id")
    request_id = params.get("__request_id")
    if not isinstance(node_id, int):
        return {}, "missing_node_id"

    aspect_ratio = params.get("aspect_ratio", "9:16")
    audio_mode = params.get("audio_mode", "original")
    audio_media_id = params.get("audio_media_id")
    raw_order = params.get("video_order")
    video_order = [
        item for item in raw_order or [] if isinstance(item, str) and item
    ]
    try:
        original_volume = float(params.get("original_volume", 1.0))
        music_volume = float(params.get("music_volume", 0.2))
    except (TypeError, ValueError):
        return {}, "invalid_audio_volume"
    if not math.isfinite(original_volume) or not math.isfinite(music_volume):
        return {}, "invalid_audio_volume"

    with get_session() as session:
        node = session.get(Node, node_id)
        if node is None:
            return {}, "node_not_found"
        if node.type != "video_composer":
            return {}, "node_must_be_video_composer"

        edges = session.exec(
            select(Edge).where(Edge.target_id == node_id)
        ).all()
        source_ids = {edge.source_id for edge in edges}
        source_nodes = (
            session.exec(select(Node).where(Node.id.in_(source_ids))).all()
            if source_ids
            else []
        )
        source_nodes = [source for source in source_nodes if source.type == "video"]
        order_index = {value: index for index, value in enumerate(video_order)}
        source_nodes.sort(
            key=lambda source: (
                order_index.get(str(source.id), len(order_index)),
                source.x,
                source.id or 0,
            )
        )
        if not source_nodes:
            return {}, "at_least_one_connected_video_required"

        clip_media: list[tuple[str, str]] = []
        for source in source_nodes:
            media_id = (source.data or {}).get("mediaId")
            if isinstance(media_id, str) and media_id:
                clip_media.append((str(source.id), media_id))
        if not clip_media:
            return {}, "at_least_one_generated_video_required"
        if len(clip_media) == 1 and audio_mode not in {"mix", "music"}:
            return {}, "single_video_requires_background_audio"

        node.status = "running"
        node.data = {
            **dict(node.data or {}),
            "error": None,
            "videoOrder": [source_id for source_id, _ in clip_media],
            "assemblyProgress": 5,
            "assemblyStage": "loading_media",
        }
        session.add(node)
        session.commit()

    clip_paths: list[Path] = []
    for _source_id, media_id in clip_media:
        cached = media_service.cached_path(media_id)
        if cached is None:
            await media_service.fetch_and_cache(media_id)
            cached = media_service.cached_path(media_id)
        if cached is None:
            error = f"video_not_available:{media_id}"
            _set_composer_node_error(node_id, error)
            return {}, error
        clip_paths.append(cached)

    audio_path: Optional[Path] = None
    if isinstance(audio_media_id, str) and audio_media_id:
        audio_path = media_service.cached_path(audio_media_id)
        if audio_path is None:
            error = "background_audio_not_available"
            _set_composer_node_error(node_id, error)
            return {}, error
    elif audio_mode in {"mix", "music"}:
        error = "background_audio_required"
        _set_composer_node_error(node_id, error)
        return {}, error

    output_media_id = str(uuid.uuid4())
    output_path = media_service.MEDIA_CACHE_DIR / f"{output_media_id}.mp4"
    try:
        duration = await compose_video(
            clip_paths,
            output_path=output_path,
            aspect_ratio=aspect_ratio,
            audio_path=audio_path,
            audio_mode=audio_mode,
            original_volume=original_volume,
            music_volume=music_volume,
            cancel_check=lambda: _is_request_canceled(request_id),
            progress_callback=lambda percent, stage: (
                _set_composer_node_progress(node_id, percent, stage)
            ),
        )
    except ComposerCanceled:
        output_path.unlink(missing_ok=True)
        _set_composer_node_idle(node_id)
        return {"canceled": True}, "canceled"
    except (RuntimeError, ValueError) as exc:
        output_path.unlink(missing_ok=True)
        logger.exception(
            "video composer failed for node_id=%s at stage=%s",
            node_id,
            _get_composer_node_stage(node_id),
        )
        detail = str(exc).strip()
        error = (
            detail[:500]
            if detail
            else f"{type(exc).__name__}: FFmpeg composition failed"
        )
        _set_composer_node_error(node_id, error)
        return {}, error

    rendered_at = datetime.now(timezone.utc).isoformat()
    with get_session() as session:
        node = session.get(Node, node_id)
        if node is None:
            output_path.unlink(missing_ok=True)
            return {}, "node_deleted_during_composition"
        session.add(
            Asset(
                uuid_media_id=output_media_id,
                kind="video",
                mime="video/mp4",
                local_path=str(output_path),
                node_id=node_id,
            )
        )
        node.status = "done"
        node.data = {
            **dict(node.data or {}),
            "mediaId": output_media_id,
            "mediaIds": [output_media_id],
            "variantCount": 1,
            "videoOrder": [source_id for source_id, _ in clip_media],
            "audioMediaId": audio_media_id,
            "audioMode": audio_mode,
            "originalVolume": original_volume,
            "musicVolume": music_volume,
            "aspectRatio": aspect_ratio,
            "durationS": duration,
            "renderedAt": rendered_at,
            "assemblyProgress": 100,
            "assemblyStage": "completed",
            "error": None,
        }
        session.add(node)
        session.commit()

    return (
        {
            "media_ids": [output_media_id],
            "clip_count": len(clip_paths),
            "duration_s": duration,
            "aspect_ratio": aspect_ratio,
            "audio_mode": audio_mode,
        },
        None,
    )


def _set_composer_node_error(node_id: int, error: str) -> None:
    from flowboard.db.models import Node

    with get_session() as session:
        node = session.get(Node, node_id)
        if node is None:
            return
        safe_error = error.strip() or "Video composition failed without details"
        node.status = "error"
        node.data = {
            **dict(node.data or {}),
            "assemblyStage": "failed",
            "error": safe_error,
        }
        session.add(node)
        session.commit()


def _set_composer_node_idle(node_id: int) -> None:
    from flowboard.db.models import Node

    with get_session() as session:
        node = session.get(Node, node_id)
        if node is None:
            return
        node.status = "idle"
        node.data = {
            **dict(node.data or {}),
            "assemblyProgress": 0,
            "assemblyStage": "idle",
            "error": None,
        }
        session.add(node)
        session.commit()


def _get_composer_node_stage(node_id: int) -> str:
    from flowboard.db.models import Node

    with get_session() as session:
        node = session.get(Node, node_id)
        if node is None:
            return "unknown"
        return str((node.data or {}).get("assemblyStage") or "unknown")


def _set_composer_node_progress(
    node_id: int,
    percent: int,
    stage: str,
) -> None:
    from flowboard.db.models import Node

    with get_session() as session:
        node = session.get(Node, node_id)
        if node is None or node.status not in {"queued", "running"}:
            return
        node.data = {
            **dict(node.data or {}),
            "assemblyProgress": max(0, min(99, int(percent))),
            "assemblyStage": stage,
        }
        session.add(node)
        session.commit()


_DEFAULT_HANDLERS: dict[str, Handler] = {
    "proxy": _handle_proxy,
    "create_project": _handle_create_project,
    "gen_image": _handle_gen_image,
    "gen_video": _handle_gen_video,
    "gen_video_omni": _handle_gen_video_omni,
    "compose_video": _handle_compose_video,
    "edit_image": _handle_edit_image,
}


class WorkerController:
    """Single-consumer async queue worker."""

    def __init__(self, handlers: Optional[dict[str, Handler]] = None) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._handlers = dict(handlers or _DEFAULT_HANDLERS)
        self._shutdown = asyncio.Event()
        self._active = 0
        self._started_at: Optional[float] = None

    # ── enqueue ────────────────────────────────────────────────────────────
    def enqueue(self, request_id: int) -> None:
        self._queue.put_nowait(request_id)

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._started_at = time.time()
        logger.info("worker started")
        while not self._shutdown.is_set():
            try:
                rid = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            await self._process_one(rid)

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def drain(self) -> None:
        # Wait for any in-flight task to finish.
        while self._active > 0:
            await asyncio.sleep(0.05)

    @property
    def active_count(self) -> int:
        return self._active

    @property
    def uptime_s(self) -> Optional[float]:
        if self._started_at is None:
            return None
        return time.time() - self._started_at

    # ── execution ──────────────────────────────────────────────────────────
    async def _process_one(self, rid: int) -> None:
        self._active += 1
        try:
            with get_session() as s:
                req = s.get(Request, rid)
                if req is None:
                    logger.warning("worker: request %s not found", rid)
                    return
                # Drift guard — the row might have been canceled (or
                # otherwise transitioned out of queued) between enqueue
                # and pop. The cancel endpoint mutates the DB row only;
                # it can't yank the rid back off the in-memory queue, so
                # we re-check here and bail without flipping status.
                if req.status != "queued":
                    logger.info(
                        "worker: skipping rid=%s (status=%s)", rid, req.status
                    )
                    return
                handler = self._handlers.get(req.type)
                if handler is None:
                    req.status = "failed"
                    req.error = f"unknown_request_type:{req.type}"
                    req.finished_at = datetime.now(timezone.utc)
                    s.add(req)
                    s.commit()
                    return

                req.status = "running"
                s.add(req)
                s.commit()
                params = dict(req.params or {})
                # Enrich with the request's node_id so handlers that need
                # to look up Node.data don't depend on the caller copying
                # it into params explicitly. Underscore prefix avoids
                # colliding with handler-defined fields.
                if req.node_id is not None and "__node_id" not in params:
                    params["__node_id"] = req.node_id
                # Long-running handlers re-check this rid between polls
                # to honor user-initiated cancels.
                params["__request_id"] = rid

            # Release the session during the possibly-long RPC.
            result, err = await handler(params)

            with get_session() as s:
                req = s.get(Request, rid)
                if req is None:
                    return
                # Don't overwrite a canceled row with a late-arriving
                # done/failed stamp. The cancel endpoint already set
                # status='canceled' and finished_at; we only persist the
                # partial result for debugging visibility.
                if req.status == "canceled":
                    if isinstance(result, dict):
                        req.result = result
                        s.add(req)
                        s.commit()
                    return
                req.result = result if isinstance(result, dict) else {"value": result}
                req.finished_at = datetime.now(timezone.utc)
                if err is not None:
                    error_text = str(err).strip() or (
                        "request_failed_without_error_details"
                    )
                    # Video-poll exhaustion gets its own status so the UI
                    # can render "TIMEOUT" instead of a generic failure.
                    req.status = (
                        "timeout"
                        if error_text == "timeout_waiting_video"
                        else "failed"
                    )
                    req.error = error_text
                else:
                    req.status = "done"
                    req.error = None
                s.add(req)
                s.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("worker exception on rid=%s", rid)
            try:
                with get_session() as s:
                    req = s.get(Request, rid)
                    if req is not None and req.status != "canceled":
                        req.status = "failed"
                        req.error = str(exc)[:500]
                        req.finished_at = datetime.now(timezone.utc)
                        s.add(req)
                        s.commit()
            except Exception:  # noqa: BLE001
                logger.exception("worker: failed to record failure for rid=%s", rid)
        finally:
            self._active -= 1


_worker: Optional[WorkerController] = None


def get_worker() -> WorkerController:
    global _worker
    if _worker is None:
        _worker = WorkerController()
    return _worker
