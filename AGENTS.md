# AGENTS.md

## Repository purpose

This repository contains independent, production-quality Python utilities for
managing digital music collections. Each utility must be safe to run on valuable
libraries, understandable from its own documentation, testable without real
music fixtures, and convenient to use from both Terminal and macOS Finder.

Python 3.10 or newer is required. Prefer the repository `.venv` for development
commands when it exists.

## Current structure

### `flatten_music/`

- `flatten_music.py` — CLI, deterministic planning, filesystem execution,
  rollback-safe staged renames, and Rich reporting.
- `test_flatten_music.py` — planning and temporary-filesystem behavior tests.
- `finder_workflows/Flatten Music.workflow` — version-controlled Finder Quick
  Action that previews first and confirms before mutation.
- `finder_workflows/install.sh` and `finder_workflows/README.md` — workflow
  installation and usage.
- `test_finder_workflow.py` — workflow portability and safety tests.
- `requirements.txt` — utility-specific dependencies.
- `README.md` — complete setup, CLI, safety, and Finder instructions.

### `create_metadata/`

- `create_metadata.py` — CLI orchestration, tag reading/writing, AcoustID
  selection, caching adapters, progress, metrics, and Rich reporting.
- `recognition.py` — filename parsing and Discogs/Shazam provider clients.
- `cache.py` — versioned, persistent SQLite recognition cache.
- `test_*.py` — unit, provider, cache, workflow, and temporary-media tests.
- `finder_workflows/` — Analyze, Write Accepted, Refresh Analysis, and Force
  Analyze Finder Quick Actions, plus their installer and README.
- `requirements.txt` — utility-specific dependencies.
- `README.md` — complete setup, configuration, CLI, cache, safety, and Finder
  instructions.

### `find_duplicates/`

- `find_duplicates.py` — exact-file discovery, SHA-256 grouping, interactive
  keeper review, JSON manifest writing, and Rich reporting.
- `test_find_duplicates.py` — scanning, hashing, hard-link, review, manifest,
  and CLI behavior tests.
- `finder_workflows/Music Duplicates — Review.workflow` — version-controlled
  Finder Quick Action for interactive review.
- `finder_workflows/install.sh` and `finder_workflows/README.md` — workflow
  installation and usage.
- `test_finder_workflow.py` — workflow portability and exact-command tests.
- `requirements.txt` and `README.md` — dependencies and complete usage.

### Repository root

- `README.md` — project index and combined macOS/Terminal/Finder setup.
- `.env.example` — documented, non-secret configuration template.
- `.gitignore` — secrets, environments, generated caches, and local files.

## Required structure for a new utility

Create every new utility as a self-contained subproject. Use a lowercase,
snake_case name consistently:

```text
new_utility/
├── new_utility.py
├── __init__.py                  # when imports are useful
├── requirements.txt
├── README.md
├── test_new_utility.py
└── finder_workflows/            # for a user-facing macOS utility
    ├── New Utility.workflow/
    │   └── Contents/
    │       ├── Info.plist
    │       └── document.wflow
    ├── install.sh
    └── README.md
```

Additional modules are encouraged when they isolate a real responsibility such
as providers, caching, filename policy, or filesystem execution. Do not build a
generic framework shared by unrelated utilities unless repeated requirements
clearly justify it.

When adding, removing, or renaming a utility, update the root `README.md` project
index and combined installation instructions.

## Python implementation standards

- Use Python 3.10+ syntax, complete type annotations, `pathlib.Path`, and small
  functions with explicit inputs and return values.
- Prefer immutable `@dataclass(frozen=True)` value objects for plans, matches,
  outcomes, configuration, and other records.
- Prefer the standard library. Add a dependency only when it materially improves
  correctness or user experience, pin it appropriately, and document it in the
  subproject `requirements.txt` and README.
- Keep module imports usable both as a package and, where supported, through
  direct script execution.
- Put CLI parsing in `parse_args`, orchestration in `main`, and terminate through
  `raise SystemExit(main())`. Return predictable exit codes: `0` for success,
  `1` for completed work with operational errors, and `2` for invalid usage or
  configuration when practical.
- Separate discovery, reading, planning/analysis, mutation, and reporting. A
  report must not contain hidden mutation, and execution must not recalculate a
  plan after mutation begins.
- Inject filesystem readers, metadata readers, clocks, network functions, and
  provider callables where doing so makes behavior deterministic and testable.
- Maintain deterministic ordering in scans, plans, reports, provider selection,
  collision allocation, and tests.
- Catch narrow expected exceptions. Never hide programming errors behind a
  broad `except Exception` or bare `except`.
- Send user-facing results through the Rich reporting layer and diagnostics to
  stderr. Keep core functions independent of terminal rendering.
- Avoid global mutable state. Make rate limits, caches, and provider state
  explicit objects.
- Keep generated filenames cross-platform-safe, Unicode-aware, and within the
  filesystem byte limit.
- Avoid adding a framework, build system, or shared abstraction unless the
  change clearly requires it.

## CLI and user-experience requirements

- Every mutating utility must provide a safe preview mode. Preview output must
  be accurate and strictly read-only.
- Long operations must display the current file and meaningful stage rather
  than appearing frozen.
- End every run with a stable summary. When performance depends on local work,
  provider calls, or caching, report useful timing/cache/network metrics.
- Make safe behavior the default. Flags that overwrite or replace existing data
  must be explicit and difficult to invoke accidentally.
- Validate paths, numeric ranges, required executables, credentials, and
  timeouts before mutation begins.
- Quote every example path that may contain spaces.
- Keep `--help` accurate whenever behavior or flags change.

## Configuration and secrets

- Normalize and validate configuration once. CLI values override process
  environment variables, which override `.env`, which override defaults.
- Document every setting in `.env.example` without putting a real credential in
  the repository.
- Never log, cache, commit, or display API keys, access tokens, or other secrets.
- Cache keys must not contain secrets.
- Configuration errors should identify the setting without echoing its secret
  value.

## Filesystem safety invariants

These rules apply to every utility that moves, renames, deletes, or edits files:

- Never overwrite an existing file, directory, or symlink.
- Resolve all final paths before mutation. Recheck destinations immediately
  before writing or renaming to protect against changes after planning.
- Detect collisions case-insensitively and Unicode-normalization-insensitively,
  even on a case-sensitive development filesystem.
- Allocate deterministic collision suffixes such as ` [2]`, ` [3]`, and so on.
- Do not follow directory symlinks. Never remove a directory symlink as if it
  were an empty real directory.
- Remove only real directories proven empty after successful file operations.
- Treat cleanup failures as reportable errors, not permission to delete data.
- Use staged temporary names for rename swaps and cycles. Reject stale staging
  files from interrupted runs and provide enough information for recovery.
- If staging fails before finalization, restore already staged files to their
  original paths. Preserve recoverability after any later failure.
- Detect immutable/locked files and duplicate final paths before changing any
  files.
- Never verify mutation against a real music collection. Use
  `tempfile.TemporaryDirectory` or another disposable fixture.

### `flatten_music` invariants

- `build_plan` decides all final paths before `execute_plan` changes anything.
- Move every nested file, not only recognized audio files.
- Files without usable artist/title metadata may be flattened but retain their
  existing filename.
- Preserve staged rename protection against overwrite and rename cycles.
- `--dry-run` remains read-only and accurately describes execution.
- `--skip-rename` works without importing Mutagen.
- Extensions are lowercase and generated names remain portable.

### `find_duplicates` invariants

- Scan supported audio files only and match complete file contents by SHA-256.
- Group by size before hashing and report files that change during hashing.
- Do not follow directory or file symlinks.
- Include hard-linked paths in groups without counting them as independent
  recoverable copies.
- Default and ordinary interactive review modes never change audio files.
- Explicit `--review --auto-remove` previews a fixed plan and requires typed
  confirmation before permanent removal; `--dry-run` remains strictly read-only.
- `--menu` offers report, manual review, automatic preview, and automatic
  removal. The macOS menu uses a native confirmation before removal, with Cancel
  as the default; opening the menu or choosing a read-only option never mutates.
- Automatic removal keeps only a unique unsuffixed original among matching
  numbered copies. Skip ambiguous and hard-linked groups, revalidate the whole
  plan before mutation, and recheck the keeper before each deletion.
- Write a review manifest only when `--review --output` is explicitly supplied,
  and never overwrite an existing output path or symlink.

## Metadata and provider quality invariants

- Never replace an existing artist or title unless the user explicitly supplies
  the force and write flags required for that behavior.
- A filename-only candidate is never writable without corroboration.
- Preserve provider order and acceptance/conflict rules unless a requested
  behavior change includes corresponding tests and documentation.
- Rate-limit public services according to their published limits.
- Give every subprocess and network operation a configurable timeout.
- Provider failures must not prevent later fallback providers from being tried
  when continuing is safe.
- Cache raw provider responses or provider-neutral selections so current
  thresholds and conflict rules can still be applied. Do not cache credentials.
- Key file-derived cache entries so they invalidate when the source changes.
  Keep a refresh option for provider data.
- A cache failure must degrade to uncached recognition rather than aborting the
  analysis.
- Performance optimization must not weaken match confidence, conflict checks,
  or overwrite protection. Prefer caching and response reuse over skipping
  quality checks.

### `create_metadata` invariants

- Default runs inspect only files missing artist or title and remain read-only.
- `--write` writes accepted missing fields only.
- `--force` is still read-only by itself.
- Existing tags can be replaced only by `--force --write`.
- The narrow packed-title repair remains corroborated by the filename.
- Shazam remains a final fallback and cannot override a conflicting filename.
- Without filename agreement, AcoustID retains its exceptionally high safety
  threshold.
- A preview followed by a write should reuse valid cached recognition work.

## macOS launcher convention

User-facing utilities should document a short launcher in `~/.local/bin`. The
launcher uses the repository virtual environment and script's absolute path:

```bash
repo_path="$PWD"
printf '#!/bin/zsh\nexec %q %q "$@"\n' \
  "$repo_path/.venv/bin/python" \
  "$repo_path/subproject/subproject.py" \
  > "$HOME/.local/bin/command-name"
chmod +x "$HOME/.local/bin/command-name"
```

Documentation must state that the launcher needs to be recreated if the
repository or virtual environment moves.

## Finder workflow requirements

Every user-facing utility intended for normal macOS use should include a
version-controlled Finder Quick Action when the operation maps safely to a
selected folder.

### Bundle structure and portability

- Store workflows under `subproject/finder_workflows/*.workflow`.
- Commit only `Contents/Info.plist` and `Contents/document.wflow`. Do not commit
  generated Quick Look previews or thumbnails.
- Use a native **Run AppleScript** Automator action. Do not wrap AppleScript in
  a **Run Shell Script** action or depend on Automator's “Pass input” setting.
- Configure the Quick Action to receive folders in Finder.
- Resolve the current user's home dynamically and call the documented launcher
  under `~/.local/bin`; never embed a username, clone path, or virtual
  environment path in the workflow.
- Build Terminal commands with `quoted form of` for the launcher and selected
  path.
- Invoke the launcher explicitly through `/bin/zsh`, for example:

  ```applescript
  set shellCommand to "/bin/zsh " & (quoted form of commandPath) & " " & ¬
    (quoted form of selectedPath)
  ```

  This prevents the cold-start failure where Terminal opens but a directly
  invoked launcher does no visible work until the action is run a second time.
- Open Terminal for long-running actions so progress, results, prompts, and
  errors remain visible.

### Finder safety and naming

- Give related actions a common prefix and order normal analysis before
  mutation, for example **Music Metadata — 1 Analyze** and
  **Music Metadata — 2 Write Accepted**.
- Preview/analyze actions must be read-only.
- A write action must display a native confirmation dialog immediately before
  launching the mutating command and explain what will and will not be replaced.
- Do not ship a Finder shortcut for a destructive force-write operation. Keep
  that operation Terminal-only.
- For `flatten_music`, run `--dry-run` first and require explicit Terminal
  confirmation before executing the real command.
- For `create_metadata`, preserve these mappings:
  - Analyze → `cm FOLDER`
  - Write Accepted → `cm --write FOLDER`
  - Refresh Analysis → `cm --refresh-cache FOLDER`
  - Force Analyze → `cm --force FOLDER`
- For `find_duplicates`, **Music Duplicates — Review** maps to
  `music-dupes --menu FOLDER`. Report, review, and preview are read-only; removal
  requires explicit menu selection and native confirmation after the plan.

### Finder installer and documentation

Every `finder_workflows/` directory must contain:

- an executable `install.sh` that copies local `.workflow` bundles into
  `~/Library/Services` with `/usr/bin/ditto`;
- a `FINDER_SERVICES_DIR` override for testing without touching installed user
  workflows;
- a README listing each action, its exact behavior, prerequisites,
  installation, Finder enablement, first-run Terminal Automation permission,
  normal usage, safety boundaries, and update procedure.

The root and subproject READMEs must show the installer command and tell users
to rerun it after pulling workflow changes.

### Finder validation

- Run `plutil -lint` on every committed `Info.plist` and `document.wflow`.
- Smoke-test installers with `FINDER_SERVICES_DIR` pointing to a temporary
  directory. Do not overwrite installed workflows during automated tests.
- Add `plistlib` unit tests that verify the action type, launcher path,
  `/bin/zsh` invocation, exact flags, input file type, confirmation, and absence
  of `/Users/...` paths.
- Run `git diff --check`; Automator XML often contains trailing tabs that must be
  normalized before committing.

## Documentation requirements

Each subproject README must independently document:

- purpose and important safety guarantees;
- Python and external executable requirements;
- dependency and launcher installation;
- configuration and secret handling;
- preview and write/apply usage with quoted paths;
- every relevant flag and its mutation semantics;
- cache/provider behavior where applicable;
- Finder workflow installation, exact action behavior, expected review flow,
  enablement/permissions, updating, and troubleshooting;
- test commands.

The root README must provide a concise combined installation path, project
index, Finder action table, terminal examples, and cross-utility warnings such
as Rekordbox/Traktor path changes.

Update documentation in the same change as any flag, dependency, workflow,
filename rule, configuration setting, or other user-visible behavior.

## Testing expectations

Add or update tests for every behavior change. Prefer temporary directories,
small generated audio such as WAV silence, and injected fakes over checked-in
media fixtures or real provider calls.

Important coverage includes:

- recursive scans, root files, empty-directory cleanup, and directory symlinks;
- duplicate names, including case-only and Unicode-normalization collisions;
- invalid filename characters, reserved names, byte limits, and mix/version
  parsing;
- missing, malformed, unreadable, and changed metadata;
- rename swaps/cycles, locked files, stale staging files, rollback, and entries
  created after planning;
- provider ordering, rate limiting, timeouts, low confidence, ambiguity,
  conflicts, fallback, and filename disagreement;
- cache persistence, expiry/refresh, invalidation, failure, and avoidance of
  duplicate network or fingerprint work;
- preview/write reports, progress stages, summaries, metrics, and exit codes;
- Finder workflow portability, confirmation, exact flags, and installer output;
- optional dependencies and skip modes.

## Validation before handoff or commit

From the repository root:

```bash
.venv/bin/python -m unittest -v
git diff --check
```

Also run the affected CLI's `--help`. For filesystem-mutating behavior, use a
disposable temporary directory. Never run a non-preview command against a real
collection for development validation.

For workflow changes, lint every plist and smoke-test installers with temporary
destinations. Inspect the staged diff for secrets, absolute user paths,
generated files, executable-bit mistakes, and unrelated changes.

If dependencies or platform tools are unavailable, state exactly which checks
could not run. Never claim a check passed when it did not.

## Git and release hygiene

- Preserve unrelated work already present in the worktree.
- Do not commit `.env`, caches, virtual environments, Finder Quick Look images,
  real media, credentials, or machine-specific paths.
- Keep installer executable bits under source control.
- Use a commit message that describes the user-visible outcome.
- Commit or push only when explicitly requested. Before pushing, report the
  branch, destination, test result, and commit being exported.
