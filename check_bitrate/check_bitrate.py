#!/usr/bin/env python3
"""Report MP3 bitrates; optionally confirm permanent removal below a cutoff."""

from __future__ import annotations

import argparse
import math
import os
import stat
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Sequence

from mutagen import MutagenError
from mutagen.mp3 import BitrateMode, MPEGInfo
from rich.console import Console
from rich.table import Table
from rich.text import Text


@dataclass(frozen=True)
class Bitrate:
    bps: int
    mode: str


@dataclass(frozen=True)
class Identity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    links: int

    @classmethod
    def from_stat(cls, details: os.stat_result) -> Identity:
        if not stat.S_ISREG(details.st_mode):
            raise OSError("not a regular file (symlinks are excluded)")
        return cls(details.st_dev, details.st_ino, details.st_size,
                   details.st_mtime_ns, details.st_ctime_ns, details.st_nlink)


@dataclass(frozen=True)
class Track:
    path: Path
    identity: Identity
    bitrate: Bitrate
    decision: str


@dataclass(frozen=True)
class Issue:
    path: Path
    detail: str


@dataclass(frozen=True)
class Plan:
    root: Path
    below_kbps: int
    tracks: tuple[Track, ...]
    skipped_links: int
    errors: tuple[Issue, ...]

    @property
    def candidates(self) -> tuple[Track, ...]:
        return tuple(track for track in self.tracks if track.decision == "DELETE candidate")


@dataclass(frozen=True)
class Removal:
    removed: tuple[Path, ...]
    errors: tuple[Issue, ...]


Reader = Callable[[BinaryIO], Bitrate]
Progress = Callable[[str, Path], None]


def read_bitrate(source: BinaryIO) -> Bitrate:
    """Read the audio stream, independently of filenames or ID3 bitrate tags."""
    info = MPEGInfo(source)
    if info.layer != 3 or info.sketchy:
        raise ValueError("not a reliably recognized MPEG Layer III stream")
    if not math.isfinite(info.bitrate) or info.bitrate <= 0:
        raise ValueError("missing or invalid audio bitrate")
    modes = {BitrateMode.CBR: "CBR", BitrateMode.VBR: "VBR",
             BitrateMode.ABR: "ABR", BitrateMode.UNKNOWN: "UNKNOWN"}
    mode = modes[info.bitrate_mode]
    # Info/Xing frame padding can make a 320 kbps CBR stream read 319,998 bps.
    # Only confirmed CBR is normalized; VBR/ABR retain their reported average.
    bps = round(info.bitrate / 1000) * 1000 if mode == "CBR" else info.bitrate
    return Bitrate(bps, mode)


def safe_paths_supported() -> bool:
    return (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
            and all(func in os.supports_dir_fd for func in (os.open, os.stat, os.unlink)))


@contextmanager
def open_parent(root: Path, path: Path) -> Iterator[int]:
    """Anchor each path component without following directory symlinks."""
    relative = path.relative_to(root)
    if not root.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("file path must be inside the absolute scan root")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root.anchor, flags)
    try:
        for component in (*root.parts[1:], *relative.parts[:-1]):
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def current_identity(path: Path, parent: int) -> Identity:
    return Identity.from_stat(os.stat(path.name, dir_fd=parent, follow_symlinks=False))


def inspect_track(root: Path, path: Path, reader: Reader) -> tuple[Identity, Bitrate]:
    with open_parent(root, path) as parent:
        before = current_identity(path, parent)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
        with os.fdopen(descriptor, "rb") as source:
            if Identity.from_stat(os.fstat(source.fileno())) != before:
                raise OSError("file changed before reading")
            bitrate = reader(source)
            if (Identity.from_stat(os.fstat(source.fileno())) != before
                    or current_identity(path, parent) != before):
                raise OSError("file changed while reading")
    if bitrate.bps <= 0 or bitrate.mode not in {"CBR", "VBR", "ABR", "UNKNOWN"}:
        raise ValueError("invalid bitrate result")
    return before, bitrate


def path_key(path: Path) -> tuple[str, str]:
    return unicodedata.normalize("NFD", str(path)).casefold(), str(path)


def build_plan(root: Path, below_kbps: int = 320, *, reader: Reader = read_bitrate,
               progress: Progress | None = None) -> Plan:
    """Recursively inspect MP3s; never modify files or directories."""
    if not 1 <= below_kbps <= 320:
        raise ValueError("below_kbps must be between 1 and 320")
    tracks: list[Track] = []
    errors: list[Issue] = []
    skipped_links = 0

    def walk_error(error: OSError) -> None:
        errors.append(Issue(Path(error.filename) if error.filename else root, str(error)))

    for current, directories, filenames in os.walk(root, followlinks=False, onerror=walk_error):
        folder = Path(current)
        if progress:
            progress("Scanning", folder)
        retained = []
        for name in sorted(directories):
            if (folder / name).is_symlink():
                skipped_links += 1
            else:
                retained.append(name)
        directories[:] = retained
        for name in sorted(filenames):
            path = folder / name
            if path.suffix.lower() != ".mp3":
                continue
            try:
                details = path.lstat()
                if stat.S_ISLNK(details.st_mode):
                    skipped_links += 1
                    continue
                if not stat.S_ISREG(details.st_mode):
                    continue
                if progress:
                    progress("Reading bitrate", path)
                identity, bitrate = inspect_track(root, path, reader)
                if bitrate.mode == "UNKNOWN":
                    decision = "SKIP: estimated bitrate"
                elif identity.links != 1:
                    decision = "SKIP: hard-linked"
                elif bitrate.bps < below_kbps * 1000:
                    decision = "DELETE candidate"
                else:
                    decision = "KEEP"
                tracks.append(Track(path, identity, bitrate, decision))
            except (OSError, MutagenError, ValueError) as error:
                errors.append(Issue(path, str(error)))
    tracks.sort(key=lambda track: path_key(track.path))
    errors.sort(key=lambda issue: path_key(issue.path))
    return Plan(root, below_kbps, tuple(tracks), skipped_links, tuple(errors))


def check_candidate(track: Track, parent: int) -> None:
    if current_identity(track.path, parent) != track.identity:
        raise OSError("file changed since analysis; run a fresh preview")
    details = os.stat(track.path.name, dir_fd=parent, follow_symlinks=False)
    flags = sum(getattr(stat, name, 0) for name in
                ("UF_IMMUTABLE", "SF_IMMUTABLE", "UF_APPEND", "SF_APPEND"))
    if any(getattr(item, "st_flags", 0) & flags for item in (details, os.fstat(parent))):
        raise OSError("file or parent directory is locked/immutable")
    if details.st_nlink != 1:
        raise OSError("hard-linked files cannot be deleted")


def execute_plan(plan: Plan, *, progress: Progress | None = None) -> Removal:
    """Revalidate every candidate first; stop on the first removal failure."""
    selected = plan.candidates
    if len({track.path for track in selected}) != len(selected):
        raise ValueError("duplicate deletion paths")
    for track in selected:
        if (track.bitrate.mode not in {"CBR", "VBR", "ABR"}
                or not 0 < track.bitrate.bps < plan.below_kbps * 1000):
            raise ValueError("invalid deletion candidate")
    errors: list[Issue] = []
    for track in selected:
        try:
            if progress:
                progress("Verifying", track.path)
            with open_parent(plan.root, track.path) as parent:
                check_candidate(track, parent)
        except OSError as error:
            errors.append(Issue(track.path, str(error)))
        except KeyboardInterrupt:
            return Removal((), (Issue(track.path, "verification interrupted"),))
    if errors:
        return Removal((), tuple(errors))
    removed: list[Path] = []
    for track in selected:
        try:
            if progress:
                progress("Deleting", track.path)
            with open_parent(plan.root, track.path) as parent:
                check_candidate(track, parent)
                os.unlink(track.path.name, dir_fd=parent)
            removed.append(track.path)
        except OSError as error:
            return Removal(tuple(removed), (Issue(track.path, str(error)),))
        except KeyboardInterrupt:
            return Removal(tuple(removed), (Issue(track.path, "deletion interrupted; rescan"),))
    return Removal(tuple(removed), ())


def render_plan(console: Console, plan: Plan) -> None:
    console.print(f"MP3 bitrate report — deletion cutoff: below {plan.below_kbps} kbps")
    ordered = sorted(plan.tracks, key=lambda track: (-track.bitrate.bps, path_key(track.path)))
    groups = (
        ("≥320 kbps", "green", [track for track in ordered if track.bitrate.bps >= 320000]),
        ("Below 320 kbps", "yellow", [track for track in ordered if track.bitrate.bps < 320000]),
    )
    for label, color, tracks in groups:
        table = Table(title=f"{label} — {len(tracks)} files", title_style=f"bold {color}",
                      border_style=color, header_style=f"bold {color}")
        for heading in ("File", "kbps", "Mode", "Decision"):
            table.add_column(heading)
        for track in tracks:
            table.add_row(Text(str(track.path.relative_to(plan.root))),
                          f"{track.bitrate.bps / 1000:.3f}".rstrip("0").rstrip("."),
                          track.bitrate.mode, track.decision, style=color)
        console.print(table)
    console.print("VBR/ABR: reported average. UNKNOWN: first-frame estimate; protected.")
    size = sum(track.identity.size for track in plan.candidates)
    console.print(f"Plan: {len(plan.candidates)} deletion candidates · {size / 1024**2:.2f} MiB")
    if plan.errors:
        console.print(f"Skipped {len(plan.errors)} files or folders that could not be analyzed; "
                      "excluded from deletion.")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="folder to scan recursively for .mp3 files")
    parser.add_argument("--below", type=int, default=320, metavar="KBPS",
                        help="select bitrates strictly below KBPS (1–320; default: 320)")
    parser.add_argument("--delete", action="store_true",
                        help="preview, then require typing DELETE for permanent removal (not Trash); "
                             "files with analysis errors are skipped")
    parser.add_argument("--dry-run", action="store_true",
                        help="read-only preview, even with --delete; never asks for confirmation")
    args = parser.parse_args(argv)
    if not 1 <= args.below <= 320:
        parser.error("--below must be between 1 and 320")
    return args


def main(argv: Sequence[str] | None = None, *, console: Console | None = None,
         error_console: Console | None = None, reader: Reader = read_bitrate,
         input_func: Callable[[str], str] | None = None,
         clock: Callable[[], float] = time.monotonic) -> int:
    args = parse_args(argv)
    console = console or Console()
    error_console = error_console or Console(stderr=True)
    started = clock()
    try:
        folder = args.folder.expanduser()
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("folder must be a real directory, not a symlink")
        root = folder.resolve(strict=True)
        if not safe_paths_supported():
            raise ValueError("safe file access requires macOS or Linux with POSIX dir_fd support")
    except (OSError, ValueError) as error:
        error_console.print(f"Error: {error}", markup=False)
        console.print("Summary: 0 analyzed · 0 deleted · 1 configuration error")
        return 2
    try:
        with console.status("Scanning MP3 files…") as status:
            plan = build_plan(root, args.below, reader=reader, progress=lambda stage, path:
                              status.update(Text(f"{stage}: {path.relative_to(root)}")))
    except KeyboardInterrupt:
        console.print("Summary: analysis interrupted · 0 deleted")
        return 1
    render_plan(console, plan)
    errors = list(plan.errors)
    for issue in plan.errors:
        error_console.print(f"Error (skipped): {issue.path}: {issue.detail}", markup=False)
    removed: tuple[Path, ...] = ()
    outcome = "read-only"
    if args.delete and not args.dry_run and plan.candidates:
        console.print("Deletion is permanent (not Trash). Only the listed candidates are selected.")
        try:
            answer = (input_func or input)(
                f"Type DELETE to permanently remove {len(plan.candidates)} MP3 files: ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        except OSError as error:
            errors.append(Issue(root, f"confirmation failed: {error}"))
            answer = ""
        outcome = "cancelled"
        if answer == "DELETE":
            with console.status("Verifying deletion plan…") as status:
                result = execute_plan(plan, progress=lambda stage, path:
                                      status.update(Text(f"{stage}: {path.relative_to(root)}")))
            removed = result.removed
            errors.extend(result.errors)
            outcome = "deletion stopped" if result.errors else "deletion complete"
            if not result.errors and plan.errors:
                outcome += " with analysis errors skipped"
    for path in removed:
        console.print(f"DELETED {path.relative_to(root)}", markup=False)
    for issue in errors[len(plan.errors):]:
        error_console.print(f"Error: {issue.path}: {issue.detail}", markup=False)
    protected = sum(track.decision.startswith("SKIP") for track in plan.tracks)
    console.print(f"Summary: {len(plan.tracks)} analyzed · {len(plan.candidates)} candidates · "
                  f"{protected} protected · {plan.skipped_links} symlinks skipped · "
                  f"{len(removed)} deleted · {len(errors)} errors · "
                  f"{clock() - started:.2f}s · {outcome}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
