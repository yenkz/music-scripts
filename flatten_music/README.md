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

### Install the `flatten-music` command on macOS

The Finder workflow expects an `flatten-music` launcher at `~/.local/bin/flatten-music`. From the
repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r flatten_music/requirements.txt

mkdir -p "$HOME/.local/bin"
repo_path="$PWD"
printf '#!/bin/zsh\nexec %q %q "$@"\n' \
  "$repo_path/.venv/bin/python" \
  "$repo_path/flatten_music/flatten_music.py" \
  > "$HOME/.local/bin/flatten-music"
chmod +x "$HOME/.local/bin/flatten-music"
```

For direct Terminal use, add `export PATH="$HOME/.local/bin:$PATH"` to
`~/.zshrc`, open a new Terminal window, and verify `flatten-music --help`. Recreate the
launcher if the repository or virtual environment is moved.

## Usage

An explicit folder is required. The default is a strictly read-only preview:

```bash
flatten-music "/path/to/music-folder"
flatten-music --dry-run "/path/to/music-folder"
flatten-music --apply "/path/to/music-folder"
```

`--apply` displays the complete plan and resolved target, then requires typing
`FLATTEN` exactly. Any other response, EOF, or Ctrl-C cancels. `--dry-run` always
wins over `--apply` and never prompts or changes files. There is no unattended
confirmation bypass. `--skip-rename` skips metadata reading and audio renaming;
it still uses every safety check and requires `--apply` to move files. `--help`
shows all flags. Direct script invocation accepts the same arguments:

```bash
.venv/bin/python "flatten_music/flatten_music.py" --dry-run "/path/to/music-folder"
```

Filesystem roots, your home folder, folders without supported audio extensions,
and trees containing common software-project markers are refused (exit 2).
Markers include `.git`, `.hg`, `.svn`, `node_modules`, `.venv`, `venv`,
`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `CMakeLists.txt`, and
`AGENTS.md`. Audio detection uses extensions, not content verification, and these
checks cannot prove that a folder is safe. Use a dedicated music folder.

Artwork, documents, and other companion files still move with the audio; their
count is called out in a warning. Back up the folder before applying changes.
The file list and source stats are checked again after confirmation; changes
require a fresh preview. Operational errors return 1; success or cancellation
returns 0. No credentials, environment settings, cache, or network are used.

### Migrating from `fm`

Create the `flatten-music` launcher above and reinstall the Finder workflow.
Inspect `type -a fm` and `cat "$HOME/.local/bin/fm"`. Remove the old file only if
it is this repository's launcher; remove any old `fm` alias from your shell
configuration, then run `rehash`. Do not remove an Apple-provided command.

## Finder Quick Action installation and usage

The version-controlled **Flatten Music** workflow is in
[`finder_workflows/`](finder_workflows/). After installing the `flatten-music` launcher,
install or update it from the repository root:

```bash
./flatten_music/finder_workflows/install.sh
```

The installer copies the workflow into `~/Library/Services`. Enable it under
**System Settings → Privacy & Security → Extensions → Finder** if it is not
listed in Finder. On first use, allow the action to control Terminal.

To use it, Control-click a folder in Finder and choose **Quick Actions → Flatten
Music**. Terminal opens and runs a read-only `flatten-music --dry-run` first. Review the
complete plan; the same Terminal window runs `flatten-music --apply`, displays its
plan again, and asks you to type `FLATTEN`. Confirm only when the preview is
correct. Any other response leaves the collection unchanged.

Rerun the installer after pulling workflow updates. The workflow resolves
`~/.local/bin/flatten-music` dynamically and does not embed the original contributor's
repository path.

## Tests

From the repository root, run:

```bash
python3 -m unittest -v
```
