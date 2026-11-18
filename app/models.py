"""Domain models and helpers shared by the API and the collector."""

from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

UTC = timezone.utc


def ensure_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC and normalise aware values to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_epoch_us(value: datetime) -> int:
    value = ensure_utc(value)
    return calendar.timegm(value.utctimetuple()) * 1_000_000 + value.microsecond


def us_to_datetime(value_us: int) -> datetime:
    seconds, micros = divmod(value_us, 1_000_000)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=micros)


def format_iso(value: datetime) -> str:
    """Human friendly, always-UTC ISO-8601 text (fixed precision when needed)."""
    value = ensure_utc(value)
    if value.microsecond:
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def us_to_iso(value_us: int) -> str:
    return format_iso(us_to_datetime(value_us))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return format_iso(utc_now())


class SourceAnnouncement(BaseModel):
    """One suspension notice as it appears in the upstream JSON payload."""

    model_config = ConfigDict(extra="ignore")

    announcement_no: str = Field(min_length=1, max_length=200)
    stop_id: str = Field(min_length=1, max_length=200)
    start_utc: datetime
    end_utc: datetime

    @field_validator("announcement_no", "stop_id")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("start_utc", "end_utc")
    @classmethod
    def _normalise_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _check_period(self) -> "SourceAnnouncement":
        if self.start_utc >= self.end_utc:
            raise ValueError("start_utc must be earlier than end_utc")
        return self


class SourcePage(BaseModel):
    """One page returned by the paginated announcement source."""

    model_config = ConfigDict(extra="ignore")

    source_version: str = Field(min_length=1, max_length=200)
    source_revision: int = Field(default=0, ge=0)
    page: int = Field(ge=1)
    total_pages: int = Field(ge=1)
    announcements: list[SourceAnnouncement]

    @field_validator("source_version")
    @classmethod
    def _strip_version(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("source_version must not be blank")
        return value


@dataclass(frozen=True)
class Announcement:
    """Normalised announcement used internally and persisted in SQLite."""

    announcement_no: str
    stop_id: str
    start_us: int
    end_us: int

    @classmethod
    def from_source(cls, item: SourceAnnouncement) -> "Announcement":
        return cls(
            announcement_no=item.announcement_no,
            stop_id=item.stop_id,
            start_us=to_epoch_us(item.start_utc),
            end_us=to_epoch_us(item.end_utc),
        )

    @property
    def start_utc(self) -> str:
        return us_to_iso(self.start_us)

    @property
    def end_utc(self) -> str:
        return us_to_iso(self.end_us)

    def canonical(self) -> dict:
        return {
            "announcement_no": self.announcement_no,
            "stop_id": self.stop_id,
            "start_us": self.start_us,
            "end_us": self.end_us,
        }


@dataclass(frozen=True)
class CandidateSet:
    """A validated, complete page set of one source version."""

    source_version: str
    source_revision: int
    total_pages: int
    announcements: tuple[Announcement, ...]
    content_hash: str

    @property
    def ordering_key(self) -> tuple[int, str]:
        return (self.source_revision, self.source_version)

    def is_newer_than(self, other_revision: int, other_version: str) -> bool:
        return is_newer(
            self.source_revision, self.source_version, other_revision, other_version
        )


def is_newer(
    candidate_revision: int,
    candidate_version: str,
    current_revision: int,
    current_version: str,
) -> bool:
    """Decide whether a candidate may replace the current version.

    The source revision is the primary ordering (it is monotonic per source).
    When revisions tie — including sources that carry no revision at all — the
    version label breaks the tie; version labels must then sort chronologically
    (ISO timestamps do).
    """
    return (candidate_revision, candidate_version) > (current_revision, current_version)


def content_hash(announcements: tuple[Announcement, ...]) -> str:
    payload = [item.canonical() for item in announcements]
    payload.sort(key=lambda item: item["announcement_no"])
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_candidate(pages: list[SourcePage]) -> CandidateSet:
    """Flatten a complete page set and reject duplicate numbers with differing content."""
    first = pages[0]
    by_number: dict[str, Announcement] = {}

    for page in pages:
        for item in page.announcements:
            announcement = Announcement.from_source(item)
            existing = by_number.get(announcement.announcement_no)
            if existing is None:
                by_number[announcement.announcement_no] = announcement
            elif existing != announcement:
                raise DuplicateAnnouncementError(
                    announcement_no=announcement.announcement_no,
                    page=page.page,
                )

    ordered = tuple(
        sorted(by_number.values(), key=lambda item: item.announcement_no)
    )
    return CandidateSet(
        source_version=first.source_version,
        source_revision=first.source_revision,
        total_pages=first.total_pages,
        announcements=ordered,
        content_hash=content_hash(ordered),
    )


class DuplicateAnnouncementError(ValueError):
    def __init__(self, announcement_no: str, page: int) -> None:
        self.announcement_no = announcement_no
        self.page = page
        super().__init__(
            f"announcement_no {announcement_no!r} appears more than once with different content"
            f" (last seen on page {page})"
        )
