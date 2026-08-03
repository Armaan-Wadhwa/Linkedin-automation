"""
select_build.py — Manual-selection entry point (Phase 4, Task 12 / part A).   # STEP [19]

build_from_selection(selection) is a SECOND entry into the SAME notify/approve
machinery as main.run() — NOT a parallel pipeline. It exists so a future web
dashboard (Task 13, out of scope here) can let Harvey hand-pick stories, choose
the image, and add a custom story; the draft still lands in Telegram with the
same Approve/Reject buttons, and approve.py / post_linkedin.py post it via the
SAME path the auto flow uses.

Pipeline (mirrors main.run() with explicit diffs):
  1. supersede_pending()  — SAME function main.run() calls (terminal-state guard)
  2. fetch_all() + rank.dedupe_only() — rebuild the SAME candidate universe the
     dashboard saw; resolve Harvey's story_ids by SHA-256 candidate_id
     (rank.title_hash, the contract from emit_candidates.py). Robust to feed
     drift between refresh and generate.
  3. Append custom_story (if present) with sentinel source_id=0. Bypasses
     dedupe (appended after) — a manual entry is intentional.
  4. Cap the final list at TOP_N_STORIES (5), leaving room for the custom story.
  5. generate_post(stories) — EXISTING generation seam, unchanged.
  6. send_draft(post, warnings, selection.image_url) — EXISTING notify seam.
     selection.image_url overrides auto-pull (it's the dashboard's explicit pick;
     null = text-only post).
  7. record_stories + save_history — AFTER send_draft success.
     # STEP [19] DIVERGENCE FROM main.run(): the auto path records BEFORE
     # STEP [19] send_draft (so a notify failure still burns the stories). The
     # STEP [19] manual path records AFTER a successful send_draft — a Telegram
     # STEP [19] outage must NOT burn Harvey's hand-picked selection. This is a
     # STEP [19] strict improvement, intentional, asserted by test_select.py.
  8. Write pending_post.json exactly as main.run() does (same keys, same
     status vocabulary, same image_source mapping).

INVARIANTS preserved by REUSE (not by reimplementing):
  - supersede-on-start terminal-state guard  -> main.supersede_pending
  - button-message-id stored in pending_post.json -> notify.send_draft return
  - record AFTER successful generation -> generate_post != None check
  - never raises out of the module -> top-level try/except, log + return 1
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
import generate                                                       # STEP [19] noqa: E402
import main                                                           # STEP [19] noqa: E402 reuses supersede_pending
import notify                                                         # STEP [19] noqa: E402
import rank                                                           # STEP [19] noqa: E402

log = logging.getLogger("select_build")                               # STEP [19]

REPO_ROOT = main.REPO_ROOT                                            # STEP [19] single source for paths
HISTORY_PATH = main.HISTORY_PATH                                      # STEP [19]
PENDING_POST_PATH = main.PENDING_POST_PATH                            # STEP [19]

# STEP [19] Sentinel for the manual custom story. source_id 0 is unused across
# STEP [19] config.SOURCES (1..21 reserved) and config.NEWSLETTER_SOURCE_ID (18).
# STEP [19] priority 10 = the highest tier in config (Claude/Anthropic), so the
# STEP [19] manual entry is treated as first-class by any priority-aware code.
MANUAL_SOURCE_ID = 0                                                  # STEP [19]
MANUAL_SOURCE_NAME = "manual"                                         # STEP [19]
MANUAL_PRIORITY = 10                                                  # STEP [19]


def _is_str(x):
    return isinstance(x, str)                                         # STEP [19]


def _validate_selection(selection):
    """Return (story_ids, image_url, custom_story) on success, or None on any
    # STEP [19] malformation. Strict: rejects unknown keys / wrong types instead
    # STEP [19] of guessing. Logs the reason; the caller exits 1."""
    if not isinstance(selection, dict):                               # STEP [19]
        log.error("build_from_selection: selection must be a JSON object")  # STEP [19]
        return None                                                   # STEP [19]
    # STEP [19] Unknown top-level keys are rejected (defense against a future
    # STEP [19] dashboard bug sending the wrong shape — fail loud, never guess).
    allowed = {"story_ids", "image_url", "custom_story"}               # STEP [19]
    unknown = set(selection) - allowed                                # STEP [19]
    if unknown:                                                       # STEP [19]
        log.error("build_from_selection: unknown keys %s (allowed %s)",  # STEP [19]
                  sorted(unknown), sorted(allowed))                   # STEP [19]
        return None                                                   # STEP [19]

    raw_ids = selection.get("story_ids")                              # STEP [19]
    if not isinstance(raw_ids, list) or not raw_ids:                  # STEP [19]
        log.error("build_from_selection: story_ids must be a non-empty list")  # STEP [19]
        return None                                                   # STEP [19]
    # STEP [19] Contract: story_ids are the candidate_id (sha256 hex) strings
    # STEP [19] emit_candidates.py wrote to candidates.json — what Harvey tapped
    # STEP [19] in the dashboard. Strict on type (string) so a malformed payload
    # STEP [19] fails loud instead of being guessed at; resolve will skip ids
    # STEP [19] that don't match a current candidate (feed drift between refresh
    # STEP [19] and generate can drop a story — non-fatal if ≥1 still resolves).
    if not all(_is_str(i) and i for i in raw_ids):                    # STEP [19]
        log.error("build_from_selection: every story_id must be a non-empty "  # STEP [19]
                  "string (candidate_id sha256 hex from candidates.json; "  # STEP [19]
                  "got types %s)", [type(i).__name__ for i in raw_ids])  # STEP [19]
        return None                                                   # STEP [19]

    image_url = selection.get("image_url", None)                      # STEP [19]
    if image_url is not None and not _is_str(image_url):              # STEP [19]
        log.error("build_from_selection: image_url must be a string or null")  # STEP [19]
        return None                                                   # STEP [19]

    custom = selection.get("custom_story", None)                      # STEP [19]
    if custom is not None:                                            # STEP [19]
        if not isinstance(custom, dict):                              # STEP [19]
            log.error("build_from_selection: custom_story must be an object or null")  # STEP [19]
            return None                                               # STEP [19]
        missing = [k for k in ("title", "link") if not _is_str(custom.get(k))]  # STEP [19]
        if missing:                                                   # STEP [19]
            log.error("build_from_selection: custom_story missing/invalid %s",  # STEP [19]
                      missing)                                        # STEP [19]
            return None                                               # STEP [19]
        summary = custom.get("summary", None)                         # STEP [19]
        if summary is not None and not _is_str(summary):              # STEP [19]
            log.error("build_from_selection: custom_story.summary must be a string or null")  # STEP [19]
            return None                                               # STEP [19]
        unknown_c = set(custom) - {"title", "link", "summary"}        # STEP [19]
        if unknown_c:                                                 # STEP [19]
            log.error("build_from_selection: custom_story unknown keys %s",  # STEP [19]
                      sorted(unknown_c))                              # STEP [19]
            return None                                               # STEP [19]

    return raw_ids, image_url, custom                                 # STEP [19]


def _resolve_stories(story_ids):
    """Fetch + dedupe and return (resolved_list, candidate_index).
    # STEP [19] candidate_index maps candidate_id (= rank.title_hash, identical
    # STEP [19] to emit_candidates.py's output) → story dict. resolved_list is
    # STEP [19] the stories Harvey picked, IN HIS ORDER. Unresolvable ids are
    # STEP [19] logged and skipped (feed drift between refresh and generate can
    # STEP [19] drop a story; that's non-fatal as long as ≥1 resolves)."""
    history = rank.load_history(HISTORY_PATH)                         # STEP [19]
    candidates = rank.dedupe_only(fetch.fetch_all(), history)         # STEP [19]
    index = {c["hash"]: c for c in candidates}                        # STEP [19]
    resolved = []                                                     # STEP [19]
    for sid in story_ids:                                             # STEP [19]
        # STEP [19] story_ids ARE the candidate_id strings from candidates.json.
        # STEP [19] No coercion, no second normalization — direct index lookup.
        story = index.get(sid)                                        # STEP [19]
        if story is None:                                             # STEP [19]
            log.warning("build_from_selection: story_id %s not in current "  # STEP [19]
                        "candidate set (%d candidates) — skipping",    # STEP [19]
                        sid, len(index))                              # STEP [19]
            continue                                                  # STEP [19]
        resolved.append(story)                                        # STEP [19]
    return resolved, index                                            # STEP [19]


def _build_custom_story(custom, image_url):
    """Build a story dict from the manual custom_story input.
    # STEP [19] source_id 0 sentinel; priority 10 (highest tier) so it's treated
    # STEP [19] as first-class; published=now so it survives rank's recency gate
    # STEP [19] (defensive — record_stories uses title_hash, not score, but a
    # STEP [19] 'published' key is required by generate.py's _stories_block)."""
    title = custom["title"]                                           # STEP [19]
    return {                                                          # STEP [19]
        "source_id": MANUAL_SOURCE_ID,                                # STEP [19]
        "source_name": MANUAL_SOURCE_NAME,                            # STEP [19]
        "priority": MANUAL_PRIORITY,                                  # STEP [19]
        "title": title,                                               # STEP [19]
        "link": custom["link"],                                       # STEP [19]
        "summary": custom.get("summary") or "",                       # STEP [19]
        "published": datetime.now(timezone.utc),                      # STEP [19]
        "image_url": None,                                            # STEP [19] post-level image only (Q3)
        "hash": rank.title_hash(title),                               # STEP [19] for record_stories
        "score": 0.0,                                                 # STEP [19] advisory only
    }


def build_from_selection(selection):
    """Build a post from Harvey's dashboard selection and send the draft to
    # STEP [19] Telegram for approval. Returns process exit code 0/1. Never raises.
    # STEP [19] Does NOT post to LinkedIn — approve.py does that, untouched."""
    try:                                                              # STEP [19] top-level guard
        validated = _validate_selection(selection)                    # STEP [19]
        if validated is None:                                         # STEP [19]
            return 1                                                  # STEP [19] reason already logged
        story_ids, image_url, custom = validated                      # STEP [19]

        # STEP [19] Supersede any stale un-actioned draft BEFORE we touch anything.
        # STEP [19] Same function main.run() calls → same terminal-state guard.
        if main.supersede_pending() != 0:                             # STEP [19]
            return 1                                                  # STEP [19]

        resolved, _index = _resolve_stories(story_ids)                # STEP [19]

        # STEP [19] Compose final story list (resolved ids in Harvey's order,
        # STEP [19] then optional custom story). Cap at TOP_N_STORIES (5):
        # STEP [19] leave room for the custom story so Harvey's manual entry is
        # STEP [19] never dropped in favor of a 5th fetched story.
        cap = config.TOP_N_STORIES - (1 if custom is not None else 0)  # STEP [19]
        picked = resolved[:cap]                                       # STEP [19]
        if custom is not None:                                        # STEP [19]
            picked.append(_build_custom_story(custom, image_url))     # STEP [19]

        if not picked:                                                # STEP [19]
            log.error("build_from_selection: no story_ids resolved "  # STEP [19]
                      "(%d requested, %d resolved) — nothing to post",  # STEP [19]
                      len(story_ids), len(resolved))                  # STEP [19]
            return 1                                                  # STEP [19]

        log.info("build_from_selection: %d stories (%d from ids + %d custom)",  # STEP [19]
                 len(picked), len(resolved[:cap]),                     # STEP [19]
                 1 if custom is not None else 0)                       # STEP [19]
        for s in picked:                                              # STEP [19]
            log.info("  [%s] %s", s["source_name"], s["title"][:70])   # STEP [19]

        post = generate.generate_post(picked)                         # STEP [19] EXISTING seam
        if post is None:                                              # STEP [19]
            log.error("build_from_selection: generation failed — not "  # STEP [19]
                      "recording history, not sending a draft")       # STEP [19]
            return 1                                                  # STEP [19]

        warnings = generate.validate_post(post)                       # STEP [19]

        # STEP [19] selection.image_url overrides auto-pull: if null → text-only,
        # STEP [19] even if the picked stories have images. This is the dashboard's
        # STEP [19] explicit choice.
        ok, message_id = notify.send_draft(post, warnings, image_url)  # STEP [19] EXISTING seam

        # STEP [19] Record history ONLY after send_draft succeeded (strict
        # STEP [19] improvement over main.run(); see module docstring + test 9).
        if ok:                                                        # STEP [19]
            history = rank.load_history(HISTORY_PATH)                 # STEP [19]
            rank.record_stories(history, picked)                      # STEP [19]
            if not rank.save_history(history, HISTORY_PATH):          # STEP [19]
                log.warning("build_from_selection: history not saved "  # STEP [19]
                            "(picked stories may resurface)")          # STEP [19]

        # STEP [19] Write pending_post.json with the EXACT same shape main.run() uses,
        # STEP [19] so approve.py reads it identically (status vocabulary, keys,
        # STEP [19] image_source mapping all unchanged). image_source = "story"
        # STEP [19] because the dashboard picked a story image URL (not a Telegram
        # STEP [19] photo reply, which would be "telegram_override" via approve.py).
        pending = {                                                   # STEP [19]
            "draft": post,                                            # STEP [19]
            "created_utc": datetime.now(timezone.utc).isoformat(),    # STEP [19]
            "telegram_message_id": message_id,                        # STEP [19]
            "status": "awaiting_approval" if ok else "notify_failed",  # STEP [19]
            "image_url": image_url,                                   # STEP [19]
            "image_source": "story" if image_url else None,           # STEP [19]
        }                                                            # STEP [19]
        try:                                                          # STEP [19]
            with open(PENDING_POST_PATH, "w", encoding="utf-8") as fh:  # STEP [19]
                json.dump(pending, fh, indent=2, ensure_ascii=False)  # STEP [19]
        except OSError as exc:                                        # STEP [19]
            log.error("build_from_selection: could not write %s (%s) — "  # STEP [19]
                      "Run B will have nothing to approve",            # STEP [19]
                      PENDING_POST_PATH, exc)                          # STEP [19]
            return 1                                                  # STEP [19]

        print("\n" + "=" * 70)                                        # STEP [19]
        print("DRAFT POST (manual selection — sent to Telegram, awaiting approval)"  # STEP [19]
              if ok else                                               # STEP [19]
              "DRAFT POST (Telegram notify FAILED — marked not-approvable)")  # STEP [19]
        print("=" * 70)                                               # STEP [19]
        print(post)                                                   # STEP [19]
        print("=" * 70)                                               # STEP [19]
        print(f"({len(post)} characters, {len(picked)} stories)")     # STEP [19]
        if not ok:                                                    # STEP [19]
            log.error("build_from_selection: draft generated but not "  # STEP [19]
                      "delivered for approval — run goes red")         # STEP [19]
            return 1                                                  # STEP [19]
        return 0                                                      # STEP [19]
    except Exception as exc:  # noqa: BLE001                          # STEP [19] never raise out
        log.exception("build_from_selection: unexpected failure: %s: %s",  # STEP [19]
                      type(exc).__name__, exc)                        # STEP [19]
        return 1                                                      # STEP [19]


if __name__ == "__main__":                                            # STEP [19]
    # STEP [19] Manual smoke test: python src/select_build.py sel.json
    # STEP [19] Reads a selection JSON from argv[1], builds, sends draft.
    if hasattr(sys.stdout, "reconfigure"):                            # STEP [19]
        sys.stdout.reconfigure(encoding="utf-8")                      # STEP [19]
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")  # STEP [19]
    if len(sys.argv) != 2:                                            # STEP [19]
        print("usage: python src/select_build.py <selection.json>")  # STEP [19]
        sys.exit(2)                                                   # STEP [19]
    try:                                                              # STEP [19]
        with open(sys.argv[1], "r", encoding="utf-8") as fh:          # STEP [19]
            selection = json.load(fh)                                 # STEP [19]
    except (OSError, json.JSONDecodeError) as exc:                    # STEP [19]
        log.error("cannot read %s (%s)", sys.argv[1], exc)            # STEP [19]
        sys.exit(2)                                                   # STEP [19]
    sys.exit(build_from_selection(selection))                         # STEP [19]
