"""Nemotron streaming Farsi as a small local HTTP service.

Runs with the GPU environment (torch + transformers 5.x). The main ASR app
proxies file and live requests here so it does not need torch in its own venv.

    NEMOTRON_MODEL=/path/to/model \
    PYTHONPATH=/path/to/tf518 \
    /path/to/python nemotron_service.py

POST /transcribe?language=fa-IR   body: raw PCM, 16 kHz mono s16le
GET  /health
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool
from transformers import AutoModelForRNNT, AutoProcessor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("nemotron")

ROOT = Path(__file__).resolve().parent
MODEL_DIR = Path(os.environ.get("NEMOTRON_MODEL", ROOT / "models" / "nemotron"))
LOOKAHEAD = int(os.environ.get("NEMOTRON_LOOKAHEAD", "13"))
DEVICE = os.environ.get("NEMOTRON_DEVICE", "cuda")
SAMPLE_RATE = 16000
VALID_LANGUAGES = {"fa", "fa-ir", "auto"}


class Engine:
    def __init__(self) -> None:
        self.processor = None
        self.model = None
        self.lock = threading.Lock()
        self.loaded = False
        self.error = ""
        self.load_seconds = 0.0
        self.pad = None

    def load(self) -> None:
        started = time.monotonic()
        try:
            self.processor = AutoProcessor.from_pretrained(str(MODEL_DIR))
            self.model = AutoModelForRNNT.from_pretrained(
                str(MODEL_DIR), dtype=torch.float16
            )
            self.model.to(DEVICE)
            self.model.eval()
            self.processor.set_num_lookahead_tokens(LOOKAHEAD)
            self.sample_rate = int(self.processor.feature_extractor.sampling_rate)
            self.pad = np.zeros(
                2 * int(self.processor.num_samples_per_audio_chunk), dtype=np.float32
            )
            self.loaded = True
            self.load_seconds = round(time.monotonic() - started, 3)
            logger.info(
                "nemotron loaded device=%s dtype=%s load=%.2fs lookahead=%s",
                DEVICE,
                self.model.dtype,
                self.load_seconds,
                LOOKAHEAD,
            )
        except Exception as error:
            self.error = str(error)
            logger.exception("nemotron failed to load")

    def transcribe_pcm(self, pcm: bytes, language: str = "fa-IR") -> dict:
        if not self.loaded or self.model is None or self.processor is None:
            raise RuntimeError(self.error or "nemotron model is not loaded")
        if language.lower() not in VALID_LANGUAGES:
            raise ValueError("nemotron supports Persian (fa / fa-IR) only")

        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if audio.size == 0:
            return {"text": "", "duration": 0.0, "elapsed": 0.0}

        padded = np.concatenate([audio, self.pad])
        inputs = self.processor(
            padded,
            sampling_rate=self.sample_rate,
            language="fa-IR",
            return_tensors="pt",
        ).to(self.model.device, dtype=self.model.dtype)

        if torch.cuda.is_available():
            torch.cuda.synchronize()
        started = time.monotonic()
        with self.lock, torch.inference_mode():
            output = self.model.generate(**inputs, return_dict_in_generate=True)
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        elapsed = time.monotonic() - started
        text = self.processor.decode(output.sequences, skip_special_tokens=True)[0]
        return {
            "text": text,
            "duration": round(audio.size / self.sample_rate, 3),
            "elapsed": round(elapsed, 2),
        }

    def info(self) -> dict:
        if not self.loaded:
            return {
                "ok": False,
                "loaded": False,
                "error": self.error or "not loaded",
                "model": str(MODEL_DIR),
            }
        return {
            "ok": True,
            "loaded": True,
            "model": str(MODEL_DIR),
            "device": DEVICE,
            "dtype": str(self.model.dtype),
            "lookahead_tokens": LOOKAHEAD,
            "sample_rate": self.sample_rate,
            "chunk_s": round(
                self.processor.num_samples_per_audio_chunk / self.sample_rate, 3
            ),
            "load_s": self.load_seconds,
        }


engine = Engine()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await run_in_threadpool(engine.load)
    yield


app = FastAPI(title="Nemotron Farsi ASR", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return engine.info()


@app.post("/transcribe")
async def transcribe(
    request: Request,
    language: str = Query("fa-IR"),
) -> dict:
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty PCM body.")
    if not engine.loaded:
        raise HTTPException(
            status_code=503, detail=engine.error or "Nemotron is not loaded."
        )
    try:
        return await run_in_threadpool(engine.transcribe_pcm, body, language)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def main() -> None:
    host = os.environ.get("NEMOTRON_HOST", "127.0.0.1")
    port = int(os.environ.get("NEMOTRON_PORT", "8010"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
