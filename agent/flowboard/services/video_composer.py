"""Local FFmpeg video composition.

The composer deliberately stays independent from Google Flow: upstream clips
are read from the local media cache, normalized to one stream shape, joined,
and optionally mixed with locally uploaded music.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from flowboard.config import STORAGE_DIR

CancelCheck = Callable[[], bool]
ProgressCallback = Callable[[int, str], None]

COMPOSER_TMP_DIR = STORAGE_DIR / "tmp" / "video-composer"
COMPOSER_TMP_DIR.mkdir(parents=True, exist_ok=True)


class ComposerCanceled(RuntimeError):
    pass


def _report_progress(
    callback: Optional[ProgressCallback],
    percent: int,
    stage: str,
) -> None:
    if callback is not None:
        callback(max(0, min(100, percent)), stage)


def resolve_ffmpeg() -> tuple[str, str]:
    """Return (ffmpeg, ffprobe), preferring the repo-local Windows bundle."""
    configured = os.getenv("FLOWBOARD_FFMPEG")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    repo_root = Path(__file__).resolve().parents[3]
    candidates.append(repo_root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe")

    ffmpeg: Optional[str] = None
    for candidate in candidates:
        if candidate.is_file():
            ffmpeg = str(candidate)
            break
    if ffmpeg is None:
        ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "FFmpeg not found. Run scripts/install-ffmpeg.ps1 or set "
            "FLOWBOARD_FFMPEG, then restart the backend."
        )

    ffmpeg_path = Path(ffmpeg)
    sibling_name = "ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe"
    sibling = ffmpeg_path.with_name(sibling_name)
    ffprobe = str(sibling) if sibling.is_file() else shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found next to FFmpeg or in PATH.")
    return ffmpeg, ffprobe


def ffmpeg_available() -> bool:
    try:
        resolve_ffmpeg()
        return True
    except RuntimeError:
        return False


async def _run_command(
    cmd: list[str],
    *,
    cancel_check: CancelCheck,
    error_label: str,
    timeout_s: float = 1800.0,
) -> bytes:
    return await asyncio.to_thread(
        _run_command_sync,
        cmd,
        cancel_check=cancel_check,
        error_label=error_label,
        timeout_s=timeout_s,
    )


def _run_command_sync(
    cmd: list[str],
    *,
    cancel_check: CancelCheck,
    error_label: str,
    timeout_s: float,
) -> bytes:
    """Run FFmpeg without relying on asyncio's Windows subprocess support."""
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"{error_label}: executable not found") from exc

    started = time.monotonic()
    while True:
        if cancel_check():
            process.terminate()
            try:
                process.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            raise ComposerCanceled("canceled")
        if time.monotonic() - started > timeout_s:
            process.kill()
            process.communicate()
            raise RuntimeError(f"{error_label}: timed out")
        try:
            stdout, stderr = process.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            continue

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        detail = detail[-1600:] if detail else f"exit code {process.returncode}"
        raise RuntimeError(f"{error_label}: {detail}")
    return stdout


async def _probe(
    path: Path,
    ffprobe: str,
    cancel_check: CancelCheck,
) -> dict:
    raw = await _run_command(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            str(path),
        ],
        cancel_check=cancel_check,
        error_label=f"Could not inspect {path.name}",
        timeout_s=60.0,
    )
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"ffprobe returned invalid data for {path.name}") from exc


def _has_audio(probe: dict) -> bool:
    return any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio"
        for stream in probe.get("streams") or []
    )


def _duration(probe: dict) -> float:
    try:
        value = float((probe.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        raise RuntimeError("Composed video has no measurable duration")
    return value


async def compose_video(
    clip_paths: list[Path],
    *,
    output_path: Path,
    aspect_ratio: str,
    audio_path: Optional[Path],
    audio_mode: str,
    original_volume: float,
    music_volume: float,
    cancel_check: CancelCheck,
    progress_callback: Optional[ProgressCallback] = None,
) -> float:
    """Compose clips and return the output duration in seconds."""
    if not clip_paths:
        raise ValueError("At least one video clip is required")
    if len(clip_paths) > 20:
        raise ValueError("At most twenty video clips are supported")
    if aspect_ratio not in {"9:16", "16:9"}:
        raise ValueError("aspect_ratio must be 9:16 or 16:9")
    if audio_mode not in {"original", "muted", "mix", "music"}:
        raise ValueError("invalid audio_mode")
    if audio_mode in {"mix", "music"} and audio_path is None:
        raise ValueError("background music is required for this audio mode")
    if len(clip_paths) == 1 and audio_mode not in {"mix", "music"}:
        raise ValueError("A single video clip requires background music")
    if not 0 <= original_volume <= 2 or not 0 <= music_volume <= 2:
        raise ValueError("audio volumes must be between 0 and 2")

    ffmpeg, ffprobe = resolve_ffmpeg()
    target_w, target_h = (720, 1280) if aspect_ratio == "9:16" else (1280, 720)
    scale = (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
        f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,"
        "setsar=1,fps=30,format=yuv420p"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _report_progress(progress_callback, 5, "preparing")

    with tempfile.TemporaryDirectory(
        prefix="compose-",
        dir=str(COMPOSER_TMP_DIR),
    ) as temp_name:
        temp_dir = Path(temp_name)
        normalized: list[Path] = []

        for index, source in enumerate(clip_paths):
            if cancel_check():
                raise ComposerCanceled("canceled")
            _report_progress(
                progress_callback,
                10 + int(index / len(clip_paths) * 50),
                "normalizing",
            )
            probe = await _probe(source, ffprobe, cancel_check)
            source_duration = _duration(probe)
            target = temp_dir / f"clip-{index:03d}.mp4"
            if _has_audio(probe):
                cmd = [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(source),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-vf",
                    scale,
                    "-af",
                    "aresample=48000,apad",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "23",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-shortest",
                    "-t",
                    f"{source_duration:.3f}",
                    str(target),
                ]
            else:
                cmd = [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(source),
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-vf",
                    scale,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "23",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-shortest",
                    "-t",
                    f"{source_duration:.3f}",
                    str(target),
                ]
            await _run_command(
                cmd,
                cancel_check=cancel_check,
                error_label=f"Failed to normalize clip {index + 1}",
            )
            normalized.append(target)
            _report_progress(
                progress_callback,
                10 + int((index + 1) / len(clip_paths) * 50),
                "normalizing",
            )

        _report_progress(progress_callback, 65, "concatenating")
        concat_file = temp_dir / "concat.txt"
        concat_file.write_text(
            "".join(
                f"file '{path.resolve().as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
                for path in normalized
            ),
            encoding="utf-8",
        )
        joined = temp_dir / "joined.mp4"
        await _run_command(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-c",
                "copy",
                str(joined),
            ],
            cancel_check=cancel_check,
            error_label="Failed to concatenate clips",
        )
        duration = _duration(await _probe(joined, ffprobe, cancel_check))
        _report_progress(progress_callback, 75, "concatenating")

        if audio_mode == "original" and original_volume == 1:
            _report_progress(progress_callback, 82, "finalizing")
            await _run_command(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(joined),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ],
                cancel_check=cancel_check,
                error_label="Failed to finalize composed video",
            )
        elif audio_mode == "muted":
            _report_progress(progress_callback, 82, "finalizing")
            await _run_command(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(joined),
                    "-map",
                    "0:v:0",
                    "-c:v",
                    "copy",
                    "-an",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ],
                cancel_check=cancel_check,
                error_label="Failed to mute original audio",
            )
        elif audio_mode == "original":
            _report_progress(progress_callback, 82, "finalizing")
            await _run_command(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(joined),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-c:v",
                    "copy",
                    "-filter:a",
                    f"volume={original_volume}",
                    "-c:a",
                    "aac",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ],
                cancel_check=cancel_check,
                error_label="Failed to adjust original audio",
            )
        else:
            assert audio_path is not None
            _report_progress(progress_callback, 82, "mixing_audio")
            fade_start = max(duration - 1.0, 0.0)
            music_filter = (
                f"volume={music_volume},atrim=0:{duration:.3f},"
                f"afade=t=out:st={fade_start:.3f}:d=1[music]"
            )
            if audio_mode == "music":
                filter_complex = f"[1:a]{music_filter}"
                audio_map = "[music]"
            else:
                filter_complex = (
                    f"[0:a]volume={original_volume}[original];"
                    f"[1:a]{music_filter};"
                    "[original][music]amix=inputs=2:duration=first:"
                    "dropout_transition=0[audio]"
                )
                audio_map = "[audio]"
            await _run_command(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(joined),
                    "-stream_loop",
                    "-1",
                    "-i",
                    str(audio_path),
                    "-filter_complex",
                    filter_complex,
                    "-map",
                    "0:v:0",
                    "-map",
                    audio_map,
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-t",
                    f"{duration:.3f}",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ],
                cancel_check=cancel_check,
                error_label="Failed to apply background music",
            )
        _report_progress(progress_callback, 95, "finalizing")

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("FFmpeg did not produce an output video")
    return duration
