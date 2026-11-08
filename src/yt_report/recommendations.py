"""Rule-based recommendation engine (default, no paid API required).

Every rule only fires on measured values and cites those values, so the
output is traceable to the data. Lower ``priority`` = more important.

Relationships between metrics are described as "coincided with" — the data
shows co-occurrence, never a proven cause.
"""

from __future__ import annotations

from .analysis import format_share, pct_change, retention_changes
from .formatting import fmt_duration, fmt_int, fmt_pct, fmt_pp
from .models import FORMAT_LABELS, LONG_FORM, REPORTED_FORMATS, SHORTS, Recommendation, WeeklyReport

# Thresholds are deliberately conservative and documented in ARCHITECTURE.md.
SIGNIFICANT_CHANGE = 15.0        # % week-over-week
LONG_FORM_LOW_RETENTION = 35.0   # avg % viewed
LONG_FORM_GOOD_RETENTION = 50.0
SHORTS_LOW_RETENTION = 70.0
LOW_SUB_CONVERSION = 1.0         # net subscribers per 1,000 views
TOP_VIDEO_DOMINANCE = 50.0       # % of channel views from one video
MIN_VIEWS_FOR_RULES = 100        # below this, percentages are too noisy
# Views-trend decision tree (v1.2.0)
NEW_UPLOAD_DOMINANCE = 50.0      # % of a period's views from that period's own new uploads
CATALOG_STABLE = 15.0            # |% change| of older-catalog views still counted as "stable"

BRANCH_UPLOAD_SPIKE = "upload_spike"
BRANCH_PUBLISHING = "publishing"
BRANCH_PERFORMANCE = "performance"
BRANCH_UNAVAILABLE = "unavailable"
BRANCH_UNSEPARATED = "unseparated"

SHORT_LABELS = {SHORTS: "Shorts", LONG_FORM: "long-form videos", "LIVE_STREAM": "livestreams"}


def _publication_diffs(report: WeeklyReport, direction: int) -> list[tuple[str, int, int]]:
    """Formats whose public-upload count moved in ``direction`` (-1 fewer, +1 more) vs. the previous period."""
    pc, pp = report.publications_current, report.publications_previous
    out = []
    for ct in REPORTED_FORMATS:
        c, p = pc.count(ct), pp.count(ct)
        if (direction < 0 and p >= 1 and c < p) or (direction > 0 and c > p):
            out.append((ct, c, p))
    return out


def _retention_summary(report: WeeklyReport) -> str:
    parts = [f"{rc.label} {rc.status}" for rc in retention_changes(report.formats_current, report.formats_previous)
             if rc.status != "insufficient"]
    return f" Retention by format: {', '.join(parts)}." if parts else ""


def views_trend_branch(report: WeeklyReport, direction: int) -> str:
    """Deterministic decision tree for a significant views change; first matching branch wins.

    1. upload_spike  – the dominant period's new uploads made >= 50 % of its views and the
                       older catalog (same videos in both periods) moved less than ±15 %.
    2. publishing    – fewer (decline) / more (growth) public uploads in at least one format.
    3. performance   – publishing unchanged in that direction and BOTH new-upload views and
                       older-catalog views moved in the same direction as the total.
    4. unavailable   – publication statistics missing or incomplete.
       unseparated   – data complete but none of the branches above applies.
    """
    pc, pp, age = report.publications_current, report.publications_previous, report.views_by_age
    if (pc is None or pp is None or not pc.available or not pp.available or not pc.complete or not pp.complete):
        return BRANCH_UNAVAILABLE
    if age is not None and age.complete:
        share = age.new_share_prev if direction < 0 else age.new_share_cur
        older = pct_change(age.older_cur, age.older_prev)
        if share is not None and share >= NEW_UPLOAD_DOMINANCE and older is not None and abs(older) < CATALOG_STABLE:
            return BRANCH_UPLOAD_SPIKE
    if _publication_diffs(report, direction):
        return BRANCH_PUBLISHING
    if age is not None and age.complete:
        new_change, older = pct_change(age.new_cur, age.new_prev), pct_change(age.older_cur, age.older_prev)
        if new_change is not None and older is not None and new_change * direction > 0 and older * direction > 0:
            return BRANCH_PERFORMANCE
    return BRANCH_UNSEPARATED


def _views_trend(report: WeeklyReport, vchange: float) -> Recommendation:
    cur, prev = report.current, report.previous
    direction = -1 if vchange < 0 else 1
    verb = "fell" if direction < 0 else "rose"
    head = f"Views {verb} {fmt_pct(abs(vchange))} ({fmt_int(prev.views)} → {fmt_int(cur.views)})."
    branch = views_trend_branch(report, direction)
    age = report.views_by_age
    priority = 20 if direction < 0 else 30

    if branch == BRANCH_UPLOAD_SPIKE:
        older = pct_change(age.older_cur, age.older_prev)
        catalog = (f"views of older videos changed by {fmt_pct(older) if older < 0 else '+' + fmt_pct(older)} "
                   f"({fmt_int(age.older_prev)} → {fmt_int(age.older_cur)})")
        if direction < 0:
            return Recommendation(
                "Views fell after last week's upload spike",
                f"{head} Last week's own new uploads brought {fmt_pct(age.new_share_prev)} of its views "
                f"({fmt_int(age.new_prev)}); this week they added {fmt_int(age.prev_uploads_cur)}, while {catalog}. "
                "The drop coincided with those uploads cooling off, not with a decline of the catalog — "
                "plan the next upload rather than reworking existing videos.",
                priority=priority,
            )
        return Recommendation(
            "Views rose with this week's new uploads",
            f"{head} This week's new uploads brought {fmt_pct(age.new_share_cur)} of views "
            f"({fmt_int(age.new_cur)}), while {catalog}. The rise coincided with the new uploads; expect views to "
            "move back toward the catalog baseline as they cool off unless the next uploads follow.",
            priority=priority,
        )

    if branch == BRANCH_PUBLISHING:
        diffs = _publication_diffs(report, direction)
        word = "Fewer" if direction < 0 else "More"
        parts = "; ".join(f"{SHORT_LABELS.get(ct, FORMAT_LABELS.get(ct, ct))} {c} vs. {p}" for ct, c, p in diffs)
        split = ""
        if age is not None and age.complete:
            split = (f" New-upload views {fmt_int(age.new_prev)} → {fmt_int(age.new_cur)}; "
                     f"older catalog {fmt_int(age.older_prev)} → {fmt_int(age.older_cur)}.")
        if direction < 0:
            return Recommendation(
                "Publishing slowed down",
                f"{head} {word} public uploads than the previous week ({parts}); the drop in views coincided with "
                f"this.{split} If the earlier rhythm was sustainable, returning to it is the first thing to test.",
                priority=priority,
            )
        return Recommendation(
            "Publishing picked up",
            f"{head} {word} public uploads than the previous week ({parts}); the rise in views coincided with this."
            f"{split} Keep the higher rhythm if it is sustainable and compare again next week.",
            priority=priority,
        )

    if branch == BRANCH_PERFORMANCE:
        pc, pp = report.publications_current, report.publications_previous
        uploads = f"{pc.total_public} vs. {pp.total_public} public uploads"
        detail = (f"new-upload views {fmt_int(age.new_prev)} → {fmt_int(age.new_cur)}, "
                  f"older-catalog views {fmt_int(age.older_prev)} → {fmt_int(age.older_cur)}")
        if direction < 0:
            return Recommendation(
                "Views fell across new and older videos",
                f"{head} Publishing did not slow down ({uploads}), yet both {detail} declined. Check the click-through "
                "rate of this week's uploads in YouTube Studio and refresh the titles/thumbnails that underperform."
                + _retention_summary(report),
                priority=priority,
            )
        top = report.top_videos[0] if report.top_videos else None
        lead = (f" The most-viewed video was “{top.title or top.video_id}” with {fmt_int(top.views)} views — "
                "plan a follow-up on the same topic or format.") if top else ""
        return Recommendation(
            "Double down on what grew views",
            f"{head} Publishing did not increase ({uploads}), yet both {detail} grew.{lead}" + _retention_summary(report),
            priority=priority,
        )

    if branch == BRANCH_UNAVAILABLE:
        return Recommendation(
            "Views declined" if direction < 0 else "Views increased",
            f"{head} Publication statistics are unavailable or incomplete for this comparison, so the change cannot "
            "be separated into new-upload and catalog effects with the available data.",
            priority=priority,
        )

    detail = ""
    if age is not None:
        detail = (f" New uploads: {fmt_int(age.new_prev)} → {fmt_int(age.new_cur)} views; older catalog: "
                  f"{fmt_int(age.older_prev)} → {fmt_int(age.older_cur)}.")
    return Recommendation(
        "Views declined" if direction < 0 else "Views increased",
        f"{head}{detail} No single pattern (upload spike, publishing change, or a decline/rise across both new and "
        "older videos) explains it with the available data; keep the current plan and compare next week.",
        priority=priority,
    )


def build_recommendations(report: WeeklyReport, limit: int = 3) -> list[Recommendation]:
    recs: list[Recommendation] = []
    cur, prev = report.current, report.previous
    views = cur.views or 0

    if views < MIN_VIEWS_FOR_RULES:
        recs.append(Recommendation(
            "Build a consistent upload rhythm",
            f"The channel had {fmt_int(views)} views this period — too few for reliable trend analysis. "
            "Publishing on fixed days for the next weeks will create enough data for meaningful comparisons.",
            priority=10,
        ))

    # 1) Views trend
    vchange = pct_change(cur.views, prev.views)
    if vchange is not None and views >= MIN_VIEWS_FOR_RULES and abs(vchange) >= SIGNIFICANT_CHANGE:
        recs.append(_views_trend(report, vchange))

    # 2) Single-video dependency
    if report.top_videos and views >= MIN_VIEWS_FOR_RULES:
        top = report.top_videos[0]
        share = top.views / views * 100 if views else 0
        if share >= TOP_VIDEO_DOMINANCE:
            recs.append(Recommendation(
                "Reduce dependence on one video",
                f"“{top.title or top.video_id}” generated {fmt_pct(share)} of all views. "
                "Link it to related videos (end screens, pinned comment, playlist) to spread traffic.",
                priority=35,
            ))

    # 3) Format-specific retention
    lf = report.formats_current.get(LONG_FORM)
    sh = report.formats_current.get(SHORTS)
    if lf and lf.avg_view_pct is not None and (lf.views or 0) >= MIN_VIEWS_FOR_RULES:
        if lf.avg_view_pct < LONG_FORM_LOW_RETENTION:
            recs.append(Recommendation(
                "Strengthen long-form intros",
                f"Long-form viewers watched {fmt_pct(lf.avg_view_pct)} of a video on average "
                f"({fmt_duration(lf.avg_view_duration_s)}). Deliver the promised payoff within the first "
                "30 seconds and cut slow segments.",
                priority=25,
            ))
        elif lf.avg_view_pct >= LONG_FORM_GOOD_RETENTION:
            recs.append(Recommendation(
                "Leverage strong long-form retention",
                f"Long-form retention is {fmt_pct(lf.avg_view_pct)} — viewers stay. Consider slightly longer "
                "videos or adding a mid-roll call to subscribe.",
                priority=60,
            ))
    if sh and sh.avg_view_pct is not None and (sh.views or 0) >= MIN_VIEWS_FOR_RULES:
        if sh.avg_view_pct < SHORTS_LOW_RETENTION:
            recs.append(Recommendation(
                "Tighten the first seconds of Shorts",
                f"Shorts were watched {fmt_pct(sh.avg_view_pct)} on average. Start with the strongest moment "
                "and remove any intro so viewers do not swipe away.",
                priority=28,
            ))

    # 3b) Week-over-week retention, each format against itself (never the channel-wide average)
    changes = retention_changes(report.formats_current, report.formats_previous)
    for rc in changes:
        if rc.status == "declined":
            recs.append(Recommendation(
                f"{rc.label} retention dropped",
                f"{rc.label} viewers watched {fmt_pct(rc.current_pct)} on average vs. {fmt_pct(rc.previous_pct)} "
                f"the previous week ({fmt_pp(rc.delta_pp)}; {fmt_int(rc.current_views)} vs. {fmt_int(rc.previous_views)} views). "
                f"Compare the openings of this week's {rc.label.lower()} uploads with last week's to find what changed.",
                priority=22,
            ))
        elif rc.status == "improved":
            recs.append(Recommendation(
                f"{rc.label} retention improved",
                f"{rc.label} viewers watched {fmt_pct(rc.current_pct)} on average vs. {fmt_pct(rc.previous_pct)} "
                f"the previous week ({fmt_pp(rc.delta_pp)}; {fmt_int(rc.current_views)} vs. {fmt_int(rc.previous_views)} views). "
                f"Compare this week's {rc.label.lower()} openings with last week's to identify what differs, "
                "and repeat it in the next uploads.",
                priority=55,
            ))

    # 4) Format mix: views vs watch time
    share = format_share(report.formats_current)
    sv, lm = share[SHORTS]["views"], share[LONG_FORM]["minutes"]
    if sv is not None and lm is not None and sv >= 60 and lm >= 50 and views >= MIN_VIEWS_FOR_RULES:
        recs.append(Recommendation(
            "Funnel Shorts viewers to long-form",
            f"Shorts brought {fmt_pct(sv)} of views but long-form produced {fmt_pct(lm)} of watch time. "
            "Use Shorts' related-video link to point viewers to a matching long video.",
            priority=40,
        ))

    # 5) Subscribers
    net = cur.net_subs
    if net is not None and views >= MIN_VIEWS_FOR_RULES:
        if net < 0:
            recs.append(Recommendation(
                "Investigate subscriber losses",
                f"The channel lost more subscribers than it gained ({fmt_int(cur.subs_gained)} gained, "
                f"{fmt_int(cur.subs_lost)} lost). Review whether recent uploads match what existing "
                "subscribers expect.",
                priority=15,
            ))
        else:
            per_k = net / views * 1000 if views else 0
            if per_k < LOW_SUB_CONVERSION:
                recs.append(Recommendation(
                    "Improve subscriber conversion",
                    f"Only {per_k:.2f} net subscribers per 1,000 views. Add a clear, specific reason to subscribe "
                    "(what viewers get next) near the end of your best-performing videos.",
                    priority=45,
                ))

    # 6) Weakest video
    if report.weakest:
        w = report.weakest
        recs.append(Recommendation(
            "Review the weakest video",
            f"“{w.video.title or w.video.video_id}” kept viewers for {fmt_pct(w.video.avg_view_pct)} vs. a "
            f"{fmt_pct(w.format_median_pct)} median for {FORMAT_LABELS.get(w.video.content_type, 'its format')} videos. "
            "Check its audience-retention graph in YouTube Studio for the exact drop-off point.",
            priority=50,
        ))

    recs.sort(key=lambda r: r.priority)
    unique: list[Recommendation] = []
    for r in recs:
        if r.title not in {u.title for u in unique}:
            unique.append(r)

    # Ensure at least two data-backed items even for a stable, healthy week.
    if len(unique) < 2 and report.top_videos:
        top = report.top_videos[0]
        unique.append(Recommendation(
            "Build on your best performer",
            f"“{top.title or top.video_id}” led the week with {fmt_int(top.views)} views and "
            f"{fmt_pct(top.avg_view_pct)} average viewed. Reuse its topic, title structure or hook in the next upload.",
            priority=90,
        ))
    # Stable retention is only reported per format with enough data in both weeks;
    # the channel-wide average % viewed is never used (it moves with the format mix).
    stable = [rc for rc in changes if rc.status == "stable"]
    if len(unique) < 2 and stable:
        parts = "; ".join(f"{rc.label} {fmt_pct(rc.current_pct)} ({fmt_pp(rc.delta_pp)})" for rc in stable)
        unique.append(Recommendation(
            "Retention is stable within each format",
            f"Week-over-week average % viewed, compared format by format: {parts}. "
            "No format changed significantly, so test one new opening style in the next upload and compare.",
            priority=95,
        ))
    return unique[:limit]
