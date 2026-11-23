"""Live transcription handlers, one per server model.

Both handlers assume the WebSocket is already accepted and send the same
event protocol, so the UI does not need model-specific branches:

    {"type": "ready", "model": "...", "sample_rate": 16000,
     "language": "fa-IR", "languages": [...], "chunk_ms": 320}
    {"type": "partial", "text": "..."}                 not final yet
    {"type": "final", "text": "...", "words": [...]}   committed text
    {"type": "language", "lang": "fa"}                 language switch
    {"type": "error", "message": "..."}

* ``run_tinyasr`` — one streaming tinyasr session per connection; the model's
  own VAD and end-of-turn head replace the energy VAD.
* ``run_google`` — energy VAD + the hosted endpoint, one request per utterance.

Clients send only binary PCM (16 kHz, mono, signed 16-bit little-endian).
Nothing is stored.
"""

from __future__ import annotations

import json
import math
from array import array
from collections import deque

from fastapi import WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

import asr_service
import nemotron_client
from transcriber import TranscriptionError, transcribe_pcm

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH

# Google engine (energy VAD); tinyasr uses the model's own VAD.
START_RMS = 400.0
STOP_RMS = 250.0
SILENCE_MS = 600
PREROLL_FRAMES = 300 // FRAME_MS
MIN_SPEECH_MS = 400
MAX_SPEECH_MS = 20000


def _rms(frame: bytes) -> float:
    samples = array("h")
    samples.frombytes(frame)
    if not samples:
        return 0.0
    return math.sqrt(sum(value * value for value in samples) / len(samples))


class Segmenter:
    """Split continuous PCM into utterances for the Google engine."""

    def __init__(self) -> None:
        self._pending = bytearray()
        self._preroll: deque[bytes] = deque(maxlen=PREROLL_FRAMES)
        self._speech: list[bytes] = []
        self._in_speech = False
        self._silence_ms = 0

    def feed(self, data: bytes) -> list[tuple[str, bytes | int]]:
        self._pending.extend(data)
        events: list[tuple[str, bytes | int]] = []
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            events.extend(self._frame(frame))
        return events

    def _frame(self, frame: bytes) -> list[tuple[str, bytes | int]]:
        level = _rms(frame)

        if not self._in_speech:
            self._preroll.append(frame)
            if level >= START_RMS:
                self._in_speech = True
                self._speech = list(self._preroll)
                self._preroll.clear()
                self._silence_ms = 0
                return [("start", (len(self._speech) - 1) * FRAME_MS)]
            return []

        self._speech.append(frame)
        if level < STOP_RMS:
            self._silence_ms += FRAME_MS
        else:
            self._silence_ms = 0

        if (
            self._silence_ms >= SILENCE_MS
            or len(self._speech) * FRAME_MS >= MAX_SPEECH_MS
        ):
            return self._finish()
        return []

    def _finish(self) -> list[tuple[str, bytes | int]]:
        speech = self._speech
        self._speech = []
        self._in_speech = False
        self._silence_ms = 0
        self._preroll.clear()

        voiced_ms = max(0, len(speech) - SILENCE_MS // FRAME_MS) * FRAME_MS
        if voiced_ms < MIN_SPEECH_MS:
            return [("empty", 0)]
        return [("end", b"".join(speech))]

    def flush(self) -> list[tuple[str, bytes | int]]:
        """Flush an open utterance when the client stops mid-sentence."""
        return self._finish()


def _text(words: list[dict]) -> str:
    return " ".join((word.get("text") or "").strip() for word in words).strip()


async def _forward_events(websocket: WebSocket, events: list[dict]) -> None:
    for event in events:
        kind = event.get("type")
        if kind == "commit":
            words = event.get("words") or []
            text = _text(words)
            if text:
                await websocket.send_json(
                    {"type": "final", "text": text, "words": words}
                )
        elif kind == "partial":
            words = (event.get("open") or []) + (event.get("tail") or [])
            # The word times are what lets a client merge this draft with the
            # durable file-path transcript instead of showing both: it can
            # drop exactly the words a finished chunk already covers. Older
            # clients ignore the extra field.
            await websocket.send_json(
                {"type": "partial", "text": _text(words), "words": words}
            )
        elif kind == "language":
            await websocket.send_json(
                {"type": "language", "lang": event.get("lang", "")}
            )


async def run_tinyasr(websocket: WebSocket, language: str) -> None:
    engine = asr_service.get_engine()
    if engine is None:
        await websocket.send_json(
            {"type": "error", "message": "tinyasr model is not available."}
        )
        return

    session = await run_in_threadpool(
        engine.open_session,
        asr_service.language_code(language),
        4,
    )
    await websocket.send_json(
        {
            "type": "ready",
            "model": "tinyasr",
            "sample_rate": SAMPLE_RATE,
            "language": language,
            "chunk_ms": engine.chunk_ms,
            "languages": engine.languages(),
        }
    )
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break

            data = message.get("bytes")
            if data:
                try:
                    events = await run_in_threadpool(session.push, data)
                except Exception as error:
                    await websocket.send_json(
                        {"type": "error", "message": f"tinyasr: {error}"}
                    )
                    break
                await _forward_events(websocket, events)
                continue

            text = message.get("text")
            if text:
                try:
                    command = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if command.get("type") == "stop":
                    break
    except WebSocketDisconnect:
        pass
    finally:
        try:
            events = await run_in_threadpool(session.finish)
            await _forward_events(websocket, events)
            await websocket.send_json({"type": "done"})
        except Exception:
            # The client is gone; the session is still released below.
            pass
        session.close()


async def run_nemotron(websocket: WebSocket, language: str) -> None:
    """Energy-VAD utterances, each sent to the GPU Nemotron service.

    The GPU service returns one final transcript per utterance; there are no
    partial words, because the proxy keeps the main process torch-free.
    """
    await websocket.send_json(
        {
            "type": "ready",
            "model": "nemotron",
            "sample_rate": SAMPLE_RATE,
            "language": language,
            "chunk_ms": 1145,
            "languages": ["fa"],
        }
    )
    segmenter = Segmenter()

    async def emit(pcm: bytes) -> None:
        try:
            result = await run_in_threadpool(
                nemotron_client.transcribe_pcm, pcm, language
            )
        except Exception as error:
            await websocket.send_json(
                {"type": "error", "message": f"nemotron: {error}"}
            )
            return
        text = (result.get("text") or "").strip()
        if text:
            await websocket.send_json(
                {
                    "type": "final",
                    "text": text,
                    "duration": result.get("duration"),
                }
            )

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break

            data = message.get("bytes")
            if data:
                for kind, payload in segmenter.feed(data):
                    if kind == "start":
                        await websocket.send_json({"type": "speech"})
                    elif kind == "end":
                        assert isinstance(payload, bytes)
                        await emit(payload)
                continue

            text = message.get("text")
            if text:
                try:
                    command = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if command.get("type") == "stop":
                    break
    except WebSocketDisconnect:
        pass
    finally:
        for kind, payload in segmenter.flush():
            if kind == "end":
                assert isinstance(payload, bytes)
                await emit(payload)
        try:
            await websocket.send_json({"type": "done"})
        except Exception:
            pass


async def run_google(websocket: WebSocket, language: str) -> None:
    await websocket.send_json(
        {
            "type": "ready",
            "model": "google",
            "sample_rate": SAMPLE_RATE,
            "language": language,
        }
    )
    segmenter = Segmenter()
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break

            data = message.get("bytes")
            if not data:
                text = message.get("text")
                if text:
                    try:
                        command = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if command.get("type") == "stop":
                        break
                continue

            for kind, payload in segmenter.feed(data):
                if kind == "start":
                    await websocket.send_json({"type": "speech"})
                elif kind == "end":
                    assert isinstance(payload, bytes)
                    try:
                        text = await run_in_threadpool(
                            transcribe_pcm,
                            payload,
                            language,
                            SAMPLE_RATE,
                            SAMPLE_WIDTH,
                            2,
                        )
                    except TranscriptionError as error:
                        await websocket.send_json(
                            {"type": "error", "message": str(error)}
                        )
                        continue
                    await websocket.send_json(
                        {
                            "type": "final",
                            "text": text,
                            "duration": round(
                                len(payload) / (SAMPLE_RATE * SAMPLE_WIDTH), 2
                            ),
                        }
                    )
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.send_json({"type": "done"})
        except Exception:
            pass
