import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rich.console import Console

from find_duplicates import find_duplicates as dupes


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.console = Console(file=io.StringIO())
        for name in ("Track.mp3", "Track [2].mp3"):
            (self.root / name).write_bytes(b"matching audio")

    def run_menu(self, answer):
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value=answer):
            return dupes.main(["--menu", str(self.root)])

    def test_selector_modes_and_safe_default(self):
        for answer, expected in (("1", "report"), ("2", "review"),
                                 ("3", "preview"), ("4", "remove"),
                                 ("", "report"), (" Q ", None)):
            self.assertEqual(dupes.choose_mode(
                self.console, self.root, lambda _: answer,
            ), expected)
        answers = iter(("DELETE", "9", "3"))
        self.assertEqual(dupes.choose_mode(
            self.console, self.root, lambda _: next(answers),
        ), "preview")

    def test_quit_eof_interrupt_never_scan(self):
        for answer in ("q", EOFError(), KeyboardInterrupt()):
            with mock.patch("sys.stdin.isatty", return_value=True), \
                    mock.patch("builtins.input", side_effect=answer if isinstance(answer, BaseException) else [answer]), \
                    mock.patch.object(dupes, "scan_audio_files") as scan:
                self.assertEqual(dupes.main(["--menu", str(self.root)]), 0)
                scan.assert_not_called()

    def test_report_and_preview_never_mutate_or_confirm(self):
        for answer in ("1", "3"):
            with mock.patch.object(dupes, "execute_removal") as execute, \
                    mock.patch.object(dupes, "confirm_macos_removal") as confirm:
                self.assertEqual(self.run_menu(answer), 0)
                execute.assert_not_called()
                confirm.assert_not_called()
                self.assertTrue((self.root / "Track [2].mp3").exists())

    def test_review_mode_uses_existing_read_only_review(self):
        with mock.patch.object(dupes, "review_groups", return_value=()) as review:
            self.assertEqual(self.run_menu("2"), 0)
            review.assert_called_once()
        self.assertTrue((self.root / "Track [2].mp3").exists())

    def test_macos_removal_requires_native_confirmation(self):
        for allowed in (False, True):
            with mock.patch.object(dupes.sys, "platform", "darwin"), \
                    mock.patch.object(dupes, "confirm_macos_removal", return_value=allowed) as confirm:
                self.assertEqual(self.run_menu("4"), 0)
                confirm.assert_called_once_with(self.root, 1, 120)
            self.assertEqual((self.root / "Track [2].mp3").exists(), not allowed)
            self.assertEqual((self.root / "Track.mp3").read_bytes(), b"matching audio")

    def test_confirmation_failure_never_removes(self):
        with mock.patch.object(dupes.sys, "platform", "darwin"), \
                mock.patch.object(dupes, "confirm_macos_removal", side_effect=OSError("failed")):
            self.assertEqual(self.run_menu("4"), 1)
        self.assertTrue((self.root / "Track [2].mp3").exists())

    def test_menu_requires_tty_and_rejects_mode_flags_and_invalid_timeout(self):
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(dupes.main(["--menu", str(self.root)]), 2)
        with mock.patch("sys.stdin.isatty", return_value=True):
            for flags in (("--review",), ("--auto-remove",), ("--dry-run",),
                          ("--output", "choices.json"), ("--dialog-timeout", "0"),
                          ("--dialog-timeout", "3601")):
                self.assertEqual(dupes.main(["--menu", *flags, str(self.root)]), 2)

    def test_native_dialog_uses_safe_arguments_timeout_and_cancel_default(self):
        unusual = self.root / 'Music "quoted" $(echo nope)'
        with mock.patch.object(dupes.subprocess, "run", return_value=mock.Mock(stdout="Remove Duplicates\n")) as run:
            self.assertTrue(dupes.confirm_macos_removal(unusual, 7, 45))
        args, kwargs = run.call_args
        self.assertEqual(args[0][-3:], [str(unusual), "7", "45"])
        self.assertEqual(kwargs["timeout"], 50)
        self.assertNotIn(str(unusual), dupes.REMOVAL_DIALOG_SCRIPT)
        self.assertIn('default button "Cancel"', dupes.REMOVAL_DIALOG_SCRIPT)
        self.assertIn("Original keepers and skipped groups will remain unchanged", dupes.REMOVAL_DIALOG_SCRIPT)
        self.assertIn("giving up after waitSeconds", dupes.REMOVAL_DIALOG_SCRIPT)
        for output in ("cancel\n", "", "unexpected"):
            with mock.patch.object(dupes.subprocess, "run", return_value=mock.Mock(stdout=output)):
                self.assertFalse(dupes.confirm_macos_removal(self.root, 1))
        for error in (subprocess.TimeoutExpired("osascript", 125),
                      subprocess.CalledProcessError(1, "osascript")):
            with mock.patch.object(dupes.subprocess, "run", side_effect=error):
                with self.assertRaises(OSError):
                    dupes.confirm_macos_removal(self.root, 1)


if __name__ == "__main__":
    unittest.main()
