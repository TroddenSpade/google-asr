"""Speech-to-text engine shared by the upload route and the live socket.

Audio is decoded with a bundled ffmpeg, converted to 16 kHz mono WAV,
split at natural pauses into chunks that fit Google's free speech
endpoint, and transcribed with SpeechRecognition's ``recognize_google``.

This uses the same Google speech backend that powers voice typing in
Chrome/Gboard online dictation. It is an unofficial, free endpoint: it
has no SLA, strips punctuation, and may rate-limit heavy use.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import speech_recognition as sr

DEFAULT_LANGUAGE = "fa-IR"
DEFAULT_CHUNK_SECONDS = 40.0
MAX_CHUNK_SECONDS = 50.0
MIN_CHUNK_SECONDS = 6.0

AUDIO_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".m4a",
    ".mp4",
    ".aac",
    ".flac",
    ".ogg",
    ".oga",
    ".opus",
    ".wma",
    ".webm",
    ".amr",
    ".3gp",
    ".aiff",
    ".aif",
    ".caf",
    ".m4b",
    ".mkv",
    ".mov",
}

ProgressCallback = Callable[[str, int, int], None]

_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")

_ffmpeg_cache: str | None = None


class TranscriptionError(RuntimeError):
    """A user-facing transcription failure."""


@dataclass
class Segment:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
        }


@dataclass
class Transcript:
    source: str
    language: str
    duration: float
    segments: list[Segment] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def text(self) -> str:
        return " ".join(
            segment.text.strip() for segment in self.segments if segment.text.strip()
        )

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "language": self.language,
            "duration": round(self.duration, 3),
            "elapsed": round(self.elapsed, 2),
            "text": self.text,
            "segments": [segment.to_dict() for segment in self.segments],
        }


def is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def ffmpeg_path() -> str:
    """Return a usable ffmpeg executable, preferring the pip-bundled one."""
    global _ffmpeg_cache
    if _ffmpeg_cache:
        return _ffmpeg_cache

    try:
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        if executable and Path(executable).exists():
            _ffmpeg_cache = executable
            return executable
    except Exception:
        pass

    executable = shutil.which("ffmpeg")
    if executable:
        _ffmpeg_cache = executable
        return executable

    raise TranscriptionError(
        "ffmpeg was not found. Run: pip install -r requirements.txt "
        "(this installs a bundled ffmpeg), or put ffmpeg on your PATH."
    )


def _run_ffmpeg(
    args: list[str], loglevel: str = "error"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-loglevel", loglevel, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def convert_to_wav(source: Path, destination: Path) -> None:
    """Decode any supported input into 16 kHz mono 16-bit PCM WAV."""
    process = _run_ffmpeg(
        [
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            "-f",
            "wav",
            str(destination),
        ]
    )
    if process.returncode != 0 or not destination.exists():
        lines = [line for line in (process.stderr or "").splitlines() if line.strip()]
        detail = lines[-1] if lines else "unknown ffmpeg error"
        raise TranscriptionError(f"Could not decode {source.name}: {detail}")


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as reader:
        return reader.getnframes() / float(reader.getframerate())


def detect_silences(
    wav_path: Path, noise_db: int = -35, min_silence: float = 0.35
) -> list[tuple[float, float]]:
    """Find quiet stretches so chunks can be cut between words."""
    # silencedetect reports at info level, so this call cannot use -loglevel error.
    process = _run_ffmpeg(
        [
            "-nostats",
            "-i",
            str(wav_path),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_silence}",
            "-f",
            "null",
            "-",
        ],
        loglevel="info",
    )

    intervals: list[tuple[float, float]] = []
    start: float | None = None
    for line in (process.stderr or "").splitlines():
        match = _SILENCE_START.search(line)
        if match:
            start = max(0.0, float(match.group(1)))
            continue
        match = _SILENCE_END.search(line)
        if match and start is not None:
            intervals.append((start, float(match.group(1))))
            start = None
    return intervals


def plan_cuts(
    duration: float,
    silences: list[tuple[float, float]],
    target: float = DEFAULT_CHUNK_SECONDS,
    max_chunk: float = MAX_CHUNK_SECONDS,
    min_chunk: float = MIN_CHUNK_SECONDS,
) -> list[float]:
    """Return chunk boundaries, preferring a pause near the target length."""
    if duration <= max_chunk:
        return [0.0, round(duration, 3)]

    target = max(min_chunk, min(target, max_chunk - 1.0))
    cuts = [0.0]
    position = 0.0

    while duration - position > max_chunk:
        ideal = position + target
        low = position + max(min_chunk, target * 0.6)
        high = min(position + max_chunk, duration - min_chunk)

        best: tuple[float, float] | None = None
        for silence_start, silence_end in silences:
            midpoint = (silence_start + silence_end) / 2
            if low <= midpoint <= high:
                distance = abs(midpoint - ideal)
                if best is None or distance < best[0]:
                    best = (distance, midpoint)

        cut = best[1] if best else min(ideal, high)
        if cut <= position + 0.1:
            cut = min(position + target, duration)
        cuts.append(round(cut, 3))
        position = cut

    cuts.append(round(duration, 3))
    return cuts


def write_wav_slice(source: Path, destination: Path, start: float, end: float) -> None:
    with wave.open(str(source), "rb") as reader:
        params = reader.getparams()
        rate = reader.getframerate()
        start_frame = max(0, int(start * rate))
        end_frame = min(reader.getnframes(), int(end * rate))
        reader.setpos(start_frame)
        frames = reader.readframes(max(0, end_frame - start_frame))

    with wave.open(str(destination), "wb") as writer:
        writer.setparams(params)
        writer.writeframes(frames)


def _recognize(
    recognizer: sr.Recognizer, audio: sr.AudioData, language: str, retries: int
) -> str:
    last_error: sr.RequestError | None = None
    for attempt in range(retries + 1):
        try:
            return recognizer.recognize_google(audio, language=language)
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as error:
            last_error = error
            if attempt < retries:
                time.sleep(min(2**attempt, 8))

    raise TranscriptionError(
        "Google's speech service rejected the request "
        f"({last_error}). Wait a minute and try again; if it keeps "
        "happening, check your internet connection or proxy settings."
    )


def transcribe_chunk(
    recognizer: sr.Recognizer, chunk_path: Path, language: str, retries: int
) -> str:
    with sr.AudioFile(str(chunk_path)) as source:
        audio = recognizer.record(source)
    return _recognize(recognizer, audio, language, retries)


def transcribe_pcm(
    pcm: bytes,
    language: str = DEFAULT_LANGUAGE,
    sample_rate: int = 16000,
    sample_width: int = 2,
    retries: int = 3,
) -> str:
    """Transcribe one raw PCM utterance, as received from the live socket."""
    recognizer = sr.Recognizer()
    recognizer.operation_timeout = 45
    audio = sr.AudioData(pcm, sample_rate, sample_width)
    return _recognize(recognizer, audio, language, retries)


def transcribe_file(
    path: str | Path,
    language: str = DEFAULT_LANGUAGE,
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    progress: ProgressCallback | None = None,
    retries: int = 4,
) -> Transcript:
    """Transcribe one audio/video file and return timed segments."""
    source = Path(path)
    if not source.is_file():
        raise TranscriptionError(f"File not found: {source}")

    started = time.monotonic()
    workdir = Path(tempfile.mkdtemp(prefix="voice-typing-"))
    try:
        wav_path = workdir / "audio.wav"
        if progress:
            progress("convert", 0, 1)
        convert_to_wav(source, wav_path)

        duration = wav_duration(wav_path)
        if duration <= 0:
            raise TranscriptionError("The audio file is empty.")

        if progress:
            progress("analyze", 0, 1)
        silences = detect_silences(wav_path)
        cuts = plan_cuts(duration, silences, target=chunk_seconds)
        total_chunks = len(cuts) - 1

        recognizer = sr.Recognizer()
        recognizer.operation_timeout = 90

        segments: list[Segment] = []
        for index, (start, end) in enumerate(zip(cuts, cuts[1:]), start=1):
            chunk_path = workdir / f"chunk_{index:03d}.wav"
            write_wav_slice(wav_path, chunk_path, start, end)

            if progress:
                progress("transcribe", index, total_chunks)
            text = transcribe_chunk(recognizer, chunk_path, language, retries)
            segments.append(Segment(start=start, end=end, text=text.strip()))

            if index < total_chunks:
                time.sleep(0.2)

        return Transcript(
            source=source.name,
            language=language,
            duration=duration,
            segments=segments,
            elapsed=time.monotonic() - started,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def srt_timestamp(seconds: float) -> str:
    milliseconds = int(round(max(0.0, seconds) * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def format_srt(segments: list[Segment]) -> str:
    blocks: list[str] = []
    number = 0
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        number += 1
        blocks.append(str(number))
        blocks.append(f"{srt_timestamp(segment.start)} --> {srt_timestamp(segment.end)}")
        blocks.append(text)
        blocks.append("")
    return "\n".join(blocks).strip() + "\n" if blocks else ""
