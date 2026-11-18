"""Collector that fetches every page of one source snapshot and publishes it.

The source hands out an immutable ``snapshot_id`` with page 1. Every following
page is requested with that id, so a scenario switch between two page requests
can never splice an old and a new page set together — even when both scenarios
use the same ``source_version``, ``source_revision`` and page count. If the
source has restarted (or evicted the snapshot) the continuation request fails
and the whole batch is rejected; mixed data is never published.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from pydantic import ValidationError

from .config import Settings
from .db import Database, PublishConflict, PublishedVersion
from .models import (
    CandidateSet,
    DuplicateAnnouncementError,
    SourcePage,
    build_candidate,
    utc_now_iso,
)


class SourceError(RuntimeError):
    """The upstream source is unreachable or violates the page contract."""

    def __init__(self, message: str, *, snapshot_id: str | None = None) -> None:
        super().__init__(message)
        self.snapshot_id = snapshot_id


class SnapshotUnavailableError(SourceError):
    """The snapshot a collection started from no longer exists at the source."""


@dataclass
class CollectionResult:
    status: str  # published | already_current | failed
    http_status: int
    detail: str
    source_version: str | None = None
    source_revision: int | None = None
    snapshot_id: str | None = None
    version: PublishedVersion | None = None
    collected_at: str = field(default_factory=utc_now_iso)


class PageFetcher(Protocol):
    def fetch_page(
        self, page: int, snapshot_id: str | None = None
    ) -> SourcePage | dict: ...


def _accepts_snapshot_argument(function: object) -> bool:
    """Whether a fetch implementation can be called with ``snapshot_id``.

    Page fetchers written before snapshots existed only take ``page``. They
    keep working in best-effort label-consistency mode; snapshot-aware
    fetchers get the token. Only the signature is inspected, so a genuine
    ``TypeError`` raised *inside* a fetch is never mistaken for "this fetcher
    does not support snapshots".
    """
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return False
    return "snapshot_id" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


class HttpPageFetcher:
    def __init__(self, base_url: str, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def fetch_page(self, page: int, snapshot_id: str | None = None) -> dict:
        url = f"{self.base_url}/announcements"
        params: dict[str, object] = {"page": page}
        if snapshot_id:
            params["snapshot"] = snapshot_id
        try:
            response = httpx.get(
                url,
                params=params,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise SourceError(
                f"page {page}: source request failed: {exc}", snapshot_id=snapshot_id
            ) from exc
        if response.status_code == 410:
            raise SnapshotUnavailableError(
                f"page {page}: snapshot {snapshot_id!r} is no longer available at "
                "the source (it restarted or evicted the snapshot); "
                "refusing to mix pages from different page sets",
                snapshot_id=snapshot_id,
            )
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


def _parse_page(raw: SourcePage | dict, page: int) -> SourcePage:
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


def _fetch_page(fetcher: PageFetcher, page: int, snapshot_id: str | None) -> dict:
    method = fetcher.fetch_page
    if _accepts_snapshot_argument(getattr(method, "__func__", method)):
        return method(page, snapshot_id=snapshot_id)
    return method(page)


def fetch_complete_page_set(fetcher: PageFetcher, max_pages: int) -> CandidateSet:
    """Fetch pages 1..total_pages from one immutable source snapshot.

    Page 1 starts the page set and returns the snapshot token. Every further
    page is requested with that token, and the token is re-checked on every
    response. Snapshot ids are mandatory for multi-page sets when the source
    provides them; sources that predate the snapshot contract are still
    accepted in label-consistency mode.
    """
    first = _parse_page(_fetch_page(fetcher, 1, None), 1)
    total_pages = first.total_pages
    if total_pages > max_pages:
        raise SourceError(
            f"source reports {total_pages} pages, above the configured limit of {max_pages}"
        )

    snapshot_id = first.snapshot_id
    pages = [first]

    def fail(message: str) -> SourceError:
        return SourceError(message, snapshot_id=snapshot_id)

    for page_no in range(2, total_pages + 1):
        page = _parse_page(_fetch_page(fetcher, page_no, snapshot_id), page_no)
        if page.snapshot_id != snapshot_id:
            raise fail(
                "page set changed snapshot mid-collection: "
                f"page 1 is snapshot {snapshot_id!r}, page {page_no} is "
                f"snapshot {page.snapshot_id!r}"
            )
        if page.source_version != first.source_version:
            raise fail(
                "page set changed version mid-collection: "
                f"page 1 is {first.source_version!r}, page {page_no} is "
                f"{page.source_version!r}"
            )
        if page.source_revision != first.source_revision:
            raise fail(
                "page set changed revision mid-collection: "
                f"page 1 is revision {first.source_revision}, page {page_no} is "
                f"revision {page.source_revision}"
            )
        if page.total_pages != total_pages:
            raise fail(
                "page set changed total_pages mid-collection: "
                f"page 1 says {total_pages}, page {page_no} says {page.total_pages}"
            )
        pages.append(page)

    if len(pages) != total_pages:
        raise fail(
            f"incomplete page set: expected {total_pages} pages, received {len(pages)}"
        )

    try:
        return build_candidate(pages)
    except DuplicateAnnouncementError as exc:
        raise fail(str(exc)) from exc


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
                snapshot_id=getattr(exc, "snapshot_id", None),
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
                snapshot_id=candidate.snapshot_id,
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
            snapshot_id=candidate.snapshot_id,
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
