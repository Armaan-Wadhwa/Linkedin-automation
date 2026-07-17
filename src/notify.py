"""
notify.py — Run A of the two-run Telegram approval flow (Phase 2, Task 5).

send_draft(text, warnings) sends the generated draft to Harvey's Telegram
chat with inline ✅ Approve / ❌ Reject buttons (callback_data "approve" /
"reject"). Run B (Task 6, a separate workflow run) reads the tapped button
via getUpdates and acts on it. This module NEVER posts to LinkedIn and has
no auto-approve or timeout path (hard constraint #4).

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


def _build_message(text, warnings):
    """Title + warnings + separator + draft, capped to the Telegram limit."""
    header = "\U0001f916 Daily digest draft — approve to send to LinkedIn\n"
    if warnings:
        header += "\n⚠ checks: " + "; ".join(warnings) + "\n"
    header += "─" * 20 + "\n"
    # ponytail: Telegram counts UTF-16 code units, len() counts code points —
    # emoji count double there, so keep a flat 100-char safety margin.
    budget = config.TELEGRAM_MSG_LIMIT - 100 - len(header)
    if len(text) > budget:
        text = text[: budget - len(_TRUNC_NOTE)] + _TRUNC_NOTE
    return header + text


def send_draft(text, warnings):
    """Send the draft for approval. Returns (ok, telegram_message_id).
    Never raises; never logs the token or chat id."""
    token = os.environ.get(config.TELEGRAM_TOKEN_ENV)
    chat_id = os.environ.get(config.TELEGRAM_CHAT_ID_ENV)
    if not token or not chat_id:
        log.error("send_draft: %s / %s not set — cannot send the draft",
                  config.TELEGRAM_TOKEN_ENV, config.TELEGRAM_CHAT_ID_ENV)
        return False, None
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id,
                  "text": _build_message(text, warnings),
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
        log.info("send_draft: draft sent for approval (message_id=%s)", message_id)
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
