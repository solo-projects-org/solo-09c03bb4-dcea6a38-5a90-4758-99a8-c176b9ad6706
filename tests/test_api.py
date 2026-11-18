import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.collector import Collector
from app.main import create_app


def page(
    version: str,
    revision: int,
    page_no: int,
    total: int,
    announcements: list[dict],
) -> dict:
    return {
        "source_version": version,
        "source_revision": revision,
        "page": page_no,
        "total_pages": total,
        "announcements": announcements,
    }


class FakeFetcher:
    """Static page map with an optional per-page delay to order concurrent runs."""

    def __init__(self, pages: dict[int, dict], delay: float = 0.0) -> None:
        self.pages = pages
        self.delay = delay
        self.calls: list[int] = []
        self._lock = threading.Lock()

    def fetch_page(self, page_no: int) -> dict:
        with self._lock:
            self.calls.append(page_no)
        if self.delay:
            time.sleep(self.delay)
        if page_no not in self.pages:
            from app.collector import SourceError

            raise SourceError(f"page {page_no}: missing")
        return self.pages[page_no]


def mapping(
    version: str,
    revision: int,
    pages: list[list[dict]],
) -> dict[int, dict]:
    total = len(pages)
    return {
        index: page(version, revision, index, total, items)
        for index, items in enumerate(pages, start=1)
    }


V1_PAGES = mapping(
    "v1",
    1,
    [
        [
            {
                "announcement_no": "A-1001",
                "stop_id": "STOP-001",
                "start_utc": "2026-11-17T08:00:00Z",
                "end_utc": "2026-11-17T12:00:00Z",
            },
            {
                "announcement_no": "A-1002",
                "stop_id": "STOP-001",
                "start_utc": "2026-11-18T08:00:00Z",
                "end_utc": "2026-11-18T10:00:00Z",
            },
        ],
        [
            {
                "announcement_no": "A-1003",
                "stop_id": "STOP-002",
                "start_utc": "2026-11-17T09:00:00Z",
                "end_utc": "2026-11-17T11:00:00Z",
            }
        ],
    ],
)

V2_PAGES = mapping(
    "v2",
    2,
    [
        [
            {
                "announcement_no": "A-2001",
                "stop_id": "STOP-001",
                "start_utc": "2026-11-17T10:00:00Z",
                "end_utc": "2026-11-17T14:00:00Z",
            },
            {
                "announcement_no": "A-1003",
                "stop_id": "STOP-002",
                "start_utc": "2026-11-17T09:00:00Z",
                "end_utc": "2026-11-17T11:00:00Z",
            },
        ],
        [
            {
                "announcement_no": "A-2002",
                "stop_id": "STOP-003",
                "start_utc": "2026-11-17T10:30:00Z",
                "end_utc": "2026-11-17T12:30:00Z",
            }
        ],
    ],
)


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "transit.db")


def make_client(db_path: str, fetcher: FakeFetcher) -> TestClient:
    from app.config import Settings

    settings = Settings(database_path=db_path, source_base_url="http://unused")
    app = create_app(settings)
    app.state.collector = Collector(app.state.db, fetcher, max_pages=50)
    return TestClient(app)


def collect(client: TestClient) -> dict:
    response = client.post("/api/v1/collect")
    return response.json() | {"_status": response.status_code}


def test_complete_page_set_becomes_query_basis(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    result = collect(client)
    assert result["_status"] == 200
    assert result["status"] == "published"
    assert result["source_version"] == "v1"

    response = client.get(
        "/api/v1/announcements", params={"stop_id": "STOP-001", "at": "2026-11-17T09:00:00Z"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source_version"] == "v1"
    assert body["source_revision"] == 1
    assert [item["announcement_no"] for item in body["announcements"]] == ["A-1001"]

    outside = client.get(
        "/api/v1/announcements", params={"stop_id": "STOP-001", "at": "2026-11-17T13:00:00Z"}
    )
    assert outside.json()["announcements"] == []


def test_period_boundaries_are_inclusive(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    collect(client)
    for at in ("2026-11-17T08:00:00Z", "2026-11-17T12:00:00Z"):
        body = client.get(
            "/api/v1/announcements", params={"stop_id": "STOP-001", "at": at}
        ).json()
        assert [item["announcement_no"] for item in body["announcements"]] == ["A-1001"]


def test_naive_timestamp_is_utc(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    collect(client)
    body = client.get(
        "/api/v1/announcements",
        params={"stop_id": "STOP-001", "at": "2026-11-17T09:00:00"},
    ).json()
    assert body["at_utc"] == "2026-11-17T09:00:00Z"


def test_new_version_drops_missing_notices_but_keeps_other_stop_notices(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    collect(client)
    client.app.state.collector.fetcher = FakeFetcher(V2_PAGES)

    result = collect(client)
    assert result["_status"] == 200, result
    assert result["source_version"] == "v2"

    # A-1001 existed only in v1 and must be gone.
    body = client.get(
        "/api/v1/announcements", params={"stop_id": "STOP-001", "at": "2026-11-17T09:00:00Z"}
    ).json()
    assert body["announcements"] == []
    assert body["source_version"] == "v2"

    # Another stop's notice survives the switch because it is still in v2.
    body = client.get(
        "/api/v1/announcements", params={"stop_id": "STOP-002", "at": "2026-11-17T10:00:00Z"}
    ).json()
    assert [item["announcement_no"] for item in body["announcements"]] == ["A-1003"]

    # A stop that has both an old and a new notice only sees the current version.
    body = client.get(
        "/api/v1/announcements", params={"stop_id": "STOP-001", "at": "2026-11-17T11:00:00Z"}
    ).json()
    assert [item["announcement_no"] for item in body["announcements"]] == ["A-2001"]


def test_version_flip_between_pages_fails_and_keeps_old_data(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    collect(client)

    broken = dict(V2_PAGES)
    broken[2] = dict(V2_PAGES[2], source_version="v2-rogue")
    client.app.state.collector.fetcher = FakeFetcher(broken)
    result = collect(client)
    assert result["_status"] == 502
    assert result["status"] == "failed"
    assert "version" in result["detail"]

    version = client.get("/api/v1/current-version").json()
    assert version["source_version"] == "v1"


def test_missing_page_fails_and_keeps_old_data(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    collect(client)
    broken = dict(V2_PAGES)
    del broken[2]
    client.app.state.collector.fetcher = FakeFetcher(broken)
    result = collect(client)
    assert result["_status"] == 502
    assert client.get("/api/v1/current-version").json()["source_version"] == "v1"


def test_short_page_set_fails(db_path: str) -> None:
    # Page 1 claims 3 pages, but only 2 exist.
    broken = dict(V2_PAGES)
    broken[1] = dict(broken[1], total_pages=3)
    client = make_client(db_path, FakeFetcher(broken))
    result = collect(client)
    assert result["_status"] == 502
    assert client.get("/api/v1/current-version").status_code == 404


def test_duplicate_number_conflict_fails(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    collect(client)
    broken = dict(V2_PAGES)
    conflicting = dict(V2_PAGES[1]["announcements"][0])
    conflicting["end_utc"] = "2026-11-17T23:00:00Z"
    broken[2] = dict(broken[2], announcements=list(broken[2]["announcements"]) + [conflicting])
    client.app.state.collector.fetcher = FakeFetcher(broken)
    result = collect(client)
    assert result["_status"] == 502
    assert "more than once" in result["detail"]
    assert client.get("/api/v1/current-version").json()["source_version"] == "v1"


def test_identical_duplicates_are_deduplicated(db_path: str) -> None:
    pages = dict(V1_PAGES)
    repeated = dict(pages[1]["announcements"][0])
    pages[2] = dict(pages[2], announcements=list(pages[2]["announcements"]) + [repeated])
    client = make_client(db_path, FakeFetcher(pages))
    result = collect(client)
    assert result["_status"] == 200
    assert result["version"]["announcement_count"] == 3


def test_same_version_retry_does_not_republish(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    first = collect(client)
    second = collect(client)
    assert first["status"] == "published"
    assert second["_status"] == 200
    assert second["status"] == "already_current"
    assert second["version"]["published_at"] == first["version"]["published_at"]

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM published_version").fetchone()[0] == 1
        attempts = conn.execute(
            "SELECT outcome FROM collection_attempt ORDER BY id"
        ).fetchall()
    assert [row[0] for row in attempts] == ["published", "noop"]


def test_same_version_with_changed_content_is_rejected(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    collect(client)
    changed = mapping(
        "v1",
        1,
        [[dict(V1_PAGES[1]["announcements"][0], end_utc="2026-11-17T13:00:00Z")]],
    )
    client.app.state.collector.fetcher = FakeFetcher(changed)
    result = collect(client)
    assert result["_status"] == 409
    assert "different content" in result["detail"]
    assert client.get("/api/v1/current-version").json()["source_version"] == "v1"


def test_old_revision_cannot_overwrite_newer(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V2_PAGES))
    collect(client)
    client.app.state.collector.fetcher = FakeFetcher(V1_PAGES)
    result = collect(client)
    assert result["_status"] == 409
    body = client.get(
        "/api/v1/announcements", params={"stop_id": "STOP-002", "at": "2026-11-17T10:00:00Z"}
    ).json()
    assert body["source_version"] == "v2"


def test_identical_old_version_cannot_roll_back(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    collect(client)
    client.app.state.collector.fetcher = FakeFetcher(V2_PAGES)
    collect(client)
    client.app.state.collector.fetcher = FakeFetcher(V1_PAGES)
    result = collect(client)
    assert result["_status"] == 409
    assert client.get("/api/v1/current-version").json()["source_version"] == "v2"


def test_slow_older_collector_cannot_overwrite_newer(db_path: str) -> None:
    """v2 starts first but finishes last; v3 must remain current."""
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    collect(client)

    slow_v2 = FakeFetcher(V2_PAGES, delay=0.4)
    fast_v3_pages = mapping(
        "v3",
        3,
        [
            [
                {
                    "announcement_no": "A-3001",
                    "stop_id": "STOP-001",
                    "start_utc": "2026-11-17T11:00:00Z",
                    "end_utc": "2026-11-17T13:00:00Z",
                }
            ]
        ],
    )
    fast_v3 = FakeFetcher(fast_v3_pages, delay=0.0)

    results: dict[str, dict] = {}

    def run(name: str, fetcher: FakeFetcher) -> None:
        db = client.app.state.db
        collector = Collector(db, fetcher, max_pages=50)
        results[name] = collector.collect()

    thread = threading.Thread(target=run, args=("v2", slow_v2))
    thread.start()
    time.sleep(0.05)
    run("v3", fast_v3)
    thread.join()

    assert results["v3"].status == "published"
    # v2 may legitimately be published only if it committed before v3; here it
    # is forced to finish later and must be rejected.
    assert results["v2"].status == "failed"
    assert results["v2"].http_status == 409

    current = client.get("/api/v1/current-version").json()
    assert current["source_version"] == "v3"
    body = client.get(
        "/api/v1/announcements", params={"stop_id": "STOP-001", "at": "2026-11-17T12:00:00Z"}
    ).json()
    assert body["source_version"] == "v3"
    assert [item["announcement_no"] for item in body["announcements"]] == ["A-3001"]


def test_published_data_survives_restart(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    collect(client)
    client.close()

    # Fresh app object, same DB file: simulates a process restart.
    restarted = make_client(db_path, FakeFetcher({}))
    version = restarted.get("/api/v1/current-version").json()
    assert version["source_version"] == "v1"
    body = restarted.get(
        "/api/v1/announcements", params={"stop_id": "STOP-001", "at": "2026-11-17T09:00:00Z"}
    ).json()
    assert body["source_version"] == "v1"
    assert [item["announcement_no"] for item in body["announcements"]] == ["A-1001"]


def test_query_before_any_publish_returns_404(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher({}))
    assert client.get(
        "/api/v1/announcements", params={"stop_id": "STOP-001", "at": "2026-11-17T09:00:00Z"}
    ).status_code == 404
    assert client.get("/api/v1/current-version").status_code == 404


def test_invalid_at_returns_422(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    collect(client)
    response = client.get(
        "/api/v1/announcements", params={"stop_id": "STOP-001", "at": "yesterday"}
    )
    assert response.status_code == 422


def test_source_outage_keeps_old_data(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    collect(client)

    class Down:
        def fetch_page(self, page: int) -> dict:
            from app.collector import SourceError

            raise SourceError(f"page {page}: source request failed: connection refused")

    client.app.state.collector.fetcher = Down()
    result = collect(client)
    assert result["_status"] == 502
    assert client.get("/api/v1/current-version").json()["source_version"] == "v1"


def test_openapi_and_docs_available(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher({}))
    spec = client.get("/openapi.json").json()
    assert "/api/v1/collect" in spec["paths"]
    assert "/api/v1/announcements" in spec["paths"]
    assert client.get("/docs").status_code == 200


def test_revision_flip_between_pages_fails(db_path: str) -> None:
    broken = dict(V2_PAGES)
    broken[2] = dict(V2_PAGES[2], source_revision=99)
    client = make_client(db_path, FakeFetcher(broken))
    result = collect(client)
    assert result["_status"] == 502
    assert "revision" in result["detail"]


def test_total_pages_flip_between_pages_fails(db_path: str) -> None:
    broken = dict(V2_PAGES)
    broken[2] = dict(V2_PAGES[2], total_pages=5)
    client = make_client(db_path, FakeFetcher(broken))
    result = collect(client)
    assert result["_status"] == 502
    assert "total_pages" in result["detail"]


def test_page_index_mismatch_fails(db_path: str) -> None:
    broken = dict(V2_PAGES)
    broken[2] = dict(V2_PAGES[2], page=1)
    client = make_client(db_path, FakeFetcher(broken))
    result = collect(client)
    assert result["_status"] == 502
    assert "labelled" in result["detail"]


def test_invalid_period_fails_collection(db_path: str) -> None:
    broken = mapping(
        "v1",
        1,
        [
            [
                {
                    "announcement_no": "A-1001",
                    "stop_id": "STOP-001",
                    "start_utc": "2026-11-17T12:00:00Z",
                    "end_utc": "2026-11-17T08:00:00Z",
                }
            ]
        ],
    )
    client = make_client(db_path, FakeFetcher(broken))
    result = collect(client)
    assert result["_status"] == 502
    assert "start_utc" in result["detail"]


def test_failed_attempts_are_audited(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    collect(client)
    broken = dict(V2_PAGES)
    del broken[2]
    client.app.state.collector.fetcher = FakeFetcher(broken)
    collect(client)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT outcome, source_version FROM collection_attempt ORDER BY id"
        ).fetchall()
    assert [row[0] for row in rows] == ["published", "failed"]
    assert rows[1][1] is None


def test_concurrent_identical_collections_publish_once(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES, delay=0.05))
    results: list[tuple[str, int]] = []
    lock = threading.Lock()

    def run() -> None:
        collector = Collector(client.app.state.db, client.app.state.collector.fetcher, max_pages=50)
        outcome = collector.collect()
        with lock:
            results.append((outcome.status, outcome.http_status))

    threads = [threading.Thread(target=run) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(status for status, _ in results) == ["already_current"] * 5 + ["published"]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM published_version").fetchone()[0] == 1


def test_source_without_revision_orders_by_version_label(db_path: str) -> None:
    """Sources that omit source_revision still advance by version label."""
    first = mapping(
        "2026-11-17T06:00:00Z",
        0,
        [
            [
                {
                    "announcement_no": "A-1",
                    "stop_id": "STOP-9",
                    "start_utc": "2026-11-17T08:00:00Z",
                    "end_utc": "2026-11-17T09:00:00Z",
                }
            ]
        ],
    )
    second = mapping(
        "2026-11-17T07:00:00Z",
        0,
        [
            [
                {
                    "announcement_no": "A-2",
                    "stop_id": "STOP-9",
                    "start_utc": "2026-11-17T08:00:00Z",
                    "end_utc": "2026-11-17T09:00:00Z",
                }
            ]
        ],
    )
    client = make_client(db_path, FakeFetcher(first))
    assert collect(client)["status"] == "published"
    client.app.state.collector.fetcher = FakeFetcher(second)
    assert collect(client)["status"] == "published"
    body = client.get(
        "/api/v1/announcements", params={"stop_id": "STOP-9", "at": "2026-11-17T08:30:00Z"}
    ).json()
    assert [item["announcement_no"] for item in body["announcements"]] == ["A-2"]

    # Going back to the earlier label is a rollback and must be refused.
    client.app.state.collector.fetcher = FakeFetcher(first)
    result = collect(client)
    assert result["_status"] == 409


def test_offset_timestamp_is_converted_to_utc(db_path: str) -> None:
    client = make_client(db_path, FakeFetcher(V1_PAGES))
    collect(client)
    body = client.get(
        "/api/v1/announcements",
        params={"stop_id": "STOP-001", "at": "2026-11-17T17:00:00+08:00"},
    ).json()
    assert body["at_utc"] == "2026-11-17T09:00:00Z"
    assert [item["announcement_no"] for item in body["announcements"]] == ["A-1001"]
