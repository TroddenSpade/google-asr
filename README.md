# Voice Typing ASR

FastAPI service and UI where **every model has two APIs**: one for files and one
for live audio.

| Model | Kind | File | Live | Notes |
|---|---|---|---|---|
| `tinyasr` | local CPU | yes | yes | Zipformer2, Persian + English, punctuation, 320 ms streaming |
| `nemotron` | local GPU | yes | yes | FastConformer RNNT, Persian, 1.12 s look-ahead; utterance-level live finals |
| `google` | hosted | yes | yes | Google's free endpoint, any language, unpunctuated |
| `browser` | client | no | yes | Chrome Web Speech API, live only, audio goes to Google |

The UI has one model selector; both the live panel and the file panel use it.
`tinyasr` is selected by default when its model files are present.

## Model toggles

Models are enabled in [`config.py`](config.py):

```python
MODEL_FLAGS = {
    "tinyasr": 1,
    "nemotron": 0,
    "google": 0,
    "browser": 0,
}
```

`1` enables, `0` disables. A disabled model is **not registered in the API and
is not loaded at startup** — Nemotron is not loaded and its GPU service is not
contacted. The current file enables only `tinyasr`, so `GET /api/v1/models`
returns just that model. Restart the service after changing the flags.

The separate Nemotron GPU service (`nemotron_service.py`) must also be stopped
when the `nemotron` flag is `0`; the main app will not start it.

## Run

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python fetch_tinyasr.py --runtime  # optional: local model + native runtime
python app.py
```

Open <http://127.0.0.1:8765>. `--runtime` needs Rust (`cargo`) and clones
[`dkaraush/tinyasr`](https://github.com/dkaraush/tinyasr). Without the local
model the service still runs and the UI falls back to Google.

Environment:

| Variable | Default | Meaning |
| --- | --- | --- |
| `VOICE_HOST` | `127.0.0.1` | Bind address. `0.0.0.0` for LAN. |
| `VOICE_PORT` | `8765` | HTTP port. |
| `VOICE_MAX_UPLOAD_MB` | `500` | Upload/download limit per file. |
| `VOICE_URL_TIMEOUT` | `30` | Seconds allowed for a remote URL download. |
| `VOICE_ALLOW_PRIVATE_URLS` | off | Set `1` to allow all private/loopback URL downloads. |
| `VOICE_URL_ALLOW_HOSTS` | empty | Comma-separated hosts allowed despite being private, e.g. `10.10.10.210`. |
| `VOICE_DISABLE_TINYASR` | off | Set `1` to hide the local model. |
| `TINYASR_MODEL` | `models/en-fa.tzf` | Local model file. |
| `TINYASR_VAD` | `models/silero_vad.onnx` | VAD model. |
| `TINYASR_LIB` | `models/libtinyasr.so` | Native runtime. |
| `TINYASR_LIVE_THREADS` | `1` | Threads per live model; keep at 1 when many sessions share it. |
| `TINYASR_FILE_THREADS` | `4` | Threads for file jobs; 4 is ~2.2× faster than 1. |
| `NEMOTRON_URL` | `http://127.0.0.1:8010` | Internal GPU service for the Nemotron model. |

Live dictation needs a secure context: `http://127.0.0.1`,
`http://localhost`, or HTTPS.

## API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/v1/models` | Models, capabilities, languages, availability. |
| `GET` | `/api/v1/health` | Service, ffmpeg and model status. |
| `POST` | `/api/v1/models/{model}/transcribe` | Multipart `file` **or** `url`, optional `language`, `chunk_seconds`. |
| `POST` | `/api/v1/models/{model}/transcribe-url` | JSON `{url, language, chunk_seconds}`. |
| `WS` | `/api/v1/models/{model}/live?language=fa-IR` | Send 16 kHz mono s16le PCM; receive JSON. |

```bash
# List models
curl -s http://127.0.0.1:8765/api/v1/models

# Transcribe a file
curl -s -X POST http://127.0.0.1:8765/api/v1/models/tinyasr/transcribe \
  -F "file=@voice.mp3" -F "language=fa-IR"

# Same file through Google
curl -s -X POST http://127.0.0.1:8765/api/v1/models/google/transcribe \
  -F "file=@voice.mp3" -F "language=fa-IR"

# A remote link, as a form field
curl -s -X POST http://127.0.0.1:8765/api/v1/models/tinyasr/transcribe \
  -F "url=https://example.com/voice.mp3" -F "language=fa-IR"

# A remote link, as JSON
curl -s -X POST http://127.0.0.1:8765/api/v1/models/tinyasr/transcribe-url \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/voice.mp3","language":"fa-IR"}'
```

URL downloads are capped by `VOICE_MAX_UPLOAD_MB` and time out after
`VOICE_URL_TIMEOUT`. Private, loopback and link-local hosts are rejected with
`400` unless `VOICE_ALLOW_PRIVATE_URLS=1`; redirects are re-checked the same
way. Set that switch only on a trusted network, since the service has no auth.
The UI has a **Transcribe link** box that uses the same endpoint.

The file response is the same for every model:

```json
{
  "ok": true,
  "model": "tinyasr",
  "language": "fa-IR",
  "text": "تنه می‌زنه، ناخواسته ...",
  "segments": [{"start": 0.24, "end": 2.36, "text": "..."}],
  "srt": "1\n00:00:00,240 --> 00:00:02,360\n...",
  "duration": 7.762,
  "elapsed": 0.53
}
```

Live protocol (identical for every server model):

```json
{"type":"ready","model":"tinyasr","sample_rate":16000,"language":"fa-IR","chunk_ms":320,"languages":["en","fa"]}
{"type":"partial","text":"..."}
{"type":"final","text":"...","words":[...]}
{"type":"done"}
```

The client sends binary PCM and, when finished, `{"type":"stop"}`. The server
flushes the open sentence and sends `final` + `done`. Full details in
[API.md](API.md); interactive docs at `/docs`.

## Nemotron GPU service

Nemotron runs in a **separate process** so the main app stays torch-free and
the GPU model can be restarted independently.

```bash
# GPU environment (torch + transformers 5.x)
PYTHONPATH=/path/to/tf518 \
NEMOTRON_MODEL=/path/to/model \
NEMOTRON_PORT=8010 \
/path/to/python nemotron_service.py

# main app
NEMOTRON_URL=http://127.0.0.1:8010 python app.py
```

The service exposes `POST /transcribe?language=fa-IR` (raw 16 kHz mono s16le
PCM) and `GET /health`. The main app downloads uploads/URLs, converts to 16 kHz,
and proxies the PCM. Live mode uses energy VAD and returns one final transcript
per utterance — there are no partial words. If the service is down, the model
shows as unavailable in the UI.

Measured on the A100 80GB: 7.76 s clip in 0.16–0.18 s after warm-up; 134.1 s
call in 1.76–2.68 s, ~2 GB GPU memory. See the benchmark caveats below.

## How it works

**tinyasr (files).** ffmpeg decodes any upload to 16 kHz mono WAV; the model
runs a full-context pass (beam 4) and returns punctuated words with timestamps.
Words are grouped into subtitle lines by pause/length. No network, no rate
limit.

**tinyasr (live).** One streaming session per WebSocket. The model's own VAD
and end-of-turn head replace the energy VAD; every 320 ms the server forwards
`partial` text and committed `final` sentences.

**Threading.** `tinyasr_set_threads` splits the encoder steps. On the 64-core
host, a 134 s call decodes in 5.6 s with 1 thread, 3.5 s with 4, and 2.6 s with
8. The service keeps a separate one-thread live engine (many sessions) and a
4-thread file engine (few jobs), so live latency does not compete with files.

**google.** For files: ffmpeg → silence-aware chunks of ~40 s → hosted endpoint.
For live: energy VAD → one request per utterance. Unpunctuated, rate-limited,
and audio leaves the machine.

**browser.** `SpeechRecognition` in Chrome; the server is not involved. Live
only, because the browser cannot recognize a file.

## Persistence

None. Uploads and URL downloads are written to a temporary directory and
deleted when the request finishes. Live audio is processed in memory. There is
no database, queue, or auth.

## tinyasr licence

`tinyasr` and its models are **GPL-3.0-or-later**. Using it internally is fine;
distributing an app that bundles the runtime or models requires releasing that
app's source. `models/` is gitignored and recreated by
`python fetch_tinyasr.py --runtime`.

## Layout

```
app.py               model registry and the two routes
live.py              live handlers (tinyasr, Nemotron, Google VAD)
asr_service.py       tinyasr file path, word grouping, model loading
tinyasr_engine.py    ctypes wrapper for libtinyasr.so
nemotron_service.py  separate GPU service (torch + transformers)
nemotron_client.py   HTTP proxy used by the main app
fetch_tinyasr.py     download models, optionally build the runtime
transcriber.py       ffmpeg, chunking, Google endpoint, SRT
web/                 UI (model selector, live, files)
models/              local weights and runtime (gitignored)
```
