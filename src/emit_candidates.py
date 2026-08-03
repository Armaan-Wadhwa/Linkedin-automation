"""
emit_candidates.py — Refresh the dashboard's candidate menu (Phase 4, Task 12).  # STEP [19]

Standalone entry: fetch + dedupe + write docs/candidates.json. The future Task 13
dashboard (a static GitHub Pages site, OUT OF SCOPE here) reads this file and
lets Harvey hand-pick which stories become today's post.

Output shape (the contract select_build.py resolves against):
    {
      "generated_utc": "<ISO8601>",
      "candidates": [
        {
          "candidate_id": "<sha256 hex of normalized title>",   # = rank.title_hash
          "source_id": int, "source_name": str, "priority": int,
          "title": str, "link": str, "summary": str,
          "published": "<ISO8601>" | null,
          "image_url": str | null
        }, ...
      ],
      "error": str | null   # present only on partial/total failure
    }

Design:
- Emits the FULL ranked, deduped list (via rank.dedupe_only — NOT dedupe_and_rank,
  which would cap at top-5 with the per-source cap of 2). Cap at 40 to bound file
  size; this gives Harvey a real menu.
- candidate_id MUST match select_build.py's resolution exactly: both use
  rank.title_hash verbatim. Never change one without the other.
- ALWAYS non-fatal: a fetch/rank failure writes an empty list + an "error" field
  so the dashboard degrades gracefully instead of crashing on a 404.
- No secrets in the file. published is ISO8601 (or null for undated stories).
"""

import json                                                           # STEP [19]
import logging                                                        # STEP [19]
import os                                                             # STEP [19]
import sys                                                            # STEP [19]
from datetime import datetime, timezone                               # STEP [19]

SRC_DIR = os.path.dirname(os.path.abspath(__file__))                  # STEP [19]
if SRC_DIR not in sys.path:                                           # STEP [19] idempotent
    sys.path.insert(0, SRC_DIR)                                       # STEP [19]

import config                                                         # STEP [19] noqa: E402
import fetch                                                          # STEP [19] noqa: E402
import rank                                                           # STEP [19] noqa: E402

log = logging.getLogger("emit_candidates")                            # STEP [19]

REPO_ROOT = os.path.dirname(SRC_DIR)                                  # STEP [19]
DOCS_DIR = os.path.join(REPO_ROOT, "docs")                            # STEP [19] GitHub Pages source dir (Task 13)
CANDIDATES_FILE = os.path.join(DOCS_DIR, "candidates.json")           # STEP [19]

MAX_CANDIDATES = 40                                                   # STEP [19] cap to bound file size


def _serialize_story(story):
    """Project a rank.dedupe_only story dict to the public candidates.json shape.
    # STEP [19] 'hash' → 'candidate_id' (same value, just renamed for the
    # STEP [19] dashboard-facing contract). 'score' is internal — dropped."""
    pub = story.get("published")                                      # STEP [19]
    return {                                                          # STEP [19]
        "candidate_id": story["hash"],                                # STEP [19] = rank.title_hash
        "source_id": story["source_id"],                              # STEP [19]
        "source_name": story["source_name"],                          # STEP [19]
        "priority": story["priority"],                                # STEP [19]
        "title": story["title"],                                      # STEP [19]
        "link": story["link"],                                        # STEP [19]
        "summary": story.get("summary") or "",                        # STEP [19]
        "published": pub.isoformat() if pub else None,                # STEP [19]
        "image_url": story.get("image_url"),                          # STEP [19]
    }


def emit():
    """Fetch + rank + write candidates.json. Returns 0 on success, 1 on any
    # STEP [19] failure. The file is ALWAYS written (even on failure: empty list
    # STEP [19] + error field) so the dashboard never 404s."""
    error = None                                                      # STEP [19]
    candidates_payload = []                                           # STEP [19]
    try:                                                              # STEP [19]
        history = rank.load_history()                                 # STEP [19] exclude already-used stories
        ranked = rank.dedupe_only(fetch.fetch_all(), history)         # STEP [19] full ranked+deduped list
        candidates_payload = [_serialize_story(s) for s in ranked[:MAX_CANDIDATES]]  # STEP [19]
        log.info("emit_candidates: %d ranked, %d emitted (cap %d)",    # STEP [19]
                 len(ranked), len(candidates_payload), MAX_CANDIDATES)  # STEP [19]
    except Exception as exc:  # noqa: BLE001                          # STEP [19] never crash the run
        log.exception("emit_candidates: fetch/rank failed: %s: %s",   # STEP [19]
                      type(exc).__name__, exc)                        # STEP [19]
        error = f"{type(exc).__name__}: {exc}"                        # STEP [19]

    payload = {                                                       # STEP [19]
        "generated_utc": datetime.now(timezone.utc).isoformat(),      # STEP [19]
        "candidates": candidates_payload,                             # STEP [19]
    }
    if error is not None:                                             # STEP [19]
        payload["error"] = error                                      # STEP [19]

    try:                                                              # STEP [19]
        os.makedirs(DOCS_DIR, exist_ok=True)                          # STEP [19] create docs/ if absent
        with open(CANDIDATES_FILE, "w", encoding="utf-8") as fh:      # STEP [19]
            json.dump(payload, fh, indent=2, ensure_ascii=False)      # STEP [19]
        log.info("emit_candidates: wrote %s (%d candidates)",         # STEP [19]
                 CANDIDATES_FILE, len(candidates_payload))            # STEP [19]
    except OSError as exc:                                            # STEP [19]
        log.error("emit_candidates: cannot write %s (%s)",            # STEP [19]
                  CANDIDATES_FILE, exc)                               # STEP [19]
        return 1                                                      # STEP [19]

    return 0 if error is None else 1                                  # STEP [19]


if __name__ == "__main__":                                            # STEP [19]
    if hasattr(sys.stdout, "reconfigure"):                            # STEP [19]
        sys.stdout.reconfigure(encoding="utf-8")                      # STEP [19]
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")  # STEP [19]
    sys.exit(emit())                                                  # STEP [19]
