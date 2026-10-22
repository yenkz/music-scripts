# Find Duplicate Music

`find_duplicates.py` recursively finds supported audio files whose complete file
contents are byte-for-byte identical. It helps distinguish confirmed duplicates
from tracks that merely have similar names. The normal scan is strictly
read-only, and ordinary interactive review records decisions without changing
audio. Explicit `--review --auto-remove` can permanently remove clearly named
duplicate copies after showing a plan and asking for confirmation.

Optional `--similar` finds similar filenames and checks their decoded audio,
ignoring tags and artwork. It can combine three, four, or more copies into one
verified audio group even when their whole-file hashes differ. This mode is
also read-only unless `--review --auto-remove` is explicitly supplied.

The scanner groups files by size before calculating SHA-256 hashes, so a unique
file size requires no content read. It checks each candidate before and after
hashing and reports a file that changed, disappeared, or became unreadable while
allowing the rest of the scan to finish. Directory and file symlinks are never
followed.

The default mode finds exact whole-file duplicates. Copies with different
tags, artwork, containers, encodings, or other bytes are not matches, even when
they contain the same recording. Use `--similar` for the additional audio check.

## Requirements and installation

- Python 3.10 or newer.
- The Python packages in `requirements.txt`.
- No API credentials, network access, Mutagen, or Chromaprint.
- Optional similar-name/audio verification requires `ffmpeg` and `ffprobe` on
  PATH. Install both with `brew install ffmpeg`. Ordinary exact-file scans do
  not require them.
- Automatic removal requires macOS or Linux with POSIX directory-descriptor
  support. Reports and ordinary review do not require this capability.

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r find_duplicates/requirements.txt

mkdir -p "$HOME/.local/bin"
repo_path="$PWD"
printf '#!/bin/zsh\nexec %q %q "$@"\n' \
  "$repo_path/.venv/bin/python" \
  "$repo_path/find_duplicates/find_duplicates.py" \
  > "$HOME/.local/bin/music-dupes"
chmod +x "$HOME/.local/bin/music-dupes"
```

For direct Terminal use, add `export PATH="$HOME/.local/bin:$PATH"` to
`~/.zshrc`, open a new Terminal window, and verify `music-dupes --help`. The
launcher contains absolute paths, so recreate it if the repository or virtual
environment moves.

The utility has no environment settings or secrets. Similar-name sensitivity and
audio command timeouts are configured with the CLI options below.

## Terminal option selector

The Finder Quick Action opens this menu. You can also open it directly:

```bash
music-dupes --menu "/path/to/music folder"
```

Choose a number, then the selected operation runs and exits:

1. **Report exact duplicates** — read-only; also the default when you press Enter.
2. **Review keepers** — read-only, one group at a time.
3. **Preview automatic removal** — lists keepers, removals, and skipped groups without changes.
4. **Automatically remove numbered and same-name copies** — shows the plan, then asks for confirmation.
5. **Review similar names and verify audio** — read-only report of verified audio groups and other name candidates.
6. **Remove verified audio copies with similar names** — shows the audio groups and keeper/removal plan, then asks for confirmation.
7. **Choose keepers for exact copies, then confirm removal** — select one keeper per byte-identical group, skip groups as needed, inspect the fixed plan, and confirm permanent removal.

Enter `q` or press Ctrl-C to quit the menu without scanning or changing files.
On macOS, options 4, 6, and 7 display a native **Remove Duplicates** confirmation with
**Cancel** selected by default. It explains that removal is permanent and that
keepers and skipped groups remain unchanged. Cancelling or letting the dialog
expire removes nothing. Other platforms use the typed `DELETE` confirmation.

`--menu` requires an interactive terminal and cannot be combined with `--review`,
`--auto-remove`, `--remove-reviewed`, `--dry-run`, `--similar`, or `--output`. The macOS dialog requires the built-in
`/usr/bin/osascript` and expires after 120 seconds; use `--dialog-timeout SECONDS`
with `--menu` to change that limit (1–3600). Direct commands below keep their
existing behavior, including a read-only report when no mode flag is supplied.

## Read-only report

Scan the current directory or a chosen music folder:

```bash
music-dupes
music-dupes "/path/to/music folder"
```

The report shows every exact duplicate group, relative paths, size, a shortened
SHA-256 digest, hard-link annotations, and the maximum storage that independent
copies could reclaim. The summary reports audio files discovered, candidates
hashed, duplicate groups, symlinks skipped, errors, and elapsed time.

Hard-linked paths are included because they are multiple library paths, but
they already share one stored file. They therefore do not inflate the reported
recoverable space.

## Interactive review and JSON manifests

Review the duplicate groups in Terminal:

```bash
music-dupes --review "/path/to/music folder"
```

For each group, enter a path number to select its keeper, `s` to skip the group,
or `q` to stop. Quitting leaves the current and remaining groups unreviewed. No
choice changes the music collection.

To preserve the decisions, explicitly provide a new output path:

```bash
music-dupes --review \
  --output "/path/to/reviews/library-duplicates.json" \
  "/path/to/music folder"
```

`--output` is accepted only with `--review`. The destination's parent directory
must already exist, and the command refuses to overwrite an existing file,
directory, or symlink. The manifest is published atomically and contains:

- schema version, absolute scan root, and SHA-256 algorithm;
- each group's size, digest, relative paths, and hard-link sets;
- `reviewed`, `skipped`, or `unreviewed` status;
- the selected keeper and duplicate candidates for reviewed groups;
- scan completeness and any file errors.

The manifest records review choices, not completed removals. There is no command
to apply a saved manifest; automatic removal always performs a fresh scan.

## Remove exact duplicates with chosen keepers

Use menu option **7** or run:

```bash
music-dupes --review --remove-reviewed "/path/to/music folder"
```

The tool reports byte-for-byte duplicate groups, then asks which numbered path
to keep in each group. Enter `s` to leave a group untouched. Enter `q` or press
Ctrl-C to cancel the entire removal run; earlier choices are discarded. After
all groups are reviewed, it prints every `KEEP`, `REMOVE`, and `SKIP` decision.
In Terminal, type `DELETE` exactly to confirm; the Finder menu uses a native
macOS confirmation. Any other response leaves all files untouched. Groups with
different filenames can be selected because the complete file contents must
match. Hard-linked files or changed files block removal during revalidation.
This operation is permanent and does not use Trash. Plain `--review` remains
read-only, and `--remove-reviewed` cannot be combined with `--similar`,
`--auto-remove`, `--dry-run`, or `--output`.

## Automatically remove numbered and same-name duplicate copies

Preview all automatic choices without prompts or filesystem writes:

```bash
music-dupes --review --auto-remove --dry-run "/path/to/music folder"
```

Then run in Terminal to show the plan and apply it with one confirmation:

```bash
music-dupes --review --auto-remove "/path/to/music folder"
```

For each byte-identical group with matching names, the script prefers a filename
without a terminal numbered suffix and removes its duplicate copies. For example:

```text
KEEP   S-Ampel - Questions (Ruben F Remix).mp3
REMOVE S-Ampel - Questions (Ruben F Remix) [2].mp3
REMOVE S-Ampel - Questions (Ruben F Remix) [3].mp3
```

The rule recognizes ` [2]`, ` [3]`, and larger numbers immediately before the
extension, including repeated suffixes such as ` [2] [3]`. Matching ignores case
and Unicode normalization and can match files in different subfolders. All
filenames must reduce to the same name and extension. If several unsuffixed
originals exist, the copy closest to the selected scan root is kept. For example:

```text
KEEP   2012/014. Robosonic - The Edge (Original Mix).mp3
REMOVE 2013/Maure/014. Robosonic - The Edge (Original Mix).mp3
```

Among originals at the same folder depth, one keeper is chosen randomly. The
choice is made once when building the plan and stays fixed through confirmation
and removal. A separate preview or later run may choose a different equal-depth
keeper. Numbered copies never take priority over an unsuffixed original, even
if they are closer to the root.

Groups consisting entirely of the same filename also use the folder-depth rule,
including names such as `Track [2].mp3` repeated across folders. Groups with
different numbered suffixes but no unsuffixed original, unrelated names, or
hard-linked paths are skipped. Names such as `Track (2)` or `Track copy` are not
guessed to match `Track`. Filename similarity alone never establishes a
duplicate: complete file hashes must match first.

The plan lists every `KEEP`, `REMOVE`, and skipped group. Type **DELETE** exactly
once to remove all planned duplicates; any other answer, EOF, or Ctrl-C at the
prompt cancels. **Removal is permanent, not a move to Trash.** Back up valuable
libraries first. Ordinary `--review` remains read-only. The Finder menu also
offers automatic removal; on macOS its native confirmation replaces typing
`DELETE`.

After confirmation, the script revalidates identities and hashes for the entire
plan before deleting anything. Scan errors, changed/missing files, symlinks,
locked files or parent directories, and hard links (including links outside the
scan) block removal. Each duplicate and its keeper are checked again immediately
before deletion, using directory descriptors to avoid following symlink parents.
Do not edit, move, or retag the collection concurrently: these checks cannot lock
out other applications. If removal fails or is interrupted partway through,
completed deletions cannot be rolled back; the summary reports completed
removals and errors. Rescan before retrying. Empty directories are left in place.

`--auto-remove` requires `--review`; `--dry-run` requires both. Automatic mode
cannot be combined with `--output`, so a preview never writes a manifest.
Actual removal requires an interactive terminal; its dry run can be redirected
to a file. No per-group keeper questions are asked in automatic mode.

Exit status is `0` for a successful report or deliberately shortened review,
`1` when operational errors occurred, and `2` for invalid paths, arguments, or
non-interactive menu/review/removal (except automatic `--dry-run`). Cancelling the
removal confirmation returns `0` with no deletions.

## Similar names and identical audio with different metadata

For collections with `-1` copies, truncated titles, track-number prefixes, or
different metadata, use the additional audio verification mode:

```bash
# Read-only report of verified groups and other similar-name candidates.
music-dupes --similar "/path/to/music folder"

# Preview which copies would be kept and removed, without prompts or writes.
music-dupes --similar --review --auto-remove --dry-run "/path/to/music folder"

# Show a fresh plan, then require typing DELETE before permanent removal.
music-dupes --similar --review --auto-remove "/path/to/music folder"
```

Finder menu **5** opens the report; **6** opens the confirmed removal flow with
the native macOS dialog. Existing menu options 1–4 retain their exact-file rules.
`--similar --review` offers read-only keeper selection for verified audio groups.
`--output` is not supported with `--similar`.

Names are suggestions, never proof. Candidate matching ignores case, accents,
extensions, track-number and `Unknown Artist` prefixes, common copy suffixes
(` [2]`, `-1`, `(2)`), and some trailing download-site text. It compares names
sharing a useful word or initial prefix, allowing minor spelling differences
and truncated titles. `--similarity NUMBER` sets the minimum name score from
0.5 to 1.0 (default 0.86). The score is a text heuristic, not a probability that
two recordings match. Unrelated filenames may be missed; different remixes can
appear as candidates. Lowering the threshold considers more candidates and
takes longer.

Every candidate is hashed and its full audio decoded locally with FFmpeg. The
comparison preserves sample rate, channel count/layout, and decoded sample
precision; it does not resample, change volume, trim silence, or apply a fuzzy
audio threshold. Tags and artwork are excluded. Exact decoded-audio matches
form one group regardless of the number of copies or original whole-file hashes.
The report labels other name matches **DIFFERENT AUDIO** or **UNVERIFIED / ERROR**;
they are not eligible for removal together. Different encodings of the same
recording can produce different samples and remain separate.

For a verified group, the keeper is closest to the selected root. At that depth,
an unnumbered filename is preferred; remaining ties are chosen randomly once
per plan. All other independent copies in the group are removal candidates.
**Tags, artwork, cue points, and encoding may differ; removal preserves only the
keeper's metadata.** Review the displayed paths before confirming. The tool does
not merge tags or select the most complete metadata automatically.

The entire plan is revalidated using each file's original whole-file hash before
any deletion, including its metadata. The existing locked-file, symlink,
hard-link, and per-deletion keeper checks still apply. Scan, file-read, or
changed-file errors block automatic removal for the run. Files that FFmpeg
cannot verify (including decoder timeouts) are explicitly excluded from removal;
other independently verified groups can still be removed after confirmation.
No audio files are modified during analysis.

`--audio-timeout SECONDS` limits each FFprobe/FFmpeg command (1–3600, default 120).
Decoding can take considerably longer than ordinary hashing; progress shows the
current stage and file. The summary includes decoded files, reuse of results
for byte-identical files, candidate pairs, verified groups, unverified files,
errors, and elapsed time. A run with unverified files returns exit status `1`,
even when the verified portion completes successfully. Results are reused only
within a run; there is no persistent audio cache,
and a new run rescans and verifies the files. Ctrl-C during analysis changes no
music files.

## Finder Quick Action

Install the `music-dupes` launcher first, then install or update the Finder
workflow from the repository root:

```bash
./find_duplicates/finder_workflows/install.sh
```

Enable it under **System Settings → Privacy & Security → Extensions → Finder**
if it does not appear. On first use, allow the workflow to control Terminal.

Control-click a music folder and choose **Quick Actions → Music Duplicates —
Review**. Terminal opens `music-dupes --menu` for the selected folder, letting
you choose exact-file report/review/removal or similar-name audio verification
and confirmed removal. The action keeps its existing Finder name so reinstalling updates it
in place. The menu does not write manifests; use the Terminal command with
`--review --output` when decisions need to be saved.

Rerun the installer after pulling workflow updates. See
[`finder_workflows/README.md`](finder_workflows/README.md) for installation and
troubleshooting details.

## Tests

From the repository root:

```bash
.venv/bin/python -m unittest -v
```
