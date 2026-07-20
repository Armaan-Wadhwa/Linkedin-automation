"""
approve.py — Run B of the two-run Telegram approval flow (Phase 2, Task 6).

Reads Harvey's ✅/❌ tap from Telegram and records it in pending_post.json.
This module is DECISION-ONLY: it never posts to LinkedIn (that is Task 7),
has no auto-approve path and no timeout-defaults-to-yes (hard constraint #4).
Every ambiguous or unverifiable case resolves to "not approvable".

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
import telegram_api   # noqa: E402

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


def _confirm(token, chat_id, message_id, cq, status):
    """Best-effort Telegram feedback. Cannot affect the exit code and must
    never undo the recorded decision — the state file is the source of truth."""
    label = "✅ Approved — will post" if status == "approved" else "❌ Rejected — discarded"
    # quiet=True on answerCallbackQuery: callback ids expire seconds after the
    # tap, so on the cron path it fails essentially always ("query is too old").
    # Expected, not a bug — warning-level noise there would train us to ignore
    # this script's logs. Button removal + confirmation never expire.
    calls = (
        ("answerCallbackQuery",
         {"callback_query_id": cq.get("id"), "text": label}, True),
        # Explicit empty keyboard removes the buttons; removal-by-omission is
        # not documented, so don't rely on it.
        ("editMessageReplyMarkup",
         {"chat_id": chat_id, "message_id": message_id,
          "reply_markup": {"inline_keyboard": []}}, False),
        # No reply_to_message_id: that 400s if the message was deleted.
        ("sendMessage", {"chat_id": chat_id, "text": label}, False),
    )
    # Guarded INDIVIDUALLY: the decision is already saved, so no failure here
    # may crash the run — and one failing call must not skip the others (the
    # button removal is what Harvey actually sees).
    for method, payload, quiet in calls:
        try:
            telegram_api.api_call(method, payload, token, quiet=quiet)
        except Exception as exc:  # noqa: BLE001 — the decision stands regardless
            log.warning("confirm: %s raised (%s: %s) — decision already recorded",
                        method, type(exc).__name__, exc)


def run():
    state = _load_state()
    if state is _MISSING:
        log.info("run: no %s — nothing to approve", config.PENDING_POST_FILE)
        return 0
    if state is None:
        return 1

    status = state.get("status")
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

    _confirm(token, chat_id, message_id, cq, new_status)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(run())
