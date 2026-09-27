import unittest
from pathlib import Path
from unittest.mock import patch

from create_metadata.recognition import (
    DiscogsClient,
    Match,
    ProviderError,
    ShazamClient,
    filename_candidate,
    select_discogs_match,
    select_shazam_match,
)


class RecognitionTests(unittest.TestCase):
    def test_discogs_request_cache_avoids_network_and_rate_limit(self):
        payload = {"results": []}
        stored = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def read(self):
                return b'{"results": []}'

        def urlopen(request, timeout):
            return Response()

        client = DiscogsClient(
            "token",
            30,
            urlopen=urlopen,
            cache_get=stored.get,
            cache_put=stored.__setitem__,
        )

        self.assertEqual(client.request("https://api.discogs.com/test"), payload)
        self.assertEqual(client.request("https://api.discogs.com/test"), payload)
        self.assertEqual(client.network_requests, 1)
        self.assertEqual(client.cache_hits, 1)

    def test_filename_candidate_removes_dj_codes_and_download_markers(self):
        candidate = filename_candidate(
            Path(
                "8B - 6 - James Teej - Liking Your Disorder "
                "(Timo Maas Remix) [www.example.kz].mp3"
            )
        )

        self.assertEqual(
            candidate,
            Match(
                "James Teej",
                "Liking Your Disorder (Timo Maas Remix)",
                0.70,
                source="Filename",
            ),
        )

    def test_filename_candidate_preserves_punctuation_artist(self):
        candidate = filename_candidate(Path("!!! - Funk (I Got This).mp3"))

        self.assertEqual(candidate.artist, "!!!")
        self.assertEqual(candidate.title, "Funk (I Got This)")

    def test_discogs_validates_track_and_removes_artist_disambiguation(self):
        candidate = Match(
            "Feathered Sun", "Saubohnen", 0.70, source="Filename"
        )
        releases = [
            {
                "id": 123,
                "artists": [{"name": "Feathered Sun (2)"}],
                "tracklist": [{"position": "A1", "title": "Saubohnen"}],
            }
        ]

        selection = select_discogs_match(candidate, releases)

        self.assertEqual(selection.status, "ready")
        self.assertEqual(selection.match.artist, "Feathered Sun")
        self.assertEqual(selection.match.title, "Saubohnen")
        self.assertEqual(selection.match.source, "Discogs")

    def test_shazam_selects_track_title_and_subtitle(self):
        payload = {
            "matches": [
                {
                    "id": "53982678",
                    "offset": 0.0,
                    "timeskew": 0.0,
                    "frequencyskew": 0.0,
                }
            ],
            "track": {
                "key": "53982678",
                "title": "I Will Survive",
                "subtitle": "Gloria Gaynor",
            },
        }

        selection = select_shazam_match(payload)

        self.assertEqual(selection.status, "ready")
        self.assertEqual(
            selection.match,
            Match(
                "Gloria Gaynor",
                "I Will Survive",
                1.0,
                "53982678",
                "Shazam",
            ),
        )

    def test_shazam_empty_matches_is_no_match(self):
        selection = select_shazam_match({"matches": []})

        self.assertEqual(selection.status, "no_match")
        self.assertIsNone(selection.match)

    def test_shazam_ignores_stray_track_when_matches_are_empty(self):
        selection = select_shazam_match(
            {
                "matches": [],
                "track": {"title": "Track", "subtitle": "Artist"},
            }
        )

        self.assertEqual(selection.status, "no_match")
        self.assertIsNone(selection.match)

    def test_shazam_match_without_track_is_invalid(self):
        with self.assertRaisesRegex(ProviderError, "without track metadata"):
            select_shazam_match({"matches": [{"id": "123"}]})

    def test_shazam_track_without_artist_is_not_usable(self):
        selection = select_shazam_match(
            {"matches": [{"id": "123"}], "track": {"title": "Track"}}
        )

        self.assertEqual(selection.status, "no_match")
        self.assertIsNone(selection.match)

    def test_shazam_client_runs_async_recognizer_on_temporary_sample(self):
        seen_paths = []

        async def recognize(path):
            seen_paths.append(path)
            self.assertTrue(path.is_file())
            return {
                "matches": [{"id": "123"}],
                "track": {"key": "123", "title": "Track", "subtitle": "Artist"},
            }

        def create_sample(path, output, ffmpeg, *, timeout):
            output.write_bytes(b"sample")

        client = ShazamClient(
            "/usr/bin/ffmpeg",
            120,
            30,
            recognizer=recognize,
        )
        with patch(
            "create_metadata.recognition.create_audio_sample",
            side_effect=create_sample,
        ):
            selection = client(Path("source.flac"))

        self.assertEqual(selection.match.source, "Shazam")
        self.assertEqual(len(seen_paths), 1)
        self.assertFalse(seen_paths[0].exists())

    def test_shazam_client_wraps_known_provider_error(self):
        class NetworkFailure(Exception):
            pass

        async def recognize(path):
            raise NetworkFailure("service unavailable")

        def create_sample(path, output, ffmpeg, *, timeout):
            output.write_bytes(b"sample")

        client = ShazamClient(
            "/usr/bin/ffmpeg",
            120,
            30,
            recognizer=recognize,
            provider_errors=(NetworkFailure,),
        )
        with patch(
            "create_metadata.recognition.create_audio_sample",
            side_effect=create_sample,
        ):
            with self.assertRaisesRegex(
                ProviderError, "Shazam recognition failed: service unavailable"
            ):
                client(Path("source.flac"))


if __name__ == "__main__":
    unittest.main()
