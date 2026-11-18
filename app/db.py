"""SQLite persistence for published announcement versions and their notices.

The database file is the single source of truth: once a version is published it
survives process restarts, and the latest *published* version remains the
query basis until a newer complete page set is atomically published.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from .models import CandidateSet, us_to_iso, utc_now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS published_version (
    source_version    TEXT PRIMARY KEY,
    source_revision   INTEGER NOT NULL,
    total_pages       INTEGER NOT NULL,
    announcement_count INTEGER NOT NULL,
    content_hash      TEXT NOT NULL,
    published_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS published_announcement (
    source_version   TEXT NOT NULL REFERENCES published_version(source_version)
                     ON DELETE CASCADE,
    announcement_no  TEXT NOT NULL,
    stop_id          TEXT NOT NULL,
    start_us         INTEGER NOT NULL,
    end_us           INTEGER NOT NULL,
    PRIMARY KEY (source_version, announcement_no)
);

CREATE INDEX IF NOT EXISTS idx_published_announcement_stop
    ON published_announcement (source_version, stop_id, start_us, end_us);

CREATE TABLE IF NOT EXISTS current_version (
    singleton     INTEGER PRIMARY KEY CHECK (singleton = 1),
    source_version TEXT NOT NULL REFERENCES published_version(source_version),
    switched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_attempt (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    source_version  TEXT,
    source_revision INTEGER,
    detail          TEXT
);
"""


class PublishConflict(RuntimeError):
    """Raised when a candidate can no longer be published safely."""


@dataclass(frozen=True)
class PublishedVersion:
    source_version: str
    source_revision: int
    total_pages: int
    announcement_count: int
    content_hash: str
    published_at: str


@dataclass(frozen=True)
class QueryHit:
    announcement_no: str
    stop_id: str
    start_us: int
    end_us: int
    source_version: str
    source_revision: int

    @property
    def start_utc(self) -> str:
        return us_to_iso(self.start_us)

    @property
    def end_utc(self) -> str:
        return us_to_iso(self.end_us)


class Database:
    """Thread-safe connection manager.

    FastAPI runs sync endpoints in a thread pool, so every checkout opens a
    fresh connection; SQLite's own locking plus WAL keeps writers serialised.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._initialised = False

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialise(self) -> None:
        with self._lock:
            if self._initialised:
                return
            conn = self._connect()
            try:
                # WAL is a persistent database property, so set it once here
                # rather than on every connection (changing it needs an
                # exclusive lock and would fail during concurrent startup).
                deadline = time.monotonic() + 30.0
                while True:
                    try:
                        conn.execute("PRAGMA journal_mode=WAL")
                        break
                    except sqlite3.OperationalError as exc:
                        if "locked" not in str(exc) or time.monotonic() > deadline:
                            raise
                        time.sleep(0.05)
                conn.executescript(SCHEMA)
            finally:
                conn.close()
            self._initialised = True

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.initialise()
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.initialise()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    # ---------------------------------------------------------------- reads

    def current_version(self) -> PublishedVersion | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT v.*
                  FROM current_version c
                  JOIN published_version v ON v.source_version = c.source_version
                 WHERE c.singleton = 1
                """
            ).fetchone()
        return _version_from_row(row) if row else None

    def _current_row(self, conn: sqlite3.Connection) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT v.*
              FROM current_version c
              JOIN published_version v ON v.source_version = c.source_version
             WHERE c.singleton = 1
            """
        ).fetchone()

    def query_stop(self, stop_id: str, at_us: int) -> tuple[PublishedVersion, list[QueryHit]]:
        with self.connection() as conn:
            # One deferred read transaction gives both statements a single
            # snapshot, so a concurrent publish can never mix two versions.
            conn.execute("BEGIN")
            try:
                current = self._current_row(conn)
                if current is None:
                    raise LookupError("no announcement version has been published yet")
                rows = conn.execute(
                    """
                    SELECT announcement_no, stop_id, start_us, end_us, source_version
                      FROM published_announcement
                     WHERE source_version = ? AND stop_id = ?
                       AND start_us <= ? AND end_us >= ?
                     ORDER BY announcement_no
                    """,
                    (current["source_version"], stop_id, at_us, at_us),
                ).fetchall()
            finally:
                conn.execute("COMMIT")
        version = _version_from_row(current)
        return version, _hits_from_rows(rows, version.source_revision)

    # --------------------------------------------------------------- writes

    def publish(
        self,
        candidate: CandidateSet,
        *,
        started_at: str,
    ) -> tuple[PublishedVersion, bool]:
        """Atomically publish a candidate and make it current.

        * A version already published with identical content is a no-op
          (idempotent retry); it is not re-published and does not become
          current if it was superseded in the meantime.
        * A version already published with different content raises
          PublishConflict.
        * Revisions only move forward: a candidate with a revision older than
          the current one raises PublishConflict, so a slow collector that
          started earlier can never overwrite a newer one.

        Returns (version, changed) where changed is False for an idempotent
        retry of the already-current version.
        """
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM published_version WHERE source_version = ?",
                (candidate.source_version,),
            ).fetchone()
            current = conn.execute(
                "SELECT source_version FROM current_version WHERE singleton = 1"
            ).fetchone()
            current_version = current["source_version"] if current else None

            if existing is not None:
                record = _version_from_row(existing)
                if record.content_hash != candidate.content_hash:
                    raise PublishConflict(
                        f"source_version {candidate.source_version!r} was already published "
                        "with different content"
                    )
                if current_version != candidate.source_version:
                    raise PublishConflict(
                        f"source_version {candidate.source_version!r} is identical to an "
                        f"already-published version but was superseded by "
                        f"{current_version!r}; refusing to roll the query basis back"
                    )
                self._record_attempt(
                    conn,
                    started_at=started_at,
                    outcome="noop",
                    candidate=candidate,
                    detail="identical version already current; not republished",
                )
                return record, False

            self._ensure_newer(conn, candidate)

            conn.execute(
                """
                INSERT INTO published_version
                    (source_version, source_revision, total_pages,
                     announcement_count, content_hash, published_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.source_version,
                    candidate.source_revision,
                    candidate.total_pages,
                    len(candidate.announcements),
                    candidate.content_hash,
                    utc_now_iso(),
                ),
            )
            conn.executemany(
                """
                INSERT INTO published_announcement
                    (source_version, announcement_no, stop_id, start_us, end_us)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        candidate.source_version,
                        item.announcement_no,
                        item.stop_id,
                        item.start_us,
                        item.end_us,
                    )
                    for item in candidate.announcements
                ],
            )
            conn.execute(
                """
                INSERT INTO current_version (singleton, source_version, switched_at)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    source_version = excluded.source_version,
                    switched_at = excluded.switched_at
                """,
                (candidate.source_version, utc_now_iso()),
            )
            self._record_attempt(
                conn,
                started_at=started_at,
                outcome="published",
                candidate=candidate,
                detail=f"{len(candidate.announcements)} announcements published",
            )
            row = conn.execute(
                "SELECT * FROM published_version WHERE source_version = ?",
                (candidate.source_version,),
            ).fetchone()
            return _version_from_row(row), True

    def _ensure_newer(self, conn: sqlite3.Connection, candidate: CandidateSet) -> None:
        current = conn.execute(
            """
            SELECT v.source_version, v.source_revision
              FROM current_version c
              JOIN published_version v ON v.source_version = c.source_version
             WHERE c.singleton = 1
            """
        ).fetchone()
        if current is None:
            return
        if not candidate.is_newer_than(current["source_revision"], current["source_version"]):
            raise PublishConflict(
                "candidate "
                f"(revision {candidate.source_revision}, version "
                f"{candidate.source_version!r}) is not newer than current "
                f"(revision {current['source_revision']}, version "
                f"{current['source_version']!r})"
            )

    def record_failed_attempt(
        self,
        *,
        started_at: str,
        source_version: str | None,
        source_revision: int | None,
        detail: str,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO collection_attempt
                    (started_at, finished_at, outcome, source_version,
                     source_revision, detail)
                VALUES (?, ?, 'failed', ?, ?, ?)
                """,
                (started_at, utc_now_iso(), source_version, source_revision, detail),
            )

    @staticmethod
    def _record_attempt(
        conn: sqlite3.Connection,
        *,
        started_at: str,
        outcome: str,
        candidate: CandidateSet,
        detail: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO collection_attempt
                (started_at, finished_at, outcome, source_version,
                 source_revision, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                utc_now_iso(),
                outcome,
                candidate.source_version,
                candidate.source_revision,
                detail,
            ),
        )


def _version_from_row(row: sqlite3.Row) -> PublishedVersion:
    return PublishedVersion(
        source_version=row["source_version"],
        source_revision=row["source_revision"],
        total_pages=row["total_pages"],
        announcement_count=row["announcement_count"],
        content_hash=row["content_hash"],
        published_at=row["published_at"],
    )


def _hits_from_rows(rows: list[sqlite3.Row], source_revision: int) -> list[QueryHit]:
    return [
        QueryHit(
            announcement_no=row["announcement_no"],
            stop_id=row["stop_id"],
            start_us=row["start_us"],
            end_us=row["end_us"],
            source_version=row["source_version"],
            source_revision=source_revision,
        )
        for row in rows
    ]
