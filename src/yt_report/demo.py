"""Demo mode: the real pipeline on synthetic data, fully isolated from production.

* No Google API calls: the YouTube services are replaced by deterministic
  in-memory fakes that answer the same ``reports.query`` / ``list`` calls.
* No Discord messages: embeds are only written to a local JSON preview file.
* Separate database (``data/demo/demo.db``); refuses to run if it would point
  at the production database.
* Everything is labelled as synthetic (channel name, dashboard banner).

The synthetic numbers follow the same metric relationships observed on the
real API (engaged views < views, AVD weighted by engaged views, channel-wide
average % viewed = watched time / played length), so the analytics code paths
behave as they do in production.

Every synthetic video has a publication date relative to an ``anchor`` (the
last day of the newest demo period). A video has no views before it is
published and a boost in its first week, and the uploads playlist also holds
a private, an unlisted and a scheduled upload, so publication statistics and
the views-trend decision tree run on realistic inputs.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import random
from datetime import date, timedelta
from pathlib import Path

from .analytics_client import AnalyticsClient
from .config import Config
from .discord_notifier import build_embeds, split_messages
from .models import WeeklyReport
from .periods import analytics_today, compute_periods
from .pipeline import build_report
from .storage import Storage
from .youtube_client import YouTubeClient

log = logging.getLogger(__name__)

DEMO_CHANNEL_ID = "DEMO_CHANNEL"
DEMO_CHANNEL_TITLE = "Demo Channel (synthetic data)"
DEMO_DIR = Path("data") / "demo"
DEMO_DB_NAME = "demo.db"
DEMO_PREVIEW_NAME = "discord_preview.json"


class DemoIsolationError(RuntimeError):
    pass


# ---------------------------------------------------------------- catalog ---
def _thumb(color: str, label: str) -> str:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270">'
           f'<rect width="480" height="270" fill="{color}"/>'
           f'<text x="240" y="150" font-family="sans-serif" font-size="40" fill="#fff" text-anchor="middle">{label}</text>'
           f'<text x="240" y="200" font-family="sans-serif" font-size="20" fill="#ffffffaa" text-anchor="middle">DEMO</text></svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def _catalog() -> list[dict]:
    """Synthetic videos: id, API content type, length (s), popularity, title, age (days before the anchor)."""
    vids = []
    for i in range(1, 15):
        vids.append({"id": f"demoS{i:02d}", "type": "shorts", "length": 30 + (i * 7) % 29,
                     "pop": 900 / i ** 0.9, "title": f"Demo Short #{i}", "color": "#d95926", "age": 2 + 5 * (i - 1)})
    for i in range(1, 9):
        vids.append({"id": f"demoV{i:02d}", "type": "videoOnDemand", "length": 360 + i * 95,
                     "pop": 420 / i ** 0.8, "title": f"Demo Video #{i}", "color": "#3987e5", "age": 4 + 10 * (i - 1)})
    for i in range(1, 3):
        vids.append({"id": f"demoL{i:02d}", "type": "liveStream", "length": 3600 + i * 1200,
                     "pop": 160 / i, "title": f"Demo Livestream #{i}", "color": "#199e70", "age": 5 + 25 * (i - 1)})
    for v in vids:
        v["thumb"] = _thumb(v["color"], v["title"])
    return vids


CATALOG = _catalog()
# Uploads without analytics rows: excluded from public publication counts.
EXTRA_UPLOADS = [
    {"id": "demoP01", "title": "Demo private upload", "privacy": "private", "age": 3},
    {"id": "demoU01", "title": "Demo unlisted upload", "privacy": "unlisted", "age": 9},
    {"id": "demoX01", "title": "Demo scheduled livestream", "privacy": "public", "age": 1, "upcoming": True},
]
DEFAULT_ANCHOR = date(2026, 1, 31)  # used only when a service is created without an explicit anchor


def _published(v: dict, anchor: date) -> date:
    return anchor - timedelta(days=v["age"])
RETENTION = {"shorts": (0.50, 0.72), "videoOnDemand": (0.24, 0.42), "liveStream": (0.03, 0.07)}
ENGAGED_RATIO = {"shorts": (0.40, 0.50), "videoOnDemand": (0.35, 0.45), "liveStream": (0.25, 0.32)}


def _period_stats(start: str, anchor: date = DEFAULT_ANCHOR) -> list[dict]:
    """Deterministic per-video stats for the 7-day window starting at ``start``."""
    d0 = date.fromisoformat(start)
    end = d0 + timedelta(days=6)
    rng = random.Random(d0.toordinal())
    growth = 1.0 + 0.04 * ((d0.toordinal() // 7) % 10)  # gentle multi-week trend
    rows = []
    for v in CATALOG:
        factor = rng.uniform(0.6, 1.4)  # drawn for every video so the sequence stays deterministic
        published = _published(v, anchor)
        if published > end:
            continue  # not published yet: no views
        boost = 2.0 if published >= d0 else 1.0  # first-week boost of a new upload
        views = int(v["pop"] * growth * factor * boost)
        if views <= 0:
            continue
        engaged = max(1, int(views * rng.uniform(*ENGAGED_RATIO[v["type"]])))
        avd = v["length"] * rng.uniform(*RETENTION[v["type"]])
        minutes = engaged * avd / 60 * 1.05
        rows.append({"video": v["id"], "type": v["type"], "length": v["length"], "views": views,
                     "engagedViews": engaged, "estimatedMinutesWatched": round(minutes),
                     "averageViewDuration": round(avd), "averageViewPercentage": round(avd / v["length"] * 100, 2),
                     "subscribersGained": int(views * rng.uniform(0.002, 0.01)),
                     "subscribersLost": int(views * rng.uniform(0.0, 0.004))})
    return rows


def _aggregate(rows: list[dict]) -> dict:
    views = sum(r["views"] for r in rows)
    engaged = sum(r["engagedViews"] for r in rows)
    watched = sum(r["engagedViews"] * r["averageViewDuration"] for r in rows)
    length = sum(r["engagedViews"] * r["length"] for r in rows)
    return {
        "views": views, "engagedViews": engaged,
        "estimatedMinutesWatched": sum(r["estimatedMinutesWatched"] for r in rows),
        "averageViewDuration": round(watched / engaged) if engaged else 0,
        "averageViewPercentage": round(watched / length * 100, 2) if length else 0,
        "subscribersGained": sum(r["subscribersGained"] for r in rows),
        "subscribersLost": sum(r["subscribersLost"] for r in rows),
    }


# ------------------------------------------------------ synthetic services ---
class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, num_retries=0):
        return self._fn()


def _response(dims: list[str], metrics: list[str], rows: list[tuple[list, dict]]) -> dict:
    return {"columnHeaders": [{"name": n} for n in dims + metrics],
            "rows": [list(dv) + [m[k] for k in metrics] for dv, m in rows]}


class SyntheticAnalyticsService:
    """Answers the same ``reports().query`` calls the real client makes."""

    def __init__(self, anchor: date | None = None):
        self.anchor = anchor or DEFAULT_ANCHOR

    def reports(self):
        return self

    def query(self, ids, startDate, endDate, metrics, dimensions=None, filters=None, sort=None, maxResults=None):
        mets = metrics.split(",")
        anchor = self.anchor

        def run():
            per_video = _period_stats(startDate, anchor)
            dims = dimensions.split(",") if dimensions else []
            if not dims:
                return _response([], mets, [([], _aggregate(per_video))])
            if dims == ["creatorContentType"]:
                groups = {}
                for r in per_video:
                    groups.setdefault(r["type"], []).append(r)
                return _response(dims, mets, [([t], _aggregate(g)) for t, g in groups.items()])
            if dims == ["video"]:
                rows = per_video
                if (filters or "").startswith("creatorContentType=="):
                    rows = [r for r in rows if r["type"] == filters[len("creatorContentType=="):]]
                rows = sorted(rows, key=lambda r: -r["views"])[: maxResults or 200]
                return _response(dims, mets, [([r["video"]], r) for r in rows])
            if dims == ["video", "creatorContentType"]:
                if not (filters or "").startswith("video=="):
                    raise RuntimeError("synthetic service mirrors the API: a video filter is required")
                wanted = set(filters[len("video=="):].split(","))
                return _response(dims, mets, [([r["video"], r["type"]], r) for r in per_video if r["video"] in wanted])
            if dims == ["day"]:
                start, end = date.fromisoformat(startDate), date.fromisoformat(endDate)
                out = []
                day = start
                while day <= end:
                    week_start = (day - timedelta(days=(day - start).days % 7)).isoformat()
                    week = _aggregate(_period_stats(week_start, anchor))
                    share = 1 / 7 * random.Random(day.toordinal()).uniform(0.7, 1.3)
                    out.append(([day.isoformat()], {k: int(v * share) if isinstance(v, int) else v for k, v in week.items()}))
                    day += timedelta(days=1)
                return _response(dims, mets, out)
            raise RuntimeError(f"synthetic service: unsupported dimensions {dimensions!r}")

        return _Request(run)


class SyntheticYouTubeService:
    UPLOADS_PLAYLIST = "UU_DEMO_CHANNEL"

    def __init__(self, anchor: date | None = None):
        self.anchor = anchor or DEFAULT_ANCHOR
        self._by_id = {v["id"]: v for v in CATALOG + EXTRA_UPLOADS}
        # Like the real uploads playlist: not sorted by date.
        self._uploads = sorted(self._by_id, key=lambda i: (len(i) * 7 + sum(map(ord, i))) % 11)

    def channels(self):
        return _Resource(self._channels)

    def videos(self):
        return _Resource(self._videos)

    def playlistItems(self):  # noqa: N802 - Google client naming
        return _Resource(self._playlist_items)

    def _channels(self, part=None, mine=None, **_):
        return {"items": [{"id": DEMO_CHANNEL_ID, "snippet": {"title": DEMO_CHANNEL_TITLE},
                           "contentDetails": {"relatedPlaylists": {"uploads": self.UPLOADS_PLAYLIST}},
                           "statistics": {"subscriberCount": "1500", "hiddenSubscriberCount": False}}]}

    def _playlist_items(self, part=None, playlistId=None, maxResults=50, pageToken=None, **_):
        if playlistId != self.UPLOADS_PLAYLIST:
            raise RuntimeError("synthetic service: unknown playlist")
        start = int(pageToken or 0)
        page = self._uploads[start:start + maxResults]
        resp = {"items": [{"contentDetails": {"videoId": i}} for i in page]}
        if start + maxResults < len(self._uploads):
            resp["nextPageToken"] = str(start + maxResults)
        return resp

    def _videos(self, part=None, id=None, maxResults=None, **_):
        items = []
        for i in (id or "").split(","):
            v = self._by_id.get(i)
            if v is None:
                continue
            stamp = f"{_published(v, self.anchor).isoformat()}T17:00:00Z"
            item = {"id": i, "snippet": {"title": v["title"], "publishedAt": stamp,
                                         "liveBroadcastContent": "upcoming" if v.get("upcoming") else "none"},
                    "status": {"privacyStatus": v.get("privacy", "public")}}
            if "thumb" in v:
                item["snippet"]["thumbnails"] = {"high": {"url": v["thumb"]}}
            if v.get("type") == "liveStream":
                item["liveStreamingDetails"] = {"actualStartTime": stamp}
            elif v.get("upcoming"):
                start = (_published(v, self.anchor) + timedelta(days=4)).isoformat()
                item["liveStreamingDetails"] = {"scheduledStartTime": f"{start}T18:00:00Z"}
            items.append(item)
        return {"items": items}


class _Resource:
    def __init__(self, fn):
        self._fn = fn

    def list(self, **kw):
        return _Request(lambda: self._fn(**kw))


# --------------------------------------------------------------- runner ---
def demo_paths(cfg: Config) -> tuple[Path, Path]:
    """(demo database, Discord preview file). Raises if they collide with production."""
    root = cfg.base_dir / DEMO_DIR
    db, preview = root / DEMO_DB_NAME, root / DEMO_PREVIEW_NAME
    if db.resolve() == cfg.database_path.resolve():
        raise DemoIsolationError("Demo database path equals the production DATABASE_PATH; refusing to run.")
    return db, preview


def demo_config(cfg: Config) -> Config:
    """Production config with everything external disabled."""
    db, _ = demo_paths(cfg)
    return dataclasses.replace(cfg, database_path=db, discord_webhook_url=None, ai_enabled=False,
                               anthropic_api_key=None, token_json=None)


def build_demo_reports(cfg: Config, weeks: int = 8, today: date | None = None) -> list[WeeklyReport]:
    dcfg = demo_config(cfg)
    today = today or analytics_today()
    latest, _ = compute_periods(today, dcfg.data_lag_days, dcfg.period_days, week_end=dcfg.report_week_end)
    reports = []
    for w in range(weeks - 1, -1, -1):
        end = latest.end - timedelta(days=7 * w)
        reports.append(build_report(dcfg, YouTubeClient(SyntheticYouTubeService(latest.end)),
                                    AnalyticsClient(SyntheticAnalyticsService(latest.end)), end=end, today=today))
    return reports


def run_demo(cfg: Config, weeks: int = 8, today: date | None = None) -> tuple[Path, Path, list[WeeklyReport]]:
    """Build synthetic weekly reports, store them in the demo DB, write a Discord preview."""
    db_path, preview_path = demo_paths(cfg)
    reports = build_demo_reports(cfg, weeks, today)
    with Storage(db_path) as db:
        for r in reports:
            db.save_report(r)
    latest = reports[-1]
    messages = split_messages(build_embeds(latest))
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_text(json.dumps({
        "_note": "DEMO preview of the Discord payload built from synthetic data. Nothing was sent.",
        "messages": [{"embeds": m} for m in messages],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Demo: %d synthetic reports stored in %s; Discord preview written to %s", len(reports), db_path, preview_path)
    return db_path, preview_path, reports
