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

The menu does not write a JSON manifest. Use `music-dupes --review --output ...`
directly in Terminal when decisions should be saved. See the parent README for
matching rules and safety boundaries. Direct `--review` remains read-only.

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
