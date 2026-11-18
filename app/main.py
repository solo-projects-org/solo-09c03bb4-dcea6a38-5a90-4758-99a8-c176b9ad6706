"""FastAPI application: collection trigger and UTC stop-query API."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .collector import Collector, build_fetcher
from .config import Settings
from .db import Database, PublishedVersion, QueryHit
from .models import ensure_utc, to_epoch_us, us_to_iso, utc_now

DESCRIPTION = """\
Collects paginated bus temporary-suspension notices from an upstream source and
answers stop queries for a given UTC instant.

* Only a complete page set of one source version (all pages, same version and
  revision, no conflicting duplicate announcement numbers) is published.
* Publishing is atomic: either every notice of the new version becomes current,
  or the previous version stays current untouched.
* Versions only move forward. A collection that started earlier can never
  overwrite a newer version that finished first, and retrying the same version
  does not republish it.
* Published data is stored in SQLite and survives restarts.
"""


class AnnouncementOut(BaseModel):
    announcement_no: str
    stop_id: str
    start_utc: str
    end_utc: str


class QueryResponse(BaseModel):
    stop_id: str
    at_utc: str
    source_version: str
    source_revision: int
    announcements: list[AnnouncementOut] = Field(
        description="Active suspensions at the stop at at_utc (possibly empty)."
    )


class VersionOut(BaseModel):
    source_version: str
    source_revision: int
    total_pages: int
    announcement_count: int
    content_hash: str
    published_at: str


class CurrentVersionResponse(BaseModel):
    source_version: str
    source_revision: int
    total_pages: int
    announcement_count: int
    published_at: str


class CollectResponse(BaseModel):
    status: str = Field(description="published | already_current | failed")
    detail: str
    source_version: str | None = None
    source_revision: int | None = None
    collected_at: str
    version: VersionOut | None = None


def _version_dict(version: PublishedVersion) -> dict[str, Any]:
    return {
        "source_version": version.source_version,
        "source_revision": version.source_revision,
        "total_pages": version.total_pages,
        "announcement_count": version.announcement_count,
        "content_hash": version.content_hash,
        "published_at": version.published_at,
    }


def _hit_dict(hit: QueryHit) -> dict[str, Any]:
    return {
        "announcement_no": hit.announcement_no,
        "stop_id": hit.stop_id,
        "start_utc": hit.start_utc,
        "end_utc": hit.end_utc,
    }


def _parse_at(at: str | None) -> int:
    if at is None or at.strip() == "":
        return to_epoch_us(utc_now())
    text = at.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"at must be an ISO-8601 timestamp, got {at!r}",
        ) from exc
    return to_epoch_us(ensure_utc(parsed))


def create_app(
    settings: Settings | None = None, *, database: Database | None = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    db = database or Database(settings.database_path)
    fetcher = build_fetcher(settings)
    collector = Collector(db, fetcher, max_pages=settings.max_source_pages)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.initialise()
        yield

    app = FastAPI(
        title="Transit suspension collector",
        version="1.0.0",
        description=DESCRIPTION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.collector = collector

    @app.get("/health", tags=["ops"], summary="Liveness probe and current version")
    def health(request: Request) -> dict[str, Any]:
        current = request.app.state.db.current_version()
        return {
            "status": "ok",
            "current_version": current.source_version if current else None,
        }

    @app.post(
        "/api/v1/collect",
        tags=["collection"],
        summary="Fetch all source pages and publish a complete, consistent version",
        response_model=CollectResponse,
        responses={
            200: {"description": "Version published, or an identical version is already current"},
            409: {
                "description": (
                    "Version is not newer than the current one, or the same "
                    "source_version was published with different content. "
                    "Previous data is retained."
                )
            },
            502: {
                "description": (
                    "Source unreachable, incomplete page set, version flip "
                    "between pages, or conflicting duplicate announcement "
                    "number. Previous data is retained."
                )
            },
        },
    )
    def collect(request: Request) -> JSONResponse:
        result = request.app.state.collector.collect()
        body: dict[str, Any] = {
            "status": result.status,
            "detail": result.detail,
            "source_version": result.source_version,
            "source_revision": result.source_revision,
            "collected_at": result.collected_at,
        }
        if result.version is not None:
            body["version"] = _version_dict(result.version)
        return JSONResponse(status_code=result.http_status, content=body)

    @app.get(
        "/api/v1/announcements",
        tags=["query"],
        summary="List active suspension notices for a stop at a UTC instant",
        response_model=QueryResponse,
        responses={404: {"description": "No version has been published yet"}},
    )
    def query_announcements(
        request: Request,
        stop_id: str = Query(..., min_length=1, description="Stop identifier"),
        at: str | None = Query(
            None,
            description=(
                "UTC instant, ISO-8601 (e.g. 2026-11-18T08:30:00Z). A naive "
                "timestamp is interpreted as UTC. Defaults to now."
            ),
        ),
    ) -> QueryResponse:
        stop_id = stop_id.strip()
        if not stop_id:
            raise HTTPException(status_code=422, detail="stop_id must not be blank")
        at_us = _parse_at(at)
        try:
            version, hits = request.app.state.db.query_stop(stop_id, at_us)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return QueryResponse(
            stop_id=stop_id,
            at_utc=us_to_iso(at_us),
            source_version=version.source_version,
            source_revision=version.source_revision,
            announcements=[AnnouncementOut(**_hit_dict(hit)) for hit in hits],
        )

    @app.get(
        "/api/v1/current-version",
        tags=["query"],
        summary="Return the version currently used as the query basis",
        response_model=CurrentVersionResponse,
        responses={404: {"description": "Nothing has been published yet"}},
    )
    def current_version(request: Request) -> dict[str, Any]:
        current = request.app.state.db.current_version()
        if current is None:
            raise HTTPException(
                status_code=404,
                detail="no announcement version has been published yet",
            )
        return {
            "source_version": current.source_version,
            "source_revision": current.source_revision,
            "total_pages": current.total_pages,
            "announcement_count": current.announcement_count,
            "published_at": current.published_at,
        }

    @app.get(
        "/api/v1/versions",
        tags=["query"],
        summary="List published versions (newest revision first)",
        response_model=list[VersionOut],
    )
    def versions(request: Request) -> list[dict[str, Any]]:
        with request.app.state.db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM published_version ORDER BY source_revision DESC"
            ).fetchall()
        return [
            {
                "source_version": row["source_version"],
                "source_revision": row["source_revision"],
                "total_pages": row["total_pages"],
                "announcement_count": row["announcement_count"],
                "content_hash": row["content_hash"],
                "published_at": row["published_at"],
            }
            for row in rows
        ]

    return app


app = create_app()
