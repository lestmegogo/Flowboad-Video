from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from flowboard.db import get_session
from flowboard.db.models import Node, Request
from flowboard.worker.processor import get_worker

router = APIRouter(prefix="/api/requests", tags=["requests"])


class RequestCreate(BaseModel):
    node_id: Optional[int] = None
    type: str = Field(min_length=1, max_length=40)
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("")
def create_request(body: RequestCreate):
    with get_session() as s:
        if body.node_id is not None and not s.get(Node, body.node_id):
            raise HTTPException(404, "node not found")
        if body.node_id is not None and body.type in {"gen_video", "gen_video_omni"}:
            from sqlmodel import select

            stale_video_requests = s.exec(
                select(Request).where(
                    Request.node_id == body.node_id,
                    Request.type.in_(["gen_video", "gen_video_omni"]),
                    Request.status.in_(["queued", "running"]),
                )
            ).all()
            for stale in stale_video_requests:
                stale.status = "canceled"
                stale.error = "superseded_by_new_video_request"
                stale.finished_at = datetime.now(timezone.utc)
                s.add(stale)
        req = Request(
            node_id=body.node_id,
            type=body.type,
            params=dict(body.params),
            status="queued",
        )
        s.add(req)
        s.commit()
        s.refresh(req)
        rid = req.id
        row = req

    assert rid is not None
    get_worker().enqueue(rid)
    return row


@router.get("/{request_id}")
def get_request(request_id: int):
    with get_session() as s:
        req = s.get(Request, request_id)
        if req is None:
            raise HTTPException(404, "request not found")
        return req


@router.post("/{request_id}/cancel")
def cancel_request(request_id: int):
    """Cancel a ``queued`` or ``running`` request.

    Cancellation is cooperative on the running side: we can't abort an
    in-flight HTTP call to Flow, but the long-running handlers (notably
    ``_handle_gen_video``) re-read the row between polls and bail out
    when they see ``status == 'canceled'``. The worker's ``_process_one``
    also re-checks the row before its final stamp, so the canceled
    status is never overwritten by a late-arriving result.
    """
    with get_session() as s:
        req = s.get(Request, request_id)
        if req is None:
            raise HTTPException(404, "request not found")
        if req.status not in ("queued", "running"):
            raise HTTPException(
                409,
                f"only queued or running requests can be canceled (status={req.status})",
            )
        req.status = "canceled"
        req.error = "canceled"
        req.finished_at = datetime.now(timezone.utc)
        s.add(req)
        s.commit()
        s.refresh(req)
        return req
