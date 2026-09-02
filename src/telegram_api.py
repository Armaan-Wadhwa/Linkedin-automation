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
import retryutil   # STEP [26] shared backoff delay + sleep seam

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


def api_call(method, payload, token, quiet=False, http_timeout=None):   # STEP [32]
    """POST one Bot API method. Returns the parsed `result` on success, else
    None. Never raises; never lets the token reach a log.

    # STEP [26] Retries FREELY on transient errors: a duplicate Telegram message
    # STEP [26] is harmless noise, a missed draft/decision is a lost day. Retries
    # STEP [26] on network errors / malformed JSON / 5xx / unknown error_codes;
    # STEP [26] honors a 429 `parameters.retry_after`; fails fast (no retry) on
    # STEP [26] 400/401 (bad token / chat id = permanent). Constants in config;
    # STEP [26] retryutil.sleep is the test-stubbed seam (no real sleeping).
    #
    # STEP [32] http_timeout overrides the per-request read timeout, for the ONE
    # STEP [32] caller that long-polls (approve.py's getUpdates). Default None =
    # STEP [32] config.TIMEOUT, so every existing call site is unchanged. A 50s
    # STEP [32] server-side wait under a 20s client timeout would fail every poll.
    #
    # quiet=True logs failures at debug instead of warning — for calls that are
    # EXPECTED to fail routinely (answerCallbackQuery on the cron path), so a
    # normal successful approval doesn't emit a scary warning every time."""
    fail = log.debug if quiet else log.warning
    for attempt in range(1, config.TELEGRAM_MAX_ATTEMPTS + 1):            # STEP [26]
        try:
            resp = requests.post(f"{API_BASE}/bot{token}/{method}",
                                 json=payload,
                                 timeout=http_timeout or config.TIMEOUT)  # STEP [32]
            body = resp.json()
        except Exception as exc:  # noqa: BLE001 — network / malformed JSON -> retry
            scrubbed = str(exc).replace(token, "<token>")
            if attempt < config.TELEGRAM_MAX_ATTEMPTS:
                wait = retryutil.backoff_delay(
                    attempt, config.TELEGRAM_BACKOFF_BASE_S,
                    config.TELEGRAM_BACKOFF_MAX_S)
                log.debug("%s: attempt %d transient (%s: %s) — retry in %.1fs",
                          method, attempt, type(exc).__name__, scrubbed, wait)
                retryutil.sleep(wait)
                continue
            fail("%s: %s: %s", method, type(exc).__name__, scrubbed)
            return None

        if body.get("ok"):
            return body.get("result")

        # Not ok — inspect Telegram's error_code + parameters.retry_after. Only
        # `description` is logged: the body contains chat id and the full draft.
        err_code = body.get("error_code")
        params = body.get("parameters") or {}
        retry_after = params.get("retry_after")                            # STEP [26]
        desc = body.get("description", "no description")

        if err_code in (400, 401):                                         # STEP [26] permanent
            fail("%s: Telegram permanent error (HTTP %s): %s", method, err_code, desc)
            return None
        if attempt < config.TELEGRAM_MAX_ATTEMPTS:                         # STEP [26] transient -> retry
            if err_code == 429 and retry_after:                            # STEP [26] honor the server's delay
                wait = float(retry_after)
                log.debug("%s: 429 retry_after=%.1fs — sleeping", method, wait)
            else:
                wait = retryutil.backoff_delay(
                    attempt, config.TELEGRAM_BACKOFF_BASE_S,
                    config.TELEGRAM_BACKOFF_MAX_S)
                log.debug("%s: attempt %d error_code=%s (%s) — retry in %.1fs",
                          method, attempt, err_code, desc, wait)
            retryutil.sleep(wait)
            continue
        fail("%s: Telegram API error (HTTP %s): %s", method, resp.status_code, desc)
        return None
    return None  # STEP [26] exhausted (e.g. a 429 that never lets up)


def download_file(file_path, token):                                  # STEP [11]
    """Download a file from Telegram's file endpoint. Returns raw bytes, or
    None on any failure. Never raises; scrubs the token from exceptions.

    # STEP [11] Used by approve.py to fetch Harvey's override photo: getFile
    # STEP [11] returns a file_path, then this hits the /file/bot<token>/ route
    # STEP [11] (a raw GET, not a Bot API method — hence a separate helper)."""
    try:
        resp = requests.get(f"{API_BASE}/file/bot{token}/{file_path}",
                            timeout=config.TIMEOUT)
        if resp.status_code != 200:
            log.warning("download_file: HTTP %s — file not retrieved",
                        resp.status_code)
            return None
        return resp.content
    except Exception as exc:  # noqa: BLE001 — file endpoint down must not crash
        log.warning("download_file: %s: %s", type(exc).__name__,
                    str(exc).replace(token, "<token>"))
        return None
