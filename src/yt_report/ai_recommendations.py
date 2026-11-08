"""Optional Claude API recommendations.

Disabled unless AI_RECOMMENDATIONS=true AND ANTHROPIC_API_KEY is set.
Claude only receives already-calculated, verified metrics (no raw API
access) and is instructed not to infer anything beyond them. Any failure
falls back to the rule-based engine.
"""

from __future__ import annotations

import json
import logging

from .analysis import compare_totals, format_share, retention_changes
from .models import Recommendation, WeeklyReport

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a YouTube analytics assistant. You receive verified, pre-calculated metrics for one "
    "channel. Write 2-3 specific, actionable recommendations. Rules: use ONLY the numbers provided; "
    "cite the exact figures you rely on; never invent metrics, causes, audience demographics or "
    "benchmarks that are not in the data; if data is marked unavailable, do not speculate about it. "
    "Shorts retention can exceed 100% because of loops; do not compare retention across formats. Livestreams are a separate format from long-form. "
    "Channel-wide average view duration and average % viewed move with the format mix: judge retention "
    "only from 'retention_by_format' (each format vs. itself last week; 'insufficient' means do not comment). "
    "Describe relationships between metrics as 'coincided with' — never claim a cause. Use 'publications' and "
    "'views_by_age' to explain views changes; if they are null or incomplete, say the cause cannot be separated. "
    "Do not suggest changing titles or thumbnails merely because total views declined."
)

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                "required": ["title", "body"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["recommendations"],
    "additionalProperties": False,
}


def metrics_payload(report: WeeklyReport) -> dict:
    def r(x, d=2):
        return None if x is None else round(x, d)

    return {
        "period": {"start": report.period.start.isoformat(), "end": report.period.end.isoformat()},
        "comparisons": [
            {"metric": c.label, "current": r(c.current), "previous": r(c.previous), "change_pct": r(c.change_pct, 1)}
            for c in compare_totals(report.current, report.previous)
        ],
        "formats": {
            ct: {
                "views": f.views, "watch_hours": r(f.watch_hours), "avg_view_duration_s": r(f.avg_view_duration_s, 0),
                "avg_view_pct": r(f.avg_view_pct, 1), "source": f.source,
            }
            for ct, f in report.formats_current.items()
        },
        "format_share_pct": {ct: {k: r(v, 1) for k, v in s.items()} for ct, s in format_share(report.formats_current).items()},
        "top_videos": [
            {"title": v.title, "format": v.content_type, "views": v.views, "watch_hours": r(v.watch_hours),
             "avg_view_pct": r(v.avg_view_pct, 1)}
            for v in report.top_videos
        ],
        "weakest_video": None if not report.weakest else {
            "title": report.weakest.video.title, "format": report.weakest.video.content_type,
            "avg_view_pct": r(report.weakest.video.avg_view_pct, 1),
            "format_median_pct": r(report.weakest.format_median_pct, 1),
        },
        "retention_by_format": [
            {"format": rc.label, "current_pct": r(rc.current_pct, 1), "previous_pct": r(rc.previous_pct, 1),
             "delta_pp": r(rc.delta_pp, 1), "status": rc.status}
            for rc in retention_changes(report.formats_current, report.formats_previous)
        ],
        "publications": None if not report.publications_current or not report.publications_current.available else {
            which: {"public_by_format": p.published, "unclassified": p.unclassified, "complete": p.complete}
            for which, p in (("current", report.publications_current), ("previous", report.publications_previous))
            if p is not None
        },
        "views_by_age": None if report.views_by_age is None else {
            "new_uploads_current": report.views_by_age.new_cur,
            "previous_uploads_in_current": report.views_by_age.prev_uploads_cur,
            "older_catalog_current": report.views_by_age.older_cur,
            "new_uploads_previous": report.views_by_age.new_prev,
            "older_catalog_previous": report.views_by_age.older_prev,
            "complete": report.views_by_age.complete,
        },
        "data_notes": report.notes,
    }


def claude_recommendations(report: WeeklyReport, api_key: str, model: str) -> list[Recommendation] | None:
    try:
        import anthropic
    except ImportError:
        log.warning("AI recommendations enabled but the 'anthropic' package is not installed (pip install .[ai]).")
        return None

    client = anthropic.Anthropic(api_key=api_key, timeout=120.0, max_retries=2)
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            messages=[{"role": "user", "content": "Metrics (JSON):\n" + json.dumps(metrics_payload(report), ensure_ascii=False)}],
        )
    except anthropic.AuthenticationError:
        log.warning("Claude API key rejected; using rule-based recommendations.")
        return None
    except anthropic.RateLimitError:
        log.warning("Claude API rate-limited; using rule-based recommendations.")
        return None
    except anthropic.APIStatusError as exc:
        log.warning("Claude API error %s; using rule-based recommendations.", exc.status_code)
        return None
    except anthropic.APIConnectionError:
        log.warning("Claude API unreachable; using rule-based recommendations.")
        return None

    if msg.stop_reason != "end_turn":
        log.warning("Claude response not usable (stop_reason=%s); using rules.", msg.stop_reason)
        return None
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    try:
        items = json.loads(text)["recommendations"]
    except (json.JSONDecodeError, KeyError, TypeError):
        log.warning("Claude returned malformed JSON; using rule-based recommendations.")
        return None
    recs = [Recommendation(i["title"][:120], i["body"][:900], source="claude", priority=n) for n, i in enumerate(items[:3])]
    return recs if len(recs) >= 2 else None
