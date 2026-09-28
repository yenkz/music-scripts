# Check MP3 bitrate

Recursively report the audio bitrate of `.mp3` files (including uppercase
extensions), and optionally delete files strictly below **320 kbps** or another
cutoff. The default is read-only. This reads MPEG audio information, not bitrate
claims in filenames or ID3 tags. No credentials, `.env` settings, cache, network
access, or external audio executables are needed.

## Setup

Requires Python 3.10+ on macOS or Linux. Safe file access uses POSIX directory
descriptors and no-follow flags. Mutagen reads audio information; Rich provides
progress and reports. Their supported version ranges are in `requirements.txt`.

From the repository root:

```bash
python3 -m venv .venv  # only if the environment does not exist yet
.venv/bin/python -m pip install -r check_bitrate/requirements.txt

mkdir -p "$HOME/.local/bin"
repo_path="$PWD"
printf '#!/bin/zsh\nexec %q %q "$@"\n' \
  "$repo_path/.venv/bin/python" \
  "$repo_path/check_bitrate/check_bitrate.py" \
  > "$HOME/.local/bin/music-bitrate"
chmod +x "$HOME/.local/bin/music-bitrate"
```

On macOS, add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` and open a
new Terminal. Recreate the launcher if the repository or virtual environment
moves. On Linux, invoke the Python script directly instead of the zsh launcher.

## Usage

```bash
music-bitrate "/path/to/music folder"
music-bitrate --delete --dry-run "/path/to/music folder"
music-bitrate --delete "/path/to/music folder"
music-bitrate --below 256 "/path/to/music folder"
music-bitrate --below 256 --delete "/path/to/music folder"
```

Without the launcher:

```bash
.venv/bin/python check_bitrate/check_bitrate.py "/path/to/music folder"
.venv/bin/python check_bitrate/check_bitrate.py --delete "/path/to/music folder"
```

| Argument | Behavior |
| --- | --- |
| `folder` | Required real directory; scan it and its subfolders. |
| `--below KBPS` | Integer 1–320; default 320. Select strictly lower bitrates. |
| `--delete` | Show the fixed plan, skip analysis failures, then require typing exactly `DELETE` before permanent deletion of valid candidates. |
| `--dry-run` | Read-only, including when combined with `--delete`; no confirmation or deletion. |
| `--help` | Show CLI help. |

Every MP3 has a bitrate, mode, and decision in the report. Reports and deletion
previews list files from highest to lowest bitrate, with filename/path order
breaking ties. Separate colored sections show **≥320 kbps in green** and
**below 320 kbps in yellow**. The section labels also work without terminal
color. This visual split stays at 320 kbps even with a custom `--below` cutoff;
the Decision column identifies actual deletion candidates and protected files.
Unreadable or invalid
files are excluded from deletion and reported as errors on stderr before the
confirmation prompt. Other valid candidates can still be deleted. Progress shows the current folder/file
and stage. The final summary reports analyzed files, candidates, protected
files, skipped symlinks, deletions, errors, and elapsed time. Exit codes: `0`
for successful reports, deletion, or cancellation; `1` for operational errors
or an interrupted operation; `2` for invalid usage/configuration.
Exit code `1` also applies when valid candidates were deleted successfully but
some files or folders could not be analyzed; the summary distinguishes this
from a stopped deletion.

Deletion is **permanent, not Trash**. Any response other than exactly `DELETE`,
EOF, or Ctrl-C at the confirmation cancels it. There is no unattended bypass.
Keep a backup; removing tracks can break Rekordbox/Traktor playlists that
reference their paths.

## Bitrate interpretation and safety

- Confirmed CBR (constant bitrate) uses the nearest whole kbps to account for
  MPEG frame padding. A 320 kbps CBR file is retained even if its header
  calculation yields 319.998 kbps.
- VBR and ABR use the **reported average bitrate**, without rounding for the
  threshold comparison. A VBR file averaging 245 kbps is a deletion candidate
  at the default cutoff, even if some frames reach 320 kbps.
- `UNKNOWN` means Mutagen could only estimate bitrate from the first frame.
  These files are displayed but **never deleted**, even when the estimate is
  below the cutoff. This can include CBR files lacking an Info/Xing header.
- Bitrate is not a listening-quality test or proof of the source quality;
  re-encoding a poor source at 320 kbps does not restore it.

See [Mutagen's MPEGInfo documentation](https://mutagen.readthedocs.io/en/latest/api/mp3.html)
for the bitrate and mode fields.

Only regular `.mp3` files recognized as MPEG Layer III are considered. Other
formats, file/directory symlinks, special files, and hard-linked files are
excluded from deletion. Directories are left in place. Tags are never written.
Files or folders that cannot be analyzed are skipped and reported; they do not
block deletion of successfully analyzed candidates. Before deletion, every
candidate is checked for changed size, timestamps, device/inode, link count,
and macOS immutable/append flags, then checked again immediately before its
removal. Directory descriptors prevent following replaced symlink parents.
New files arriving after analysis are not added to the plan.

Stop other programs editing or reorganizing the folder during a run. Filesystem
checks cannot make a multi-file deletion transactional: a concurrent change,
permission error, or interruption after deletion begins can leave a partially
completed run. The utility stops at the first deletion error and reports which
files were removed; they cannot be rolled back.

## Finder Quick Actions

Install the launcher above, then run from the repository root:

```bash
./check_bitrate/finder_workflows/install.sh
```

Control-click a folder in Finder and choose **Quick Actions**:

| Action | Exact command and behavior |
| --- | --- |
| **Music Bitrate — 1 Report** | `music-bitrate FOLDER`: read-only report at the 320 kbps cutoff. |
| **Music Bitrate — 2 Delete Below 320** | Native confirmation (Cancel is the default), then `music-bitrate --delete FOLDER`: displays the plan and requires typing `DELETE` in Terminal. |

Enable the actions in **System Settings → Privacy & Security → Extensions →
Finder** if needed. Allow first-run permission to control Terminal. Each
selected folder opens a separate Terminal session; review each separately.
Rerun the installer after pulling workflow changes. If a launcher is missing,
recreate it; if Python reports missing modules, install requirements into the
repository `.venv`. See [the workflow guide](finder_workflows/README.md) for
installation testing and troubleshooting. Custom cutoffs are Terminal-only.

## Tests

From the repository root:

```bash
.venv/bin/python -m unittest check_bitrate.test_check_bitrate check_bitrate.test_finder_workflow -v
.venv/bin/python -m unittest -v
git diff --check
```

Tests use temporary files and injected readers. An additional integration test
generates disposable MP3s with FFmpeg when available; otherwise that test is
explicitly skipped. FFmpeg is not needed to use the utility. Workflow installer
tests use temporary destinations, never installed user workflows.
