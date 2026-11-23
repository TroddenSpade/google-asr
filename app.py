"""Voice Typing ASR: FastAPI service with one API pair per model.

Every model exposes:

    POST /api/v1/models/{model}/transcribe       multipart file or url
    POST /api/v1/models/{model}/transcribe-url   JSON {"url": "..."}
    WS   /api/v1/models/{model}/live             PCM in, partial/final text out

Registered models:

    tinyasr   local Zipformer2, Persian + English, streaming, punctuation
    google    hosted Google endpoint, energy VAD, unpunctuated
    browser   Chrome Web Speech, live only, runs in the client

``GET /api/v1/models`` lists them with capabilities and availability.

No database, no queue, no auth. Uploads, downloads and live audio are never
stored after the request.

Run with ``python app.py`` (or ``uvicorn app:app``).
"""

from __future__ import annotations

import ipaddress
import logging
import mimetypes
import os
import re
import shutil
import socket
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import certifi
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import asr_service
import config
import nemotron_client
from live import run_google, run_nemotron, run_tinyasr
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
URL_TIMEOUT = float(os.environ.get("VOICE_URL_TIMEOUT", "30"))
ALLOW_PRIVATE_URLS = os.environ.get(
    "VOICE_ALLOW_PRIVATE_URLS", ""
).strip().lower() in {"1", "true", "yes", "on"}
URL_ALLOW_HOSTS = {
    host.strip().lower()
    for host in os.environ.get("VOICE_URL_ALLOW_HOSTS", "").split(",")
    if host.strip()
}

LANGUAGE_PATTERN = re.compile(r"[a-z]{2,3}(?:-[A-Za-z]{2,4})?")

logger = logging.getLogger("asr")


class UrlRequest(BaseModel):
    url: str
    language: str = DEFAULT_LANGUAGE
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS


def _tinyasr_file(source: Path, language: str, chunk_seconds: float) -> dict:
    return asr_service.transcribe_file(source, language)


def _nemotron_file(source: Path, language: str, chunk_seconds: float) -> dict:
    return nemotron_client.transcribe_file(source, language)


def _google_file(source: Path, language: str, chunk_seconds: float) -> dict:
    transcript = transcribe_file(source, language, chunk_seconds)
    return {
        "ok": True,
        "model": "google",
        "language": language,
        "text": transcript.text,
        "segments": [segment.to_dict() for segment in transcript.segments],
        "srt": format_srt(transcript.segments),
        "duration": round(transcript.duration, 3),
        "elapsed": round(transcript.elapsed, 2),
    }


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    kind: str  # local | hosted | client
    description: str
    file_handler: Callable[[Path, str, float], dict] | None
    live_handler: Callable[[WebSocket, str], Awaitable[None]] | None


MODEL_SPECS: dict[str, ModelSpec] = {
    "tinyasr": ModelSpec(
        id="tinyasr",
        name="tinyasr (local)",
        kind="local",
        description="Zipformer2 · Persian/English · streaming · punctuation",
        file_handler=_tinyasr_file,
        live_handler=run_tinyasr,
    ),
    "nemotron": ModelSpec(
        id="nemotron",
        name="Nemotron (GPU)",
        kind="gpu",
        description="FastConformer RNNT · Persian · 1.12 s look-ahead",
        file_handler=_nemotron_file,
        live_handler=run_nemotron,
    ),
    "google": ModelSpec(
        id="google",
        name="Google (hosted)",
        kind="hosted",
        description="Google speech endpoint · free, unofficial · no punctuation",
        file_handler=_google_file,
        live_handler=run_google,
    ),
    "browser": ModelSpec(
        id="browser",
        name="Browser (Chrome)",
        kind="client",
        description="Chrome Web Speech API · live only · audio goes to Google",
        file_handler=None,
        live_handler=None,
    ),
}

# Only enabled models are registered, so disabled ones never load.
MODELS: dict[str, ModelSpec] = {
    model_id: spec
    for model_id, spec in MODEL_SPECS.items()
    if config.MODEL_FLAGS.get(model_id, 0)
}


def _model_info(spec: ModelSpec) -> dict:
    if spec.id == "tinyasr":
        local = asr_service.info()
        return {
            "id": spec.id,
            "name": spec.name,
            "kind": spec.kind,
            "description": spec.description,
            "available": local["available"],
            "file": spec.file_handler is not None,
            "live": spec.live_handler is not None,
            "languages": local.get("languages", []),
            "chunk_ms": local.get("chunk_ms"),
            "threads": local.get("threads"),
        }

    if spec.id == "nemotron":
        remote = nemotron_client.info()
        return {
            "id": spec.id,
            "name": spec.name,
            "kind": spec.kind,
            "description": spec.description,
            "available": bool(remote.get("available")),
            "file": True,
            "live": True,
            "languages": ["fa"],
            "chunk_ms": round(float(remote.get("chunk_s") or 1.145) * 1000),
            "threads": None,
        }

    return {
        "id": spec.id,
        "name": spec.name,
        "kind": spec.kind,
        "description": spec.description,
        "available": True,
        "file": spec.file_handler is not None,
        # The browser engine is live-only but runs in the client, so it has no
        # server-side WebSocket handler.
        "live": spec.kind == "client" or spec.live_handler is not None,
        "languages": ["*"],
        "chunk_ms": None,
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Load only when tinyasr is enabled. Disabled models stay off the CPU/GPU.
    if "tinyasr" in MODELS:
        await run_in_threadpool(asr_service.warm)
    yield


app = FastAPI(title="Voice Typing ASR", version="3.1.0", lifespan=lifespan)


def _suffix_for(upload: UploadFile) -> str | None:
    suffix = Path(Path(upload.filename or "").name).suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        return suffix
    guessed = mimetypes.guess_extension(upload.content_type or "")
    return guessed if guessed in AUDIO_EXTENSIONS else None


def _filename_from_url(url: str) -> str:
    name = Path(urllib.parse.urlparse(url).path).name
    return name or "remote-audio"


def _suffix_from_url(url: str) -> str:
    suffix = Path(_filename_from_url(url)).suffix.lower()
    return suffix if suffix in AUDIO_EXTENSIONS else ".bin"


def _check_url(url: str) -> None:
    """Reject non-HTTP URLs and, unless allowed, private/link-local hosts."""
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f"Invalid URL: {error}") from error

    if parsed.scheme not in ("http", "https") or not hostname:
        raise HTTPException(
            status_code=400, detail="Only http:// and https:// URLs are supported."
        )
    if ALLOW_PRIVATE_URLS or hostname.lower() in URL_ALLOW_HOSTS:
        return

    try:
        addresses = socket.getaddrinfo(
            hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as error:
        raise HTTPException(
            status_code=400, detail=f"Could not resolve {hostname}."
        ) from error

    for info in addresses:
        address = info[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not ip.is_global:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Private, loopback and link-local URLs are disabled. "
                    "Add the host to VOICE_URL_ALLOW_HOSTS, or set "
                    "VOICE_ALLOW_PRIVATE_URLS=1 to allow all of them."
                ),
            )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        target = urllib.parse.urljoin(req.full_url, newurl)
        _check_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _download_url(url: str, destination: Path) -> None:
    """Download an audio URL to ``destination`` with size and SSRF guards."""
    _check_url(url)
    context = ssl.create_default_context(cafile=certifi.where())
    opener = urllib.request.build_opener(
        _SafeRedirectHandler,
        urllib.request.HTTPSHandler(context=context),
    )
    limit = MAX_UPLOAD_MB * 1024 * 1024
    request = urllib.request.Request(
        url, headers={"User-Agent": "voice-typing-asr/1.0"}
    )
    try:
        with opener.open(request, timeout=URL_TIMEOUT) as response:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise HTTPException(
                    status_code=413,
                    detail=f"File is too large. Limit: {MAX_UPLOAD_MB} MB.",
                )
            written = 0
            with destination.open("wb") as target:
                while chunk := response.read(1024 * 1024):
                    written += len(chunk)
                    if written > limit:
                        raise HTTPException(
                            status_code=413,
                            detail=f"File is too large. Limit: {MAX_UPLOAD_MB} MB.",
                        )
                    target.write(chunk)
    except HTTPException:
        raise
    except urllib.error.HTTPError as error:
        raise HTTPException(
            status_code=400, detail=f"The URL returned HTTP {error.code}."
        ) from error
    except urllib.error.URLError as error:
        raise HTTPException(
            status_code=400, detail=f"Could not download the URL: {error.reason}."
        ) from error
    except TimeoutError as error:
        raise HTTPException(status_code=504, detail="The URL download timed out.") from error

    if destination.stat().st_size == 0:
        raise HTTPException(status_code=400, detail="The URL returned an empty file.")


async def _prepare_model(
    spec: ModelSpec | None, language: str, chunk_seconds: float
) -> ModelSpec:
    if spec is None:
        raise HTTPException(status_code=404, detail="Unknown model.")
    if spec.file_handler is None:
        raise HTTPException(
            status_code=400,
            detail=f"{spec.name} has no file API. Choose another model.",
        )
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
    if spec.id == "tinyasr" and not await run_in_threadpool(
        asr_service.supports, language
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "The local tinyasr model is unavailable or does not support "
                f"{language}. Run `python fetch_tinyasr.py --runtime`, or use "
                "the Google model."
            ),
        )
    if spec.id == "nemotron":
        if language.split("-", 1)[0].lower() != "fa":
            raise HTTPException(
                status_code=400,
                detail="Nemotron supports Persian (fa / fa-IR) only.",
            )
        if not await run_in_threadpool(nemotron_client.available):
            raise HTTPException(
                status_code=503,
                detail="The Nemotron GPU service is not running.",
            )
    return spec


async def _run_handler(
    spec: ModelSpec,
    source: Path,
    language: str,
    chunk_seconds: float,
    filename: str,
) -> dict:
    assert spec.file_handler is not None
    try:
        payload = await run_in_threadpool(
            spec.file_handler, source, language, chunk_seconds
        )
    except TranscriptionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    payload["model"] = spec.id
    payload["filename"] = filename
    return payload


@app.get("/api/v1/models")
def list_models() -> dict:
    return {"models": [_model_info(spec) for spec in MODELS.values()]}


@app.get("/api/v1/health")
def health() -> dict:
    models = [_model_info(spec) for spec in MODELS.values()]
    available = [model for model in models if model["available"]]
    preferred = next((model for model in available if model["id"] == "tinyasr"), None)
    default = preferred["id"] if preferred else (available[0]["id"] if available else None)

    body = {
        "ok": True,
        "default_model": default,
        "models": models,
        "default_language": DEFAULT_LANGUAGE,
        "max_upload_mb": MAX_UPLOAD_MB,
        "allow_private_urls": ALLOW_PRIVATE_URLS,
        "url_allow_hosts": sorted(URL_ALLOW_HOSTS),
    }
    try:
        body["ffmpeg"] = Path(ffmpeg_path()).name
    except TranscriptionError as error:
        body["ok"] = False
        body["ffmpeg_error"] = str(error)
    return body


@app.post("/api/v1/models/{model_id}/transcribe")
async def model_transcribe(
    model_id: str,
    file: UploadFile | None = File(default=None),
    url: str = Form(""),
    language: str = Form(DEFAULT_LANGUAGE),
    chunk_seconds: float = Form(DEFAULT_CHUNK_SECONDS),
) -> dict:
    """Transcribe an uploaded file or a remote audio URL."""
    spec = await _prepare_model(MODELS.get(model_id), language, chunk_seconds)
    url = url.strip()

    has_file = file is not None and bool(file.filename)
    if has_file == bool(url):
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of file or url.",
        )

    workdir = Path(tempfile.mkdtemp(prefix="voice-upload-"))
    try:
        if url:
            source = workdir / f"remote{_suffix_from_url(url)}"
            await run_in_threadpool(_download_url, url, source)
            filename = _filename_from_url(url)
        else:
            assert file is not None
            suffix = _suffix_for(file)
            if suffix is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Unsupported file type. Supported: "
                        + ", ".join(sorted(AUDIO_EXTENSIONS))
                    ),
                )
            source = workdir / f"upload{suffix}"
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
            filename = Path(file.filename or "audio").name

        return await _run_handler(
            spec, source, language, chunk_seconds, filename
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.post("/api/v1/models/{model_id}/transcribe-url")
async def model_transcribe_url(model_id: str, body: UrlRequest) -> dict:
    """Transcribe a remote audio URL from a JSON body."""
    spec = await _prepare_model(
        MODELS.get(model_id), body.language, body.chunk_seconds
    )
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="url must not be empty.")

    workdir = Path(tempfile.mkdtemp(prefix="voice-url-"))
    try:
        source = workdir / f"remote{_suffix_from_url(url)}"
        await run_in_threadpool(_download_url, url, source)
        return await _run_handler(
            spec,
            source,
            body.language,
            body.chunk_seconds,
            _filename_from_url(url),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.websocket("/api/v1/models/{model_id}/live")
async def model_live(
    websocket: WebSocket,
    model_id: str,
    language: str = DEFAULT_LANGUAGE,
) -> None:
    """Stream PCM to the selected model; events are model-independent."""
    spec = MODELS.get(model_id)
    if spec is None or not LANGUAGE_PATTERN.fullmatch(language):
        await websocket.close(code=1008, reason="Unknown model or language")
        return

    if spec.live_handler is None:
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "error",
                "message": (
                    f"{spec.name} runs in the browser and has no server-side "
                    "live API. Use the Browser model from the UI."
                ),
            }
        )
        await websocket.close(code=1011)
        return

    if spec.id == "tinyasr" and not await run_in_threadpool(
        asr_service.supports, language
    ):
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "error",
                "message": (
                    "The local tinyasr model is unavailable or does not support "
                    f"{language}. Use the Google model."
                ),
            }
        )
        await websocket.close(code=1011)
        return

    if spec.id == "nemotron":
        if language.split("-", 1)[0].lower() != "fa":
            await websocket.accept()
            await websocket.send_json(
                {"type": "error", "message": "Nemotron supports Persian only."}
            )
            await websocket.close(code=1011)
            return
        if not await run_in_threadpool(nemotron_client.available):
            await websocket.accept()
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "The Nemotron GPU service is not running.",
                }
            )
            await websocket.close(code=1011)
            return

    await websocket.accept()
    await spec.live_handler(websocket, language)


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("VOICE_HOST", "127.0.0.1")
    port = int(os.environ.get("VOICE_PORT", "8765"))
    print(f"Voice Typing ASR on http://{host}:{port}")
    print(f"API: http://{host}:{port}/api/v1  ·  docs: /docs")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
