"""Switchable, paginated mock announcement source.

Serves ``GET /announcements?page=N`` from a scenario loaded at startup or via
``POST /scenario``. Every page of a response carries the scenario's
``source_version``, ``source_revision`` and ``total_pages`` so the collector can
detect a version flip between page requests.

Switch modes (POST /scenario, form fields ``name`` or ``path``):

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
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

SCENARIO_DIR = Path(os.getenv("SCENARIO_DIR", Path(__file__).parent / "scenarios"))
DEFAULT_SCENARIO = os.getenv("DEFAULT_SCENARIO", "v1")

SCENARIOS: dict[str, dict[str, Any]] = {}


def _load_scenario_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def _refresh_index() -> list[str]:
    SCENARIO_DIR.mkdir(parents=True, exist_ok=True)
    names = []
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        SCENARIOS[path.stem] = _load_scenario_file(path)
        names.append(path.stem)
    return names


def _resolve(name: str) -> dict[str, Any]:
    scenario = SCENARIOS.get(name)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"unknown scenario {name!r}")
    return scenario


def _page_payload(scenario: dict[str, Any], page: int) -> tuple[int, dict[str, Any]]:
    mode = scenario.get("mode", "normal")
    page_size = int(scenario.get("page_size", 2))
    items: list[dict[str, Any]] = list(scenario.get("announcements", []))
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    reported_total = total_pages
    fault_page = int(scenario.get("fault_page", 1))
    version = scenario["source_version"]
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

    payload = {
        "source_version": version,
        "source_revision": revision,
        "page": page,
        "total_pages": reported_total,
        "announcements": page_items,
    }
    return 200, payload


def create_app() -> FastAPI:
    _refresh_index()
    current = {"name": DEFAULT_SCENARIO}
    _resolve(current["name"])

    app = FastAPI(
        title="Mock transit announcement source",
        version="1.0.0",
        description="Paginated announcement source with switchable scenarios.",
    )
    app.state.current = current

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "scenario": current["name"]}

    @app.get("/scenarios")
    def list_scenarios() -> dict[str, Any]:
        names = _refresh_index()
        return {"scenarios": names, "current": current["name"]}

    @app.get("/announcements")
    async def announcements(page: int = Query(1, ge=1)) -> JSONResponse:
        return await _serve(current["name"], page)

    @app.get("/pinned/{name}/announcements")
    async def pinned_announcements(name: str, page: int = Query(1, ge=1)) -> JSONResponse:
        """Serve a scenario pinned by URL, independent of the global switch.

        Point a collector at ``http://source:8001/pinned/slow_v2`` to let two
        collectors run concurrently against different versions.
        """
        return await _serve(name, page)

    async def _serve(name: str, page: int) -> JSONResponse:
        scenario = _resolve(name)
        if scenario.get("mode") == "delay":
            await asyncio.sleep(float(scenario.get("delay_seconds", 1.0)))
        elif scenario.get("page_delay_seconds"):
            await asyncio.sleep(float(scenario["page_delay_seconds"]))
        status, payload = _page_payload(scenario, page)
        return JSONResponse(status_code=status, content=payload)

    @app.post("/scenario")
    async def switch(request: Request) -> dict[str, Any]:
        """Switch the active scenario.

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
            name = str(scenario.get("name") or f"inline-{len(SCENARIOS) + 1}")
            scenario.setdefault("source_version", name)
            SCENARIOS[name] = scenario
        elif "path" in body:
            path = Path(str(body["path"])).resolve()
            allowed = SCENARIO_DIR.resolve()
            if allowed not in path.parents:
                raise HTTPException(
                    status_code=422,
                    detail=f"scenario files must live under {allowed}",
                )
            if not path.is_file():
                raise HTTPException(status_code=404, detail=f"no file at {path}")
            name = path.stem
            SCENARIOS[name] = _load_scenario_file(path)
        elif "name" in body:
            name = str(body["name"])
            _refresh_index()
            if name not in SCENARIOS:
                raise HTTPException(status_code=404, detail=f"unknown scenario {name!r}")
        else:
            raise HTTPException(
                status_code=422, detail="provide name, path or scenario"
            )

        current["name"] = name
        return {"current": name, "scenario": SCENARIOS[name]}

    return app


app = create_app()
