"""Live transcription over a WebSocket.

The browser sends raw PCM (16 kHz, mono, signed 16-bit little-endian) as
binary frames. This module cuts the stream into utterances with a simple
energy VAD and sends each finished utterance to the same Google speech
endpoint the file pipeline uses.

Protocol (JSON text frames from the server):

    {"type": "ready", "sample_rate": 16000, "language": "fa-IR"}
    {"type": "speech"}                                  speech started
    {"type": "final", "text": "...", "duration": 2.4}   utterance done
    {"type": "error", "message": "..."}                 transcription failed

The client does not need to send anything but binary audio. Closing the
socket ends the session; nothing is stored.
"""

from __future__ import annotations

import math
import re
from array import array
from collections import deque

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from transcriber import DEFAULT_LANGUAGE, TranscriptionError, transcribe_pcm

router = APIRouter()

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH

# 16-bit full scale is 32768; speech on a phone line sits around 1000-8000.
START_RMS = 400.0
STOP_RMS = 250.0
SILENCE_MS = 600
PREROLL_FRAMES = 300 // FRAME_MS
MIN_SPEECH_MS = 400
MAX_SPEECH_MS = 20000

LANGUAGE_PATTERN = re.compile(r"[a-z]{2,3}(?:-[A-Za-z]{2,4})?")


def _rms(frame: bytes) -> float:
    samples = array("h")
    samples.frombytes(frame)
    if not samples:
        return 0.0
    return math.sqrt(sum(value * value for value in samples) / len(samples))


class Segmenter:
    """Split a continuous PCM stream into utterances.

    Frames before speech starts are kept as a short pre-roll so the first
    phoneme is not clipped. An utterance ends after ``SILENCE_MS`` below
    ``STOP_RMS``, or when it reaches ``MAX_SPEECH_MS``.
    """

    def __init__(self) -> None:
        self._pending = bytearray()
        self._preroll: deque[bytes] = deque(maxlen=PREROLL_FRAMES)
        self._speech: list[bytes] = []
        self._in_speech = False
        self._silence_ms = 0

    def feed(self, data: bytes) -> list[tuple[str, bytes | int]]:
        """Return ``("start", ms)``, ``("end", pcm)`` and ``("empty", 0)``."""
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


@router.websocket("/api/v1/live")
async def live(websocket: WebSocket, language: str = DEFAULT_LANGUAGE) -> None:
    if not LANGUAGE_PATTERN.fullmatch(language):
        await websocket.close(code=1008, reason="Invalid language code")
        return

    await websocket.accept()
    await websocket.send_json(
        {"type": "ready", "sample_rate": SAMPLE_RATE, "language": language}
    )

    segmenter = Segmenter()
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break

            data = message.get("bytes")
            if not data:
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
