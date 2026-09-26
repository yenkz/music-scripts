# Create Metadata

`create_metadata.py` fills in missing artist and title tags with a layered
workflow designed for electronic music:

1. Parse `Artist - Title (Version)` from the filename, removing common DJ key,
   track-number, and download-site prefixes or suffixes.
2. Reuse a matching partial artist or title tag to complete the other field. If
   a missing artist is paired with a title containing the complete filename,
   split that packed title into proper artist and title fields.
3. Validate that candidate against Discogs release tracklists when configured.
4. Try the AcoustID/MusicBrainz fingerprint lookup when configured.
5. Use Shazam recognition from a short middle-of-track sample only as the last
   fallback when enabled.

A filename alone is displayed as `UNVERIFIED` and is never written. A matching
existing partial tag, Discogs validation, filename agreement with an acoustic
result, or an exceptionally confident AcoustID result is required before a
match becomes writable. Packed-title repair is deliberately narrow: the stored
title must match the complete filename and the filename must parse as
`Artist - Title`. Shazam returns a single catalog match rather than a numerical
confidence score, so a positive Shazam result is treated as provider-confirmed
only when there is no filename candidate or when it agrees with that candidate.
A conflicting Shazam result never overrides a parsed filename. Without filename
agreement, AcoustID must score at least 98%. The normal configured threshold
applies when the filename and acoustic result agree.

The script is separate from `flatten_music`: it does not move or rename files.
It scans directories recursively. By default, it fingerprints only files that
are missing artist or title metadata, never overwrites an existing value, and
only previews matches. Fully tagged files are counted in the summary but omitted
from the results table. Tags are written only when `--write` is supplied.

## Requirements

- Python 3.10 or newer.
- At least one lookup service is recommended:
  - An [AcoustID application key](https://acoustid.org/new-application).
  - A [Discogs personal token](https://www.discogs.com/settings/developers).
  - The optional Shazam fallback requires no account or API key.
- Chromaprint's `fpcalc` command when AcoustID is configured.
- FFmpeg when Shazam is enabled.
- The Python packages in `requirements.txt`.

On macOS, install the prerequisites from the repository root:

```bash
brew install chromaprint ffmpeg
python3 -m pip install -r create_metadata/requirements.txt
```

Create the repository-root `.env` file and restrict its permissions:

```bash
cp .env.example .env
chmod 600 .env
```

Then edit `.env` and replace `replace-with-your-application-key`. The file is
ignored by Git; `.env.example` documents the available settings without storing
real secrets:

```dotenv
ACOUSTID_API_KEY=your-application-key
DISCOGS_TOKEN=your-personal-discogs-token

CREATE_METADATA_SHAZAM=true

CREATE_METADATA_FPCALC=fpcalc
CREATE_METADATA_FFMPEG=ffmpeg
CREATE_METADATA_MIN_SCORE=0.90
CREATE_METADATA_MIN_MARGIN=0.05
CREATE_METADATA_TIMEOUT=120
CREATE_METADATA_NETWORK_TIMEOUT=30
```

All three services are optional. Discogs and AcoustID are tried only when their
credentials are present, and Shazam is tried only when
`CREATE_METADATA_SHAZAM=true` or `--shazam` is supplied. Shazam is the final
lookup, after filename/Discogs and AcoustID have not produced an accepted match.
Discogs receives the parsed artist/title text. AcoustID receives only a
Chromaprint fingerprint and track duration. ShazamIO creates a signature locally
from a temporary middle-of-track sample shorter than 15 seconds; the full track
is not uploaded.

ShazamIO uses a reverse-engineered, unofficial Shazam endpoint. It is useful as
a subscription-free fallback, but the endpoint may be rate-limited, changed, or
blocked without notice. Disable it with `CREATE_METADATA_SHAZAM=false` or the
`--no-shazam` command-line flag. The dependency is pinned to an official
upstream commit because the latest PyPI release uses a native component that is
incompatible with Python 3.14 on Apple Silicon.

Command-line options override process environment variables, which override
values from `.env`. Use `--env-file /path/to/config.env` to select another file.

The public AcoustID service is for non-commercial use and limits clients to
three requests per second. The script observes that limit. Fingerprints and
track durations are sent to AcoustID; the audio files themselves are not sent.

## Usage

Preview proposed tags for one file or a whole directory:

```bash
python3 create_metadata/create_metadata.py "/path/to/file.mp3"
python3 create_metadata/create_metadata.py "/path/to/music-folder"
```

Review every match, especially remixes, edits, live recordings, and tracks that
produce more than one possible result. To write only the accepted, unambiguous
matches into missing fields:

```bash
python3 create_metadata/create_metadata.py --write "/path/to/music-folder"
```

To deliberately check files that already have complete metadata, use `--force`.
This is still a read-only preview:

```bash
python3 create_metadata/create_metadata.py --force "/path/to/music-folder"
```

Replacing existing artist and title tags requires both safety flags:

```bash
python3 create_metadata/create_metadata.py \
  --force --write "/path/to/music-folder"
```

Forced replacement changes only artist and title. Other tags remain intact.

The default acceptance rules require a score of at least 90% and a five-point
lead over the next distinct artist/title candidate. They can be made stricter:

```bash
python3 create_metadata/create_metadata.py \
  --min-score 0.95 --min-margin 0.10 "/path/to/music-folder"
```

Without `--force`, an existing artist or title is never overwritten. If it
disagrees with the fingerprint result, the file is reported as a conflict for
manual review. The one exception is a title that exactly represents the full
`Artist - Title` filename while the artist tag is missing; this malformed title
can be split and repaired without `--force`. Files with no database match, a low-confidence result, or
ambiguous results are always left unchanged. Rerun `flatten_music.py --dry-run`
after writing tags.

For a short `cm` terminal command and a read-only Finder Quick Action, follow
the [macOS integration instructions](../README.md#install-the-cm-command-on-macos).

## Tests

From the repository root:

```bash
python3 -m unittest -v
```
