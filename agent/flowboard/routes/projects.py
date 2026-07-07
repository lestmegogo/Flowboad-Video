"""Bootstrap a Google Flow project for a local board.

One-to-one: each board gets exactly one `flow_project_id`. The bootstrap is
idempotent — calling POST multiple times returns the same project id without
creating a new one on labs.google.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from flowboard.db import get_session
from flowboard.db.models import Board, BoardFlowProject, Request as FlowRequest
from flowboard.services.flow_sdk import get_flow_sdk, is_valid_project_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/boards", tags=["board-projects"])


@router.get("/{board_id}/project")
def get_board_project(board_id: int):
    with get_session() as s:
        if not s.get(Board, board_id):
            raise HTTPException(404, "board not found")
        row = s.get(BoardFlowProject, board_id)
        if row is None:
            raise HTTPException(404, "no project bound to this board")
        return {"flow_project_id": row.flow_project_id, "created": False}


@router.post("/{board_id}/project")
async def ensure_board_project(board_id: int):
    # Cheap path: DB hit only.
    with get_session() as s:
        board = s.get(Board, board_id)
        if not board:
            raise HTTPException(404, "board not found")
        row = s.get(BoardFlowProject, board_id)
        if row is not None:
            return {"flow_project_id": row.flow_project_id, "created": False}
        board_name = board.name

    # Release the session before the extension round-trip.
    resp = await get_flow_sdk().create_project(title=board_name or "Untitled")
    if resp.get("error"):
        # Surface the extension/TRPC error cleanly to the caller.
        raise HTTPException(
            status_code=502,
            detail={"message": resp["error"], "raw": resp.get("raw")},
        )
    flow_project_id = resp.get("project_id")
    if not isinstance(flow_project_id, str) or not flow_project_id:
        raise HTTPException(
            status_code=502,
            detail={"message": "no project_id in Flow response", "raw": resp.get("raw")},
        )
    # Defense-in-depth: refuse to persist a project_id that would later be
    # rejected by the worker's validator. Keeps the DB clean of anything that
    # could be URL-injected by a future code path.
    if not is_valid_project_id(flow_project_id):
        raise HTTPException(
            status_code=502,
            detail={
                "message": "invalid project_id shape from Flow",
                "raw": resp.get("raw"),
            },
        )

    # Persist. Guard against concurrent callers that may have beaten us to it.
    with get_session() as s:
        existing = s.get(BoardFlowProject, board_id)
        if existing is not None:
            return {"flow_project_id": existing.flow_project_id, "created": False}
        row = BoardFlowProject(board_id=board_id, flow_project_id=flow_project_id)
        s.add(row)
        s.commit()
        s.refresh(row)
        logger.info("bound board %s → flow_project %s", board_id, flow_project_id)
        return {"flow_project_id": row.flow_project_id, "created": True}


@router.get("/debug/poll-media")
async def debug_poll_media(media_id: str, project_id: str):
    from flowboard.services.flow_client import flow_client
    from flowboard.services.flow_sdk import FLOW_API_BASE, _API_HEADERS

    variations = {
        "v1_plain_key": f"{FLOW_API_BASE}/v1/media/{media_id}?key=AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY",
        "v2_tool_key": f"{FLOW_API_BASE}/v1/media/{media_id}?clientContext.tool=PINHOLE&key=AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY",
        "v3_tool_proj_key": f"{FLOW_API_BASE}/v1/media/{media_id}?clientContext.tool=PINHOLE&clientContext.projectId={project_id}&key=AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY",
        "v4_tool_proj_tier_key": f"{FLOW_API_BASE}/v1/media/{media_id}?clientContext.tool=PINHOLE&clientContext.projectId={project_id}&clientContext.userPaygateTier=PAYGATE_TIER_ONE&key=AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY",
        "v5_tool_proj_tier_sess_key": f"{FLOW_API_BASE}/v1/media/{media_id}?clientContext.tool=PINHOLE&clientContext.projectId={project_id}&clientContext.userPaygateTier=PAYGATE_TIER_ONE&clientContext.sessionId=;12345&key=AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY",
    }

    results = {}
    for name, url in variations.items():
        try:
            resp = await flow_client.api_request(
                url=url,
                method="GET",
                headers=dict(_API_HEADERS),
                body=None,
            )
            status = resp.get("status")
            error = resp.get("data", {}).get("error") if isinstance(resp.get("data"), dict) else None
            results[name] = {"url": url, "status": status, "error": error}
        except Exception as e:
            results[name] = {"url": url, "exception": str(e)}

    return {
        "token_len": len(flow_client._flow_key) if flow_client._flow_key else 0,
        "token_present": flow_client._flow_key_present,
        "token_captured_at": flow_client._token_captured_at,
        "results": results,
    }


@router.get("/debug/video-request/{request_id}")
async def debug_video_request(request_id: int):
    from flowboard.services.flow_client import flow_client
    from flowboard.services.flow_sdk import (
        _API_HEADERS,
        _media_get_url,
        _media_redirect_url,
        _project_initial_data_url,
        extract_project_media_states,
    )

    with get_session() as s:
        req = s.get(FlowRequest, request_id)
        if req is None:
            raise HTTPException(404, "request not found")
        params = dict(req.params or {})
        result = dict(req.result or {})
        progress = result.get("progress") if isinstance(result.get("progress"), dict) else {}

    project_id = params.get("project_id")
    operation_names = progress.get("operation_names")
    workflow_id = (
        operation_names[0]
        if isinstance(operation_names, list) and operation_names
        else None
    )
    if not isinstance(project_id, str) or not project_id:
        raise HTTPException(400, "request has no project_id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise HTTPException(400, "request has no workflow id in progress")

    project_resp = await flow_client.trpc_request(
        url=_project_initial_data_url(project_id),
        method="GET",
        headers={},
        body=None,
        timeout=30.0,
    )
    payload = {}
    if isinstance(project_resp.get("data"), dict):
        result_obj = project_resp["data"].get("result")
        if isinstance(result_obj, dict):
            data_obj = result_obj.get("data")
            if isinstance(data_obj, dict) and isinstance(data_obj.get("json"), dict):
                payload = data_obj["json"]
    contents = payload.get("projectContents") if isinstance(payload, dict) else {}
    workflows = contents.get("workflows") if isinstance(contents, dict) else []
    media = contents.get("media") if isinstance(contents, dict) else []
    if not isinstance(workflows, list):
        workflows = []
    if not isinstance(media, list):
        media = []

    workflow = next(
        (item for item in workflows if isinstance(item, dict) and item.get("name") == workflow_id),
        None,
    )
    primary_media_id = None
    if isinstance(workflow, dict) and isinstance(workflow.get("metadata"), dict):
        primary_media_id = workflow["metadata"].get("primaryMediaId")
    media_matches = [
        item
        for item in media
        if isinstance(item, dict)
        and (
            item.get("workflowId") == workflow_id
            or (
                isinstance(primary_media_id, str)
                and item.get("name") == primary_media_id
            )
        )
    ]
    if not isinstance(primary_media_id, str) or not primary_media_id:
        for item in media_matches:
            candidate = item.get("name") if isinstance(item, dict) else None
            if isinstance(candidate, str) and candidate:
                primary_media_id = candidate
                break

    media_states = extract_project_media_states(project_resp)
    media_probe = None
    redirect_probe = None
    if isinstance(primary_media_id, str) and primary_media_id:
        media_probe = await flow_client.api_request(
            url=_media_get_url(primary_media_id),
            method="GET",
            headers=dict(_API_HEADERS),
            body=None,
            timeout=30.0,
        )
        redirect_probe = await flow_client.trpc_request(
            url=_media_redirect_url(primary_media_id),
            method="GET",
            headers={},
            body=None,
            timeout=30.0,
        )

    def summarize_media(item: dict) -> dict:
        media_id = item.get("name")
        return {
            "name": media_id,
            "workflowId": item.get("workflowId"),
            "status": (
                media_states.get(media_id, {}).get("status")
                if isinstance(media_id, str)
                else None
            ),
            "error": (
                media_states.get(media_id, {}).get("error")
                if isinstance(media_id, str)
                else None
            ),
        }

    return {
        "request_id": request_id,
        "request_status": req.status,
        "request_error": req.error,
        "project_id": project_id,
        "ref_media_ids": params.get("ref_media_ids"),
        "workflow_id": workflow_id,
        "project_status": project_resp.get("status"),
        "workflow_count": len(workflows),
        "media_count": len(media),
        "workflow_names": [
            item.get("name") for item in workflows if isinstance(item, dict)
        ],
        "media_names": [
            item.get("name") for item in media if isinstance(item, dict)
        ],
        "workflow_found": workflow is not None,
        "workflow_primary_media_id": primary_media_id,
        "matched_media": [
            summarize_media(item) for item in media_matches if isinstance(item, dict)
        ],
        "media_probe": {
            "status": media_probe.get("status") if isinstance(media_probe, dict) else None,
            "error": media_probe.get("error") if isinstance(media_probe, dict) else None,
        }
        if media_probe is not None
        else None,
        "redirect_probe": {
            "status": redirect_probe.get("status") if isinstance(redirect_probe, dict) else None,
            "error": redirect_probe.get("error") if isinstance(redirect_probe, dict) else None,
            "has_redirect": bool(
                isinstance(redirect_probe, dict)
                and isinstance(redirect_probe.get("data"), dict)
                and redirect_probe["data"].get("redirectUrl")
            ),
        }
        if redirect_probe is not None
        else None,
    }
