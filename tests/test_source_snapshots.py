"""Snapshot handover tests for the mock source itself."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from mock_source.server import SnapshotStore, create_app

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "mock_source" / "scenarios"


def make_source(**kwargs) -> TestClient:
    return TestClient(create_app(scenario_dir=SCENARIO_DIR, **kwargs))


def first_page(client: TestClient) -> dict:
    response = client.get("/announcements", params={"page": 1})
    assert response.status_code == 200, response.text
    return response.json()


def test_page_one_returns_snapshot_id_and_continuation_is_pinned() -> None:
    client = make_source()
    page1 = first_page(client)
    assert page1["snapshot_id"]

    page2 = client.get(
        "/announcements", params={"page": 2, "snapshot": page1["snapshot_id"]}
    )
    assert page2.status_code == 200
    assert page2.json()["snapshot_id"] == page1["snapshot_id"]


def test_page_above_one_without_snapshot_is_rejected() -> None:
    client = make_source()
    response = client.get("/announcements", params={"page": 2})
    assert response.status_code == 400
    assert "snapshot_id" in response.json()["detail"]


def test_switching_scenario_does_not_change_an_in_flight_snapshot() -> None:
    client = make_source()
    page1 = first_page(client)
    snapshot_id = page1["snapshot_id"]

    switched = client.post("/scenario", json={"name": "v2"})
    assert switched.status_code == 200
    new_snapshot = switched.json()["snapshot_id"]
    assert new_snapshot != snapshot_id

    # The in-flight page set keeps reading the old scenario ...
    old_page2 = client.get(
        "/announcements", params={"page": 2, "snapshot": snapshot_id}
    )
    assert old_page2.status_code == 200
    assert old_page2.json()["snapshot_id"] == snapshot_id
    assert [item["announcement_no"] for item in old_page2.json()["announcements"]] == [
        "A-1003"
    ]

    # ... while a new page set immediately sees the new scenario.
    new_page1 = first_page(client)
    assert new_page1["snapshot_id"] == new_snapshot
    assert new_page1["source_version"] == "2026-11-17T09:00:00Z"


def test_unknown_snapshot_returns_gone() -> None:
    client = make_source()
    response = client.get(
        "/announcements", params={"page": 2, "snapshot": "does-not-exist"}
    )
    assert response.status_code == 410


def test_snapshot_ids_are_not_reused_after_a_source_restart() -> None:
    scenario_dir = Path(__file__).resolve().parent.parent / "mock_source" / "scenarios"
    first = TestClient(create_app(scenario_dir=scenario_dir))
    snapshot_id = first_page(first)["snapshot_id"]
    first.close()

    # A new process has a new boot id and an empty history.
    restarted = TestClient(create_app(scenario_dir=scenario_dir))
    response = restarted.get(
        "/announcements", params={"page": 2, "snapshot": snapshot_id}
    )
    assert response.status_code == 410
    assert "no longer available" in response.json()["detail"]


def test_snapshot_eviction_fails_continuation() -> None:
    scenario_dir = Path(__file__).resolve().parent.parent / "mock_source" / "scenarios"
    client = TestClient(create_app(scenario_dir=scenario_dir, snapshot_history=2))
    snapshot_id = first_page(client)["snapshot_id"]
    # Two more switches evict the first snapshot from the 2-entry history.
    client.post("/scenario", json={"name": "v2"})
    client.post("/scenario", json={"name": "v3"})
    response = client.get(
        "/announcements", params={"page": 2, "snapshot": snapshot_id}
    )
    assert response.status_code == 410


def test_snapshot_store_is_bounded_and_ids_are_unique() -> None:
    store = SnapshotStore(history=2)
    ids = [store.create(f"s{i}", {"announcements": []}).snapshot_id for i in range(5)]
    assert len(set(ids)) == 5
    assert store.get(ids[0]) is None
    assert store.get(ids[-1]) is not None
    assert len(store.list()) == 2


def test_pinned_route_keeps_its_snapshot_across_global_switch() -> None:
    client = make_source()
    pinned1 = client.get("/pinned/slow_v2/announcements", params={"page": 1}).json()
    client.post("/scenario", json={"name": "v3"})
    pinned2 = client.get(
        "/pinned/slow_v2/announcements",
        params={"page": 2, "snapshot": pinned1["snapshot_id"]},
    )
    assert pinned2.status_code == 200
    assert pinned2.json()["snapshot_id"] == pinned1["snapshot_id"]
    assert pinned2.json()["source_version"] == "2026-11-17T09:00:00Z"


def test_pinned_route_rejects_foreign_snapshot() -> None:
    client = make_source()
    v1_snapshot = first_page(client)["snapshot_id"]
    response = client.get(
        "/pinned/v2/announcements",
        params={"page": 2, "snapshot": v1_snapshot},
    )
    assert response.status_code == 400


def test_same_labels_but_different_content_get_distinct_snapshots() -> None:
    """handoff_a and handoff_b share version/revision/page count."""
    client = make_source()
    client.post("/scenario", json={"name": "handoff_a"})
    a = first_page(client)

    client.post("/scenario", json={"name": "handoff_b"})
    b = first_page(client)

    assert a["source_version"] == b["source_version"]
    assert a["source_revision"] == b["source_revision"]
    assert a["total_pages"] == b["total_pages"]
    assert a["snapshot_id"] != b["snapshot_id"]
    assert (
        client.get("/announcements", params={"page": 1, "snapshot": a["snapshot_id"]})
        .json()["announcements"][0]["announcement_no"]
        == "A-4001"
    )
    assert (
        client.get("/announcements", params={"page": 1, "snapshot": b["snapshot_id"]})
        .json()["announcements"][0]["announcement_no"]
        == "A-5001"
    )


def test_switching_with_inline_scenario_freezes_a_new_snapshot() -> None:
    client = make_source()
    scenario = {
        "name": "inline_check",
        "source_version": "inline-v1",
        "source_revision": 1,
        "page_size": 2,
        "announcements": [
            {
                "announcement_no": "A-9001",
                "stop_id": "STOP-900",
                "start_utc": "2026-11-17T08:00:00Z",
                "end_utc": "2026-11-17T09:00:00Z",
            }
        ],
    }
    response = client.post("/scenario", json={"scenario": scenario})
    assert response.status_code == 200
    snapshot_id = response.json()["snapshot_id"]
    served = client.get("/announcements", params={"page": 1}).json()
    assert served["snapshot_id"] == snapshot_id
    assert served["source_version"] == "inline-v1"


def test_scenario_files_are_loaded_on_start(tmp_path: Path) -> None:
    path = tmp_path / "custom.json"
    path.write_text(
        json.dumps(
            {
                "source_version": "custom-v1",
                "source_revision": 1,
                "page_size": 1,
                "announcements": [
                    {
                        "announcement_no": "A-1",
                        "stop_id": "STOP-1",
                        "start_utc": "2026-11-17T08:00:00Z",
                        "end_utc": "2026-11-17T09:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(scenario_dir=tmp_path, default_scenario="custom"))
    assert client.get("/announcements", params={"page": 1}).json()["source_version"] == "custom-v1"
