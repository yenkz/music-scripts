import io
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rich.console import Console

from find_duplicates import find_duplicates as dupes


class AutomaticRemovalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.output = io.StringIO()
        self.console = Console(file=self.output, color_system=None, width=120)

    def files(self, *names, content=b"identical audio"):
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def decisions(self):
        scan = dupes.scan_audio_files(self.root)
        groups = dupes.find_duplicate_groups(self.root, scan.files).groups
        return dupes.automatic_review(groups)

    def test_examples_keep_unsuffixed_originals_regardless_of_sort_order(self):
        names = (
            "S-Ampel - Questions (Ruben F Remix)",
            "Sasse - 8A - 6 - Soul Sounds (Dirt Crew Solid Diamond Remix)",
            "Totally Enormous Extinct Dinosaurs - Garden",
            "Umo Detic - Fahrenheit (edited by mebo)",
            "Undo - Love What You Do (Original Mix)",
        )
        for index, name in enumerate(names):
            self.files(f"{name}.mp3", f"{name} [2].mp3", f"{name} [3].mp3",
                       content=f"track {index}".encode())
        decisions = self.decisions()
        self.assertEqual(len(decisions), 5)
        self.assertEqual({item.keeper.name for item in decisions},
                         {f"{name}.mp3" for name in names})

    def test_matching_handles_case_unicode_nested_folders_and_repeated_suffixes(self):
        self.files("original/Café.MP3", "copies/CAFE\u0301 [2] [3].mp3")
        decision, = self.decisions()
        self.assertEqual(decision.keeper, self.root / "original/Café.MP3")

    def test_ambiguous_or_different_names_are_skipped(self):
        pairs = (
            ("Song [2].mp3", "Song [3].mp3"),
            ("Original.mp3", "Unrelated [2].mp3"),
            ("Mix.mp3", "Mix (2).mp3"),
            ("Number.mp3", "Number [1].mp3"),
            ("Format.mp3", "Format [2].wav"),
        )
        for index, pair in enumerate(pairs):
            self.files(*pair, content=f"audio {index}".encode())
        self.assertTrue(all(item.status == "skipped" for item in self.decisions()))

    def test_same_names_keep_shallowest_copy_including_root_and_numbered_names(self):
        names = (
            "(BacauHouseMafia.Ro) - Nadja Lind - Limbus (Hernan Cattaneo & Soundexile Remix).mp3",
            "014. Robosonic - The Edge (Original Mix).mp3",
            "Track [2].mp3",
        )
        for index, name in enumerate(names):
            self.files(f"2012/{name}", f"2013/Maure/{name}",
                       content=f"audio {index}".encode())
        self.files("Root.mp3", "nested/Root.mp3", content=b"root audio")
        decisions = self.decisions()
        self.assertEqual({item.keeper for item in decisions},
                         {self.root / "2012" / name for name in names}
                         | {self.root / "Root.mp3"})

    def test_depth_wins_over_path_sort_order_and_normalizes_names(self):
        self.files("a/deep/CAFE\u0301.MP3", "z/Café.mp3")
        decision, = self.decisions()
        self.assertEqual(decision.keeper, self.root / "z/Café.mp3")

    def test_mixed_copies_prefer_shallowest_unsuffixed_original(self):
        self.files("Track [2].mp3", "a/deep/Track.mp3", "z/Track.mp3")
        decision, = self.decisions()
        self.assertEqual(decision.keeper, self.root / "z/Track.mp3")

    def test_equal_depth_ties_choose_once_and_execution_uses_displayed_keeper(self):
        self.files("a/Track.mp3", "b/Track.mp3", "c/deep/Track.mp3")
        scan = dupes.scan_audio_files(self.root)
        groups = dupes.find_duplicate_groups(self.root, scan.files).groups
        candidates = [self.root / "a/Track.mp3", self.root / "b/Track.mp3"]
        # Either shallow copy can be kept, but a deeper copy cannot win the tie.
        for keeper in candidates:
            chooser = mock.Mock(return_value=keeper)
            decision, = dupes.automatic_review(groups, choose_keeper=chooser)
            chooser.assert_called_once_with(candidates)
            self.assertEqual(decision.keeper, keeper)
        with mock.patch.object(dupes.random, "choice", return_value=candidates[1]) as choose:
            decisions = dupes.automatic_review(groups)
            self.assertEqual(dupes.run_automatic_removal(
                self.console, self.console, self.root, decisions, False,
                lambda _: "DELETE",
            ), 0)
            choose.assert_called_once_with(candidates)
        self.assertEqual(list(self.root.rglob("*.mp3")), [candidates[1]])
        self.assertIn("KEEP   b/Track.mp3", self.output.getvalue())
        self.assertIn("2 removed", self.output.getvalue())

    def test_same_name_preview_and_cancel_leave_all_files_unchanged(self):
        self.files("2012/Track.mp3", "2013/Maure/Track.mp3")
        decisions = self.decisions()
        before = {path: path.read_bytes() for path in self.root.rglob("*.mp3")}
        for dry_run in (True, False):
            self.assertEqual(dupes.run_automatic_removal(
                self.console, self.console, self.root, decisions, dry_run,
                lambda _: "cancel",
            ), 0)
            self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*.mp3")})
        self.assertIn("KEEP   2012/Track.mp3", self.output.getvalue())
        self.assertIn("REMOVE 2013/Maure/Track.mp3", self.output.getvalue())

    def test_same_name_changed_keeper_blocks_removal(self):
        self.files("Track.mp3", "nested/Track.mp3")
        decisions = self.decisions()
        (self.root / "Track.mp3").write_bytes(b"changed")
        result = dupes.execute_removal(self.root, decisions)
        self.assertTrue(result.errors)
        self.assertEqual(result.removed, ())
        self.assertTrue((self.root / "nested/Track.mp3").exists())

    def test_same_names_with_different_bytes_do_not_match(self):
        self.files("Track.mp3", content=b"aaaa")
        self.files("nested/Track.mp3", content=b"bbbb")
        self.assertEqual(self.decisions(), ())

    def test_same_name_hard_link_group_is_skipped(self):
        self.files("Track.mp3", "nested/Track.mp3")
        (self.root / "nested/Track.mp3").unlink()
        os.link(self.root / "Track.mp3", self.root / "nested/Track.mp3")
        decision, = self.decisions()
        self.assertEqual(decision.status, "skipped")

    def test_only_byte_identical_files_are_selected(self):
        self.files("Track.mp3", content=b"aaaa")
        self.files("Track [2].mp3", content=b"bbbb")
        self.assertEqual(self.decisions(), ())

    def test_hard_link_group_is_skipped(self):
        self.files("Track.mp3")
        os.link(self.root / "Track.mp3", self.root / "Track [2].mp3")
        decision, = self.decisions()
        self.assertEqual(decision.status, "skipped")

    def test_preview_is_read_only_and_shows_literal_filenames(self):
        self.files("[bold]Track.mp3", "[bold]Track [2].mp3")
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        result = dupes.run_automatic_removal(
            self.console, self.console, self.root, self.decisions(), True,
            input_func=lambda _: self.fail("preview prompted"),
        )
        self.assertEqual(result, 0)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.root.iterdir()})
        self.assertIn("KEEP   [bold]Track.mp3", self.output.getvalue())
        self.assertIn("REMOVE [bold]Track [2].mp3", self.output.getvalue())

    def test_cancel_eof_and_interrupt_remove_nothing(self):
        self.files("Track.mp3", "Track [2].mp3")
        for answer in ("", "yes", "delete", EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=answer):
                prompt = mock.Mock(side_effect=answer) if isinstance(answer, BaseException) else mock.Mock(return_value=answer)
                self.assertEqual(dupes.run_automatic_removal(
                    self.console, self.console, self.root, self.decisions(), False, prompt,
                ), 0)
                self.assertTrue((self.root / "Track [2].mp3").exists())

    def test_confirmed_removal_keeps_original_and_skips_ambiguous_groups(self):
        self.files("Track.mp3", "nested/Track [2].mp3", "Track [3].mp3")
        self.files("Other.mp3", "Different.mp3", content=b"other audio")
        self.assertEqual(dupes.run_automatic_removal(
            self.console, self.console, self.root, self.decisions(), False,
            lambda _: "DELETE",
        ), 0)
        self.assertEqual((self.root / "Track.mp3").read_bytes(), b"identical audio")
        self.assertFalse((self.root / "nested/Track [2].mp3").exists())
        self.assertFalse((self.root / "Track [3].mp3").exists())
        self.assertTrue((self.root / "Other.mp3").exists())
        self.assertTrue((self.root / "nested").is_dir())
        self.assertIn("2 removed", self.output.getvalue())

    def test_changed_keeper_or_duplicate_prevents_every_removal(self):
        for changed in ("Track.mp3", "Track [2].mp3"):
            with self.subTest(changed=changed):
                self.files("Track.mp3", "Track [2].mp3", "Track [3].mp3")
                decisions = self.decisions()
                (self.root / changed).write_bytes(b"changed bytes")
                result = dupes.execute_removal(self.root, decisions)
                self.assertTrue(result.errors)
                self.assertEqual(result.removed, ())
                self.assertEqual(len(tuple(self.root.iterdir())), 3)

    def test_wrong_digest_prevents_removal(self):
        self.files("Track.mp3", "Track [2].mp3")
        decision, = self.decisions()
        bad = replace(decision, group=replace(decision.group, sha256="0" * 64))
        result = dupes.execute_removal(self.root, (bad,))
        self.assertEqual(result.removed, ())
        self.assertTrue(result.errors)

    def test_symlink_replacement_is_never_followed(self):
        self.files("Track.mp3", "Track [2].mp3")
        decisions = self.decisions()
        duplicate = self.root / "Track [2].mp3"
        duplicate.unlink()
        duplicate.symlink_to(self.root / "Track.mp3")
        result = dupes.execute_removal(self.root, decisions)
        self.assertTrue(result.errors)
        self.assertTrue(duplicate.is_symlink())
        self.assertTrue((self.root / "Track.mp3").exists())

    def test_directory_replaced_with_symlink_is_rejected(self):
        self.files("Track.mp3", "nested/Track [2].mp3")
        decisions = self.decisions()
        (self.root / "nested").rename(self.root / "moved")
        (self.root / "nested").symlink_to(self.root / "moved", target_is_directory=True)
        result = dupes.execute_removal(self.root, decisions)
        self.assertTrue(result.errors)
        self.assertEqual(result.removed, ())
        self.assertTrue((self.root / "moved/Track [2].mp3").exists())

    def test_new_hard_link_after_planning_blocks_removal(self):
        self.files("Track.mp3", "Track [2].mp3")
        decisions = self.decisions()
        os.link(self.root / "Track.mp3", self.root / "external-link.mp3")
        result = dupes.execute_removal(self.root, decisions)
        self.assertTrue(result.errors)
        self.assertEqual(result.removed, ())

    def test_locked_file_blocks_entire_plan(self):
        self.files("Track.mp3", "Track [2].mp3")
        decisions = self.decisions()
        real_stat = os.stat

        def locked_stat(*args, **kwargs):
            details = real_stat(*args, **kwargs)
            if args[0] == "Track [2].mp3":
                fields = {name: getattr(details, name) for name in dir(details) if name.startswith("st_")}
                fields["st_flags"] = stat.UF_IMMUTABLE
                return SimpleNamespace(**fields)
            return details

        with mock.patch.object(dupes, "removal_supported", return_value=True), \
                mock.patch.object(dupes.os, "stat", side_effect=locked_stat):
            result = dupes.execute_removal(self.root, decisions)
        self.assertEqual(result.removed, ())
        self.assertIn("locked", result.errors[0].detail)

    def test_keeper_change_immediately_before_removal_stops_execution(self):
        self.files("Track.mp3", "Track [2].mp3")
        decisions = self.decisions()

        def progress(stage, path):
            if stage == "Removing":
                (self.root / "Track.mp3").unlink()

        result = dupes.execute_removal(self.root, decisions, progress)
        self.assertEqual(result.removed, ())
        self.assertTrue(result.errors)
        self.assertTrue((self.root / "Track [2].mp3").exists())

    def test_partial_failure_reports_removals_and_retains_keeper(self):
        self.files("Track.mp3", "Track [2].mp3", "Track [3].mp3")
        decisions = self.decisions()
        real_unlink = os.unlink

        def failing_unlink(path, **kwargs):
            if path == "Track [3].mp3":
                raise PermissionError("test failure")
            real_unlink(path, **kwargs)

        with mock.patch.object(dupes, "removal_supported", return_value=True), \
                mock.patch.object(dupes.os, "unlink", side_effect=failing_unlink):
            result = dupes.execute_removal(self.root, decisions)
        self.assertEqual(result.removed, (self.root / "Track [2].mp3",))
        self.assertTrue(result.errors)
        self.assertTrue((self.root / "Track.mp3").exists())
        self.assertTrue((self.root / "Track [3].mp3").exists())

    def test_duplicate_plan_entries_are_rejected(self):
        self.files("Track.mp3", "Track [2].mp3")
        decision, = self.decisions()
        with self.assertRaises(ValueError):
            dupes.execute_removal(self.root, (decision, decision))
        self.assertEqual(len(tuple(self.root.iterdir())), 2)

    def test_cli_validation_and_noninteractive_preview(self):
        self.files("Track.mp3", "Track [2].mp3")
        with mock.patch("sys.stdin.isatty", return_value=False):
            for flags in (("--auto-remove",), ("--dry-run",),
                          ("--review", "--auto-remove"),
                          ("--review", "--auto-remove", "--dry-run", "--output", str(self.root / "new.json"))):
                self.assertEqual(dupes.main([*flags, str(self.root)]), 2)
            self.assertEqual(dupes.main([
                "--review", "--auto-remove", "--dry-run", str(self.root),
            ]), 0)
        self.assertEqual(len(tuple(self.root.iterdir())), 2)

    def test_scan_errors_block_automatic_removal(self):
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch.object(dupes, "scan_audio_files", return_value=dupes.ScanResult(
                    (), (), (dupes.ScanError(self.root, "unreadable file"),),
                )), mock.patch.object(dupes, "run_automatic_removal") as run:
            self.assertEqual(dupes.main(["--review", "--auto-remove", str(self.root)]), 1)
            run.assert_not_called()

    def test_cli_confirmed_removal_uses_automatic_choices_without_group_prompts(self):
        self.files("Track.mp3", "Track [2].mp3", "Track [3].mp3")
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value="DELETE") as prompt:
            self.assertEqual(dupes.main([
                "--review", "--auto-remove", str(self.root),
            ]), 0)
        prompt.assert_called_once()
        self.assertEqual([path.name for path in self.root.iterdir()], ["Track.mp3"])

    def test_interrupt_during_execution_reports_completed_removals(self):
        self.files("Track.mp3", "Track [2].mp3", "Track [3].mp3")

        def progress(stage, path):
            if stage == "Removing" and path.name == "Track [3].mp3":
                raise KeyboardInterrupt

        result = dupes.execute_removal(self.root, self.decisions(), progress)
        self.assertEqual(result.removed, (self.root / "Track [2].mp3",))
        self.assertIn("interrupted", result.errors[0].detail)
        self.assertTrue((self.root / "Track.mp3").exists())
        self.assertTrue((self.root / "Track [3].mp3").exists())


if __name__ == "__main__":
    unittest.main()
