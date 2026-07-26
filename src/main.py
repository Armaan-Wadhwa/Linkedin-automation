"""
main.py — orchestrator: fetch -> rank -> generate -> notify -> log.

STEP [8] (Phase 2, Task 5 / Run A): after a successful generation the draft
is saved to pending_post.json and sent to Harvey on Telegram with Approve /
Reject buttons. NOTHING is posted to LinkedIn here — Run B (Task 6) reads
the decision. Stories are recorded in history.json ONLY when generation
succeeded (a failed run must not burn the day's stories); a notify failure
still records history (the draft exists, the stories are used).

Run from the repo root:  python src/main.py
(history.json / pending_post.json resolve to the repo root regardless of CWD.)

Exit codes: 0 = draft generated AND awaiting approval on Telegram;
1 = anything else (no stories, LLM failed, or notify/state-file failed —
the unreviewed draft is marked not-approvable). Red run = alerting.
"""

import json      # STEP [8]
import logging
import os
import sys
from datetime import datetime, timezone   # STEP [8]

# Make sibling modules importable no matter where main.py is invoked from.
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SRC_DIR)
sys.path.insert(0, SRC_DIR)

import config           # noqa: E402
import fetch            # noqa: E402
import generate         # noqa: E402
import notify           # noqa: E402  # STEP [8]
import rank             # noqa: E402

log = logging.getLogger("main")

HISTORY_PATH = os.path.join(REPO_ROOT, config.HISTORY_FILE)
PENDING_POST_PATH = os.path.join(REPO_ROOT, config.PENDING_POST_FILE)  # STEP [8]


def run():
    # STEP [8] FIRST, before anything can fail: the workflow commits
    # STEP [8] pending_post.json, so checkout restores yesterday's file on
    # STEP [8] every run. Rewrite it to a terminal status now (NOT delete —
    # STEP [8] the commit step can only `git add` a file that exists), so no
    # STEP [8] early exit or crash today leaves an old draft approvable.
    #
    # STEP [10] GUARD: only clobber an UN-ACTIONED stale draft. Terminal /
    # STEP [10] retryable states from Task 7 (posted / approved / post_failed /
    # STEP [10] rejected / expired) must survive — "posted" carries the
    # STEP [10] linkedin_post_id audit trail, "approved" is the retryable
    # STEP [10] state approve.py picks up on its next poll. Clobbering those
    # STEP [10] would lose the post record or a pending retry. The concurrency
    # STEP [10] group already prevents a same-moment approve.py interruption,
    # STEP [10] so this guard is defense-in-depth for the audit trail.
    if os.path.exists(PENDING_POST_PATH):                            # STEP [10]
        prev_status = None                                          # STEP [10]
        try:                                                         # STEP [10]
            with open(PENDING_POST_PATH, "r", encoding="utf-8") as fh:  # STEP [10]
                prev = json.load(fh)                                 # STEP [10]
            if isinstance(prev, dict):                               # STEP [10]
                prev_status = prev.get("status")                     # STEP [10]
        except (OSError, json.JSONDecodeError):                      # STEP [10]
            prev_status = None   # corrupt → can't trust it → supersede  # STEP [10]
        if prev_status in ("awaiting_approval", "notify_failed",      # STEP [10]
                           "superseded", None):                       # STEP [10]
            try:                                                     # STEP [10]
                with open(PENDING_POST_PATH, "w", encoding="utf-8") as fh:  # STEP [10]
                    json.dump({"status": "superseded"}, fh, indent=2)  # STEP [10]
            except OSError as exc:                                   # STEP [10]
                log.error("run: cannot invalidate stale %s (%s) — aborting so "
                          "an old draft can never be approved",
                          PENDING_POST_PATH, exc)                    # STEP [10]
                return 1                                             # STEP [10]
        else:                                                        # STEP [10]
            log.info("run: leaving pending_post.json alone "          # STEP [10]
                     "(status=%s is terminal/retryable)", prev_status)  # STEP [10]

    stories = fetch.fetch_all()
    if not stories:
        log.error("run: every source failed — nothing to work with")
        return 1

    history = rank.load_history(HISTORY_PATH)
    top = rank.dedupe_and_rank(stories, history)
    if not top:
        log.error("run: no fresh, unused stories today — skipping generation")
        return 1

    log.info("run: today's selection:")
    for s in top:
        when = s["published"].strftime("%m-%d %H:%M") if s["published"] else "undated"
        log.info("  %6.2f  [%s] (%s) %s", s["score"], s["source_name"], when, s["title"][:70])

    post = generate.generate_post(top)
    if post is None:
        return 1  # generation failed; stories NOT recorded, reusable tomorrow/retry

    # Success: record the used stories so they never repeat.
    rank.record_stories(history, top)
    if not rank.save_history(history, HISTORY_PATH):
        log.warning("run: history not saved — tomorrow may repeat today's stories")

    warnings = generate.validate_post(post)                          # STEP [8]
    ok, message_id = notify.send_draft(post, warnings)               # STEP [8]

    # STEP [8] Single write AFTER the send attempt: a crash mid-send leaves
    # STEP [8] the "superseded" file from the top of run(), never a stale
    # STEP [8] "awaiting_approval" one.
    pending = {                                                      # STEP [8]
        "draft": post,                                               # STEP [8]
        "created_utc": datetime.now(timezone.utc).isoformat(),       # STEP [8]
        "telegram_message_id": message_id,                           # STEP [8]
        "status": "awaiting_approval" if ok else "notify_failed",    # STEP [8]
        "image_url": (top[0].get("image_url") if top else None),     # STEP [11]
        "image_source": "story" if (top and top[0].get("image_url")) else None,  # STEP [11]
    }                                                                # STEP [8]
    try:                                                             # STEP [8]
        with open(PENDING_POST_PATH, "w", encoding="utf-8") as fh:   # STEP [8]
            json.dump(pending, fh, indent=2, ensure_ascii=False)     # STEP [8]
    except OSError as exc:                                           # STEP [8]
        log.error("run: could not write %s (%s) — Run B will have nothing "
                  "to approve", PENDING_POST_PATH, exc)              # STEP [8]
        return 1                                                     # STEP [8]

    print("\n" + "=" * 70)
    print("DRAFT POST (sent to Telegram, awaiting approval)" if ok else
          "DRAFT POST (Telegram notify FAILED — marked not-approvable)")  # STEP [8]
    print("=" * 70)
    print(post)
    print("=" * 70)
    print(f"({len(post)} characters, {len(top)} stories)")
    if not ok:                                                       # STEP [8]
        log.error("run: draft generated but not delivered for approval — "
                  "run goes red")                                    # STEP [8]
        return 1                                                     # STEP [8]
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):        # STEP [8] Windows cp1252 console
        sys.stdout.reconfigure(encoding="utf-8")  # STEP [8] can't encode draft emoji
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(run())