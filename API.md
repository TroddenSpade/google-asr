# HTTP API

Base URL: `http://127.0.0.1:8765`. No authentication, no jobs, no database.

Every model is addressable by id:

| id | file API | live API | notes |
|---|---|---|---|
| `tinyasr` | yes | yes | local CPU, Persian + English |
| `nemotron` | yes | yes | local GPU, Persian; live returns utterance finals |
| `google` | yes | yes | hosted, any language |
| `browser` | no | no (client-side) | Chrome Web Speech, used directly by the UI |

`nemotron` is proxied to a separate GPU service (`NEMOTRON_URL`, default
`http://127.0.0.1:8010`). Its live path uses energy VAD and therefore emits
`speech` + `final` per utterance, not partial words.

| Method | Path |
|---|---|
| `GET` | `/api/v1/models` |
| `GET` | `/api/v1/health` |
| `POST` | `/api/v1/models/{model}/transcribe` |
| `POST` | `/api/v1/models/{model}/transcribe-url` |
| `WS` | `/api/v1/models/{model}/live` |

## Models

```http
GET /api/v1/models
```

```json
{
  "models": [
    {
      "id": "tinyasr",
      "name": "tinyasr (local)",
      "kind": "local",
      "description": "Zipformer2 · Persian/English · streaming · punctuation",
      "available": true,
      "file": true,
      "live": true,
      "languages": ["en", "fa"],
      "chunk_ms": 320,
      "threads": {"live": 1, "file": 4}
    },
    {
      "id": "google",
      "name": "Google (hosted)",
      "kind": "hosted",
      "available": true,
      "file": true,
      "live": true,
      "languages": ["*"],
      "chunk_ms": null
    }
  ]
}
```

`available=false` means the local model/runtime is missing. The UI disables that
option.

Models disabled in `config.py` are not registered at all. The current config
enables only `tinyasr`. If every flag is set to `0`, the list is empty:

```json
{ "models": [] }
```

`GET /api/v1/health` then reports `"default_model": null`, and every
`/api/v1/models/{model}/...` route returns `404 Unknown model` or closes the
WebSocket with `1008`. No model is loaded into memory at startup.

## Transcribe a file

```http
POST /api/v1/models/{model}/transcribe
Content-Type: multipart/form-data
```

| Field | Required | Default | Notes |
| --- | --- | --- | --- |
| `file` | yes | — | WAV, MP3, M4A, FLAC, OGG, OPUS, WMA, AMR, MP4, and more. |
| `language` | no | `fa-IR` | BCP-47. `tinyasr` supports `fa`, `en`. |
| `chunk_seconds` | no | `40` | Google only, 5–50. Ignored by tinyasr. |

```bash
curl -s -X POST http://127.0.0.1:8765/api/v1/models/tinyasr/transcribe \
  -F "file=@voice.mp3" -F "language=fa-IR"
```

```json
{
  "ok": true,
  "model": "tinyasr",
  "filename": "voice.mp3",
  "language": "fa-IR",
  "text": "تنه می‌زنه، ناخواسته این دومیه می‌افته ته دره. بعد",
  "segments": [
    { "start": 0.24, "end": 2.36, "text": "تنه می‌زنه، ناخواسته" }
  ],
  "srt": "1\n00:00:00,240 --> 00:00:02,360\n...",
  "duration": 7.762,
  "elapsed": 0.53,
  "words": 9
}
```

`browser` returns `400` because it has no file API. An unknown model returns
`404`. A local model that does not support the requested language returns
`400`.

## Transcribe a URL

Two equivalent ways. As a form field on the same endpoint:

```bash
curl -s -X POST http://127.0.0.1:8765/api/v1/models/tinyasr/transcribe \
  -F "url=https://example.com/voice.mp3" \
  -F "language=fa-IR"
```

Or as JSON:

```http
POST /api/v1/models/{model}/transcribe-url
Content-Type: application/json
```

```json
{
  "url": "https://example.com/voice.mp3",
  "language": "fa-IR",
  "chunk_seconds": 40
}
```

The response is identical to a file upload, with `filename` taken from the URL.
The server downloads to a temporary file, transcribes it, and deletes it.

Rules:

- Only `http://` and `https://`.
- Downloads are capped at `VOICE_MAX_UPLOAD_MB` and time out after
  `VOICE_URL_TIMEOUT` seconds (default 30).
- Private, loopback, link-local and reserved addresses are rejected with
  `400`, and every redirect target is checked the same way.
- `VOICE_URL_ALLOW_HOSTS` is a comma-separated allowlist for specific internal
  hosts, e.g. `VOICE_URL_ALLOW_HOSTS=10.10.10.210,media.internal`.
- `VOICE_ALLOW_PRIVATE_URLS=1` allows every private host. Use the allowlist
  when only one internal server is needed; the service has no auth.
- A slow or unreachable host returns `504` or `400` with a `detail` message.

### Python

```python
import requests

with open("voice.mp3", "rb") as audio:
    response = requests.post(
        "http://127.0.0.1:8765/api/v1/models/tinyasr/transcribe",
        files={"file": ("voice.mp3", audio, "audio/mpeg")},
        data={"language": "fa-IR"},
        timeout=600,
    )
response.raise_for_status()
print(response.json()["text"])
```

## Live transcription

```
ws://127.0.0.1:8765/api/v1/models/{model}/live?language=fa-IR
```

Send **binary** frames of raw PCM: 16 kHz, mono, signed 16-bit little-endian.
Any frame size is accepted. The server replies with JSON:

| Type | Payload | Meaning |
| --- | --- | --- |
| `ready` | `{model, sample_rate, language, languages?, chunk_ms?}` | Session accepted. |
| `partial` | `{text, words?}` | Words not final yet; replace the previous partial. `words` are `{text, start, end}` with seconds from the start of this stream. |
| `final` | `{text, words?}` | Committed text; append it. |
| `language` | `{lang}` | tinyasr switched language. |
| `error` | `{message}` | A transcription error. |
| `done` | — | Flush finished; the server will close. |

When the speaker stops, send a text frame:

```json
{"type": "stop"}
```

The server flushes the open sentence with `final`, then sends `done`. Closing
the socket without `stop` may lose the last unfinished sentence.

```js
const ws = new WebSocket(
  `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}` +
  `/api/v1/models/tinyasr/live?language=fa-IR`
);
ws.binaryType = "arraybuffer";

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.type === "partial") console.log("…", message.text);
  if (message.type === "final") console.log(message.text);
};

ws.onopen = () => {
  // pcm16k: Int16Array of 16 kHz mono samples
  ws.send(pcm16k.buffer);
};

// on stop:
ws.send(JSON.stringify({ type: "stop" }));
```

`google` uses energy VAD and returns `final` per utterance plus `speech` when
speech starts. `tinyasr` uses its own VAD and returns partials every ~320 ms.

## Health

```http
GET /api/v1/health
```

```json
{
  "ok": true,
  "default_model": "tinyasr",
  "models": [ ... ],
  "default_language": "fa-IR",
  "max_upload_mb": 500,
  "allow_private_urls": false,
  "url_allow_hosts": ["10.10.10.210"],
  "ffmpeg": "ffmpeg-linux-x86_64-v9.0.2"
}
```

## Errors

Errors are JSON with a `detail` string:

```json
{ "detail": "The local tinyasr model is unavailable or does not support ar-SA." }
```

| Status | Meaning |
| --- | --- |
| `400` | Model has no file API, bad language, bad `chunk_seconds`, unsupported file type. |
| `404` | Unknown model. |
| `413` | Upload or URL download exceeds `VOICE_MAX_UPLOAD_MB`. |
| `422` | Decode or transcription failure. |
| `503` | Local runtime failed to load. |
| `504` | The URL download timed out. |

WebSocket errors close with `1008` (bad model/language) or `1011` (model
unavailable), after an `error` event when possible.

## Deployment notes

- Microphone access requires HTTPS or `localhost`.
- Set `VOICE_HOST=0.0.0.0` for LAN, and put TLS in front of the service.
- One uvicorn process per GPU/CPU budget. No shared state; scale horizontally.
- Uploads are deleted after each request. Live audio is never written.
