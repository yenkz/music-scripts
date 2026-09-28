import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rich.console import Console

from find_duplicates.find_duplicates import (
    DuplicateGroup,
    FileChangedError,
    FileRecord,
    ReviewDecision,
    ScanError,
    find_duplicate_groups,
    hard_link_sets,
    hash_file,
    main,
    manifest_data,
    record_from_stat,
    recoverable_bytes,
    review_groups,
    scan_audio_files,
    write_manifest_exclusive,
)


def create_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def record(path: Path) -> FileRecord:
    return record_from_stat(path, path.lstat())


class ScanTests(unittest.TestCase):
    def test_scan_is_recursive_filtered_and_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_file(root / "Z.MP3", b"z")
            create_file(root / "nested" / "a.flac", b"a")
            create_file(root / "nested" / "cover.jpg", b"image")
            create_file(root / "empty.WAV", b"")

            result = scan_audio_files(root)

            self.assertEqual(
                [item.path.relative_to(root) for item in result.files],
                [Path("empty.WAV"), Path("nested/a.flac"), Path("Z.MP3")],
            )
            self.assertEqual(result.errors, ())

    def test_scan_skips_file_and_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_file(root / "real.mp3", b"audio")
            create_file(root / "outside" / "other.mp3", b"other")
            (root / "linked.mp3").symlink_to(root / "real.mp3")
            (root / "linked-directory").symlink_to(root / "outside", target_is_directory=True)

            result = scan_audio_files(root)

            self.assertEqual(
                [item.path.relative_to(root) for item in result.files],
                [Path("outside/other.mp3"), Path("real.mp3")],
            )
            self.assertEqual(
                {path.name for path in result.skipped_symlinks},
                {"linked.mp3", "linked-directory"},
            )


class HashTests(unittest.TestCase):
    def test_hash_file_reads_in_chunks(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "track.mp3"
            create_file(path, b"abcdefgh")

            digest = hash_file(record(path), chunk_size=3)

            self.assertEqual(
                digest,
                "9c56cc51b374c3ba189210d5b6d4bf57790d351c96c47c02190ecf1e430635ab",
            )

    def test_hash_file_rejects_file_changed_after_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "track.mp3"
            create_file(path, b"before")
            original = record(path)
            path.write_bytes(b"different length")

            with self.assertRaisesRegex(FileChangedError, "changed after discovery"):
                hash_file(original)

    def test_hash_file_rejects_file_changed_during_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "track.mp3"
            create_file(path, b"audio")
            original = record(path)
            actual = path.stat()
            changed = SimpleNamespace(
                st_mode=actual.st_mode,
                st_size=actual.st_size,
                st_dev=actual.st_dev,
                st_ino=actual.st_ino,
                st_mtime_ns=actual.st_mtime_ns + 1,
                st_ctime_ns=actual.st_ctime_ns,
            )

            with mock.patch(
                "find_duplicates.find_duplicates.os.fstat",
                side_effect=(actual, changed),
            ):
                with self.assertRaisesRegex(FileChangedError, "while it was being hashed"):
                    hash_file(original)

    def test_search_hashes_only_size_collisions_and_groups_exact_matches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_file(root / "a.mp3", b"same")
            create_file(root / "b.flac", b"same")
            create_file(root / "c.wav", b"diff")
            create_file(root / "unique.aac", b"unique")
            records = scan_audio_files(root).files
            calls: list[Path] = []

            def counting_hasher(item: FileRecord) -> str:
                calls.append(item.path)
                return hash_file(item)

            result = find_duplicate_groups(root, records, counting_hasher)

            self.assertEqual(result.hashed_files, 3)
            self.assertEqual({path.name for path in calls}, {"a.mp3", "b.flac", "c.wav"})
            self.assertEqual(len(result.groups), 1)
            self.assertEqual(
                [item.path.name for item in result.groups[0].files], ["a.mp3", "b.flac"]
            )

    def test_search_continues_after_hash_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("a.mp3", "b.mp3", "c.mp3"):
                create_file(root / name, b"same")
            records = scan_audio_files(root).files

            def sometimes_fails(item: FileRecord) -> str:
                if item.path.name == "b.mp3":
                    raise OSError("unreadable")
                return hash_file(item)

            result = find_duplicate_groups(root, records, sometimes_fails)

            self.assertEqual(len(result.groups), 1)
            self.assertEqual(
                [item.path.name for item in result.groups[0].files], ["a.mp3", "c.mp3"]
            )
            self.assertEqual(result.errors[0].path.name, "b.mp3")

    def test_empty_audio_files_can_form_an_exact_duplicate_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_file(root / "empty-a.wav", b"")
            create_file(root / "empty-b.WAV", b"")

            result = find_duplicate_groups(root, scan_audio_files(root).files)

            self.assertEqual(len(result.groups), 1)
            self.assertEqual(result.groups[0].size, 0)
            self.assertEqual(result.groups[0].sha256, hashlib.sha256(b"").hexdigest())

    def test_hard_links_are_included_but_not_counted_as_recoverable_copies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.mp3"
            linked = root / "linked.mp3"
            copied = root / "copied.mp3"
            create_file(first, b"same audio")
            os.link(first, linked)
            create_file(copied, b"same audio")

            result = find_duplicate_groups(root, scan_audio_files(root).files)
            group = result.groups[0]

            self.assertEqual(len(group.files), 3)
            self.assertEqual(len(hard_link_sets(group)), 1)
            self.assertEqual(
                {item.path.name for item in hard_link_sets(group)[0]},
                {"first.mp3", "linked.mp3"},
            )
            self.assertEqual(recoverable_bytes(group), len(b"same audio"))


class ReviewTests(unittest.TestCase):
    def make_groups(self, root: Path) -> tuple[DuplicateGroup, DuplicateGroup]:
        files = []
        for name, content in (
            ("a.mp3", b"one"),
            ("b.mp3", b"one"),
            ("c.mp3", b"two"),
            ("d.mp3", b"two"),
        ):
            path = root / name
            create_file(path, content)
            files.append(record(path))
        return (
            DuplicateGroup("1" * 64, 3, tuple(files[:2])),
            DuplicateGroup("2" * 64, 3, tuple(files[2:])),
        )

    def test_review_reprompts_then_records_keeper_and_skip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            groups = self.make_groups(root)
            answers = iter(("bad", "2", "s"))
            output = io.StringIO()

            decisions = review_groups(
                Console(file=output, force_terminal=False, color_system=None),
                root,
                groups,
                lambda _: next(answers),
            )

            self.assertEqual([item.status for item in decisions], ["reviewed", "skipped"])
            self.assertEqual(decisions[0].keeper, root / "b.mp3")
            self.assertIn("Enter a listed number", output.getvalue())

    def test_quit_marks_current_and_remaining_groups_unreviewed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            groups = self.make_groups(root)

            decisions = review_groups(
                Console(file=io.StringIO(), force_terminal=False, color_system=None),
                root,
                groups,
                lambda _: "q",
            )

            self.assertEqual([item.status for item in decisions], ["unreviewed", "unreviewed"])

    def test_manifest_uses_relative_paths_and_records_partial_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = self.make_groups(root)
            decisions = (
                ReviewDecision(first, "reviewed", root / "a.mp3"),
                ReviewDecision(second, "unreviewed"),
            )

            data = manifest_data(
                root, decisions, (ScanError(root / "broken.mp3", "cannot read"),)
            )

            self.assertEqual(data["schema_version"], 1)
            self.assertFalse(data["scan_complete"])
            first_group = data["groups"][0]
            self.assertEqual(first_group["paths"], ["a.mp3", "b.mp3"])
            self.assertEqual(first_group["decision"]["keeper"], "a.mp3")
            self.assertEqual(first_group["decision"]["duplicates"], ["b.mp3"])
            self.assertEqual(data["groups"][1]["decision"], {"status": "unreviewed"})

    def test_manifest_write_is_deterministic_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "review.json"
            data = {"schema_version": 1, "groups": []}

            write_manifest_exclusive(destination, data)

            self.assertEqual(json.loads(destination.read_text()), data)
            original = destination.read_text()
            with self.assertRaises(FileExistsError):
                write_manifest_exclusive(destination, {"changed": True})
            self.assertEqual(destination.read_text(), original)

    def test_manifest_refuses_dangling_symlink_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "review.json"
            destination.symlink_to(Path(temporary) / "missing.json")

            with self.assertRaises(FileExistsError):
                write_manifest_exclusive(destination, {"schema_version": 1})


class CliTests(unittest.TestCase):
    def test_output_requires_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch("sys.stdin.isatty", return_value=True):
                self.assertEqual(main(["--output", "review.json", temporary]), 2)

    def test_review_requires_tty(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch("sys.stdin.isatty", return_value=False):
                self.assertEqual(main(["--review", temporary]), 2)

    def test_scan_errors_produce_exit_one_after_reporting_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_scan = mock.Mock(
                files=(),
                skipped_symlinks=(),
                errors=(ScanError(root / "bad.mp3", "failed"),),
            )
            with mock.patch(
                "find_duplicates.find_duplicates.scan_audio_files", return_value=fake_scan
            ):
                self.assertEqual(main([temporary]), 1)


if __name__ == "__main__":
    unittest.main()
