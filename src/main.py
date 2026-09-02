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

import argparse                                                       # STEP [19]
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


def already_ran_today(now=None, pending_path=None):                    # STEP [31]
    """True if pending_post.json holds a draft created on today's UTC date.

    # STEP [31] daily.yml now fires FOUR morning crons, because GitHub drops and
    # STEP [31] delays scheduled events badly on this repo (measured: the 02:53Z
    # STEP [31] run landing as late as 08:56Z, i.e. 14:26 IST). The first cron
    # STEP [31] GitHub actually starts writes the draft; this guard turns the
    # STEP [31] other three into a green no-op instead of a second digest.
    # STEP [31]
    # STEP [31] MUST be called BEFORE supersede_pending() — that rewrites the file
    # STEP [31] to {"status": "superseded"} with no created_utc, so a check made
    # STEP [31] after it always reads "nothing today" and every cron regenerates.
    # STEP [31]
    # STEP [31] Unprovable date (missing/corrupt/naive stamp) -> False, i.e.
    # STEP [31] GENERATE. Opposite of _is_expired's fail-closed stance in
    # STEP [31] approve.py, and deliberately so: there the risk is posting
    # STEP [31] something stale without consent, here the only risk is a
    # STEP [31] duplicate draft that supersede_pending clobbers anyway. Missing
    # STEP [31] the day's digest entirely is the worse failure.
    # STEP [31]
    # STEP [31] Any status counts, not just awaiting_approval: if today's draft
    # STEP [31] was already posted or rejected, a later cron must NOT quietly
    # STEP [31] produce a second one. Harvey regenerates on purpose via the
    # STEP [31] dashboard or a manual dispatch (--force)."""
    path = pending_path or PENDING_POST_PATH                          # STEP [31]
    try:                                                              # STEP [31]
        with open(path, "r", encoding="utf-8") as fh:                 # STEP [31]
            prev = json.load(fh)                                      # STEP [31]
    except (OSError, json.JSONDecodeError):                           # STEP [31]
        return False                                                  # STEP [31] unreadable -> generate
    if not isinstance(prev, dict):                                    # STEP [31]
        return False                                                  # STEP [31]
    try:                                                              # STEP [31]
        created = datetime.fromisoformat(prev.get("created_utc"))     # STEP [31]
    except (TypeError, ValueError):                                   # STEP [31]
        return False                                                  # STEP [31] no/bad stamp -> generate
    if created.tzinfo is None:                                        # STEP [31]
        return False                                                  # STEP [31] naive stamp proves nothing
    now = now or datetime.now(timezone.utc)                           # STEP [31]
    if now.tzinfo is None:                                            # STEP [31] naive .astimezone() would
        now = now.replace(tzinfo=timezone.utc)                        # STEP [31] silently assume LOCAL time
    return (created.astimezone(timezone.utc).date()                   # STEP [31]
            == now.astimezone(timezone.utc).date())                   # STEP [31]


def supersede_pending(pending_path=None):                              # STEP [19]
    """Invalidate any un-actioned stale draft so the workflow's checkout-restore
    # STEP [19] can never leave an old draft approvable. Terminal / retryable
    # STEP [19] states (posted / approved / post_failed / rejected / expired) are
    # STEP [19] left untouched — defense-in-depth for the audit trail.
    # STEP [19]
    # STEP [19] Extracted from run() so select_build.py reuses the EXACT same
    # STEP [19] supersede-on-start logic — the invariant holds because both entry
    # STEP [19] points call this function, not because each reimplements it.
    # STEP [19] Returns 0 on success, 1 if the supersede write failed (caller
    # STEP [19] must abort red so an old draft can never be approved)."""
    path = pending_path or PENDING_POST_PATH                            # STEP [19]
    if not os.path.exists(path):                                        # STEP [19]
        return 0                                                        # STEP [19] nothing to do
    prev_status = None                                                  # STEP [19]
    try:                                                                # STEP [19]
        with open(path, "r", encoding="utf-8") as fh:                   # STEP [19]
            prev = json.load(fh)                                        # STEP [19]
        if isinstance(prev, dict):                                      # STEP [19]
            prev_status = prev.get("status")                            # STEP [19]
    except (OSError, json.JSONDecodeError):                             # STEP [19]
        prev_status = None   # corrupt → can't trust it → supersede      # STEP [19]
    if prev_status in ("awaiting_approval", "notify_failed",            # STEP [19]
                       "superseded", None):                             # STEP [19]
        try:                                                            # STEP [19]
            with open(path, "w", encoding="utf-8") as fh:               # STEP [19]
                json.dump({"status": "superseded"}, fh, indent=2)       # STEP [19]
        except OSError as exc:                                          # STEP [19]
            log.error("supersede_pending: cannot invalidate stale %s (%s) — "
                      "aborting so an old draft can never be approved",
                      path, exc)                                        # STEP [19]
            return 1                                                    # STEP [19]
    else:                                                               # STEP [19]
        log.info("supersede_pending: leaving %s alone "                  # STEP [19]
                 "(status=%s is terminal/retryable)", path, prev_status)  # STEP [19]
    return 0                                                            # STEP [19]


def run(force=False):                                                  # STEP [31]
    # STEP [31] Before the supersede below, which erases the evidence.
    if not force and already_ran_today():                              # STEP [31]
        log.info("run: today already has a draft — skipping. This is a "  # STEP [31]
                 "later daily.yml cron; an earlier one already generated. "  # STEP [31]
                 "Pass --force to regenerate on purpose.")              # STEP [31]
        return 0                                                       # STEP [31] green no-op, not a failure

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
    if supersede_pending() != 0:                                        # STEP [19] was inline
        return 1                                                        # STEP [19]

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

    # STEP [17] Pick the first usable image across the top stories (not just
    # STEP [17] #1). The lead is frequently a text-only Reddit post with no
    # STEP [17] image, which left every post image-less even when stories #2-5
    # STEP [17] had perfectly good images. The digest is a multi-story roundup,
    # STEP [17] so any top-story image is an appropriate visual.
    image_url = None                                                 # STEP [17]
    for s in top:                                                    # STEP [17]
        if s.get("image_url"):                                       # STEP [17]
            image_url = s["image_url"]                               # STEP [17]
            log.info("run: image from [%s] %s", s["source_name"],    # STEP [17]
                     image_url[:70])                                 # STEP [17]
            break                                                    # STEP [17]
    if not image_url:                                                # STEP [17]
        log.info("run: no image in any top story — post will be text-only")  # STEP [17]

    ok, message_id = notify.send_draft(post, warnings, image_url)   # STEP [8] # STEP [17]

    # STEP [8] Single write AFTER the send attempt: a crash mid-send leaves
    # STEP [8] the "superseded" file from the top of run(), never a stale
    # STEP [8] "awaiting_approval" one.
    pending = {                                                      # STEP [8]
        "draft": post,                                               # STEP [8]
        "created_utc": datetime.now(timezone.utc).isoformat(),       # STEP [8]
        "telegram_message_id": message_id,                           # STEP [8]
        "status": "awaiting_approval" if ok else "notify_failed",    # STEP [8]
        "image_url": image_url,                                      # STEP [11] # STEP [17]
        "image_source": "story" if image_url else None,              # STEP [11] # STEP [17]
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

    # STEP [19] Optional manual-selection entry: dashboard writes a JSON file,
    # STEP [19] workflow copies it to a tmp path, runs main.py with this flag.
    # STEP [19] Default (no args) = unchanged auto-ranked flow. The two paths
    # STEP [19] share supersede_pending() + notify.send_draft + pending_post.json
    # STEP [19] state machine; the manual path is a second entry into the SAME
    # STEP [19] approval machinery, NOT a parallel one.
    parser = argparse.ArgumentParser(description="LinkedIn AI digest generator")  # STEP [19]
    parser.add_argument("--from-selection", metavar="PATH", default=None,          # STEP [19]
                        help="Build the post from a JSON selection file "          # STEP [19]
                             "(dashboard manual-override path)")                  # STEP [19]
    parser.add_argument("--force", action="store_true",                            # STEP [31]
                        help="Generate even if today already has a draft. "        # STEP [31]
                             "daily.yml passes this on manual dispatch only, so "  # STEP [31]
                             "the four morning crons stay idempotent.")            # STEP [31]
    args = parser.parse_args()                                       # STEP [19]
    if args.from_selection:                                          # STEP [19]
        import select_build                                          # STEP [19] lazy: keeps the auto path import-light
        try:                                                         # STEP [19]
            with open(args.from_selection, "r", encoding="utf-8") as fh:  # STEP [19]
                selection = json.load(fh)                            # STEP [19]
        except (OSError, json.JSONDecodeError) as exc:               # STEP [19]
            log.error("main: cannot read selection file %s (%s)",     # STEP [19]
                      args.from_selection, exc)                       # STEP [19]
            sys.exit(1)                                              # STEP [19]
        sys.exit(select_build.build_from_selection(selection))       # STEP [19]

    sys.exit(run(force=args.force))                                  # STEP [31]