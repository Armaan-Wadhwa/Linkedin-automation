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
- STEP [18] STANDARD getUpdates pattern: read with NO offset (returns all
  unconfirmed updates, oldest-first), process, then CONFIRM with
  offset=max_update_id+1. This is the canonical Telegram bot approach.
  Previously used a non-standard negative offset (offset=-100) which "reads
  the newest 100 and forgets older ones" — the "forgetting" silently
  confirmed/deleted Harvey's taps between polls, causing the approve button
  to mysteriously fail on cron while working on manual trigger.
- Confirming after read is SAFE: any decision found is saved to
  pending_post.json (the source of truth) BEFORE the confirm call. If the
  post fails later, the decision is already recorded — the tap is no longer
  needed (the status="approved" retry path re-posts without getUpdates).
- `allowed_updates=["callback_query","message"]` so BOTH button taps and photo
  overrides are visible. This setting persists bot-globally.
- A tap can only be matched while the draft is fresh (APPROVAL_EXPIRY_H).

Exit codes: 0 = nothing to do, or a decision was recorded. 1 = genuinely
broken state (unreadable/corrupt state file, or a failed state save).
"""

import json
import logging
import os
import sys
import tempfile                                                     # STEP [11]
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
    # STEP [16] Make the two silent "no decision" failure modes LOUD. Previously
    # STEP [16] these only logged at INFO and the run exited 0, so a wrong
    # STEP [16] TELEGRAM_CHAT_ID secret (or a stale-button tap) looked identical
    # STEP [16] to "Harvey hasn't tapped yet" — the bot went dead for days with
    # STEP [16] no warning. Now both surface at WARNING so they're visible in the
    # STEP [16] Actions log. No secret value is logged — only its length, enough
    # STEP [16] to spot a malformed/stale value vs the real one.
    if seen_callbacks and matched_chat == 0:                            # STEP [16]
        log.warning("scan: %d button tap(s) seen but NONE matched the " # STEP [16]
                    "configured TELEGRAM_CHAT_ID (len=%d) — the GitHub " # STEP [16]
                    "secret likely differs from the tapper's chat id. "  # STEP [16]
                    "Tap(s) came from a different chat than expected.",  # STEP [16]
                    seen_callbacks, len(str(chat_id)))                  # STEP [16]
    elif matched_chat and not matches:                                  # STEP [16]
        log.warning("scan: %d tap(s) from the right chat but NONE matched" # STEP [16]
                    " message_id=%s — Harvey may be tapping a stale/"    # STEP [16]
                    " superseded draft's button (expected the latest).", # STEP [16]
                    matched_chat, message_id)                           # STEP [16]
    if not matches:
        return None
    rejects = [m for m in matches if m[0] == "rejected"]
    return rejects[0] if rejects else matches[0]


def _confirm_updates(updates, token):                                 # STEP [18]
    """Confirm all read updates so the getUpdates queue stays clean. Calls
    getUpdates with offset=max_update_id+1 — the standard Telegram pattern.
    # STEP [18] Without this, old non-matching callbacks accumulate and clog
    # STEP [18] the limit window. Non-fatal: a failure here just means the
    # STEP [18] queue isn't cleaned this cycle (next poll retries). Never raises."""
    try:
        max_uid = max(u.get("update_id", 0) for u in updates)          # STEP [18]
        telegram_api.api_call("getUpdates",                            # STEP [18]
            {"offset": max_uid + 1, "limit": 1, "timeout": 0},         # STEP [18]
            token, quiet=True)                                         # STEP [18]
        log.debug("confirm_updates: confirmed up to update_id=%s", max_uid)  # STEP [18]
    except Exception as exc:  # noqa: BLE001                            # STEP [18]
        log.debug("confirm_updates: failed (%s) — non-fatal", exc)     # STEP [18]


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
    and persist the outcome before returning.

    # STEP [10] The re-read is the ordering-hazard guard made mechanical: we
    # STEP [10] NEVER post against a status read earlier in the run — only
    # STEP [10] against a fresh disk read taken in the same moment as the post.
    # STEP [10] If the file changed under us, we refuse and mark post_failed.
    # STEP [10] 'posted' is terminal on success; 'post_failed' is terminal on
    # STEP [10] failure — a later run may retry ONLY if this save also failed
    # STEP [10] (status then stays 'approved'). One attempt per run, no loop.
    # STEP [12] Returns (ok, post_id, error, image_attempted, image_attached)."""
    fresh = _load_state()
    if fresh is None or fresh is _MISSING:
        log.error("post: state file vanished/broke after approval — refusing to post")
        return False, None, "state file unreadable after approval", False, False

    if fresh.get("status") != "approved":
        bad = fresh.get("status")
        log.error("post: re-read status=%s, expected 'approved' — refusing to post", bad)
        fresh["status"] = "post_failed"
        fresh["post_error"] = f"re-read status was {bad!r}, refused to post"
        _save_state(fresh)
        return False, None, fresh["post_error"], False, False

    draft = fresh.get("draft")
    if not isinstance(draft, str) or not draft.strip():
        log.error("post: approved draft is empty/non-str — cannot post")
        fresh["status"] = "post_failed"
        fresh["post_error"] = "approved draft text is empty"
        _save_state(fresh)
        return False, None, fresh["post_error"], False, False

    # STEP [12] Build image_ref from the re-read state (not the earlier copy).
    image_url = fresh.get("image_url")
    image_source = fresh.get("image_source")
    image_ref = None
    if image_url and image_source:                                     # STEP [12]
        image_ref = {"url": image_url, "source": image_source}

    ok, result, image_attached = post_linkedin.post(draft, image_ref)  # STEP [12]
    image_attempted = bool(image_ref)                                  # STEP [12]
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
        return True, result, None, image_attempted, image_attached

    # STEP [10] FAILURE: record post_failed (terminal, no retry this run) and
    # STEP [10] let run() exit non-zero so the red workflow alerts Harvey.
    fresh["status"] = "post_failed"
    fresh["post_error"] = (result or "")[:200]
    _save_state(fresh)
    log.error("post: failed — %s", fresh["post_error"])
    return False, None, result, False, False


def _announce_outcome(token, chat_id, ok, post_id, error,              # STEP [12]
                      image_attempted=False, image_attached=False):
    """Best-effort Telegram message with the post result. Cannot affect the
    exit code — the state file is the source of truth. Never raises.

    # STEP [10] Success carries the post URL (so Harvey can verify it live) and,
    # STEP [10] if the token is past its warn age, a re-auth nudge. The URL is
    # STEP [10] Telegram-only — it never enters the LinkedIn post body.
    # STEP [12] Image status is mentioned ONLY when an image was attempted, so
    # STEP [12] a normal no-image post reads exactly as before."""
    if ok:
        text = "✅ Posted to LinkedIn"
        if image_attempted and image_attached:                         # STEP [12]
            text += " (with image)"
        elif image_attempted and not image_attached:                   # STEP [12]
            text += " (text-only — image unavailable)"
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


def _find_override_photo(updates, chat_id, created_utc):              # STEP [11]
    """Scan message updates for a photo from Harvey sent AFTER the draft was
    created. Returns (file_id, update_id) of the most recent qualifying photo,
    or None. A photo is NOT an approval — approval still needs the ✅ button.

    # STEP [11] Same auth rule as _find_decision: chat id AND sender id must
    # STEP [11] both match. A photo sent before the draft existed is ignored
    # STEP [11] (it can't be a reply to today's draft). Never raises."""
    if not created_utc:
        return None
    try:
        created = datetime.fromisoformat(created_utc)
    except (TypeError, ValueError):
        return None
    if created.tzinfo is None:
        return None   # unverifiable age → fail safe (don't accept stale photos)

    want = str(chat_id).strip()
    best = None   # (msg_date, file_id, update_id)
    for upd in updates:
        msg = upd.get("message")
        if not msg or not isinstance(msg, dict):
            continue
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        if str(chat.get("id")).strip() != want:
            continue
        if str(sender.get("id")).strip() != want:
            continue
        photos = msg.get("photo")
        if not photos or not isinstance(photos, list):
            continue
        msg_date = msg.get("date")
        if not isinstance(msg_date, int):
            continue
        try:
            sent = datetime.fromtimestamp(msg_date, tz=timezone.utc)
        except (OverflowError, ValueError, OSError):
            continue
        if sent < created:
            continue   # photo predates the draft — not an override for today
        # Largest photo = highest-resolution entry (Telegram sends S→L).
        largest = max(photos, key=lambda p: p.get("width", 0) if isinstance(p, dict) else 0)
        file_id = largest.get("file_id") if isinstance(largest, dict) else None
        if not file_id:
            continue
        if best is None or msg_date > best[0]:
            best = (msg_date, file_id, upd.get("update_id"))
    return (best[1], best[2]) if best else None


def _download_override_photo(token, file_id):                         # STEP [11]
    """Download a Telegram photo by file_id to a temp file. Returns the local
    path, or None on any failure. Never raises; scrubs the token from errors.

    # STEP [11] getFile → file_path → download_file → save to tempfile.
    # STEP [11] Size-checked against IMAGE_MAX_BYTES so a huge photo is rejected
    # STEP [11] early (keeps 8b's upload within LinkedIn's cap). Non-fatal: a
    # STEP [11] None return means the caller keeps the story image or text-only."""
    result = telegram_api.api_call("getFile", {"file_id": file_id}, token)
    if not result or not isinstance(result, dict):
        log.warning("photo: getFile returned no result — download skipped")
        return None
    file_path = result.get("file_path")
    if not file_path:
        log.warning("photo: getFile returned no file_path — download skipped")
        return None
    file_size = result.get("file_size") or 0
    if file_size and file_size > config.IMAGE_MAX_BYTES:
        log.warning("photo: %d bytes > %d cap — skipping override",
                    file_size, config.IMAGE_MAX_BYTES)
        return None
    data = telegram_api.download_file(file_path, token)
    if not data:
        log.warning("photo: download returned no bytes — override skipped")
        return None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".jpg",
                                        prefix=config.IMAGE_TEMP_PREFIX)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        log.warning("photo: could not save temp file (%s) — override skipped", exc)
        return None
    log.info("photo: override saved → %s (%d bytes)", tmp_path, len(data))
    return tmp_path


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
        ok, post_id, err, img_att, img_ok = _post_approved_draft()      # STEP [12]
        _announce_outcome(token, chat_id, ok, post_id, err, img_att, img_ok)  # STEP [12]
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
        {"limit": config.TELEGRAM_UPDATE_LIMIT,                       # STEP [18] no offset
         "timeout": 0,
         "allowed_updates": ["callback_query", "message"]},           # STEP [11]
        token)
    if updates is None:
        # Transient must never become terminal: write nothing, retry next run.
        log.error("run: could not reach Telegram — leaving the draft pending")
        return 0

    # STEP [11] Photo override: scan BEFORE the decision scan. A photo is NOT an
    # STEP [11] approval (✅ still required), but if found it beats the story
    # STEP [11] image. Saved even when no ✅ exists yet — Harvey may send the
    # STEP [11] photo first, tap later. A download failure is non-fatal: the
    # STEP [11] story image (if any) is preserved and the flow continues.
    photo = _find_override_photo(updates, chat_id, state.get("created_utc"))
    if photo:
        file_id, _ = photo
        image_path = _download_override_photo(token, file_id)
        if image_path:
            state["image_url"] = image_path
            state["image_source"] = "telegram_override"
            state["image_file_id"] = file_id
            if not _save_state(state):
                log.warning("photo: override downloaded but state save failed "
                            "— may not persist to the next run")

    decision = _find_decision(updates, chat_id, message_id)
    if decision is None:
        _confirm_updates(updates, token)                               # STEP [18] clean queue
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
    _confirm_updates(updates, token)                                   # STEP [18] decision saved → safe to clean queue
    log.info("run: decision recorded — %s (update_id=%s)", new_status, update_id)

    if new_status == "approved":
        # STEP [10] Post HERE, atomically with the approval — not in a later
        # STEP [10] run. This closes the supersede window: main.py can't
        # STEP [10] clobber an approved-but-unposted draft because approval and
        # STEP [10] posting are now one run. Strip buttons + ack the tap FIRST
        # STEP [10] (immediate feedback, no double-tap); the outcome message
        # STEP [10] from _announce_outcome follows after the post resolves.
        _confirm(token, chat_id, message_id, cq, new_status, skip_message=True)
        ok, post_id, err, img_att, img_ok = _post_approved_draft()      # STEP [12]
        _announce_outcome(token, chat_id, ok, post_id, err, img_att, img_ok)  # STEP [12]
        return 0 if ok else 1   # post_failed → red run (alerting)

    # Reject path: unchanged.
    _confirm(token, chat_id, message_id, cq, new_status)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(run())
