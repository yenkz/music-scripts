# Music Bitrate Finder actions

Requires macOS, Python 3.10+, the repository `.venv` dependencies, and the
`~/.local/bin/music-bitrate` launcher from the [parent README](../README.md).

| Action | Command | Behavior |
| --- | --- | --- |
| **Music Bitrate — 1 Report** | `music-bitrate FOLDER` | Read-only recursive MP3 bitrate report and below-320 deletion candidates. |
| **Music Bitrate — 2 Delete Below 320** | `music-bitrate --delete FOLDER` | Native confirmation with Cancel as default, then a Terminal preview and typed `DELETE` confirmation before permanent deletion. |

Install or update from the repository root:

```bash
./check_bitrate/finder_workflows/install.sh
```

The installer copies the version-controlled bundles to `~/Library/Services`
with `/usr/bin/ditto`. Rerun it after pulling workflow changes. To smoke-test
installation without changing installed services:

```bash
FINDER_SERVICES_DIR="/tmp/music-bitrate-services" \
  ./check_bitrate/finder_workflows/install.sh
```

If actions are missing, enable them in **System Settings → Privacy & Security
→ Extensions → Finder**. Control-click a folder and select **Quick Actions →
Music Bitrate — 1 Report**. Grant permission to control Terminal on first use.
The actions open a fresh Terminal session and wait for the shell to initialize.
Selecting multiple folders opens one session per folder.

For deletion, choose **Music Bitrate — 2 Delete Below 320**, read the native
dialog, then review the exact list in Terminal. Type `DELETE` only to proceed;
any other answer cancels. Deletion is permanent, not Trash. At/above-320 files,
unknown bitrate modes, hard links, symlinks, other formats, and directories
remain. VBR/ABR candidates use their reported average. Tags are never replaced.
Files or folders with analysis errors are skipped and reported before confirmation;
valid candidates can still be deleted. Changed or locked candidates abort the
entire plan during verification. Keep a backup and stop other programs modifying the folder.
An execution failure can leave a partially completed deletion; Terminal reports
completed removals and errors. Use Terminal directly for custom cutoffs.

If Terminal reports a missing launcher, recreate it using the parent README.
The launcher contains absolute paths and must be recreated after moving the
repository or `.venv`. If imports fail, install requirements into that `.venv`.
If the actions behave like an older version, rerun the installer. Leave
Terminal open to read progress, confirmation prompts, and the final summary.
