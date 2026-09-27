import json
import tempfile
import unittest
import wave
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from create_metadata.create_metadata import (
    ConfigError,
    ExistingMetadata,
    Fingerprint,
    FileOutcome,
    Match,
    Selection,
    analyze_missing_file,
    configuration_from_args,
    fingerprint_file,
    load_env_file,
    needs_lookup,
    parse_args,
    partial_metadata_candidate,
    placeholder_metadata_candidate,
    read_existing_metadata,
    render_report,
    scan_audio_files,
    select_match,
    write_missing_metadata,
)


class FakeAudio:
    def __init__(self, tags=None):
        self.tags = tags
        self.saved = False

    def __setitem__(self, key, value):
        self.tags[key] = value

    def add_tags(self):
        self.tags = {}

    def save(self):
        self.saved = True


def recording(artist, title, recording_id="recording-id"):
    return {
        "id": recording_id,
        "title": title,
        "artists": [{"name": artist}],
    }


class CreateMetadataTests(unittest.TestCase):
    def test_env_file_loads_secrets_quotes_and_optional_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / ".env"
            env_file.write_text(
                "# local configuration\n"
                "export ACOUSTID_API_KEY='secret # value'\n"
                'CREATE_METADATA_FPCALC="/opt/tools/fpcalc" # executable\n'
                "CREATE_METADATA_MIN_SCORE=0.93\n",
                encoding="utf-8",
            )

            values = load_env_file(env_file)

            self.assertEqual(values["ACOUSTID_API_KEY"], "secret # value")
            self.assertEqual(values["CREATE_METADATA_FPCALC"], "/opt/tools/fpcalc")
            self.assertEqual(values["CREATE_METADATA_MIN_SCORE"], "0.93")

    def test_configuration_precedence_is_cli_then_environment_then_dotenv(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / ".env"
            env_file.write_text(
                "ACOUSTID_API_KEY=file-key\n"
                "CREATE_METADATA_MIN_SCORE=0.91\n"
                "CREATE_METADATA_MIN_MARGIN=0.07\n",
                encoding="utf-8",
            )
            args = parse_args(
                [
                    "--env-file",
                    str(env_file),
                    "--min-margin",
                    "0.12",
                ]
            )

            configuration = configuration_from_args(
                args,
                environ={
                    "ACOUSTID_API_KEY": "environment-key",
                    "CREATE_METADATA_MIN_SCORE": "0.96",
                },
            )

            self.assertEqual(configuration.api_key, "environment-key")
            self.assertEqual(configuration.min_score, 0.96)
            self.assertEqual(configuration.min_margin, 0.12)
            self.assertEqual(configuration.fpcalc, "fpcalc")
            self.assertFalse(configuration.shazam_enabled)

    def test_shazam_can_be_enabled_from_dotenv(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / ".env"
            env_file.write_text("CREATE_METADATA_SHAZAM=yes\n", encoding="utf-8")
            args = parse_args(["--env-file", str(env_file)])

            configuration = configuration_from_args(args, environ={})

            self.assertTrue(configuration.shazam_enabled)

    def test_no_shazam_cli_flag_overrides_dotenv(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / ".env"
            env_file.write_text("CREATE_METADATA_SHAZAM=true\n", encoding="utf-8")
            args = parse_args(["--env-file", str(env_file), "--no-shazam"])

            configuration = configuration_from_args(args, environ={})

            self.assertFalse(configuration.shazam_enabled)

    def test_invalid_shazam_boolean_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / ".env"
            env_file.write_text("CREATE_METADATA_SHAZAM=perhaps\n", encoding="utf-8")
            args = parse_args(["--env-file", str(env_file)])

            with self.assertRaises(ConfigError):
                configuration_from_args(args, environ={})

    def test_scan_audio_files_is_recursive_and_skips_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            nested = root / "Album"
            nested.mkdir()
            (root / "root.MP3").write_bytes(b"audio")
            (nested / "track.flac").write_bytes(b"audio")
            (nested / "cover.jpg").write_bytes(b"image")
            linked = root / "Linked"
            try:
                linked.symlink_to(nested, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks are unavailable")

            files = scan_audio_files(root)

            self.assertEqual(
                [path.relative_to(root) for path in files],
                [Path("Album/track.flac"), Path("root.MP3")],
            )

    def test_default_lookup_only_targets_incomplete_metadata(self):
        complete = ExistingMetadata("Artist", "Title")
        missing_artist = ExistingMetadata(None, "Title")
        missing_title = ExistingMetadata("Artist", None)

        self.assertFalse(needs_lookup(complete, replace_existing=False))
        self.assertTrue(needs_lookup(missing_artist, replace_existing=False))
        self.assertTrue(needs_lookup(missing_title, replace_existing=False))
        self.assertTrue(needs_lookup(complete, replace_existing=True))

    def test_partial_metadata_recovers_missing_artist_from_matching_title(self):
        recovered = partial_metadata_candidate(
            Path("Artist - Track (Dub Mix).mp3"),
            ExistingMetadata(None, "Track (Dub Mix)"),
        )

        self.assertIsNotNone(recovered)
        match, repairs_packed_title = recovered
        self.assertEqual(match.artist, "Artist")
        self.assertEqual(match.title, "Track (Dub Mix)")
        self.assertFalse(repairs_packed_title)

    def test_partial_metadata_recovers_missing_title_from_matching_artist(self):
        recovered = partial_metadata_candidate(
            Path("Artist - Track.mp3"), ExistingMetadata("artist", None)
        )

        self.assertIsNotNone(recovered)
        match, repairs_packed_title = recovered
        self.assertEqual(match.title, "Track")
        self.assertFalse(repairs_packed_title)

    def test_partial_metadata_detects_full_filename_stored_as_title(self):
        recovered = partial_metadata_candidate(
            Path("Solomun - Forever (The Teenagers remix).mp3"),
            ExistingMetadata(
                None, "Solomun - Forever (The Teenagers remix)"
            ),
        )

        self.assertIsNotNone(recovered)
        match, repairs_packed_title = recovered
        self.assertEqual(match.artist, "Solomun")
        self.assertEqual(match.title, "Forever (The Teenagers remix)")
        self.assertTrue(repairs_packed_title)

    def test_partial_metadata_repairs_premiere_prefixed_packed_title(self):
        recovered = partial_metadata_candidate(
            Path(
                "110 Unknown Artist - Damon Jee & Darlyn Vlys - "
                "UFO (Original Mix) [Dust & Blood].mp3"
            ),
            ExistingMetadata(
                None,
                "PREMIERE: Damon Jee & Darlyn Vlys - "
                "UFO (Original Mix) [Dust & Blood]",
            ),
        )

        self.assertIsNotNone(recovered)
        match, repairs_packed_title = recovered
        self.assertEqual(match.artist, "Damon Jee & Darlyn Vlys")
        self.assertEqual(match.title, "UFO (Original Mix) [Dust & Blood]")
        self.assertTrue(repairs_packed_title)

    def test_partial_metadata_rejects_tag_that_disagrees_with_filename(self):
        recovered = partial_metadata_candidate(
            Path("Artist - Track.mp3"), ExistingMetadata(None, "Different Track")
        )

        self.assertIsNone(recovered)

    def test_numbered_unknown_artist_tags_are_recovered_safely(self):
        path = Path(
            "1 Unknown Artist - Alan Fitzpatrick & Lawrence Hart - "
            "Closing In (Jody Wisternoff Remix).mp3"
        )
        existing = ExistingMetadata(
            "1 Unknown Artist",
            "Alan Fitzpatrick & Lawrence Hart - Closing In (Jody Wisternoff Remix)",
        )

        recovered = placeholder_metadata_candidate(path, existing)

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.artist, "Alan Fitzpatrick & Lawrence Hart")
        self.assertEqual(recovered.title, "Closing In (Jody Wisternoff Remix)")

    def test_placeholder_repair_requires_exact_generated_filename(self):
        recovered = placeholder_metadata_candidate(
            Path("unrelated.mp3"),
            ExistingMetadata("1 Unknown Artist", "Artist - Track"),
        )

        self.assertIsNone(recovered)

    def test_analysis_repairs_numbered_unknown_artist_without_force(self):
        outcome = analyze_missing_file(
            Path("4 Unknown Artist - Anderholm - Let Me In feat. Richard Walters.mp3"),
            ExistingMetadata(
                "4 Unknown Artist", "Anderholm - Let Me In feat. Richard Walters"
            ),
            fpcalc=None,
            acoustid_lookup=None,
            min_score=0.9,
            min_margin=0.05,
            fingerprint_timeout=120,
        )

        self.assertEqual(outcome.status, "ready")
        self.assertEqual(outcome.match.artist, "Anderholm")
        self.assertEqual(outcome.match.title, "Let Me In feat. Richard Walters")

    def test_write_repairs_numbered_unknown_artist_without_force(self):
        path = Path("4 Unknown Artist - Anderholm - Let Me In feat. Richard Walters.mp3")
        audio = FakeAudio(
            {
                "artist": ["4 Unknown Artist"],
                "title": ["Anderholm - Let Me In feat. Richard Walters"],
            }
        )

        changed = write_missing_metadata(
            path,
            Match(
                "Anderholm",
                "Let Me In feat. Richard Walters",
                1.0,
                source="Placeholder tag repair",
            ),
            mutagen_file=lambda value, easy=True: audio,
        )

        self.assertEqual(changed, ("artist", "title"))
        self.assertEqual(audio.tags["artist"], "Anderholm")
        self.assertEqual(audio.tags["title"], "Let Me In feat. Richard Walters")

    def test_default_report_omits_complete_files_but_keeps_summary_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = StringIO()
            console = Console(
                file=output,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            outcomes = [
                FileOutcome(
                    root / "complete.mp3",
                    ExistingMetadata("Artist", "Title"),
                    None,
                    "already_tagged",
                    "artist and title exist",
                ),
                FileOutcome(
                    root / "missing.mp3",
                    ExistingMetadata(None, "Title"),
                    None,
                    "no_match",
                    "no MusicBrainz artist/title match",
                ),
            ]

            render_report(
                console,
                root,
                outcomes,
                write=False,
                replace_existing=False,
            )

            report = output.getvalue()
            self.assertNotIn("complete.mp3", report)
            self.assertIn("missing.mp3", report)
            self.assertIn("COMPLETE", report)

    def test_shazam_report_does_not_invent_a_confidence_percentage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = StringIO()
            console = Console(
                file=output,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            outcomes = [
                FileOutcome(
                    root / "missing.mp3",
                    ExistingMetadata(None, None),
                    Match("Artist", "Track", 1.0, "123", "Shazam"),
                    "ready",
                    "add artist and title; audio recognized by Shazam",
                )
            ]

            render_report(
                console,
                root,
                outcomes,
                write=False,
                replace_existing=False,
            )

            report = output.getvalue()
            self.assertIn("catalog match · Shazam", report)
            self.assertNotIn("100.0% confidence", report)

    def test_fingerprint_file_parses_fpcalc_json(self):
        process = type(
            "Process",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps({"duration": 184.6, "fingerprint": "abc123"}),
                "stderr": "",
            },
        )()
        with patch("create_metadata.create_metadata.subprocess.run", return_value=process) as run:
            result = fingerprint_file(Path("song.mp3"), "/usr/bin/fpcalc")

        self.assertEqual(result, Fingerprint(185, "abc123"))
        run.assert_called_once()

    def test_select_match_accepts_one_high_confidence_candidate(self):
        payload = {
            "status": "ok",
            "results": [
                {"score": 0.97, "recordings": [recording("Artist", "Track")]}
            ],
        }

        selection = select_match(payload, min_score=0.9, min_margin=0.05)

        self.assertEqual(selection.status, "ready")
        self.assertEqual(selection.match, Match("Artist", "Track", 0.97, "recording-id"))

    def test_select_match_rejects_close_distinct_candidate(self):
        payload = {
            "status": "ok",
            "results": [
                {"score": 0.96, "recordings": [recording("Artist", "Track A", "a")]},
                {"score": 0.93, "recordings": [recording("Artist", "Track B", "b")]},
            ],
        }

        selection = select_match(payload, min_score=0.9, min_margin=0.05)

        self.assertEqual(selection.status, "ambiguous")
        self.assertIsNone(selection.match)

    def test_analyze_rejects_match_that_conflicts_with_existing_artist(self):
        payload = {
            "status": "ok",
            "results": [
                {"score": 0.99, "recordings": [recording("Other Artist", "Track")]}
            ],
        }
        with patch(
            "create_metadata.create_metadata.fingerprint_file",
            return_value=Fingerprint(180, "fingerprint"),
        ):
            outcome = analyze_missing_file(
                Path("song.mp3"),
                ExistingMetadata("Known Artist", None),
                fpcalc="fpcalc",
                acoustid_lookup=lambda fingerprint: payload,
                min_score=0.9,
                min_margin=0.05,
                fingerprint_timeout=120,
            )

        self.assertEqual(outcome.status, "conflict")

    def test_discogs_validation_short_circuits_acoustic_lookup(self):
        discogs_match = Match(
            "Artist", "Track", 1.0, "release-id", "Discogs"
        )
        with patch(
            "create_metadata.create_metadata.fingerprint_file"
        ) as fingerprint:
            outcome = analyze_missing_file(
                Path("Artist - Track.mp3"),
                ExistingMetadata(None, None),
                fpcalc="fpcalc",
                acoustid_lookup=lambda value: self.fail("AcoustID should not run"),
                discogs_lookup=lambda candidate: Selection(
                    discogs_match, "ready", "filename validated by Discogs"
                ),
                min_score=0.9,
                min_margin=0.05,
                fingerprint_timeout=120,
            )

        fingerprint.assert_not_called()
        self.assertEqual(outcome.status, "ready")
        self.assertEqual(outcome.match.source, "Discogs")

    def test_acoustid_match_short_circuits_shazam(self):
        payload = {
            "status": "ok",
            "results": [
                {"score": 0.99, "recordings": [recording("Artist", "Track")]}
            ],
        }
        with patch(
            "create_metadata.create_metadata.fingerprint_file",
            return_value=Fingerprint(180, "fingerprint"),
        ):
            outcome = analyze_missing_file(
                Path("unknown.mp3"),
                ExistingMetadata(None, None),
                fpcalc="fpcalc",
                acoustid_lookup=lambda fingerprint: payload,
                shazam_lookup=lambda path: self.fail("Shazam should not run"),
                min_score=0.9,
                min_margin=0.05,
                fingerprint_timeout=120,
            )

        self.assertEqual(outcome.status, "ready")
        self.assertEqual(outcome.match.source, "AcoustID")

    def test_existing_packed_title_short_circuits_external_lookups(self):
        outcome = analyze_missing_file(
            Path("Solomun - Forever (The Teenagers remix).mp3"),
            ExistingMetadata(
                None, "Solomun - Forever (The Teenagers remix)"
            ),
            fpcalc="fpcalc",
            acoustid_lookup=lambda value: self.fail("AcoustID should not run"),
            discogs_lookup=lambda value: self.fail("Discogs should not run"),
            shazam_lookup=lambda value: self.fail("Shazam should not run"),
            min_score=0.9,
            min_margin=0.05,
            fingerprint_timeout=120,
        )

        self.assertEqual(outcome.status, "ready")
        self.assertEqual(outcome.match.artist, "Solomun")
        self.assertEqual(outcome.match.title, "Forever (The Teenagers remix)")
        self.assertIn("repair packed title", outcome.detail)

    def test_acoustid_no_match_falls_back_to_shazam(self):
        shazam_match = Match("Artist", "Track", 1.0, "123", "Shazam")
        payload = {"status": "ok", "results": []}
        with patch(
            "create_metadata.create_metadata.fingerprint_file",
            return_value=Fingerprint(180, "fingerprint"),
        ):
            outcome = analyze_missing_file(
                Path("unknown.mp3"),
                ExistingMetadata(None, None),
                fpcalc="fpcalc",
                acoustid_lookup=lambda fingerprint: payload,
                shazam_lookup=lambda path: Selection(
                    shazam_match, "ready", "audio recognized by Shazam"
                ),
                min_score=0.9,
                min_margin=0.05,
                fingerprint_timeout=120,
            )

        self.assertEqual(outcome.status, "ready")
        self.assertEqual(outcome.match.source, "Shazam")

    def test_analysis_reports_each_provider_stage(self):
        stages = []

        outcome = analyze_missing_file(
            Path("Artist - Track.mp3"),
            ExistingMetadata(None, None),
            fpcalc="fpcalc",
            acoustid_lookup=lambda fingerprint: {"status": "ok", "results": []},
            discogs_lookup=lambda candidate: Selection(
                None, "no_match", "Discogs found no match"
            ),
            shazam_lookup=lambda path: Selection(
                None, "no_match", "Shazam found no match"
            ),
            min_score=0.9,
            min_margin=0.05,
            fingerprint_timeout=120,
            fingerprint_lookup=lambda path: Fingerprint(180, "fingerprint"),
            progress=stages.append,
        )

        self.assertEqual(
            stages, ["Discogs", "fingerprinting", "AcoustID", "Shazam"]
        )
        self.assertEqual(outcome.status, "unverified")

    def test_shazam_does_not_override_a_conflicting_filename(self):
        shazam_match = Match("Wrong Artist", "Wrong Track", 1.0, "123", "Shazam")

        outcome = analyze_missing_file(
            Path("Filename Artist - Filename Track.mp3"),
            ExistingMetadata(None, None),
            fpcalc=None,
            acoustid_lookup=None,
            shazam_lookup=lambda path: Selection(
                shazam_match, "ready", "audio recognized by Shazam"
            ),
            min_score=0.9,
            min_margin=0.05,
            fingerprint_timeout=120,
        )

        self.assertEqual(outcome.status, "unverified")
        self.assertEqual(outcome.match.source, "Filename")
        self.assertIn("Shazam result disagrees with the filename", outcome.detail)

    def test_shazam_can_validate_an_agreeing_filename_as_last_fallback(self):
        shazam_match = Match("Artist", "Track", 1.0, "123", "Shazam")

        outcome = analyze_missing_file(
            Path("Artist - Track.mp3"),
            ExistingMetadata(None, None),
            fpcalc=None,
            acoustid_lookup=None,
            shazam_lookup=lambda path: Selection(
                shazam_match, "ready", "audio recognized by Shazam"
            ),
            min_score=0.9,
            min_margin=0.05,
            fingerprint_timeout=120,
        )

        self.assertEqual(outcome.status, "ready")
        self.assertEqual(outcome.match.source, "Shazam")

    def test_filename_candidate_is_previewed_but_not_written_without_validation(self):
        outcome = analyze_missing_file(
            Path("Artist - Track (Dub Mix).mp3"),
            ExistingMetadata(None, None),
            fpcalc=None,
            acoustid_lookup=None,
            min_score=0.9,
            min_margin=0.05,
            fingerprint_timeout=120,
        )

        self.assertEqual(outcome.status, "unverified")
        self.assertEqual(outcome.match.source, "Filename")

    def test_forced_analysis_allows_replacement_match(self):
        payload = {
            "status": "ok",
            "results": [
                {"score": 0.99, "recordings": [recording("Correct Artist", "Track")]}
            ],
        }
        with patch(
            "create_metadata.create_metadata.fingerprint_file",
            return_value=Fingerprint(180, "fingerprint"),
        ):
            outcome = analyze_missing_file(
                Path("song.mp3"),
                ExistingMetadata("Wrong Artist", "Wrong Title"),
                fpcalc="fpcalc",
                acoustid_lookup=lambda fingerprint: payload,
                min_score=0.9,
                min_margin=0.05,
                fingerprint_timeout=120,
                replace_existing=True,
            )

        self.assertEqual(outcome.status, "ready")
        self.assertEqual(
            outcome.detail,
            "replace artist and title; audio recognized by AcoustID",
        )

    def test_write_adds_only_missing_title_and_preserves_other_tags(self):
        audio = FakeAudio({"artist": ["Known Artist"], "album": ["Album"]})

        changed = write_missing_metadata(
            Path("song.mp3"),
            Match("Known Artist", "Found Title", 0.99),
            mutagen_file=lambda path, easy=True: audio,
        )

        self.assertEqual(changed, ("title",))
        self.assertEqual(audio.tags["artist"], ["Known Artist"])
        self.assertEqual(audio.tags["album"], ["Album"])
        self.assertEqual(audio.tags["title"], "Found Title")
        self.assertTrue(audio.saved)

    def test_write_repairs_full_filename_stored_as_title(self):
        path = Path("Solomun - Forever (The Teenagers remix).mp3")
        audio = FakeAudio(
            {"title": ["Solomun - Forever (The Teenagers remix)"]}
        )

        changed = write_missing_metadata(
            path,
            Match(
                "Solomun",
                "Forever (The Teenagers remix)",
                1.0,
                source="Existing tag + filename",
            ),
            mutagen_file=lambda value, easy=True: audio,
        )

        self.assertEqual(changed, ("artist", "title"))
        self.assertEqual(audio.tags["artist"], "Solomun")
        self.assertEqual(audio.tags["title"], "Forever (The Teenagers remix)")
        self.assertTrue(audio.saved)

    def test_write_creates_tag_container_when_absent(self):
        audio = FakeAudio()

        changed = write_missing_metadata(
            Path("song.mp3"),
            Match("Artist", "Title", 1.0),
            mutagen_file=lambda path, easy=True: audio,
        )

        self.assertEqual(changed, ("artist", "title"))
        self.assertEqual(audio.tags, {"artist": "Artist", "title": "Title"})
        self.assertTrue(audio.saved)

    def test_forced_write_replaces_artist_and_title_but_preserves_other_tags(self):
        audio = FakeAudio(
            {
                "artist": ["Wrong Artist"],
                "title": ["Wrong Title"],
                "album": ["Preserved Album"],
            }
        )

        changed = write_missing_metadata(
            Path("song.mp3"),
            Match("Correct Artist", "Correct Title", 0.99),
            mutagen_file=lambda path, easy=True: audio,
            replace_existing=True,
        )

        self.assertEqual(changed, ("artist", "title"))
        self.assertEqual(audio.tags["artist"], "Correct Artist")
        self.assertEqual(audio.tags["title"], "Correct Title")
        self.assertEqual(audio.tags["album"], ["Preserved Album"])
        self.assertTrue(audio.saved)

    def test_real_wav_uses_id3_frames_for_artist_and_title(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "silence.wav"
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(b"\0\0" * 8000)

            changed = write_missing_metadata(
                path, Match("Test Artist", "Test Title", 1.0)
            )

            self.assertEqual(changed, ("artist", "title"))
            self.assertEqual(
                read_existing_metadata(path),
                ExistingMetadata("Test Artist", "Test Title"),
            )

            replaced = write_missing_metadata(
                path,
                Match("Corrected Artist", "Corrected Title", 1.0),
                replace_existing=True,
            )

            self.assertEqual(replaced, ("artist", "title"))
            self.assertEqual(
                read_existing_metadata(path),
                ExistingMetadata("Corrected Artist", "Corrected Title"),
            )


if __name__ == "__main__":
    unittest.main()
