"""Recognition providers and filename parsing for create_metadata."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from mutagen import File as MutagenFile


DISCOGS_SEARCH_URL = "https://api.discogs.com/database/search"
DISCOGS_REQUEST_INTERVAL = 1.05
LEADING_LIBRARY_CODE = re.compile(
    r"^(?:(?:1[0-2]|[1-9])[AB]|\d{1,3}|[A-Z]{1,6}\d{2,6})$", re.IGNORECASE
)
SEPARATOR_DASH = re.compile(r"(?:\s+[-\u2010-\u2015]\s*|\s*[-\u2010-\u2015]\s+)")
UNKNOWN_ARTIST = re.compile(
    r"^(?:\d{1,3}\s+)?(?:\[\s*)?unknown(?:\s+artist)?(?:\s*\])?$",
    re.IGNORECASE,
)
DOWNLOAD_MARKER = re.compile(
    r"\s*[\[(](?:www\.)?[a-z0-9_-]+(?:\.[a-z0-9_-]+)+[^\])]*[\])]\s*$",
    re.IGNORECASE,
)
DISCOGS_ARTIST_NUMBER = re.compile(r"\s+\(\d+\)$")


class ProviderError(Exception):
    """An external recognition provider failed."""


@dataclass(frozen=True)
class Match:
    artist: str
    title: str
    score: float
    recording_id: str | None = None
    source: str = "AcoustID"


@dataclass(frozen=True)
class Selection:
    match: Match | None
    status: str
    detail: str


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return text or None


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"[^\w]+", " ", value)
    return " ".join(value.split())


def matches_agree(left: Match, right: Match) -> bool:
    return (
        normalized(left.artist) == normalized(right.artist)
        and normalized(left.title) == normalized(right.title)
    )


def filename_candidate_from_stem(stem: str) -> Match | None:
    """Extract an Artist - Title candidate from filename-like text."""
    stem = unicodedata.normalize("NFC", stem)
    stem = DOWNLOAD_MARKER.sub("", stem).strip()
    parts = [part.strip() for part in SEPARATOR_DASH.split(stem) if part.strip()]
    while len(parts) > 2 and (
        LEADING_LIBRARY_CODE.fullmatch(parts[0])
        or UNKNOWN_ARTIST.fullmatch(parts[0])
    ):
        parts.pop(0)
    if len(parts) < 2:
        return None

    artist = parts[0]
    title = " - ".join(parts[1:])
    if not artist or not title or UNKNOWN_ARTIST.fullmatch(artist):
        return None
    return Match(artist, title, 0.70, source="Filename")


def filename_candidate(path: Path) -> Match | None:
    """Extract an Artist - Title candidate from a common DJ filename."""
    return filename_candidate_from_stem(path.stem)


def credited_artists(items: Any) -> str | None:
    if not isinstance(items, list):
        return None
    names: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = clean_text(item.get("name"))
        if name:
            names.append(DISCOGS_ARTIST_NUMBER.sub("", name))
    return ", ".join(names) or None


def similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalized(left), normalized(right)).ratio()


def select_discogs_match(
    candidate: Match,
    releases: Sequence[Mapping[str, Any]],
    *,
    min_score: float = 0.86,
    min_margin: float = 0.04,
) -> Selection:
    """Find the filename candidate in fetched Discogs release tracklists."""
    matches: dict[tuple[str, str], Match] = {}
    for release in releases:
        release_artist = credited_artists(release.get("artists"))
        if not release_artist:
            release_artist = clean_text(release.get("artists_sort"))
        tracklist = release.get("tracklist")
        if not isinstance(tracklist, list):
            continue
        for track in tracklist:
            if not isinstance(track, Mapping):
                continue
            title = clean_text(track.get("title"))
            artist = credited_artists(track.get("artists")) or release_artist
            if not artist or not title:
                continue
            artist_score = similarity(candidate.artist, artist)
            title_score = similarity(candidate.title, title)
            score = (artist_score * 0.45) + (title_score * 0.55)
            if artist_score < 0.72 or title_score < 0.78 or score < min_score:
                continue
            match = Match(
                artist,
                title,
                score,
                clean_text(release.get("id")),
                "Discogs",
            )
            key = (normalized(artist), normalized(title))
            previous = matches.get(key)
            if previous is None or match.score > previous.score:
                matches[key] = match

    ranked = sorted(
        matches.values(),
        key=lambda match: (-match.score, match.artist.casefold(), match.title.casefold()),
    )
    if not ranked:
        return Selection(None, "no_match", "Discogs did not validate the filename")
    if len(ranked) > 1 and ranked[0].score - ranked[1].score < min_margin:
        return Selection(None, "ambiguous", "Discogs returned close track candidates")
    return Selection(ranked[0], "ready", "filename validated by Discogs")


def read_json_response(response: Any, provider: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(response.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError(f"{provider} returned invalid JSON") from error
    if not isinstance(payload, Mapping):
        raise ProviderError(f"{provider} returned an unexpected response")
    return payload


class DiscogsClient:
    """Validate filename metadata against Discogs release tracklists."""

    def __init__(
        self,
        token: str,
        timeout: float,
        *,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        cache_get: Callable[[str], Any | None] | None = None,
        cache_put: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self.urlopen = urlopen
        self.cache_get = cache_get
        self.cache_put = cache_put
        self.last_request: float | None = None
        self.network_requests = 0
        self.cache_hits = 0
        self.elapsed = 0.0

    def request(self, url: str) -> Mapping[str, Any]:
        started = time.monotonic()
        if self.cache_get is not None:
            cached = self.cache_get(url)
            if isinstance(cached, Mapping):
                self.cache_hits += 1
                self.elapsed += time.monotonic() - started
                return cached
        if self.last_request is not None:
            remaining = DISCOGS_REQUEST_INTERVAL - (
                time.monotonic() - self.last_request
            )
            if remaining > 0:
                time.sleep(remaining)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Discogs token={self.token}",
                "User-Agent": "create-metadata/1.0",
            },
        )
        try:
            self.network_requests += 1
            with self.urlopen(request, timeout=self.timeout) as response:
                payload = read_json_response(response, "Discogs")
            if self.cache_put is not None:
                self.cache_put(url, payload)
            return payload
        except urllib.error.HTTPError as error:
            raise ProviderError(f"Discogs returned HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise ProviderError(f"cannot reach Discogs: {error.reason}") from error
        except TimeoutError as error:
            raise ProviderError(
                f"Discogs timed out after {self.timeout:g} seconds"
            ) from error
        finally:
            self.last_request = time.monotonic()
            self.elapsed += self.last_request - started

    def __call__(self, candidate: Match) -> Selection:
        query = urllib.parse.urlencode(
            {
                "artist": candidate.artist,
                "track": candidate.title,
                "type": "release",
                "per_page": 5,
            }
        )
        search = self.request(f"{DISCOGS_SEARCH_URL}?{query}")
        results = search.get("results")
        if not isinstance(results, list):
            return Selection(None, "no_match", "Discogs returned no releases")

        releases: list[Mapping[str, Any]] = []
        for result in results[:5]:
            if not isinstance(result, Mapping):
                continue
            resource_url = clean_text(result.get("resource_url"))
            if resource_url and resource_url.startswith("https://api.discogs.com/"):
                releases.append(self.request(resource_url))
        return select_discogs_match(candidate, releases)


def audio_duration(path: Path) -> float:
    try:
        audio = MutagenFile(path)
        duration = float(getattr(getattr(audio, "info", None), "length", 0.0))
    except (OSError, TypeError, ValueError) as error:
        raise ProviderError(f"cannot determine audio duration: {error}") from error
    if duration <= 0:
        raise ProviderError("cannot determine audio duration")
    return duration


def create_audio_sample(
    path: Path, output: Path, ffmpeg: str, *, timeout: float
) -> None:
    """Create a short middle-of-track sample for acoustic recognition."""
    duration = audio_duration(path)
    sample_length = min(14.0, duration)
    start = max(0.0, (duration - sample_length) * 0.50)
    try:
        process = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{start:.3f}",
                "-i",
                str(path),
                "-t",
                f"{sample_length:.3f}",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "44100",
                "-b:a",
                "128k",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ProviderError(f"ffmpeg timed out after {timeout:g} seconds") from error
    except OSError as error:
        raise ProviderError(f"cannot run ffmpeg: {error}") from error
    if process.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        detail = process.stderr.strip() or f"ffmpeg exited with {process.returncode}"
        raise ProviderError(f"cannot create recognition sample: {detail}")


def select_shazam_match(payload: Mapping[str, Any]) -> Selection:
    """Extract the single artist/title match returned by Shazam."""
    matches = payload.get("matches")
    if not isinstance(matches, list):
        raise ProviderError("Shazam returned an unexpected response")
    if not matches:
        return Selection(None, "no_match", "Shazam found no match")

    track = payload.get("track")
    if not isinstance(track, Mapping):
        raise ProviderError("Shazam returned a match without track metadata")
    artist = clean_text(track.get("subtitle"))
    title = clean_text(track.get("title"))
    if not artist or not title:
        return Selection(None, "no_match", "Shazam returned no artist/title")

    recording_id = clean_text(track.get("key"))
    if recording_id is None and isinstance(matches[0], Mapping):
        recording_id = clean_text(matches[0].get("id"))
    # Shazam exposes a single match, not a numerical confidence value. The 1.0
    # sentinel keeps Match compatible; reports deliberately omit a percentage.
    match = Match(artist, title, 1.0, recording_id, "Shazam")
    return Selection(match, "ready", "audio recognized by Shazam")


AsyncRecognizer = Callable[[Path], Awaitable[Mapping[str, Any]]]


def load_shazam_recognizer() -> tuple[AsyncRecognizer, tuple[type[BaseException], ...]]:
    """Load the optional unofficial Shazam client only when it is enabled."""
    try:
        from aiohttp import ClientError
        from shazamio import Shazam
        from shazamio.exceptions import BadParseData, FailedDecodeJson
        from shazamio_core import SignatureError
    except (ImportError, ModuleNotFoundError) as error:
        raise ProviderError(
            "Shazam is enabled, but its Python packages are unavailable; "
            "install create_metadata/requirements.txt"
        ) from error

    async def recognize(path: Path) -> Mapping[str, Any]:
        async with Shazam() as shazam:
            payload = await shazam.recognize(path)
        if not isinstance(payload, Mapping):
            raise ProviderError("Shazam returned an unexpected response")
        return payload

    return recognize, (ClientError, BadParseData, FailedDecodeJson, SignatureError)


class ShazamClient:
    """Run ShazamIO's asynchronous recognizer from the synchronous CLI."""

    def __init__(
        self,
        ffmpeg: str,
        sample_timeout: float,
        network_timeout: float,
        *,
        recognizer: AsyncRecognizer | None = None,
        provider_errors: tuple[type[BaseException], ...] = (),
    ) -> None:
        if recognizer is None:
            recognizer, provider_errors = load_shazam_recognizer()
        self.ffmpeg = ffmpeg
        self.sample_timeout = sample_timeout
        self.network_timeout = network_timeout
        self.recognizer = recognizer
        self.provider_errors = (OSError, ValueError, *provider_errors)

    def __call__(self, path: Path) -> Selection:
        with tempfile.TemporaryDirectory(prefix="create-metadata-") as temp:
            sample_path = Path(temp) / "sample.mp3"
            create_audio_sample(
                path,
                sample_path,
                self.ffmpeg,
                timeout=self.sample_timeout,
            )

            async def recognize_with_timeout() -> Mapping[str, Any]:
                return await asyncio.wait_for(
                    self.recognizer(sample_path), timeout=self.network_timeout
                )

            try:
                payload = asyncio.run(recognize_with_timeout())
            except (TimeoutError, asyncio.TimeoutError) as error:
                raise ProviderError(
                    f"Shazam timed out after {self.network_timeout:g} seconds"
                ) from error
            except self.provider_errors as error:
                detail = clean_text(error) or error.__class__.__name__
                raise ProviderError(f"Shazam recognition failed: {detail}") from error
        return select_shazam_match(payload)
