#!/usr/bin/env python3
"""Flatten a directory tree and rename audio files from their metadata."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


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

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TRAILING_VERSION = re.compile(r"^(?P<title>.+?)\s*\((?P<version>[^()]*)\)\s*$")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True)
class Metadata:
    artist: str
    title: str
    version: str | None = None


@dataclass(frozen=True)
class FilePlan:
    source: Path
    flattened: Path
    final: Path
    metadata: Metadata | None


@dataclass(frozen=True)
class ExecutionResult:
    moved: int
    renamed: int
    removed_directories: tuple[Path, ...]
    kept_directories: tuple[tuple[Path, str], ...]


def clean_text(value: Any) -> str | None:
    """Return the first useful tag value as normalized text."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    if hasattr(value, "text"):
        value = value.text
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def first_tag(tags: Mapping[str, Any] | None, keys: Iterable[str]) -> str | None:
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


def read_metadata(path: Path, mutagen_file: Callable[..., Any]) -> Metadata | None:
    """Read common artist/title/version fields through Mutagen."""
    try:
        audio = mutagen_file(path, easy=True)
    except Exception as error:  # Mutagen raises format-specific exceptions.
        print(f"warning: cannot read metadata from {path}: {error}", file=sys.stderr)
        return None

    tags = getattr(audio, "tags", None) if audio is not None else None
    # Some containers, including WAV and AIFF, expose native ID3 frame names
    # even when Mutagen is opened with easy=True.
    artist = first_tag(tags, ("artist", "albumartist", "author", "tpe1", "tpe2"))
    title = first_tag(tags, ("title", "tit2"))
    version = first_tag(
        tags, ("version", "mix", "remix", "subtitle", "tit3")
    )
    if not artist or not title:
        return None

    # Electronic-music files commonly store the mix/version at the end of TITLE.
    title_match = TRAILING_VERSION.match(title)
    if title_match:
        title = title_match.group("title").strip()
        title_version = title_match.group("version").strip()
        version = version or title_version or None

    return Metadata(artist=artist, title=title, version=version)


def sanitize_component(text: str) -> str:
    """Make metadata safe as one cross-platform filename component."""
    text = INVALID_FILENAME_CHARS.sub("-", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        return "Unknown"
    if text.upper() in WINDOWS_RESERVED_NAMES:
        text = f"_{text}"
    return text


def truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")


def music_filename(metadata: Metadata, suffix: str) -> str:
    artist = sanitize_component(metadata.artist)
    title = sanitize_component(metadata.title)
    stem = f"{artist} - {title}"
    if metadata.version:
        stem += f" ({sanitize_component(metadata.version)})"

    # Most filesystems cap a filename at 255 bytes. Leave room for the extension.
    suffix = suffix.lower()
    stem = truncate_utf8(stem, max(1, 240 - len(suffix.encode("utf-8"))))
    return f"{stem}{suffix}"


def filename_key(name: str) -> str:
    """Compare names like a typical case- and Unicode-insensitive filesystem."""
    return unicodedata.normalize("NFD", name).casefold()


def collision_safe_name(name: str, occupied: set[str]) -> str:
    """Return a case-insensitively unique name, without overwriting anything."""
    if filename_key(name) not in occupied:
        occupied.add(filename_key(name))
        return name

    path = Path(name)
    counter = 2
    while True:
        candidate = f"{path.stem} [{counter}]{path.suffix}"
        if filename_key(candidate) not in occupied:
            occupied.add(filename_key(candidate))
            return candidate
        counter += 1


def scan_tree(root: Path) -> tuple[list[Path], list[Path]]:
    """Return all files and real subdirectories, in deterministic order."""
    files: list[Path] = []
    directories: list[Path] = []

    def walk_error(error: OSError) -> None:
        raise error

    for current, dirnames, filenames in os.walk(
        root, topdown=True, followlinks=False, onerror=walk_error
    ):
        current_path = Path(current)
        # Do not later try to rmdir directory symlinks; they are intentionally kept.
        real_dirnames: list[str] = []
        for dirname in sorted(dirnames, key=str.casefold):
            directory = current_path / dirname
            if not directory.is_symlink():
                directories.append(directory)
                real_dirnames.append(dirname)
        dirnames[:] = real_dirnames
        for filename in sorted(filenames, key=str.casefold):
            files.append(current_path / filename)

    files.sort(key=lambda path: str(path.relative_to(root)).casefold())
    directories.sort(key=lambda path: (len(path.parts), str(path).casefold()), reverse=True)
    return files, directories


def build_plan(
    root: Path,
    files: Sequence[Path],
    mutagen_file: Callable[..., Any] | None,
    skip_rename: bool,
) -> list[FilePlan]:
    """Plan every destination before changing the filesystem."""
    root_files = [path for path in files if path.parent == root]
    nested_files = [path for path in files if path.parent != root]

    # Reserve every current root entry, including symlinked directories.
    occupied_flat = {filename_key(entry.name) for entry in root.iterdir()}
    flattened_by_source: dict[Path, Path] = {path: path for path in root_files}
    for source in nested_files:
        flattened_name = collision_safe_name(source.name, occupied_flat)
        flattened_by_source[source] = root / flattened_name

    metadata_by_source: dict[Path, Metadata | None] = {}
    renameable: set[Path] = set()
    for source in files:
        flattened = flattened_by_source[source]
        metadata = None
        if not skip_rename and source.suffix.lower() in AUDIO_EXTENSIONS:
            assert mutagen_file is not None
            metadata = read_metadata(source, mutagen_file)
            if metadata is not None:
                renameable.add(source)
        metadata_by_source[source] = metadata

    # Files without usable metadata keep their flattened names. Renamed audio files
    # are assigned together so swaps (A -> B and B -> A) are safe and deterministic.
    occupied_final = {
        filename_key(flattened_by_source[source].name)
        for source in files
        if source not in renameable
    }
    occupied_final.update(
        filename_key(entry.name)
        for entry in root.iterdir()
        if entry.is_dir() or (entry.is_symlink() and not entry.is_file())
    )

    plans: list[FilePlan] = []
    for source in sorted(files, key=lambda path: flattened_by_source[path].name.casefold()):
        flattened = flattened_by_source[source]
        metadata = metadata_by_source[source]
        if metadata is None:
            final = flattened
        else:
            desired = music_filename(metadata, source.suffix)
            if filename_key(desired) == filename_key(flattened.name):
                occupied_final.add(filename_key(flattened.name))
                final = flattened
            else:
                final = root / collision_safe_name(desired, occupied_final)
        plans.append(FilePlan(source, flattened, final, metadata))
    return plans


def action_for(plan: FilePlan) -> tuple[str, str, str] | None:
    moved = plan.source != plan.flattened
    renamed = plan.flattened != plan.final
    if moved and renamed:
        return "MOVE + RENAME", str(plan.source), plan.final.name
    if moved:
        return "MOVE", str(plan.source), plan.flattened.name
    if renamed:
        return "RENAME", plan.flattened.name, plan.final.name
    return None


def summary_table(
    files: int,
    moves: int,
    renames: int,
    skipped: int,
    directories: int,
) -> Table:
    table = Table.grid(expand=True, padding=(0, 1))
    for _ in range(5):
        table.add_column(justify="center", ratio=1)
    table.add_row(
        Text("FILES", style="dim"),
        Text("MOVES", style="dim"),
        Text("RENAMES", style="dim"),
        Text("SKIPPED", style="dim"),
        Text("FOLDERS", style="dim"),
    )
    table.add_row(
        Text(str(files), style="bold white"),
        Text(str(moves), style="bold cyan"),
        Text(str(renames), style="bold magenta"),
        Text(str(skipped), style="bold yellow" if skipped else "dim"),
        Text(str(directories), style="bold blue"),
    )
    return table


def render_report(
    console: Console,
    root: Path,
    plans: Sequence[FilePlan],
    directories: Sequence[Path],
    *,
    dry_run: bool,
    rename_enabled: bool,
    result: ExecutionResult | None = None,
) -> None:
    moves = result.moved if result else sum(p.source != p.flattened for p in plans)
    renames = result.renamed if result else sum(p.flattened != p.final for p in plans)
    skipped_plans = [
        plan
        for plan in plans
        if rename_enabled
        and plan.source.suffix.lower() in AUDIO_EXTENSIONS
        and plan.metadata is None
    ]
    folder_count = len(result.removed_directories) if result else len(directories)
    status = "DRY RUN — NOTHING CHANGED" if dry_run else "COMPLETE"
    status_style = "bold yellow" if dry_run else "bold green"

    console.print()
    console.rule(Text("♫  FLATTEN MUSIC", style="bold bright_cyan"))
    heading = Text()
    heading.append(status, style=status_style)
    heading.append("\n")
    heading.append(str(root), style="white")
    console.print(Panel(heading, border_style="yellow" if dry_run else "green"))
    changes = Table(
        title="PLANNED CHANGES" if dry_run else "COMPLETED CHANGES",
        box=box.ROUNDED,
        header_style="bold white",
        border_style="bright_black",
        show_lines=True,
        expand=True,
    )
    changes.add_column("ACTION", no_wrap=True, width=15)
    changes.add_column("BEFORE", ratio=1, overflow="fold")
    changes.add_column("AFTER", ratio=1, overflow="fold")

    action_styles = {
        "MOVE": "bold cyan",
        "RENAME": "bold magenta",
        "MOVE + RENAME": "bold bright_blue",
        "REMOVE FOLDER": "bold blue",
        "REMOVED FOLDER": "bold blue",
        "KEPT FOLDER": "bold yellow",
    }
    change_count = 0
    for plan in plans:
        action = action_for(plan)
        if action is None:
            continue
        label, before, after = action
        before_path = Path(before)
        if before_path.is_absolute():
            before = str(before_path.relative_to(root))
        changes.add_row(
            Text(label, style=action_styles[label]),
            Text(before),
            Text(after, style="green" if not dry_run else "white"),
        )
        change_count += 1

    kept_by_path = dict(result.kept_directories) if result else {}
    for directory in directories:
        relative = str(directory.relative_to(root))
        if directory in kept_by_path:
            changes.add_row(
                Text("KEPT FOLDER", style=action_styles["KEPT FOLDER"]),
                Text(relative),
                Text(kept_by_path[directory], style="yellow"),
            )
        else:
            folder_action = "REMOVE FOLDER" if dry_run else "REMOVED FOLDER"
            changes.add_row(
                Text(folder_action, style=action_styles[folder_action]),
                Text(relative),
                Text("—", style="dim"),
            )
        change_count += 1

    if change_count:
        console.print(changes)
    else:
        console.print(
            Panel(
                "This folder is already flat and its tagged audio is consistently named.",
                title="[bold green]NO CHANGES NEEDED[/bold green]",
                border_style="green",
            )
        )

    if skipped_plans:
        skipped = Table(
            title="UNCHANGED AUDIO",
            box=box.SIMPLE,
            header_style="bold yellow",
            expand=True,
        )
        skipped.add_column("FILE", ratio=1, overflow="fold")
        skipped.add_column("REASON", ratio=1)
        for plan in skipped_plans:
            skipped.add_row(
                Text(plan.flattened.name),
                Text("Artist or title metadata is missing", style="yellow"),
            )
        console.print(skipped)

    if dry_run and change_count:
        console.print(
            Panel(
                "Review the before/after names above. Run without [bold]--dry-run[/bold] "
                "to apply them.",
                border_style="yellow",
            )
        )
    elif not dry_run:
        console.print("[bold green]✓ Music folder organized successfully.[/bold green]\n")

    # Keep the overview last so it remains visible after long reports finish.
    console.print(
        Panel(
            summary_table(len(plans), moves, renames, len(skipped_plans), folder_count),
            title="[bold]SUMMARY[/bold]",
            border_style="bright_blue",
        )
    )


def execute_plan(
    root: Path, plans: Sequence[FilePlan], directories: Sequence[Path]
) -> ExecutionResult:
    moved = 0
    for plan in plans:
        if plan.source != plan.flattened:
            shutil.move(str(plan.source), str(plan.flattened))
            moved += 1

    # Stage all renames first, preventing overwrite and rename-cycle problems.
    staged: list[tuple[Path, Path]] = []
    for plan in plans:
        if plan.flattened == plan.final:
            continue
        temporary = root / f".flatten-music-{uuid.uuid4().hex}.tmp"
        plan.flattened.rename(temporary)
        staged.append((temporary, plan.final))

    renamed = 0
    for temporary, final in staged:
        temporary.rename(final)
        renamed += 1

    removed_directories: list[Path] = []
    kept_directories: list[tuple[Path, str]] = []
    for directory in directories:
        try:
            directory.rmdir()
            removed_directories.append(directory)
        except OSError as error:
            kept_directories.append((directory, str(error)))

    return ExecutionResult(
        moved=moved,
        renamed=renamed,
        removed_directories=tuple(removed_directories),
        kept_directories=tuple(kept_directories),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Move every file from nested directories into one root directory, "
            "remove empty directories, and rename audio as "
            "'Artist - Title (Version)'."
        )
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="directory to process (default: current directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the complete plan without changing anything",
    )
    parser.add_argument(
        "--skip-rename",
        action="store_true",
        help="flatten files but do not read metadata or rename audio",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    console = Console(highlight=False)
    error_console = Console(stderr=True, highlight=False)
    root = Path(args.directory).expanduser().resolve()
    if not root.is_dir():
        error_console.print(f"[bold red]Error:[/bold red] not a directory: {root}")
        return 2

    try:
        with console.status("[bold cyan]Scanning files and reading metadata…[/bold cyan]"):
            files, directories = scan_tree(root)
    except OSError as error:
        error_console.print(f"[bold red]Error:[/bold red] cannot scan {root}: {error}")
        return 1

    mutagen_file: Callable[..., Any] | None = None
    if not args.skip_rename and any(
        path.suffix.lower() in AUDIO_EXTENSIONS for path in files
    ):
        try:
            from mutagen import File as mutagen_file
        except ImportError:
            requirements = Path(__file__).with_name("requirements.txt")
            error_console.print(
                "[bold red]Error:[/bold red] audio files were found, but Mutagen "
                "is not installed.\nInstall it with: "
                f"[bold]python3 -m pip install -r {requirements}[/bold]\n"
                "Or flatten only with: [bold]--skip-rename[/bold]"
            )
            return 2

    with console.status("[bold cyan]Building the organization plan…[/bold cyan]"):
        plans = build_plan(root, files, mutagen_file, args.skip_rename)

    if args.dry_run:
        render_report(
            console,
            root,
            plans,
            directories,
            dry_run=True,
            rename_enabled=not args.skip_rename,
        )
        return 0

    try:
        with console.status("[bold cyan]Organizing the music folder…[/bold cyan]"):
            result = execute_plan(root, plans, directories)
    except OSError as error:
        error_console.print(f"[bold red]Error:[/bold red] operation stopped: {error}")
        return 1

    render_report(
        console,
        root,
        plans,
        directories,
        dry_run=False,
        rename_enabled=not args.skip_rename,
        result=result,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
