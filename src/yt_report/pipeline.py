"""End-to-end report building: API -> analysis -> report object."""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from . import analysis
from .analytics_client import AnalyticsClient, fallback_formats
from .config import Config
from .models import DailyStat, FormatStats, PublicationStats, Recommendation, VideoStats, WeeklyReport
from .periods import WEEKDAYS, Period, analytics_today, compute_periods
from .recommendations import build_recommendations
from .retry import http_error_reason
from .youtube_client import ChannelInfo, VideoMeta, YouTubeClient

log = logging.getLogger(__name__)


class ReportDataError(RuntimeError):
    """The API returned no usable data for the reporting period; no report should be sent."""


class IncompletePeriodError(ReportDataError):
    """The last day(s) of the current period have no Analytics data yet and --allow-incomplete was not given."""


def last_day_with_data(daily: list[DailyStat], period: Period) -> date | None:
    """Last day of ``period`` that has a row in the daily series (None if none has)."""
    days = [d.day for d in daily if period.start <= d.day <= period.end]
    return max(days) if days else None


def _partial_recommendation(current: Period, through: date) -> Recommendation:
    n = (through - current.start).days + 1
    return Recommendation(
        "Recommendations paused — partial week",
        f"YouTube Analytics data covers {n} of {current.days} days ({current.start:%b} {current.start.day} – "
        f"{through:%b} {through.day}). Automatic recommendations compare complete weeks, so none are made from "
        "this partial data; they will be included in the complete report.",
        priority=0,
    )


def _api_format_totals(analytics: AnalyticsClient, period: Period) -> dict[str, FormatStats] | None:
    """API format totals, or None when the query fails (the fallback decides what to show)."""
    try:
        return analytics.format_totals(period)
    except HttpError as exc:
        log.warning("Format totals for %s..%s failed (%s); using fallback.", period.start, period.end,
                    http_error_reason(exc))
        return None


def _merge_types(*video_lists: list[VideoStats]) -> dict[str, str | None]:
    """Analytics content type per video from the per-video rows; conflicting types -> None."""
    found: dict[str, set[str]] = {}
    for videos in video_lists:
        for v in videos:
            if v.content_type:
                found.setdefault(v.video_id, set()).add(v.content_type)
    return {vid: (next(iter(cts)) if len(cts) == 1 else None) for vid, cts in found.items()}


def _publications(youtube: YouTubeClient, analytics: AnalyticsClient, channel: ChannelInfo, current: Period,
                  previous: Period, meta: dict[str, VideoMeta], videos_cur: list[VideoStats],
                  videos_prev: list[VideoStats]) -> tuple[PublicationStats, PublicationStats]:
    """Publication statistics for both periods. Never raises for API errors (marked unavailable)."""
    if not channel.uploads_playlist_id:
        na = analysis.unavailable_publications("no_uploads_playlist")
        return na, analysis.unavailable_publications("no_uploads_playlist")
    try:
        scan = youtube.list_uploads(channel.uploads_playlist_id)
        missing = [v for v in scan.video_ids if v not in meta]
        meta = {**meta, **youtube.get_videos(missing)}
    except HttpError as exc:
        log.warning("Uploads scan failed (%s); publication statistics unavailable.", http_error_reason(exc))
        return analysis.unavailable_publications("api_error"), analysis.unavailable_publications("api_error")

    uploads = {v: meta[v] for v in scan.video_ids if v in meta}
    types = _merge_types(videos_cur, videos_prev)
    window = Period(previous.start, current.end)
    candidates = [v for v in analysis.publication_candidates(uploads, window.start, window.end)
                  if v not in types and uploads[v].live_broadcast_content != "upcoming"]
    if candidates:
        # Official classification for new uploads that are not in the per-video rows (e.g. no views yet).
        try:
            types.update({k: v for k, v in analytics.video_content_types(window, candidates).items()})
        except HttpError as exc:
            log.warning("Format lookup for new uploads failed (%s); they stay unclassified.", http_error_reason(exc))
    pubs = tuple(analysis.publication_stats(p, scan.video_ids, meta, types, scan.complete, scan.reason,
                                            item_dates=scan.item_dates)
                 for p in (current, previous))
    return pubs[0], pubs[1]


def build_report(cfg: Config, youtube: YouTubeClient, analytics: AnalyticsClient, end: date | None = None,
                 today: date | None = None, history: list[tuple[str, Period]] | None = None,
                 allow_incomplete: bool = False) -> WeeklyReport:
    """``history``: (channel_id, period) of stored reports, used only to flag overlapping periods.

    If the last day(s) of the current period have no Analytics data yet, the run stops with
    ``IncompletePeriodError`` unless ``allow_incomplete`` is set; then the report is marked partial
    (``data_through``), carries no week-over-week judgement and no automatic recommendations.
    """
    today = today or analytics_today()
    current, previous = compute_periods(today, cfg.data_lag_days, cfg.period_days, end=end,
                                        week_end=cfg.report_week_end)
    if end is not None and cfg.report_week_end is not None and end.weekday() != cfg.report_week_end:
        log.warning("Explicit --end-date %s is a %s, not the configured week end (%s); the period is used as given.",
                    end, WEEKDAYS[end.weekday()].title(), WEEKDAYS[cfg.report_week_end].title())
    log.info("Reporting period %s..%s (previous %s..%s)", current.start, current.end, previous.start, previous.end)

    channel = youtube.get_channel()
    totals_cur = analytics.channel_totals(current)
    if totals_cur.views is None:
        raise ReportDataError(
            f"YouTube Analytics returned no data for {current.start}..{current.end}. "
            "The data may not be processed yet — try again later or increase DATA_LAG_DAYS."
        )
    totals_prev = analytics.channel_totals(previous)

    # Completeness of the current period is checked before the expensive per-video queries.
    daily = analytics.daily(Period(previous.start, current.end))
    through = last_day_with_data(daily, current)
    data_through = None
    if through is None or through < current.end:
        available = (f"only through {through:%b} {through.day} ({(through - current.start).days + 1} of "
                     f"{current.days} days)" if through else "for no day")
        message = (f"YouTube Analytics data for {current.label()} is available {available}; the remaining days "
                   "are not processed yet, so the period cannot be confirmed complete. Run again later "
                   "(scheduled runs: Tuesday from 10:00 CET) or pass --allow-incomplete for a clearly labelled "
                   "partial report.")
        if not allow_incomplete or through is None:
            raise IncompletePeriodError(message)
        data_through = through
        log.warning("Partial report: %s", message)

    api_cur = _api_format_totals(analytics, current)
    api_prev = _api_format_totals(analytics, previous)
    videos, typed, cov_cur = analytics.fetch_videos(current, api_cur, totals_cur.views)
    videos_prev, typed_prev, cov_prev = analytics.fetch_videos(previous, api_prev, totals_prev.views)
    formats_cur = api_cur if api_cur is not None else fallback_formats(videos, typed, cov_cur.complete)
    formats_prev = api_prev if api_prev is not None else fallback_formats(videos_prev, typed_prev, cov_prev.complete)

    # Enrich all current rows (<=600 videos) so top and weakest both get titles.
    meta = youtube.get_videos([v.video_id for v in videos])
    for v in videos:
        m = meta.get(v.video_id)
        if m:
            v.title, v.thumbnail_url, v.published_at = m.title, m.thumbnail_url, m.published_at
        elif not v.title:
            v.title = "(private or deleted video)"

    pubs_cur, pubs_prev = _publications(youtube, analytics, channel, current, previous, meta, videos, videos_prev)
    # Same base as the per-video rows (format totals); channel totals only when no coverage reference exists.
    base_cur = cov_cur.views_reference if cov_cur.views_reference is not None else totals_cur.views
    base_prev = cov_prev.views_reference if cov_prev.views_reference is not None else totals_prev.views
    by_age = analysis.views_by_age(videos, videos_prev, base_cur, base_prev, pubs_cur, pubs_prev, cov_cur, cov_prev)

    top = analysis.top_videos(videos, cfg.top_videos_count)
    if typed:
        weakest, weakest_note = analysis.find_weakest(videos, cfg.min_views_weakest, cfg.min_videos_per_format)
    else:
        weakest, weakest_note = None, "Content format per video unavailable — a fair same-format comparison is not possible."

    notes = analysis.data_quality_notes(formats_cur, formats_prev, videos_typed=typed)
    if data_through is not None:
        notes.insert(0, analysis.NOTE_PARTIAL.format(
            n=(data_through - current.start).days + 1, days=current.days, period=current.label(),
            start=f"{current.start:%b} {current.start.day}", through=f"{data_through:%b} {data_through.day}"))
    if totals_prev.views is None:
        notes.append("No data for the previous period; week-over-week comparisons are unavailable.")
    overlaps = analysis.period_overlaps(history or [], channel.channel_id, current, previous)
    if overlaps:
        notes.insert(0, analysis.NOTE_OVERLAP.format(periods=", ".join(p.label() for p in overlaps),
                                                     current=current.label(), previous=previous.label()))
        log.warning("Report periods overlap stored report(s): %s", ", ".join(p.label() for p in overlaps))
    consistency = analysis.format_consistency_note(formats_cur, totals_cur)
    if consistency:
        log.warning(consistency)
        notes.append(consistency)
    gaps = analysis.missing_days(daily, previous.start, current.end)
    notes += analysis.completeness_notes(cov_cur, cov_prev, pubs_cur, pubs_prev, gaps, bool(daily),
                                         weakest_min_views=cfg.min_views_weakest if typed else None)

    report = WeeklyReport(
        channel_id=channel.channel_id,
        channel_title=channel.title,
        subscriber_count=channel.subscriber_count,
        period=current,
        previous_period=previous,
        current=totals_cur,
        previous=totals_prev,
        formats_current=formats_cur,
        formats_previous=formats_prev,
        videos=videos,
        top_videos=top,
        weakest=weakest,
        weakest_note=weakest_note,
        daily=daily,
        generated_at=datetime.now(ZoneInfo(cfg.report_timezone)),
        notes=notes,
        coverage_current=cov_cur,
        coverage_previous=cov_prev,
        publications_current=pubs_cur,
        publications_previous=pubs_prev,
        views_by_age=by_age,
        missing_days=gaps if daily else [],
        period_overlaps=overlaps,
        data_through=data_through,
    )
    if report.partial:
        # No rule or AI recommendations from a partial week (they compare complete weeks).
        report.recommendations = [_partial_recommendation(current, data_through)]
        return report
    report.recommendations = build_recommendations(report)

    if cfg.ai_enabled:
        from .ai_recommendations import claude_recommendations

        ai = claude_recommendations(report, cfg.anthropic_api_key or "", cfg.anthropic_model)
        if ai:
            report.recommendations = ai
            log.info("Using Claude API recommendations.")
    return report
