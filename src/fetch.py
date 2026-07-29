"""
fetch.py — Fetch all configured feeds and return a normalized story list.

Guarantees:
- A failing source NEVER crashes the run: every source is wrapped in
  try/except; failures are logged and skipped.
- Reddit requests are throttled (REDDIT_DELAY_S) and use the optional
  authenticated feed token from the environment (never logged).
- Feeds are NOT assumed to be sorted; each source is sorted newest-first
  here and capped at MAX_ENTRIES_PER_SOURCE.
- HN entries are keyword-filtered for AI relevance.

Each story is a dict:
    {
      "source_id": int, "source_name": str, "priority": int,
      "title": str, "link": str, "summary": str (plain text, <=500 chars),
      "published": datetime | None (UTC),
    }
"""

import logging
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode   # STEP [13]

import feedparser
import requests

import config

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# STEP [11] first <img src="..."> in HTML — used to extract article images
_IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


def _clean_text(html_or_text, limit=500):
    """Strip HTML tags and collapse whitespace; truncate to `limit` chars."""
    if not html_or_text:
        return ""
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", html_or_text)).strip()
    return text[:limit]


def _extract_image(entry):                                            # STEP [11]
    """Best-effort image URL from a feed entry, or None.

    # STEP [11] Checks in priority order: media:content → media:thumbnail →
    # STEP [11] enclosure(image/*) → first <img src> in content/summary.
    # STEP [11] Never raises — a malformed entry yields None, not a crash.
    # STEP [11] 8a only CAPTURES the URL; download/validation is 8b's job."""
    try:
        # 1. media:content — usually the highest-quality article image.
        for media in entry.get("media_content", []) or []:
            if not isinstance(media, dict):
                continue
            url = media.get("url")
            if not url:
                continue
            mtype = (media.get("type") or "").lower()
            if not mtype or mtype.startswith("image/"):
                return url

        # 2. media:thumbnail — lower-res but commonly present (Reddit, YouTube).
        for thumb in entry.get("media_thumbnail", []) or []:
            if not isinstance(thumb, dict):
                continue
            url = thumb.get("url")
            if url:
                return url

        # 3. enclosure with an image MIME type.
        for link in entry.get("links", []) or []:
            if not isinstance(link, dict):
                continue
            if link.get("rel") == "enclosure" and \
               (link.get("type") or "").lower().startswith("image/"):
                url = link.get("href") or link.get("url")
                if url:
                    return url

        # 4. First <img src> in content or summary HTML.
        for field in ("content", "summary", "description"):
            html = ""
            if field == "content":
                contents = entry.get("content", [])
                if contents and isinstance(contents[0], dict):
                    html = contents[0].get("value", "")
            else:
                html = entry.get(field, "") or ""
            if html:
                match = _IMG_SRC_RE.search(html)
                if match:
                    return match.group(1)
    except Exception:  # noqa: BLE001 — never let image extraction crash a feed
        pass
    return None


def _normalize_image_url(url):                                      # STEP [13]
    """Strip webp/avif-forcing query params so CDNs serve real JPEG/PNG bytes.

    # STEP [13] LinkedIn (via post_linkedin._detect_content_type) accepts ONLY
    # STEP [13] JPEG/PNG magic bytes. Reddit/CDN preview URLs commonly append
    # STEP [13] &auto=webp / &format=webp, which make them serve WebP — the sniff
    # STEP [13] rejects it and the post silently falls back to text-only. Removing
    # STEP [13] those params (the path already ends in .png/.jpg) yields real bytes.
    # STEP [13] Best-effort: any parse failure returns the original url untouched."""
    if not url or not isinstance(url, str) or "?" not in url:
        return url
    try:
        parts = urlsplit(url)
        kept = [(k, v) for k, v in parse_qsl(parts.query)
                if not (k == "auto" and v == "webp")
                and not (k == "format" and v in ("webp", "avif", "pjpg"))]
        return urlunsplit(parts._replace(query=urlencode(kept)))
    except Exception:  # noqa: BLE001 — never let URL cleaning crash a feed
        return url


def _entry_datetime(entry):
    """Best-effort UTC datetime for a feed entry, or None."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
    except (OverflowError, ValueError, OSError):
        return None


def _hn_is_ai(title):
    """True if an HN title matches the AI keyword list (whole-word for short terms)."""
    lower = title.lower()
    if lower.startswith(config.HN_EXCLUDE_PREFIXES):   # FIX [5] drop Launch HN / Ask HN
        return False                                    # FIX [5]
    for kw in config.HN_AI_KEYWORDS:
        if kw in config.HN_WHOLE_WORD_ONLY:
            if re.search(rf"\b{re.escape(kw)}\b", lower):
                return True
        elif kw in lower:
            return True
    return False


def _prepare_url(url):
    """Apply Reddit throttle + optional authenticated feed params."""
    if "reddit.com" in url:
        time.sleep(config.REDDIT_DELAY_S)
        params = os.environ.get(config.REDDIT_PARAMS_ENV, "")
        if params:
            url = f"{url}?{params.lstrip('?')}"
    return url


def fetch_source(source_id, name, url, priority):
    """Fetch one feed. Returns a list of story dicts; [] on any failure."""
    try:
        resp = requests.get(
            _prepare_url(url),
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.TIMEOUT,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            log.warning("[%s] %s: HTTP %s — skipping", source_id, name, resp.status_code)
            return []
        feed = feedparser.parse(resp.content)
    except requests.RequestException as exc:
        log.warning("[%s] %s: request failed (%s: %s) — skipping",
                    source_id, name, type(exc).__name__, exc)
        return []
    except Exception as exc:  # feedparser very rarely raises; never crash
        log.warning("[%s] %s: parse failed (%s) — skipping", source_id, name, exc)
        return []

    stories = []
    for entry in feed.entries:
        title = _clean_text(entry.get("title", ""), limit=200)
        link = entry.get("link", "")
        if not title or not link:
            continue
        if name == "HN frontpage" and not _hn_is_ai(title):
            continue
        stories.append({
            "source_id": source_id,
            "source_name": name,
            "priority": priority,
            "title": title,
            "link": link,
            "summary": _clean_text(entry.get("summary", entry.get("description", ""))),
            "published": _entry_datetime(entry),
            "image_url": _normalize_image_url(_extract_image(entry)),  # STEP [11] # STEP [13]
        })

    # Feeds are not reliably sorted (e.g. VentureBeat, GitHub-hosted feeds):
    # sort newest-first, undated entries last, then cap.
    epoch = datetime.fromtimestamp(0, tz=timezone.utc)
    stories.sort(key=lambda s: s["published"] or epoch, reverse=True)
    stories = stories[:config.MAX_ENTRIES_PER_SOURCE]

    log.info("[%s] %s: %d stories kept", source_id, name, len(stories))
    return stories


def fetch_all():
    """Fetch every configured source. Never raises; returns combined story list."""
    all_stories = []
    for source_id, name, url, priority in config.SOURCES:
        all_stories.extend(fetch_source(source_id, name, url, priority))
    log.info("fetch_all: %d stories from %d sources", len(all_stories), len(config.SOURCES))
    return all_stories


if __name__ == "__main__":
    # Manual test: python3 -m fetch  (run from src/) or python3 src/fetch.py
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    stories = fetch_all()
    print(f"\nTotal stories: {len(stories)}")
    for s in stories[:10]:
        when = s["published"].strftime("%Y-%m-%d %H:%M") if s["published"] else "undated"
        print(f"  [{s['source_name']}] ({when}) {s['title'][:70]}")