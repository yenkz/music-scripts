import tempfile
import unittest
from io import StringIO
from pathlib import Path

from rich.console import Console

from flatten_music import (
    Metadata,
    build_plan,
    collision_safe_name,
    execute_plan,
    music_filename,
    render_report,
    scan_tree,
)


class FakeAudio:
    def __init__(self, tags):
        self.tags = tags


class FakeFrame:
    def __init__(self, text):
        self.text = text


class FlattenMusicTests(unittest.TestCase):
    def test_music_filename_uses_standard_format_and_sanitizes(self):
        metadata = Metadata("DJ / Producer", "Night:Drive", "Original Mix")
        self.assertEqual(
            music_filename(metadata, ".MP3"),
            "DJ - Producer - Night-Drive (Original Mix).mp3",
        )

    def test_collision_suffix_is_case_insensitive(self):
        occupied = {"track.mp3"}
        self.assertEqual(collision_safe_name("Track.mp3", occupied), "Track [2].mp3")

    def test_unicode_normalization_does_not_cause_repeat_rename(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # The filename uses decomposed characters, as commonly stored by macOS.
            source = root / "Isole\u0301e - Pisco.mp3"
            source.write_bytes(b"audio")
            plans = build_plan(
                root,
                [source],
                lambda path, easy=True: FakeAudio(
                    {"artist": ["Isolée"], "title": ["Pisco"]}
                ),
                skip_rename=False,
            )

            self.assertEqual(plans[0].flattened, plans[0].final)

    def test_plan_reads_native_id3_frames_used_by_wav_and_aiff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "untagged.wav"
            source.write_bytes(b"audio")

            plans = build_plan(
                root,
                [source],
                lambda path, easy=True: FakeAudio(
                    {
                        "TPE1": FakeFrame(["Frame Artist"]),
                        "TIT2": FakeFrame(["Frame Title"]),
                    }
                ),
                skip_rename=False,
            )

            self.assertEqual(plans[0].final.name, "Frame Artist - Frame Title.wav")

    def test_plan_and_execute_flattens_renames_and_removes_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            nested = root / "Album" / "Disc 1"
            nested.mkdir(parents=True)
            audio = nested / "01 old name.mp3"
            audio.write_bytes(b"not real audio")
            note = root / "Album" / "notes.txt"
            note.write_text("notes")

            def fake_mutagen_file(path, easy=True):
                self.assertEqual(path, audio)
                self.assertTrue(easy)
                return FakeAudio(
                    {"artist": ["Example Artist"], "title": ["Signal (Club Mix)"]}
                )

            files, directories = scan_tree(root)
            plans = build_plan(root, files, fake_mutagen_file, skip_rename=False)
            result = execute_plan(root, plans, directories)

            self.assertTrue((root / "Example Artist - Signal (Club Mix).mp3").is_file())
            self.assertTrue((root / "notes.txt").is_file())
            self.assertFalse((root / "Album").exists())
            self.assertEqual(result.moved, 2)
            self.assertEqual(result.renamed, 1)
            self.assertEqual(len(result.removed_directories), 2)

    def test_report_leaves_summary_after_changes_and_review_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "old.mp3"
            source.write_bytes(b"audio")
            plan = build_plan(
                root,
                [source],
                lambda path, easy=True: FakeAudio(
                    {"artist": ["Artist"], "title": ["Track (Dub Mix)"]}
                ),
                skip_rename=False,
            )
            output = StringIO()
            console = Console(
                file=output,
                force_terminal=False,
                color_system=None,
                width=100,
            )

            render_report(
                console,
                root,
                plan,
                [],
                dry_run=True,
                rename_enabled=True,
            )

            report = output.getvalue()
            self.assertIn("SUMMARY", report)
            self.assertIn("BEFORE", report)
            self.assertIn("AFTER", report)
            self.assertIn("old.mp3", report)
            self.assertIn("Artist - Track (Dub Mix).mp3", report)
            self.assertGreater(report.index("SUMMARY"), report.index("AFTER"))
            self.assertGreater(report.index("SUMMARY"), report.index("Review the before"))


if __name__ == "__main__":
    unittest.main()
