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
`[2]`. It moves every file found below the root (not only music), removes real
directories after they become empty, and leaves directory symlinks alone.

## Setup

Python 3.10 or newer is required. From the repository root, install the
dependencies:

```bash
python3 -m pip install -r flatten_music/requirements.txt
```

The output uses Rich to provide colored summaries and before/after tables. Color
is enabled automatically in a terminal and omitted cleanly when redirected to a
file.

## Usage

Preview the operation first:

```bash
python3 /path/to/music-scripts/flatten_music/flatten_music.py --dry-run /path/to/music-folder
```

Run it:

```bash
python3 /path/to/music-scripts/flatten_music/flatten_music.py /path/to/music-folder
```

If the script is copied into the folder you want to process, the directory can
be omitted:

```bash
python3 flatten_music.py --dry-run
python3 flatten_music.py
```

The script itself remains in the root because it is already there and is not an
audio file. To flatten without installing Mutagen or renaming audio, add
`--skip-rename`.

The report lists all changes first and leaves the summary at the bottom, where
it remains visible when a long dry run finishes.

## Tests

From the repository root, run:

```bash
python3 -m unittest -v
```
