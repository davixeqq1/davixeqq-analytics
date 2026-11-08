"""YouTube Data API v3: channel info, video metadata and the uploads playlist."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .retry import execute_with_retry

log = logging.getLogger(__name__)

MAX_UPLOAD_PAGES = 40   # 50 items per page -> at most 2,000 uploads scanned (1 quota unit per page)
VIDEO_PARTS = "snippet,status,liveStreamingDetails"  # same quota cost as "snippet" alone


@dataclass
class ChannelInfo:
    channel_id: str
    title: str
    subscriber_count: int | None
    uploads_playlist_id: str | None = None


@dataclass
class VideoMeta:
    video_id: str
    title: str
    thumbnail_url: str | None
    published_at: str | None
    privacy_status: str | None = None          # public | unlisted | private (None = not returned)
    live_broadcast_content: str | None = None  # none | live | upcoming
    actual_start_time: str | None = None       # livestreams / premieres that started
    scheduled_start_time: str | None = None


@dataclass
class UploadScan:
    """Result of paging through the uploads playlist.

    ``complete`` is True only when the last page had no ``nextPageToken``.
    Reaching the page limit or a repeated page token never counts as a full scan.
    """

    video_ids: list[str] = field(default_factory=list)
    # Playlist-item date (snippet.publishedAt, else contentDetails.videoPublishedAt). Used only for
    # uploads without videos.list metadata (deleted), to tell whether they could matter for a period.
    item_dates: dict[str, str | None] = field(default_factory=dict)
    pages: int = 0
    complete: bool = False
    reason: str | None = None   # page_limit | repeated_page_token
    duplicates: int = 0


class YouTubeClient:
    def __init__(self, service):
        self._svc = service

    def get_channel(self) -> ChannelInfo:
        resp = execute_with_retry(self._svc.channels().list(part="snippet,statistics,contentDetails", mine=True))
        items = resp.get("items") or []
        if not items:
            raise RuntimeError("The authorized Google account has no YouTube channel.")
        item = items[0]
        stats = item.get("statistics", {})
        hidden = stats.get("hiddenSubscriberCount", False)
        subs = stats.get("subscriberCount")
        uploads = ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
        return ChannelInfo(
            channel_id=item["id"],
            title=item.get("snippet", {}).get("title", ""),
            subscriber_count=None if hidden or subs is None else int(subs),
            uploads_playlist_id=uploads or None,
        )

    def list_uploads(self, playlist_id: str, max_pages: int = MAX_UPLOAD_PAGES) -> UploadScan:
        """All video IDs of the uploads playlist (the playlist is NOT sorted by date)."""
        scan = UploadScan()
        seen: set[str] = set()
        seen_tokens: set[str] = set()
        token: str | None = None
        while True:
            if scan.pages >= max_pages:
                scan.reason = "page_limit"
                log.warning("Uploads scan stopped at the %d-page limit; publication counts are incomplete.", max_pages)
                break
            params = {"part": "snippet,contentDetails", "playlistId": playlist_id, "maxResults": 50}
            if token:
                params["pageToken"] = token
            resp = execute_with_retry(self._svc.playlistItems().list(**params))
            scan.pages += 1
            for item in resp.get("items") or []:
                vid = (item.get("contentDetails") or {}).get("videoId")
                if not vid:
                    continue
                if vid in seen:
                    scan.duplicates += 1
                    continue
                seen.add(vid)
                scan.video_ids.append(vid)
                scan.item_dates[vid] = ((item.get("snippet") or {}).get("publishedAt")
                                        or (item.get("contentDetails") or {}).get("videoPublishedAt"))
            nxt = resp.get("nextPageToken")
            if not nxt:
                scan.complete = True
                break
            if nxt == token or nxt in seen_tokens:
                scan.reason = "repeated_page_token"
                log.warning("Uploads playlist returned a repeated page token; scan stopped as incomplete.")
                break
            seen_tokens.add(nxt)
            token = nxt
        if scan.duplicates:
            log.info("Uploads playlist contained %d duplicate entr(y/ies); counted once.", scan.duplicates)
        return scan

    def get_videos(self, video_ids: list[str]) -> dict[str, VideoMeta]:
        result: dict[str, VideoMeta] = {}
        unique = list(dict.fromkeys(v for v in video_ids if v))
        for i in range(0, len(unique), 50):  # API limit: 50 ids per request
            chunk = unique[i : i + 50]
            resp = execute_with_retry(self._svc.videos().list(part=VIDEO_PARTS, id=",".join(chunk), maxResults=50))
            for item in resp.get("items", []):
                snip = item.get("snippet", {})
                thumbs = snip.get("thumbnails", {})
                thumb = next((thumbs[k]["url"] for k in ("high", "medium", "default") if k in thumbs), None)
                live = item.get("liveStreamingDetails") or {}
                result[item["id"]] = VideoMeta(
                    video_id=item["id"],
                    title=snip.get("title", ""),
                    thumbnail_url=thumb,
                    published_at=snip.get("publishedAt"),
                    privacy_status=(item.get("status") or {}).get("privacyStatus"),
                    live_broadcast_content=snip.get("liveBroadcastContent"),
                    actual_start_time=live.get("actualStartTime"),
                    scheduled_start_time=live.get("scheduledStartTime"),
                )
        missing = set(unique) - set(result)
        if missing:
            log.info("%d video(s) have no metadata (deleted or not accessible).", len(missing))
        return result
