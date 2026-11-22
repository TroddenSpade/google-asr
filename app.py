"""Voice Typing ASR: FastAPI service.

Two things, and nothing else:

    POST /api/v1/transcribe   upload one audio/video file, get text
    WS   /api/v1/live         stream PCM, get text per utterance
    GET  /api/v1/health       liveness

No database, no queue, no auth. Uploads are written to a temporary
directory, transcribed, and deleted. Live audio is never stored.

Run with ``python app.py`` (or ``uvicorn app:app``).
"""

from __future__ import annotations

import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from live import router as live_router
from transcriber import (
    AUDIO_EXTENSIONS,
    DEFAULT_CHUNK_SECONDS,
    DEFAULT_LANGUAGE,
    TranscriptionError,
    ffmpeg_path,
    format_srt,
    transcribe_file,
)

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

MAX_UPLOAD_MB = int(os.environ.get("VOICE_MAX_UPLOAD_MB", "500"))
LANGUAGE_PATTERN = re.compile(r"[a-z]{2,3}(?:-[A-Za-z]{2,4})?")

app = FastAPI(title="Voice Typing ASR", version="2.0.0")
app.include_router(live_router)


def _suffix_for(upload: UploadFile) -> str | None:
    suffix = Path(Path(upload.filename or "").name).suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        return suffix
    guessed = mimetypes.guess_extension(upload.content_type or "")
    return guessed if guessed in AUDIO_EXTENSIONS else None


@app.get("/api/v1/health")
def health() -> dict:
    info = {
        "ok": True,
        "default_language": DEFAULT_LANGUAGE,
        "max_upload_mb": MAX_UPLOAD_MB,
    }
    try:
        info["ffmpeg"] = Path(ffmpeg_path()).name
    except TranscriptionError as error:
        info["ok"] = False
        info["ffmpeg_error"] = str(error)
    return info


@app.post("/api/v1/transcribe")
async def transcribe_upload(
    file: UploadFile = File(...),
    language: str = Form(DEFAULT_LANGUAGE),
    chunk_seconds: float = Form(DEFAULT_CHUNK_SECONDS),
) -> dict:
    """Transcribe one uploaded file and return the text with timed segments."""
    if not LANGUAGE_PATTERN.fullmatch(language):
        raise HTTPException(
            status_code=400,
            detail="Invalid language code. Use a BCP-47 code such as fa-IR.",
        )
    if not 5 <= chunk_seconds <= 50:
        raise HTTPException(
            status_code=400,
            detail="chunk_seconds must be a number between 5 and 50.",
        )

    suffix = _suffix_for(file)
    if suffix is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported file type. Supported: "
                + ", ".join(sorted(AUDIO_EXTENSIONS))
            ),
        )

    workdir = Path(tempfile.mkdtemp(prefix="voice-upload-"))
    source = workdir / f"upload{suffix}"
    try:
        limit = MAX_UPLOAD_MB * 1024 * 1024
        written = 0
        with source.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File is too large. Limit: {MAX_UPLOAD_MB} MB.",
                    )
                target.write(chunk)

        try:
            transcript = await run_in_threadpool(
                transcribe_file, source, language, chunk_seconds
            )
        except TranscriptionError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        payload = transcript.to_dict()
        payload.update(
            ok=True,
            filename=Path(file.filename or "audio").name,
            srt=format_srt(transcript.segments),
        )
        return payload
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def main() -> None:
    host = os.environ.get("VOICE_HOST", "127.0.0.1")
    port = int(os.environ.get("VOICE_PORT", "8765"))
    print(f"Voice Typing ASR on http://{host}:{port}")
    print(f"API: http://{host}:{port}/api/v1  ·  docs: /docs")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
