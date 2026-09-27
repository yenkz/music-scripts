# Create Metadata Finder workflows

These macOS Automator Quick Actions run the installed `~/.local/bin/cm`
launcher for a folder selected in Finder:

- **Music Metadata — 1 Analyze** performs the normal read-only preview.
- **Music Metadata — 2 Write Accepted** asks for confirmation and writes only
  accepted metadata into missing fields.
- **Music Metadata — Refresh Analysis** refreshes cached provider responses.
- **Music Metadata — Force Analyze** previews every audio file, including files
  with complete metadata. It remains read-only.

None of the bundled workflows runs `--force --write`.

Install the `cm` launcher first by following the parent README, then install or
update all four workflows:

```bash
./create_metadata/finder_workflows/install.sh
```

The installer copies the version-controlled workflow bundles into
`~/Library/Services`. Enable them under **System Settings → Privacy & Security
→ Extensions → Finder** if they do not appear in Finder's **Quick Actions**
menu. The first run may ask for permission to control Terminal.

To test installation without changing the user's services directory:

```bash
FINDER_SERVICES_DIR=/tmp/cm-services \
  ./create_metadata/finder_workflows/install.sh
```
