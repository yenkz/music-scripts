# Flatten Music Finder workflow

The **Flatten Music** macOS Automator Quick Action runs the installed
`~/.local/bin/flatten-music` launcher for a folder selected in Finder. It previews the
complete flattening plan with `flatten-music --dry-run FOLDER`, then runs
`flatten-music --apply FOLDER` and requires typing `FLATTEN` in Terminal before
applying changes. Cancel with any other response. Both commands validate the
folder; no audio or software-project markers prevent execution. Companion files
still move, so back up the selected music folder first.

Install the `flatten-music` launcher first by following the parent README, then install or
update the workflow:

```bash
./flatten_music/finder_workflows/install.sh
```

The installer copies the version-controlled workflow bundle into
`~/Library/Services`. Enable it under **System Settings → Privacy & Security →
Extensions → Finder** if it does not appear in Finder's **Quick Actions** menu.
The first run may ask for permission to control Terminal.

To test installation without changing the user's services directory:

```bash
FINDER_SERVICES_DIR=/tmp/flatten-music-services \
  ./flatten_music/finder_workflows/install.sh
```

Rerun the installer after pulling updates, especially when migrating from `fm`.
If the command is missing, recreate the launcher using the parent README;
recreate it whenever the repository or virtual environment moves.
