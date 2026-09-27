# Flat Music Folder

`flatten_music.py` turns a nested directory into a flat collection and renames
recognized audio files using their embedded metadata:

```text
Artist - Track (Version).ext
```

If the title metadata already ends in a version such as `(Original Mix)` or
`(Club Edit)`, that text is preserved as the version. If no version exists, the
name becomes `Artist - Track.ext`. Files without both artist and title metadata
keep their existing names.

The script never overwrites a file. Name collisions receive a suffix such as
`[2]`; existing generated collision suffixes remain stable on later runs. It
moves every file found below the root (not only music), removes real
directories after they become empty, and leaves directory symlinks alone.
Locked (immutable) files are reported before any files are changed. If an
unexpected error occurs while staging renames, already staged files are restored
to their original names. Staging files left by an older interrupted run are
detected and must be recovered before a new run can start.

## Setup

Python 3.10 or newer is required. From the repository root, install the
dependencies:

```bash
python3 -m pip install -r flatten_music/requirements.txt
```

The output uses Rich to provide colored summaries and before/after tables. Color
is enabled automatically in a terminal and omitted cleanly when redirected to a
file.

### Install the `fm` command on macOS

The Finder workflow expects an `fm` launcher at `~/.local/bin/fm`. From the
repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r flatten_music/requirements.txt

mkdir -p "$HOME/.local/bin"
repo_path="$PWD"
printf '#!/bin/zsh\nexec %q %q "$@"\n' \
  "$repo_path/.venv/bin/python" \
  "$repo_path/flatten_music/flatten_music.py" \
  > "$HOME/.local/bin/fm"
chmod +x "$HOME/.local/bin/fm"
```

For direct Terminal use, add `export PATH="$HOME/.local/bin:$PATH"` to
`~/.zshrc`, open a new Terminal window, and verify `fm --help`. Recreate the
launcher if the repository or virtual environment is moved.

## Usage

Preview the operation first:

```bash
python3 /path/to/music-scripts/flatten_music/flatten_music.py --dry-run /path/to/music-folder
```

Run it:

```bash
python3 /path/to/music-scripts/flatten_music/flatten_music.py /path/to/music-folder
```

If the script is copied into the folder you want to process, the directory can
be omitted:

```bash
python3 flatten_music.py --dry-run
python3 flatten_music.py
```

The script itself remains in the root because it is already there and is not an
audio file. To flatten without installing Mutagen or renaming audio, add
`--skip-rename`.

The report lists all changes first and leaves the summary at the bottom, where
it remains visible when a long dry run finishes.

## Finder Quick Action installation and usage

The version-controlled **Flatten Music** workflow is in
[`finder_workflows/`](finder_workflows/). After installing the `fm` launcher,
install or update it from the repository root:

```bash
./flatten_music/finder_workflows/install.sh
```

The installer copies the workflow into `~/Library/Services`. Enable it under
**System Settings → Privacy & Security → Extensions → Finder** if it is not
listed in Finder. On first use, allow the action to control Terminal.

To use it, Control-click a folder in Finder and choose **Quick Actions → Flatten
Music**. Terminal opens and runs a read-only `fm --dry-run` first. Review the
complete plan; the same Terminal window then asks `Run for real? [y/N]`. Enter
`y` only when the preview is correct. Any other response leaves the collection
unchanged.

Rerun the installer after pulling workflow updates. The workflow resolves
`~/.local/bin/fm` dynamically and does not embed the original contributor's
repository path.

## Tests

From the repository root, run:

```bash
python3 -m unittest -v
```
