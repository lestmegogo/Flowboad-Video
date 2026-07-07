import json
import subprocess
from array import array
from pathlib import Path

import pytest


def _make_board(client):
    return client.post("/api/boards", json={"name": "Composer"}).json()


def _make_node(client, board_id, node_type, *, x=0, data=None):
    response = client.post(
        "/api/nodes",
        json={
            "board_id": board_id,
            "type": node_type,
            "x": x,
            "data": data or {},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _connect(client, board_id, source_id, target_id):
    response = client.post(
        "/api/edges",
        json={
            "board_id": board_id,
            "source_id": source_id,
            "target_id": target_id,
        },
    )
    assert response.status_code == 200, response.text


def test_upload_audio_for_composer(client):
    from flowboard.services import media as media_service

    board = _make_board(client)
    composer = _make_node(client, board["id"], "video_composer")
    wav = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 32

    response = client.post(
        "/api/video-composer/upload-audio",
        data={"node_id": str(composer["id"])},
        files={"file": ("music.wav", wav, "audio/wav")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filename"] == "music.wav"
    assert body["mime"] == "audio/wav"
    cached = media_service.cached_path(body["media_id"])
    assert cached is not None
    assert cached.read_bytes() == wav


def test_upload_audio_rejects_fake_file(client):
    board = _make_board(client)
    composer = _make_node(client, board["id"], "video_composer")

    response = client.post(
        "/api/video-composer/upload-audio",
        data={"node_id": str(composer["id"])},
        files={"file": ("music.mp3", b"not audio", "audio/mpeg")},
    )

    assert response.status_code == 415


def test_deleting_composer_cancels_queued_composition(client):
    from flowboard.db import get_session
    from flowboard.db.models import Request

    board = _make_board(client)
    composer = _make_node(client, board["id"], "video_composer")
    with get_session() as session:
        request = Request(
            node_id=composer["id"],
            type="compose_video",
            params={},
            status="queued",
        )
        session.add(request)
        session.commit()
        session.refresh(request)
        request_id = request.id

    response = client.delete(f"/api/nodes/{composer['id']}")

    assert response.status_code == 200
    saved_request = client.get(f"/api/requests/{request_id}").json()
    assert saved_request["status"] == "canceled"
    assert saved_request["node_id"] is None


@pytest.mark.asyncio
async def test_compose_video_worker_uses_saved_order(monkeypatch, client):
    from flowboard.services import media as media_service
    from flowboard.services import video_composer as composer_service
    from flowboard.worker import processor

    board = _make_board(client)
    media_a = "11111111-1111-1111-1111-111111111111"
    media_b = "22222222-2222-2222-2222-222222222222"
    first = _make_node(
        client,
        board["id"],
        "video",
        x=10,
        data={"mediaId": media_a},
    )
    second = _make_node(
        client,
        board["id"],
        "video",
        x=20,
        data={"mediaId": media_b},
    )
    target = _make_node(client, board["id"], "video_composer")
    _connect(client, board["id"], first["id"], target["id"])
    _connect(client, board["id"], second["id"], target["id"])
    assert media_service.ingest_inline_bytes(media_a, b"video-a")
    assert media_service.ingest_inline_bytes(media_b, b"video-b")

    captured = {}

    async def _fake_compose(paths, **kwargs):
        captured["paths"] = [Path(path).stem for path in paths]
        captured.update(kwargs)
        kwargs["output_path"].write_bytes(b"composed-video")
        return 12.5

    monkeypatch.setattr(composer_service, "compose_video", _fake_compose)

    result, error = await processor._handle_compose_video(
        {
            "__node_id": target["id"],
            "video_order": [str(second["id"]), str(first["id"])],
            "aspect_ratio": "9:16",
            "audio_mode": "muted",
            "original_volume": 1,
            "music_volume": 0.2,
        }
    )

    assert error is None
    assert result["clip_count"] == 2
    assert result["duration_s"] == 12.5
    assert captured["paths"] == [media_b, media_a]
    node = client.get(f"/api/boards/{board['id']}").json()["nodes"]
    saved = next(item for item in node if item["id"] == target["id"])
    assert saved["status"] == "done"
    assert saved["data"]["videoOrder"] == [
        str(second["id"]),
        str(first["id"]),
    ]
    assert saved["data"]["audioMode"] == "muted"
    assert media_service.cached_path(result["media_ids"][0]) is not None


@pytest.mark.asyncio
async def test_single_video_requires_background_audio(client):
    from flowboard.worker import processor

    board = _make_board(client)
    source = _make_node(
        client,
        board["id"],
        "video",
        data={"mediaId": "11111111-1111-1111-1111-111111111111"},
    )
    target = _make_node(client, board["id"], "video_composer")
    _connect(client, board["id"], source["id"], target["id"])

    result, error = await processor._handle_compose_video(
        {
            "__node_id": target["id"],
            "aspect_ratio": "9:16",
            "audio_mode": "original",
        }
    )

    assert result == {}
    assert error == "single_video_requires_background_audio"


@pytest.mark.asyncio
async def test_compose_single_video_with_background_audio(monkeypatch, client):
    from flowboard.services import media as media_service
    from flowboard.services import video_composer as composer_service
    from flowboard.worker import processor

    board = _make_board(client)
    video_media_id = "33333333-3333-3333-3333-333333333333"
    audio_media_id = "44444444-4444-4444-4444-444444444444"
    source = _make_node(
        client,
        board["id"],
        "video",
        data={"mediaId": video_media_id},
    )
    target = _make_node(client, board["id"], "video_composer")
    _connect(client, board["id"], source["id"], target["id"])
    assert media_service.ingest_inline_bytes(video_media_id, b"video")
    assert media_service.ingest_inline_bytes(
        audio_media_id,
        b"audio",
        kind="audio",
        mime="audio/mpeg",
    )

    captured = {}

    async def _fake_compose(paths, **kwargs):
        captured["paths"] = list(paths)
        captured.update(kwargs)
        kwargs["progress_callback"](42, "normalizing")
        saved_nodes = client.get(f"/api/boards/{board['id']}").json()["nodes"]
        saved_target = next(
            item for item in saved_nodes if item["id"] == target["id"]
        )
        captured["progress"] = saved_target["data"]["assemblyProgress"]
        captured["stage"] = saved_target["data"]["assemblyStage"]
        kwargs["output_path"].write_bytes(b"video-with-music")
        return 8.0

    monkeypatch.setattr(composer_service, "compose_video", _fake_compose)

    result, error = await processor._handle_compose_video(
        {
            "__node_id": target["id"],
            "aspect_ratio": "9:16",
            "audio_mode": "mix",
            "audio_media_id": audio_media_id,
        }
    )

    assert error is None
    assert result["clip_count"] == 1
    assert result["audio_mode"] == "mix"
    assert len(captured["paths"]) == 1
    assert captured["audio_path"] == media_service.cached_path(audio_media_id)
    assert captured["progress"] == 42
    assert captured["stage"] == "normalizing"


@pytest.mark.asyncio
async def test_compose_failure_never_returns_blank_error(monkeypatch, client):
    from flowboard.services import media as media_service
    from flowboard.services import video_composer as composer_service
    from flowboard.worker import processor

    board = _make_board(client)
    video_media_id = "55555555-5555-5555-5555-555555555555"
    audio_media_id = "66666666-6666-6666-6666-666666666666"
    source = _make_node(
        client,
        board["id"],
        "video",
        data={"mediaId": video_media_id},
    )
    target = _make_node(client, board["id"], "video_composer")
    _connect(client, board["id"], source["id"], target["id"])
    assert media_service.ingest_inline_bytes(video_media_id, b"video")
    assert media_service.ingest_inline_bytes(
        audio_media_id,
        b"audio",
        kind="audio",
        mime="audio/mpeg",
    )

    async def _blank_failure(*_args, **_kwargs):
        raise ValueError

    monkeypatch.setattr(composer_service, "compose_video", _blank_failure)

    result, error = await processor._handle_compose_video(
        {
            "__node_id": target["id"],
            "aspect_ratio": "9:16",
            "audio_mode": "music",
            "audio_media_id": audio_media_id,
        }
    )

    assert result == {}
    assert error == "ValueError: FFmpeg composition failed"
    saved_nodes = client.get(f"/api/boards/{board['id']}").json()["nodes"]
    saved_target = next(item for item in saved_nodes if item["id"] == target["id"])
    assert saved_target["status"] == "error"
    assert saved_target["data"]["error"] == error


@pytest.mark.asyncio
async def test_real_ffmpeg_composition_with_music_loop(tmp_path):
    from flowboard.services.video_composer import (
        ComposerCanceled,
        compose_video,
        ffmpeg_available,
        resolve_ffmpeg,
    )

    if not ffmpeg_available():
        pytest.skip("FFmpeg is not installed")
    ffmpeg, ffprobe = resolve_ffmpeg()
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    music = tmp_path / "music.wav"
    output = tmp_path / "output.mp4"

    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x240:d=0.6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.6",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(clip_a),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=240x320:d=0.6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(clip_b),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=0.25",
            str(music),
        ],
        check=True,
        capture_output=True,
    )

    duration = await compose_video(
        [clip_a, clip_b],
        output_path=output,
        aspect_ratio="9:16",
        audio_path=music,
        audio_mode="mix",
        original_volume=1.0,
        music_volume=0.2,
        cancel_check=lambda: False,
    )

    assert output.stat().st_size > 0
    assert duration >= 1.0
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height",
            "-of",
            "json",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    streams = json.loads(probe.stdout)["streams"]
    video = next(stream for stream in streams if stream["codec_type"] == "video")
    assert (video["width"], video["height"]) == (720, 1280)
    assert any(stream["codec_type"] == "audio" for stream in streams)

    single_output = tmp_path / "single-with-music.mp4"
    progress_events = []
    single_duration = await compose_video(
        [clip_a],
        output_path=single_output,
        aspect_ratio="9:16",
        audio_path=music,
        audio_mode="mix",
        original_volume=1.0,
        music_volume=0.2,
        cancel_check=lambda: False,
        progress_callback=lambda percent, stage: progress_events.append(
            (percent, stage)
        ),
    )
    single_probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            str(single_output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    single_streams = json.loads(single_probe.stdout)["streams"]
    assert single_output.stat().st_size > 0
    assert 0.5 <= single_duration <= 0.9
    assert any(stream["codec_type"] == "audio" for stream in single_streams)
    assert progress_events[0] == (5, "preparing")
    assert progress_events[-1] == (95, "finalizing")
    assert any(stage == "mixing_audio" for _, stage in progress_events)
    assert [percent for percent, _ in progress_events] == sorted(
        percent for percent, _ in progress_events
    )

    music_only_output = tmp_path / "single-music-only.mp4"
    await compose_video(
        [clip_a],
        output_path=music_only_output,
        aspect_ratio="9:16",
        audio_path=music,
        audio_mode="music",
        original_volume=1.0,
        music_volume=1.0,
        cancel_check=lambda: False,
    )
    decoded = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(music_only_output),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-t",
            "0.5",
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    samples = array("h")
    samples.frombytes(decoded.stdout)
    zero_crossings = sum(
        (left < 0) != (right < 0)
        for left, right in zip(samples, samples[1:])
    )
    # Background music is 220 Hz (~220 crossings over 0.5s). The source
    # audio is 440 Hz (~440 crossings), so this catches accidental [0:a].
    assert 150 <= zero_crossings <= 300

    muted_output = tmp_path / "muted.mp4"
    await compose_video(
        [clip_a, clip_b],
        output_path=muted_output,
        aspect_ratio="16:9",
        audio_path=None,
        audio_mode="muted",
        original_volume=1.0,
        music_volume=0.2,
        cancel_check=lambda: False,
    )
    muted_probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height",
            "-of",
            "json",
            str(muted_output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    muted_streams = json.loads(muted_probe.stdout)["streams"]
    muted_video = next(
        stream for stream in muted_streams if stream["codec_type"] == "video"
    )
    assert (muted_video["width"], muted_video["height"]) == (1280, 720)
    assert not any(stream["codec_type"] == "audio" for stream in muted_streams)

    cancel_checks = iter((False, True))
    with pytest.raises(ComposerCanceled):
        await compose_video(
            [clip_a, clip_b],
            output_path=tmp_path / "canceled.mp4",
            aspect_ratio="9:16",
            audio_path=None,
            audio_mode="original",
            original_volume=1.0,
            music_volume=0.2,
            cancel_check=lambda: next(cancel_checks, True),
        )
    assert not (tmp_path / "canceled.mp4").exists()
