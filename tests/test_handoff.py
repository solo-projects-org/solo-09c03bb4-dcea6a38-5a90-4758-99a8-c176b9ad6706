"""End-to-end handover tests: real mock-source app served over HTTP.

These tests exercise the actual failure the collector used to have: two
scenarios with *identical* ``source_version``, ``source_revision`` and page
count swapped between two page requests. The collector must either publish one
complete page set or fail the whole batch — never splice two sets together.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Self

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.collector import Collector
from app.config import Settings
from app.main import create_app
from mock_source.server import create_app as create_source_app

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "mock_source" / "scenarios"


class LiveSource:
    """A mock source running on a real socket, so page timing is realistic."""

    def __init__(self, **kwargs) -> None:
        self.app = create_source_app(scenario_dir=SCENARIO_DIR, **kwargs)
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        servers = getattr(self.server, "servers", [])
        if servers:
            host, port = servers[0].sockets[0].getsockname()[:2]
            return f"http://{host}:{port}"
        raise RuntimeError("server did not start")

    def __enter__(self) -> Self:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("mock source did not start")
            time.sleep(0.01)
        return self

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def switch(self, name: str) -> str:
        response = self.client.post("/scenario", json={"name": name})
        assert response.status_code == 200, response.text
        return response.json()["snapshot_id"]

    @property
    def client(self) -> TestClient:
        # TestClient talks to the app object directly; safe while uvicorn also
        # serves it (both only read/write thread-safe in-memory state).
        return TestClient(self.app)


class FlippingFetcher:
    """Fetches page 1, triggers a scenario switch, then fetches page 2.

    This deterministically reproduces a switch between two page requests. A
    collector that only compares version/revision/total_pages cannot tell the
    page sets apart (handoff_a and handoff_b share all three); a collector that
    pins the snapshot id reads page 2 from the original snapshot instead.
    """

    def __init__(self, source: LiveSource, switch_to: str) -> None:
        self.source = source
        self.switch_to = switch_to
        self.snapshot_ids: list[str | None] = []
        self.client = TestClient(source.app)

    def fetch_page(self, page: int, snapshot_id: str | None = None) -> dict:
        if page == 2:
            self.source.switch(self.switch_to)
        params: dict[str, object] = {"page": page}
        if snapshot_id:
            params["snapshot"] = snapshot_id
        response = self.client.get("/announcements", params=params)
        self.snapshot_ids.append(response.json().get("snapshot_id"))
        if response.status_code != 200:
            from app.collector import SourceError

            raise SourceError(
                f"page {page}: source returned HTTP {response.status_code}"
            )
        return response.json()


class RestartingFetcher:
    """Fetches page 1, then swaps the source for a fresh process (new boot id)."""

    def __init__(self, first: LiveSource) -> None:
        self.first = first
        self.restarted = LiveSource()
        self.started = False

    def fetch_page(self, page: int, snapshot_id: str | None = None) -> dict:
        if page == 2 and not self.started:
            self.restarted.__enter__()
            self.started = True
        params: dict[str, object] = {"page": page}
        if snapshot_id:
            params["snapshot"] = snapshot_id
        source = self.first.client if page == 1 else self.restarted.client
        response = source.get("/announcements", params=params)
        if response.status_code == 410:
            from app.collector import SnapshotUnavailableError

            raise SnapshotUnavailableError(
                f"page {page}: snapshot {snapshot_id!r} is no longer available",
                snapshot_id=snapshot_id,
            )
        if response.status_code != 200:
            from app.collector import SourceError

            raise SourceError(f"page {page}: source returned HTTP {response.status_code}")
        return response.json()


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "transit.db")


def make_api(db_path: str):
    settings = Settings(database_path=db_path, source_base_url="http://unused")
    app = create_app(settings)
    return app


def test_old_collection_finishes_from_original_snapshot_after_switch(db_path: str) -> None:
    """A collector that started before the switch reads the old page set completely."""
    with LiveSource() as source:
        source.switch("handoff_a")
        api = make_api(db_path)
        fetcher = FlippingFetcher(source, "handoff_b")
        collector = Collector(api.state.db, fetcher, max_pages=50)

        result = collector.collect()

        assert result.status == "published", result.detail
        # Both pages came from snapshot a, not a mix of a and b.
        assert len(set(fetcher.snapshot_ids)) == 1
        assert result.snapshot_id == fetcher.snapshot_ids[0]

        client = TestClient(api)
        body = client.get(
            "/api/v1/announcements",
            params={"stop_id": "STOP-010", "at": "2026-11-17T11:00:00Z"},
        ).json()
        assert [item["announcement_no"] for item in body["announcements"]] == ["A-4001"]


def test_new_collection_reads_the_new_scenario_immediately(db_path: str) -> None:
    with LiveSource() as source:
        api = make_api(db_path)
        source.switch("handoff_a")
        first = Collector(api.state.db, source_http_fetcher(source), max_pages=50).collect()
        assert first.status == "published"

        source.switch("handoff_b")
        second = Collector(api.state.db, source_http_fetcher(source), max_pages=50).collect()
        # Same version/revision, different content: a conflict, not a mix.
        assert second.status == "failed"
        assert second.http_status == 409
        assert first.snapshot_id != source.client.get("/announcements", params={"page": 1}).json()["snapshot_id"]

        client = TestClient(api)
        assert client.get("/api/v1/current-version").json()["source_version"] == (
            "2026-11-17T10:00:00Z"
        )
        body = client.get(
            "/api/v1/announcements",
            params={"stop_id": "STOP-010", "at": "2026-11-17T11:00:00Z"},
        ).json()
        assert [item["announcement_no"] for item in body["announcements"]] == ["A-4001"]


def source_http_fetcher(source: LiveSource):
    from app.collector import HttpPageFetcher

    return HttpPageFetcher(source.base_url)


def test_mixed_page_sets_cannot_hide_behind_equal_version_labels(db_path: str) -> None:
    """Two page sets with identical labels are only distinguishable by snapshot."""
    with LiveSource() as source:
        source.switch("handoff_a")
        api = make_api(db_path)

        class SplicedFetcher:
            """Deliberately serves page 1 from snapshot A and page 2 from snapshot B.

            This is exactly what the pre-fix collector saw when the source
            switched scenario between the two requests. Both scenarios share
            source_version, source_revision, total_pages and page_size, so all
            legacy consistency checks pass; only the snapshot id reveals it.
            """

            def __init__(self) -> None:
                self.client = TestClient(source.app)
                self.calls: list[dict] = []

            def fetch_page(self, page: int, snapshot_id: str | None = None) -> dict:
                if page != 1:
                    # Serve page 2 from the *new* scenario, as an unpinned
                    # source would after the switch.
                    source.switch("handoff_b")
                    fresh = self.client.get("/announcements", params={"page": 1}).json()
                    response = self.client.get(
                        "/announcements",
                        params={"page": page, "snapshot": fresh["snapshot_id"]},
                    )
                else:
                    response = self.client.get("/announcements", params={"page": 1})
                payload = response.json()
                self.calls.append(payload)
                return payload

        fetcher = SplicedFetcher()
        result = Collector(api.state.db, fetcher, max_pages=50).collect()

        # Sanity: the two pages really do share every legacy label.
        assert len(fetcher.calls) == 2
        assert fetcher.calls[0]["source_version"] == fetcher.calls[1]["source_version"]
        assert fetcher.calls[0]["source_revision"] == fetcher.calls[1]["source_revision"]
        assert fetcher.calls[0]["total_pages"] == fetcher.calls[1]["total_pages"]
        assert fetcher.calls[0]["snapshot_id"] != fetcher.calls[1]["snapshot_id"]

        assert result.status == "failed"
        assert result.http_status == 502
        assert "snapshot" in result.detail


def test_concurrent_switch_while_collection_is_in_flight(db_path: str) -> None:
    """A real over-the-wire collection keeps its snapshot while another switches."""
    with LiveSource() as source:
        source.switch("handoff_a_slow")
        api = make_api(db_path)
        collector = Collector(
            api.state.db, source_http_fetcher(source), max_pages=50
        )

        switched_at: list[float] = []

        def switch_during_collection() -> None:
            # Page 1 takes page_delay_seconds; switch while page 2 waits.
            time.sleep(0.15)
            source.switch("handoff_b")
            switched_at.append(time.monotonic())

        thread = threading.Thread(target=switch_during_collection)
        thread.start()
        result = collector.collect()
        thread.join()

        assert switched_at, "switch did not run"
        assert result.status == "published", result.detail

        client = TestClient(api)
        # Only the old scenario's notices are present; no A-500x notice leaked in.
        for notice, stop in (
            ("A-4001", "STOP-010"),
            ("A-4004", "STOP-013"),
        ):
            body = client.get(
                "/api/v1/announcements",
                params={"stop_id": stop, "at": "2026-11-17T11:00:00Z"},
            ).json()
            assert [item["announcement_no"] for item in body["announcements"]] == [notice]
        leaked = client.get(
            "/api/v1/announcements",
            params={"stop_id": "STOP-013", "at": "2026-11-17T11:00:00Z"},
        ).json()
        assert all(
            item["announcement_no"].startswith("A-4")
            for item in leaked["announcements"]
        )
        assert (
            client.get("/api/v1/current-version").json()["announcement_count"] == 4
        )


def test_source_restart_mid_collection_fails_whole_batch(db_path: str) -> None:
    with LiveSource() as source:
        source.switch("handoff_a")
        api = make_api(db_path)
        # Seed an existing published version that must survive.
        Collector(api.state.db, source_http_fetcher(source), max_pages=50).collect()
        source.switch("v2")
        Collector(api.state.db, source_http_fetcher(source), max_pages=50).collect()
        current_before = TestClient(api).get("/api/v1/current-version").json()

        fetcher = RestartingFetcher(source)
        try:
            source.switch("handoff_a")
            result = Collector(api.state.db, fetcher, max_pages=50).collect()
        finally:
            if fetcher.started:
                fetcher.restarted.__exit__()

        assert result.status == "failed"
        assert result.http_status == 502
        assert "no longer available" in result.detail
        assert result.snapshot_id, "failing result should expose the lost snapshot id"
        client = TestClient(api)
        assert client.get("/api/v1/current-version").json() == current_before


def test_snapshot_flip_between_pages_is_rejected(db_path: str) -> None:
    """A page that echoes a different snapshot id must abort the batch."""
    api = make_api(db_path)

    from tests.test_api import V2_PAGES, FakeFetcher

    class FlipFetcher(FakeFetcher):
        def fetch_page(self, page_no: int) -> dict:
            payload = super().fetch_page(page_no)
            payload = dict(payload)
            payload["snapshot_id"] = "snap-1" if page_no == 1 else "snap-2"
            return payload

    result = Collector(api.state.db, FlipFetcher(V2_PAGES), max_pages=50).collect()
    assert result.status == "failed"
    assert result.http_status == 502
    assert "snapshot" in result.detail


def test_legacy_fetchers_without_snapshot_support_still_work(db_path: str) -> None:
    """Sources predating snapshots keep the label-consistency contract."""
    api = make_api(db_path)
    from tests.test_api import V1_PAGES, FakeFetcher

    result = Collector(api.state.db, FakeFetcher(V1_PAGES), max_pages=50).collect()
    assert result.status == "published"
    assert result.snapshot_id is None
