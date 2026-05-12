"""Tests for ClassificationCache.

Phase Perf. Verifies:
- get/set roundtrip,
- text-hash key (different chunk_id, same text -> same cache entry),
- TTL enforcement on load,
- save/load roundtrip across instances,
- defensive behavior on corrupt cache files (no exception, empty memory),
- get/set never raise.
"""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spectrum_systems_core.extraction.classification_cache import (
    ClassificationCache,
)


class CacheGetSetTests(unittest.TestCase):
    def test_cache_hit_returns_cached_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = ClassificationCache(tmp)
            chunk = {"chunk_id": "c1", "text": "Approved by the group."}
            self.assertIsNone(cache.get(chunk))
            cache.set(chunk, "decision")
            self.assertEqual(cache.get(chunk), "decision")

    def test_cache_miss_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = ClassificationCache(tmp)
            self.assertIsNone(
                cache.get({"chunk_id": "x", "text": "Has anyone seen the agenda?"})
            )


class CacheKeyIsTextHashTests(unittest.TestCase):
    def test_different_chunk_ids_same_text_collapse_to_same_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = ClassificationCache(tmp)
            first = {"chunk_id": "c1", "text": "Same body of speaker turn."}
            second = {"chunk_id": "c2", "text": "Same body of speaker turn."}

            self.assertIsNone(cache.get(first))
            cache.set(first, "claim")
            # Specifically call cache.get() on the SECOND chunk: different
            # ID, same text, must hit because the key is the text hash.
            self.assertEqual(cache.get(second), "claim")

    def test_different_text_does_not_collide(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = ClassificationCache(tmp)
            cache.set({"chunk_id": "c1", "text": "alpha"}, "claim")
            self.assertIsNone(
                cache.get({"chunk_id": "c1", "text": "beta"})
            )

    def test_long_text_with_shared_preamble_does_not_collide(self) -> None:
        # Speaker turns can be many KB and frequently share a long
        # preamble. Hashing the full text (not a prefix) is required so a
        # change at the END of a long turn still invalidates the cache.
        # If hashed in prefix mode this would collide and return the
        # first classification for the second chunk -- a stale-result
        # bug.
        common = "x" * 5000
        with tempfile.TemporaryDirectory() as tmp:
            cache = ClassificationCache(tmp)
            cache.set({"chunk_id": "c1", "text": common + "approved"}, "decision")
            self.assertIsNone(
                cache.get({"chunk_id": "c2", "text": common + "tabled"}),
                "Long-preamble chunks with different endings must NOT "
                "share a cache entry",
            )


class CacheTTLTests(unittest.TestCase):
    def test_cache_expires_after_ttl(self) -> None:
        # Write a cache file with one entry stamped 31 days in the past.
        with tempfile.TemporaryDirectory() as tmp:
            cache = ClassificationCache(tmp)
            chunk = {"chunk_id": "c1", "text": "stale entry text"}
            cache.set(chunk, "claim")
            cache.save("mtg-001")

            # Manually rewrite the file with an old cached_at.
            path = (
                Path(tmp) / "cache" / "classifications" / "mtg-001_cache.json"
            )
            data = json.loads(path.read_text())
            stale_ts = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=31)
            ).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            for k in data:
                data[k]["cached_at"] = stale_ts
            path.write_text(json.dumps(data))

            # New instance loads -> entry must be dropped.
            fresh = ClassificationCache(tmp)
            fresh.load("mtg-001")
            self.assertEqual(len(fresh), 0)
            self.assertIsNone(fresh.get(chunk))

    def test_recent_entry_is_kept_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = ClassificationCache(tmp)
            chunk = {"chunk_id": "c1", "text": "fresh entry text"}
            cache.set(chunk, "claim")
            cache.save("mtg-001")

            fresh = ClassificationCache(tmp)
            fresh.load("mtg-001")
            self.assertEqual(fresh.get(chunk), "claim")


class CacheSaveLoadRoundtripTests(unittest.TestCase):
    def test_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = ClassificationCache(tmp)
            chunks = [
                {"chunk_id": f"c{i}", "text": f"unique body {i}"} for i in range(5)
            ]
            for c in chunks:
                cache.set(c, "claim")
            cache.save("mtg-001")

            fresh = ClassificationCache(tmp)
            fresh.load("mtg-001")
            self.assertEqual(len(fresh), 5)
            for c in chunks:
                self.assertEqual(fresh.get(c), "claim")

    def test_per_source_id_files(self) -> None:
        # Cache file is named per source_id so matrix jobs don't fight.
        with tempfile.TemporaryDirectory() as tmp:
            cache_a = ClassificationCache(tmp)
            cache_a.set({"chunk_id": "c1", "text": "alpha"}, "claim")
            cache_a.save("source-a")

            cache_b = ClassificationCache(tmp)
            cache_b.set({"chunk_id": "c1", "text": "beta"}, "decision")
            cache_b.save("source-b")

            files = sorted(
                p.name for p in (Path(tmp) / "cache" / "classifications").iterdir()
            )
            self.assertEqual(
                files, ["source-a_cache.json", "source-b_cache.json"]
            )


class CacheNeverRaisesTests(unittest.TestCase):
    def test_load_corrupt_file_returns_empty_no_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache" / "classifications"
            cache_dir.mkdir(parents=True)
            (cache_dir / "mtg-001_cache.json").write_text("{not valid json")

            fresh = ClassificationCache(tmp)
            fresh.load("mtg-001")  # must NOT raise
            self.assertEqual(len(fresh), 0)

    def test_get_with_non_dict_chunk_returns_none(self) -> None:
        cache = ClassificationCache(tempfile.gettempdir())
        self.assertIsNone(cache.get(None))
        self.assertIsNone(cache.get("some string"))
        self.assertIsNone(cache.get(42))

    def test_set_with_non_dict_chunk_does_not_raise(self) -> None:
        cache = ClassificationCache(tempfile.gettempdir())
        # Must not raise.
        cache.set(None, "claim")
        cache.set("not a dict", "claim")
        # Cache stays empty.
        self.assertEqual(len(cache), 0)

    def test_save_to_unwritable_dir_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = ClassificationCache(tmp)
            cache.set({"chunk_id": "c1", "text": "alpha"}, "claim")
            # Force the path to an unwritable location.
            with mock.patch.object(
                ClassificationCache,
                "_path_for",
                return_value=Path("/nonexistent/dir/cache.json"),
            ):
                cache.save("mtg-001")  # must NOT raise


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
