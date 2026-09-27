"""Persistent, versioned cache for expensive music-recognition work."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any


CACHE_SCHEMA_VERSION = 1


def default_cache_path() -> Path:
    """Return the platform-appropriate per-user cache location."""
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "create-metadata" / "cache.sqlite3"


def stable_key(value: str) -> str:
    """Keep large fingerprints and URLs out of SQLite primary keys."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_cache_key(path: Path) -> str:
    """Identify the current bytes of a file without reading them again."""
    stat = path.stat()
    identity = f"{path.resolve()}\0{stat.st_size}\0{stat.st_mtime_ns}"
    return stable_key(identity)


class RecognitionCache:
    """Thread-safe JSON cache with TTLs and atomic SQLite writes."""

    def __init__(
        self,
        path: Path,
        *,
        ttl_seconds: float,
        refresh: bool = False,
        clock: Any = time.time,
    ) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self.refresh = refresh
        self.clock = clock
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None
        self._written: set[tuple[str, str]] = set()
        self.hits = 0
        self.misses = 0

        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_entries (
                namespace TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                created REAL NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (namespace, cache_key)
            )
            """
        )
        connection.execute(f"PRAGMA user_version = {CACHE_SCHEMA_VERSION}")
        connection.commit()
        self._connection = connection

    def get(self, namespace: str, key: str, *, permanent: bool = False) -> Any | None:
        cache_key = stable_key(key)
        with self._lock:
            connection = self._connection
            if connection is None or (
                self.refresh
                and not permanent
                and (namespace, cache_key) not in self._written
            ):
                self.misses += 1
                return None
            try:
                row = connection.execute(
                    "SELECT created, payload FROM cache_entries "
                    "WHERE namespace = ? AND cache_key = ?",
                    (namespace, cache_key),
                ).fetchone()
            except sqlite3.Error:
                connection.close()
                self._connection = None
                self.misses += 1
                return None
        if row is None:
            self.misses += 1
            return None
        created, payload = row
        if not permanent and self.clock() - float(created) > self.ttl_seconds:
            self.misses += 1
            return None
        try:
            value = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, namespace: str, key: str, value: Any) -> None:
        try:
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return
        cache_key = stable_key(key)
        with self._lock:
            connection = self._connection
            if connection is None:
                return
            try:
                connection.execute(
                    """
                    INSERT INTO cache_entries(namespace, cache_key, created, payload)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(namespace, cache_key) DO UPDATE SET
                        created = excluded.created,
                        payload = excluded.payload
                    """,
                    (namespace, cache_key, self.clock(), payload),
                )
                connection.commit()
            except sqlite3.Error:
                connection.close()
                self._connection = None
                return
            self._written.add((namespace, cache_key))

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> RecognitionCache:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
