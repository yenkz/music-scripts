import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest import mock

from rich.console import Console

from find_duplicates import find_duplicates as dupes
from find_duplicates.audio_match import AudioSignature, decoded_signature, find_name_pairs


SIGNATURE = AudioSignature(44100, 1, "mono", "a" * 64)


class NameTests(unittest.TestCase):
    def test_real_naming_patterns_and_cross_format_candidates(self):
        pairs = (
            ("083. Elfenberg - Gaivota (Original Mix)-1.mp3", "Elfenberg - Gaivota (Original Mix).flac"),
            ("17 Unknown Artist - Amine K - Arnold's Rav-1.mp3", "Amine K - Arnold's Rave (Original Mix).mp3"),
            ("48 Unknown Artist - Klunsh - Anyway (Origi-3.mp3", "10 Unknown Artist - Klunsh - Anyway (Origina.mp3"),
            ("Café - Title [2].mp3", "CAFE\u0301 - Title.mp3"),
        )
        for left, right in pairs:
            with self.subTest(left=left):
                result = find_name_pairs([Path(left), Path(right)])
                self.assertEqual(len(result), 1)

    def test_four_copies_generate_all_six_pairs_deterministically(self):
        paths = [Path(f"Artist - Title{suffix}.mp3") for suffix in ("", " [2]", " [3]", " [4]")]
        self.assertEqual(len(find_name_pairs(paths)), 6)
        self.assertEqual(find_name_pairs(paths), find_name_pairs(list(reversed(paths))))

    def test_unrelated_names_and_empty_names_are_not_candidates(self):
        self.assertEqual(find_name_pairs([Path("Original - Alpha.mp3"), Path("Someone - Other.mp3"), Path("---.wav")]), ())

    def test_threshold_applies_even_to_truncated_prefix(self):
        paths = [Path("An Artist - Quite a long title.mp3"), Path("An Artist - Quite a long title remix.mp3")]
        self.assertEqual(len(find_name_pairs(paths)), 1)
        self.assertEqual(find_name_pairs(paths, 1.0), ())


class DecoderTests(unittest.TestCase):
    def runner(self, digest="a" * 64):
        return mock.Mock(side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps({"streams": [{"sample_rate": "44100", "channels": 2}]})),
            subprocess.CompletedProcess([], 0, f"SHA256={digest}\n"),
        ])

    def test_preserves_precision_rate_channels_and_uses_timeout_and_safe_arguments(self):
        runner = self.runner()
        path = Path('/tmp/a "quoted" $(nope).wav')
        result = decoded_signature(path, "ffmpeg", "ffprobe", 17, runner)
        self.assertEqual(result, AudioSignature(44100, 2, "stereo", "a" * 64))
        for call in runner.call_args_list:
            self.assertIn(str(path), call.args[0])
            self.assertEqual(call.kwargs["timeout"], 17)
            self.assertTrue(call.kwargs["check"])
            self.assertNotIn("shell", call.kwargs)
        command = runner.call_args_list[1].args[0]
        self.assertIn("pcm_f64le", command)
        self.assertIn("-xerror", command)
        self.assertNotIn("-ar", command)
        self.assertNotIn("-ac", command)

    def test_timeouts_and_decoder_errors_fail_closed(self):
        for error in (subprocess.TimeoutExpired("ffmpeg", 2), subprocess.CalledProcessError(1, "ffmpeg")):
            with self.subTest(error=error), self.assertRaises(OSError):
                decoded_signature(Path("a.wav"), "ffmpeg", "ffprobe", 2, mock.Mock(side_effect=error))

    def test_empty_audio_and_malformed_or_multiple_streams_fail_closed(self):
        with self.assertRaises(OSError):
            decoded_signature(Path("a.wav"), "ffmpeg", "ffprobe", 2, self.runner(hashlib.sha256(b"").hexdigest()))
        for payload in ("not json", '{"streams": []}', '{"streams": [{}, {}]}', '{"streams": [{}]}'):
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, payload))
            with self.subTest(payload=payload), self.assertRaises(OSError):
                decoded_signature(Path("a.wav"), "ffmpeg", "ffprobe", 2, runner)


class SimilarTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.output = io.StringIO()
        self.console = Console(file=self.output, width=140, color_system=None)

    def files(self, names=("Artist - Title.mp3", "Artist - Title-1.mp3", "Artist - Title [3].mp3", "Artist - Title [4].mp3")):
        paths = []
        for index, name in enumerate(names):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio plus different metadata" + b"!" * index)
            paths.append(path)
        return paths

    def search(self, reader=None):
        return dupes.find_similar_groups(self.root, dupes.scan_audio_files(self.root).files, 0.86,
                                         reader or (lambda _: SIGNATURE))

    def test_four_different_whole_file_hashes_form_one_audio_group_and_one_report(self):
        paths = self.files()
        result = self.search()
        self.assertEqual(len(result.groups), 1)
        group, = result.groups
        self.assertEqual({record.path for record in group.files}, set(paths))
        self.assertEqual(len(set(dict(group.file_hashes).values())), 4)
        dupes.render_similar_report(self.console, self.root, dupes.scan_audio_files(self.root), result, 1.0)
        output = self.output.getvalue()
        self.assertIn("VERIFIED AUDIO GROUP 1 · 4 copies", output)
        self.assertNotIn("VERIFIED AUDIO GROUP 2", output)
        for path in paths:
            self.assertEqual(output.count(path.name), 1)

    def test_matching_names_with_different_audio_or_rates_or_channels_remain_separate(self):
        paths = self.files()
        signatures = [SIGNATURE, replace(SIGNATURE, sha256="b" * 64),
                      replace(SIGNATURE, sample_rate=48000), replace(SIGNATURE, channels=2)]
        result = self.search(dict(zip(paths, signatures)).__getitem__)
        self.assertEqual(result.groups, ())
        dupes.render_similar_report(self.console, self.root, dupes.scan_audio_files(self.root), result, 0)
        self.assertIn("DIFFERENT AUDIO", self.output.getvalue())

    def test_transitive_name_similarity_does_not_merge_different_audio(self):
        paths = self.files()
        result = self.search(lambda path: SIGNATURE if path != paths[-1] else replace(SIGNATURE, sha256="b" * 64))
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(len(result.groups[0].files), 3)

    def test_byte_identical_files_reuse_decoding(self):
        paths = self.files()
        for path in paths[1:]:
            path.write_bytes(paths[0].read_bytes())
        reader = mock.Mock(return_value=SIGNATURE)
        result = self.search(reader)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual((result.decoded_files, result.reused_files), (1, 3))

    def test_two_exact_file_pairs_merge_into_one_four_copy_audio_group(self):
        paths = self.files()
        paths[1].write_bytes(paths[0].read_bytes())
        paths[3].write_bytes(paths[2].read_bytes())
        scan = dupes.scan_audio_files(self.root)
        self.assertEqual(len(dupes.find_duplicate_groups(self.root, scan.files).groups), 2)
        result = self.search()
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(len(result.groups[0].files), 4)
        self.assertEqual((result.decoded_files, result.reused_files), (2, 2))

    def test_decoder_failure_is_reported_and_later_files_are_still_checked(self):
        paths = self.files()
        def reader(path):
            if path == paths[1]:
                raise OSError("decoder failed")
            return SIGNATURE
        result = self.search(reader)
        self.assertEqual(len(result.unverified), 1)
        self.assertEqual(result.errors, ())
        self.assertEqual(len(result.groups[0].files), 3)
        with mock.patch.object(dupes, "decoded_signature", side_effect=lambda path, *args: reader(path)), \
                mock.patch.object(dupes.shutil, "which", side_effect=lambda name: name), \
                mock.patch.object(dupes, "run_automatic_removal", return_value=0) as remove:
            self.assertEqual(dupes.main(["--similar", "--review", "--auto-remove", "--dry-run", str(self.root)]), 1)
            remove.assert_called_once()
            planned = {r.path for decision in remove.call_args.args[3] for r in decision.group.files}
            self.assertEqual(planned, set(paths) - {paths[1]})

    def test_confirmed_cli_removes_only_verified_files_when_another_cannot_decode(self):
        paths = self.files()
        def reader(path, *args):
            if path == paths[1]:
                raise OSError("decoder failed")
            return SIGNATURE
        with mock.patch.object(dupes, "decoded_signature", side_effect=reader), \
                mock.patch.object(dupes.shutil, "which", side_effect=lambda name: name), \
                mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value="DELETE"):
            self.assertEqual(dupes.main(["--similar", "--review", "--auto-remove", str(self.root)]), 1)
        self.assertEqual(set(self.root.iterdir()), {paths[0], paths[1]})

    def test_file_hash_errors_still_block_entire_removal(self):
        self.files()
        with mock.patch.object(dupes, "hash_file", side_effect=OSError("read failed")), \
                mock.patch.object(dupes.shutil, "which", side_effect=lambda name: name), \
                mock.patch.object(dupes, "run_automatic_removal") as remove:
            self.assertEqual(dupes.main(["--similar", "--review", "--auto-remove", "--dry-run", str(self.root)]), 1)
            remove.assert_not_called()

    def test_file_changed_during_decode_is_not_verified(self):
        paths = self.files()
        def reader(path):
            if path == paths[1]:
                path.write_bytes(b"changed during decode")
            return SIGNATURE
        result = self.search(reader)
        self.assertEqual(result.errors[0].path, paths[1])
        self.assertNotIn(paths[1], dict(result.groups[0].file_hashes))

    def test_confirmed_plan_keeps_one_of_four_and_rechecks_each_original_file_hash(self):
        paths = self.files()
        result = self.search()
        decisions = dupes.audio_review(result.groups)
        self.assertEqual(decisions[0].keeper, paths[0])
        self.assertEqual(dupes.run_automatic_removal(self.console, self.console, self.root, decisions, False,
                                                  lambda _: "DELETE"), 0)
        self.assertEqual(list(self.root.iterdir()), [paths[0]])
        self.assertIn("3 removed", self.output.getvalue())
        self.assertIn("cue points", self.output.getvalue())

    def test_preview_and_cancel_never_mutate(self):
        paths = self.files()
        before = {path: path.read_bytes() for path in paths}
        decisions = dupes.audio_review(self.search().groups)
        for dry_run in (True, False):
            with mock.patch.object(dupes, "execute_removal") as execute:
                self.assertEqual(dupes.run_automatic_removal(self.console, self.console, self.root, decisions,
                                                          dry_run, lambda _: "no"), 0)
                execute.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_audio_keeper_prefers_depth_then_unnumbered_names(self):
        paths = self.files(("Artist - Title-1.mp3", "nested/Artist - Title.mp3"))
        self.assertEqual(dupes.audio_review(self.search().groups)[0].keeper, paths[0])

    def test_changed_metadata_or_wrong_hash_blocks_entire_plan(self):
        paths = self.files()
        result = self.search()
        group = result.groups[0]
        incorrect = replace(group, file_hashes=tuple((path, "0" * 64) for path, _ in group.file_hashes))
        failed = dupes.execute_removal(self.root, dupes.audio_review((incorrect,)))
        self.assertTrue(failed.errors)
        self.assertEqual(failed.removed, ())
        decisions = dupes.audio_review((group,))
        paths[1].write_bytes(b"changed tags")
        self.assertTrue(dupes.execute_removal(self.root, decisions).errors)
        self.assertTrue(all(path.exists() for path in paths))

    def test_incomplete_audio_plan_hashes_are_rejected(self):
        self.files()
        group, = self.search().groups
        bad = replace(group, file_hashes=group.file_hashes[:-1])
        with self.assertRaisesRegex(ValueError, "incomplete whole-file hashes"):
            dupes.execute_removal(self.root, dupes.audio_review((bad,)))

    def test_hard_links_are_not_selected_for_audio_removal(self):
        paths = self.files()
        paths[1].unlink()
        os.link(paths[0], paths[1])
        self.assertEqual(dupes.audio_review(self.search().groups)[0].status, "skipped")

    def test_missing_tools_invalid_options_and_output_rejected_before_scan(self):
        cases = (("--similarity", "nan"), ("--similarity", "0.4"), ("--audio-timeout", "0"),
                 ("--similar", "--output", "new.json"), ("--menu", "--similar"), ("--similar",))
        with mock.patch.object(dupes.shutil, "which", return_value=None), \
                mock.patch.object(dupes, "scan_audio_files") as scan:
            for flags in cases:
                with self.subTest(flags=flags):
                    self.assertEqual(dupes.main([*flags, str(self.root)]), 2)
            scan.assert_not_called()

    def test_cli_report_is_read_only_and_passes_timeout(self):
        self.files()
        with mock.patch.object(dupes, "decoded_signature", return_value=SIGNATURE) as decoder, \
                mock.patch.object(dupes.shutil, "which", side_effect=lambda name: name), \
                mock.patch.object(dupes, "execute_removal") as execute:
            self.assertEqual(dupes.main(["--similar", "--audio-timeout", "9", str(self.root)]), 0)
            execute.assert_not_called()
            self.assertTrue(all(call.args[-1] == 9 for call in decoder.call_args_list))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg not installed")
class RealDecoderTests(unittest.TestCase):
    def test_small_float_sample_differences_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, value in enumerate((0.25, 0.25 + 2 ** -40)):
                fmt = struct.pack("<HHIIHH", 3, 1, 44100, 44100 * 8, 8, 64)
                samples = struct.pack("<d", value) * 1000
                chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
                chunks += b"data" + struct.pack("<I", len(samples)) + samples
                (root / f"Artist - Title [{index + 2}].wav").write_bytes(
                    b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks,
                )
            result = dupes.find_similar_groups(
                root, dupes.scan_audio_files(root).files, 0.86,
                lambda path: decoded_signature(path, shutil.which("ffmpeg"), shutil.which("ffprobe"), 10),
            )
            self.assertEqual(result.errors, ())
            self.assertEqual(result.groups, ())

    def test_generated_wav_metadata_ignored_but_changed_samples_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / f"Artist - Title{suffix}.wav" for suffix in ("", " [2]", " [3]", " [4]")]
            for index, path in enumerate(paths):
                with wave.open(str(path), "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(44100)
                    audio.writeframes(struct.pack("<h", 1200 if index < 3 else 2400) * 1000)
                data = path.read_bytes()
                # Valid RIFF JUNK metadata makes whole-file hashes and sizes differ.
                extra = b"JUNK" + struct.pack("<I", 2 * index) + b"!" * (2 * index)
                path.write_bytes(data[:4] + struct.pack("<I", len(data) + len(extra) - 8) + data[8:] + extra)
            result = dupes.find_similar_groups(
                root, dupes.scan_audio_files(root).files, 0.86,
                lambda path: decoded_signature(path, shutil.which("ffmpeg"), shutil.which("ffprobe"), 10),
            )
            self.assertEqual(result.errors, ())
            self.assertEqual(len(result.groups), 1)
            self.assertEqual({record.path for record in result.groups[0].files}, set(paths[:3]))


if __name__ == "__main__":
    unittest.main()
