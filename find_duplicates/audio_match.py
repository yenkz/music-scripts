"""Similar-name candidates and strict decoded-audio verification (no mutation)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Sequence


@dataclass(frozen=True)
class NamePair:
    first: Path
    second: Path
    similarity: float


@dataclass(frozen=True)
class AudioSignature:
    sample_rate: int
    channels: int
    channel_layout: str
    sha256: str


def normalized_name(path: Path) -> str:
    """Normalize naming noise for suggestions only, never proof of duplication."""
    name = unicodedata.normalize("NFKD", path.stem).casefold()
    name = "".join(char for char in name if not unicodedata.combining(char))
    name = re.sub(r"^(?:\d+\s+)?unknown artist\s*-\s*", "", name)
    name = re.sub(r"^\d+[.)]\s*", "", name)
    name = re.sub(r"^\d{1,2}[ab]\s*-\s*\d{2,3}\s*-\s*", "", name)
    name = re.sub(r"(?:\s*\[\d+\]|\s*\(\d+\)|-\d+)+\s*$", "", name)
    name = re.sub(r"\s+(?:www[._]|my[-_]free[-_]).*$", "", name)
    return " ".join(re.sub(r"[\W_]+", " ", name).split())


def find_name_pairs(
    paths: Sequence[Path], threshold: float = 0.86,
    progress: Callable[[Path, int, int], None] | None = None,
) -> tuple[NamePair, ...]:
    """Compare names sharing a useful word, including truncated title prefixes."""
    ordered = sorted(paths, key=lambda path: (str(path).casefold(), str(path)))
    names = [normalized_name(path) for path in ordered]
    ignored = {"original", "mix", "remix", "unknown", "artist", "feat", "the", "and"}
    postings: dict[str, list[int]] = defaultdict(list)
    for index, name in enumerate(names):
        words = {word for word in name.split() if len(word) >= 3 and word not in ignored}
        for word in words:
            postings[word].append(index)
        if name:
            postings["prefix:" + name[:8]].append(index)
    neighbors: dict[int, set[int]] = defaultdict(set)
    for indexes in postings.values():
        for offset, first in enumerate(indexes):
            neighbors[first].update(indexes[offset + 1:])
    pairs: list[NamePair] = []
    for first, path in enumerate(ordered):
        if progress:
            progress(path, first + 1, len(ordered))
        for second in sorted(neighbors[first]):
            left, right = names[first], names[second]
            if not left or not right:
                continue
            short, long = sorted((left, right), key=len)
            if len(short) >= 18 and long.startswith(short) and len(short) / len(long) >= 0.55:
                score = max(0.94, len(short) / len(long))
            else:
                matcher = SequenceMatcher(None, left, right, autojunk=False)
                if matcher.quick_ratio() < threshold:
                    continue
                score = matcher.ratio()
            if score >= threshold:
                pairs.append(NamePair(path, ordered[second], score))
    return tuple(pairs)


def decoded_signature(
    path: Path, ffmpeg: str, ffprobe: str, timeout: int,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> AudioSignature:
    """Hash lossless PCM at the source rate/channels, with no resampling."""
    try:
        probe = runner(
            [ffprobe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=sample_rate,channels,channel_layout,sample_fmt",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=True, timeout=timeout,
        )
        streams = json.loads(probe.stdout)["streams"]
        if len(streams) != 1:
            raise ValueError("expected exactly one audio stream")
        stream = streams[0]
        rate, channels = int(stream["sample_rate"]), int(stream["channels"])
        if rate <= 0 or channels <= 0:
            raise ValueError("invalid audio stream")
        # Float64 preserves decoded floating-point samples and all int32 values.
        # Forcing int16/int32 could hide small differences by quantizing floats.
        result = runner(
            [ffmpeg, "-v", "error", "-nostdin", "-xerror", "-i", str(path),
             "-map", "0:a:0", "-c:a", "pcm_f64le", "-f", "hash", "-hash", "sha256", "-"],
            capture_output=True, text=True, check=True, timeout=timeout,
        )
        match = re.fullmatch(r"SHA256=([0-9a-f]{64})\s*", result.stdout)
        if not match or match[1] == hashlib.sha256(b"").hexdigest():
            raise ValueError("missing or empty decoded audio hash")
        return AudioSignature(rate, channels, stream.get("channel_layout", ""), match[1])
    except subprocess.TimeoutExpired as error:
        raise OSError(f"audio verification timed out after {timeout}s") from error
    except subprocess.CalledProcessError as error:
        raise OSError("audio decoder failed; file was not verified") from error
    except (ValueError, KeyError, TypeError) as error:
        raise OSError("invalid audio verification output") from error
