# Music Scripts

This repository contains independent utilities for managing digital music files.
Each utility lives in its own subproject directory with its documentation,
dependencies, and tests.

## Projects

- [`flatten_music/`](flatten_music/) — flatten a directory tree and rename audio
  files from their embedded metadata.
- [`create_metadata/`](create_metadata/) — identify untagged audio with
  filename parsing, Discogs, AcoustID, and Shazam, then create missing artist
  and title tags.

See each subproject's README for setup and usage instructions.

## Install the `fm` command on macOS

The following setup keeps this project's dependencies in a virtual environment
and installs a small `fm` launcher in `~/.local/bin`. Run these commands from the
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

Ensure this line appears once in `~/.zshrc` so Terminal can find the command:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Open a new Terminal window, or run `source ~/.zshrc`, then verify the command:

```bash
fm --help
fm --dry-run "/path/to/music-folder"
```

Always review a dry run first. To apply the displayed plan, omit `--dry-run`:

```bash
fm "/path/to/music-folder"
```

The launcher contains the repository's current absolute path. Recreate it with
the installation commands above if you move or rename this repository.

## Install the `cm` command on macOS

The `cm` command previews missing metadata by default and uses the repository's
`.env` configuration. Install Chromaprint and create the launcher from the
repository root:

```bash
brew install chromaprint ffmpeg
python3 -m venv .venv
.venv/bin/python -m pip install -r create_metadata/requirements.txt

mkdir -p "$HOME/.local/bin"
repo_path="$PWD"
printf '#!/bin/zsh\nexec %q %q "$@"\n' \
  "$repo_path/.venv/bin/python" \
  "$repo_path/create_metadata/create_metadata.py" \
  > "$HOME/.local/bin/cm"
chmod +x "$HOME/.local/bin/cm"
```

If you have not already configured the `fm` command, ensure this line appears
once in `~/.zshrc`, then open a new Terminal window:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Create `.env` as described in the
[`create_metadata` setup instructions](create_metadata/README.md#requirements),
then verify the command:

```bash
cm --help
cm "/path/to/music-folder"
```

The first scan is a read-only preview. After reviewing every proposed match,
write the accepted metadata with:

```bash
cm --write "/path/to/music-folder"
```

By default, `cm` only fingerprints files missing artist or title metadata and
never overwrites an existing value. To preview every audio file, including
fully tagged files, use `cm --force`. Replacing existing artist and title tags
requires both explicit flags:

```bash
cm --force --write "/path/to/music-folder"
```

The same `~/.zshrc` `PATH` entry used by `fm` makes `cm` available. Like the
`fm` launcher, recreate `cm` if this repository is moved or renamed.

## Add a Finder Quick Action with Automator

This creates a safe Finder action that opens Terminal and previews the selected
folder with `fm --dry-run`:

1. Open **Automator**, choose **New Document**, then select **Quick Action**.
2. Set **Workflow receives current** to **folders** in **Finder**.
3. Add the **Run Shell Script** action.
4. Set **Shell** to `/bin/zsh` and **Pass input** to **as arguments**.
5. Paste this script into the action:

   ```bash
   for selected_folder in "$@"; do
     /usr/bin/osascript - "$selected_folder" <<'APPLESCRIPT'
   on run argv
     set selectedFolder to item 1 of argv
     tell application "Terminal"
       activate
       do script "$HOME/.local/bin/fm --dry-run " & quoted form of selectedFolder
     end tell
   end run
   APPLESCRIPT
   done
   ```

6. Save it as **Preview Flatten Music**.

In Finder, Control-click a folder and choose **Quick Actions → Preview Flatten
Music**. After reviewing the report, run the displayed folder through `fm`
without `--dry-run` in Terminal to apply it. If the action is not shown, enable
it under **System Settings → Privacy & Security → Extensions → Finder**.

## Add a Create Metadata Finder Quick Action with Automator

This Finder action opens Terminal and runs the safe, read-only `cm` preview for
the selected folder:

1. Open **Automator**, choose **New Document**, then select **Quick Action**.
2. Set **Workflow receives current** to **folders** in **Finder**.
3. Add the **Run Shell Script** action.
4. Set **Shell** to `/bin/zsh` and **Pass input** to **as arguments**.
5. Paste this script into the action:

   ```bash
   for selected_folder in "$@"; do
     /usr/bin/osascript - "$selected_folder" <<'APPLESCRIPT'
   on run argv
     set selectedFolder to item 1 of argv
     tell application "Terminal"
       activate
       do script "$HOME/.local/bin/cm " & quoted form of selectedFolder
     end tell
   end run
   APPLESCRIPT
   done
   ```

6. Save it as **Preview Create Music Metadata**.

In Finder, Control-click a folder and choose **Quick Actions → Preview Create
Music Metadata**. The action never writes or replaces tags. After reviewing the report, run
`cm --write "/path/to/music-folder"` in Terminal to apply accepted matches. If
the action is not shown, enable it under **System Settings → Privacy & Security
→ Extensions → Finder**.

## Important: Rekordbox and Traktor libraries

Flattening and renaming files changes their filesystem paths. If those tracks
have already been used in Rekordbox or Traktor playlists, the applications may
report them as missing and the playlists may appear broken. Back up the DJ
library before applying changes. Afterwards, relocate or re-scan the moved
music and refresh or rebuild the affected playlists. Previewing with
`--dry-run` does not change paths and will not affect a DJ library.

## Validation

Run all repository tests from this directory:

```bash
python3 -m unittest -v
```
