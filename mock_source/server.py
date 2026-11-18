"""Switchable, paginated mock announcement source with consistent snapshots.

Serves ``GET /announcements?page=N`` from an immutable *snapshot* of a scenario.
Every snapshot is frozen when it is created and is identified by a unique
``snapshot_id`` which is echoed on every page.

Pagination contract
-------------------
1. ``GET /announcements?page=1`` (no ``snapshot`` parameter) starts a page set
   and returns the ``snapshot_id`` of the snapshot it was served from.
2. Every further page must be requested as
   ``GET /announcements?page=N&snapshot=<snapshot_id>``. A page request that
   continues an existing page set (``page > 1``) without a snapshot token is
   rejected with HTTP 400: without the token the source cannot promise that
   the pages belong to the same scenario.
3. A snapshot is independent of ``POST /scenario``. Switching the active
   scenario only changes which snapshot *new* page sets start from; page sets
   already in flight keep reading their original snapshot until it is evicted.
4. Snapshot ids embed a per-process boot id. After a source restart an old id
   is unknown and the source answers HTTP 410 — the collector must fail the
   whole batch instead of mixing a restarted source's pages into it.

Switch modes (POST /scenario, JSON/form fields ``name``, ``path`` or
``scenario``):

* normal          — every page carries the scenario's version.
* mixed_versions  — page ``fault_page`` claims version ``fault_version``.
* missing_page    — page ``fault_page`` returns HTTP 404.
* short_pages     — first page claims ``total_pages + 1``, so one page is absent.
* duplicate_conflict — page ``fault_page`` repeats an announcement number from
  page 1 with different content.
* delay           — every page request sleeps ``delay_seconds`` before replying
  (used to force concurrent collectors with opposite finish order).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

SCENARIO_DIR = Path(os.getenv("SCENARIO_DIR", Path(__file__).parent / "scenarios"))
DEFAULT_SCENARIO = os.getenv("DEFAULT_SCENARIO", "v1")


def _env_history() -> int:
    raw = os.getenv("SNAPSHOT_HISTORY", "128")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"SNAPSHOT_HISTORY must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError("SNAPSHOT_HISTORY must be at least 1")
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Snapshot:
    """An immutable copy of one scenario, served under one ``snapshot_id``."""

    snapshot_id: str
    scenario_name: str
    created_at: str
    scenario: dict[str, Any]


class SnapshotStore:
    """Bounded, in-memory store of recent scenarios.

    Ids are ``<boot_id>-<counter>``. The boot id is random per process, so an
    id from a previous process can never resolve to some unrelated snapshot
    after a restart: it is simply unknown (the endpoint answers 410).
    """

    def __init__(self, history: int) -> None:
        if history < 1:
            raise ValueError("snapshot history must be at least 1")
        self._history = history
        self._lock = threading.Lock()
        self._snapshots: "OrderedDict[str, Snapshot]" = OrderedDict()
        self._boot_id = uuid.uuid4().hex
        self._counter = 0

    @property
    def boot_id(self) -> str:
        return self._boot_id

    def create(self, scenario_name: str, scenario: dict[str, Any]) -> Snapshot:
        with self._lock:
            self._counter += 1
            snapshot = Snapshot(
                snapshot_id=f"{self._boot_id[:12]}-{self._counter}",
                scenario_name=scenario_name,
                created_at=_now_iso(),
                scenario=copy.deepcopy(scenario),
            )
            self._snapshots[snapshot.snapshot_id] = snapshot
            while len(self._snapshots) > self._history:
                self._snapshots.popitem(last=False)
            return snapshot

    def get(self, snapshot_id: str) -> Snapshot | None:
        with self._lock:
            return self._snapshots.get(snapshot_id)

    def list(self) -> list[Snapshot]:
        with self._lock:
            return list(reversed(self._snapshots.values()))


class ScenarioRegistry:
    """Scenario files on disk plus scenarios registered inline via the API."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self._lock = threading.Lock()
        self._scenarios: dict[str, dict[str, Any]] = {}
        self.refresh()

    def refresh(self) -> list[str]:
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            for path in sorted(self.directory.glob("*.json")):
                self._scenarios[path.stem] = json.loads(
                    path.read_text(encoding="utf-8")
                )
            return sorted(self._scenarios)

    def get(self, name: str) -> dict[str, Any]:
        with self._lock:
            scenario = self._scenarios.get(name)
        if scenario is None:
            raise HTTPException(status_code=404, detail=f"unknown scenario {name!r}")
        return scenario

    def put(self, name: str, scenario: dict[str, Any]) -> None:
        with self._lock:
            self._scenarios[name] = scenario

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._scenarios)


def _load_scenario_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _page_payload(snapshot: Snapshot, page: int) -> tuple[int, dict[str, Any]]:
    """Render one page strictly from the frozen snapshot."""
    scenario = snapshot.scenario
    mode = scenario.get("mode", "normal")
    page_size = max(1, int(scenario.get("page_size", 2)))
    items = list(scenario.get("announcements", []))
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    reported_total = total_pages
    fault_page = int(scenario.get("fault_page", 1))
    version = scenario.get("source_version", snapshot.scenario_name)
    revision = scenario.get("source_revision", 1)

    if mode == "missing_page" and page == fault_page:
        return 404, {"detail": f"page {page} does not exist"}

    if mode == "short_pages":
        reported_total = total_pages + 1

    if page > total_pages:
        return 404, {"detail": f"page {page} is beyond total pages"}

    start = (page - 1) * page_size
    page_items = items[start : start + page_size]

    if mode == "mixed_versions" and page == fault_page:
        version = scenario.get("fault_version", "rogue-version")

    if mode == "duplicate_conflict" and page == fault_page and page != 1:
        duplicated = dict(items[0])
        duplicated["end_utc"] = scenario.get(
            "conflict_end_utc", "2026-11-17T23:59:00Z"
        )
        page_items = list(page_items) + [duplicated]

    return 200, {
        "source_version": version,
        "source_revision": revision,
        "snapshot_id": snapshot.snapshot_id,
        "page": page,
        "total_pages": reported_total,
        "announcements": page_items,
    }


def create_app(
    scenario_dir: Path | str | None = None,
    default_scenario: str | None = None,
    snapshot_history: int | None = None,
) -> FastAPI:
    registry = ScenarioRegistry(scenario_dir or SCENARIO_DIR)
    store = SnapshotStore(_env_history() if snapshot_history is None else snapshot_history)
    default = default_scenario or DEFAULT_SCENARIO
    try:
        default_data = registry.get(default)
    except HTTPException as exc:
        raise RuntimeError(f"default scenario {default!r} is not available") from exc

    state = {
        "current_name": default,
        "current_snapshot_id": store.create(default, default_data).snapshot_id,
    }
    # Snapshot used by /pinned/<name> while its scenario file is unchanged.
    pinned: dict[str, str] = {}

    app = FastAPI(
        title="Mock transit announcement source",
        version="1.1.0",
        description=(
            "Paginated announcement source with switchable scenarios and "
            "immutable, pinnable snapshots."
        ),
    )
    app.state.store = store
    app.state.registry = registry
    app.state.state = state

    async def _serve(snapshot: Snapshot, page: int) -> JSONResponse:
        scenario = snapshot.scenario
        if scenario.get("mode") == "delay":
            await asyncio.sleep(float(scenario.get("delay_seconds", 1.0)))
        elif scenario.get("page_delay_seconds"):
            await asyncio.sleep(float(scenario["page_delay_seconds"]))
        status, payload = _page_payload(snapshot, page)
        return JSONResponse(status_code=status, content=payload)

    def _snapshot_for(
        snapshot_id: str | None, page: int, pinned_name: str | None
    ) -> Snapshot:
        token = (snapshot_id or "").strip() or None

        if token is not None:
            snapshot = store.get(token)
            if snapshot is None:
                raise HTTPException(
                    status_code=410,
                    detail=(
                        f"snapshot {token!r} is no longer available (the source "
                        "may have restarted or evicted it); restart the collection"
                    ),
                )
            if pinned_name is not None and snapshot.scenario_name != pinned_name:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"snapshot {token!r} belongs to scenario "
                        f"{snapshot.scenario_name!r}, not {pinned_name!r}"
                    ),
                )
            return snapshot

        if page != 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    "page > 1 requires the snapshot_id returned by page 1; "
                    "send it as ?snapshot=<snapshot_id> so the whole page set "
                    "is read from one snapshot"
                ),
            )

        if pinned_name is not None:
            cached_id = pinned.get(pinned_name)
            snapshot = store.get(cached_id) if cached_id else None
            if snapshot is None:
                snapshot = store.create(pinned_name, registry.get(pinned_name))
                pinned[pinned_name] = snapshot.snapshot_id
            return snapshot

        snapshot = store.get(state["current_snapshot_id"])
        if snapshot is None:  # evicted by a very small history: rebuild from current
            snapshot = store.create(
                state["current_name"], registry.get(state["current_name"])
            )
            state["current_snapshot_id"] = snapshot.snapshot_id
        return snapshot

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "scenario": state["current_name"],
            "snapshot_id": state["current_snapshot_id"],
        }

    @app.get("/scenarios")
    def list_scenarios() -> dict[str, Any]:
        registry.refresh()
        # Files may have changed on disk; drop cached pinned snapshots so the
        # next pinned page 1 freezes the reloaded data.
        pinned.clear()
        return {
            "scenarios": registry.names(),
            "current": state["current_name"],
            "snapshot_id": state["current_snapshot_id"],
        }

    @app.get("/snapshots")
    def list_snapshots() -> dict[str, Any]:
        return {
            "current_snapshot_id": state["current_snapshot_id"],
            "boot_id": store.boot_id,
            "snapshots": [
                {
                    "snapshot_id": item.snapshot_id,
                    "scenario": item.scenario_name,
                    "source_version": item.scenario.get("source_version"),
                    "source_revision": item.scenario.get("source_revision"),
                    "created_at": item.created_at,
                }
                for item in store.list()
            ],
        }

    @app.get("/announcements")
    async def announcements(
        page: int = Query(1, ge=1),
        snapshot: str | None = Query(
            None,
            description=(
                "Snapshot id returned by page 1. Required for page > 1; all "
                "pages of one collection must use the same id."
            ),
        ),
    ) -> JSONResponse:
        return await _serve(_snapshot_for(snapshot, page, None), page)

    @app.get("/pinned/{name}/announcements")
    async def pinned_announcements(
        name: str,
        page: int = Query(1, ge=1),
        snapshot: str | None = Query(None),
    ) -> JSONResponse:
        """Serve a scenario pinned by URL, independent of the global switch.

        Point a collector at ``http://source:8001/pinned/slow_v2`` to let two
        collectors run concurrently against different versions.
        """
        return await _serve(_snapshot_for(snapshot, page, name), page)

    @app.post("/scenario")
    async def switch(request: Request) -> dict[str, Any]:
        """Switch the active scenario and freeze a new snapshot for new reads.

        Accepts JSON (``{"name": "v2"}``, ``{"path": "/abs/v2.json"}``,
        ``{"scenario": {...}}``), form fields, or ``?name=`` query parameter.
        """
        body: dict[str, Any] = {}
        query_name = request.query_params.get("name")
        if query_name:
            body["name"] = query_name
        if request.headers.get("content-type", "").startswith("application/json"):
            raw = await request.body()
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    body.update(parsed)
        else:
            form = await request.form()
            body.update({key: value for key, value in form.items()})

        if "scenario" in body:
            scenario = body["scenario"]
            if not isinstance(scenario, dict):
                raise HTTPException(status_code=422, detail="scenario must be an object")
            name = str(
                scenario.get("name") or f"inline-{uuid.uuid4().hex[:8]}"
            )
            scenario.setdefault("source_version", name)
            registry.put(name, scenario)
        elif "path" in body:
            path = Path(str(body["path"])).resolve()
            allowed = registry.directory.resolve()
            if allowed not in path.parents:
                raise HTTPException(
                    status_code=422,
                    detail=f"scenario files must live under {allowed}",
                )
            if not path.is_file():
                raise HTTPException(status_code=404, detail=f"no file at {path}")
            name = path.stem
            registry.put(name, _load_scenario_file(path))
        elif "name" in body:
            name = str(body["name"])
            registry.refresh()
            if name not in registry.names():
                raise HTTPException(status_code=404, detail=f"unknown scenario {name!r}")
        else:
            raise HTTPException(
                status_code=422, detail="provide name, path or scenario"
            )

        snapshot = store.create(name, registry.get(name))
        state["current_name"] = name
        state["current_snapshot_id"] = snapshot.snapshot_id
        pinned.clear()
        return {
            "current": name,
            "snapshot_id": snapshot.snapshot_id,
            "scenario": registry.get(name),
        }

    return app


app = create_app()
