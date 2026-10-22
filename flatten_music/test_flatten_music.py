import os
import stat
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

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
from flatten_music.flatten_music import FilePlan, main, parse_args


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

    def test_plan_reserves_a_later_file_that_already_has_its_final_name(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            incoming = root / "01 incoming.mp3"
            stationary = root / "Target - Song.mp3"
            incoming.write_bytes(b"incoming")
            stationary.write_bytes(b"stationary")
            files = [incoming, stationary]

            plans = build_plan(
                root,
                files,
                lambda path, easy=True: FakeAudio(
                    {"artist": ["Target"], "title": ["Song"]}
                ),
                skip_rename=False,
            )

            by_source = {plan.source: plan for plan in plans}
            self.assertEqual(by_source[incoming].final.name, "Target - Song [2].mp3")
            self.assertEqual(by_source[stationary].final, stationary)

            execute_plan(root, plans, [])
            self.assertEqual((root / "Target - Song.mp3").read_bytes(), b"stationary")
            self.assertEqual(
                (root / "Target - Song [2].mp3").read_bytes(), b"incoming"
            )

    def test_plan_preserves_existing_collision_suffixes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "Artist - Track.mp3"
            second = root / "Artist - Track [2].mp3"
            incoming = root / "01 incoming.mp3"
            for path in (first, second, incoming):
                path.write_bytes(b"audio")

            plans = build_plan(
                root,
                [incoming, first, second],
                lambda path, easy=True: FakeAudio(
                    {"artist": ["Artist"], "title": ["Track"]}
                ),
                skip_rename=False,
            )

            by_source = {plan.source: plan for plan in plans}
            self.assertEqual(by_source[first].final, first)
            self.assertEqual(by_source[second].final, second)
            self.assertEqual(by_source[incoming].final.name, "Artist - Track [3].mp3")

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

    def test_execute_rolls_back_staged_renames_after_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "01 first.mp3"
            second = root / "02 second.mp3"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            plans = build_plan(
                root,
                [first, second],
                lambda path, easy=True: FakeAudio(
                    {
                        "artist": ["Artist"],
                        "title": ["First" if path == first else "Second"],
                    }
                ),
                skip_rename=False,
            )
            real_rename = Path.rename
            staging_calls = 0

            def fail_second_stage(source, target):
                nonlocal staging_calls
                if source.suffix == ".mp3" and Path(target).suffix == ".tmp":
                    staging_calls += 1
                    if staging_calls == 2:
                        raise PermissionError("simulated locked file")
                return real_rename(source, target)

            with patch.object(Path, "rename", fail_second_stage):
                with self.assertRaises(PermissionError):
                    execute_plan(root, plans, [])

            self.assertEqual(first.read_bytes(), b"first")
            self.assertEqual(second.read_bytes(), b"second")
            self.assertEqual(list(root.glob(".flatten-music-*.tmp")), [])

    def test_execute_rejects_staging_file_from_interrupted_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stale = root / ".flatten-music-0123456789abcdef0123456789abcdef.tmp"
            stale.write_bytes(b"audio")
            plans = build_plan(root, [stale], None, skip_rename=True)

            with self.assertRaisesRegex(FileExistsError, "requires recovery"):
                execute_plan(root, plans, [])

            self.assertEqual(stale.read_bytes(), b"audio")

    def test_execute_rejects_duplicate_final_paths_before_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.mp3"
            second = root / "second.mp3"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            shared_final = root / "same.mp3"
            plans = [
                FilePlan(first, first, shared_final, Metadata("Artist", "Song")),
                FilePlan(second, second, shared_final, Metadata("Artist", "Song")),
            ]

            with self.assertRaisesRegex(FileExistsError, "duplicate final paths"):
                execute_plan(root, plans, [])

            self.assertEqual(first.read_bytes(), b"first")
            self.assertEqual(second.read_bytes(), b"second")
            self.assertFalse(shared_final.exists())

    @unittest.skipUnless(
        hasattr(os, "chflags") and hasattr(stat, "UF_IMMUTABLE"),
        "needs user immutable flags",
    )
    def test_execute_rejects_locked_file_before_any_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "old.mp3"
            source.write_bytes(b"audio")
            plans = build_plan(
                root,
                [source],
                lambda path, easy=True: FakeAudio(
                    {"artist": ["Artist"], "title": ["Track"]}
                ),
                skip_rename=False,
            )
            immutable = stat.UF_IMMUTABLE
            try:
                os.chflags(source, immutable)
                with self.assertRaisesRegex(PermissionError, "locked file"):
                    execute_plan(root, plans, [])
                self.assertTrue(source.exists())
                self.assertEqual(list(root.glob(".flatten-music-*.tmp")), [])
            finally:
                os.chflags(source, 0)

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


class CliSafetyTests(unittest.TestCase):
    def run_cli(self, arguments, answer="", effect=None):
        output = StringIO()
        errors = StringIO()
        with patch("sys.stdout", output), patch("sys.stderr", errors), patch.object(
            Console, "input", side_effect=effect, return_value=answer
        ) as prompt:
            code = main(arguments)
        return code, output.getvalue() + errors.getvalue(), prompt.call_count

    def test_explicit_directory_required(self):
        with patch("sys.stderr", StringIO()), self.assertRaises(SystemExit) as raised:
            parse_args([])
        self.assertEqual(raised.exception.code, 2)

    def test_no_audio_refuses_even_apply_and_skip_rename(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            nested = root / "project"
            nested.mkdir()
            source = nested / "important.txt"
            source.write_text("keep")
            code, output, prompts = self.run_cli(["--apply", "--skip-rename", temp])
            self.assertEqual(code, 2)
            self.assertIn("no supported audio", output)
            self.assertIn("SUMMARY", output)
            self.assertEqual(prompts, 0)
            self.assertEqual(source.read_text(), "keep")

    def test_project_with_audio_is_refused(self):
        for marker in (".git", "package.json", "pyproject.toml"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / marker).mkdir() if marker == ".git" else (root / marker).touch()
                (root / "sound.wav").write_bytes(b"audio")
                code, output, prompts = self.run_cli(["--apply", "--skip-rename", temp])
                self.assertEqual(code, 2)
                self.assertIn("software-project", output)
                self.assertEqual(prompts, 0)
                self.assertTrue((root / marker).exists())

    def test_default_preview_and_dry_run_never_mutate_or_prompt(self):
        for flags in ([], ["--dry-run"], ["--apply", "--dry-run"]):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as temp:
                nested = Path(temp) / "album"
                nested.mkdir()
                (nested / "track.wav").write_bytes(b"audio")
                (nested / "cover.jpg").write_bytes(b"art")
                code, output, prompts = self.run_cli([*flags, "--skip-rename", temp])
                self.assertEqual(code, 0)
                self.assertEqual(prompts, 0)
                self.assertIn("non-audio", output)
                self.assertTrue((nested / "track.wav").exists())
                self.assertTrue((nested / "cover.jpg").exists())

    def test_apply_requires_exact_confirmation_and_handles_closed_input(self):
        for answer, effect in (("", None), ("y", None), ("flatten", None),
                               ("FLATTEN", None), ("", EOFError), ("", KeyboardInterrupt)):
            with self.subTest(answer=answer, effect=effect), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                nested = root / "album"
                nested.mkdir()
                (nested / "track.wav").write_bytes(b"audio")
                code, output, prompts = self.run_cli(
                    ["--apply", "--skip-rename", temp], answer, effect
                )
                self.assertEqual(code, 0)
                self.assertEqual(prompts, 1)
                self.assertEqual((root / "track.wav").exists(), answer == "FLATTEN")
                self.assertEqual(nested.exists(), answer != "FLATTEN")

    def test_change_during_confirmation_aborts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            nested = root / "album"
            nested.mkdir()
            source = nested / "track.wav"
            source.write_bytes(b"audio")
            def change(*args):
                (nested / "new.txt").write_text("new")
                return "FLATTEN"
            code, output, _ = self.run_cli(["--apply", "--skip-rename", temp], effect=change)
            self.assertEqual(code, 1)
            self.assertIn("folder changed", output)
            self.assertTrue(source.exists())

    def test_filesystem_root_and_home_refused_before_scan(self):
        with patch("flatten_music.flatten_music.scan_tree") as scan:
            for target in ("/", str(Path.home())):
                code, _, prompts = self.run_cli(["--apply", target])
                self.assertEqual(code, 2)
                self.assertEqual(prompts, 0)
            scan.assert_not_called()

    def test_audio_symlink_does_not_make_non_music_folder_eligible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "notes.txt").write_text("notes")
            (root / "track.wav").symlink_to(root / "notes.txt")
            code, _, prompts = self.run_cli(["--apply", "--skip-rename", temp])
            self.assertEqual(code, 2)
            self.assertEqual(prompts, 0)

    def test_destination_created_after_plan_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            nested = root / "album"
            nested.mkdir()
            source = nested / "track.wav"
            source.write_bytes(b"audio")
            files, directories = scan_tree(root)
            plans = build_plan(root, files, None, skip_rename=True)
            destination = root / "track.wav"
            destination.write_bytes(b"new")
            with self.assertRaises(FileExistsError):
                execute_plan(root, plans, directories)
            self.assertEqual(source.read_bytes(), b"audio")
            self.assertEqual(destination.read_bytes(), b"new")


if __name__ == "__main__":
    unittest.main()
