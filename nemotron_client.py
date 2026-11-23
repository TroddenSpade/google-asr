"""Proxy helpers for the separate Nemotron GPU service.

The main ASR app stays torch-free; it sends WAV/PCM to
``NEMOTRON_URL/transcribe``. Availability is cached briefly so ``/models`` does
not block when the GPU service is down.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import wave
from pathlib import Path

import httpx

from transcriber import Segment, convert_to_wav, format_srt

URL = os.environ.get("NEMOTRON_URL", "http://127.0.0.1:8010").rstrip("/")
TIMEOUT = float(os.environ.get("NEMOTRON_TIMEOUT", "180"))
SAMPLE_RATE = 16000

_lock = threading.Lock()
_available: bool | None = None
_checked_at = 0.0


def available(force: bool = False) -> bool:
    global _available, _checked_at
    now = time.monotonic()
    with _lock:
        if not force and _available is not None and now - _checked_at < 3.0:
            return _available
        try:
            with httpx.Client(timeout=1.5) as client:
                response = client.get(f"{URL}/health")
            _available = bool(response.json().get("ok"))
        except Exception:
            _available = False
        _checked_at = now
        return _available


def info() -> dict:
    try:
        with httpx.Client(timeout=1.5) as client:
            response = client.get(f"{URL}/health")
            body = response.json()
    except Exception as error:
        return {"available": False, "error": str(error)}
    return {"available": bool(body.get("ok")), **body}


def transcribe_pcm(pcm16: bytes, language: str = "fa-IR") -> dict:
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.post(
            f"{URL}/transcribe",
            params={"language": language},
            content=pcm16,
            headers={"Content-Type": "application/octet-stream"},
        )
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except Exception:
            detail = response.text
        raise RuntimeError(detail or f"Nemotron returned {response.status_code}")
    return response.json()


def transcribe_file(source: Path, language: str = "fa-IR") -> dict:
    started = time.monotonic()
    workdir = Path(tempfile.mkdtemp(prefix="nemotron-upload-"))
    try:
        wav_path = workdir / "audio.wav"
        convert_to_wav(source, wav_path)
        with wave.open(str(wav_path), "rb") as reader:
            if (
                reader.getnchannels() != 1
                or reader.getsampwidth() != 2
                or reader.getframerate() != SAMPLE_RATE
            ):
                raise RuntimeError("expected mono 16 kHz 16-bit WAV")
            pcm16 = reader.readframes(reader.getnframes())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    result = transcribe_pcm(pcm16, language)
    text = (result.get("text") or "").strip()
    duration = float(result.get("duration") or len(pcm16) / (SAMPLE_RATE * 2))
    segments = [Segment(start=0.0, end=duration, text=text)] if text else []
    return {
        "ok": True,
        "model": "nemotron",
        "language": language,
        "text": text,
        "segments": [segment.to_dict() for segment in segments],
        "srt": format_srt(segments),
        "duration": round(duration, 3),
        "elapsed": round(time.monotonic() - started, 2),
    }
