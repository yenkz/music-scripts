# Flatten Music Finder workflow

The **Flatten Music** macOS Automator Quick Action runs the installed
`~/.local/bin/fm` launcher for a folder selected in Finder. It previews the
complete flattening plan first and asks in Terminal before applying changes.

Install the `fm` launcher first by following the parent README, then install or
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
FINDER_SERVICES_DIR=/tmp/fm-services \
  ./flatten_music/finder_workflows/install.sh
```
