#!/usr/bin/env python3
"""Create missing artist/title tags with layered music recognition."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from mutagen import File as MutagenFile
from mutagen import MutagenError
from mutagen.asf import ASF
from mutagen.id3 import ID3, TIT2, TPE1
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

try:
    from .cache import RecognitionCache, default_cache_path, file_cache_key
    from .recognition import (
        DiscogsClient,
        Match,
        ProviderError,
        Selection,
        ShazamClient,
        filename_candidate,
        filename_candidate_from_stem,
        matches_agree,
    )
except ImportError:  # Direct execution: python create_metadata/create_metadata.py
    from cache import RecognitionCache, default_cache_path, file_cache_key  # type: ignore[no-redef]
    from recognition import (  # type: ignore[no-redef]
        DiscogsClient,
        Match,
        ProviderError,
        Selection,
        ShazamClient,
        filename_candidate,
        filename_candidate_from_stem,
        matches_agree,
    )


AUDIO_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".alac",
    ".ape",
    ".dsf",
    ".flac",
    ".m4a",
    ".m4b",
    ".mp3",
    ".mp4",
    ".mpc",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".wma",
    ".wv",
}
ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
MINIMUM_REQUEST_INTERVAL = 0.34  # AcoustID asks clients to stay below 3 req/s.
DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MetadataError(Exception):
    """An audio file could not be read or safely updated."""


class FingerprintError(Exception):
    """Chromaprint could not fingerprint an audio file."""


class LookupError(Exception):
    """AcoustID could not return a usable response."""


class ConfigError(Exception):
    """The .env configuration is invalid or unreadable."""


@dataclass(frozen=True)
class ExistingMetadata:
    artist: str | None
    title: str | None

    @property
    def complete(self) -> bool:
        return bool(self.artist and self.title)


@dataclass(frozen=True)
class Fingerprint:
    duration: int
    value: str


@dataclass(frozen=True)
class FileOutcome:
    path: Path
    existing: ExistingMetadata
    match: Match | None
    status: str
    detail: str


@dataclass(frozen=True)
class Configuration:
    api_key: str | None
    fpcalc: str
    discogs_token: str | None
    shazam_enabled: bool
    ffmpeg: str
    min_score: float
    min_margin: float
    fingerprint_timeout: float
    network_timeout: float
    cache_enabled: bool
    cache_file: Path
    cache_ttl_days: float
    refresh_cache: bool
    env_file: Path


@dataclass(frozen=True)
class RunMetrics:
    total: float
    metadata: float
    identification: float
    writing: float
    fingerprint: float
    discogs: float
    acoustid: float
    shazam: float
    cache_hits: int
    cache_misses: int
    network_requests: int


def parse_env_value(value: str, *, path: Path, line_number: int) -> str:
    """Parse a plain, single-quoted, or double-quoted .env value."""
    value = value.strip()
    if not value:
        return ""
    if value[0] not in {'"', "'"}:
        comment = re.search(r"\s+#", value)
        if comment:
            value = value[: comment.start()]
        return value.strip()

    quote = value[0]
    escaped = False
    closing = None
    for index, character in enumerate(value[1:], start=1):
        if quote == '"' and character == "\\" and not escaped:
            escaped = True
            continue
        if character == quote and not escaped:
            closing = index
            break
        escaped = False
    if closing is None:
        raise ConfigError(f"{path}:{line_number}: unterminated quoted value")

    remainder = value[closing + 1 :].strip()
    if remainder and not remainder.startswith("#"):
        raise ConfigError(f"{path}:{line_number}: unexpected text after quoted value")
    quoted = value[: closing + 1]
    if quote == "'":
        return quoted[1:-1]
    try:
        parsed = ast.literal_eval(quoted)
    except (SyntaxError, ValueError) as error:
        raise ConfigError(f"{path}:{line_number}: invalid quoted value") from error
    if not isinstance(parsed, str):
        raise ConfigError(f"{path}:{line_number}: value must be text")
    return parsed


def load_env_file(path: Path) -> dict[str, str]:
    """Load a small, dependency-free subset of the dotenv file format."""
    if not path.exists():
        return {}
    if not path.is_file():
        raise ConfigError(f"not a regular file: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from error

    values: dict[str, str] = {}
    for line_number, original in enumerate(lines, start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(f"{path}:{line_number}: expected NAME=value")
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not ENV_NAME.fullmatch(name):
            raise ConfigError(f"{path}:{line_number}: invalid setting name")
        values[name] = parse_env_value(
            raw_value, path=path, line_number=line_number
        )
    return values


def configuration_from_args(
    args: argparse.Namespace, environ: Mapping[str, str] | None = None
) -> Configuration:
    """Resolve CLI, process-environment, .env, and default values in order."""
    env_file = Path(args.env_file).expanduser().resolve()
    file_values = load_env_file(env_file)
    process_values = os.environ if environ is None else environ

    def configured(name: str, command_line: Any, default: Any = None) -> Any:
        if command_line is not None:
            return command_line
        if name in process_values:
            return process_values[name]
        return file_values.get(name, default)

    def number(name: str, command_line: float | None, default: float) -> float:
        raw_value = configured(name, command_line, default)
        try:
            return float(raw_value)
        except (TypeError, ValueError) as error:
            raise ConfigError(f"{name} must be a number") from error

    def boolean(name: str, command_line: bool | None, default: bool) -> bool:
        raw_value = configured(name, command_line, default)
        if isinstance(raw_value, bool):
            return raw_value
        normalized = str(raw_value).strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(
            f"{name} must be true/false, yes/no, on/off, or 1/0"
        )

    api_key = str(configured("ACOUSTID_API_KEY", args.api_key, "")).strip() or None
    fpcalc = str(configured("CREATE_METADATA_FPCALC", args.fpcalc, "fpcalc")).strip()
    if not fpcalc:
        raise ConfigError("CREATE_METADATA_FPCALC cannot be empty")
    discogs_token = str(configured("DISCOGS_TOKEN", None, "")).strip() or None
    shazam_enabled = boolean("CREATE_METADATA_SHAZAM", args.shazam, False)
    ffmpeg = str(configured("CREATE_METADATA_FFMPEG", None, "ffmpeg")).strip()
    if not ffmpeg:
        raise ConfigError("CREATE_METADATA_FFMPEG cannot be empty")
    return Configuration(
        api_key=api_key,
        fpcalc=fpcalc,
        discogs_token=discogs_token,
        shazam_enabled=shazam_enabled,
        ffmpeg=ffmpeg,
        min_score=number("CREATE_METADATA_MIN_SCORE", args.min_score, 0.90),
        min_margin=number("CREATE_METADATA_MIN_MARGIN", args.min_margin, 0.05),
        fingerprint_timeout=number(
            "CREATE_METADATA_TIMEOUT", args.timeout, 120.0
        ),
        network_timeout=number(
            "CREATE_METADATA_NETWORK_TIMEOUT", args.network_timeout, 30.0
        ),
        cache_enabled=boolean("CREATE_METADATA_CACHE", args.cache, True),
        cache_file=Path(
            str(
                configured(
                    "CREATE_METADATA_CACHE_FILE",
                    args.cache_file,
                    default_cache_path(),
                )
            )
        ).expanduser(),
        cache_ttl_days=number(
            "CREATE_METADATA_CACHE_TTL_DAYS", args.cache_ttl_days, 30.0
        ),
        refresh_cache=args.refresh_cache,
        env_file=env_file,
    )


def clean_text(value: Any) -> str | None:
    """Return the first non-empty tag value as normalized text."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if hasattr(value, "text"):
        value = value.text
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return text or None


def first_tag(tags: Mapping[str, Any] | None, keys: Sequence[str]) -> str | None:
    if not tags:
        return None
    lowercase_keys = {str(key).lower(): key for key in tags.keys()}
    for wanted in keys:
        actual = lowercase_keys.get(wanted.lower())
        if actual is not None:
            value = clean_text(tags.get(actual))
            if value:
                return value
    return None


def read_existing_metadata(
    path: Path, mutagen_file: Callable[..., Any] = MutagenFile
) -> ExistingMetadata:
    """Read the fields used by flatten_music without changing the file."""
    try:
        audio = mutagen_file(path, easy=True)
    except (MutagenError, OSError) as error:
        raise MetadataError(f"cannot read tags: {error}") from error
    if audio is None:
        raise MetadataError("unsupported or unreadable audio format")

    tags = getattr(audio, "tags", None)
    return ExistingMetadata(
        artist=first_tag(
            tags, ("artist", "albumartist", "author", "tpe1", "tpe2")
        ),
        title=first_tag(tags, ("title", "tit2")),
    )


def scan_audio_files(target: Path) -> list[Path]:
    """Return audio files recursively, without following directory symlinks."""
    if target.is_file():
        return [target] if target.suffix.lower() in AUDIO_EXTENSIONS else []

    files: list[Path] = []

    def walk_error(error: OSError) -> None:
        raise error

    for current, dirnames, filenames in os.walk(
        target, topdown=True, followlinks=False, onerror=walk_error
    ):
        current_path = Path(current)
        dirnames[:] = sorted(
            (
                dirname
                for dirname in dirnames
                if not (current_path / dirname).is_symlink()
            ),
            key=str.casefold,
        )
        for filename in sorted(filenames, key=str.casefold):
            path = current_path / filename
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                files.append(path)

    files.sort(key=lambda path: str(path.relative_to(target)).casefold())
    return files


def fingerprint_file(
    path: Path, fpcalc: str, *, timeout: float = 120.0
) -> Fingerprint:
    """Generate a complete-file Chromaprint fingerprint with fpcalc."""
    try:
        process = subprocess.run(
            [fpcalc, "-json", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise FingerprintError(f"fpcalc timed out after {timeout:g} seconds") from error
    except OSError as error:
        raise FingerprintError(f"cannot run fpcalc: {error}") from error

    if process.returncode != 0:
        detail = process.stderr.strip() or f"fpcalc exited with {process.returncode}"
        raise FingerprintError(detail)

    try:
        payload = json.loads(process.stdout)
        duration = int(round(float(payload["duration"])))
        value = str(payload["fingerprint"]).strip()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise FingerprintError("fpcalc returned invalid JSON") from error
    if duration <= 0 or not value:
        raise FingerprintError("fpcalc returned an empty fingerprint")
    return Fingerprint(duration=duration, value=value)


class CachedFingerprinter:
    """Reuse complete-file fingerprints while the source file is unchanged."""

    def __init__(
        self,
        fpcalc: str,
        timeout: float,
        cache: RecognitionCache | None,
    ) -> None:
        self.fpcalc = fpcalc
        self.timeout = timeout
        self.cache = cache
        self.calls = 0
        self.cache_hits = 0
        self.elapsed = 0.0

    def __call__(self, path: Path) -> Fingerprint:
        started = time.monotonic()
        self.calls += 1
        try:
            key = file_cache_key(path)
        except OSError as error:
            raise FingerprintError(f"cannot stat audio file: {error}") from error
        if self.cache is not None:
            cached = self.cache.get("fingerprint-v1", key, permanent=True)
            if isinstance(cached, Mapping):
                try:
                    fingerprint = Fingerprint(
                        duration=int(cached["duration"]),
                        value=str(cached["value"]),
                    )
                except (KeyError, TypeError, ValueError):
                    pass
                else:
                    if fingerprint.duration > 0 and fingerprint.value:
                        self.cache_hits += 1
                        self.elapsed += time.monotonic() - started
                        return fingerprint
        try:
            fingerprint = fingerprint_file(path, self.fpcalc, timeout=self.timeout)
            if self.cache is not None:
                self.cache.put(
                    "fingerprint-v1",
                    key,
                    {"duration": fingerprint.duration, "value": fingerprint.value},
                )
            return fingerprint
        finally:
            self.elapsed += time.monotonic() - started


def lookup_acoustid(
    fingerprint: Fingerprint,
    api_key: str,
    *,
    timeout: float = 30.0,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> Mapping[str, Any]:
    """Look up a fingerprint and request MusicBrainz recording metadata."""
    body = urllib.parse.urlencode(
        {
            "client": api_key,
            "duration": fingerprint.duration,
            "fingerprint": fingerprint.value,
            "meta": "recordings",
            "format": "json",
        }
    ).encode("ascii")
    request = urllib.request.Request(
        ACOUSTID_LOOKUP_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "create-metadata/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise LookupError(f"AcoustID returned HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise LookupError(f"cannot reach AcoustID: {error.reason}") from error
    except TimeoutError as error:
        raise LookupError(f"AcoustID timed out after {timeout:g} seconds") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LookupError("AcoustID returned invalid JSON") from error

    if not isinstance(payload, Mapping):
        raise LookupError("AcoustID returned an unexpected response")
    if payload.get("status") != "ok":
        error_data = payload.get("error")
        if isinstance(error_data, Mapping):
            message = clean_text(error_data.get("message")) or "lookup failed"
        else:
            message = "lookup failed"
        raise LookupError(f"AcoustID: {message}")
    return payload


def artist_credit(recording: Mapping[str, Any]) -> str | None:
    """Render the MusicBrainz artist credit returned by AcoustID."""
    artists = recording.get("artists")
    if not isinstance(artists, list):
        return None

    pieces: list[str] = []
    for artist in artists:
        if not isinstance(artist, Mapping):
            continue
        name = clean_text(artist.get("name"))
        if not name:
            continue
        pieces.append(name)
        joinphrase = artist.get("joinphrase")
        if isinstance(joinphrase, str):
            pieces.append(joinphrase)
        elif len(artists) > 1 and artist is not artists[-1]:
            pieces.append(", ")
    return "".join(pieces).strip() or None


def metadata_key(artist: str, title: str) -> tuple[str, str]:
    def normalize(value: str) -> str:
        return unicodedata.normalize("NFKC", " ".join(value.split())).casefold()

    return normalize(artist), normalize(title)


def select_match(
    payload: Mapping[str, Any], *, min_score: float, min_margin: float
) -> Selection:
    """Choose one confident metadata candidate, rejecting close alternatives."""
    candidates: dict[tuple[str, str], Match] = {}
    results = payload.get("results")
    if not isinstance(results, list):
        return Selection(None, "no_match", "AcoustID returned no recordings")

    for result in results:
        if not isinstance(result, Mapping):
            continue
        try:
            score = float(result.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        recordings = result.get("recordings")
        if not isinstance(recordings, list):
            continue
        for recording in recordings:
            if not isinstance(recording, Mapping):
                continue
            artist = artist_credit(recording)
            title = clean_text(recording.get("title"))
            if not artist or not title:
                continue
            recording_id = clean_text(recording.get("id"))
            match = Match(artist, title, score, recording_id)
            key = metadata_key(artist, title)
            previous = candidates.get(key)
            if previous is None or match.score > previous.score:
                candidates[key] = match

    ranked = sorted(
        candidates.values(),
        key=lambda match: (-match.score, match.artist.casefold(), match.title.casefold()),
    )
    if not ranked:
        return Selection(None, "no_match", "no MusicBrainz artist/title match")

    best = ranked[0]
    if best.score < min_score:
        return Selection(
            None,
            "low_confidence",
            f"best score {best.score:.1%} is below {min_score:.1%}",
        )
    if len(ranked) > 1 and best.score - ranked[1].score < min_margin:
        alternative = ranked[1]
        return Selection(
            None,
            "ambiguous",
            (
                f"{best.artist} — {best.title} ({best.score:.1%}) vs "
                f"{alternative.artist} — {alternative.title} "
                f"({alternative.score:.1%})"
            ),
        )
    return Selection(best, "ready", "audio recognized by AcoustID")


def fields_to_add(existing: ExistingMetadata) -> str:
    missing = []
    if not existing.artist:
        missing.append("artist")
    if not existing.title:
        missing.append("title")
    return "add " + " and ".join(missing)


def needs_lookup(existing: ExistingMetadata, *, replace_existing: bool) -> bool:
    """Fingerprint incomplete files by default, or every file when forced."""
    return replace_existing or not existing.complete


def partial_metadata_candidate(
    path: Path, existing: ExistingMetadata
) -> tuple[Match, bool] | None:
    """Recover a filename candidate corroborated by one existing tag.

    The boolean records whether the existing title is a malformed packed
    ``Artist - Title`` value that must be replaced as part of the repair.
    """
    candidate = filename_candidate(path)
    if candidate is None or existing.complete:
        return None

    candidate_key = metadata_key(candidate.artist, candidate.title)
    if existing.artist and not existing.title:
        if metadata_key(existing.artist, candidate.title)[0] != candidate_key[0]:
            return None
        return replace(candidate, score=1.0, source="Existing tag + filename"), False

    if existing.title and not existing.artist:
        existing_title_key = metadata_key(candidate.artist, existing.title)[1]
        if existing_title_key == candidate_key[1]:
            return replace(candidate, score=1.0, source="Existing tag + filename"), False

        stem_key = metadata_key(candidate.artist, path.stem)[1]
        if existing_title_key == stem_key:
            return replace(candidate, score=1.0, source="Existing tag + filename"), True

        packed_title = re.sub(
            r"^\s*PREMIERE\s*:\s*", "", existing.title, flags=re.IGNORECASE
        )
        packed_candidate = filename_candidate_from_stem(packed_title)
        if packed_candidate is not None and matches_agree(candidate, packed_candidate):
            return replace(candidate, score=1.0, source="Existing tag + filename"), True
    return None


def placeholder_metadata_candidate(
    path: Path, existing: ExistingMetadata
) -> Match | None:
    """Recover tags written by the old numbered-Unknown-Artist parser bug.

    The repair is deliberately limited to the exact legacy signature: a
    complete ``<number> Unknown Artist`` artist tag, a filename generated from
    those tags, and a title that independently parses as ``Artist - Title``.
    """
    if not existing.artist or not existing.title:
        return None
    if not re.fullmatch(r"\d{1,3}\s+Unknown Artist", existing.artist, re.IGNORECASE):
        return None
    expected_stem = f"{existing.artist} - {existing.title}"
    if metadata_key(path.stem, path.stem)[0] != metadata_key(expected_stem, expected_stem)[0]:
        return None
    candidate = filename_candidate_from_stem(existing.title)
    if candidate is None:
        return None
    return replace(candidate, score=1.0, source="Placeholder tag repair")


def values_agree(existing: ExistingMetadata, match: Match) -> bool:
    if existing.artist and metadata_key(existing.artist, match.title)[0] != metadata_key(
        match.artist, match.title
    )[0]:
        return False
    if existing.title and metadata_key(match.artist, existing.title)[1] != metadata_key(
        match.artist, match.title
    )[1]:
        return False
    return True


def accepted_outcome(
    path: Path,
    existing: ExistingMetadata,
    match: Match,
    detail: str,
    *,
    replace_existing: bool,
) -> FileOutcome:
    if not replace_existing and not values_agree(existing, match):
        return FileOutcome(
            path,
            existing,
            match,
            "conflict",
            "match conflicts with an existing artist or title tag",
        )
    action = "replace artist and title" if replace_existing else fields_to_add(existing)
    return FileOutcome(path, existing, match, "ready", f"{action}; {detail}")


def acoustic_match_is_safe(match: Match, candidate: Match | None) -> bool:
    """Keep filename agreement ahead of Shazam and require strong AcoustID evidence."""
    if candidate is not None and matches_agree(match, candidate):
        return True
    if match.source == "Shazam":
        return candidate is None
    return match.score >= 0.98


def analyze_missing_file(
    path: Path,
    existing: ExistingMetadata,
    *,
    fpcalc: str | None,
    acoustid_lookup: Callable[[Fingerprint], Mapping[str, Any]] | None,
    discogs_lookup: Callable[[Match], Selection] | None = None,
    shazam_lookup: Callable[[Path], Selection] | None = None,
    min_score: float,
    min_margin: float,
    fingerprint_timeout: float,
    replace_existing: bool = False,
    fingerprint_lookup: Callable[[Path], Fingerprint] | None = None,
    progress: Callable[[str], None] | None = None,
) -> FileOutcome:
    candidate = filename_candidate(path)
    notes: list[str] = []
    errors: list[str] = []

    placeholder_repair = placeholder_metadata_candidate(path, existing)
    if placeholder_repair is not None and not replace_existing:
        return FileOutcome(
            path,
            existing,
            placeholder_repair,
            "ready",
            "replace legacy numbered Unknown Artist tags",
        )

    recovered = partial_metadata_candidate(path, existing)
    if recovered is not None and not replace_existing:
        match, repairs_packed_title = recovered
        action = (
            "add artist and repair packed title"
            if repairs_packed_title
            else fields_to_add(existing)
        )
        return FileOutcome(
            path,
            existing,
            match,
            "ready",
            f"{action}; existing tag agrees with filename",
        )

    if candidate is not None and discogs_lookup is not None:
        if progress is not None:
            progress("Discogs")
        try:
            selection = discogs_lookup(candidate)
        except ProviderError as error:
            errors.append(str(error))
        else:
            if selection.match is not None:
                return accepted_outcome(
                    path,
                    existing,
                    selection.match,
                    selection.detail,
                    replace_existing=replace_existing,
                )
            notes.append(selection.detail)

    if acoustid_lookup is not None and fpcalc is not None:
        try:
            if progress is not None:
                progress("fingerprinting")
            fingerprint = (
                fingerprint_lookup(path)
                if fingerprint_lookup is not None
                else fingerprint_file(path, fpcalc, timeout=fingerprint_timeout)
            )
            if progress is not None:
                progress("AcoustID")
            payload = acoustid_lookup(fingerprint)
            selection = select_match(
                payload, min_score=min_score, min_margin=min_margin
            )
        except (FingerprintError, LookupError) as error:
            errors.append(str(error))
        else:
            if selection.match is not None and acoustic_match_is_safe(
                selection.match, candidate
            ):
                return accepted_outcome(
                    path,
                    existing,
                    selection.match,
                    selection.detail,
                    replace_existing=replace_existing,
                )
            if selection.match is not None:
                notes.append("AcoustID result disagrees with the filename")
            else:
                notes.append(selection.detail)

    if shazam_lookup is not None:
        if progress is not None:
            progress("Shazam")
        try:
            selection = shazam_lookup(path)
        except ProviderError as error:
            errors.append(str(error))
        else:
            if selection.match is not None and acoustic_match_is_safe(
                selection.match, candidate
            ):
                return accepted_outcome(
                    path,
                    existing,
                    selection.match,
                    selection.detail,
                    replace_existing=replace_existing,
                )
            if selection.match is not None:
                notes.append("Shazam result disagrees with the filename")
            else:
                notes.append(selection.detail)

    if candidate is not None:
        detail_parts = ["filename candidate was not externally validated", *notes, *errors]
        return FileOutcome(
            path,
            existing,
            candidate,
            "unverified",
            "; ".join(dict.fromkeys(detail_parts)),
        )
    if errors:
        return FileOutcome(path, existing, None, "error", "; ".join(errors))
    detail = "; ".join(dict.fromkeys(notes)) or "no recognition provider found a match"
    return FileOutcome(path, existing, None, "no_match", detail)


def write_missing_metadata(
    path: Path,
    match: Match,
    mutagen_file: Callable[..., Any] = MutagenFile,
    *,
    replace_existing: bool = False,
) -> tuple[str, ...]:
    """Write missing fields, a safe packed-title repair, or forced replacements."""
    try:
        audio = mutagen_file(path, easy=True)
    except (MutagenError, OSError) as error:
        raise MetadataError(f"cannot reopen tags: {error}") from error
    if audio is None:
        raise MetadataError("unsupported or unreadable audio format")

    tags = getattr(audio, "tags", None)
    if tags is None:
        try:
            audio.add_tags()
        except (MutagenError, OSError, NotImplementedError) as error:
            raise MetadataError(f"cannot create tags: {error}") from error
        tags = getattr(audio, "tags", None)
    if tags is None:
        raise MetadataError("audio format does not support writable tags")

    existing = ExistingMetadata(
        artist=first_tag(
            tags, ("artist", "albumartist", "author", "tpe1", "tpe2")
        ),
        title=first_tag(tags, ("title", "tit2")),
    )
    placeholder_repair = placeholder_metadata_candidate(path, existing)
    safe_placeholder_repair = (
        placeholder_repair is not None and matches_agree(placeholder_repair, match)
    )
    recovery = partial_metadata_candidate(path, existing)
    safe_recovery = recovery is not None and matches_agree(recovery[0], match)
    if (
        not replace_existing
        and not values_agree(existing, match)
        and not safe_recovery
        and not safe_placeholder_repair
    ):
        raise MetadataError("tags changed after scanning and now conflict with the match")

    changed: list[str] = []
    write_artist = replace_existing or not existing.artist or safe_placeholder_repair
    repair_packed_title = bool(safe_recovery and recovery and recovery[1])
    write_title = (
        replace_existing
        or not existing.title
        or repair_packed_title
        or safe_placeholder_repair
    )
    try:
        if isinstance(tags, ID3):
            if write_artist:
                tags.add(TPE1(encoding=3, text=[match.artist]))
                changed.append("artist")
            if write_title:
                tags.add(TIT2(encoding=3, text=[match.title]))
                changed.append("title")
        elif isinstance(audio, ASF):
            if write_artist:
                audio["Author"] = match.artist
                changed.append("artist")
            if write_title:
                audio["Title"] = match.title
                changed.append("title")
        else:
            if write_artist:
                audio["artist"] = match.artist
                changed.append("artist")
            if write_title:
                audio["title"] = match.title
                changed.append("title")
        if changed:
            audio.save()
    except (MutagenError, OSError, KeyError, TypeError, ValueError) as error:
        raise MetadataError(f"cannot save tags: {error}") from error
    return tuple(changed)


class RateLimitedLookup:
    """Small AcoustID client that enforces the public-service rate limit."""

    def __init__(
        self,
        api_key: str,
        timeout: float,
        cache: RecognitionCache | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.cache = cache
        self.last_request: float | None = None
        self.network_requests = 0
        self.cache_hits = 0
        self.elapsed = 0.0

    def __call__(self, fingerprint: Fingerprint) -> Mapping[str, Any]:
        started = time.monotonic()
        key = f"{fingerprint.duration}\0{fingerprint.value}"
        if self.cache is not None:
            cached = self.cache.get("acoustid-v1", key)
            if isinstance(cached, Mapping):
                self.cache_hits += 1
                self.elapsed += time.monotonic() - started
                return cached
        if self.last_request is not None:
            remaining = MINIMUM_REQUEST_INTERVAL - (
                time.monotonic() - self.last_request
            )
            if remaining > 0:
                time.sleep(remaining)
        try:
            self.network_requests += 1
            payload = lookup_acoustid(
                fingerprint, self.api_key, timeout=self.timeout
            )
            if self.cache is not None:
                self.cache.put("acoustid-v1", key, payload)
            return payload
        finally:
            self.last_request = time.monotonic()
            self.elapsed += self.last_request - started


def selection_to_cache(selection: Selection) -> Mapping[str, Any]:
    match = selection.match
    return {
        "status": selection.status,
        "detail": selection.detail,
        "match": None
        if match is None
        else {
            "artist": match.artist,
            "title": match.title,
            "score": match.score,
            "recording_id": match.recording_id,
            "source": match.source,
        },
    }


def selection_from_cache(value: Any) -> Selection | None:
    if not isinstance(value, Mapping):
        return None
    status = value.get("status")
    detail = value.get("detail")
    if not isinstance(status, str) or not isinstance(detail, str):
        return None
    match_data = value.get("match")
    if match_data is None:
        return Selection(None, status, detail)
    if not isinstance(match_data, Mapping):
        return None
    try:
        match = Match(
            artist=str(match_data["artist"]),
            title=str(match_data["title"]),
            score=float(match_data["score"]),
            recording_id=(
                str(match_data["recording_id"])
                if match_data.get("recording_id") is not None
                else None
            ),
            source=str(match_data["source"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return Selection(match, status, detail)


class CachedShazamLookup:
    """Cache Shazam's final selection for an unchanged audio file."""

    def __init__(
        self,
        lookup: Callable[[Path], Selection],
        cache: RecognitionCache | None,
    ) -> None:
        self.lookup = lookup
        self.cache = cache
        self.calls = 0
        self.cache_hits = 0
        self.elapsed = 0.0

    def __call__(self, path: Path) -> Selection:
        started = time.monotonic()
        self.calls += 1
        try:
            key = file_cache_key(path)
        except OSError:
            key = ""
        if self.cache is not None and key:
            selection = selection_from_cache(self.cache.get("shazam-v1", key))
            if selection is not None:
                self.cache_hits += 1
                self.elapsed += time.monotonic() - started
                return selection
        try:
            selection = self.lookup(path)
            if self.cache is not None and key:
                self.cache.put("shazam-v1", key, selection_to_cache(selection))
            return selection
        finally:
            self.elapsed += time.monotonic() - started


def display_path(path: Path, target: Path) -> str:
    if target.is_file():
        return path.name
    return str(path.relative_to(target))


def render_report(
    console: Console,
    target: Path,
    outcomes: Sequence[FileOutcome],
    *,
    write: bool,
    replace_existing: bool,
    metrics: RunMetrics | None = None,
) -> None:
    console.print()
    console.rule(Text("♫  CREATE METADATA", style="bold bright_cyan"))
    if write and replace_existing:
        mode = "FORCED WRITE — REPLACED ARTIST AND TITLE TAGS"
    elif write:
        mode = "WRITTEN MISSING TAGS"
    elif replace_existing:
        mode = "FORCED PREVIEW — NOTHING CHANGED"
    else:
        mode = "PREVIEW — NOTHING CHANGED"
    style = "bold green" if write else "bold yellow"
    console.print(Panel(Text(mode, style=style), border_style="green" if write else "yellow"))

    table = Table(
        box=box.ROUNDED,
        header_style="bold white",
        border_style="bright_black",
        show_lines=True,
        expand=True,
    )
    table.add_column("FILE", ratio=2, overflow="fold")
    table.add_column("CURRENT", ratio=2, overflow="fold")
    table.add_column("MATCH", ratio=2, overflow="fold")
    table.add_column("RESULT", ratio=2, overflow="fold")
    status_styles = {
        "already_tagged": "dim",
        "ready": "bold yellow",
        "written": "bold green",
        "no_match": "yellow",
        "low_confidence": "yellow",
        "ambiguous": "yellow",
        "conflict": "bold yellow",
        "unverified": "yellow",
        "error": "bold red",
    }
    labels = {
        "already_tagged": "UNCHANGED",
        "ready": "WOULD WRITE",
        "written": "WRITTEN",
        "no_match": "NO MATCH",
        "low_confidence": "LOW CONFIDENCE",
        "ambiguous": "AMBIGUOUS",
        "conflict": "CONFLICT",
        "unverified": "UNVERIFIED",
        "error": "ERROR",
    }

    visible_outcomes = [
        outcome for outcome in outcomes if outcome.status != "already_tagged"
    ]
    for outcome in visible_outcomes:
        current = f"{outcome.existing.artist or '—'} — {outcome.existing.title or '—'}"
        if outcome.match is None:
            matched = "—"
        elif outcome.match.source == "Shazam":
            matched = (
                f"{outcome.match.artist} — {outcome.match.title}\n"
                "catalog match · Shazam"
            )
        elif outcome.match.source == "Existing tag + filename":
            matched = (
                f"{outcome.match.artist} — {outcome.match.title}\n"
                "recovered from existing tag + filename"
            )
        else:
            matched = (
                f"{outcome.match.artist} — {outcome.match.title}\n"
                f"{outcome.match.score:.1%} confidence · {outcome.match.source}"
            )
        result = Text()
        result.append(labels[outcome.status], style=status_styles[outcome.status])
        if outcome.detail:
            result.append(f"\n{outcome.detail}", style="white")
        table.add_row(display_path(outcome.path, target), current, matched, result)
    if visible_outcomes:
        console.print(table)
    else:
        console.print(
            Panel(
                "Every supported audio file already has artist and title metadata.",
                title="[bold green]NO FILES NEED METADATA[/bold green]",
                border_style="green",
            )
        )

    complete = sum(outcome.status == "already_tagged" for outcome in outcomes)
    changed = sum(outcome.status in {"ready", "written"} for outcome in outcomes)
    unresolved = sum(
        outcome.status
        in {"no_match", "low_confidence", "ambiguous", "conflict", "unverified"}
        for outcome in outcomes
    )
    errors = sum(outcome.status == "error" for outcome in outcomes)
    summary = Table.grid(expand=True, padding=(0, 1))
    for _ in range(5):
        summary.add_column(justify="center", ratio=1)
    summary.add_row("FILES", "COMPLETE", "MATCHED", "UNRESOLVED", "ERRORS")
    summary.add_row(
        str(len(outcomes)),
        str(complete),
        str(changed),
        str(unresolved),
        Text(str(errors), style="bold red" if errors else "dim"),
    )
    console.print(Panel(summary, title="[bold]SUMMARY[/bold]", border_style="bright_blue"))
    if metrics is not None:
        timing = Table.grid(padding=(0, 1))
        timing.add_column(style="dim")
        timing.add_column(justify="right")
        timing.add_row("Total", f"{metrics.total:.2f}s")
        timing.add_row("Read metadata", f"{metrics.metadata:.2f}s")
        timing.add_row("Identify", f"{metrics.identification:.2f}s")
        if write:
            timing.add_row("Write tags", f"{metrics.writing:.2f}s")
        timing.add_row("Discogs", f"{metrics.discogs:.2f}s")
        timing.add_row("Fingerprint", f"{metrics.fingerprint:.2f}s")
        timing.add_row("AcoustID", f"{metrics.acoustid:.2f}s")
        timing.add_row("Shazam", f"{metrics.shazam:.2f}s")
        timing.add_row(
            "Cache",
            f"{metrics.cache_hits} hits · {metrics.cache_misses} misses",
        )
        timing.add_row("Network", f"{metrics.network_requests} requests")
        console.print(
            Panel(timing, title="[bold]PERFORMANCE[/bold]", border_style="cyan")
        )
    if not write and changed:
        write_command = "--force --write" if replace_existing else "--write"
        action = (
            "replace artist/title tags"
            if replace_existing
            else "add the missing tags"
        )
        console.print(
            f"Review every proposed match, then rerun with [bold]{write_command}[/bold] "
            f"to {action}."
        )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find missing artist/title tags with filename parsing, Discogs, "
            "AcoustID, and Shazam. The default is a read-only preview."
        )
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="audio file or directory to scan recursively (default: current directory)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write accepted artist/title matches into the audio files",
    )
    parser.add_argument(
        "--force",
        "--replace-existing",
        dest="replace_existing",
        action="store_true",
        help=(
            "scan all audio and replace artist/title matches; remains read-only "
            "unless combined with --write"
        ),
    )
    parser.add_argument(
        "--api-key",
        help="override ACOUSTID_API_KEY from .env",
    )
    parser.add_argument(
        "--shazam",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable or disable Shazam as the final recognition fallback",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"dotenv configuration file (default: {DEFAULT_ENV_FILE})",
    )
    parser.add_argument(
        "--fpcalc",
        help="override CREATE_METADATA_FPCALC from .env",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        help="override CREATE_METADATA_MIN_SCORE from .env",
    )
    parser.add_argument(
        "--min-margin",
        type=float,
        help="override CREATE_METADATA_MIN_MARGIN from .env",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        help="override CREATE_METADATA_TIMEOUT from .env",
    )
    parser.add_argument(
        "--network-timeout",
        type=float,
        help="override CREATE_METADATA_NETWORK_TIMEOUT from .env",
    )
    parser.add_argument(
        "--cache",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable or disable the persistent recognition cache (default: enabled)",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        help="override CREATE_METADATA_CACHE_FILE from .env",
    )
    parser.add_argument(
        "--cache-ttl-days",
        type=float,
        help="override CREATE_METADATA_CACHE_TTL_DAYS from .env",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="ignore cached provider responses and replace them with fresh results",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    run_started = time.monotonic()
    args = parse_args(argv)
    console = Console(highlight=False)
    error_console = Console(stderr=True, highlight=False)
    target = Path(args.path).expanduser().resolve()

    try:
        configuration = configuration_from_args(args)
    except ConfigError as error:
        error_console.print(f"[bold red]Error:[/bold red] invalid configuration: {error}")
        return 2

    if not target.exists() or not (target.is_dir() or target.is_file()):
        error_console.print(f"[bold red]Error:[/bold red] not a file or directory: {target}")
        return 2
    if not 0 <= configuration.min_score <= 1 or not 0 <= configuration.min_margin <= 1:
        error_console.print(
            "[bold red]Error:[/bold red] --min-score and --min-margin must be between 0 and 1"
        )
        return 2
    if configuration.fingerprint_timeout <= 0 or configuration.network_timeout <= 0:
        error_console.print("[bold red]Error:[/bold red] timeouts must be positive")
        return 2
    if configuration.cache_ttl_days <= 0:
        error_console.print("[bold red]Error:[/bold red] cache TTL must be positive")
        return 2

    metadata_started = time.monotonic()
    try:
        files = scan_audio_files(target)
    except OSError as error:
        error_console.print(f"[bold red]Error:[/bold red] cannot scan {target}: {error}")
        return 1
    if not files:
        error_console.print(f"[bold yellow]No supported audio files found in {target}[/bold yellow]")
        return 0

    outcomes: list[FileOutcome] = []
    pending_indexes: list[int] = []
    with console.status("[bold cyan]Reading existing metadata…[/bold cyan]"):
        for path in files:
            try:
                existing = read_existing_metadata(path)
            except MetadataError as error:
                outcomes.append(
                    FileOutcome(path, ExistingMetadata(None, None), None, "error", str(error))
                )
                continue
            if (
                not needs_lookup(existing, replace_existing=args.replace_existing)
                and placeholder_metadata_candidate(path, existing) is None
            ):
                outcomes.append(
                    FileOutcome(path, existing, None, "already_tagged", "artist and title exist")
                )
            else:
                pending_indexes.append(len(outcomes))
                outcomes.append(FileOutcome(path, existing, None, "error", "not scanned"))
    metadata_elapsed = time.monotonic() - metadata_started

    cache: RecognitionCache | None = None
    if pending_indexes and configuration.cache_enabled:
        try:
            cache = RecognitionCache(
                configuration.cache_file,
                ttl_seconds=configuration.cache_ttl_days * 86400,
                refresh=configuration.refresh_cache,
            )
        except (OSError, sqlite3.Error) as error:
            error_console.print(
                f"[yellow]Warning:[/yellow] recognition cache is unavailable: {error}"
            )

    discogs_client: DiscogsClient | None = None
    acoustid_client: RateLimitedLookup | None = None
    fingerprinter: CachedFingerprinter | None = None
    shazam_client: CachedShazamLookup | None = None
    identification_started = time.monotonic()

    if pending_indexes:
        discogs_lookup: Callable[[Match], Selection] | None = None
        if configuration.discogs_token:
            discogs_client = DiscogsClient(
                configuration.discogs_token,
                configuration.network_timeout,
                cache_get=(
                    (lambda key: cache.get("discogs-v1", key))
                    if cache is not None
                    else None
                ),
                cache_put=(
                    (lambda key, value: cache.put("discogs-v1", key, value))
                    if cache is not None
                    else None
                ),
            )
            discogs_lookup = discogs_client

        shazam_lookup: Callable[[Path], Selection] | None = None
        if configuration.shazam_enabled:
            ffmpeg = shutil.which(configuration.ffmpeg)
            if ffmpeg is None:
                if cache is not None:
                    cache.close()
                error_console.print(
                    "[bold red]Error:[/bold red] Shazam is enabled, but "
                    f"ffmpeg was not found: {configuration.ffmpeg}\n"
                    "On macOS, install it with: [bold]brew install ffmpeg[/bold]"
                )
                return 2
            try:
                base_shazam_lookup = ShazamClient(
                    ffmpeg,
                    configuration.fingerprint_timeout,
                    configuration.network_timeout,
                )
                shazam_client = CachedShazamLookup(base_shazam_lookup, cache)
                shazam_lookup = shazam_client
            except ProviderError as error:
                if cache is not None:
                    cache.close()
                error_console.print(
                    f"[bold red]Error:[/bold red] cannot enable Shazam: {error}"
                )
                return 2

        fpcalc: str | None = None
        acoustid_lookup: Callable[[Fingerprint], Mapping[str, Any]] | None = None
        if configuration.api_key:
            fpcalc = shutil.which(configuration.fpcalc)
            if fpcalc is None:
                if cache is not None:
                    cache.close()
                error_console.print(
                    "[bold red]Error:[/bold red] AcoustID is configured, but "
                    f"fpcalc was not found: {configuration.fpcalc}\n"
                    "On macOS, install it with: [bold]brew install chromaprint[/bold]"
                )
                return 2
            fingerprinter = CachedFingerprinter(
                fpcalc,
                configuration.fingerprint_timeout,
                cache,
            )
            acoustid_client = RateLimitedLookup(
                configuration.api_key,
                configuration.network_timeout,
                cache,
            )
            acoustid_lookup = acoustid_client

        with console.status("[bold cyan]Identifying missing metadata…[/bold cyan]") as status:
            for position, index in enumerate(pending_indexes, start=1):
                path = outcomes[index].path

                def show_stage(stage: str) -> None:
                    status.update(
                        f"[bold cyan]Identifying {position}/{len(pending_indexes)} "
                        f"· {stage}: {path.name}[/bold cyan]"
                    )

                show_stage("filename and existing tags")
                outcomes[index] = analyze_missing_file(
                    path,
                    outcomes[index].existing,
                    fpcalc=fpcalc,
                    acoustid_lookup=acoustid_lookup,
                    discogs_lookup=discogs_lookup,
                    shazam_lookup=shazam_lookup,
                    min_score=configuration.min_score,
                    min_margin=configuration.min_margin,
                    fingerprint_timeout=configuration.fingerprint_timeout,
                    replace_existing=args.replace_existing,
                    fingerprint_lookup=fingerprinter,
                    progress=show_stage,
                )
    identification_elapsed = time.monotonic() - identification_started

    writing_started = time.monotonic()
    if args.write:
        with console.status("[bold cyan]Writing accepted metadata…[/bold cyan]"):
            for index, outcome in enumerate(outcomes):
                if outcome.status != "ready" or outcome.match is None:
                    continue
                try:
                    changed = write_missing_metadata(
                        outcome.path,
                        outcome.match,
                        replace_existing=args.replace_existing,
                    )
                except MetadataError as error:
                    outcomes[index] = replace(outcome, status="error", detail=str(error))
                    continue
                verb = "replaced" if args.replace_existing else "added"
                detail = f"{verb} " + " and ".join(changed) if changed else "tags already complete"
                outcomes[index] = replace(outcome, status="written", detail=detail)
    writing_elapsed = time.monotonic() - writing_started

    cache_hits = cache.hits if cache is not None else 0
    cache_misses = cache.misses if cache is not None else 0
    if cache is not None:
        cache.close()

    metrics = RunMetrics(
        total=time.monotonic() - run_started,
        metadata=metadata_elapsed,
        identification=identification_elapsed,
        writing=writing_elapsed,
        fingerprint=fingerprinter.elapsed if fingerprinter is not None else 0.0,
        discogs=discogs_client.elapsed if discogs_client is not None else 0.0,
        acoustid=acoustid_client.elapsed if acoustid_client is not None else 0.0,
        shazam=shazam_client.elapsed if shazam_client is not None else 0.0,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        network_requests=(
            (discogs_client.network_requests if discogs_client is not None else 0)
            + (acoustid_client.network_requests if acoustid_client is not None else 0)
            + (
                shazam_client.calls - shazam_client.cache_hits
                if shazam_client is not None
                else 0
            )
        ),
    )

    render_report(
        console,
        target,
        outcomes,
        write=args.write,
        replace_existing=args.replace_existing,
        metrics=metrics,
    )
    return 1 if any(outcome.status == "error" for outcome in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
