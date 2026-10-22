from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path

from .build_crates import (Track, allocate_path, bpm_range, build_plan, main,
                           playlist_text, read_tags, scan, write_plan)


class FakeAudio:
    def __init__(self, tags: dict[str, object]) -> None:
        self.tags = tags


class CrateTests(unittest.TestCase):
    def test_reads_common_raw_tags_and_invalid_bpm(self) -> None:
        values = {"TPE1": ["Artist"], "TIT2": ["Title"], "TPUB": ["Label"],
                  "TBPM": ["128"]}
        self.assertEqual(read_tags(Path("song.mp3"), lambda *_args, **_kw: FakeAudio(values)),
                         ("Artist", "Title", ("Label",), 128.0))
        values["TBPM"] = ["not a tempo"]
        self.assertIsNone(read_tags(Path("song.mp3"), lambda *_args, **_kw: FakeAudio(values))[3])

    def test_scan_skips_symlinks_and_reports_bad_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nested").mkdir()
            (root / "nested" / "good.mp3").write_bytes(b"audio")
            (root / "bad.flac").write_bytes(b"bad")
            (root / "link.mp3").symlink_to(root / "nested" / "good.mp3")
            (root / "linked_folder").symlink_to(root / "nested", target_is_directory=True)
            def reader(path: Path) -> tuple[str | None, str | None, tuple[str, ...], float | None]:
                if path.name == "bad.flac":
                    raise ValueError("bad tags")
                return "Artist", "Title", ("Label",), 120.0
            tracks, skipped, issues = scan(root, reader)
            self.assertEqual([t.path.name for t in tracks], ["good.mp3"])
            self.assertEqual(skipped, 2)
            self.assertEqual(len(issues), 1)

    def test_default_rules_boundaries_and_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "music"
            nested.mkdir()
            paths = [nested / name for name in ("a.mp3", "b.mp3", "c.mp3")]
            for path in paths:
                path.write_bytes(b"test")
            tracks = (
                Track(paths[0], "A", "One", ("Deep House",), 120, 1000),
                Track(paths[1], None, "Two", ("Deep House",), 139.9, 1),
                Track(paths[2], "C", "Three", (), 140, 1),
            )
            plan = build_plan(root, root / "Crates", tracks, (), now=1000,
                              recent_days=1, labels=(), all_labels=True,
                              bpm_ranges=(), bpm_buckets=20, needs_review=True)
            names = {c.name: c for c in plan.crates}
            self.assertEqual(len(names["Recent Additions - 1 Days"].tracks), 3)
            self.assertEqual(len(names["Label - Deep House"].tracks), 2)
            self.assertEqual(len(names["BPM - 120 to 140 exclusive"].tracks), 2)
            self.assertEqual(len(names["BPM - 140 to 160 exclusive"].tracks), 1)
            self.assertEqual(len(names["Needs Review - Missing Artist or Title"].tracks), 1)
            self.assertIn("../music/a.mp3\n", playlist_text(names["Label - Deep House"]))
            self.assertFalse((root / "Crates").exists())

    def test_collision_allocation_and_no_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / "Crates"
            out.mkdir()
            (out / "Label - Café.m3u8").write_text("preserve", encoding="utf-8")
            used = {"label - café.m3u8"}
            path = allocate_path(out, "Label - Café", used)
            self.assertEqual(path.name, "Label - Café [2].m3u8")
            track_path = root / "song.mp3"
            track_path.write_bytes(b"audio")
            crate_plan = build_plan(root, out, (Track(track_path, "A", "T", ("Café",), None, 0),),
                                    tuple(p.name for p in out.iterdir()), now=0,
                                    recent_days=None, labels=("Café",), all_labels=False,
                                    bpm_ranges=(), bpm_buckets=None, needs_review=False)
            written, errors = write_plan(crate_plan)
            self.assertEqual(errors, ())
            self.assertEqual(len(written), 1)
            self.assertEqual((out / "Label - Café.m3u8").read_text(), "preserve")
            self.assertEqual(written[0].read_text(), "#EXTM3U\n../song.mp3\n")
            written_again, errors_again = write_plan(crate_plan)
            self.assertEqual(written_again, ())
            self.assertEqual(len(errors_again), 1)

    def test_cli_preview_is_read_only_and_write_creates_playlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with wave.open(str(root / "track.wav"), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(b"\0" * 160)
            self.assertEqual(main(["--needs-review", str(root)]), 0)
            self.assertFalse((root / "Crates").exists())
            self.assertEqual(main(["--needs-review", "--write", str(root)]), 0)
            self.assertEqual(list((root / "Crates").glob("*.m3u8"))[0].read_text(),
                             "#EXTM3U\n../track.wav\n")
            self.assertEqual(main(["--needs-review", "--write", str(root)]), 0)
            self.assertEqual(len(list((root / "Crates").glob("*.m3u8"))), 2)

    def test_symlink_output_and_newline_path_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "elsewhere"
            target.mkdir()
            (root / "Crates").symlink_to(target, target_is_directory=True)
            self.assertEqual(main(["--needs-review", "--write", str(root)]), 2)
            self.assertEqual(list(target.iterdir()), [])
            (root / "bad\nname.mp3").write_bytes(b"audio")
            tracks, _, issues = scan(root, lambda _path: (None, None, (), None))
            self.assertEqual(tracks, ())
            self.assertEqual(len(issues), 1)

    def test_invalid_ranges_and_output_name(self) -> None:
        with self.assertRaises(Exception):
            bpm_range("130:120")
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(main(["--output-name", "../elsewhere", temporary]), 2)
            self.assertEqual(main(["--recent-days", "0", temporary]), 2)


if __name__ == "__main__":
    unittest.main()
