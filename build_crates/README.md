# Build music crates

Build UTF-8 M3U8 playlists from existing audio files and tags. The default
preview plans crates for files added in the last 30 days, each discovered label,
populated 20 BPM buckets, and tracks missing an artist or title. `--write`
creates playlists in a `Crates` child folder. It never modifies audio, follows
symlinks, or overwrites any file. Existing playlists receive deterministic
` [2]`, ` [3]` suffixes. Empty crates are omitted.

## Setup

Requires Python 3.10+ and the packages pinned by `requirements.txt`. Mutagen
reads tags and Rich displays progress. No credentials, `.env` settings, cache,
network, or external executables are needed.

From the repository root:

```bash
python3 -m venv .venv  # if needed
.venv/bin/python -m pip install -r build_crates/requirements.txt
mkdir -p "$HOME/.local/bin"
repo_path="$PWD"
printf '#!/bin/zsh\nexec %q %q "$@"\n' \
  "$repo_path/.venv/bin/python" \
  "$repo_path/build_crates/build_crates.py" \
  > "$HOME/.local/bin/music-crates"
chmod +x "$HOME/.local/bin/music-crates"
```

Add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` for Terminal use.
Recreate the launcher if the repository or virtual environment moves. On Linux,
run the Python file directly instead of using the zsh launcher.

## Usage

```bash
music-crates "/path/to/music folder"                         # read-only preview
music-crates --write "/path/to/music folder"                 # create planned playlists
music-crates --recent-days 7 --write "/path/to/music folder"
music-crates --label "Deep House" --bpm 120:130 "/path/to/music folder"
music-crates --all-labels --needs-review "/path/to/music folder"
music-crates --bpm-buckets 10 --output-name "DJ Crates" "/path/to/music folder"
```

If any rule flag is supplied, only the selected rules run. Repeating `--label`
or `--bpm` makes separate playlists. A BPM range includes its minimum and
excludes its maximum. Buckets cover the BPM values actually found, so no empty
bucket files are made. Label matching ignores case but otherwise requires the
full tag value. Needs Review means missing artist or missing title; it does not
judge tag quality. `--output-name` names one direct child folder of the selected
root; its default is `Crates`. `--help` lists all flags. Exit status is `0` for
success, `1` for scan or write errors, and `2` for invalid paths or options.

Recent additions use filesystem creation time where available (macOS); on
other systems they use modification time. They do not use a player library's
import date. BPM comes from common BPM tags, not audio analysis. Label comes
from common label, publisher, organization, or ID3 publisher tags. Files without
readable tags are reported and skipped. Supported extensions match the metadata
utility, including MP3, FLAC, M4A, WAV, AIFF, OGG, and Opus. Scans are recursive;
file and directory symlinks are skipped.

Playlist paths are relative to the output folder. Keep the `Crates` folder with
the music tree when moving it. Existing playlist names are reserved using
case-insensitive and Unicode-normalized comparisons. New files use exclusive
creation, so a concurrent new file cannot be overwritten. A write error can
leave other newly created playlists in place; the summary reports each error.
The preview creates no directories or files. Repeating a write creates suffixed
new playlists; remove old versions yourself when no longer needed.

## Finder Quick Actions

Install the launcher, then run:

```bash
./build_crates/finder_workflows/install.sh
```

**Music Crates — 1 Preview** runs `music-crates FOLDER` without writing.
**Music Crates — 2 Build** shows a native confirmation, then runs
`music-crates --write FOLDER`. Both use the default rules. Open Finder Quick
Actions on a folder; enable them in System Settings → Privacy & Security →
Extensions → Finder if needed. Allow first-run Terminal Automation permission.
Terminal shows current-file progress and the final summary. Rerun the installer
after pulling workflow changes. For missing actions, launcher, or dependencies,
see the [workflow guide](finder_workflows/README.md).

## Tests

```bash
.venv/bin/python -m unittest build_crates.test_build_crates build_crates.test_finder_workflow -v
.venv/bin/python -m unittest -v
git diff --check
```

Tests use disposable folders and small generated WAV audio, never a real
collection.
