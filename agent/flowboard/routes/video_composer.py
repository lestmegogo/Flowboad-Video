"""Audio upload and capability endpoints for the local Video Composer."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from flowboard.db import get_session
from flowboard.db.models import Asset, Node
from flowboard.services import media as media_service
from flowboard.services.video_composer import ffmpeg_available

router = APIRouter(prefix="/api/video-composer", tags=["video-composer"])

MAX_AUDIO_BYTES = 50 * 1024 * 1024
_ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
_MIME_BY_EXTENSION = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
}


def _looks_like_audio(raw: bytes, extension: str) -> bool:
    if extension == ".mp3":
        return raw.startswith(b"ID3") or (
            len(raw) >= 2 and raw[0] == 0xFF and raw[1] & 0xE0 == 0xE0
        )
    if extension == ".wav":
        return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    if extension == ".flac":
        return raw.startswith(b"fLaC")
    if extension == ".ogg":
        return raw.startswith(b"OggS")
    if extension == ".m4a":
        return len(raw) >= 12 and raw[4:8] == b"ftyp"
    if extension == ".aac":
        return len(raw) >= 2 and raw[0] == 0xFF and raw[1] & 0xF0 == 0xF0
    return False


@router.get("/capabilities")
def composer_capabilities() -> dict:
    return {
        "ffmpeg_available": ffmpeg_available(),
        "max_clips": 20,
        "max_audio_bytes": MAX_AUDIO_BYTES,
    }


@router.post("/upload-audio")
async def upload_audio(
    file: UploadFile = File(...),
    node_id: Optional[int] = Form(default=None),
) -> dict:
    filename = file.filename or "audio"
    extension = Path(filename).suffix.lower()
    if extension not in _ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="unsupported audio format")

    raw = await file.read(MAX_AUDIO_BYTES + 1)
    if not raw:
        raise HTTPException(status_code=400, detail="empty audio file")
    if len(raw) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio file exceeds 50 MB")
    if not _looks_like_audio(raw, extension):
        raise HTTPException(
            status_code=415,
            detail="file contents do not match the selected audio format",
        )

    if node_id is not None:
        with get_session() as session:
            node = session.get(Node, node_id)
            if node is None:
                raise HTTPException(status_code=404, detail="node not found")
            if node.type != "video_composer":
                raise HTTPException(
                    status_code=400,
                    detail="audio can only be attached to a Video Composer node",
                )

    media_id = str(uuid.uuid4())
    path = media_service.MEDIA_CACHE_DIR / f"{media_id}{extension}"
    try:
        path.write_bytes(raw)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="failed to cache audio") from exc

    mime = _MIME_BY_EXTENSION[extension]
    try:
        with get_session() as session:
            session.add(
                Asset(
                    uuid_media_id=media_id,
                    kind="audio",
                    mime=mime,
                    local_path=str(path),
                    node_id=node_id,
                )
            )
            session.commit()
    except Exception:
        path.unlink(missing_ok=True)
        raise

    return {
        "media_id": media_id,
        "filename": filename,
        "mime": mime,
        "size": len(raw),
    }
