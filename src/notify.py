"""
notify.py — Run A of the two-run Telegram approval flow (Phase 2, Task 5).

send_draft(text, warnings, image_url=None) sends the generated draft to
Harvey's Telegram chat with inline ✅ Approve / ❌ Reject buttons (callback_data
"approve" / "reject"). Run B (Task 6, a separate workflow run) reads the tapped
button via getUpdates and acts on it. This module NEVER posts to LinkedIn and
has no auto-approve or timeout path (hard constraint #4).

# STEP [17] If image_url is provided, a sendPhoto is sent FIRST (best-effort,
# STEP [17] rendered inline) so Harvey can preview the image that will accompany
# STEP [17] the LinkedIn post. The text message with buttons always follows as a
# STEP [17] separate message; its message_id is the one returned (and stored in
# STEP [17] pending_post.json) so approve.py's callback match is unaffected. If
# STEP [17] Telegram can't fetch the image URL, it falls back to showing the URL
# STEP [17] as a tappable link inside the text message.

- Secrets: bot token and chat id come from the env vars named in config
  (TELEGRAM_TOKEN_ENV / TELEGRAM_CHAT_ID_ENV); values are never logged.
  Exception text is scrubbed before logging because requests errors embed
  the request URL — which contains the token.
- Fault isolation: any failure returns (False, None) — Telegram being down
  must not crash the run. main.py then marks pending_post.json as
  "notify_failed" so Run B cannot treat an unreviewed draft as approvable.
- Telegram caps messages at 4096 chars: only the *preview* is truncated;
  pending_post.json always holds the full draft.
"""

import logging
import os

import requests

import config

log = logging.getLogger(__name__)

_KEYBOARD = {"inline_keyboard": [[
    {"text": "✅ Approve", "callback_data": "approve"},
    {"text": "❌ Reject", "callback_data": "reject"},
]]}
_TRUNC_NOTE = "\n…[preview truncated — full draft is in pending_post.json]"


def _build_message(text, warnings, image_url=None):
    """Title + warnings + optional image URL + separator + draft, capped."""
    header = "\U0001f916 Daily digest draft — approve to send to LinkedIn\n"
    if warnings:
        header += "\n⚠ checks: " + "; ".join(warnings) + "\n"
    if image_url:                                                          # STEP [17]
        header += f"\n📷 Image: {image_url}\n"                              # STEP [17]
    header += "─" * 20 + "\n"
    # ponytail: Telegram counts UTF-16 code units, len() counts code points —
    # emoji count double there, so keep a flat 100-char safety margin.
    budget = config.TELEGRAM_MSG_LIMIT - 100 - len(header)
    if len(text) > budget:
        text = text[: budget - len(_TRUNC_NOTE)] + _TRUNC_NOTE
    return header + text


def _send_photo(image_url, token, chat_id):                                # STEP [17]
    """Download the image and send it as a multipart sendPhoto so Harvey sees
    it rendered inline BEFORE the draft text. Returns True on success, False on
    any failure (the caller then includes the URL in the text as a tappable
    fallback). Never raises; scrubs the token from errors.

    # STEP [17] We download with our OWN User-Agent and upload the bytes, rather
    # STEP [17] than passing the URL to Telegram, because Telegram's server-side
    # STEP [17] fetcher is blocked by some CDNs (notably Reddit's
    # STEP [17] external-preview.redd.it → 'failed to get HTTP URL content').
    # STEP [17] A photo sent by the bot has from.id = bot id, so approve.py's
    # STEP [17] _find_override_photo (which requires from.id == Harvey's chat id)
    # STEP [17] correctly ignores it — no conflict with the STEP [11] override."""
    try:
        img_resp = requests.get(image_url,
                                headers={"User-Agent": config.USER_AGENT},
                                timeout=config.TIMEOUT)                  # STEP [17]
        if img_resp.status_code != 200 or not img_resp.content:           # STEP [17]
            log.warning("send_photo: download HTTP %s (%d bytes) — URL in text",
                        img_resp.status_code, len(img_resp.content))     # STEP [17]
            return False                                                # STEP [17]
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat_id,
                  "caption": "📷 Image that will be attached to the LinkedIn post:"},
            files={"photo": img_resp.content},                           # STEP [17]
            timeout=config.TIMEOUT,
        )
        payload = resp.json()
        if payload.get("ok"):
            log.info("send_photo: image preview sent (%d bytes from %s)",
                     len(img_resp.content), image_url[:60])              # STEP [17]
            return True                                                 # STEP [17]
        log.warning("send_photo: sendPhoto rejected — %s — URL in text",
                    payload.get("description", "no description"))        # STEP [17]
        return False                                                    # STEP [17]
    except Exception as exc:  # noqa: BLE001 — image preview must not crash the send
        log.warning("send_photo: %s: %s — URL will appear in text instead",
                    type(exc).__name__, str(exc).replace(token, "<token>"))
        return False                                                    # STEP [17]


def send_draft(text, warnings, image_url=None):
    """Send the draft for approval. Returns (ok, telegram_message_id).
    Never raises; never logs the token or chat id.

    # STEP [17] If image_url is provided, sends a sendPhoto preview first (best
    # STEP [17] effort). The returned message_id is ALWAYS the text message
    # STEP [17] carrying the ✅/❌ buttons — never the photo's — so approve.py's
    # STEP [17] callback message_id match is unaffected. If sendPhoto fails, the
    # STEP [17] image URL is included in the text as a tappable fallback."""
    token = os.environ.get(config.TELEGRAM_TOKEN_ENV)
    chat_id = os.environ.get(config.TELEGRAM_CHAT_ID_ENV)
    if not token or not chat_id:
        log.error("send_draft: %s / %s not set — cannot send the draft",
                  config.TELEGRAM_TOKEN_ENV, config.TELEGRAM_CHAT_ID_ENV)
        return False, None

    # STEP [17] Best-effort image preview before the text. If it fails, the URL
    # STEP [17] is shown in the text instead so Harvey can still see/tap it.
    photo_sent = False                                                     # STEP [17]
    if image_url:                                                          # STEP [17]
        photo_sent = _send_photo(image_url, token, chat_id)                # STEP [17]
    image_url_in_text = image_url if (image_url and not photo_sent) else None  # STEP [17]

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id,
                  "text": _build_message(text, warnings, image_url_in_text),  # STEP [17]
                  "reply_markup": _KEYBOARD},
            timeout=config.TIMEOUT,
        )
        payload = resp.json()
        if not payload.get("ok"):
            log.error("send_draft: Telegram API error (HTTP %s): %s",
                      resp.status_code,
                      payload.get("description", "no description"))
            return False, None
        message_id = payload["result"]["message_id"]
        log.info("send_draft: draft sent for approval (message_id=%s, image=%s)",
                 message_id, "photo" if photo_sent else
                 ("url-in-text" if image_url_in_text else "none"))         # STEP [17]
        return True, message_id
    except Exception as exc:  # noqa: BLE001 — Telegram down must not crash the run
        # requests exceptions embed the URL, which contains the bot token.
        log.error("send_draft: %s: %s", type(exc).__name__,
                  str(exc).replace(token, "<token>"))
        return False, None


if __name__ == "__main__":
    # Manual test: send a dummy draft (requires both env vars in .env).
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ok, mid = send_draft("Test draft from notify.py — ignore.",
                         ["this is a test warning"])
    print(f"ok={ok} message_id={mid}")
