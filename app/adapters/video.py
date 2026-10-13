"""Video upload. We never decode video ourselves.

Order: ffprobe metadata (if installed) -> duration cap (20 min) -> transcript:
  1. uploaded transcript (.txt/.srt/.vtt)  2. faster-whisper tiny, only if WHISPER_ENABLED=true  3. 'partial'
"""
import json
import os
import re
import shutil
import subprocess

from app.adapters.base import FAILED, OK, PARTIAL, SKIPPED, AdapterResult, register
from app.config import get_settings

MAX_DURATION_S = 20 * 60
_TS = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})")


def _probe_duration(path: str) -> float | None:
    """Duration in seconds via ffprobe, or None if ffprobe is unavailable/fails."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
            capture_output=True, timeout=20, check=True,
        ).stdout
        return float(json.loads(out)["format"]["duration"])
    except Exception:  # noqa: BLE001
        return None


def parse_subtitles(raw: str) -> tuple[str, float]:
    """Return (plain text, last cue end time in seconds) for srt/vtt/plain text."""
    last = 0.0
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.isdigit() or s.upper() == "WEBVTT":
            continue
        times = _TS.findall(s)
        if times and "-->" in s:
            h, m, sec, ms = times[-1]
            last = max(last, int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000)
            continue
        lines.append(s)
    return "\n".join(lines), last


def _whisper(path: str) -> str | None:
    try:
        from faster_whisper import WhisperModel  # optional heavy dependency
    except ImportError:
        return None
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(path)
    return " ".join(seg.text.strip() for seg in segments)


@register("video")
def fetch_video(artifact, storage) -> AdapterResult:
    if not artifact.storage_key:
        return AdapterResult(SKIPPED, "video URLs are not downloaded; upload the file (and optional transcript)")
    path = storage.local_path(artifact.storage_key)
    meta: dict = {"filename": artifact.source_ref}
    duration = _probe_duration(path)
    meta["ffprobe"] = duration is not None

    transcript_text, cue_end = "", 0.0
    tkey = (artifact.meta or {}).get("transcript_key")
    if tkey:
        raw = storage.get(tkey).decode("utf-8", errors="replace")
        transcript_text, cue_end = parse_subtitles(raw)
        if duration is None and cue_end:
            duration = cue_end  # fallback: the transcript's last cue bounds the length
            meta["duration_source"] = "transcript"
    if duration is not None:
        meta["duration_s"] = round(duration, 1)
        if duration > MAX_DURATION_S:
            return AdapterResult(SKIPPED, f"video is {duration / 60:.1f} min, over the 20 min limit", meta=meta)

    if transcript_text.strip():
        return AdapterResult(OK, "", {"video_transcript.txt": transcript_text}, meta)

    if get_settings().whisper_enabled or os.environ.get("WHISPER_ENABLED", "").lower() == "true":
        try:
            text = _whisper(path)
        except Exception as e:  # noqa: BLE001
            return AdapterResult(FAILED, f"whisper failed: {type(e).__name__}", meta=meta)
        if text is None:
            return AdapterResult(PARTIAL, "WHISPER_ENABLED but faster-whisper is not installed", meta=meta)
        if text.strip():
            return AdapterResult(OK, "transcribed with faster-whisper tiny", {"video_transcript.txt": text}, meta)
    reason = "no transcript: upload a .txt/.srt or set WHISPER_ENABLED=true"
    if duration is None:
        reason += "; duration unknown (ffprobe not installed)"
    return AdapterResult(PARTIAL, reason, {}, meta)
