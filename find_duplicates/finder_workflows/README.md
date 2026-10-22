# Music Duplicates Finder workflow

The **Music Duplicates — Review** macOS Quick Action opens Terminal and runs:

```text
music-dupes --menu SELECTED_FOLDER
```

The Terminal menu offers:

1. Report exact duplicates (read-only; Enter selects this default).
2. Review keeper/skip choices one group at a time (read-only).
3. Preview automatic removal (read-only).
4. Automatically remove numbered and same-name copies after showing the plan and confirming.
5. Review similar names and verify decoded audio (read-only; requires FFmpeg).
6. Remove verified audio copies with similar names after showing a plan and confirming.
7. Choose keepers for byte-identical copies, then show the selected plan and confirm permanent removal.

Enter `q` or press Ctrl-C to quit. On macOS, removal requires clicking **Remove
Duplicates** in a native confirmation dialog; **Cancel** is the default. The
dialog expires after 120 seconds without removal. Deletion is permanent, not
Trash. Only byte-identical copies with matching names are selected. Unsuffixed
originals take priority over numbered copies; among matching originals, the
copy closest to the selected root is kept. Equal-depth ties are chosen randomly
once per plan. Identical filenames repeated across folders follow the same depth
rule even when numbered. Ambiguous names and hard-linked groups are skipped,
and all selected files are revalidated before removal. Keepers and skipped
groups remain unchanged.

Options 5 and 6 require `ffmpeg` and `ffprobe` on Terminal's PATH (`brew install
ffmpeg`). They also find `-1` suffixes and truncated titles, and compare the
decoded audio without tags. Three, four, or more copies with matching audio form
one verified group even if their file hashes differ. Other similar-name pairs
are shown for inspection but never removed together. Option 6 keeps the shallowest
copy, prefers an unnumbered name at that depth, and chooses remaining ties randomly
once per plan. Only the keeper's tags, artwork and cue points survive; they may
differ between otherwise identical audio copies. All file hashes are rechecked
before removal. Scan/read/changed-file errors block removal. Files that cannot
be decoded are excluded, while other independently verified groups remain
eligible for confirmed removal; unverified files are listed in the summary.

The menu does not write a JSON manifest. Use `music-dupes --review --output ...`
directly in Terminal when decisions should be saved. See the parent README for
matching rules and safety boundaries. Direct `--review` remains read-only.
Option 7 accepts a keeper number for each exact group; `s` skips a group and
`q` cancels the entire removal run. Differently named files can be selected
only when their complete contents match. The native confirmation appears after
the full `KEEP`/`REMOVE` plan is printed.

Install the `~/.local/bin/music-dupes` launcher as described in the parent
README, then install or update the workflow from the repository root:

```bash
./find_duplicates/finder_workflows/install.sh
```

The installer copies the version-controlled workflow bundle into
`~/Library/Services`. Enable it under **System Settings → Privacy & Security →
Extensions → Finder** if it is absent from Finder's **Quick Actions** menu. On
first use, allow the workflow to control Terminal. The action opens a fresh tab
and waits for its shell to initialize before launching the menu. Removal uses
the macOS built-in `/usr/bin/osascript` to display its confirmation.

To use it, Control-click one folder in Finder and choose **Quick Actions → Music
Duplicates — Review**. If multiple folders are selected, each receives its own
Terminal menu session. Choose an option in Terminal; the operation runs and
exits. Reopen the Quick Action for another operation.

To test installation without changing installed user workflows:

```bash
FINDER_SERVICES_DIR=/tmp/music-dupes-services \
  ./find_duplicates/finder_workflows/install.sh
```

Rerun the installer after pulling workflow changes. If Terminal reports that
the command is missing, recreate the launcher; it contains the repository and
virtual environment's absolute paths.
If the old per-group review appears immediately, rerun the installer to replace
the old workflow with the menu version. The Finder action name stays the same.
The seven-option menu is implemented in the Python script, so an existing menu
workflow picks up these options when the repository code is updated.
