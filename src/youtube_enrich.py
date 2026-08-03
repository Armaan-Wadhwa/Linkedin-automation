"""
youtube_enrich.py — Fetch YouTube transcripts to enrich a story's summary.
                                                    # STEP [16] Phase 3, Task 11

Vaibhav Sisinty's YouTube RSS (source_id 12) ships clickbait titles like
"The results shocked me!" with empty/thin summaries, so the generated bullet
is weak or risks invention. Pulling the transcript lets generate.py ground
the bullet in what the video actually says.

GUARANTEES (mirror the rest of the pipeline):
- enrich(story) NEVER raises. Any failure (missing captions, rate limit,
  library absent, network error, malformed response) -> the story is returned
  UNCHANGED with its title-only form intact. Transcripts only ENRICH.
- Only source_id 12 stories are touched; everything else is returned as-is
  with zero network calls (the source gate is also enforced in fetch.py).
- Logged at INFO for missing captions: that's the normal case (many videos
  have no captions), not warning-worthy.

The youtube-transcript-api library is imported lazily inside _fetch so this
module imports cleanly even when the library isn't installed (the final
gate lives in fetch.py via try/except ImportError).
"""

import logging
import re

import config

log = logging.getLogger(__name__)

# STEP [16] YouTube video IDs are exactly 11 chars: [A-Za-z0-9_-]
# STEP [16] Form 1: youtube.com/watch?v=ID  (what the RSS feed emits for full videos)
# STEP [16] Form 2: youtu.be/ID             (short-link, robustness)
# STEP [16] Form 3: youtube.com/shorts/ID   (Shorts — ~half of Vaibhav's feed)
_WATCH_V_RE = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")
_SHORT_RE = re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})")
_SHORTS_RE = re.compile(r"youtube\.com/shorts/([A-Za-z0-9_-]{11})")  # STEP [16]

# STEP [16] whitespace collapse for joined transcript snippets
_WS_RE = re.compile(r"\s+")


def _extract_video_id(link):
    """Return the 11-char YouTube video id from a watch?v= / youtu.be/ / shorts/ URL.

    # STEP [16] None for non-YouTube links, malformed URLs, or missing v= param.
    # STEP [16] Never raises."""
    if not link or not isinstance(link, str):
        return None
    m = _WATCH_V_RE.search(link)
    if m:
        return m.group(1)
    m = _SHORTS_RE.search(link)
    if m:
        return m.group(1)
    m = _SHORT_RE.search(link)
    if m:
        return m.group(1)
    return None


def _snippet_text(snippet):
    """Defensively read the .text field from a transcript snippet.

    # STEP [16] youtube-transcript-api 0.6.x returns plain dicts {'text': ...};
    # STEP [16] 1.x returns FetchedTranscriptSnippet objects with a .text attr.
    # STEP [16] Handle both so a library version bump can't break enrichment."""
    if snippet is None:
        return ""
    if isinstance(snippet, dict):
        return snippet.get("text") or ""
    return getattr(snippet, "text", "") or ""


def _fetch_transcript_text(video_id):
    """Fetch the best-available transcript for video_id, joined + trimmed.

    # STEP [16] Returns the cleaned transcript text (<= YT_TRANSCRIPT_MAX_CHARS),
    # STEP [16] or None if no transcript is available. Never raises.
    #
    # STEP [16] Preference order (each falls through to the next on
    # STEP [16] NoTranscriptFound): manually-created English > auto-generated
    # STEP [16] English > first available transcript of any kind/language."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None  # fetch.py gate normally catches this; defensive double-cover

    # STEP [16] youtube-transcript-api 1.x renamed list_transcripts -> list and
    # STEP [16] made it an INSTANCE method; 0.6.x exposes list_transcripts as a
    # STEP [16] CLASSMETHOD. Probe both shapes so the code works across versions
    # STEP [16] without a hard pin (the dep is loosely pinned >=0.6.2).
    try:
        if hasattr(YouTubeTranscriptApi, "list"):       # STEP [16] 1.x instance method
            transcript_list = YouTubeTranscriptApi().list(video_id)
        else:                                            # STEP [16] 0.6.x classmethod
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
    except Exception as exc:  # noqa: BLE001 — any metadata failure = no transcript
        log.info("youtube_enrich: %s: transcript list failed (%s: %s)",
                 video_id, type(exc).__name__, exc)
        return None

    transcript = None
    # STEP [16] Try manual English, then auto English, then any available.
    # STEP [16] list_transcripts exposes find_manually_created_transcript and
    # STEP [16] find_generated_transcript (each raises NoTranscriptFound on miss).
    try:
        transcript = transcript_list.find_manually_created_transcript(["en"])
    except Exception:  # noqa: BLE001 — NoTranscriptFound or API shape change
        try:
            transcript = transcript_list.find_generated_transcript(["en"])
        except Exception:  # noqa: BLE001
            for candidate in transcript_list:
                transcript = candidate
                break

    if transcript is None:
        return None

    try:
        snippets = transcript.fetch()
    except Exception as exc:  # noqa: BLE001 — fetch can fail (rate limit, etc.)
        log.info("youtube_enrich: %s: transcript fetch failed (%s: %s)",
                 video_id, type(exc).__name__, exc)
        return None

    text = _WS_RE.sub(" ", " ".join(_snippet_text(s) for s in snippets)).strip()
    if not text:
        return None

    # STEP [16] Trim to YT_TRANSCRIPT_MAX_CHARS on a word boundary; append
    # STEP [16] ellipsis only when we actually cut something.
    cap = config.YT_TRANSCRIPT_MAX_CHARS
    if len(text) <= cap:
        return text
    cut = text[:cap].rstrip()
    # back up to the last space so we don't split a word mid-token
    if " " in cut and cut != text[:cap]:
        cut = cut.rsplit(" ", 1)[0].rstrip()
    return cut + "\u2026" if cut else text[:cap]


def enrich(story):
    """Enrich a single story's summary with its YouTube transcript (in place).

    # STEP [16] Returns the SAME story dict (mutated). Never raises.
    # STEP [16] - source_id != 12 -> returned untouched, no API call.
    # STEP [16] - no video id in link -> returned untouched.
    # STEP [16] - transcript available -> summary replaced (thin case) or
    # STEP [16]   prefixed (when the original RSS summary carried real text).
    # STEP [16] - transcript missing/unavailable -> returned untouched, INFO log.
    """
    if not story or story.get("source_id") != config.YOUTUBE_SOURCE_ID:
        return story

    video_id = _extract_video_id(story.get("link", ""))
    if not video_id:
        return story

    try:
        text = _fetch_transcript_text(video_id)
    except Exception as exc:  # noqa: BLE001 — hard guarantee: enrich never raises
        log.info("youtube_enrich: %s: transcript fetch raised (%s: %s) — keeping title-only",
                 video_id, type(exc).__name__, exc)
        return story

    if not text:
        log.info("youtube_enrich: %s: no transcript — keeping title-only", video_id)
        return story

    original = (story.get("summary") or "").strip()
    # STEP [16] Replace when thin (YouTube RSS summaries are HTML boilerplate);
    # STEP [16] only prefix when the original carried real content. Title is
    # STEP [16] already passed to the LLM separately, so the summary slot is
    # STEP [16] the right place for the substantive transcript text.
    if original:
        story["summary"] = f"{original}\n\u2014 transcript \u2014\n{text}"
    else:
        story["summary"] = text
    log.info("youtube_enrich: %s: enriched summary (%d chars)", video_id, len(text))
    return story


if __name__ == "__main__":
    # Manual smoke test:  python src/youtube_enrich.py
    # Pass a YouTube watch URL as argv[1] to probe one video's transcript.
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) > 1:
        probe = enrich({
            "source_id": config.YOUTUBE_SOURCE_ID,
            "source_name": "Vaibhav Sisinty (YouTube)",
            "priority": 4,
            "title": "Probe",
            "link": sys.argv[1],
            "summary": "",
            "published": None,
            "image_url": None,
        })
        print(f"\nsummary ({len(probe.get('summary', ''))} chars):")
        print(probe.get("summary", "")[:500])
