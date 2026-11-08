"""Pure calculations: period comparisons, top videos, weakest video."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import median

from .formatting import fmt_int
from .models import (
    FORMAT_LABELS,
    LIVE,
    LONG_FORM,
    METHOD_SINGLE_QUERY,
    PUB_OK,
    PUB_UNAVAILABLE,
    REPORTED_FORMATS,
    SHORTS,
    SOURCE_API,
    SOURCE_DERIVED,
    SOURCE_UNAVAILABLE,
    ChannelTotals,
    Coverage,
    DailyStat,
    FormatStats,
    PublicationStats,
    VideoStats,
    ViewsByAge,
    WeakVideo,
    WeeklyReport,
)
from .periods import ANALYTICS_TZ, Period
from .youtube_client import VideoMeta

FALLBACK_NO_FORMAT_TOTALS = "format totals unavailable"  # mirrors analytics_client (no import cycle)

# ---- data-quality notes (built from the FINAL report state only) -------------
NOTE_DERIVED = "Format totals for {which} were derived from per-video rows (not returned directly by the API)."
NOTE_DERIVED_WATCH_TIME = "Derived watch time may be slightly understated because of per-video minute rounding."
NOTE_DERIVED_CAPPED = ("Derived format totals for {which} come from per-video rows that do not cover all views "
                       "and may be understated.")
NOTE_UNAVAILABLE = "Format breakdown (Shorts / long-form / livestream) unavailable for {which}."
NOTE_NO_VIDEO_TYPES = "Per-video content type unavailable from the API; weakest-video check skipped."

# ---- retention comparison (same format, previous period) ---------------------
RETENTION_MIN_VIEWS = 100  # per format, in BOTH periods
# A change counts only if it clears an absolute (percentage points) AND a relative threshold.
RETENTION_THRESHOLD_PP = {SHORTS: 5.0, LONG_FORM: 3.0, LIVE: 1.0}
RETENTION_THRESHOLD_REL = 10.0  # %


def pct_change(current: float | None, previous: float | None) -> float | None:
    """Percentage change; None when undefined (missing data or previous == 0)."""
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / abs(previous) * 100.0


@dataclass
class Comparison:
    key: str
    label: str
    current: float | None
    previous: float | None
    higher_is_better: bool = True
    kind: str = "int"  # int | hours | duration | pct

    @property
    def change_pct(self) -> float | None:
        return pct_change(self.current, self.previous)

    @property
    def delta(self) -> float | None:
        if self.current is None or self.previous is None:
            return None
        return self.current - self.previous

    @property
    def is_improvement(self) -> bool | None:
        d = self.delta
        if d is None or d == 0:
            return None
        return (d > 0) == self.higher_is_better


def compare_totals(current: ChannelTotals, previous: ChannelTotals) -> list[Comparison]:
    return [
        Comparison("views", "Views", current.views, previous.views),
        Comparison("watch_hours", "Watch time", current.watch_hours, previous.watch_hours, kind="hours"),
        Comparison("net_subs", "Net subscribers", current.net_subs, previous.net_subs),
        Comparison("subs_gained", "Subscribers gained", current.subs_gained, previous.subs_gained),
        Comparison("subs_lost", "Subscribers lost", current.subs_lost, previous.subs_lost, higher_is_better=False),
        Comparison("avg_view_duration_s", "Avg view duration", current.avg_view_duration_s, previous.avg_view_duration_s, kind="duration"),
        Comparison("avg_view_pct", "Avg % viewed", current.avg_view_pct, previous.avg_view_pct, kind="pct"),
    ]


def compare_format(current: FormatStats, previous: FormatStats | None) -> list[Comparison]:
    prev = previous or FormatStats(current.content_type)
    return [
        Comparison("views", "Views", current.views, prev.views),
        Comparison("watch_hours", "Watch time", current.watch_hours, prev.watch_hours, kind="hours"),
        Comparison("avg_view_duration_s", "Avg view duration", current.avg_view_duration_s, prev.avg_view_duration_s, kind="duration"),
        Comparison("avg_view_pct", "Avg % viewed", current.avg_view_pct, prev.avg_view_pct, kind="pct"),
    ]


def top_videos(videos: list[VideoStats], n: int = 3) -> list[VideoStats]:
    eligible = [v for v in videos if v.views > 0]
    return sorted(eligible, key=lambda v: (v.views, v.minutes_watched), reverse=True)[:n]


def find_weakest(
    videos: list[VideoStats], min_views: int = 30, min_peers: int = 3
) -> tuple[WeakVideo | None, str | None]:
    """Lowest retention relative to the median of its own format.

    Shorts and long-form retention are not comparable (Shorts loop and can
    exceed 100 %), so each video is scored against the median average
    percentage viewed of eligible videos in the same format. Videos below
    ``min_views`` or without retention data are excluded.
    """
    candidates: list[WeakVideo] = []
    skipped: list[str] = []
    for ct in REPORTED_FORMATS:
        eligible = [
            v for v in videos
            if v.content_type == ct and v.views >= min_views and v.avg_view_pct is not None
        ]
        if len(eligible) < min_peers:
            skipped.append(ct)
            continue
        med = median(v.avg_view_pct for v in eligible)
        if med <= 0:
            continue
        for v in eligible:
            candidates.append(WeakVideo(video=v, format_median_pct=med, ratio=v.avg_view_pct / med, peers=len(eligible)))

    below = [c for c in candidates if c.ratio < 1.0]
    if not below:
        if not candidates:
            return None, (
                f"Not enough data: each format needs at least {min_peers} videos with ≥{min_views} views "
                "in this period for a fair comparison."
            )
        return None, "No video is below its format's median retention."
    weakest = min(below, key=lambda c: (c.ratio, -c.video.views))
    return weakest, None


def format_consistency_note(formats: dict[str, FormatStats], totals: ChannelTotals) -> str | None:
    """Check that API format views add up to the channel total (all types, incl. unspecified).

    Returns a human-readable note on mismatch, None when consistent or not checkable.
    """
    if not formats or totals.views is None or any(f.source != SOURCE_API for f in formats.values()):
        return None
    if any(f.views is None for f in formats.values()):
        return None
    total = sum(f.views for f in formats.values())
    tolerance = max(1, round(totals.views * 0.005))
    if abs(total - totals.views) > tolerance:
        return (f"Format breakdown sums to {total:,} views but the channel total is {totals.views:,}; "
                "YouTube may still be processing data for some content types.")
    return None


def data_quality_notes(formats_current: dict[str, FormatStats], formats_previous: dict[str, FormatStats],
                       videos_typed: bool = True) -> list[str]:
    """Notes that describe how the final format figures were obtained.

    Derived only from the final state, so a failed first attempt that a
    fallback later recovered never produces a contradictory warning.
    """
    notes: list[str] = []
    any_derived = False
    for which, formats in (("this period", formats_current), ("the previous period", formats_previous)):
        sources = {f.source for ct, f in formats.items() if ct in REPORTED_FORMATS}
        if SOURCE_DERIVED in sources:
            any_derived = True
            notes.append(NOTE_DERIVED.format(which=which))
            if any(not f.complete for f in formats.values()):
                notes.append(NOTE_DERIVED_CAPPED.format(which=which))
        elif SOURCE_UNAVAILABLE in sources or not sources:
            notes.append(NOTE_UNAVAILABLE.format(which=which))
    if any_derived:
        notes.append(NOTE_DERIVED_WATCH_TIME)
    if not videos_typed:
        notes.append(NOTE_NO_VIDEO_TYPES)
    return notes


@dataclass
class RetentionChange:
    content_type: str
    current_pct: float | None
    previous_pct: float | None
    current_views: int | None
    previous_views: int | None
    status: str  # improved | declined | stable | insufficient

    @property
    def label(self) -> str:
        return FORMAT_LABELS.get(self.content_type, self.content_type)

    @property
    def delta_pp(self) -> float | None:
        if self.current_pct is None or self.previous_pct is None:
            return None
        return self.current_pct - self.previous_pct


def retention_changes(formats_current: dict[str, FormatStats], formats_previous: dict[str, FormatStats],
                      min_views: int = RETENTION_MIN_VIEWS) -> list[RetentionChange]:
    """Average % viewed per format vs. the same format in the previous period.

    Never uses the channel-wide average (it moves with the format mix). A
    format is compared only when both periods have API-sourced retention and
    at least ``min_views`` views; otherwise its status is ``insufficient``.
    """
    out: list[RetentionChange] = []
    for ct in REPORTED_FORMATS:
        cur, prev = formats_current.get(ct), formats_previous.get(ct)
        cp = cur.avg_view_pct if cur else None
        pp = prev.avg_view_pct if prev else None
        cv = cur.views if cur else None
        pv = prev.views if prev else None
        comparable = (
            cur is not None and prev is not None
            and cur.source == SOURCE_API and prev.source == SOURCE_API
            and cp is not None and pp is not None and pp > 0
            and (cv or 0) >= min_views and (pv or 0) >= min_views
        )
        status = "insufficient"
        if comparable:
            delta = cp - pp
            rel = abs(delta) / pp * 100
            if abs(delta) >= RETENTION_THRESHOLD_PP.get(ct, 3.0) and rel >= RETENTION_THRESHOLD_REL:
                status = "improved" if delta > 0 else "declined"
            else:
                status = "stable"
        out.append(RetentionChange(ct, cp, pp, cv, pv, status))
    return out


def format_share(formats: dict[str, FormatStats]) -> dict[str, dict[str, float | None]]:
    """Share of views and watch time per reported format (0-100)."""
    total_views = sum(formats[ct].views or 0 for ct in REPORTED_FORMATS if ct in formats)
    total_min = sum(formats[ct].minutes_watched or 0 for ct in REPORTED_FORMATS if ct in formats)
    out: dict[str, dict[str, float | None]] = {}
    for ct in REPORTED_FORMATS:
        f = formats.get(ct)
        out[ct] = {
            "views": (f.views or 0) / total_views * 100 if f and total_views else None,
            "minutes": (f.minutes_watched or 0) / total_min * 100 if f and total_min else None,
        }
    return out


# ==== v1.2.0: publications, views by upload age, completeness =================
NOTE_COVERAGE = ("Per-video rows for {which} account for {rows} of {ref} views ({pct}): {causes}. "
                 "Top/weakest videos and new-upload figures are based on these rows.")
NOTE_EXCESS = ("Per-video rows for {which} exceed the API format totals by {n} views (inconsistent API data); "
               "coverage is not marked complete.")
NOTE_OTHER_TYPES = ("Coverage for {which} includes {parts} — counted against the format totals, "
                    "never ranked as videos.")
NOTE_WEAKEST_CAPPED = ("The per-video list for this period {how} while its smallest row still "
                       "had {n} views (≥ {min_views}); the weakest-video check may have missed candidates.")
NOTE_FALLBACK = "Per-video rows for {which} were fetched with the single-query fallback ({reason})."
NOTE_PUB_UNAVAILABLE = "Publication statistics unavailable ({reason}); upload counts are not shown."
NOTE_PUB_INCOMPLETE = "Publication statistics are incomplete ({reason}); counts may be too low."
NOTE_UNCLASSIFIED = ("{n} public upload(s) in {which} have no YouTube Analytics format yet and are listed as "
                     "unclassified (format is never inferred from duration).")
NOTE_MISSING_DAYS = "No daily rows returned for {n} day(s): {days} (no activity or not processed yet)."
NOTE_NO_DAILY = "The daily series is unavailable for this report window."
NOTE_PARTIAL = ("PARTIAL REPORT: YouTube Analytics data covers {n} of {days} days of {period} ({start} – {through}); "
                "the remaining days are not processed yet. Figures are informational only: no week-over-week "
                "comparison and no automatic recommendations.")
NOTE_OVERLAP = ("This report's periods overlap earlier stored report(s) for {periods} (the reporting week was "
                "realigned). Week-over-week figures compare {current} with {previous} using YouTube Analytics data, "
                "not with those earlier reports.")

PUB_REASONS = {
    "page_limit": "upload scan stopped at the 40-page limit",
    "repeated_page_token": "uploads playlist returned a repeated page token",
    "no_uploads_playlist": "the channel has no uploads playlist",
    "api_error": "YouTube Data API error",
    "missing_metadata": "some uploads returned no metadata",
    "missing_privacy": "privacy status missing for some uploads",
}


def parse_api_time(value: str | None) -> datetime | None:
    """ISO-8601 timestamp from the Data API (UTC, 'Z') -> aware datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def publication_date_pt(meta: VideoMeta, content_type: str | None) -> date | None:
    """Pacific publication date of one upload.

    * Livestreams: ``actualStartTime`` (``publishedAt`` is when the broadcast was created).
    * Unclassified uploads with an ``actualStartTime`` (live or premiere): ``actualStartTime``.
    * Everything else, incl. premieres classified as videoOnDemand: ``publishedAt``.
    """
    stamp = meta.published_at
    if meta.actual_start_time and content_type in (LIVE, None):
        stamp = meta.actual_start_time
    dt = parse_api_time(stamp)
    return None if dt is None else dt.astimezone(ANALYTICS_TZ).date()


def publication_candidates(metas: dict[str, VideoMeta], window_start: date, window_end: date) -> list[str]:
    """Uploads whose publishedAt or actualStartTime (Pacific) falls in [start, end]."""
    out = []
    for vid, m in metas.items():
        for stamp in (m.published_at, m.actual_start_time):
            dt = parse_api_time(stamp)
            if dt is not None and window_start <= dt.astimezone(ANALYTICS_TZ).date() <= window_end:
                out.append(vid)
                break
    return out


def publication_stats(period: Period, upload_ids: list[str], metas: dict[str, VideoMeta],
                      types: dict[str, str | None], scan_complete: bool,
                      scan_reason: str | None = None,
                      item_dates: dict[str, str | None] | None = None) -> PublicationStats:
    """Count uploads first published in ``period`` (pure function, Pacific dates).

    Uploads without ``videos.list`` metadata (deleted / inaccessible) can never
    be public and are never counted. They make the result incomplete only when
    their playlist-item date is unknown or falls inside the period.
    """
    stats = PublicationStats(status=PUB_OK, complete=scan_complete, incomplete_reason=scan_reason)
    missing_meta = missing_privacy = 0
    item_dates = item_dates or {}
    for vid in dict.fromkeys(upload_ids):  # duplicate IDs are counted once
        meta = metas.get(vid)
        if meta is None:
            added = parse_api_time(item_dates.get(vid))
            if added is None or period.start <= added.astimezone(ANALYTICS_TZ).date() <= period.end:
                missing_meta += 1
            continue
        if meta.live_broadcast_content == "upcoming":
            # Scheduled, not published yet — never counted; reported in the period of its scheduled start.
            sched = parse_api_time(meta.scheduled_start_time or meta.published_at)
            if sched is not None and period.start <= sched.astimezone(ANALYTICS_TZ).date() <= period.end:
                stats.upcoming += 1
            continue
        ct = types.get(vid)
        day = publication_date_pt(meta, ct)
        if day is None or not (period.start <= day <= period.end):
            continue
        privacy = meta.privacy_status
        if privacy == "private":
            stats.private += 1
        elif privacy == "unlisted":
            stats.unlisted += 1
        elif privacy == "public":
            stats.video_ids.append(vid)
            if ct is None:
                stats.unclassified += 1
            else:
                stats.published[ct] = stats.published.get(ct, 0) + 1
        else:
            missing_privacy += 1
    if missing_meta or missing_privacy:
        stats.complete = False
        stats.incomplete_reason = stats.incomplete_reason or ("missing_metadata" if missing_meta else "missing_privacy")
    return stats


def unavailable_publications(reason: str) -> PublicationStats:
    return PublicationStats(status=PUB_UNAVAILABLE, complete=False, incomplete_reason=reason)


def views_by_age(videos_cur: list[VideoStats], videos_prev: list[VideoStats], total_cur: int | None,
                 total_prev: int | None, pubs_cur: PublicationStats | None, pubs_prev: PublicationStats | None,
                 cov_cur: Coverage | None, cov_prev: Coverage | None) -> ViewsByAge | None:
    """New-upload vs older-catalog views over equivalent periods, or None if not computable."""
    if (total_cur is None or total_prev is None or pubs_cur is None or pubs_prev is None
            or not pubs_cur.available or not pubs_prev.available):
        return None
    cur_views = {v.video_id: v.views for v in videos_cur}
    prev_views = {v.video_id: v.views for v in videos_prev}

    def known(ids: list[str], rows: dict[str, int], cov: Coverage | None) -> bool:
        # An upload absent from the rows is a measured zero only when the rows cover all views.
        return all(i in rows for i in ids) or (cov is not None and cov.complete)

    complete = (pubs_cur.complete and pubs_prev.complete
                and known(pubs_cur.video_ids + pubs_prev.video_ids, cur_views, cov_cur)
                and known(pubs_prev.video_ids, prev_views, cov_prev))
    return ViewsByAge(
        total_cur=total_cur, total_prev=total_prev,
        new_cur=sum(cur_views.get(i, 0) for i in pubs_cur.video_ids),
        prev_uploads_cur=sum(cur_views.get(i, 0) for i in pubs_prev.video_ids),
        new_prev=sum(prev_views.get(i, 0) for i in pubs_prev.video_ids),
        complete=complete,
    )


def period_overlaps(history: list[tuple[str, Period]], channel_id: str, current: Period,
                    previous: Period) -> list[Period]:
    """Stored periods that make this report's comparison look like a normal sequel when it is not.

    A stored report overlaps if it intersects the current period, or the previous period while no
    stored report covers exactly that previous period. Reports for exactly the current or the previous
    period are the normal weekly series and never count. Result is sorted and de-duplicated.
    """
    def intersects(a: Period, b: Period) -> bool:
        return a.start <= b.end and b.start <= a.end

    same = {p for ch, p in history if ch == channel_id and p != current}
    aligned_previous = previous in same
    found = {p for p in same if p != previous
             and (intersects(p, current) or (not aligned_previous and intersects(p, previous)))}
    return sorted(found, key=lambda p: (p.start, p.end))


def missing_days(daily: list[DailyStat], start: date, end: date) -> list[date]:
    """Every day in [start, end] without a row in the daily series."""
    present = {d.day for d in daily}
    out, day = [], start
    while day <= end:
        if day not in present:
            out.append(day)
        day += timedelta(days=1)
    return out


def coverage_pct_text(cov: Coverage) -> str:
    """Coverage as text; an incomplete coverage is never rounded up to 100 %."""
    r = cov.ratio
    if r is None:
        return "n/a"
    pct = r * 100
    if not cov.complete:
        pct = min(pct, 99.9)
    return f"{pct:.1f}%"


def _type_label(ct: str) -> str:
    return FORMAT_LABELS.get(ct, ct.replace("_", " ").lower())


def coverage_causes(cov: Coverage) -> list[str]:
    """Why per-video rows do not cover all views — only causes that were actually measured."""
    causes = []
    if cov.unassigned_views:
        causes.append(f"{fmt_int(cov.unassigned_views)} views not assigned to any video by the API")
    if cov.truncated_views:
        causes.append(f"{fmt_int(cov.truncated_views)} views missing from a query that hit the API limit of 200 rows")
    if cov.unexplained_views:
        causes.append(f"{fmt_int(cov.unexplained_views)} views missing from a fallback query that may be truncated "
                      "(suspected, not confirmed)")
    if cov.unidentified_views:
        causes.append(f"{fmt_int(cov.unidentified_views)} views: cause not identified "
                      "(videos with an ambiguous format classification in the fallback rows)")
    if cov.failed_types:
        causes.append("query failed for " + ", ".join(_type_label(t) for t in cov.failed_types))
    if cov.method == METHOD_SINGLE_QUERY:
        if cov.capped:
            causes.append(f"single query returned {cov.rows} rows, the API limit of 200 rows")
        elif cov.truncation_suspected:
            causes.append(f"single query returned {cov.rows} rows — possibly truncated by the API's 200-row limit "
                          "(suspected, not confirmed)")
    explained = cov.unassigned_views + cov.truncated_views + cov.unexplained_views + cov.unidentified_views
    if not causes or (cov.method != METHOD_SINGLE_QUERY and not cov.failed_types
                      and (cov.missing_views or 0) > explained):
        causes.append("cause not identified")
    return causes


def completeness_notes(cov_cur: Coverage | None, cov_prev: Coverage | None, pubs_cur: PublicationStats | None,
                       pubs_prev: PublicationStats | None, daily_missing: list[date], daily_available: bool,
                       weakest_min_views: int | None = None) -> list[str]:
    """Data-completeness notes, built from the final report state only."""
    notes: list[str] = []
    for which, cov in (("this period", cov_cur), ("the previous period", cov_prev)):
        if cov is None:
            continue
        if cov.fallback_reason and cov.fallback_reason != FALLBACK_NO_FORMAT_TOTALS:
            notes.append(NOTE_FALLBACK.format(which=which, reason=cov.fallback_reason))
        if cov.excess_views:
            notes.append(NOTE_EXCESS.format(which=which, n=fmt_int(cov.excess_views)))
        if cov.other_type_views:
            parts = ", ".join(f"{fmt_int(n)} views of {_type_label(t)}" for t, n in cov.other_type_views.items())
            notes.append(NOTE_OTHER_TYPES.format(which=which, parts=parts))
        if (cov.missing_views or 0) > 0:
            notes.append(NOTE_COVERAGE.format(which=which, rows=fmt_int(cov.views_rows), ref=fmt_int(cov.views_reference),
                                              pct=coverage_pct_text(cov), causes="; ".join(coverage_causes(cov))))
    if (cov_cur is not None and (cov_cur.capped or cov_cur.truncation_suspected) and weakest_min_views is not None
            and (cov_cur.capped_min_views or 0) >= weakest_min_views):
        how = ("hit the API limit of 200 rows" if cov_cur.capped
               else "may have been truncated by the API (suspected, not confirmed)")
        notes.append(NOTE_WEAKEST_CAPPED.format(how=how, n=fmt_int(cov_cur.capped_min_views), min_views=weakest_min_views))
    if pubs_cur is None or not pubs_cur.available:
        reason = (pubs_cur.incomplete_reason if pubs_cur else None) or "api_error"
        notes.append(NOTE_PUB_UNAVAILABLE.format(reason=PUB_REASONS.get(reason, reason)))
    else:
        if not (pubs_cur.complete and pubs_prev is not None and pubs_prev.complete):
            reason = pubs_cur.incomplete_reason or (pubs_prev.incomplete_reason if pubs_prev else None) or "unknown"
            notes.append(NOTE_PUB_INCOMPLETE.format(reason=PUB_REASONS.get(reason, reason)))
        for which, p in (("this period", pubs_cur), ("the previous period", pubs_prev)):
            if p is not None and p.unclassified:
                notes.append(NOTE_UNCLASSIFIED.format(n=p.unclassified, which=which))
    if not daily_available:
        notes.append(NOTE_NO_DAILY)
    elif daily_missing:
        days = ", ".join(f"{d:%b} {d.day}" for d in daily_missing[:7]) + (" …" if len(daily_missing) > 7 else "")
        notes.append(NOTE_MISSING_DAYS.format(n=len(daily_missing), days=days))
    return notes


def completeness_issues(report: WeeklyReport) -> list[str]:
    """Short completeness items for the Discord overview (empty list = complete)."""
    issues: list[str] = []
    if report.partial:
        t = report.data_through
        issues.append(f"Partial period: data {report.period.start:%b} {report.period.start.day} – {t:%b} {t.day} "
                      f"only ({report.days_with_data}/{report.period.days} days)")
    for which, cov in (("", report.coverage_current), (" (prev. period)", report.coverage_previous)):
        if cov is None:
            issues.append(f"Per-video coverage not measured{which}")
        elif not cov.complete:
            if cov.excess_views:
                issues.append(f"Per-video rows exceed the format totals by {fmt_int(cov.excess_views)} views{which}")
            if cov.views_reference is None:
                issues.append(f"Per-video coverage could not be measured{which}")
            elif (cov.missing_views or 0) > 0:
                short = []
                if cov.unassigned_views:
                    short.append(f"{fmt_int(cov.unassigned_views)} unassigned by API")
                if cov.truncated_views or (cov.method == METHOD_SINGLE_QUERY and cov.capped):
                    short.append("API cap 200")
                if cov.unexplained_views or cov.truncation_suspected:
                    short.append("truncation suspected")
                if cov.unidentified_views:
                    short.append(f"{fmt_int(cov.unidentified_views)} cause not identified")
                if cov.failed_types:
                    short.append("query failed: " + ", ".join(_type_label(t) for t in cov.failed_types))
                tail = "; " + "; ".join(short) if short else ""
                issues.append(f"Per-video rows cover {coverage_pct_text(cov)} of views "
                              f"({fmt_int(cov.views_rows)} of {fmt_int(cov.views_reference)}{tail}){which}")
    pc, pp = report.publications_current, report.publications_previous
    if pc is None or not pc.available:
        issues.append("Publication counts unavailable")
    else:
        if not (pc.complete and pp is not None and pp.complete):
            reason = pc.incomplete_reason or (pp.incomplete_reason if pp else None) or "unknown"
            issues.append(f"Publication counts incomplete ({PUB_REASONS.get(reason, reason)})")
        if pc.unclassified:
            issues.append(f"{pc.unclassified} new upload(s) not classified by format")
    sources = {f.source for ct, f in report.formats_current.items() if ct in REPORTED_FORMATS}
    if SOURCE_DERIVED in sources:
        issues.append("Format totals derived from per-video rows")
    elif SOURCE_UNAVAILABLE in sources or not sources:
        issues.append("Format breakdown unavailable")
    if format_consistency_note(report.formats_current, report.current):
        issues.append("Format views do not add up to the channel total")
    if not report.daily:
        issues.append("Daily series unavailable")
    elif report.missing_days:
        issues.append(f"Daily series missing {len(report.missing_days)} day(s)")
    if report.previous.views is None:
        issues.append("No data for the previous period")
    return issues
