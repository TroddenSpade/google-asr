# Voice Typing ASR

A small FastAPI service with two things:

- **Live dictation** — two engines: Chrome's Web Speech API (client-side, with
  interim words) or a server WebSocket that cuts audio into utterances.
- **File transcription** — upload an audio/video file over HTTP and get text,
  timed segments and optional SRT.

Both use Google's free speech endpoint through `SpeechRecognition`. There is no
database, no job queue, no auth, and no stored audio.

## Run

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Then open <http://127.0.0.1:8765>. On Windows, `run.bat` does the setup and
starts the app.

Environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `VOICE_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` for LAN access. |
| `VOICE_PORT` | `8765` | HTTP port. |
| `VOICE_MAX_UPLOAD_MB` | `500` | Upload limit per file. |

Live dictation needs a **secure context**: `http://127.0.0.1`, `http://localhost`,
or HTTPS. A plain `http://` LAN address will not get microphone access from the
browser.

## API

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/v1/transcribe` | Multipart upload: `file`, optional `language` (`fa-IR`), `chunk_seconds` (5–50). |
| `WS` | `/api/v1/live?language=fa-IR` | Send 16 kHz mono signed 16-bit PCM; receive JSON text frames. |
| `GET` | `/api/v1/health` | Service status and ffmpeg check. |

Interactive docs are at `/docs`. Full examples are in [API.md](API.md).

```bash
curl -s -X POST http://127.0.0.1:8765/api/v1/transcribe \
  -F "file=@voice.mp3" \
  -F "language=fa-IR"
```

The response contains `text`, `srt`, `segments`, `duration` and `elapsed`.

## How file transcription works

1. `ffmpeg` decodes the upload to 16 kHz mono 16-bit WAV.
2. `silencedetect` finds pauses; the audio is cut near them into chunks of
   roughly 40 seconds (50 maximum).
3. Each chunk is sent to Google's free endpoint, with retries and backoff.
4. Chunk texts are joined; SRT timestamps come from the chunk boundaries.

The uploaded file is deleted as soon as the request finishes.

## How live transcription works

The **Engine** selector chooses between two paths. Both end at Google's speech
service; they differ in where the audio goes first.

### Browser (Chrome)

The page uses `window.SpeechRecognition`, sends microphone audio from the
browser straight to Google, and receives interim and final hypotheses. Text
appears as you speak, and no audio touches this server.

- Chrome only for the Google backend (Edge uses Azure).
- The session restarts automatically after a pause.
- Needs HTTPS or localhost.
- Not available in Firefox/Safari without a recognizer; the option is disabled.

### Server (FastAPI)

The browser captures the mic with `getUserMedia`, resamples to 16 kHz, and sends
binary PCM frames over `/api/v1/live`. The server runs a simple energy VAD:

- speech starts above 400 RMS;
- a 300 ms pre-roll keeps the first phoneme;
- an utterance ends after 600 ms below 250 RMS, or at 20 seconds;
- each finished utterance is transcribed and returned as
  `{"type": "final", "text": "...", "duration": 2.4}`.

There are no interim words — the free endpoint returns one result per request.
Utterances are transcribed one at a time, so a long monologue can queue a little.
This mode works in any browser with microphone support and keeps a single
FastAPI contract for other clients.

## Limits

- The Google endpoint is **unofficial**: no API key, no SLA, no punctuation, and
  it may rate-limit or change.
- Accuracy is best with clean, single-speaker audio. Telephone-band, noisy,
  overlapping speech is harder.
- No speaker diarization, no word-level timestamps.
- Audio is sent to Google's speech service. Delete nothing — nothing is kept
  locally, but the request itself leaves the machine.
- For production, swap `transcribe_chunk` / `transcribe_pcm` in
  `transcriber.py` for Google Cloud Speech-to-Text or a local Whisper model.
  The API and UI do not need to change.

## Layout

```
app.py           FastAPI app: upload route, health, static UI
live.py          WebSocket route, energy VAD, utterance segmentation
transcriber.py   engine: ffmpeg, silence-aware chunking, Google endpoint, SRT
web/             UI (index.html, app.js, styles.css)
requirements.txt fastapi, uvicorn, SpeechRecognition, bundled ffmpeg
run.bat          one-click Windows launcher
```
