#!/usr/bin/env python3
"""Build M3U8 crates from local music tags without changing audio files."""

from __future__ import annotations

import argparse
import math
import os
import re
import stat
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from mutagen import File as MutagenFile
from mutagen import MutagenError
from rich.console import Console
from rich.table import Table
from rich.text import Text


AUDIO_EXTENSIONS = frozenset({
    ".aac", ".aif", ".aiff", ".alac", ".ape", ".dsf", ".flac", ".m4a",
    ".m4b", ".mp3", ".mp4", ".mpc", ".ogg", ".oga", ".opus", ".wav",
    ".wma", ".wv",
})
INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.I)


@dataclass(frozen=True)
class Track:
    path: Path
    artist: str | None
    title: str | None
    labels: tuple[str, ...]
    bpm: float | None
    added_at: float


@dataclass(frozen=True)
class Issue:
    path: Path
    detail: str


@dataclass(frozen=True)
class Crate:
    name: str
    path: Path
    tracks: tuple[Track, ...]


@dataclass(frozen=True)
class Plan:
    root: Path
    output_dir: Path
    crates: tuple[Crate, ...]
    scanned: int
    skipped_links: int
    errors: tuple[Issue, ...]


Progress = Callable[[str, Path], None]
Reader = Callable[[Path], tuple[str | None, str | None, tuple[str, ...], float | None]]


def tag_values(tags: Any, names: tuple[str, ...]) -> tuple[str, ...]:
    """Find text in common ID3, Vorbis, ASF, and MP4 tag representations."""
    if tags is None:
        return ()
    wanted = {name.casefold() for name in names}
    values: list[str] = []
    for key, item in tags.items():
        if str(key).casefold() not in wanted:
            continue
        raw = getattr(item, "text", item)
        for value in raw if isinstance(raw, (list, tuple)) else (raw,):
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            clean = str(value).strip()
            if clean:
                values.append(clean)
    return tuple(dict.fromkeys(values))


def read_tags(path: Path, opener: Callable[..., Any] = MutagenFile
              ) -> tuple[str | None, str | None, tuple[str, ...], float | None]:
    audio = opener(path, easy=False)
    if audio is None:
        raise ValueError("unsupported or unreadable audio format")
    tags = getattr(audio, "tags", None)
    artist = tag_values(tags, ("artist", "TPE1", "©ART", "Author"))
    title = tag_values(tags, ("title", "TIT2", "©nam"))
    labels = tag_values(tags, (
        "label", "publisher", "organization", "TPUB", "TXXX:LABEL",
        "TXXX:Record Label", "----:com.apple.iTunes:LABEL", "©pub",
    ))
    bpm_values = tag_values(tags, ("bpm", "TBPM", "tmpo", "WM/BeatsPerMinute"))
    bpm: float | None = None
    if bpm_values:
        try:
            candidate = float(bpm_values[0])
            if math.isfinite(candidate) and candidate > 0:
                bpm = candidate
        except ValueError:
            pass
    return (artist[0] if artist else None, title[0] if title else None,
            labels, bpm)


def scan(root: Path, reader: Reader = read_tags,
         progress: Progress | None = None) -> tuple[tuple[Track, ...], int, tuple[Issue, ...]]:
    tracks: list[Track] = []
    issues: list[Issue] = []
    skipped = 0

    def walk_error(error: OSError) -> None:
        issues.append(Issue(Path(error.filename or root), str(error)))

    for current, directories, filenames in os.walk(root, topdown=True,
                                                    followlinks=False, onerror=walk_error):
        folder = Path(current)
        kept: list[str] = []
        for name in sorted(directories, key=lambda x: (x.casefold(), x)):
            if (folder / name).is_symlink():
                skipped += 1
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(filenames, key=lambda x: (x.casefold(), x)):
            path = folder / name
            if path.is_symlink():
                skipped += 1
                continue
            if path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            if progress:
                progress("Reading tags", path)
            try:
                if "\n" in str(path) or "\r" in str(path):
                    raise ValueError("path contains a newline and cannot be used in M3U8")
                details = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(details.st_mode):
                    continue
                artist, title, labels, bpm = reader(path)
                added_at = getattr(details, "st_birthtime", details.st_mtime)
                tracks.append(Track(path, artist, title, labels, bpm, added_at))
            except (OSError, MutagenError, ValueError) as error:
                issues.append(Issue(path, str(error)))
    tracks.sort(key=lambda track: (str(track.path.relative_to(root)).casefold(),
                                   str(track.path.relative_to(root))))
    return tuple(tracks), skipped, tuple(issues)


def safe_name(value: str) -> str:
    name = INVALID_NAME.sub("_", unicodedata.normalize("NFC", value))
    name = " ".join(name.split()).strip(" .") or "Untitled"
    if WINDOWS_RESERVED.fullmatch(name):
        name += "_"
    while len((name + ".m3u8").encode("utf-8")) > 240:
        name = name[:-1].rstrip(" .")
    return name or "Untitled"


def collision_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def allocate_path(output_dir: Path, title: str, used: set[str]) -> Path:
    base = safe_name(title)
    number = 1
    while True:
        suffix = "" if number == 1 else f" [{number}]"
        stem = base
        while len((stem + suffix + ".m3u8").encode("utf-8")) > 240:
            stem = stem[:-1]
        filename = stem + suffix + ".m3u8"
        key = collision_key(filename)
        if key not in used:
            used.add(key)
            return output_dir / filename
        number += 1


def build_plan(root: Path, output_dir: Path, tracks: tuple[Track, ...],
               existing: Sequence[str], *, now: float, recent_days: int | None,
               labels: tuple[str, ...], all_labels: bool,
               bpm_ranges: tuple[tuple[float, float], ...],
               bpm_buckets: int | None, needs_review: bool,
               skipped_links: int = 0, errors: tuple[Issue, ...] = ()) -> Plan:
    selections: list[tuple[str, tuple[Track, ...]]] = []
    if recent_days is not None:
        start = now - recent_days * 86400
        selections.append((f"Recent Additions - {recent_days} Days",
                           tuple(t for t in tracks if start <= t.added_at <= now)))
    requested_labels = list(labels)
    if all_labels:
        requested_labels.extend(label for t in tracks for label in t.labels)
    unique_labels = {label.casefold(): label for label in sorted(
        requested_labels, key=lambda x: (x.casefold(), x), reverse=True)}
    for key in sorted(unique_labels):
        label = unique_labels[key]
        selections.append((f"Label - {label}", tuple(
            t for t in tracks if any(item.casefold() == key for item in t.labels))))
    ranges = list(bpm_ranges)
    if bpm_buckets is not None:
        bucket_starts = sorted({int(t.bpm // bpm_buckets) * bpm_buckets
                                for t in tracks if t.bpm is not None})
        ranges.extend((float(start), float(start + bpm_buckets)) for start in bucket_starts)
    for low, high in ranges:
        high_label = f"{high:g}" if bpm_buckets is None or (low, high) in bpm_ranges else f"{high:g} exclusive"
        selections.append((f"BPM - {low:g} to {high_label}", tuple(
            t for t in tracks if t.bpm is not None and low <= t.bpm < high)))
    if needs_review:
        selections.append(("Needs Review - Missing Artist or Title", tuple(
            t for t in tracks if not t.artist or not t.title)))
    used = {collision_key(name) for name in existing}
    crates = tuple(Crate(name, allocate_path(output_dir, name, used), selected)
                   for name, selected in selections if selected)
    return Plan(root, output_dir, crates, len(tracks), skipped_links, errors)


def playlist_text(crate: Crate) -> str:
    lines = ["#EXTM3U"]
    for track in crate.tracks:
        relative = os.path.relpath(track.path, crate.path.parent)
        if "\n" in relative or "\r" in relative:
            raise ValueError(f"path contains a newline: {track.path}")
        lines.append(relative.replace(os.sep, "/"))
    return "\n".join(lines) + "\n"


def write_plan(plan: Plan, progress: Progress | None = None) -> tuple[tuple[Path, ...], tuple[Issue, ...]]:
    written: list[Path] = []
    errors: list[Issue] = []
    if not plan.crates:
        return (), ()
    try:
        if plan.output_dir.is_symlink():
            raise OSError("output directory is a symlink")
        plan.output_dir.mkdir(mode=0o755, exist_ok=True)
        if not plan.output_dir.is_dir() or plan.output_dir.is_symlink():
            raise OSError("output directory is not a real directory")
        directory = os.open(plan.output_dir, os.O_RDONLY | os.O_DIRECTORY |
                            getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        return (), (Issue(plan.output_dir, str(error)),)
    try:
        for crate in plan.crates:
            if progress:
                progress("Writing playlist", crate.path)
            try:
                content = playlist_text(crate)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(crate.path.name, flags, 0o644, dir_fd=directory)
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                        output.write(content)
                except (OSError, UnicodeError):
                    os.unlink(crate.path.name, dir_fd=directory)
                    raise
                written.append(crate.path)
            except (OSError, UnicodeError, ValueError) as error:
                errors.append(Issue(crate.path, str(error)))
    finally:
        os.close(directory)
    return tuple(written), tuple(errors)


def bpm_range(value: str) -> tuple[float, float]:
    try:
        lower, upper = (float(part.strip()) for part in value.split(":"))
    except (ValueError, TypeError) as error:
        raise argparse.ArgumentTypeError("use MIN:MAX, for example 120:130") from error
    if not all(math.isfinite(number) for number in (lower, upper)) or lower <= 0 or upper <= lower:
        raise argparse.ArgumentTypeError("BPM range must have 0 < MIN < MAX")
    return lower, upper


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or write M3U8 crates from existing audio tags.")
    parser.add_argument("folder", type=Path, help="music folder to scan recursively")
    parser.add_argument("--write", action="store_true", help="write new playlists; never change audio or overwrite existing files")
    parser.add_argument("--output-name", default="Crates", metavar="NAME",
                        help="direct child folder for playlists (default: Crates)")
    parser.add_argument("--recent-days", type=int, metavar="DAYS",
                        help="playlist for files added in the last DAYS days")
    parser.add_argument("--label", action="append", default=[], metavar="LABEL",
                        help="playlist for an exact label tag; repeatable")
    parser.add_argument("--all-labels", action="store_true", help="one playlist per discovered label")
    parser.add_argument("--bpm", action="append", type=bpm_range, default=[], metavar="MIN:MAX",
                        help="playlist with MIN <= BPM < MAX; repeatable")
    parser.add_argument("--bpm-buckets", type=int, metavar="WIDTH",
                        help="one playlist per populated BPM bucket of WIDTH")
    parser.add_argument("--needs-review", action="store_true",
                        help="playlist for tracks missing artist or title tags")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    console = Console()
    err = Console(stderr=True)
    started = time.monotonic()
    if args.folder.is_symlink() or not args.folder.is_dir():
        err.print("[red]Error:[/red] folder must be a real directory")
        return 2
    if (args.output_name in {"", ".", ".."} or
            Path(args.output_name).name != args.output_name or
            INVALID_NAME.search(args.output_name) or
            args.output_name.endswith((" ", ".")) or
            WINDOWS_RESERVED.fullmatch(args.output_name)):
        err.print("[red]Error:[/red] --output-name must be one folder name")
        return 2
    if args.recent_days is not None and args.recent_days < 1:
        err.print("[red]Error:[/red] --recent-days must be positive")
        return 2
    if args.bpm_buckets is not None and not 1 <= args.bpm_buckets <= 200:
        err.print("[red]Error:[/red] --bpm-buckets must be 1–200")
        return 2
    if any(not label.strip() for label in args.label):
        err.print("[red]Error:[/red] --label cannot be empty")
        return 2
    root = args.folder.resolve()
    output_dir = root / args.output_name
    if output_dir.is_symlink() or (output_dir.exists() and not output_dir.is_dir()):
        err.print("[red]Error:[/red] output path must be a real directory or absent")
        return 2
    selected = any((args.recent_days is not None, args.label, args.all_labels,
                    args.bpm, args.bpm_buckets is not None, args.needs_review))
    recent_days = args.recent_days if selected else 30
    all_labels = args.all_labels or not selected
    buckets = args.bpm_buckets if selected else 20
    needs_review = args.needs_review or not selected
    try:
        with console.status("Scanning music files") as status:
            tracks, skipped, issues = scan(root, progress=lambda stage, path: status.update(
                Text(f"{stage}: {path.relative_to(root)}")))
        existing = tuple(path.name for path in output_dir.iterdir()) if output_dir.exists() else ()
    except OSError as error:
        err.print(f"[red]Error:[/red] scan failed: {error}")
        return 1
    plan = build_plan(root, output_dir, tracks, existing, now=time.time(),
                      recent_days=recent_days, labels=tuple(args.label),
                      all_labels=all_labels, bpm_ranges=tuple(args.bpm),
                      bpm_buckets=buckets, needs_review=needs_review,
                      skipped_links=skipped, errors=issues)
    table = Table(title="Crate plan" + (" — preview" if not args.write else ""))
    table.add_column("Playlist")
    table.add_column("Tracks", justify="right")
    table.add_column("Output")
    for crate in plan.crates:
        table.add_row(crate.name, str(len(crate.tracks)), str(crate.path.relative_to(root)))
    console.print(table)
    for issue in plan.errors:
        err.print(f"[red]Error:[/red] {issue.path}: {issue.detail}")
    written: tuple[Path, ...] = ()
    write_errors: tuple[Issue, ...] = ()
    if args.write:
        with console.status("Writing playlists") as status:
            written, write_errors = write_plan(plan, progress=lambda stage, path: status.update(
                Text(f"{stage}: {path.relative_to(root)}")))
        for issue in write_errors:
            err.print(f"[red]Error:[/red] {issue.path}: {issue.detail}")
    console.print(f"Summary: {plan.scanned} tracks scanned · {len(plan.crates)} crates planned · "
                  f"{len(written)} written · {plan.skipped_links} symlinks skipped · "
                  f"{len(plan.errors) + len(write_errors)} errors · {time.monotonic() - started:.1f}s")
    if not args.write:
        console.print("Preview only. Add --write to create the listed playlists.")
    return 1 if plan.errors or write_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
