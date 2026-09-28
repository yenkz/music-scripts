# Music Scripts

This repository contains independent utilities for managing digital music files.
Each utility lives in its own subproject directory with its documentation,
dependencies, and tests.

## Projects

- [`flatten_music/`](flatten_music/) — flatten a directory tree and rename audio
  files from their embedded metadata.
- [`create_metadata/`](create_metadata/) — identify untagged audio with
  filename parsing, Discogs, AcoustID, and Shazam, then create missing artist
  and title tags.
- [`find_duplicates/`](find_duplicates/) — find byte-for-byte identical audio,
  review which path to keep, save decisions as JSON, or explicitly remove
  numbered and same-name copies while preferring originals closest to the root.
- [`check_bitrate/`](check_bitrate/) — report MP3 bitrates and optionally confirm
  permanent deletion of files below 320 kbps (or a custom cutoff).

See each subproject's README for setup and usage instructions.

## macOS installation

The Finder workflows call small `fm`, `cm`, `music-dupes`, and `music-bitrate` launchers in
`~/.local/bin`. Run the complete setup from the repository root:

```bash
brew install chromaprint ffmpeg
python3 -m venv .venv
.venv/bin/python -m pip install -r flatten_music/requirements.txt
.venv/bin/python -m pip install -r create_metadata/requirements.txt
.venv/bin/python -m pip install -r find_duplicates/requirements.txt
.venv/bin/python -m pip install -r check_bitrate/requirements.txt

mkdir -p "$HOME/.local/bin"
repo_path="$PWD"
printf '#!/bin/zsh\nexec %q %q "$@"\n' \
  "$repo_path/.venv/bin/python" \
  "$repo_path/flatten_music/flatten_music.py" \
  > "$HOME/.local/bin/fm"
printf '#!/bin/zsh\nexec %q %q "$@"\n' \
  "$repo_path/.venv/bin/python" \
  "$repo_path/create_metadata/create_metadata.py" \
  > "$HOME/.local/bin/cm"
printf '#!/bin/zsh\nexec %q %q "$@"\n' \
  "$repo_path/.venv/bin/python" \
  "$repo_path/find_duplicates/find_duplicates.py" \
  > "$HOME/.local/bin/music-dupes"
printf '#!/bin/zsh\nexec %q %q "$@"\n' \
  "$repo_path/.venv/bin/python" \
  "$repo_path/check_bitrate/check_bitrate.py" \
  > "$HOME/.local/bin/music-bitrate"
chmod +x "$HOME/.local/bin/fm" "$HOME/.local/bin/cm" \
  "$HOME/.local/bin/music-dupes" "$HOME/.local/bin/music-bitrate"
```

Add `~/.local/bin` to Terminal's command path by placing this line in
`~/.zshrc`, then open a new Terminal window:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Configure the repository-root `.env` as described in the
[`create_metadata` requirements](create_metadata/README.md#requirements), then
verify all four launchers:

```bash
fm --help
cm --help
music-dupes --help
music-bitrate --help
```

The launchers contain the repository's absolute path. Recreate them if the
repository or virtual environment is moved.

## Install the Finder Quick Actions

The ready-to-install Automator workflow bundles are stored with their utilities.
Install or update all of them from the repository root:

```bash
./flatten_music/finder_workflows/install.sh
./create_metadata/finder_workflows/install.sh
./find_duplicates/finder_workflows/install.sh
./check_bitrate/finder_workflows/install.sh
```

The installers copy the bundles into `~/Library/Services`. If they do not
appear, enable them under **System Settings → Privacy & Security → Extensions →
Finder**. On first use, allow the workflow to control Terminal.

In Finder, Control-click a music folder and open **Quick Actions**:

| Quick Action | Behavior |
| --- | --- |
| **Flatten Music** | Shows an `fm --dry-run` plan, then asks in Terminal before applying it. |
| **Music Metadata — 1 Analyze** | Runs the normal read-only `cm` analysis. |
| **Music Metadata — 2 Write Accepted** | Confirms, then writes accepted matches only into missing fields. |
| **Music Metadata — Refresh Analysis** | Refreshes cached Discogs, AcoustID, and Shazam responses. |
| **Music Metadata — Force Analyze** | Read-only analysis of every audio file, including fully tagged files. |
| **Music Duplicates — Review** | Opens a Terminal menu: report, manual review, automatic preview, or confirmed removal of numbered and same-name copies. |
| **Music Bitrate — 1 Report** | Read-only MP3 bitrate report with below-320 candidates. |
| **Music Bitrate — 2 Delete Below 320** | Native confirmation, then the exact deletion plan and a typed `DELETE` confirmation in Terminal. |

The workflows open Terminal so progress, results, and errors remain visible.
They resolve the launchers through the current user's home directory and do not
contain a contributor-specific repository path. Rerun the installers after
pulling workflow updates.

For safety, no Finder action runs `cm --force --write`. Replacing existing
artist and title metadata remains an explicit Terminal-only operation.

## Terminal usage

```bash
fm --dry-run "/path/to/music-folder"  # preview flattening
fm "/path/to/music-folder"            # apply flattening
cm "/path/to/music-folder"            # preview metadata
cm --write "/path/to/music-folder"    # write accepted missing metadata
cm --force "/path/to/music-folder"    # preview every audio file
music-dupes "/path/to/music-folder"   # report byte-for-byte duplicates
music-dupes --menu "/path/to/music-folder"  # same option selector as the Finder action
music-dupes --review "/path/to/music-folder"  # choose keepers without changing files
music-dupes --review --auto-remove --dry-run "/path/to/music-folder"  # preview automatic choices
music-dupes --review --auto-remove "/path/to/music-folder"  # confirm permanent removal once
music-bitrate "/path/to/music-folder"  # report MP3 bitrates; read-only
music-bitrate --delete --dry-run "/path/to/music-folder"  # preview below-320 deletion
music-bitrate --delete "/path/to/music-folder"  # preview, then type DELETE to remove
```

Automatic duplicate removal keeps `Track.mp3` over byte-identical copies such as
`Track [2].mp3`. For matching originals in different folders, it keeps the copy
closest to the selected root, choosing randomly among equal-depth originals
once per plan. Identical filenames across folders follow the same depth rule
even if numbered. It skips ambiguous names and hard-linked groups, displays the
plan, and requires typing `DELETE`. The Finder menu uses a native macOS
confirmation instead. Removal is permanent, not Trash; the menu's report,
manual review, and preview options are read-only. See the [duplicate utility README](find_duplicates/README.md)
for matching rules and safety checks.

Bitrate deletion selects MP3 files strictly below 320 kbps by default; use
`--below 256` for a different cutoff. VBR/ABR use reported average bitrate.
Unknown bitrate modes and hard-linked files are protected. Removal is permanent,
not Trash. See the [bitrate utility README](check_bitrate/README.md) for details.

## Important: Rekordbox and Traktor libraries

Flattening and renaming files changes their filesystem paths. If those tracks
have already been used in Rekordbox or Traktor playlists, the applications may
report them as missing and the playlists may appear broken. Back up the DJ
library before applying changes. Afterwards, relocate or re-scan the moved
music and refresh or rebuild the affected playlists. Previewing with
`--dry-run` does not change paths and will not affect a DJ library.
Removing duplicates can also break playlists referencing the removed copies,
even when the retained file contains identical audio.
Deleting low-bitrate MP3s also breaks references to those tracks in DJ libraries.

## Validation

Run all repository tests from this directory:

```bash
python3 -m unittest -v
```
