"""
token_status.py — LinkedIn access-token age nudge (Phase 2, Task 7).     # STEP [10]

The LinkedIn standard-app ("Share on LinkedIn" product) access token is a
60-day credential with NO refresh token (project gotcha). warn_if_stale()
returns a re-auth message once the token's age exceeds LINKEDIN_TOKEN_WARN_DAYS
(config, 50) so approve.py can append it to the success Telegram message and
Harvey re-auths before the 60-day wall.

Non-fatal by design: a missing or malformed issued-date stamp returns None
(can't prove age → don't warn, don't crash, don't block the post). This module
never touches the network and never sees the token value.

Run from anywhere — reads the env var NAME from config (set at OAuth time).
"""

import logging
import os
import sys                                                       # STEP [24]
from datetime import datetime, timezone

import requests                                                  # STEP [24] validity probe

import config   # STEP [10]
import telegram_api                                              # STEP [24] reuse the shared send seam

log = logging.getLogger(__name__)


def days_old():
    """Whole-day age of the LinkedIn token, or None if unprovable.

    None on: env var missing, malformed stamp, naive (tz-less) stamp. A naive
    stamp could shift the age by up to 14h depending on the writer's timezone —
    better to stay silent than warn at the wrong time. Never raises."""
    raw = os.environ.get(config.LINKEDIN_TOKEN_ISSUED_UTC_ENV)
    if not raw:
        log.debug("days_old: %s not set — no age check",
                  config.LINKEDIN_TOKEN_ISSUED_UTC_ENV)
        return None
    try:
        issued = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        log.warning("days_old: %s=%r is not a valid ISO timestamp — no age check",
                    config.LINKEDIN_TOKEN_ISSUED_UTC_ENV, raw)
        return None
    if issued.tzinfo is None:
        log.warning("days_old: %s stamp is timezone-naive — treating as unknown "
                    "(a naive stamp could miscount the age)", raw)
        return None
    return (datetime.now(timezone.utc) - issued).days


def warn_if_stale():
    """Return a re-auth nudge string if the token is past the warn threshold,
    else None. Non-fatal: any unprovable age → None (no warning, no crash)."""
    age = days_old()
    if age is None:
        return None
    if age > config.LINKEDIN_TOKEN_WARN_DAYS:
        life = config.LINKEDIN_TOKEN_LIFETIME_DAYS                    # STEP [24] was hardcoded 60
        days_left = life - age                                        # STEP [24]
        if days_left > 0:
            return (f"⚠ LinkedIn token is {age} days old — re-auth soon, "
                    f"expires in ~{days_left} day(s) ({life}-day limit, no refresh).")  # STEP [24]
        # Already past the lifetime — token is almost certainly dead.
        return (f"⚠ LinkedIn token is {age} days old — it has PASSED the {life}-day "  # STEP [24]
                f"limit; the next post will likely fail. Re-auth now.")
    return None


# ---------------------------------------------------------------------------
# STEP [24] Task 16 — standalone daily monitor (token_check.yml).
#
# Everything above is Phase 2 and is NOT changed in behaviour: approve.py
# appends warn_if_stale() to the "✅ Posted to LinkedIn" message, so it MUST
# keep returning None on an unprovable age — otherwise every successful post
# would carry a nudge. The monitor below deliberately takes the opposite
# stance (an unrecorded date is itself alertable) and therefore lives in its
# own functions rather than changing warn_if_stale().
# ---------------------------------------------------------------------------

PROBE_VALID = "valid"                                                 # STEP [24]
PROBE_INVALID = "invalid"                                             # STEP [24]
PROBE_INCONCLUSIVE = "inconclusive"                                   # STEP [24]

_ACTION = ("→ Re-authorize LinkedIn, then update BOTH GitHub secrets: "  # STEP [24]
           f"{config.LINKEDIN_TOKEN_ENV} and "                        # STEP [24]
           f"{config.LINKEDIN_TOKEN_ISSUED_UTC_ENV} "                 # STEP [24]
           "(tz-aware ISO stamp, e.g. 2026-07-26T00:00:00+00:00).")   # STEP [24]


def probe_token(token):                                               # STEP [24]
    """One read-only GET asking LinkedIn 'does this token work right now?'.

    Returns PROBE_VALID / PROBE_INVALID / PROBE_INCONCLUSIVE. Never raises and
    never posts anything. Logs the STATUS CODE only — never the token.

    Only 401 counts as dead. A 5xx, a network blip, or a 403 (token accepted
    but the scope doesn't cover userinfo) are all INCONCLUSIVE: a monitor that
    cries wolf on a transient error is worse than no monitor.

    Note: if the token env var is unset this is inconclusive, not 'dead' — the
    date/age signal is what alerts in that case."""
    if not token:                                                     # STEP [24]
        log.error("probe_token: %s not set — cannot verify the token",
                  config.LINKEDIN_TOKEN_ENV)                          # STEP [24]
        return PROBE_INCONCLUSIVE                                     # STEP [24]
    try:                                                              # STEP [24]
        resp = requests.get(config.LINKEDIN_USERINFO_URL,             # STEP [24]
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=config.TIMEOUT)                   # STEP [24]
        if resp.status_code == 200:                                   # STEP [24]
            log.info("probe_token: HTTP 200 — token is valid")        # STEP [24]
            return PROBE_VALID                                        # STEP [24]
        if resp.status_code == 401:                                   # STEP [24]
            log.warning("probe_token: HTTP 401 — token is no longer valid")
            return PROBE_INVALID                                      # STEP [24]
        log.warning("probe_token: HTTP %s — inconclusive, not alerting on it "
                    "(403 here means the token lacks the openid/profile scope)",
                    resp.status_code)                                 # STEP [24]
        return PROBE_INCONCLUSIVE                                     # STEP [24]
    except Exception as exc:  # noqa: BLE001 — a monitor must never crash
        # requests exceptions can embed the Authorization header.
        log.warning("probe_token: %s: %s — inconclusive", type(exc).__name__,
                    str(exc).replace(token, "<token>"))               # STEP [24]
        return PROBE_INCONCLUSIVE                                     # STEP [24]


def evaluate(age_days, probe):                                        # STEP [24]
    """PURE decision function — no I/O, no env, no network. This is the seam
    test_token_status.py exercises.

    Returns ONE combined message, or None when there is nothing worth saying.
    Silence is the healthy state: a daily 'all good' ping is noise.

    age_days is None when the age is unprovable (secret missing, malformed, or
    timezone-naive). That is NOT treated as fresh — a LinkedIn token is opaque
    and carries no date, so an unrecorded issue date means we genuinely cannot
    warn before the 60-day wall. That gap is the thing this task removes."""
    parts = []                                                        # STEP [24]

    if probe == PROBE_INVALID:                                        # STEP [24]
        parts.append("❌ LinkedIn says the access token is NO LONGER VALID "
                     "(HTTP 401). Posting will fail until you re-authorize.")

    if age_days is None:                                              # STEP [24]
        parts.append(
            f"❓ No usable issue date recorded, so the token's age cannot be "
            f"checked and you will get NO warning before it expires. "
            f"Set {config.LINKEDIN_TOKEN_ISSUED_UTC_ENV} to a timezone-aware "
            f"ISO stamp.")                                            # STEP [24]
    elif age_days > config.LINKEDIN_TOKEN_WARN_DAYS:                  # STEP [24]
        life = config.LINKEDIN_TOKEN_LIFETIME_DAYS                    # STEP [24]
        left = life - age_days                                        # STEP [24]
        if left > 0:                                                  # STEP [24]
            parts.append(f"⚠ LinkedIn token is {age_days} days old — "
                         f"{left} day(s) left of its {life}-day life "
                         f"(no refresh token exists).")               # STEP [24]
        else:                                                         # STEP [24]
            parts.append(f"⚠ LinkedIn token is {age_days} days old — PAST the "
                         f"{life}-day limit. It is almost certainly dead.")

    if not parts:                                                     # STEP [24]
        return None                                                   # STEP [24]
    return ("🔑 LinkedIn token check\n\n" + "\n\n".join(parts)
            + "\n\n" + _ACTION)                                       # STEP [24]


def run():                                                            # STEP [24]
    """Monitor entry point. Returns a process exit code.

    0 = nothing to report, or a problem was reported successfully.
    1 = there WAS something to report and the Telegram send failed — the
        monitor's own failure must be visible as a red run, otherwise this
        check becomes the next silent-death gap.

    A dead token that IS successfully reported stays green on purpose:
    Telegram is the alert channel, and a red run would double-alert."""
    age = days_old()                                                  # STEP [24]
    probe = probe_token(os.environ.get(config.LINKEDIN_TOKEN_ENV))    # STEP [24]
    log.info("run: age_days=%s probe=%s", age, probe)                 # STEP [24]

    message = evaluate(age, probe)                                    # STEP [24]
    if message is None:                                               # STEP [24]
        log.info("run: nothing to report — staying silent")           # STEP [24]
        return 0                                                      # STEP [24]

    tg_token, chat_id = telegram_api.credentials()                    # STEP [24] reuse the shared seam
    if not tg_token:                                                  # STEP [24]
        log.error("run: have something to report but Telegram is not "
                  "configured — failing loud")                        # STEP [24]
        return 1                                                      # STEP [24]
    # Plain sendMessage: NO reply_markup. notify.send_draft always attaches the
    # ✅/❌ approve buttons, which would be wrong (and confusing) here.
    sent = telegram_api.api_call("sendMessage",                       # STEP [24]
                                 {"chat_id": chat_id, "text": message},
                                 tg_token)                            # STEP [24]
    if sent is None:                                                  # STEP [24]
        log.error("run: token warning could NOT be delivered — failing loud")
        return 1                                                      # STEP [24]
    log.info("run: token warning delivered")                          # STEP [24]
    return 0                                                          # STEP [24]


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):                            # STEP [24]
        sys.stdout.reconfigure(encoding="utf-8")                      # STEP [24]
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(run())                                                   # STEP [24]
