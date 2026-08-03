"""
rank.py — Dedupe, score, and select the top stories for today's post.

Pipeline (dedupe_and_rank):
  1. Discard stories older than MAX_STORY_AGE_H or undated.
  2. Dedupe by normalized-title SHA-256:
       - against history.json (stories already used in past posts), and
       - within the current batch (same story from multiple sources — the
         highest-priority source's copy wins).
  3. Score = recency + source priority + topic tier + launch/use-case bonus.
  4. Return the TOP_N_STORIES highest scorers, best first.

History helpers (load_history / record_stories / save_history) manage the
committed history.json file so the same story is never reused within
HISTORY_RETENTION_DAYS. main.py calls record_stories AFTER a successful
generation, never before.

No network access in this module. Never raises on malformed history files.
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone, timedelta

import config

log = logging.getLogger(__name__)

_NORM_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------
def title_hash(title):
    """SHA-256 of the normalized title (lowercase, alphanumerics+spaces only)."""
    norm = _WS_RE.sub(" ", _NORM_RE.sub(" ", title.lower())).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _recency_points(story, now):
    """RECENCY_MAX_POINTS at 0h, linear decay to 0 at MAX_STORY_AGE_H.
    Returns None if the story is undated or too old (=> discard)."""
    if story["published"] is None:
        return None
    age_h = (now - story["published"]).total_seconds() / 3600.0
    if age_h < 0:
        age_h = 0.0  # future-dated entries: treat as brand new
    if age_h > config.MAX_STORY_AGE_H:
        return None
    return config.RECENCY_MAX_POINTS * (1.0 - age_h / config.MAX_STORY_AGE_H)


def _topic_points(text):
    """Highest matching topic tier (not summed) + stacking launch/use-case bonuses."""
    lower = text.lower()
    points = 0
    for tier_points, keywords in config.TOPIC_TIERS:
        if any(kw in lower for kw in keywords):
            points = tier_points
            break  # tiers are ordered highest-first; take the first match only
    if any(kw in lower for kw in config.LAUNCH_KEYWORDS):
        points += config.LAUNCH_BONUS
    if any(kw in lower for kw in config.USECASE_KEYWORDS):
        points += config.USECASE_BONUS
    return points


def score_story(story, now):
    """Total score, or None if the story should be discarded (stale/undated)."""
    recency = _recency_points(story, now)
    if recency is None:
        return None
    text = f"{story['title']} {story['summary']}"
    topic = _topic_points(text)                                     # FIX [6]
    if story["source_id"] in config.COMMUNITY_SOURCE_IDS:           # FIX [6] Reddit/HN mention
        topic *= config.COMMUNITY_TOPIC_FACTOR                      # FIX [6] of "claude" != Claude news
    return recency + story["priority"] + topic                      # FIX [6]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def dedupe_only(stories, history, now=None):                          # STEP [19]
    """Dedupe + score + sort the FULL candidate list (no per-source cap, no
    # STEP [19] top-N slice). Used by emit_candidates.py (dashboard menu) and
    # STEP [19] select_build.py (manual id resolution). dedupe_and_rank() below
    # STEP [19] calls this then applies the per-source cap + top-N slice, so its
    # STEP [19] behavior is byte-identical to before this refactor.

    # STEP [19] Each returned story gets 'score' and 'hash' keys added. The
    # STEP [19] 'hash' key IS the candidate_id contract (SHA-256 of normalized
    # STEP [19] title) — emit_candidates.py and select_build.py rely on it being
    # STEP [19] identical here and in title_hash() above. Never change one
    # STEP [19] without the other."""
    now = now or datetime.now(timezone.utc)                            # STEP [19]
    seen_hashes = set(history.get("hashes", {}))                       # STEP [19]
    candidates = []                                                    # STEP [19]
    # STEP [19] Highest-priority copy of a cross-source duplicate wins the
    # STEP [19] dedupe, so consider stories in priority order.
    for story in sorted(stories, key=lambda s: s["priority"], reverse=True):  # STEP [19]
        score = score_story(story, now)                                # STEP [19]
        if score is None:                                              # STEP [19]
            continue                                                   # STEP [19]
        h = title_hash(story["title"])                                 # STEP [19]
        if h in seen_hashes:                                           # STEP [19]
            continue                                                   # STEP [19]
        seen_hashes.add(h)                                             # STEP [19]
        candidates.append(dict(story, score=round(score, 2), hash=h))  # STEP [19]
    candidates.sort(key=lambda s: s["score"], reverse=True)            # STEP [19]
    log.info("dedupe_only: %d in -> %d fresh+unique",                  # STEP [19]
             len(stories), len(candidates))                            # STEP [19]
    return candidates                                                  # STEP [19]


def dedupe_and_rank(stories, history, now=None):
    """Return the top TOP_N_STORIES as a list of story dicts (a 'score' key is
    added to each), best first. `history` is the dict from load_history()."""
    candidates = dedupe_only(stories, history, now)                    # STEP [19] extracted
    top, per_source = [], {}                                        # FIX [6] source diversity
    for story in candidates:                                        # FIX [6] # STEP [19]
        sid = story["source_id"]                                    # FIX [6]
        if per_source.get(sid, 0) >= config.MAX_PER_SOURCE_IN_TOP:  # FIX [6]
            continue                                                # FIX [6]
        per_source[sid] = per_source.get(sid, 0) + 1                # FIX [6]
        top.append(story)                                           # FIX [6]
        if len(top) >= config.TOP_N_STORIES:                        # FIX [6]
            break                                                   # FIX [6]
    log.info("dedupe_and_rank: %d in -> %d fresh+unique -> top %d",
             len(stories), len(candidates), len(top))
    return top


# ---------------------------------------------------------------------------
# History file helpers
# ---------------------------------------------------------------------------
def load_history(path=config.HISTORY_FILE):
    """Load history.json; returns {'hashes': {hash: iso_date}} pruned to
    HISTORY_RETENTION_DAYS. Missing/corrupt file => empty history (logged)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            history = json.load(fh)
        hashes = history.get("hashes", {})
        if not isinstance(hashes, dict):
            raise ValueError("'hashes' is not a dict")
    except FileNotFoundError:
        log.info("load_history: %s not found, starting empty", path)
        return {"hashes": {}}
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("load_history: %s unreadable (%s), starting empty", path, exc)
        return {"hashes": {}}

    cutoff = datetime.now(timezone.utc) - timedelta(days=config.HISTORY_RETENTION_DAYS)
    pruned = {}
    for h, iso in hashes.items():
        try:
            if datetime.fromisoformat(iso) >= cutoff:
                pruned[h] = iso
        except (TypeError, ValueError):
            continue  # drop malformed timestamps
    if len(pruned) != len(hashes):
        log.info("load_history: pruned %d expired hashes", len(hashes) - len(pruned))
    return {"hashes": pruned}


def record_stories(history, stories, now=None):
    """Add the used stories' hashes to history (in place). Call AFTER a
    successful generation so a failed run doesn't burn the day's stories."""
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    for story in stories:
        history["hashes"][story.get("hash") or title_hash(story["title"])] = now_iso
    return history


def save_history(history, path=config.HISTORY_FILE):
    """Write history.json. Returns True on success; never raises."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(history, fh, indent=2, sort_keys=True)
        return True
    except OSError as exc:
        log.warning("save_history: could not write %s (%s)", path, exc)
        return False


if __name__ == "__main__":
    # Manual test: fetch live feeds, rank them, show the top stories.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    import fetch
    history = load_history()
    top = dedupe_and_rank(fetch.fetch_all(), history)
    print(f"\nTop {len(top)} stories:")
    for s in top:
        when = s["published"].strftime("%m-%d %H:%M") if s["published"] else "undated"
        print(f"  {s['score']:>6.2f}  [{s['source_name']}] ({when}) {s['title'][:70]}")