import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from create_metadata.cache import RecognitionCache
from create_metadata.create_metadata import (
    CachedFingerprinter,
    Fingerprint,
    RateLimitedLookup,
)


class RecognitionCacheTests(unittest.TestCase):
    def test_cache_persists_json_between_instances(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cache.sqlite3"
            with RecognitionCache(path, ttl_seconds=60) as cache:
                cache.put("provider", "key", {"answer": 42})

            with RecognitionCache(path, ttl_seconds=60) as cache:
                self.assertEqual(cache.get("provider", "key"), {"answer": 42})
                self.assertEqual(cache.hits, 1)

    def test_refresh_keeps_permanent_fingerprints_and_reuses_new_responses(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cache.sqlite3"
            with RecognitionCache(path, ttl_seconds=60) as cache:
                cache.put("fingerprint-v1", "file", {"value": "abc"})
                cache.put("provider", "request", {"old": True})

            with RecognitionCache(path, ttl_seconds=60, refresh=True) as cache:
                self.assertEqual(
                    cache.get("fingerprint-v1", "file", permanent=True),
                    {"value": "abc"},
                )
                self.assertIsNone(cache.get("provider", "request"))
                cache.put("provider", "request", {"fresh": True})
                self.assertEqual(
                    cache.get("provider", "request"), {"fresh": True}
                )

    def test_fingerprinter_reuses_result_until_file_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "track.mp3"
            source.write_bytes(b"first")
            with RecognitionCache(root / "cache.sqlite3", ttl_seconds=60) as cache:
                fingerprinter = CachedFingerprinter("fpcalc", 120, cache)
                with patch(
                    "create_metadata.create_metadata.fingerprint_file",
                    return_value=Fingerprint(180, "fingerprint"),
                ) as fingerprint:
                    first = fingerprinter(source)
                    second = fingerprinter(source)
                    source.write_bytes(b"changed-size")
                    third = fingerprinter(source)

            self.assertEqual(first, second)
            self.assertEqual(second, third)
            self.assertEqual(fingerprint.call_count, 2)
            self.assertEqual(fingerprinter.cache_hits, 1)

    def test_acoustid_cache_avoids_network_and_rate_limit(self):
        payload = {"status": "ok", "results": []}
        fingerprint = Fingerprint(180, "fingerprint")
        with tempfile.TemporaryDirectory() as temp:
            with RecognitionCache(
                Path(temp) / "cache.sqlite3", ttl_seconds=60
            ) as cache:
                lookup = RateLimitedLookup("key", 30, cache)
                with patch(
                    "create_metadata.create_metadata.lookup_acoustid",
                    return_value=payload,
                ) as network:
                    self.assertEqual(lookup(fingerprint), payload)
                    self.assertEqual(lookup(fingerprint), payload)

            self.assertEqual(network.call_count, 1)
            self.assertEqual(lookup.cache_hits, 1)
            self.assertEqual(lookup.network_requests, 1)


if __name__ == "__main__":
    unittest.main()
