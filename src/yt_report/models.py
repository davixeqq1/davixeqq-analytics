"""Plain data structures shared across modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .periods import Period

SHORTS = "SHORTS"
LONG_FORM = "VIDEO_ON_DEMAND"
LIVE = "LIVE_STREAM"
STORY = "STORY"
UNSPECIFIED = "UNSPECIFIED"
REPORTED_FORMATS = (SHORTS, LONG_FORM, LIVE)
FORMAT_LABELS = {
    SHORTS: "Shorts",
    LONG_FORM: "Long-form",
    LIVE: "Livestream",
    STORY: "Story",
    UNSPECIFIED: "Unspecified",
}

# The Analytics API returns camelCase values (verified against the live API):
# "shorts", "videoOnDemand", "liveStream", "story", "creatorContentTypeUnspecified".
# Upper-case enum spellings from the documentation are accepted as well.
_API_CONTENT_TYPES = {
    "shorts": SHORTS,
    "videoondemand": LONG_FORM,
    "video_on_demand": LONG_FORM,
    "livestream": LIVE,
    "live_stream": LIVE,
    "story": STORY,
    "creatorcontenttypeunspecified": UNSPECIFIED,
    "unspecified": UNSPECIFIED,
}


def normalize_content_type(value: str | None) -> str | None:
    """Map an API creatorContentType value to the internal constant.

    Unknown values are returned upper-cased (kept, never silently re-labelled
    as another format); missing values stay None.
    """
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    return _API_CONTENT_TYPES.get(raw.lower(), raw.upper())

# Where a format figure came from. Never present a derived figure as an API figure.
SOURCE_API = "api"                  # returned directly by YouTube Analytics
SOURCE_DERIVED = "derived"          # calculated from per-video rows (view-weighted)
SOURCE_UNAVAILABLE = "unavailable"  # API did not provide it


@dataclass
class ChannelTotals:
    views: int | None = None
    minutes_watched: float | None = None
    avg_view_duration_s: float | None = None
    avg_view_pct: float | None = None
    subs_gained: int | None = None
    subs_lost: int | None = None

    @property
    def watch_hours(self) -> float | None:
        return None if self.minutes_watched is None else self.minutes_watched / 60.0

    @property
    def net_subs(self) -> int | None:
        if self.subs_gained is None or self.subs_lost is None:
            return None
        return self.subs_gained - self.subs_lost


@dataclass
class FormatStats:
    content_type: str
    views: int | None = None
    minutes_watched: float | None = None
    avg_view_duration_s: float | None = None
    avg_view_pct: float | None = None
    source: str = SOURCE_UNAVAILABLE
    complete: bool = True  # False when derived from a capped (top-N) video list
    api_value: str | None = None  # raw creatorContentType value from the API (used as a query filter)

    @property
    def watch_hours(self) -> float | None:
        return None if self.minutes_watched is None else self.minutes_watched / 60.0

    @property
    def label(self) -> str:
        return FORMAT_LABELS.get(self.content_type, self.content_type)


@dataclass
class VideoStats:
    video_id: str
    views: int
    minutes_watched: float
    avg_view_duration_s: float | None
    avg_view_pct: float | None
    content_type: str | None = None
    subs_gained: int | None = None
    engaged_views: int | None = None  # playbacks past the first frame; AVD's weighting base
    title: str = ""
    thumbnail_url: str | None = None
    published_at: str | None = None

    @property
    def url(self) -> str:
        if self.content_type == SHORTS:
            return f"https://www.youtube.com/shorts/{self.video_id}"
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def watch_hours(self) -> float:
        return self.minutes_watched / 60.0


@dataclass
class WeakVideo:
    video: VideoStats
    format_median_pct: float
    ratio: float          # video retention / format median retention
    peers: int            # number of eligible videos in the same format


@dataclass
class DailyStat:
    day: date
    views: int | None
    minutes_watched: float | None
    subs_gained: int | None
    subs_lost: int | None


@dataclass
class Recommendation:
    title: str
    body: str
    source: str = "rules"  # "rules" or "claude"
    priority: int = 50


@dataclass
class Coverage:
    """How much of the period's views the per-video rows account for (measured, not assumed).

    ``views_reference`` is the sum of the API format totals over every content
    type (incl. unspecified) when available, otherwise the channel total.
    """

    method: str                      # METHOD_BY_FORMAT | METHOD_SINGLE_QUERY
    rows: int
    views_rows: int
    views_reference: int | None
    capped: bool = False             # CONFIRMED: at least one query returned MAX_VIDEO_ROWS rows
    capped_min_views: int | None = None  # smallest row of a capped / suspected query (omitted videos have <= this)
    fallback_reason: str | None = None
    # Diagnostics (coverage fix 2026-11-08). views_rows counts each content type at most up to its API total.
    unassigned_views: int = 0        # shortfall of a format query below the row cap: views the API assigns to no video
    truncated_views: int = 0         # shortfall of a format query that returned MAX_VIDEO_ROWS rows (confirmed cap)
    unexplained_views: int = 0       # shortfall of fallback rows from a query suspected (not confirmed) to be truncated
    unidentified_views: int = 0      # shortfall of fallback rows when ambiguously classified videos make the cause unknown
    excess_views: int = 0            # rows above their format total: inconsistent data, never "complete"
    truncation_suspected: bool = False  # single query returned >= SUSPECT_ROWS (< MAX) rows and views are missing
    other_type_views: dict[str, int] = field(default_factory=dict)  # non-video content types (e.g. POSTS)
    failed_types: list[str] = field(default_factory=list)          # content types whose query failed

    @property
    def complete(self) -> bool:
        return (self.views_reference is not None and self.views_rows >= self.views_reference
                and self.excess_views == 0)

    @property
    def ratio(self) -> float | None:
        if self.views_reference is None:
            return None
        if self.views_reference == 0:
            return 1.0 if self.views_rows == 0 else None
        return self.views_rows / self.views_reference

    @property
    def missing_views(self) -> int | None:
        return None if self.views_reference is None else max(0, self.views_reference - self.views_rows)


METHOD_BY_FORMAT = "by_format"
METHOD_SINGLE_QUERY = "single_query"
METHOD_MIXED = "by_format+single_query"  # some format queries failed; their rows came from the single query

PUB_OK = "ok"
PUB_UNAVAILABLE = "unavailable"


@dataclass
class PublicationStats:
    """Public uploads first published inside one period (Pacific dates).

    Only public videos are counted. Private and unlisted uploads dated in the
    period are counted separately and excluded; scheduled (upcoming)
    livestreams are never counted. Formats come from YouTube Analytics only —
    a video without an Analytics classification stays ``unclassified``.
    """

    status: str = PUB_UNAVAILABLE
    complete: bool = False
    published: dict[str, int] = field(default_factory=dict)   # content type -> public uploads
    unclassified: int = 0
    private: int = 0
    unlisted: int = 0
    upcoming: int = 0
    video_ids: list[str] = field(default_factory=list)       # public uploads in the period
    incomplete_reason: str | None = None

    @property
    def available(self) -> bool:
        return self.status == PUB_OK

    def count(self, content_type: str) -> int:
        return self.published.get(content_type, 0)

    @property
    def total_public(self) -> int:
        return sum(self.published.values()) + self.unclassified


@dataclass
class ViewsByAge:
    """Views split by upload age, compared over equivalent periods.

    * ``new_*``: views in a period from public uploads published in that same period.
    * ``prev_uploads_cur``: views this period from the previous period's uploads.
    * ``older_*``: everything else, i.e. videos published before the previous
      period started — the same set of videos in both periods (like-for-like).
    """

    total_cur: int
    total_prev: int
    new_cur: int
    prev_uploads_cur: int
    new_prev: int
    complete: bool

    @property
    def older_cur(self) -> int:
        return self.total_cur - self.new_cur - self.prev_uploads_cur

    @property
    def older_prev(self) -> int:
        return self.total_prev - self.new_prev

    @property
    def new_share_cur(self) -> float | None:
        return self.new_cur / self.total_cur * 100 if self.total_cur else None

    @property
    def new_share_prev(self) -> float | None:
        return self.new_prev / self.total_prev * 100 if self.total_prev else None


@dataclass
class WeeklyReport:
    channel_id: str
    channel_title: str
    subscriber_count: int | None
    period: Period
    previous_period: Period
    current: ChannelTotals
    previous: ChannelTotals
    formats_current: dict[str, FormatStats]
    formats_previous: dict[str, FormatStats]
    videos: list[VideoStats]
    top_videos: list[VideoStats]
    weakest: WeakVideo | None
    weakest_note: str | None
    daily: list[DailyStat]
    generated_at: datetime
    recommendations: list[Recommendation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # v1.2.0 — None means "not measured" (e.g. reports stored by older versions).
    coverage_current: Coverage | None = None
    coverage_previous: Coverage | None = None
    publications_current: PublicationStats | None = None
    publications_previous: PublicationStats | None = None
    views_by_age: ViewsByAge | None = None
    missing_days: list[date] = field(default_factory=list)
    # Stored reports of the same channel whose periods overlap this report's comparison (e.g. after the
    # switch to Sunday–Saturday weeks). Empty for normal, aligned weekly reports.
    period_overlaps: list[Period] = field(default_factory=list)
    # Partial report (--allow-incomplete): last day of the current period with Analytics data.
    # None = the whole period has data (a normal, complete report).
    data_through: date | None = None

    @property
    def partial(self) -> bool:
        return self.data_through is not None

    @property
    def days_with_data(self) -> int:
        return self.period.days if self.data_through is None else (self.data_through - self.period.start).days + 1
