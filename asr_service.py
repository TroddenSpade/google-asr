"""Engine selection and the tinyasr (local) transcription path.

The service has two engines:

* ``tinyasr`` — local Zipformer2, Persian + English, streaming, punctuation.
* ``google`` — the hosted endpoint used by the original project.

``auto`` picks tinyasr when the model is loaded and supports the language,
otherwise Google. Nothing here is imported by the UI; the API reports which
engine ran in every response.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from transcriber import Segment, convert_to_wav, format_srt
from tinyasr_engine import TinyASR

logger = logging.getLogger("asr")

ROOT = Path(__file__).resolve().parent
MODEL = Path(os.environ.get("TINYASR_MODEL", ROOT / "models" / "en-fa.tzf"))
VAD = Path(os.environ.get("TINYASR_VAD", ROOT / "models" / "silero_vad.onnx"))
LIB = Path(os.environ.get("TINYASR_LIB", ROOT / "models" / "libtinyasr.so"))
DISABLED = os.environ.get("VOICE_DISABLE_TINYASR", "").strip().lower() in {
    "1", "true", "yes", "on",
}

# Live sessions are numerous; one thread each avoids oversubscription. File
# jobs are few and benefit from parallel encoder steps.
LIVE_THREADS = max(1, int(os.environ.get("TINYASR_LIVE_THREADS", "1")))
FILE_THREADS = max(1, int(os.environ.get("TINYASR_FILE_THREADS", "4")))

_lock = threading.Lock()
_engine: TinyASR | None = None
_engine_attempted = False
_file_engine: TinyASR | None = None
_file_engine_attempted = False


def language_code(language: str) -> str:
    """``fa-IR`` -> ``fa``; tinyasr uses bare language codes."""
    return language.split("-", 1)[0].strip().lower()


def _load(threads: int, label: str) -> TinyASR | None:
    if DISABLED:
        logger.info("tinyasr disabled by VOICE_DISABLE_TINYASR")
        return None
    if not (MODEL.exists() and VAD.exists() and LIB.exists()):
        logger.warning(
            "tinyasr model/runtime missing: %s %s %s — run "
            "`python fetch_tinyasr.py --runtime`",
            MODEL, VAD, LIB,
        )
        return None
    try:
        started = time.monotonic()
        engine = TinyASR(LIB, MODEL, VAD, threads=threads)
        logger.info(
            "%s loaded: v%s languages=%s chunk=%sms threads=%s in %.2fs",
            label,
            engine.lib.tinyasr_version(),
            engine.languages(),
            engine.chunk_ms,
            engine.threads,
            time.monotonic() - started,
        )
        return engine
    except Exception:
        logger.exception("%s failed to load", label)
        return None


def get_engine() -> TinyASR | None:
    """Live engine: one thread per session by default."""
    global _engine, _engine_attempted
    if _engine is not None:
        return _engine
    if _engine_attempted:
        return None
    with _lock:
        if _engine_attempted:
            return _engine
        _engine_attempted = True
        _engine = _load(LIVE_THREADS, "tinyasr live")
        return _engine


def get_file_engine() -> TinyASR | None:
    """File engine: parallel encoder steps for lower latency per job."""
    global _file_engine, _file_engine_attempted
    if _file_engine is not None:
        return _file_engine
    if _file_engine_attempted:
        return None
    with _lock:
        if _file_engine_attempted:
            return _file_engine
        _file_engine_attempted = True
        _file_engine = _load(FILE_THREADS, "tinyasr file")
        return _file_engine


def warm() -> None:
    get_engine()
    get_file_engine()


def available() -> bool:
    return get_engine() is not None


def supports(language: str) -> bool:
    engine = get_engine()
    return engine is not None and language_code(language) in engine.languages()


def info() -> dict:
    engine = get_engine()
    if engine is None:
        return {"available": False}
    return {
        "available": True,
        "version": int(engine.lib.tinyasr_version()),
        "languages": engine.languages(),
        "chunk_ms": engine.chunk_ms,
        "threads": {"live": engine.threads, "file": FILE_THREADS},
        "model": MODEL.name,
    }


def _group_words(
    words: list[dict],
    max_gap: float = 0.8,
    max_chars: int = 80,
    max_seconds: float = 8.0,
) -> list[Segment]:
    """Turn word timestamps into subtitle-sized lines."""
    segments: list[Segment] = []
    current: list[dict] = []
    start = end = 0.0

    def flush() -> None:
        if current:
            segments.append(
                Segment(
                    start=start,
                    end=end,
                    text=" ".join(word["text"] for word in current),
                )
            )

    for word in words:
        text = (word.get("text") or "").strip()
        if not text:
            continue
        word_start = float(word.get("start", 0.0))
        word_end = float(word.get("end", word_start))

        if current:
            length = sum(len(item["text"]) + 1 for item in current) + len(text)
            if (
                word_start - end > max_gap
                or word_end - start > max_seconds
                or length > max_chars
            ):
                flush()
                current = []

        if not current:
            start = word_start
        current.append({"text": text})
        end = word_end

    flush()
    return segments


def transcribe_file(source: Path, language: str, beam: int = 4) -> dict:
    """Decode any upload to 16 kHz WAV, then run the local full-context pass."""
    engine = get_file_engine()
    if engine is None:
        raise RuntimeError("tinyasr is not available")

    started = time.monotonic()
    workdir = Path(tempfile.mkdtemp(prefix="tinyasr-upload-"))
    try:
        wav_path = workdir / "audio.wav"
        convert_to_wav(source, wav_path)
        result = engine.transcribe_file(
            wav_path, language=language_code(language), beam=beam
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    segments = _group_words(result["segments"])
    return {
        "ok": True,
        "model": "tinyasr",
        "language": language,
        "text": " ".join(result["text"].split()),
        "segments": [segment.to_dict() for segment in segments],
        "srt": format_srt(segments),
        "duration": round(result["duration"], 3),
        "elapsed": round(time.monotonic() - started, 2),
        "words": len(result["segments"]),
    }
