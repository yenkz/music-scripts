from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mutagen.mp3 import BitrateMode
from rich.console import Console

from . import check_bitrate as cb


def fake_reader(source: cb.BinaryIO) -> cb.Bitrate:
    bps, mode = source.read().decode().split()
    return cb.Bitrate(int(bps), mode)


class BitrateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def track(self, name: str, bps: int = 192000, mode: str = "CBR") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{bps} {mode}")
        return path

    def plan(self, below: int = 320) -> cb.Plan:
        return cb.build_plan(self.root, below, reader=fake_reader)

    def run_cli(self, *flags: str, answer: str = "", reader: cb.Reader = fake_reader
                ) -> tuple[int, str, str, list[str]]:
        output, errors = io.StringIO(), io.StringIO()
        prompts: list[str] = []

        def respond(prompt: str) -> str:
            prompts.append(prompt)
            return answer

        code = cb.main([*flags, str(self.root)], reader=reader, input_func=respond,
                       console=Console(file=output, width=160),
                       error_console=Console(file=errors, width=160))
        return code, output.getvalue(), errors.getvalue(), prompts

    def test_recursive_mp3_only_threshold_modes_and_order(self) -> None:
        self.track("nested/z.MP3", 320000)
        self.track("A.mp3", 128000)
        self.track("b.mp3", 319999, "VBR")
        self.track("c.mp3", 192000, "ABR")
        self.track("ignore.flac", 128000)
        (self.root / "empty").mkdir()
        plan = self.plan()
        self.assertEqual([t.path.name for t in plan.tracks], ["A.mp3", "b.mp3", "c.mp3", "z.MP3"])
        self.assertEqual(len(plan.candidates), 3)
        self.assertEqual(len(self.plan(128).candidates), 0)
        self.assertTrue((self.root / "empty").is_dir())

    def test_report_orders_numeric_bitrates_lowest_last_with_filename_ties(self) -> None:
        self.track("a-high.mp3", 320000)
        self.track("z-low.mp3", 96000)
        self.track("c-estimated.mp3", 128000, "UNKNOWN")
        self.track("B-tie.mp3", 192000, "ABR")
        self.track("a-tie.mp3", 192000, "VBR")
        expected = ["a-high.mp3", "a-tie.mp3", "B-tie.mp3", "c-estimated.mp3", "z-low.mp3"]
        for flags in ((), ("--delete", "--dry-run"), ("--delete",)):
            with self.subTest(flags=flags):
                code, output, errors, _ = self.run_cli(*flags)
                self.assertEqual(code, 0)
                self.assertEqual(errors, "")
                positions = [output.index(name) for name in expected]
                self.assertEqual(positions, sorted(positions))
                self.assertLess(output.index("≥320 kbps"), output.index("a-high.mp3"))
                self.assertLess(output.index("a-high.mp3"), output.index("Below 320 kbps"))
                self.assertLess(output.index("Below 320 kbps"), output.index("a-tie.mp3"))
                self.assertTrue(all((self.root / name).exists() for name in expected))

    def test_report_colors_groups_at_320_even_with_custom_deletion_cutoff(self) -> None:
        self.track("high.mp3", 320000)
        self.track("middle.mp3", 192000)
        self.track("low.mp3", 96000)
        plan = self.plan(128)
        output = io.StringIO()
        cb.render_plan(Console(file=output, width=160, force_terminal=True,
                               color_system="standard"), plan)
        lines = output.getvalue().splitlines()
        for name, color in (("high.mp3", "32"), ("middle.mp3", "33"), ("low.mp3", "33")):
            line = next(line for line in lines if name in line)
            self.assertIn(f"\x1b[{color}m", line)
        middle = next(line for line in lines if "middle.mp3" in line)
        self.assertIn("KEEP", middle)
        self.assertEqual([track.path.name for track in plan.candidates], ["low.mp3"])

    def test_unknown_bitrate_and_hard_links_are_protected(self) -> None:
        self.track("unknown.mp3", mode="UNKNOWN")
        original = self.track("original.mp3")
        os.link(original, self.root / "linked.mp3")
        plan = self.plan()
        self.assertEqual(len(plan.tracks), 3)
        self.assertFalse(plan.candidates)
        self.assertTrue(all(t.decision.startswith("SKIP") for t in plan.tracks))

    def test_symlinks_are_not_followed(self) -> None:
        original = self.track("nested/real.mp3")
        (self.root / "file.mp3").symlink_to(original)
        (self.root / "directory").symlink_to(original.parent, target_is_directory=True)
        (self.root / "broken.mp3").symlink_to(self.root / "absent")
        plan = self.plan()
        self.assertEqual([t.path for t in plan.tracks], [original])
        self.assertEqual(plan.skipped_links, 3)

    def test_file_changed_during_read_is_reported(self) -> None:
        path = self.track("changing.mp3")

        def reader(source: cb.BinaryIO) -> cb.Bitrate:
            path.write_text("320000 CBR")
            return cb.Bitrate(192000, "CBR")

        plan = cb.build_plan(self.root, reader=reader)
        self.assertFalse(plan.tracks)
        self.assertIn("changed", plan.errors[0].detail)

    def test_malformed_and_unreadable_files_report_errors(self) -> None:
        self.track("bad.mp3").write_bytes(b"not audio")
        self.assertEqual(len(cb.build_plan(self.root).errors), 1)
        with patch.object(cb, "inspect_track", side_effect=PermissionError("denied")):
            self.assertIn("denied", self.plan().errors[0].detail)

    def test_nonregular_mp3_is_skipped(self) -> None:
        os.mkfifo(self.root / "pipe.mp3")
        self.assertFalse(self.plan().tracks)

    def test_default_and_delete_dry_run_never_prompt_or_mutate(self) -> None:
        path = self.track("[red] song.mp3")
        for flags in ((), ("--dry-run",), ("--delete", "--dry-run")):
            code, output, errors, prompts = self.run_cli(*flags, answer="DELETE")
            self.assertEqual(code, 0)
            self.assertIn("[red] song.mp3", output)
            self.assertIn("1 candidates", output)
            self.assertIn("0 deleted", output)
            self.assertEqual(errors, "")
            self.assertFalse(prompts)
            self.assertTrue(path.exists())

    def test_exact_confirmation_and_permanent_deletion(self) -> None:
        low = self.track("nested/low.mp3")
        high = self.track("high.mp3", 320000)
        for answer in ("", "yes", "delete", "DELETE "):
            code, output, _, prompts = self.run_cli("--delete", answer=answer)
            self.assertEqual(code, 0)
            self.assertIn("cancelled", output)
            self.assertEqual(len(prompts), 1)
            self.assertTrue(low.exists())
        code, output, errors, _ = self.run_cli("--delete", answer="DELETE")
        self.assertEqual(code, 0)
        self.assertIn("1 deleted", output)
        self.assertFalse(low.exists())
        self.assertTrue(high.exists())
        self.assertTrue(low.parent.is_dir())
        self.assertEqual(errors, "")

    def test_analysis_error_skips_bad_file_but_allows_confirmed_deletion(self) -> None:
        low = self.track("low.mp3")
        bad = self.track("bad.mp3")
        bad.write_text("garbage")
        code, output, errors, prompts = self.run_cli("--delete", answer="DELETE")
        self.assertEqual(code, 1)
        self.assertIn("deletion complete with analysis errors skipped", output)
        self.assertIn("1 deleted", output)
        self.assertIn("1 errors", output)
        self.assertIn("excluded from deletion", output)
        self.assertEqual(errors.count("bad.mp3"), 1)
        self.assertFalse(low.exists())
        self.assertEqual(bad.read_text(), "garbage")
        self.assertEqual(len(prompts), 1)

    def test_analysis_error_preserves_preview_and_cancellation(self) -> None:
        low = self.track("low.mp3")
        bad = self.track("bad.mp3")
        bad.write_text("garbage")
        for flags in ((), ("--delete", "--dry-run"), ("--delete",)):
            code, output, errors, prompts = self.run_cli(*flags)
            self.assertEqual(code, 1)
            self.assertIn("0 deleted", output)
            self.assertIn("bad.mp3", errors)
            self.assertEqual(len(prompts), int(flags == ("--delete",)))
            self.assertTrue(low.exists())
            self.assertEqual(bad.read_text(), "garbage")

    def test_analysis_error_is_reported_before_confirmation(self) -> None:
        self.track("low.mp3")
        self.track("bad.mp3").write_text("garbage")
        output = io.StringIO()
        console = Console(file=output, width=160)

        def respond(prompt: str) -> str:
            self.assertIn("bad.mp3", output.getvalue())
            self.assertIn("excluded from deletion", output.getvalue())
            return ""

        self.assertEqual(cb.main(["--delete", str(self.root)], reader=fake_reader,
                                 input_func=respond, console=console, error_console=console), 1)

    def test_no_candidates_and_empty_tree_do_not_prompt(self) -> None:
        for populated in (False, True):
            if populated:
                self.track("high.mp3", 320000)
            code, output, _, prompts = self.run_cli("--delete")
            self.assertEqual(code, 0)
            self.assertIn("0 candidates", output)
            self.assertFalse(prompts)

    def test_whole_plan_revalidation_prevents_partial_deletion(self) -> None:
        first = self.track("a.mp3")
        last = self.track("z.mp3")
        plan = self.plan()
        last.write_text("320000 CBR")
        result = cb.execute_plan(plan)
        self.assertFalse(result.removed)
        self.assertTrue(result.errors)
        self.assertTrue(first.exists())

    def test_replaced_file_and_parent_symlink_abort(self) -> None:
        path = self.track("nested/low.mp3")
        plan = self.plan()
        path.parent.rename(self.root / "moved")
        path.parent.symlink_to(self.root / "moved", target_is_directory=True)
        result = cb.execute_plan(plan)
        self.assertTrue(result.errors)
        self.assertTrue(path.exists())

    def test_changed_file_symlink_aborts(self) -> None:
        path = self.track("low.mp3")
        high = self.track("high.mp3", 320000)
        plan = self.plan()
        path.unlink()
        path.symlink_to(high)
        self.assertTrue(cb.execute_plan(plan).errors)
        self.assertTrue(high.exists())

    def test_new_files_are_not_added_to_fixed_plan(self) -> None:
        original = self.track("first.mp3")
        plan = self.plan()
        new = self.track("new.mp3")
        result = cb.execute_plan(plan)
        self.assertEqual(result.removed, (original,))
        self.assertTrue(new.exists())

    def test_new_hard_link_blocks_plan(self) -> None:
        path = self.track("low.mp3")
        plan = self.plan()
        os.link(path, self.root / "new.mp3")
        self.assertTrue(cb.execute_plan(plan).errors)
        self.assertTrue(path.exists())

    def test_locked_file_checked_before_any_deletion(self) -> None:
        self.track("low.mp3")
        plan = self.plan()
        actual_stat = os.stat

        def locked(*args: object, **kwargs: object) -> object:
            details = actual_stat(*args, **kwargs)
            return SimpleNamespace(**{name: getattr(details, name) for name in dir(details)
                                      if name.startswith("st_") and name != "st_flags"},
                                   st_flags=getattr(cb.stat, "UF_IMMUTABLE", 2))

        with patch.object(cb.os, "stat", side_effect=locked):
            result = cb.execute_plan(plan)
        self.assertFalse(result.removed)
        self.assertIn("locked", result.errors[0].detail)

    def test_deletion_rechecks_each_file_and_reports_partial_failure(self) -> None:
        first = self.track("a.mp3")
        last = self.track("z.mp3")
        plan = self.plan()

        def progress(stage: str, path: Path) -> None:
            if stage == "Deleting" and path == last:
                path.write_text("320000 CBR")

        result = cb.execute_plan(plan, progress=progress)
        self.assertEqual(result.removed, (first,))
        self.assertTrue(result.errors)
        self.assertTrue(last.exists())

    def test_duplicate_plan_rejected(self) -> None:
        path = self.track("low.mp3")
        plan = self.plan()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            cb.execute_plan(replace(plan, tracks=plan.tracks * 2))
        self.assertTrue(path.exists())

    def test_invalid_path_and_threshold(self) -> None:
        for value in ("0", "321", "nan"):
            with self.assertRaises(SystemExit) as context, patch("sys.stderr", io.StringIO()):
                cb.parse_args(["--below", value, str(self.root)])
            self.assertEqual(context.exception.code, 2)
        output = Console(file=io.StringIO())
        self.assertEqual(cb.main([str(self.root / "absent")], console=output,
                                 error_console=output), 2)

    def test_cbr_rounding_does_not_round_vbr_up(self) -> None:
        for mode, expected in ((BitrateMode.CBR, 320000), (BitrateMode.VBR, 319998),
                               (BitrateMode.ABR, 319998), (BitrateMode.UNKNOWN, 319998)):
            with patch.object(cb, "MPEGInfo", return_value=SimpleNamespace(
                    layer=3, sketchy=False, bitrate=319998, bitrate_mode=mode)):
                self.assertEqual(cb.read_bitrate(io.BytesIO()).bps, expected)

    def test_invalid_mpeg_information_is_rejected(self) -> None:
        for layer, sketchy, bps in ((2, False, 192000), (3, True, 192000),
                                   (3, False, 0), (3, False, float("nan"))):
            with patch.object(cb, "MPEGInfo", return_value=SimpleNamespace(
                    layer=layer, sketchy=sketchy, bitrate=bps, bitrate_mode=BitrateMode.CBR)):
                with self.assertRaises(ValueError):
                    cb.read_bitrate(io.BytesIO())

    def test_progress_includes_stages_and_paths(self) -> None:
        path = self.track("low.mp3")
        updates: list[tuple[str, Path]] = []
        cb.build_plan(self.root, reader=fake_reader,
                      progress=lambda stage, file: updates.append((stage, file)))
        self.assertIn(("Scanning", self.root), updates)
        self.assertIn(("Reading bitrate", path), updates)

    @unittest.skipUnless(shutil.which("ffmpeg"), "optional ffmpeg needed to generate MP3 fixtures")
    def test_generated_real_mp3s(self) -> None:
        for name, options in (("128.mp3", ["-b:a", "128k"]),
                              ("320.mp3", ["-b:a", "320k"]),
                              ("vbr.mp3", ["-q:a", "2"]),
                              ("unknown.mp3", ["-b:a", "128k", "-write_xing", "0"])):
            subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi",
                            "-i", "sine=frequency=1000:duration=0.5", "-c:a", "libmp3lame",
                            *options, str(self.root / name)], check=True, timeout=30,
                           capture_output=True)
        plan = cb.build_plan(self.root)
        self.assertFalse(plan.errors)
        tracks = {t.path.name: t for t in plan.tracks}
        self.assertEqual(tracks["320.mp3"].bitrate, cb.Bitrate(320000, "CBR"))
        self.assertEqual(tracks["320.mp3"].decision, "KEEP")
        self.assertEqual(tracks["128.mp3"].bitrate, cb.Bitrate(128000, "CBR"))
        self.assertEqual(tracks["vbr.mp3"].bitrate.mode, "VBR")
        self.assertEqual(tracks["unknown.mp3"].decision, "SKIP: estimated bitrate")
        # An actual MPEG parser failure must not block valid candidates.
        bad = self.root / "corrupt.mp3"
        bad.write_bytes(b"not an MPEG stream")
        plan = cb.build_plan(self.root)
        self.assertEqual(len(plan.errors), 1)
        self.assertIn("sync", plan.errors[0].detail)
        result = cb.execute_plan(plan)
        self.assertFalse(result.errors)
        self.assertEqual({path.name for path in result.removed}, {"128.mp3", "vbr.mp3"})
        self.assertEqual(bad.read_bytes(), b"not an MPEG stream")


if __name__ == "__main__":
    unittest.main()
