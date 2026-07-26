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
from datetime import datetime, timezone

import config   # STEP [10]

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
        days_left = 60 - age
        if days_left > 0:
            return (f"⚠ LinkedIn token is {age} days old — re-auth soon, "
                    f"expires in ~{days_left} day(s) (60-day limit, no refresh).")
        # Already past 60 days — token is almost certainly dead.
        return (f"⚠ LinkedIn token is {age} days old — it has PASSED the 60-day "
                f"limit; the next post will likely fail. Re-auth now.")
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    msg = warn_if_stale()
    print(msg if msg else f"token age OK or unknown (days_old={days_old()})")
