"""Kokoro TTS backend — the British male voice, from the local kokoro home.

Vendored into the checkout by GENESIS (tools/kokoro/codexify_kokoro_tts.py
is the source of truth; upstream ships no kokoro support, so this file
arrives whole and is re-copied after every install/update checkout).

The model, the voices and the venv come from the kokoro home the GENESIS
bootstrap builds on each machine — $CODEXIFY_TTS_KOKORO_HOME, then
$GENESIS_KOKORO_HOME, then ~/.local/share/genesis/kokoro — so this
backend needs no network at request time and no model of its own.
"""
from __future__ import annotations

import os
import re
import wave
from pathlib import Path
from typing import Optional

from .base import TTSBackend

# The voices that need no reference audio. bm_* are the British men;
# bm_george is the one GENESIS's own voice uses.
DEFAULT_VOICE = "bm_george"
SUPPORTED_VOICES = ("bm_george", "bm_fable", "bm_lewis")

FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`([^`]*)`")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^\s{0,3}#+\s*", re.MULTILINE)
BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
WHITESPACE_RE = re.compile(r"\s+")

# kokoro's phonemiser ceiling is 510 tokens; chunks are kept well under.
MAX_CHARS = 280


def kokoro_home() -> Path:
    env = (os.environ.get("CODEXIFY_TTS_KOKORO_HOME")
           or os.environ.get("GENESIS_KOKORO_HOME"))
    return Path(env).expanduser() if env else (
        Path.home() / ".local" / "share" / "genesis" / "kokoro")


def clean_text(text: str) -> str:
    clean = str(text or "")
    clean = FENCE_RE.sub(" ", clean)
    clean = INLINE_CODE_RE.sub(r"\1", clean)
    clean = LINK_RE.sub(r"\1", clean)
    clean = HEADING_RE.sub("", clean)
    clean = BULLET_RE.sub(" ", clean)
    clean = clean.replace("\r\n", "\n").replace("\r", "\n")
    clean = WHITESPACE_RE.sub(" ", clean).strip()
    return clean


def split_text(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    clean = clean_text(text)
    if not clean:
        return []
    if len(clean) <= max_chars:
        return [clean]
    sentences = [s.strip() for s in SENTENCE_RE.split(clean) if s.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            for idx in range(0, len(sentence), max_chars):
                chunks.append(sentence[idx:idx + max_chars].strip())
            continue
        candidate = sentence if not current else f"{current} {sentence}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current.strip())
            current = sentence
    if current:
        chunks.append(current.strip())
    return [c for c in chunks if c]


class KokoroTTSBackend(TTSBackend):
    """Synthesis through kokoro-onnx, in process, from the local files."""

    def __init__(self, *, voice: str = DEFAULT_VOICE):
        self.voice = voice
        self._engine = None

    def _home(self) -> Path:
        return kokoro_home()

    def engine(self):
        """Load once, lazily — the service may boot before the model is
        ever asked for, and a missing kokoro home should not stop other
        providers from serving."""
        if self._engine is None:
            from kokoro_onnx import Kokoro
            home = self._home()
            self._engine = Kokoro(
                str(home / "kokoro-v1.0.onnx"),
                str(home / "voices-v1.0.bin"))
        return self._engine

    def synthesize(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        ref_audio: Optional[str] = None,
        ref_text: Optional[str] = None,
    ) -> tuple[bytes, int]:
        if ref_audio or ref_text:
            raise ValueError(
                "kokoro voices are predefined speakers; reference audio "
                "is not accepted — use a clone-capable provider instead")
        voice = voice or self.voice or DEFAULT_VOICE
        speed = float(speed) if speed else 1.0
        chunks = split_text(text)
        if not chunks:
            raise ValueError("no text to synthesize")

        import numpy as np

        engine = self.engine()
        parts = []
        sample_rate = None
        for chunk in chunks:
            samples, sr = engine.create(chunk, voice=voice, speed=speed)
            sr = int(sr)
            if sample_rate is None:
                sample_rate = sr
            elif sr != sample_rate:
                raise RuntimeError(
                    f"sample-rate mismatch across chunks: "
                    f"{sample_rate} != {sr}")
            parts.append(np.asarray(samples, dtype=np.float32).reshape(-1))
        audio = np.concatenate(parts)

        return _wav_bytes(audio, sample_rate)


def _wav_bytes(samples, sample_rate: int) -> tuple[bytes, int]:
    """PCM WAV bytes at a standard rate, mono — the service's contract."""
    import io

    import numpy as np

    target_rate = 24000  # kokoro's native rate; no resample needed
    if sample_rate != target_rate and sample_rate > 0:
        positions = np.linspace(0, samples.size - 1,
                                num=int(samples.size * target_rate
                                        / sample_rate),
                                dtype=np.float32)
        samples = np.interp(positions,
                            np.arange(samples.size, dtype=np.float32),
                            samples).astype(np.float32)
    safe = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
    pcm = (np.clip(safe, -1.0, 1.0) * 32767.0).astype("<i2")
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(target_rate)
        handle.writeframes(pcm.tobytes())
    return out.getvalue(), target_rate
