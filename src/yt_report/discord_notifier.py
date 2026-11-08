"""Discord webhook reports (embeds) with limit-aware splitting."""

from __future__ import annotations

import logging
import time

import requests

from .analysis import NOTE_DERIVED_WATCH_TIME, Comparison, compare_format, compare_totals, completeness_issues
from .formatting import fmt_change, fmt_duration, fmt_hours, fmt_int, fmt_pct, fmt_signed_int, fmt_value
from .logging_utils import redact
from .models import FORMAT_LABELS, LIVE, LONG_FORM, REPORTED_FORMATS, SHORTS, SOURCE_DERIVED, SOURCE_UNAVAILABLE, WeeklyReport

log = logging.getLogger(__name__)

# Discord limits: https://discord.com/developers/docs/resources/message#embed-object-embed-limits
MAX_EMBEDS_PER_MESSAGE = 10
MAX_TOTAL_CHARS = 6000
LIMITS = {"title": 256, "description": 4096, "field_name": 256, "field_value": 1024, "footer": 2048, "author": 256, "fields": 25}

COLOR_BRAND = 0xFF0033
COLOR_GOOD = 0x2ECC71
COLOR_BAD = 0xE74C3C
COLOR_NEUTRAL = 0x5865F2
COLOR_WARN = 0xF39C12
COLOR_INFO = 0x3498DB

USERNAME = "YouTube Analytics Bot"
FORMAT_ICONS = {SHORTS: "📱 ", LONG_FORM: "🎬 ", LIVE: "🔴 "}
MIX_NOTE = "Channel-wide averages depend on the format mix — compare per-format values."
PUB_LABELS = {SHORTS: "Shorts", LONG_FORM: "Long-form", LIVE: "Live"}


class DiscordError(RuntimeError):
    pass


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def trend(c: Comparison) -> str:
    change = c.change_pct
    if c.current is None:
        return "data unavailable"
    if c.previous is None:
        return "no previous data"
    if change is None:
        return "▲ new" if (c.current or 0) > 0 else "• no change"
    if abs(change) < 0.05:
        return "• 0.0%"
    arrow = "▲" if change > 0 else "▼"
    dot = "🟢" if c.is_improvement else "🔴"
    return f"{dot} {arrow} {fmt_change(change)}"


def comparison_field(c: Comparison) -> dict:
    if c.key == "net_subs":
        value = f"**{fmt_signed_int(c.current)}**"
        prev = fmt_signed_int(c.previous)
    else:
        value = f"**{fmt_value(c.current, c.kind)}**"
        prev = fmt_value(c.previous, c.kind)
    return {"name": c.label, "value": f"{value}\n{trend(c)}\n`prev {prev}`", "inline": True}


def value_field(c: Comparison, days: str) -> dict:
    """Partial report: the current value only — no trend arrow, no % change, no previous value."""
    value = fmt_signed_int(c.current) if c.key == "net_subs" else fmt_value(c.current, c.kind)
    return {"name": c.label, "value": f"**{value}**\n_{days}_", "inline": True}


def _days_label(report: WeeklyReport) -> str:
    return f"{report.days_with_data}/{report.period.days} days"


def _partial_range(report: WeeklyReport) -> str:
    s, t = report.period.start, report.data_through
    return f"{s:%b} {s.day} – {t:%b} {t.day}, {t.year}"


def _overview(report: WeeklyReport) -> dict:
    comps = compare_totals(report.current, report.previous)
    views = comps[0]
    title = f"📊 {report.channel_title or 'YouTube'} — Weekly Report"
    if report.partial:
        title = f"📊 {report.channel_title or 'YouTube'} — PARTIAL Weekly Report ({_days_label(report)})"
        color = COLOR_WARN
        desc = (
            f"📅 **Data available: {_partial_range(report)} ({report.days_with_data} of {report.period.days} days)**\n"
            f"Requested week: {report.period.label()} — the remaining days are not yet processed by YouTube.\n"
            "⚠ **Partial report:** informational values only — no comparison with the previous week and no "
            "automatic recommendations. The complete report follows once all days are available."
        )
        fields = [value_field(c, _days_label(report)) for c in comps]
    else:
        color = COLOR_GOOD if views.is_improvement else COLOR_BAD if views.is_improvement is False else COLOR_NEUTRAL
        desc = (
            f"📅 **{report.period.label()}**\n"
            f"vs. previous {report.previous_period.days} days ({report.previous_period.label()})"
        )
        fields = [comparison_field(c) for c in comps]
    if report.subscriber_count is not None:
        desc += f"\n👥 Current subscribers: **{fmt_int(report.subscriber_count)}**"
    if report.previous.views is None and not report.partial:
        desc += "\n⚠ **No valid previous comparison:** YouTube returned no data for the previous period."
    if report.period_overlaps:
        earlier = ", ".join(p.label() for p in report.period_overlaps)
        desc += f"\n⚠ **Reporting week realigned:** this report overlaps the earlier report for {earlier}."
        if not report.partial:
            desc += (f" Changes below compare {report.period.label()} with {report.previous_period.label()} "
                     "(YouTube Analytics data), not with that earlier report.")
    return {
        "title": title,
        "url": f"https://studio.youtube.com/channel/{report.channel_id}/analytics",
        "description": desc,
        "color": color,
        "fields": fields + [published_field(report), completeness_field(report)],
        "footer": {"text": f"ℹ {MIX_NOTE}"},
    }


def published_field(report: WeeklyReport) -> dict:
    """Public uploads per format this period vs. the previous one (Pacific dates)."""
    pc, pp = report.publications_current, report.publications_previous
    if pc is None or not pc.available:
        return {"name": "📅 Published", "value": "_Publication data unavailable._", "inline": False}
    prev_ok = pp is not None and pp.available

    def prev(n_prev) -> str:
        return f" (prev {n_prev})" if prev_ok else " (prev n/a)"

    parts = [f"{PUB_LABELS[ct]} **{pc.count(ct)}**{prev(pp.count(ct) if prev_ok else None)}" for ct in REPORTED_FORMATS]
    lines = [" · ".join(parts)]
    other = sum(n for ct, n in pc.published.items() if ct not in REPORTED_FORMATS)
    extra = []
    if pc.unclassified or (prev_ok and pp.unclassified):
        extra.append(f"Unclassified **{pc.unclassified}**{prev(pp.unclassified if prev_ok else None)}")
    if other:
        extra.append(f"Other types **{other}**")
    if extra:
        lines.append(" · ".join(extra))
    excluded = []
    if pc.private:
        excluded.append(f"{pc.private} private")
    if pc.unlisted:
        excluded.append(f"{pc.unlisted} unlisted")
    if pc.upcoming:
        excluded.append(f"{pc.upcoming} scheduled live")
    tail = "Public uploads only, Pacific dates"
    if excluded:
        tail += "; not counted: " + ", ".join(excluded)
    if not (pc.complete and prev_ok and pp.complete):
        tail = "⚠ incomplete scan — counts may be low. " + tail
    lines.append(f"_{tail}_")
    return {"name": "📅 Published", "value": "\n".join(lines), "inline": False}


def completeness_field(report: WeeklyReport) -> dict:
    issues = completeness_issues(report)
    value = "✅ Complete" if not issues else "\n".join(f"⚠ {i}" for i in issues)
    return {"name": "Data completeness", "value": value, "inline": False}


def _formats(report: WeeklyReport) -> dict:
    fields = []
    derived = mixed = False
    for ct in REPORTED_FORMATS:
        cur = report.formats_current.get(ct)
        label = FORMAT_ICONS.get(ct, "") + FORMAT_LABELS[ct]
        if cur is None or cur.source == SOURCE_UNAVAILABLE:
            fields.append({"name": label, "value": "_Data unavailable from the API for this period._", "inline": True})
            continue
        derived |= cur.source == SOURCE_DERIVED
        prev = report.formats_previous.get(ct)
        # Week-over-week trends only when both periods were measured the same way and the week is complete.
        same_method = prev is not None and prev.source == cur.source and not report.partial
        mixed |= prev is not None and prev.source not in (cur.source, SOURCE_UNAVAILABLE)
        lines = []
        for c in compare_format(cur, prev if same_method else None):
            lines.append(f"{c.label}: **{fmt_value(c.current, c.kind)}** {trend(c) if c.previous is not None else ''}".rstrip())
        fields.append({"name": label, "value": "\n".join(lines), "inline": True})
    footer = "Source: YouTube Analytics creatorContentType."
    if report.partial:
        footer = (f"⚠ Partial period ({_days_label(report)}): values only, no week-over-week trends. " + footer)
    if derived:
        footer = ("⚠ Derived from per-video rows, not returned directly by the API "
                  "(AVD weighted by engaged views; avg % viewed unavailable). " + NOTE_DERIVED_WATCH_TIME)
        if any(not f.complete for f in report.formats_current.values()):
            cov = report.coverage_current
            if cov is not None and cov.views_reference is not None:
                footer += (f" Per-video rows cover {fmt_int(cov.views_rows)} of {fmt_int(cov.views_reference)} views"
                           f"{' (API cap 200 rows)' if cov.capped else ''} — totals may be understated.")
            else:
                footer += " Per-video rows may not cover all views — totals may be understated."
    if mixed:
        footer += " Previous-period figures were obtained by a different method, so format trends are omitted."
    footer += " Livestreams are counted separately, not as long-form."
    footer += " Shorts retention can exceed 100% (loops) and is not comparable across formats."
    return {"title": "📱 Shorts · 🎬 Long-form · 🔴 Livestream", "color": COLOR_INFO, "fields": fields, "footer": {"text": footer}}


def _video_embed(rank: int, v) -> dict:
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"#{rank}")
    fmt = FORMAT_LABELS.get(v.content_type or "", "Unknown format")
    embed = {
        "title": f"{medal} {v.title or v.video_id}",
        "url": v.url,
        "color": COLOR_BRAND,
        "description": f"{fmt} · [Watch on YouTube]({v.url})",
        "fields": [
            {"name": "Views", "value": fmt_int(v.views), "inline": True},
            {"name": "Watch time", "value": fmt_hours(v.watch_hours), "inline": True},
            {"name": "Avg duration", "value": fmt_duration(v.avg_view_duration_s), "inline": True},
            {"name": "Avg % viewed", "value": fmt_pct(v.avg_view_pct), "inline": True},
        ],
    }
    if _is_https(v.thumbnail_url):
        embed["thumbnail"] = {"url": v.thumbnail_url}
    return embed


def _is_https(url: str | None) -> bool:
    return bool(url) and url.startswith("https://")


def _weakest(report: WeeklyReport) -> dict:
    if not report.weakest:
        return {
            "title": "⚠ Weakest video",
            "color": COLOR_NEUTRAL,
            "description": report.weakest_note or "Not enough data for a fair comparison.",
        }
    w = report.weakest
    v = w.video
    fmt = FORMAT_LABELS.get(v.content_type or "", "")
    embed = {
        "title": f"⚠ Weakest retention: {v.title or v.video_id}",
        "url": v.url,
        "color": COLOR_WARN,
        "description": (
            f"{fmt} · viewers watched **{fmt_pct(v.avg_view_pct)}** on average vs. a **{fmt_pct(w.format_median_pct)}** "
            f"median across {w.peers} {fmt.lower()} videos (**{(w.ratio - 1) * 100:+.0f}%** relative)."
        ),
        "fields": [
            {"name": "Views", "value": fmt_int(v.views), "inline": True},
            {"name": "Avg duration", "value": fmt_duration(v.avg_view_duration_s), "inline": True},
            {"name": "Watch time", "value": fmt_hours(v.watch_hours), "inline": True},
        ],
    }
    if _is_https(v.thumbnail_url):
        embed["thumbnail"] = {"url": v.thumbnail_url}
    return embed


def _recommendations(report: WeeklyReport) -> dict:
    src = "Claude API (from verified metrics)" if any(r.source == "claude" for r in report.recommendations) else "rule-based engine"
    body = "\n\n".join(f"**{i}. {r.title}**\n{r.body}" for i, r in enumerate(report.recommendations, 1))
    embed = {
        "title": "💡 Recommendations",
        "color": COLOR_GOOD,
        "description": body or "_No recommendations this week._",
        "footer": {"text": f"Generated by {src} · {report.generated_at:%Y-%m-%d %H:%M %Z}"},
    }
    if report.notes:
        embed["fields"] = [{"name": "Data notes", "value": fit_notes(report.notes), "inline": False}]
    return embed


def _more_notes(n: int) -> str:
    return f"… +{n} more note{'s' if n != 1 else ''} on the dashboard"


def fit_notes(notes: list[str], limit: int = LIMITS["field_value"]) -> str:
    """Whole notes only, in order, within ``limit`` characters.

    Notes that do not fit are never cut mid-sentence; they are counted in a final
    "… +N more notes on the dashboard" line instead (the dashboard and the database
    always keep every note). The result never exceeds ``limit``.
    """
    lines = [f"• {n}" for n in notes]
    full = "\n".join(lines)
    if len(full) <= limit:
        return full
    shown: list[str] = []
    for i, line in enumerate(lines):
        candidate = "\n".join(shown + [line, _more_notes(len(lines) - i - 1)])
        if len(candidate) > limit:
            break
        shown.append(line)
    return "\n".join(shown + [_more_notes(len(lines) - len(shown))])


def build_embeds(report: WeeklyReport) -> list[dict]:
    embeds = [_overview(report), _formats(report)]
    embeds += [_video_embed(i, v) for i, v in enumerate(report.top_videos, 1)]
    embeds.append(_weakest(report))
    embeds.append(_recommendations(report))
    return [sanitize_embed(e) for e in embeds]


def sanitize_embed(embed: dict) -> dict:
    e = dict(embed)
    if "title" in e:
        e["title"] = _cut(e["title"], LIMITS["title"])
    if "description" in e:
        e["description"] = _cut(e["description"], LIMITS["description"])
    if "footer" in e:
        e["footer"] = {**e["footer"], "text": _cut(e["footer"]["text"], LIMITS["footer"])}
    if "fields" in e:
        e["fields"] = [
            {**f, "name": _cut(f["name"], LIMITS["field_name"]) or "​", "value": _cut(f["value"], LIMITS["field_value"]) or "​"}
            for f in e["fields"][: LIMITS["fields"]]
        ]
    return e


def embed_chars(embed: dict) -> int:
    n = len(embed.get("title", "")) + len(embed.get("description", ""))
    n += len(embed.get("footer", {}).get("text", "")) + len(embed.get("author", {}).get("name", ""))
    n += sum(len(f["name"]) + len(f["value"]) for f in embed.get("fields", []))
    return n


def split_messages(embeds: list[dict]) -> list[list[dict]]:
    """Group embeds into messages respecting the 10-embed and 6000-char limits."""
    messages: list[list[dict]] = []
    current: list[dict] = []
    chars = 0
    for e in embeds:
        size = embed_chars(e)
        if current and (len(current) >= MAX_EMBEDS_PER_MESSAGE or chars + size > MAX_TOTAL_CHARS):
            messages.append(current)
            current, chars = [], 0
        current.append(e)
        chars += size
    if current:
        messages.append(current)
    return messages


def post(webhook_url: str, payload: dict, attempts: int = 4) -> None:
    payload = {"username": USERNAME, "allowed_mentions": {"parse": []}, **payload}
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(webhook_url, json=payload, params={"wait": "true"}, timeout=20)
        except requests.RequestException as exc:
            if attempt == attempts:
                raise DiscordError(f"Discord unreachable: {type(exc).__name__}") from None
            time.sleep(2 * attempt)
            continue
        if resp.status_code == 429:
            try:
                retry_after = float(resp.json().get("retry_after", 2))
            except (ValueError, AttributeError, TypeError):
                retry_after = 2.0
            log.warning("Discord rate limit; waiting %.1fs", retry_after)
            time.sleep(min(retry_after, 30))
            continue
        if resp.status_code >= 500 and attempt < attempts:
            time.sleep(2 * attempt)
            continue
        if not resp.ok:
            # Discord error bodies never contain the webhook token, but redact anyway.
            raise DiscordError(f"Discord returned HTTP {resp.status_code}: {redact(resp.text[:300])}")
        return
    raise DiscordError("Discord request failed after retries.")


def send_report(webhook_url: str, report: WeeklyReport) -> int:
    messages = split_messages(build_embeds(report))
    for i, embeds in enumerate(messages):
        post(webhook_url, {"embeds": embeds})
        if i < len(messages) - 1:
            time.sleep(1)  # be gentle with the webhook rate limit
    log.info("Discord report sent in %d message(s).", len(messages))
    return len(messages)


def send_error(webhook_url: str, title: str, detail: str) -> None:
    embed = sanitize_embed({
        "title": f"❌ {title}",
        "color": COLOR_BAD,
        "description": redact(detail),
        "footer": {"text": "yt-report · check logs/yt_report.log on the host for details"},
    })
    try:
        post(webhook_url, {"embeds": [embed]}, attempts=2)
    except DiscordError as exc:
        log.error("Could not deliver error notification: %s", exc)


def send_test(webhook_url: str) -> None:
    post(webhook_url, {"embeds": [{
        "title": "✅ yt-report webhook test",
        "description": "The Discord webhook is configured correctly. No analytics data was used.",
        "color": COLOR_GOOD,
    }]})
