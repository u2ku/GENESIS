#!/usr/bin/env python3
"""Kokoro synthesis, standing alone — no Luna, no GENESIS imports.

Vendored from ~/Luna/scripts/kokoro_speak.py (Luna's wrapper and this
copy share one origin; Luna's is the live one, this is the portable
one). It reads text on stdin, writes a WAV, and that is all. The
imports it needs beyond the standard library — numpy and kokoro_onnx —
come from the venv the bootstrap builds.
"""
from __future__ import annotations

import argparse
import re
import sys
import wave
from pathlib import Path

import numpy as np
from kokoro_onnx import Kokoro


FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`([^`]*)`")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^\s{0,3}#+\s*", re.MULTILINE)
BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
WHITESPACE_RE = re.compile(r"\s+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synthesize speech with Kokoro and write a WAV file.")
    parser.add_argument("--output", required=True, help="Output WAV path")
    parser.add_argument("--voice", default="af_nicole", help="Kokoro voice name")
    parser.add_argument("--model", required=True, help="Path to kokoro ONNX model")
    parser.add_argument("--voices", required=True, help="Path to kokoro voices file")
    parser.add_argument("--max-chars", type=int, default=280, help="Maximum chunk size")
    parser.add_argument("--speed", type=float, default=1.0, help="Requested speech speed when supported")
    return parser.parse_args()


def clean_text(text: str) -> str:
    clean = str(text or "")
    clean = FENCE_RE.sub(" ", clean)
    clean = INLINE_CODE_RE.sub(r"\1", clean)
    clean = LINK_RE.sub(r"\1", clean)
    clean = HEADING_RE.sub("", clean)
    clean = BULLET_RE.sub("", clean)
    clean = clean.replace("\r\n", "\n").replace("\r", "\n")
    clean = WHITESPACE_RE.sub(" ", clean).strip()
    return clean


def split_text(text: str, max_chars: int) -> list[str]:
    clean = clean_text(text)
    if not clean:
        return []
    if len(clean) <= max_chars:
        return [clean]

    sentences = [sentence.strip() for sentence in SENTENCE_RE.split(clean) if sentence.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            for idx in range(0, len(sentence), max_chars):
                chunks.append(sentence[idx : idx + max_chars].strip())
            continue
        candidate = sentence if not current else f"{current} {sentence}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current.strip())
            current = sentence
    if current:
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]


def _synth_chunk(engine: Kokoro, chunk: str, voice: str, speed: float) -> tuple[np.ndarray, int]:
    """Synthesise a single chunk, splitting on IndexError (kokoro 510-token ceiling)."""
    try:
        samples, sr = engine.create(chunk, voice=voice, speed=speed)
    except TypeError:
        samples, sr = engine.create(chunk, voice=voice)
    except IndexError:
        if len(chunk) < 20:
            raise
        mid = len(chunk) // 2
        left = chunk[:mid].strip()
        right = chunk[mid:].strip()
        parts: list[np.ndarray] = []
        sr = 0
        for sub in (left, right):
            if not sub:
                continue
            arr, sr = _synth_chunk(engine, sub, voice, speed)
            parts.append(arr)
        if not parts:
            raise RuntimeError("No audio from sub-chunks")
        return np.concatenate(parts), sr
    return np.asarray(samples, dtype=np.float32).reshape(-1), int(sr)


def create_audio(engine: Kokoro, chunks: list[str], voice: str, speed: float) -> tuple[np.ndarray, int]:
    combined: list[np.ndarray] = []
    sample_rate: int | None = None
    for chunk in chunks:
        array, sr = _synth_chunk(engine, chunk, voice, speed)
        if sample_rate is None:
            sample_rate = int(sr)
        elif int(sr) != sample_rate:
            raise RuntimeError(f"Sample-rate mismatch across chunks: {sample_rate} != {sr}")
        combined.append(array)
    if sample_rate is None or not combined:
        raise RuntimeError("No audio produced")
    return np.concatenate(combined), sample_rate


def write_wav(samples: np.ndarray, sample_rate: int, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    safe = np.nan_to_num(np.asarray(samples, dtype=np.float32).reshape(-1), nan=0.0, posinf=1.0, neginf=-1.0)

    # macOS playback is more reliable with standard 48 kHz PCM output.
    target_rate = 48000
    if sample_rate > 0 and sample_rate != target_rate and safe.size:
        source_positions = np.arange(safe.size, dtype=np.float32)
        target_length = max(1, int(round(safe.size * (target_rate / float(sample_rate)))))
        target_positions = np.linspace(0, max(0, safe.size - 1), num=target_length, dtype=np.float32)
        safe = np.interp(target_positions, source_positions, safe).astype(np.float32)
        sample_rate = target_rate
    elif sample_rate <= 0:
        sample_rate = target_rate

    tail_padding = np.zeros(int(sample_rate * 0.35), dtype=np.float32)
    padded = np.concatenate([safe, tail_padding])
    stereo = np.column_stack((padded, padded))
    clipped = np.clip(stereo, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    with wave.open(str(output_path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


def main() -> int:
    args = parse_args()
    raw_text = sys.stdin.read()
    chunks = split_text(raw_text, max(args.max_chars, 80))
    if not chunks:
        print("No input text to synthesize", file=sys.stderr)
        return 1

    engine = Kokoro(args.model, args.voices)
    samples, sample_rate = create_audio(engine, chunks, args.voice, args.speed)
    output_path = Path(args.output).expanduser()
    write_wav(samples, sample_rate, output_path)
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
