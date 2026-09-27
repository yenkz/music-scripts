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

See each subproject's README for setup and usage instructions.

## macOS installation

The Finder workflows call small `fm` and `cm` launchers in `~/.local/bin`.
Run the complete setup from the repository root:

```bash
brew install chromaprint ffmpeg
python3 -m venv .venv
.venv/bin/python -m pip install -r flatten_music/requirements.txt
.venv/bin/python -m pip install -r create_metadata/requirements.txt

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
chmod +x "$HOME/.local/bin/fm" "$HOME/.local/bin/cm"
```

Add `~/.local/bin` to Terminal's command path by placing this line in
`~/.zshrc`, then open a new Terminal window:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Configure the repository-root `.env` as described in the
[`create_metadata` requirements](create_metadata/README.md#requirements), then
verify both launchers:

```bash
fm --help
cm --help
```

The launchers contain the repository's absolute path. Recreate them if the
repository or virtual environment is moved.

## Install the Finder Quick Actions

The ready-to-install Automator workflow bundles are stored with their utilities.
Install or update all of them from the repository root:

```bash
./flatten_music/finder_workflows/install.sh
./create_metadata/finder_workflows/install.sh
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
```

## Important: Rekordbox and Traktor libraries

Flattening and renaming files changes their filesystem paths. If those tracks
have already been used in Rekordbox or Traktor playlists, the applications may
report them as missing and the playlists may appear broken. Back up the DJ
library before applying changes. Afterwards, relocate or re-scan the moved
music and refresh or rebuild the affected playlists. Previewing with
`--dry-run` does not change paths and will not affect a DJ library.

## Validation

Run all repository tests from this directory:

```bash
python3 -m unittest -v
```
