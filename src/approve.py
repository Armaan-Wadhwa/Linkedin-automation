"""
approve.py — Run B of the two-run Telegram approval flow (Phase 2, Task 6+7).

Reads Harvey's ✅/❌ tap from Telegram and records it in pending_post.json.
On a winning ✅, the draft is posted to LinkedIn IN THIS SAME RUN (Task 7),
atomically with the approval — not deferred to a later run — so main.py's
supersede can never discard an approved-but-unposted draft. There is no
auto-approve path and no timeout-defaults-to-yes (hard constraint #4); every
ambiguous or unverifiable case resolves to "not approvable". A post happens
only after a fresh re-read of the state file confirms status == "approved",
and "posted" is terminal (a post can never be published twice).

Run from the repo root:  python src/approve.py
(pending_post.json resolves to the repo root regardless of CWD.)

Design notes worth keeping:
- We NEVER pass a positive `offset` to getUpdates. Confirming an update is
  irreversible; if we confirmed and the workflow's push then failed, the tap
  would be gone from both the repo and Telegram with the buttons already
  stripped. Not confirming makes that self-healing — the next poll re-finds
  the same callback. Idempotency comes from the status gate below instead.
- We DO pass a negative offset (-TELEGRAM_UPDATE_LIMIT). With no offset,
  getUpdates returns the OLDEST updates capped at `limit`, so a queue over
  100 would leave today's callback past the cutoff: permanently invisible,
  logging "no decision yet" forever, indistinguishable from "hasn't tapped".
  A negative offset reads the newest 100 and forgets older ones.
- `allowed_updates=["callback_query"]` keeps plain messages out of the queue.
  NOTE: this setting persists bot-globally — a plain browser getUpdates will
  then show only callbacks unless allowed_updates=["message"] is passed, and
  a future task wanting /commands must add "message" back to this list.
- A tap can only be matched while the draft is fresh (APPROVAL_EXPIRY_H).

Exit codes: 0 = nothing to do, or a decision was recorded. 1 = genuinely
broken state (unreadable/corrupt state file, or a failed state save).
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SRC_DIR)
sys.path.insert(0, SRC_DIR)

import config         # noqa: E402
import post_linkedin  # noqa: E402  # STEP [10]
import telegram_api   # noqa: E402
import token_status   # noqa: E402  # STEP [10]

log = logging.getLogger("approve")

PENDING_POST_PATH = os.path.join(REPO_ROOT, config.PENDING_POST_FILE)

# callback_data values must match notify.py's _KEYBOARD exactly. This dict is
# both the whitelist and the status mapping; sharing it via config would mean
# editing notify.py, which is out of scope for this task.
DECISIONS = {"approve": "approved", "reject": "rejected"}

_MISSING = object()   # sentinel: file absent (exit 0) vs corrupt (exit 1)


def _load_state():
    """Return the state dict, None if corrupt/unreadable, or _MISSING."""
    try:
        with open(PENDING_POST_PATH, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        return _MISSING
    except (OSError, json.JSONDecodeError) as exc:
        log.error("load: %s is unreadable (%s)", PENDING_POST_PATH, exc)
        return None
    if not isinstance(state, dict):
        log.error("load: %s is not a JSON object", PENDING_POST_PATH)
        return None
    return state


def _save_state(state):
    """Write the state file. Returns True on success; never raises."""
    try:
        with open(PENDING_POST_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
        return True
    except OSError as exc:
        log.error("save: could not write %s (%s)", PENDING_POST_PATH, exc)
        return False


def _is_expired(created_utc, now=None):
    """True if the draft is too old to approve, or its age can't be proven.
    Missing / malformed / timezone-naive stamps all count as expired: an
    unverifiable age must never stay approvable (constraint #4)."""
    now = now or datetime.now(timezone.utc)
    try:
        created = datetime.fromisoformat(created_utc)
    except (TypeError, ValueError):
        log.error("expiry: created_utc is missing or malformed (%r)", created_utc)
        return True
    if created.tzinfo is None:
        # Assuming UTC on a naive stamp could silently extend the window by
        # up to 14h depending on the writer's timezone.
        log.error("expiry: created_utc has no timezone — treating as expired")
        return True
    return now - created > timedelta(hours=config.APPROVAL_EXPIRY_H)


def _find_decision(updates, chat_id, message_id):
    """Return (status, update_id, callback_query) for Harvey's decision, or
    None. REJECT WINS over approve regardless of update_id order: a ✅ then
    ❌ correction must never post (fail-closed)."""
    matches = []
    seen_callbacks = matched_chat = 0
    for upd in sorted(updates, key=lambda u: u.get("update_id", 0)):
        cq = upd.get("callback_query")
        if not cq:
            continue
        seen_callbacks += 1
        message = cq.get("message") or {}          # absent for inline-mode callbacks
        chat = message.get("chat") or {}
        sender = cq.get("from") or {}
        # Telegram sends ints, the env var is a str: compare as strings or
        # 123 == "123" is False and NO tap ever matches — a silent dead bot.
        want = str(chat_id).strip()
        if str(chat.get("id")).strip() != want:
            continue
        # Also check who tapped: free in a private chat (from.id == chat.id),
        # but stops any group member approving if the chat id is ever a group.
        if str(sender.get("id")).strip() != want:
            continue
        matched_chat += 1
        if message.get("message_id") != message_id:
            continue
        status = DECISIONS.get(cq.get("data"))
        if status is None:
            continue
        matches.append((status, upd.get("update_id"), cq))
    log.info("scan: %d updates, %d callbacks, %d from this chat, %d matched draft",
             len(updates), seen_callbacks, matched_chat, len(matches))
    if not matches:
        return None
    rejects = [m for m in matches if m[0] == "rejected"]
    return rejects[0] if rejects else matches[0]


def _confirm(token, chat_id, message_id, cq, status, skip_message=False):
    """Best-effort Telegram feedback. Cannot affect the exit code and must
    never undo the recorded decision — the state file is the source of truth.

    # STEP [10] skip_message=True (approve path) omits the trailing sendMessage:
    # _announce_outcome sends the real outcome ("Posted" / "posting failed")
    # after the post resolves, so the static "will post" line would be wrong or
    # redundant. Reject path still sends its label here (unchanged)."""
    label = "✅ Approved — posting…" if status == "approved" else "❌ Rejected — discarded"
    # quiet=True on answerCallbackQuery: callback ids expire seconds after the
    # tap, so on the cron path it fails essentially always ("query is too old").
    # Expected, not a bug — warning-level noise there would train us to ignore
    # this script's logs. Button removal + confirmation never expire.
    calls = [
        ("answerCallbackQuery",
         {"callback_query_id": cq.get("id"), "text": label}, True),
        # Explicit empty keyboard removes the buttons; removal-by-omission is
        # not documented, so don't rely on it.
        ("editMessageReplyMarkup",
         {"chat_id": chat_id, "message_id": message_id,
          "reply_markup": {"inline_keyboard": []}}, False),
    ]
    if not skip_message:                                               # STEP [10]
        # No reply_to_message_id: that 400s if the message was deleted.
        calls.append(("sendMessage", {"chat_id": chat_id, "text": label}, False))
    # Guarded INDIVIDUALLY: the decision is already saved, so no failure here
    # may crash the run — and one failing call must not skip the others (the
    # button removal is what Harvey actually sees).
    for method, payload, quiet in calls:
        try:
            telegram_api.api_call(method, payload, token, quiet=quiet)
        except Exception as exc:  # noqa: BLE001 — the decision stands regardless
            log.warning("confirm: %s raised (%s: %s) — decision already recorded",
                        method, type(exc).__name__, exc)


def _post_approved_draft():                                            # STEP [10]
    """Re-read the state file, assert it is STILL 'approved', post to LinkedIn,
    and persist the outcome before returning. Returns (ok, post_id, error).

    # STEP [10] The re-read is the ordering-hazard guard made mechanical: we
    # STEP [10] NEVER post against a status read earlier in the run — only
    # STEP [10] against a fresh disk read taken in the same moment as the post.
    # STEP [10] If the file changed under us, we refuse and mark post_failed.
    # STEP [10] 'posted' is terminal on success; 'post_failed' is terminal on
    # STEP [10] failure — a later run may retry ONLY if this save also failed
    # STEP [10] (status then stays 'approved'). One attempt per run, no loop."""
    fresh = _load_state()
    if fresh is None or fresh is _MISSING:
        log.error("post: state file vanished/broke after approval — refusing to post")
        return False, None, "state file unreadable after approval"

    if fresh.get("status") != "approved":
        bad = fresh.get("status")
        log.error("post: re-read status=%s, expected 'approved' — refusing to post", bad)
        fresh["status"] = "post_failed"
        fresh["post_error"] = f"re-read status was {bad!r}, refused to post"
        _save_state(fresh)
        return False, None, fresh["post_error"]

    draft = fresh.get("draft")
    if not isinstance(draft, str) or not draft.strip():
        log.error("post: approved draft is empty/non-str — cannot post")
        fresh["status"] = "post_failed"
        fresh["post_error"] = "approved draft text is empty"
        _save_state(fresh)
        return False, None, fresh["post_error"]

    ok, result = post_linkedin.post(draft)
    if ok:
        # STEP [10] SUCCESS: persist 'posted' + ids BEFORE anything else can
        # STEP [10] run. A post must never be publishable twice — 'posted' is
        # STEP [10] terminal in run()'s status checks.
        fresh["status"] = "posted"
        fresh["posted_utc"] = datetime.now(timezone.utc).isoformat()
        fresh["linkedin_post_id"] = result
        if not _save_state(fresh):
            # The post IS live on LinkedIn but we couldn't record it. Return
            # ok=True so we don't go red (the post succeeded); the id still
            # carries to the Telegram announce. Harvey must record it by hand.
            log.error("post: PUBLISHED but could not save state (id=%s) — the "
                      "post is live; record this id manually", result)
        return True, result, None

    # STEP [10] FAILURE: record post_failed (terminal, no retry this run) and
    # STEP [10] let run() exit non-zero so the red workflow alerts Harvey.
    fresh["status"] = "post_failed"
    fresh["post_error"] = (result or "")[:200]
    _save_state(fresh)
    log.error("post: failed — %s", fresh["post_error"])
    return False, None, result


def _announce_outcome(token, chat_id, ok, post_id, error):             # STEP [10]
    """Best-effort Telegram message with the post result. Cannot affect the
    exit code — the state file is the source of truth. Never raises.

    # STEP [10] Success carries the post URL (so Harvey can verify it live) and,
    # STEP [10] if the token is past its warn age, a re-auth nudge. The URL is
    # STEP [10] Telegram-only — it never enters the LinkedIn post body."""
    if ok:
        text = "✅ Posted to LinkedIn"
        if post_id and not str(post_id).startswith("DRY_RUN_"):
            text += f"\nhttps://www.linkedin.com/feed/update/{post_id}/"
        try:
            nudge = token_status.warn_if_stale()
        except Exception as exc:  # noqa: BLE001 — never block the announce
            log.warning("announce: token_status raised (%s) — skipping nudge", exc)
            nudge = None
        if nudge:
            text += "\n" + nudge
    else:
        text = "⚠ Approved but posting failed — see logs"
        if error:
            text += f"\n{str(error)[:200]}"
    try:
        telegram_api.api_call("sendMessage", {"chat_id": chat_id, "text": text}, token)
    except Exception as exc:  # noqa: BLE001 — outcome is already recorded
        log.warning("announce: sendMessage raised (%s) — outcome already recorded", exc)


def run():
    state = _load_state()
    if state is _MISSING:
        log.info("run: no %s — nothing to approve", config.PENDING_POST_FILE)
        return 0
    if state is None:
        return 1

    status = state.get("status")

    # STEP [10] RETRY PATH: a prior run recorded 'approved' but crashed before
    # STEP [10] posting (or its post_failed-save also failed). Status is still
    # STEP [10] 'approved' → re-attempt the post WITHOUT rescanning Telegram,
    # STEP [10] since the decision is already on disk. This makes the "a later
    # STEP [10] run may retry only if status is still approved" contract real.
    # STEP [10] The re-read inside _post_approved_draft is the freshness guard.
    if status == "approved":
        token, chat_id = telegram_api.credentials()
        if not token:
            return 1
        log.info("run: status=approved — retrying the post from a prior run")
        ok, post_id, err = _post_approved_draft()
        _announce_outcome(token, chat_id, ok, post_id, err)
        return 0 if ok else 1

    if status != "awaiting_approval":
        log.info("run: status=%s — nothing to do", status)
        return 0

    message_id = state.get("telegram_message_id")
    if not isinstance(message_id, int):
        # main.py never writes awaiting_approval without an int message id.
        log.error("run: awaiting_approval with a non-int telegram_message_id "
                  "(%r) — corrupt state", message_id)
        return 1

    if _is_expired(state.get("created_utc")):
        log.warning("run: draft older than %dh — expiring it unapproved",
                    config.APPROVAL_EXPIRY_H)
        state["status"] = "expired"
        return 0 if _save_state(state) else 1

    token, chat_id = telegram_api.credentials()
    if not token:
        return 1

    updates = telegram_api.api_call(
        "getUpdates",
        {"offset": -config.TELEGRAM_UPDATE_LIMIT,
         "limit": config.TELEGRAM_UPDATE_LIMIT,
         "timeout": 0,
         "allowed_updates": ["callback_query"]},
        token)
    if updates is None:
        # Transient must never become terminal: write nothing, retry next run.
        log.error("run: could not reach Telegram — leaving the draft pending")
        return 0

    decision = _find_decision(updates, chat_id, message_id)
    if decision is None:
        log.info("run: no decision yet")
        return 0

    new_status, update_id, cq = decision
    state["status"] = new_status
    state["decided_utc"] = datetime.now(timezone.utc).isoformat()
    # ponytail: record the update id ONLY. The raw callback carries from.id and
    # username, and this file is committed — never widen this to the whole cq.
    state["callback_update_id"] = update_id
    if not _save_state(state):
        return 1
    log.info("run: decision recorded — %s (update_id=%s)", new_status, update_id)

    if new_status == "approved":
        # STEP [10] Post HERE, atomically with the approval — not in a later
        # STEP [10] run. This closes the supersede window: main.py can't
        # STEP [10] clobber an approved-but-unposted draft because approval and
        # STEP [10] posting are now one run. Strip buttons + ack the tap FIRST
        # STEP [10] (immediate feedback, no double-tap); the outcome message
        # STEP [10] from _announce_outcome follows after the post resolves.
        _confirm(token, chat_id, message_id, cq, new_status, skip_message=True)
        ok, post_id, err = _post_approved_draft()
        _announce_outcome(token, chat_id, ok, post_id, err)
        return 0 if ok else 1   # post_failed → red run (alerting)

    # Reject path: unchanged.
    _confirm(token, chat_id, message_id, cq, new_status)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(run())
