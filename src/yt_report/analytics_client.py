"""YouTube Analytics API v2 queries.

Report types used (see developers.google.com/youtube/analytics/channel_reports):
* Basic user activity   - no dimensions: channel totals incl. subscribers.
* Time-based activity   - dimension ``day``: daily series.
* Playback details      - dimension ``creatorContentType``: Shorts vs long-form.
* Top videos            - dimension ``video``, sorted by ``-views``, ``maxResults`` <= 200.
* Per-video format      - dimensions ``video,creatorContentType`` with a
                          ``video==id1,id2,...`` filter.

Verified against the live API:
* ``creatorContentType`` values are camelCase (``shorts``, ``videoOnDemand``,
  ``liveStream``, ...) — normalised by ``models.normalize_content_type``.
* ``video,creatorContentType`` WITHOUT a video filter returns HTTP 400
  ("query is not supported"); WITH an explicit video-ID filter it works.
  So the top-videos list is fetched with ``dimension=video`` and each video's
  format is then looked up with the filtered query.
* ``creatorContentType`` is NOT supported by the basic report, so format
  totals come from the playback-details report. If that fails, they are
  derived from per-video rows and explicitly flagged as derived.
* The top-videos report has no pagination (``startIndex`` and
  ``maxResults > 200`` return HTTP 400), so one query returns at most 200 rows.
* ``dimensions=video`` with ``filters=creatorContentType==<type>`` works on the
  live API (undocumented for this report). It is the primary per-video path:
  one query per content type with views (incl. ``posts``, counted for
  coverage but never returned as videos). Coverage is always measured.
* Verified on a live channel: a Shorts query well below the row cap returned
  a few views less than the Shorts total, and per-day queries returned the same
  sum — those views are assigned to no video by the API.
  A shortfall below the row cap is therefore kept and reported as
  "unassigned", never used to discard the primary result.
* The unfiltered single query can be truncated below 200 rows (observed: 199
  rows while more videos had views), so >= 190 rows with missing views is reported as
  *suspected* truncation.
"""

from __future__ import annotations

import logging
from datetime import date

from googleapiclient.errors import HttpError

from .analysis import FALLBACK_NO_FORMAT_TOTALS
from .models import (
    METHOD_BY_FORMAT,
    METHOD_MIXED,
    METHOD_SINGLE_QUERY,
    REPORTED_FORMATS,
    SOURCE_API,
    SOURCE_DERIVED,
    SOURCE_UNAVAILABLE,
    ChannelTotals,
    Coverage,
    DailyStat,
    FormatStats,
    VideoStats,
    normalize_content_type,
)
from .periods import Period
from .retry import execute_with_retry, http_error_reason

log = logging.getLogger(__name__)

TOTAL_METRICS = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,subscribersLost"
FORMAT_METRICS = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage"
VIDEO_METRICS = "views,engagedViews,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained"
DAILY_METRICS = "views,estimatedMinutesWatched,subscribersGained,subscribersLost"
MAX_VIDEO_ROWS = 200
# A single query returning this many rows (but < 200) while views are missing is flagged as
# *suspected* truncation: live data showed 199 rows while more videos had views.
SUSPECT_TRUNCATION_ROWS = 190
CLASSIFY_BATCH = 50  # video IDs per filtered format lookup (verified with the live API)
# Internal format constant -> creatorContentType filter value (camelCase, as the live API returns it).
API_FILTER_VALUES = {"SHORTS": "shorts", "VIDEO_ON_DEMAND": "videoOnDemand", "LIVE_STREAM": "liveStream"}

FALLBACK_FILTER_FAILED = "format filter rejected by the API"


def rows_to_dicts(response: dict) -> list[dict]:
    headers = [h["name"] for h in response.get("columnHeaders", [])]
    return [dict(zip(headers, row)) for row in response.get("rows") or []]


def _num(value, cast=float):
    if value is None:
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


class AnalyticsClient:
    """Thin wrapper over ``reports.query``.

    Methods return data only. Report notes are produced later from the final
    state of the report (``analysis.data_quality_notes``), so a transient
    failure that was recovered by a fallback never leaves a stale warning.
    """

    def __init__(self, service, channel_id: str = "MINE"):
        self._svc = service
        self._ids = f"channel=={channel_id}" if channel_id != "MINE" else "channel==MINE"

    def query(self, period: Period, metrics: str, **kwargs) -> list[dict]:
        params = {k: v for k, v in kwargs.items() if v is not None}
        req = self._svc.reports().query(
            ids=self._ids,
            startDate=period.start.isoformat(),
            endDate=period.end.isoformat(),
            metrics=metrics,
            **params,
        )
        return rows_to_dicts(execute_with_retry(req))

    # ---- channel totals -------------------------------------------------
    def channel_totals(self, period: Period) -> ChannelTotals:
        rows = self.query(period, TOTAL_METRICS)
        if not rows:
            # No row at all means YouTube returned no data for the window (e.g. not processed yet).
            # Report it as unavailable rather than as a measured zero.
            return ChannelTotals()
        r = rows[0]
        views = _num(r.get("views"), int)
        return ChannelTotals(
            views=views,
            minutes_watched=_num(r.get("estimatedMinutesWatched")),
            # Averages are meaningless with zero views; keep them unavailable.
            avg_view_duration_s=_num(r.get("averageViewDuration")) if views else None,
            avg_view_pct=_num(r.get("averageViewPercentage")) if views else None,
            subs_gained=_num(r.get("subscribersGained"), int),
            subs_lost=_num(r.get("subscribersLost"), int),
        )

    # ---- daily series ---------------------------------------------------
    def daily(self, period: Period) -> list[DailyStat]:
        rows = self.query(period, DAILY_METRICS, dimensions="day", sort="day")
        return [
            DailyStat(
                day=date.fromisoformat(r["day"]),
                views=_num(r.get("views"), int),
                minutes_watched=_num(r.get("estimatedMinutesWatched")),
                subs_gained=_num(r.get("subscribersGained"), int),
                subs_lost=_num(r.get("subscribersLost"), int),
            )
            for r in rows
        ]

    # ---- per-video rows -------------------------------------------------
    def _video_rows(self, period: Period, filters: str | None = None) -> list[VideoStats]:
        rows = self.query(period, VIDEO_METRICS, dimensions="video", sort="-views", maxResults=MAX_VIDEO_ROWS,
                          filters=filters)
        return [
            VideoStats(
                video_id=r["video"],
                views=_num(r.get("views"), int) or 0,
                minutes_watched=_num(r.get("estimatedMinutesWatched")) or 0.0,
                avg_view_duration_s=_num(r.get("averageViewDuration")),
                avg_view_pct=_num(r.get("averageViewPercentage")),
                subs_gained=_num(r.get("subscribersGained"), int),
                engaged_views=_num(r.get("engagedViews"), int),
            )
            for r in rows
        ]

    @staticmethod
    def _merge(merged: dict[str, VideoStats], rows: list[VideoStats], ct: str | None) -> None:
        """Add rows to ``merged``; a video seen again keeps additive metrics only (counted once, type unknown)."""
        for v in rows:
            if ct is not None:
                v.content_type = ct
            if v.video_id in merged:
                m = merged[v.video_id]
                m.views += v.views
                m.minutes_watched += v.minutes_watched
                m.engaged_views = (None if m.engaged_views is None or v.engaged_views is None
                                   else m.engaged_views + v.engaged_views)
                m.subs_gained = (None if m.subs_gained is None or v.subs_gained is None
                                 else m.subs_gained + v.subs_gained)
                m.avg_view_duration_s = m.avg_view_pct = m.content_type = None
            else:
                merged[v.video_id] = v

    @staticmethod
    def _account(cov: Coverage, ct: str, got: int, expected: int, capped: bool, suspected: bool = False,
                 unidentified: bool = False) -> int:
        """Compare one content type's rows with its API total; return the views counted for coverage.

        Each type counts at most up to its own total, so an excess in one type can
        never hide a shortfall in another. Shortfalls are attributed by cause;
        ``unidentified`` (ambiguously classified fallback rows) means no cause is claimed.
        """
        if got > expected:
            cov.excess_views += got - expected
            log.warning("Rows for %s sum to %d views, above the API format total %d: inconsistent data.",
                        ct, got, expected)
        elif got < expected:
            short = expected - got
            if capped:
                cov.truncated_views += short
                cause = "row cap reached"
            elif unidentified:
                cov.unidentified_views += short
                cause = "cause not identified: ambiguous format classification"
            elif suspected:
                cov.unexplained_views += short
                cause = "possible truncation"
            else:
                cov.unassigned_views += short
                cause = "not assigned to a video"
            log.info("Rows for %s sum to %d of %d views (%s).", ct, got, expected, cause)
        return min(got, expected)

    def videos_by_format(self, period: Period, format_totals: dict[str, FormatStats]
                         ) -> tuple[list[VideoStats], Coverage] | str:
        """Primary path: one ``creatorContentType``-filtered query per content type with views.

        * Every reported format (Shorts, long-form, livestream) is queried; rows
          become videos. Other types with views (e.g. ``posts``) are queried for
          coverage accounting only and are never returned as videos.
        * A shortfall against the format total is kept and attributed (row cap
          reached vs. views the API assigns to no video); the result is never
          discarded for it.
        * If some reported-format queries fail, the successful ones are kept and
          the failed formats are filled from the single query (only rows whose
          Analytics format is that failed format and that are not already present).
        * Returns the fallback reason (str) only when every reported-format query failed.
        """
        reference = sum(f.views or 0 for f in format_totals.values())  # every type, incl. unspecified and posts
        cov = Coverage(METHOD_BY_FORMAT, rows=0, views_rows=0, views_reference=reference)
        merged: dict[str, VideoStats] = {}
        capped_mins: list[int] = []
        confirmed_cap = False
        accounted = 0
        targets = [(ct, API_FILTER_VALUES[ct]) for ct in REPORTED_FORMATS]
        targets += [(ct, f.api_value) for ct, f in format_totals.items()
                    if ct not in REPORTED_FORMATS and (f.views or 0) > 0]
        for ct, api_value in targets:
            expected = (format_totals[ct].views or 0) if ct in format_totals else 0
            if not api_value:
                cov.failed_types.append(ct)
                continue
            try:
                rows = self._video_rows(period, filters=f"creatorContentType=={api_value}")
            except HttpError as exc:
                log.warning("Format-filtered video query failed for %s (%s).", ct, http_error_reason(exc))
                cov.failed_types.append(ct)
                continue
            capped = len(rows) >= MAX_VIDEO_ROWS
            if capped:
                confirmed_cap = True
                capped_mins.append(min(v.views for v in rows))
            accounted += self._account(cov, ct, sum(v.views for v in rows), expected, capped)
            if ct in REPORTED_FORMATS:
                self._merge(merged, rows, ct)
            else:
                cov.other_type_views[ct] = sum(v.views for v in rows)  # counted, never ranked as videos

        failed_formats = [ct for ct in REPORTED_FORMATS if ct in cov.failed_types]
        if len(failed_formats) == len(REPORTED_FORMATS):
            return FALLBACK_FILTER_FAILED
        if failed_formats:
            try:
                single, typed = self.videos(period)
            except HttpError as exc:
                log.warning("Single-query fallback for %s failed (%s).", ", ".join(failed_formats),
                            http_error_reason(exc))
                single, typed = [], False
            capped = len(single) >= MAX_VIDEO_ROWS
            suspected = not capped and len(single) >= SUSPECT_TRUNCATION_ROWS
            if typed:
                # Rows with views but no single Analytics format (reported under several types) that the
                # successful format queries did not return: they may belong to a failed format, so the
                # cause of a failed format's shortfall cannot be identified.
                ambiguous = any(v.content_type is None and v.views > 0 and v.video_id not in merged for v in single)
                for ct in failed_formats:
                    rows = [v for v in single if v.content_type == ct and v.video_id not in merged]
                    accounted += self._account(cov, ct, sum(v.views for v in rows),
                                               (format_totals[ct].views or 0) if ct in format_totals else 0,
                                               capped, suspected, unidentified=ambiguous)
                    self._merge(merged, rows, ct)
                if (capped or suspected) and single:
                    capped_mins.append(min(v.views for v in single))
                confirmed_cap = confirmed_cap or capped
                # Suspected truncation is reported only when a shortfall was actually attributed to it.
                cov.truncation_suspected = suspected and cov.unexplained_views > 0
                cov.method = METHOD_MIXED
                cov.fallback_reason = (f"format query failed for {', '.join(failed_formats)}; "
                                       "those rows come from the single query")
            else:
                cov.fallback_reason = (f"format query failed for {', '.join(failed_formats)} and the single-query "
                                       "format lookup is unavailable; those views are not covered")
        videos = sorted(merged.values(), key=lambda v: v.views, reverse=True)
        cov.rows = len(videos)
        cov.views_rows = accounted
        cov.capped = cov.capped or confirmed_cap
        cov.capped_min_views = min(capped_mins) if capped_mins else None
        return videos, cov

    def fetch_videos(self, period: Period, format_totals: dict[str, FormatStats] | None,
                     channel_views: int | None) -> tuple[list[VideoStats], bool, Coverage]:
        """Per-video rows with measured coverage: (videos, content_type_known, coverage).

        Primary: format-filtered queries (see ``videos_by_format``).
        Fallback (no format totals, or every format query failed): the single
        top-videos query plus the per-video format lookup.
        """
        reason = FALLBACK_NO_FORMAT_TOTALS
        if format_totals is not None:
            result = self.videos_by_format(period, format_totals)
            if not isinstance(result, str):
                videos, coverage = result
                return videos, True, coverage
            reason = result
        rows, typed = self.videos(period)
        # The unfiltered query also returns non-video content (live: a post with 13 views). Rows whose
        # Analytics type is known and not a video format count for coverage only, like on the primary path.
        videos: list[VideoStats] = []
        other: dict[str, int] = {}
        for v in rows:
            if v.content_type is not None and v.content_type not in REPORTED_FORMATS:
                other[v.content_type] = other.get(v.content_type, 0) + v.views
            else:
                videos.append(v)
        reference = sum(f.views or 0 for f in format_totals.values()) if format_totals is not None else channel_views
        rows_sum = sum(v.views for v in rows)  # every returned row, incl. non-video content
        capped = len(rows) >= MAX_VIDEO_ROWS
        # Fewer than 200 rows can still be truncated (observed: 199 rows while more videos had views).
        suspected = (not capped and len(rows) >= SUSPECT_TRUNCATION_ROWS
                     and reference is not None and rows_sum < reference)
        # ``rows`` = rows returned by the API (the cap and the suspicion refer to this count).
        coverage = Coverage(METHOD_SINGLE_QUERY, rows=len(rows), views_rows=rows_sum,
                            views_reference=reference, capped=capped,
                            capped_min_views=min(v.views for v in rows) if (capped or suspected) else None,
                            fallback_reason=reason, truncation_suspected=suspected, other_type_views=other)
        return videos, typed, coverage

    def videos(self, period: Period) -> tuple[list[VideoStats], bool]:
        """Return (videos sorted by views desc, content_type_known).

        The top-videos list uses the compatible ``dimensions=video`` query
        (``video,creatorContentType`` without a filter is rejected with 400).
        Formats are then looked up per video with the filtered query.
        """
        videos = self._video_rows(period)
        videos.sort(key=lambda v: v.views, reverse=True)

        try:
            types = self.video_content_types(period, [v.video_id for v in videos if v.views > 0])
        except HttpError as exc:
            log.warning("Per-video format lookup failed (%s).", http_error_reason(exc))
            return videos, False
        for v in videos:
            v.content_type = types.get(v.video_id)  # None = not reported by the API -> unknown
        return videos, True

    def video_content_types(self, period: Period, video_ids: list[str]) -> dict[str, str | None]:
        """Official per-video creatorContentType via a video-ID filter.

        A video reported under more than one content type in the period is
        mapped to None (ambiguous) instead of being guessed.
        """
        found: dict[str, set[str]] = {}
        for i in range(0, len(video_ids), CLASSIFY_BATCH):
            chunk = video_ids[i : i + CLASSIFY_BATCH]
            rows = self.query(period, "views", dimensions="video,creatorContentType", filters="video==" + ",".join(chunk))
            for r in rows:
                ct = normalize_content_type(r.get("creatorContentType"))
                if ct:
                    found.setdefault(r["video"], set()).add(ct)
        ambiguous = [vid for vid, cts in found.items() if len(cts) > 1]
        if ambiguous:
            log.info("%d video(s) reported under multiple content types; left unclassified.", len(ambiguous))
        return {vid: (next(iter(cts)) if len(cts) == 1 else None) for vid, cts in found.items()}

    # ---- Shorts vs long-form vs live ---------------------------------------
    def format_totals(self, period: Period) -> dict[str, FormatStats]:
        """Authoritative per-format totals from the API. Raises HttpError on failure."""
        rows = self.query(period, FORMAT_METRICS, dimensions="creatorContentType")
        # A successful query that omits a format means that format had no views (a real zero);
        # its averages stay None because they are undefined without views.
        result = {ct: FormatStats(ct, views=0, minutes_watched=0.0, source=SOURCE_API) for ct in REPORTED_FORMATS}
        for r in rows:
            ct = normalize_content_type(r.get("creatorContentType"))
            if ct is None:
                continue
            views = _num(r.get("views"), int)
            result[ct] = FormatStats(
                content_type=ct,
                views=views,
                minutes_watched=_num(r.get("estimatedMinutesWatched")),
                avg_view_duration_s=_num(r.get("averageViewDuration")) if views else None,
                avg_view_pct=_num(r.get("averageViewPercentage")) if views else None,
                source=SOURCE_API,
                api_value=r.get("creatorContentType"),
            )
        return result

def unavailable_formats() -> dict[str, FormatStats]:
    return {ct: FormatStats(ct, source=SOURCE_UNAVAILABLE) for ct in REPORTED_FORMATS}


def fallback_formats(videos: list[VideoStats] | None, videos_typed: bool,
                     complete: bool | None = None) -> dict[str, FormatStats]:
    """Derived totals when per-video formats are known, otherwise unavailable.

    ``complete`` comes from the measured ``Coverage`` (v1.2.0); the row-count
    heuristic remains only for callers that do not measure coverage.
    """
    if videos is not None and videos_typed:
        return derive_formats(videos, complete=len(videos) < MAX_VIDEO_ROWS if complete is None else complete)
    return unavailable_formats()


def derived_avg_view_duration(videos: list[VideoStats]) -> float | None:
    """AVD of a group of videos, weighted by engagedViews.

    ``averageViewDuration`` is an average per playback, and its weighting base
    matches ``engagedViews`` (verified: format and daily AVDs weighted by
    engagedViews reproduce the channel AVD exactly). ``watch time / views``
    would understate it, because ``views`` also counts non-engaged starts.
    Returns None when any contributing video lacks the inputs or when there
    are no engaged views — never a guessed value.
    """
    rows = [v for v in videos if v.views > 0]
    if not rows or any(v.engaged_views is None or v.avg_view_duration_s is None for v in rows):
        return None
    weight = sum(v.engaged_views for v in rows)
    if weight <= 0:
        return None
    return sum(v.avg_view_duration_s * v.engaged_views for v in rows) / weight


def derive_formats(videos: list[VideoStats], complete: bool) -> dict[str, FormatStats]:
    """Aggregate per-video rows into format totals. Marked as SOURCE_DERIVED.

    * views and watch time: plain sums (additive metrics).
    * AVD: engagedViews-weighted mean of per-video AVD.
    * Average % viewed: unavailable — the API value is total watched time over
      total length of played content, which cannot be rebuilt reliably without
      per-playback video lengths.
    """
    result: dict[str, FormatStats] = {}
    for ct in REPORTED_FORMATS:
        subset = [v for v in videos if v.content_type == ct]
        result[ct] = FormatStats(
            content_type=ct,
            views=sum(v.views for v in subset),
            minutes_watched=sum(v.minutes_watched for v in subset),
            avg_view_duration_s=derived_avg_view_duration(subset),
            avg_view_pct=None,
            source=SOURCE_DERIVED,
            complete=complete,
        )
    return result
