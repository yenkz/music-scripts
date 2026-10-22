# Music Crates Finder actions

Install the `music-crates` launcher described in the [utility README](../README.md),
then from the repository root run:

```bash
./build_crates/finder_workflows/install.sh
```

Control-click a music folder in Finder, choose **Quick Actions**, then choose:

| Action | Command | Behavior |
| --- | --- | --- |
| **Music Crates — 1 Preview** | `music-crates FOLDER` | Read-only plan showing playlists and track counts. |
| **Music Crates — 2 Build** | `music-crates --write FOLDER` | Native confirmation, then create new M3U8 playlists. Never changes audio or overwrites an existing playlist. |

Both actions use the default rules: recent 30 days, one crate per found label,
20 BPM buckets, and missing artist/title review. Each selected folder opens a
Terminal session for progress and the final summary. The build confirmation has
Cancel as the default. No `.env`, API key, network, or external audio executable
is needed.

If the actions are absent, enable them in **System Settings → Privacy & Security
→ Extensions → Finder**. On first use, allow the workflow to control Terminal.
If the launcher is missing, recreate it; if Python reports missing modules,
install this utility's requirements into `.venv`. Rerun the installer after
pulling workflow updates. For safe installer testing, set `FINDER_SERVICES_DIR`
to a temporary directory so installed services are not touched.
