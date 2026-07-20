"""
telegram_api.py — shared Telegram Bot API plumbing (Phase 2, Task 6).

Exists so approve.py's four API calls (getUpdates, answerCallbackQuery,
editMessageReplyMarkup, sendMessage) share ONE token-scrubbing error path
instead of four copies of the same try/except.

Secrets (hard constraint #3):
- Token and chat id come from the env vars *named* in config; values are
  never logged.
- requests exceptions embed the request URL, which contains the bot token —
  every exception string is scrubbed before it reaches a log.
- Only `description` is logged from an error payload, never resp.text or
  resp.url: a getUpdates response body carries the chat id, Harvey's
  username, and the full draft.

Fault isolation: api_call never raises and never blocks the caller — it
returns None on any failure so the poller can log, skip, and continue.

notify.py deliberately keeps its own inline scrub (Task 6 scope guard says
don't touch it); migrating it here is a one-line follow-up if wanted.
"""

import logging
import os

import requests

import config

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


def credentials():
    """(token, chat_id) from env, or (None, None) if either is missing.
    Logs the env var NAMES only — never the values."""
    token = os.environ.get(config.TELEGRAM_TOKEN_ENV)
    chat_id = os.environ.get(config.TELEGRAM_CHAT_ID_ENV)
    if not token or not chat_id:
        log.error("credentials: %s / %s not set",
                  config.TELEGRAM_TOKEN_ENV, config.TELEGRAM_CHAT_ID_ENV)
        return None, None
    return token, chat_id


def api_call(method, payload, token, quiet=False):
    """POST one Bot API method. Returns the parsed `result` on success, else
    None. Never raises; never lets the token reach a log.

    quiet=True logs failures at debug instead of warning — for calls that are
    EXPECTED to fail routinely (answerCallbackQuery on the cron path), so a
    normal successful approval doesn't emit a scary warning every time."""
    fail = log.debug if quiet else log.warning
    try:
        resp = requests.post(f"{API_BASE}/bot{token}/{method}",
                             json=payload, timeout=config.TIMEOUT)
        body = resp.json()
        if not body.get("ok"):
            # Only `description` — the full body contains chat id and draft.
            fail("%s: Telegram API error (HTTP %s): %s", method,
                 resp.status_code, body.get("description", "no description"))
            return None
        return body.get("result")
    except Exception as exc:  # noqa: BLE001 — Telegram down must not crash the run
        fail("%s: %s: %s", method, type(exc).__name__,
             str(exc).replace(token, "<token>"))
        return None
