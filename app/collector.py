"""Collector that fetches every page of one source version and publishes it."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from pydantic import ValidationError

from .config import Settings
from .db import Database, PublishedVersion, PublishConflict
from .models import (
    CandidateSet,
    DuplicateAnnouncementError,
    SourcePage,
    build_candidate,
    utc_now_iso,
)


class SourceError(RuntimeError):
    """The upstream source is unreachable or violates the page contract."""


@dataclass
class CollectionResult:
    status: str  # published | already_current | failed
    http_status: int
    detail: str
    source_version: str | None = None
    source_revision: int | None = None
    version: PublishedVersion | None = None
    collected_at: str = field(default_factory=utc_now_iso)


class PageFetcher(Protocol):
    def fetch_page(self, page: int) -> "SourcePage | dict": ...


class HttpPageFetcher:
    def __init__(self, base_url: str, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def fetch_page(self, page: int) -> dict:
        url = f"{self.base_url}/announcements"
        try:
            response = httpx.get(
                url,
                params={"page": page},
                timeout=self.timeout_seconds,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise SourceError(f"page {page}: source request failed: {exc}") from exc
        if response.status_code != 200:
            raise SourceError(
                f"page {page}: source returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise SourceError(f"page {page}: source returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SourceError(f"page {page}: source payload must be a JSON object")
        return payload


def _parse_page(raw: "SourcePage | dict", page: int) -> SourcePage:
    if isinstance(raw, SourcePage):
        parsed = raw
    else:
        try:
            parsed = SourcePage.model_validate(raw)
        except ValidationError as exc:
            raise SourceError(f"page {page}: invalid page payload: {exc}") from exc
    if parsed.page != page:
        raise SourceError(
            f"requested page {page} but source labelled it page {parsed.page}"
        )
    return parsed


def fetch_complete_page_set(fetcher: PageFetcher, max_pages: int) -> CandidateSet:
    """Fetch pages 1..total_pages, enforcing one consistent version per set."""
    first = _parse_page(fetcher.fetch_page(1), 1)
    total_pages = first.total_pages
    if total_pages > max_pages:
        raise SourceError(
            f"source reports {total_pages} pages, above the configured limit of {max_pages}"
        )

    pages = [first]
    for page_no in range(2, total_pages + 1):
        page = _parse_page(fetcher.fetch_page(page_no), page_no)
        if page.source_version != first.source_version:
            raise SourceError(
                "page set changed version mid-collection: "
                f"page 1 is {first.source_version!r}, page {page_no} is "
                f"{page.source_version!r}"
            )
        if page.source_revision != first.source_revision:
            raise SourceError(
                "page set changed revision mid-collection: "
                f"page 1 is revision {first.source_revision}, page {page_no} is "
                f"revision {page.source_revision}"
            )
        if page.total_pages != total_pages:
            raise SourceError(
                "page set changed total_pages mid-collection: "
                f"page 1 says {total_pages}, page {page_no} says {page.total_pages}"
            )
        pages.append(page)

    if len(pages) != total_pages:
        raise SourceError(
            f"incomplete page set: expected {total_pages} pages, received {len(pages)}"
        )

    try:
        return build_candidate(pages)
    except DuplicateAnnouncementError as exc:
        raise SourceError(str(exc)) from exc


class Collector:
    def __init__(
        self,
        db: Database,
        fetcher: PageFetcher,
        *,
        max_pages: int = 200,
    ) -> None:
        self.db = db
        self.fetcher = fetcher
        self.max_pages = max_pages

    def collect(self) -> CollectionResult:
        started_at = utc_now_iso()
        try:
            candidate = fetch_complete_page_set(self.fetcher, self.max_pages)
        except SourceError as exc:
            self._record_failure(started_at, None, None, str(exc))
            return CollectionResult(
                status="failed",
                http_status=502,
                detail=str(exc),
                collected_at=utc_now_iso(),
            )

        try:
            version, changed = self.db.publish(
                candidate, started_at=started_at
            )
        except PublishConflict as exc:
            self._record_failure(
                started_at,
                candidate.source_version,
                candidate.source_revision,
                str(exc),
            )
            return CollectionResult(
                status="failed",
                http_status=409,
                detail=str(exc),
                source_version=candidate.source_version,
                source_revision=candidate.source_revision,
                collected_at=utc_now_iso(),
            )

        return CollectionResult(
            status="published" if changed else "already_current",
            http_status=200,
            detail=(
                f"published {len(candidate.announcements)} announcements"
                if changed
                else "identical version already published; not republished"
            ),
            source_version=candidate.source_version,
            source_revision=candidate.source_revision,
            version=version,
            collected_at=utc_now_iso(),
        )

    def _record_failure(
        self,
        started_at: str,
        source_version: str | None,
        source_revision: int | None,
        detail: str,
    ) -> None:
        try:
            self.db.record_failed_attempt(
                started_at=started_at,
                source_version=source_version,
                source_revision=source_revision,
                detail=detail,
            )
        except Exception:  # pragma: no cover - auditing must not mask the failure
            pass


def build_fetcher(settings: Settings) -> HttpPageFetcher:
    return HttpPageFetcher(settings.source_base_url, settings.source_timeout_seconds)
