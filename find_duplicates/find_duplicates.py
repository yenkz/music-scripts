#!/usr/bin/env python3
"""Find and review byte-for-byte duplicate audio files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if __package__:
    from .audio_match import AudioSignature, NamePair, decoded_signature, find_name_pairs
else:
    from audio_match import AudioSignature, NamePair, decoded_signature, find_name_pairs


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
HASH_CHUNK_SIZE = 1024 * 1024
MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FileRecord:
    path: Path
    size: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class ScanError:
    path: Path
    detail: str


@dataclass(frozen=True)
class ScanResult:
    files: tuple[FileRecord, ...]
    skipped_symlinks: tuple[Path, ...]
    errors: tuple[ScanError, ...]


@dataclass(frozen=True)
class DuplicateGroup:
    sha256: str
    size: int
    files: tuple[FileRecord, ...]
    # Audio-verified groups can contain different tags, sizes and whole-file hashes.
    file_hashes: tuple[tuple[Path, str], ...] = ()


@dataclass(frozen=True)
class SearchResult:
    groups: tuple[DuplicateGroup, ...]
    hashed_files: int
    errors: tuple[ScanError, ...]


@dataclass(frozen=True)
class SimilarResult:
    pairs: tuple[NamePair, ...]
    groups: tuple[DuplicateGroup, ...]
    errors: tuple[ScanError, ...]
    decoded_files: int
    reused_files: int


@dataclass(frozen=True)
class ReviewDecision:
    group: DuplicateGroup
    status: str
    keeper: Path | None = None


@dataclass(frozen=True)
class RemovalResult:
    removed: tuple[Path, ...]
    errors: tuple[ScanError, ...]


class FileChangedError(OSError):
    """Raised when a candidate changes between discovery and hashing."""


def path_key(path: Path, root: Path) -> tuple[str, str]:
    relative = str(path.relative_to(root))
    return unicodedata.normalize("NFD", relative).casefold(), relative


def record_from_stat(path: Path, details: os.stat_result) -> FileRecord:
    return FileRecord(
        path=path,
        size=details.st_size,
        device=details.st_dev,
        inode=details.st_ino,
        mtime_ns=details.st_mtime_ns,
        ctime_ns=details.st_ctime_ns,
    )


def same_identity(record: FileRecord, details: os.stat_result) -> bool:
    return (
        stat.S_ISREG(details.st_mode)
        and record.size == details.st_size
        and record.device == details.st_dev
        and record.inode == details.st_ino
        and record.mtime_ns == details.st_mtime_ns
        and record.ctime_ns == details.st_ctime_ns
    )


def scan_audio_files(root: Path) -> ScanResult:
    """Discover regular audio files without following symlinks."""
    files: list[FileRecord] = []
    skipped_symlinks: list[Path] = []
    errors: list[ScanError] = []

    def walk_error(error: OSError) -> None:
        location = Path(error.filename) if error.filename else root
        errors.append(ScanError(location, str(error)))

    for current, dirnames, filenames in os.walk(
        root, topdown=True, followlinks=False, onerror=walk_error
    ):
        current_path = Path(current)
        traversable: list[str] = []
        for dirname in sorted(dirnames, key=str.casefold):
            directory = current_path / dirname
            try:
                if directory.is_symlink():
                    skipped_symlinks.append(directory)
                else:
                    traversable.append(dirname)
            except OSError as error:
                errors.append(ScanError(directory, str(error)))
        dirnames[:] = traversable

        for filename in sorted(filenames, key=str.casefold):
            path = current_path / filename
            try:
                details = path.lstat()
            except OSError as error:
                errors.append(ScanError(path, str(error)))
                continue
            if stat.S_ISLNK(details.st_mode):
                skipped_symlinks.append(path)
                continue
            if not stat.S_ISREG(details.st_mode):
                continue
            if path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            files.append(record_from_stat(path, details))

    files.sort(key=lambda record: path_key(record.path, root))
    skipped_symlinks.sort(key=lambda path: path_key(path, root))
    errors.sort(key=lambda error: path_key(error.path, root))
    return ScanResult(tuple(files), tuple(skipped_symlinks), tuple(errors))


def hash_file(
    record: FileRecord, chunk_size: int = HASH_CHUNK_SIZE,
    *, directory_fd: int | None = None,
) -> str:
    """Hash a file and reject replacements or changes during the read."""
    try:
        before = os.stat(
            record.path if directory_fd is None else record.path.name,
            dir_fd=directory_fd, follow_symlinks=False,
        )
    except OSError as error:
        raise FileChangedError(f"cannot stat before hashing: {error}") from error
    if not same_identity(record, before):
        raise FileChangedError("file changed after discovery")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(
            record.path if directory_fd is None else record.path.name,
            flags, dir_fd=directory_fd,
        )
    except OSError as error:
        raise OSError(f"cannot open for hashing: {error}") from error

    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if not same_identity(record, opened):
            raise FileChangedError("file changed before hashing began")
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            while chunk := source.read(chunk_size):
                digest.update(chunk)
            after_read = os.fstat(source.fileno())
        if not same_identity(record, after_read):
            raise FileChangedError("file changed while it was being hashed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    try:
        after_path = os.stat(
            record.path if directory_fd is None else record.path.name,
            dir_fd=directory_fd, follow_symlinks=False,
        )
    except OSError as error:
        raise FileChangedError(f"cannot stat after hashing: {error}") from error
    if not same_identity(record, after_path):
        raise FileChangedError("file changed while it was being hashed")
    return digest.hexdigest()


def find_duplicate_groups(
    root: Path,
    files: Sequence[FileRecord],
    hasher: Callable[[FileRecord], str] = hash_file,
    progress: Callable[[Path, int, int], None] | None = None,
) -> SearchResult:
    """Hash only size-collision candidates and return exact duplicate groups."""
    by_size: dict[int, list[FileRecord]] = defaultdict(list)
    for record in files:
        by_size[record.size].append(record)
    candidates = [
        record
        for size in sorted(by_size)
        if len(by_size[size]) > 1
        for record in sorted(by_size[size], key=lambda item: path_key(item.path, root))
    ]

    by_digest: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
    errors: list[ScanError] = []
    total = len(candidates)
    for index, record in enumerate(candidates, start=1):
        if progress is not None:
            progress(record.path, index, total)
        try:
            digest = hasher(record)
        except OSError as error:
            errors.append(ScanError(record.path, str(error)))
            continue
        by_digest[(record.size, digest)].append(record)

    groups = [
        DuplicateGroup(
            sha256=digest,
            size=size,
            files=tuple(sorted(records, key=lambda item: path_key(item.path, root))),
        )
        for (size, digest), records in by_digest.items()
        if len(records) > 1
    ]
    groups.sort(key=lambda group: path_key(group.files[0].path, root))
    errors.sort(key=lambda error: path_key(error.path, root))
    return SearchResult(tuple(groups), len(candidates), tuple(errors))


def hard_link_sets(group: DuplicateGroup) -> tuple[tuple[FileRecord, ...], ...]:
    by_inode: dict[tuple[int, int], list[FileRecord]] = defaultdict(list)
    for record in group.files:
        by_inode[(record.device, record.inode)].append(record)
    return tuple(tuple(records) for records in by_inode.values() if len(records) > 1)


def find_similar_groups(
    root: Path, files: Sequence[FileRecord], threshold: float,
    signature_reader: Callable[[Path], AudioSignature],
    progress: Callable[[str, Path], None] | None = None,
) -> SimilarResult:
    pairs = find_name_pairs(
        [record.path for record in files], threshold,
        (lambda path, index, total: progress(f"Comparing names {index}/{total}", path))
        if progress else None,
    )
    candidates = {path for pair in pairs for path in (pair.first, pair.second)}
    records = sorted((record for record in files if record.path in candidates),
                     key=lambda record: path_key(record.path, root))
    signatures: dict[str, AudioSignature] = {}
    grouped: dict[AudioSignature, list[FileRecord]] = defaultdict(list)
    hashes: dict[Path, str] = {}
    errors: list[ScanError] = []
    decoded = reused = 0
    for index, record in enumerate(records, start=1):
        try:
            if progress:
                progress(f"Hashing candidate {index}/{len(records)}", record.path)
            digest = hash_file(record)
            signature = signatures.get(digest)
            if signature is None:
                if progress:
                    progress(f"Decoding audio {index}/{len(records)}", record.path)
                decoded += 1
                signature = signature_reader(record.path)
            else:
                reused += 1
            if not same_identity(record, record.path.lstat()):
                raise FileChangedError("file changed during audio verification")
            signatures[digest] = signature
            grouped[signature].append(record)
            hashes[record.path] = digest
        except OSError as error:
            errors.append(ScanError(record.path, str(error)))
    groups = [
        DuplicateGroup(signature.sha256, members[0].size, tuple(members),
                       tuple((record.path, hashes[record.path]) for record in members))
        for signature, members in grouped.items() if len(members) > 1
    ]
    groups.sort(key=lambda group: path_key(group.files[0].path, root))
    return SimilarResult(pairs, tuple(groups), tuple(errors), decoded, reused)


def render_similar_report(
    console: Console, root: Path, scan: ScanResult, result: SimilarResult, elapsed: float,
) -> None:
    console.print("\n[bold]Similar-name review — no files changed[/bold]")
    console.print("Names suggest candidates only. Removal requires identical decoded audio.")
    memberships = {
        record.path: index for index, group in enumerate(result.groups) for record in group.files
    }
    failed = {error.path for error in result.errors}
    for pair in result.pairs:
        verified = pair.first in memberships and memberships.get(pair.second) == memberships[pair.first]
        label = "VERIFIED AUDIO" if verified else (
            "UNVERIFIED / ERROR" if {pair.first, pair.second} & failed else "DIFFERENT AUDIO"
        )
        console.print(f"{label} · name similarity {pair.similarity:.0%}")
        for path in (pair.first, pair.second):
            console.print(f"  {relative_display(path, root)}", markup=False)
    for error in (*scan.errors, *result.errors):
        console.print(f"Error: {error.path}: {error.detail}", markup=False)
    console.print(
        f"Summary: {len(scan.files)} audio files · {len(result.pairs)} similar-name pairs · "
        f"{len(result.groups)} verified audio groups · {result.decoded_files} decoded · "
        f"{result.reused_files} identical-file results reused · "
        f"{len(scan.errors) + len(result.errors)} errors · {elapsed:.2f}s"
    )


def audio_review(groups: Sequence[DuplicateGroup]) -> tuple[ReviewDecision, ...]:
    """Choose one shallow keeper per verified audio group, with random depth ties."""
    decisions: list[ReviewDecision] = []
    for group in groups:
        if hard_link_sets(group):
            decisions.append(ReviewDecision(group, "skipped"))
            continue
        depth = min(len(record.path.parts) for record in group.files)
        candidates = [record.path for record in group.files if len(record.path.parts) == depth]
        # Prefer a non-numbered name at the winning depth if one is available.
        originals = [path for path in candidates if not re.search(
            r"(?: \[\d+\]| \(\d+\)|-\d+)\s*$", path.stem,
        )]
        candidates = originals or candidates
        keeper = candidates[0] if len(candidates) == 1 else random.choice(candidates)
        decisions.append(ReviewDecision(group, "reviewed", keeper))
    return tuple(decisions)


def recoverable_bytes(group: DuplicateGroup) -> int:
    independent_copies = len({(record.device, record.inode) for record in group.files})
    return max(0, independent_copies - 1) * group.size


def format_bytes(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} B"


def relative_display(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def render_report(
    console: Console,
    root: Path,
    scan: ScanResult,
    search: SearchResult,
    elapsed: float,
) -> None:
    console.print(f"[bold]Exact duplicate audio in[/bold] {root}")
    if not search.groups:
        console.print("[green]No byte-for-byte duplicate audio files found.[/green]")
    for number, group in enumerate(search.groups, start=1):
        linked_paths = {
            record.path
            for linked_set in hard_link_sets(group)
            for record in linked_set
        }
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
        table.add_column("#", justify="right", style="dim")
        table.add_column("Path")
        table.add_column("Storage", style="dim")
        for index, record in enumerate(group.files, start=1):
            storage = "hard-linked" if record.path in linked_paths else "independent"
            table.add_row(str(index), relative_display(record.path, root), storage)
        subtitle = (
            f"{format_bytes(group.size)} each · SHA-256 {group.sha256[:16]}… · "
            f"up to {format_bytes(recoverable_bytes(group))} recoverable"
        )
        console.print(
            Panel(table, title=f"[bold]GROUP {number}[/bold]", subtitle=subtitle)
        )

    errors = (*scan.errors, *search.errors)
    if errors:
        error_table = Table(box=box.SIMPLE, show_header=True, header_style="bold red")
        error_table.add_column("Path")
        error_table.add_column("Error")
        for error in errors:
            try:
                path = relative_display(error.path, root)
            except ValueError:
                path = str(error.path)
            error_table.add_row(path, error.detail)
        console.print(Panel(error_table, title="[bold red]ERRORS[/bold red]"))

    independent_duplicates = sum(
        max(0, len({(item.device, item.inode) for item in group.files}) - 1)
        for group in search.groups
    )
    summary = Table.grid(padding=(0, 1))
    summary.add_column(style="dim")
    summary.add_column(justify="right")
    summary.add_row("Audio files", str(len(scan.files)))
    summary.add_row("Files hashed", str(search.hashed_files))
    summary.add_row("Duplicate groups", str(len(search.groups)))
    summary.add_row("Independent duplicate copies", str(independent_duplicates))
    summary.add_row(
        "Potentially recoverable",
        format_bytes(sum(recoverable_bytes(group) for group in search.groups)),
    )
    summary.add_row("Symlinks skipped", str(len(scan.skipped_symlinks)))
    summary.add_row(
        "Errors",
        Text(str(len(errors)), style="bold red" if errors else "dim"),
    )
    summary.add_row("Elapsed", f"{elapsed:.2f}s")
    console.print(Panel(summary, title="[bold]SUMMARY[/bold]", border_style="bright_blue"))


def render_review_group(
    console: Console, root: Path, group: DuplicateGroup, number: int, total: int
) -> None:
    console.print(
        f"\n[bold cyan]Review group {number}/{total}[/bold cyan] "
        f"({format_bytes(group.size)} each)"
    )
    for index, record in enumerate(group.files, start=1):
        console.print(f"  [bold]{index}[/bold]  {relative_display(record.path, root)}")


def review_groups(
    console: Console,
    root: Path,
    groups: Sequence[DuplicateGroup],
    input_func: Callable[[str], str] = input,
) -> tuple[ReviewDecision, ...]:
    decisions: list[ReviewDecision] = []
    for group_index, group in enumerate(groups):
        render_review_group(console, root, group, group_index + 1, len(groups))
        while True:
            try:
                answer = input_func(
                    "Choose the keeper number, [s]kip this group, or [q]uit: "
                ).strip().casefold()
            except EOFError:
                answer = "q"
            if answer == "s":
                decisions.append(ReviewDecision(group, "skipped"))
                break
            if answer == "q":
                decisions.extend(
                    ReviewDecision(remaining, "unreviewed")
                    for remaining in groups[group_index:]
                )
                return tuple(decisions)
            try:
                keeper_index = int(answer) - 1
            except ValueError:
                keeper_index = -1
            if 0 <= keeper_index < len(group.files):
                decisions.append(
                    ReviewDecision(group, "reviewed", group.files[keeper_index].path)
                )
                break
            console.print("[yellow]Enter a listed number, s, or q.[/yellow]")
    return tuple(decisions)


def original_filename(path: Path) -> str:
    """Strip only terminal collision suffixes emitted by flatten_music."""
    stem = path.stem
    while match := re.search(r" \[([0-9]+)\]$", stem):
        if int(match[1]) < 2:
            break
        stem = stem[:match.start()]
    return unicodedata.normalize("NFC", stem + path.suffix).casefold()


def automatic_review(
    groups: Sequence[DuplicateGroup],
    choose_keeper: Callable[[Sequence[Path]], Path] | None = None,
) -> tuple[ReviewDecision, ...]:
    """Prefer shallow originals, choosing equal-depth keepers once per plan."""
    decisions: list[ReviewDecision] = []
    for group in groups:
        originals = [
            record.path for record in group.files
            if original_filename(record.path)
            == unicodedata.normalize("NFC", record.path.name).casefold()
        ]
        names = {original_filename(record.path) for record in group.files}
        exact_names = {
            unicodedata.normalize("NFC", record.path.name).casefold()
            for record in group.files
        }
        # Identical filenames are safe candidates even if all have a suffix.
        candidates = originals or (
            [record.path for record in group.files] if len(exact_names) == 1 else []
        )
        if candidates and len(names) == 1 and not hard_link_sets(group):
            # All paths share the scan root, so absolute depth has the same order.
            depth = min(len(path.parts) for path in candidates)
            shallowest = sorted(path for path in candidates if len(path.parts) == depth)
            keeper = shallowest[0] if len(shallowest) == 1 else (choose_keeper or random.choice)(shallowest)
            decisions.append(ReviewDecision(group, "reviewed", keeper))
        else:
            decisions.append(ReviewDecision(group, "skipped"))
    return tuple(decisions)


def render_removal_plan(
    console: Console, root: Path, decisions: Sequence[ReviewDecision],
) -> int:
    count = 0
    console.print("\n[bold]Automatic removal plan[/bold]")
    for decision in decisions:
        if decision.status != "reviewed":
            console.print("SKIP group: ambiguous filenames or hard-linked paths.")
            for record in decision.group.files:
                console.print(f"  {relative_display(record.path, root)}", markup=False)
            continue
        for record in decision.group.files:
            keep = record.path == decision.keeper
            count += not keep
            console.print(
                f"{'KEEP  ' if keep else 'REMOVE'} {relative_display(record.path, root)}",
                markup=False,
            )
    skipped = sum(item.status != "reviewed" for item in decisions)
    console.print(f"Planned: {count} files to remove · {skipped} groups skipped")
    return count


def removal_supported() -> bool:
    return (
        hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
        and os.unlink in os.supports_dir_fd and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
    )


@contextmanager
def open_parent(root: Path, path: Path) -> Iterator[int]:
    """Pin a parent directory, refusing symlinks in every path component."""
    relative = path.relative_to(root)
    if ".." in relative.parts:
        raise ValueError("removal path escapes the scan root")
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


def check_removal_record(record: FileRecord, parent: int) -> None:
    details = os.stat(record.path.name, dir_fd=parent, follow_symlinks=False)
    if not same_identity(record, details):
        raise FileChangedError("file changed since scanning; run a fresh preview")
    locked_flags = sum(
        getattr(stat, name, 0)
        for name in ("UF_IMMUTABLE", "SF_IMMUTABLE", "UF_APPEND", "SF_APPEND")
    )
    if any(
        getattr(item, "st_flags", 0) & locked_flags
        for item in (details, os.fstat(parent))
    ):
        raise OSError("file or parent directory is locked or immutable")
    if details.st_nlink != 1:
        raise OSError("file has hard links; automatic removal is disabled for this group")


def execute_removal(
    root: Path, decisions: Sequence[ReviewDecision],
    progress: Callable[[str, Path], None] | None = None,
) -> RemovalResult:
    """Validate the entire plan, then unlink duplicates while retaining keepers."""
    if not removal_supported():
        return RemovalResult((), (ScanError(root, "safe removal requires POSIX dir_fd support"),))
    selected = tuple(item for item in decisions if item.status == "reviewed")
    seen: set[Path] = set()
    # Validate plan structure before touching files, even for programmatic callers.
    for decision in selected:
        paths = [record.path for record in decision.group.files]
        if decision.keeper not in paths or len(paths) < 2:
            raise ValueError("invalid keeper or incomplete duplicate group")
        hashes = dict(decision.group.file_hashes)
        if decision.group.file_hashes and (
            len(hashes) != len(paths) or len(decision.group.file_hashes) != len(paths)
            or set(hashes) != set(paths)
        ):
            raise ValueError("incomplete whole-file hashes in audio removal plan")
        for path in paths:
            if path in seen or ".." in path.relative_to(root).parts:
                raise ValueError("duplicate or unsafe path in removal plan")
            seen.add(path)
    errors: list[ScanError] = []
    for decision in selected:
        for record in decision.group.files:
            try:
                if progress:
                    progress("Verifying", record.path)
                with open_parent(root, record.path) as parent:
                    check_removal_record(record, parent)
                    expected = dict(decision.group.file_hashes).get(record.path, decision.group.sha256)
                    if hash_file(record, directory_fd=parent) != expected:
                        raise FileChangedError("content no longer matches the duplicate group")
            except OSError as error:
                errors.append(ScanError(record.path, str(error)))
            except KeyboardInterrupt:
                return RemovalResult((), (ScanError(record.path, "verification interrupted"),))
    if errors:
        return RemovalResult((), tuple(errors))

    removed: list[Path] = []
    for decision in selected:
        keeper = next(item for item in decision.group.files if item.path == decision.keeper)
        for record in decision.group.files:
            if record.path == keeper.path:
                continue
            try:
                if progress:
                    progress("Removing", record.path)
                with open_parent(root, keeper.path) as keeper_parent:
                    with open_parent(root, record.path) as parent:
                        check_removal_record(keeper, keeper_parent)
                        check_removal_record(record, parent)
                        os.unlink(record.path.name, dir_fd=parent)
                removed.append(record.path)
            except OSError as error:
                # Stop on the first execution failure; report any completed removals.
                return RemovalResult(tuple(removed), (ScanError(record.path, str(error)),))
            except KeyboardInterrupt:
                return RemovalResult(
                    tuple(removed),
                    (ScanError(record.path, "removal interrupted; rescan before retrying"),),
                )
    return RemovalResult(tuple(removed), ())


def run_automatic_removal(
    console: Console, error_console: Console, root: Path,
    decisions: Sequence[ReviewDecision], dry_run: bool,
    input_func: Callable[[str], str] | None = None,
    confirm_func: Callable[[int], bool] | None = None,
) -> int:
    count = render_removal_plan(console, root, decisions)
    if dry_run or not count:
        console.print(
            "Removal summary: 0 removed · preview only"
            if dry_run else "Removal summary: 0 removed"
        )
        return 0
    console.print(
        "[bold red]Removal is permanent (not Trash). "
        "Keepers and skipped groups remain.[/bold red]"
    )
    if any(decision.group.file_hashes for decision in decisions):
        console.print(
            "[bold yellow]Audio matches, but tags, artwork, cue points, and encoding may differ. "
            "Only the keeper's metadata survives.[/bold yellow]"
        )
    try:
        if confirm_func is not None:
            confirmed = confirm_func(count)
        else:
            answer = (input_func or input)(
                f"Type DELETE to permanently remove {count} duplicate files: "
            )
            confirmed = answer == "DELETE"
    except (EOFError, KeyboardInterrupt):
        confirmed = False
    except OSError as error:
        error_console.print(f"Error: {error}", markup=False)
        console.print("Removal summary: 0 removed · confirmation failed")
        return 1
    if not confirmed:
        console.print("Removal summary: 0 removed · cancelled")
        return 0
    with console.status("Verifying removal plan…") as status:
        result = execute_removal(
            root, decisions,
            progress=lambda stage, path: status.update(
                Text(f"{stage}: {relative_display(path, root)}")
            ),
        )
    for path in result.removed:
        console.print(f"REMOVED {relative_display(path, root)}", markup=False)
    for error in result.errors:
        error_console.print(f"Error: {error.path}: {error.detail}", markup=False)
    console.print(
        f"Removal summary: {len(result.removed)} removed · "
        f"{count - len(result.removed)} not removed · {len(result.errors)} errors"
    )
    return 1 if result.errors else 0


def choose_mode(
    console: Console, root: Path,
    input_func: Callable[[str], str] | None = None,
) -> str | None:
    console.print("\n[bold cyan]Music Duplicates[/bold cyan]")
    console.print(str(root), markup=False)
    console.print("1  Report exact duplicates (read-only)")
    console.print("2  Review keepers one group at a time (read-only)")
    console.print("3  Preview automatic removal (read-only)")
    console.print("4  Automatically remove numbered and same-name copies (confirmation required)")
    console.print("5  Review similar names and verify audio (read-only)")
    console.print("6  Remove verified audio copies with similar names (confirmation required)")
    console.print("q  Quit")
    modes = {"1": "report", "2": "review", "3": "preview", "4": "remove",
             "5": "similar", "6": "similar-remove"}
    while True:
        try:
            answer = (input_func or input)("Choose an option [1-6, q] (default: 1): ").strip().casefold()
        except (EOFError, KeyboardInterrupt):
            return None
        if answer == "q":
            return None
        if answer in modes or not answer:
            return modes.get(answer, "report")
        console.print("[yellow]Enter 1, 2, 3, 4, 5, 6, or q.[/yellow]")


REMOVAL_DIALOG_SCRIPT = '''on run argv
    set folderPath to item 1 of argv
    set fileCount to item 2 of argv
    set waitSeconds to (item 3 of argv) as integer
    try
        set resultDialog to display dialog "Permanently remove " & fileCount & " duplicate files from:" & return & return & folderPath & return & return & "This does not use Trash. Original keepers and skipped groups will remain unchanged." buttons {"Cancel", "Remove Duplicates"} default button "Cancel" cancel button "Cancel" with title "Music Duplicates" with icon caution giving up after waitSeconds
        if gave up of resultDialog then return "cancel"
        return button returned of resultDialog
    on error number -128
        return "cancel"
    end try
end run'''


def confirm_macos_removal(root: Path, count: int, timeout: int = 120) -> bool:
    """Confirm only the displayed plan; pass paths as data, never script text."""
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", REMOVAL_DIALOG_SCRIPT,
             str(root), str(count), str(timeout)],
            capture_output=True, text=True, check=True, timeout=timeout + 5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise OSError("native removal confirmation failed or timed out; nothing removed") from error
    return result.stdout.strip() == "Remove Duplicates"


def manifest_data(
    root: Path,
    decisions: Sequence[ReviewDecision],
    errors: Sequence[ScanError],
) -> dict[str, object]:
    groups: list[dict[str, object]] = []
    for decision in decisions:
        group = decision.group
        paths = [relative_display(record.path, root) for record in group.files]
        review: dict[str, object] = {"status": decision.status}
        if decision.status == "reviewed":
            assert decision.keeper is not None
            keeper = relative_display(decision.keeper, root)
            review["keeper"] = keeper
            review["duplicates"] = [path for path in paths if path != keeper]
        linked = [
            [relative_display(record.path, root) for record in records]
            for records in hard_link_sets(group)
        ]
        groups.append(
            {
                "decision": review,
                "hard_link_sets": linked,
                "paths": paths,
                "sha256": group.sha256,
                "size_bytes": group.size,
            }
        )
    manifest_errors: list[dict[str, str]] = []
    for error in errors:
        try:
            path = relative_display(error.path, root)
        except ValueError:
            path = str(error.path)
        manifest_errors.append({"detail": error.detail, "path": path})
    return {
        "groups": groups,
        "hash_algorithm": "sha256",
        "root": str(root),
        "scan_complete": not errors,
        "scan_errors": manifest_errors,
        "schema_version": MANIFEST_SCHEMA_VERSION,
    }


def write_manifest_exclusive(path: Path, data: dict[str, object]) -> None:
    """Atomically publish a new manifest without replacing any destination."""
    requested = path.expanduser()
    if requested.exists() or requested.is_symlink():
        raise FileExistsError(f"output already exists: {requested}")
    destination = requested.resolve(strict=False)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"output already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"output directory does not exist: {destination.parent}")

    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as target:
            json.dump(data, target, ensure_ascii=False, indent=2, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.link(temporary, destination, follow_symlinks=False)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find byte-for-byte identical audio files recursively. The default "
            "is a read-only report."
        )
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="music directory to scan recursively (default: current directory)",
    )
    parser.add_argument(
        "--menu", action="store_true",
        help="show a Terminal option selector (used by the Finder Quick Action)",
    )
    parser.add_argument(
        "--dialog-timeout", type=int, default=120,
        help="macOS menu removal confirmation timeout in seconds (1-3600; default: 120)",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="choose a keeper for each group (interactive unless --auto-remove)",
    )
    parser.add_argument(
        "--auto-remove", action="store_true",
        help="with --review, remove numbered and same-name copies; prefer shallow originals, "
             "choose depth ties randomly, and confirm permanent removal",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="with --review --auto-remove, preview choices without writing or removing files",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="with --review, write choices to a new JSON manifest",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    console = Console(highlight=False)
    error_console = Console(stderr=True, highlight=False)

    if not 1 <= args.dialog_timeout <= 3600:
        error_console.print("Error: --dialog-timeout must be between 1 and 3600 seconds")
        return 2
    if args.menu and (args.review or args.auto_remove or args.dry_run or args.output is not None):
        error_console.print("Error: --menu cannot be combined with --review, --auto-remove, --dry-run, or --output")
        return 2
    if args.menu and not sys.stdin.isatty():
        error_console.print("Error: --menu requires an interactive terminal")
        return 2
    if args.auto_remove and not args.review:
        error_console.print("Error: --auto-remove requires --review")
        return 2
    if args.dry_run and not args.auto_remove:
        error_console.print("Error: --dry-run requires --review --auto-remove")
        return 2
    if args.auto_remove and args.output is not None:
        error_console.print("Error: --output cannot be combined with --auto-remove")
        return 2
    if args.auto_remove and not args.dry_run and not removal_supported():
        error_console.print("Error: safe removal requires POSIX dir_fd support (macOS or Linux)")
        return 2
    if args.output is not None and not args.review:
        error_console.print(
            "[bold red]Error:[/bold red] --output is valid only with --review"
        )
        return 2
    if args.review and not args.dry_run and not sys.stdin.isatty():
        error_console.print(
            "[bold red]Error:[/bold red] --review requires an interactive terminal"
        )
        return 2

    requested_root = Path(args.directory).expanduser()
    if requested_root.is_symlink():
        error_console.print(
            f"[bold red]Error:[/bold red] directory symlinks are not followed: {requested_root}"
        )
        return 2
    root = requested_root.resolve()
    if not root.is_dir():
        error_console.print(f"[bold red]Error:[/bold red] not a directory: {root}")
        return 2
    if args.menu:
        mode = choose_mode(console, root)
        if mode is None:
            console.print("Menu closed · no files changed")
            return 0
        args.review = mode in {"review", "preview", "remove"}
        args.auto_remove = mode in {"preview", "remove"}
        args.dry_run = mode == "preview"
        if mode == "remove" and not removal_supported():
            error_console.print("Error: safe removal requires POSIX dir_fd support (macOS or Linux)")
            return 2
        if mode == "remove" and sys.platform == "darwin" and not Path("/usr/bin/osascript").is_file():
            error_console.print("Error: macOS menu removal requires /usr/bin/osascript")
            return 2
    if args.output is not None:
        requested_output = args.output.expanduser()
        if requested_output.exists() or requested_output.is_symlink():
            error_console.print(
                f"[bold red]Error:[/bold red] output already exists: {requested_output}"
            )
            return 2
        output = requested_output.resolve(strict=False)
        if output.exists() or output.is_symlink():
            error_console.print(
                f"[bold red]Error:[/bold red] output already exists: {output}"
            )
            return 2
        if not output.parent.is_dir():
            error_console.print(
                "[bold red]Error:[/bold red] output directory does not exist: "
                f"{output.parent}"
            )
            return 2

    started = time.monotonic()
    with console.status("[bold cyan]Scanning audio files…[/bold cyan]"):
        scan = scan_audio_files(root)

    status = console.status("[bold cyan]Preparing hashes…[/bold cyan]")
    with status:
        search = find_duplicate_groups(
            root,
            scan.files,
            progress=lambda path, index, total: status.update(
                f"[bold cyan]Hashing {index}/{total}:[/bold cyan] "
                f"{relative_display(path, root)}"
            ),
        )
    elapsed = time.monotonic() - started
    render_report(console, root, scan, search, elapsed)

    all_errors = (*scan.errors, *search.errors)
    if args.auto_remove:
        decisions = automatic_review(search.groups)
        if all_errors:
            render_removal_plan(console, root, decisions)
            error_console.print("Removal summary: 0 removed · scan errors must be resolved first")
            return 1
        confirm_func = None
        if args.menu and sys.platform == "darwin":
            confirm_func = lambda count: confirm_macos_removal(root, count, args.dialog_timeout)
        return run_automatic_removal(
            console, error_console, root, decisions, args.dry_run,
            confirm_func=confirm_func,
        )
    if args.review:
        try:
            decisions = review_groups(console, root, search.groups)
        except KeyboardInterrupt:
            error_console.print("\n[bold red]Error:[/bold red] review interrupted")
            return 1
        reviewed = sum(decision.status == "reviewed" for decision in decisions)
        skipped = sum(decision.status == "skipped" for decision in decisions)
        unreviewed = sum(decision.status == "unreviewed" for decision in decisions)
        console.print(
            f"\n[bold]Review:[/bold] {reviewed} reviewed · {skipped} skipped · "
            f"{unreviewed} unreviewed"
        )
        if args.output is not None:
            try:
                write_manifest_exclusive(
                    args.output, manifest_data(root, decisions, all_errors)
                )
            except OSError as error:
                error_console.print(
                    f"[bold red]Error:[/bold red] cannot write review manifest: {error}"
                )
                return 1
            console.print(f"Review manifest written to [bold]{args.output.resolve()}[/bold]")

    return 1 if all_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
