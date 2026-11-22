# HTTP API

Base URL: `http://127.0.0.1:8765`. No authentication, no jobs, no database.
Uploads are processed synchronously and deleted afterwards.

## Health

```http
GET /api/v1/health
```

```json
{
  "ok": true,
  "default_language": "fa-IR",
  "max_upload_mb": 500,
  "ffmpeg": "ffmpeg-linux-x86_64-v7.0.2"
}
```

## Transcribe a file

```http
POST /api/v1/transcribe
Content-Type: multipart/form-data
```

| Field | Required | Default | Notes |
| --- | --- | --- | --- |
| `file` | yes | — | WAV, MP3, M4A, FLAC, OGG, OPUS, WMA, AMR, MP4, and more. |
| `language` | no | `fa-IR` | BCP-47 code. |
| `chunk_seconds` | no | `40` | Seconds per Google request, 5–50. |

```bash
curl -s -X POST http://127.0.0.1:8765/api/v1/transcribe \
  -F "file=@voice.mp3" \
  -F "language=fa-IR" \
  -F "chunk_seconds=40"
```

```json
{
  "ok": true,
  "filename": "voice.mp3",
  "source": "voice.mp3",
  "language": "fa-IR",
  "duration": 56.808,
  "elapsed": 4.2,
  "text": "سلام روز بخیر ...",
  "srt": "1\n00:00:00,000 --> 00:00:38,888\n...",
  "segments": [
    { "start": 0.0, "end": 38.888, "text": "..." },
    { "start": 38.888, "end": 56.808, "text": "..." }
  ]
}
```

### Python client

```python
import requests

with open("voice.mp3", "rb") as audio:
    response = requests.post(
        "http://127.0.0.1:8765/api/v1/transcribe",
        files={"file": ("voice.mp3", audio, "audio/mpeg")},
        data={"language": "fa-IR"},
        timeout=600,
    )
response.raise_for_status()
print(response.json()["text"])
```

### JavaScript client

```js
const form = new FormData();
form.append("file", file);
form.append("language", "fa-IR");

const response = await fetch("/api/v1/transcribe", { method: "POST", body: form });
if (!response.ok) throw new Error((await response.json()).detail);
const { text, srt, segments } = await response.json();
```

## Live transcription

```
ws://127.0.0.1:8765/api/v1/live?language=fa-IR
```

The bundled UI can also use Chrome's Web Speech API (**Engine → Browser**),
which bypasses this endpoint and sends microphone audio to Google from the
browser. Use the WebSocket route when you want a single server contract, any
browser, or your own clients.

Send **binary** frames of raw PCM: 16 kHz, mono, signed 16-bit little-endian.
Any frame size is accepted; the server re-frames internally. Text frames from
the server are JSON:

| Type | Payload | Meaning |
| --- | --- | --- |
| `ready` | `{sample_rate, language}` | Session accepted. |
| `speech` | — | Speech started; the UI can show activity. |
| `final` | `{text, duration}` | One utterance finished. `text` may be empty. |
| `error` | `{message}` | Transcription of one utterance failed. |

The socket is stateless: closing it discards the buffer. Nothing is stored.

```js
const ws = new WebSocket(
  `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/v1/live?language=fa-IR`
);
ws.binaryType = "arraybuffer";

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.type === "final" && message.text) console.log(message.text);
};

ws.onopen = () => {
  // pcm16k is an Int16Array of microphone samples at 16 kHz
  ws.send(pcm16k.buffer);
};
```

A minimal Python client using `websockets` and `sounddevice`:

```python
import asyncio, json
import sounddevice as sd
import websockets

async def main():
    async with websockets.connect(
        "ws://127.0.0.1:8765/api/v1/live?language=fa-IR"
    ) as ws:
        print(json.loads(await ws.recv()))  # ready

        async def send():
            with sd.RawInputStream(samplerate=16000, channels=1, dtype="int16") as mic:
                while True:
                    data, _ = mic.read(1600)          # 100 ms
                    await ws.send(bytes(data))

        async def receive():
            async for raw in ws:
                message = json.loads(raw)
                if message["type"] == "final" and message["text"]:
                    print(message["text"])

        await asyncio.gather(send(), receive())

asyncio.run(main())
```

## Errors

Errors are JSON with a `detail` string:

```json
{ "detail": "chunk_seconds must be a number between 5 and 50." }
```

| Status | Meaning |
| --- | --- |
| `400` | Bad language code, bad `chunk_seconds`, unsupported file type. |
| `413` | Upload exceeds `VOICE_MAX_UPLOAD_MB`. |
| `422` | Transcription failed (decode error, Google error, rate limit). |
| `500` | Unexpected server failure. |

## Deployment notes

- For microphone access the page must be served over **HTTPS** or from
  `localhost` / `127.0.0.1`. Browsers block `getUserMedia` on plain HTTP LAN
  addresses.
- Set `VOICE_HOST=0.0.0.0` to accept LAN connections, and put the app behind a
  reverse proxy (Caddy, nginx) for TLS.
- There is no built-in auth or rate limiting; the Google endpoint has its own
  limits. Keep concurrency low.
