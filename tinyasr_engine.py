"""ctypes wrapper for tinyasr, the on-device ASR stack used by Telegram clients.

Model and runtime: https://github.com/dkaraush/tinyasr (GPL-3.0-or-later).

Audio in is mono 16 kHz signed 16-bit PCM. The model returns punctuated text
with word timestamps. This module is optional: it is not imported by app.py.

    python tinyasr_engine.py sample.wav --lang fa
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import wave
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_LIB = ROOT / "models" / "libtinyasr.so"
DEFAULT_MODEL = ROOT / "models" / "en-fa.tzf"
DEFAULT_VAD = ROOT / "models" / "silero_vad.onnx"

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2


def _float_pcm(pcm16: bytes) -> ctypes.Array:
    samples = array("h")
    samples.frombytes(pcm16)
    return (ctypes.c_float * len(samples))(*(value / 32768.0 for value in samples))


class TinyASR:
    """One loaded model; sessions/files can share it."""

    def __init__(
        self,
        lib_path: str | Path = DEFAULT_LIB,
        model_path: str | Path = DEFAULT_MODEL,
        vad_path: str | Path = DEFAULT_VAD,
        threads: int = 1,
    ) -> None:
        self.lib = ctypes.CDLL(str(lib_path))
        self._bind()

        self._asr = Path(model_path).read_bytes()
        self._vad = Path(vad_path).read_bytes()
        error = ctypes.create_string_buffer(512)
        self._models = self.lib.tinyasr_load(
            self._asr,
            len(self._asr),
            self._vad,
            len(self._vad),
            error,
            len(error),
        )
        if not self._models:
            raise RuntimeError(
                error.value.decode("utf-8", "replace") or "tinyasr_load failed"
            )

        self.threads = max(1, int(threads))
        self.lib.tinyasr_set_threads(self._models, self.threads)

    def _bind(self) -> None:
        lib = self.lib
        lib.tinyasr_version.restype = ctypes.c_uint32

        lib.tinyasr_load.restype = ctypes.c_void_p
        lib.tinyasr_load.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_char_p, ctypes.c_size_t,
        ]
        lib.tinyasr_free.argtypes = [ctypes.c_void_p]

        lib.tinyasr_chunk_ms.restype = ctypes.c_uint32
        lib.tinyasr_chunk_ms.argtypes = [ctypes.c_void_p]
        lib.tinyasr_set_threads.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.tinyasr_lang_count.restype = ctypes.c_uint32
        lib.tinyasr_lang_count.argtypes = [ctypes.c_void_p]
        lib.tinyasr_lang.restype = ctypes.c_char_p
        lib.tinyasr_lang.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

        lib.tinyasr_file_new.restype = ctypes.c_void_p
        lib.tinyasr_file_new.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_size_t,
            ctypes.c_char_p, ctypes.c_uint32,
        ]
        lib.tinyasr_file_next.restype = ctypes.c_char_p
        lib.tinyasr_file_next.argtypes = [ctypes.c_void_p]
        lib.tinyasr_file_progress.restype = ctypes.c_float
        lib.tinyasr_file_progress.argtypes = [ctypes.c_void_p]
        lib.tinyasr_file_free.argtypes = [ctypes.c_void_p]

        lib.tinyasr_session_new.restype = ctypes.c_void_p
        lib.tinyasr_session_new.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_void_p, ctypes.c_uint32,
        ]
        lib.tinyasr_session_push.restype = ctypes.c_char_p
        lib.tinyasr_session_push.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_size_t,
        ]
        lib.tinyasr_session_finish.restype = ctypes.c_char_p
        lib.tinyasr_session_finish.argtypes = [ctypes.c_void_p]
        lib.tinyasr_session_free.argtypes = [ctypes.c_void_p]

    def __del__(self) -> None:
        models = getattr(self, "_models", None)
        if models:
            self.lib.tinyasr_free(models)
            self._models = None

    @property
    def chunk_ms(self) -> int:
        return int(self.lib.tinyasr_chunk_ms(self._models))

    def languages(self) -> list[str]:
        count = self.lib.tinyasr_lang_count(self._models)
        return [
            self.lib.tinyasr_lang(self._models, index).decode("utf-8")
            for index in range(count)
        ]

    def transcribe_pcm(
        self, pcm16: bytes, language: str = "fa", beam: int = 4
    ) -> dict:
        """Full-context pass over one utterance (or file-sized buffer)."""
        pcm = _float_pcm(pcm16)
        handle = self.lib.tinyasr_file_new(
            self._models, pcm, len(pcm), language.encode(), beam
        )
        if not handle:
            return {"text": "", "segments": []}

        segments: list[dict] = []
        try:
            while raw := self.lib.tinyasr_file_next(handle):
                segments.extend(json.loads(raw.decode("utf-8")))
        finally:
            self.lib.tinyasr_file_free(handle)

        return {
            "text": " ".join(segment["text"] for segment in segments).strip(),
            "segments": segments,
        }

    def transcribe_file(self, path: str | Path, language: str = "fa", beam: int = 4) -> dict:
        with wave.open(str(path), "rb") as reader:
            if (
                reader.getnchannels() != 1
                or reader.getsampwidth() != SAMPLE_WIDTH
                or reader.getframerate() != SAMPLE_RATE
            ):
                raise ValueError("expected mono 16 kHz 16-bit WAV")
            pcm16 = reader.readframes(reader.getnframes())

        result = self.transcribe_pcm(pcm16, language=language, beam=beam)
        result["duration"] = len(pcm16) / (SAMPLE_RATE * SAMPLE_WIDTH)
        return result

    def open_session(self, languages: str = "fa", beam: int = 4) -> "TinyASRSession":
        return TinyASRSession(self, languages=languages, beam=beam)


class TinyASRSession:
    """Streaming session: push PCM, get commit/partial events."""

    def __init__(self, engine: TinyASR, languages: str = "fa", beam: int = 4) -> None:
        self.engine = engine
        self._session = engine.lib.tinyasr_session_new(
            engine._models, languages.encode(), None, None, beam
        )
        if not self._session:
            raise RuntimeError("tinyasr_session_new failed")

    def push(self, pcm16: bytes) -> list[dict]:
        pcm = _float_pcm(pcm16)
        raw = self.engine.lib.tinyasr_session_push(self._session, pcm, len(pcm))
        return json.loads(raw.decode("utf-8")) if raw else []

    def finish(self) -> list[dict]:
        raw = self.engine.lib.tinyasr_session_finish(self._session)
        return json.loads(raw.decode("utf-8")) if raw else []

    def close(self) -> None:
        if self._session:
            self.engine.lib.tinyasr_session_free(self._session)
            self._session = None

    def __enter__(self) -> "TinyASRSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", help="mono 16 kHz 16-bit WAV")
    parser.add_argument("--lang", default="fa")
    parser.add_argument("--beam", type=int, default=4)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    engine = TinyASR(threads=args.threads)
    print(f"tinyasr v{engine.lib.tinyasr_version()} · {engine.languages()} · "
          f"{engine.chunk_ms} ms chunks", file=sys.stderr)
    result = engine.transcribe_file(args.audio, language=args.lang, beam=args.beam)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
