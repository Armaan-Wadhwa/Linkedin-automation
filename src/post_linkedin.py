"""
post_linkedin.py — publish an approved draft to LinkedIn (Phase 2, Task 7).  # STEP [10]

THE single LinkedIn API seam for the whole project. post(text) returns
(ok, post_id_or_error) and NEVER raises — every failure path yields (False,
reason) so approve.py can record "post_failed" and go red without crashing.

Official API only (hard constraint #2): "Share on LinkedIn" product,
w_member_social scope, POST https://api.linkedin.com/rest/posts. No
cookie/session/scraping — account-ban risk, never.

Secrets (hard constraint #3): LINKEDIN_ACCESS_TOKEN + LINKEDIN_PERSON_URN
come from the env vars NAMED in config; values are never logged. requests
exceptions embed the Authorization header (the bearer token) — every
exception string is scrubbed with the same .replace(token, "<token>") pattern
used by telegram_api.py before it can reach a log or the state file.

Dry run: LINKEDIN_DRY_RUN=1 logs the exact request it WOULD send (token
scrubbed) and returns a fake success WITHOUT calling LinkedIn — used to prove
the approve→post path end-to-end with a real Telegram tap while posting
nothing live.
"""

import logging
import os
from datetime import datetime, timezone

import requests

import config   # STEP [10]

log = logging.getLogger(__name__)


def _scrub(text, token):
    """Strip the token from any string so it can't reach a log or state file.
    Mirrors telegram_api.py's pattern; safe to call on non-strings."""
    if not token or not isinstance(text, str):
        return text
    return text.replace(token, "<token>")


def post(text):
    """Publish `text` to the author's personal LinkedIn feed.

    Returns (ok: bool, post_id_or_error: str):
      - 201 success  → (True, x-restli-id from response headers)
      - dry-run      → (True, "DRY_RUN_<utc>")  — no API call
      - any failure  → (False, short scrubbed reason)

    Never raises. A non-str/empty text is treated as a programmer error and
    returns (False, reason) rather than sending an empty post upstream."""
    token = os.environ.get(config.LINKEDIN_TOKEN_ENV)
    person_urn = os.environ.get(config.LINKEDIN_PERSON_URN_ENV)
    if not token or not person_urn:
        # Log env-var NAMES only — never the values.
        log.error("post: %s / %s not set — cannot post to LinkedIn",
                  config.LINKEDIN_TOKEN_ENV, config.LINKEDIN_PERSON_URN_ENV)
        return False, f"{config.LINKEDIN_TOKEN_ENV} / {config.LINKEDIN_PERSON_URN_ENV} not set"

    if not isinstance(text, str) or not text.strip():
        log.error("post: draft text is empty/non-str — refusing to post nothing")
        return False, "empty draft text"

    body = {
        "author": person_urn,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": config.LINKEDIN_VERSION,
    }

    # STEP [10] Dry-run: log the exact request, fake a success, do NOT call.
    if os.environ.get(config.LINKEDIN_DRY_RUN_ENV) == "1":
        fake_id = f"DRY_RUN_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        log.info("post: LINKEDIN_DRY_RUN=1 — NOT calling LinkedIn. Would send:")
        log.info("  POST %s", config.LINKEDIN_POSTS_URL)
        log.info("  headers: %s", {k: (_scrub(v, token) if k == "Authorization" else v)
                                   for k, v in headers.items()})
        log.info("  body: %s", body)
        log.info("  → returning fake success id=%s", fake_id)
        return True, fake_id

    try:
        resp = requests.post(config.LINKEDIN_POSTS_URL, json=body,
                             headers=headers, timeout=config.TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — LinkedIn down must not crash the run
        reason = _scrub(f"{type(exc).__name__}: {exc}", token)
        log.error("post: request raised — %s", reason)
        return False, reason

    if resp.status_code == 201:
        post_id = resp.headers.get("x-restli-id")
        if not post_id:
            # 201 with no id is an API contract violation — treat as failure
            # rather than claim success with a None we'd persist as the id.
            log.error("post: 201 returned but x-restli-id header missing")
            return False, "201 OK but x-restli-id header absent"
        log.info("post: published — linkedin_post_id=%s (len=%d)",
                 post_id, len(text))
        return True, post_id

    # Non-201: capture a short reason. Only resp.text[:200] (could be large),
    # scrubbed of the token in case it echoes back in an error body.
    snippet = _scrub(resp.text[:200], token) if resp.text else ""
    reason = f"HTTP {resp.status_code}: {snippet}".strip()
    log.error("post: LinkedIn rejected the post — %s", reason)
    return False, reason


if __name__ == "__main__":
    # Manual smoke test. Set LINKEDIN_ACCESS_TOKEN / LINKEDIN_PERSON_URN in env
    # or .env. Use LINKEDIN_DRY_RUN=1 to avoid a real post.
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    draft = ("Test draft from post_linkedin.py — please ignore.\n\n"
             "#AI #Test")
    ok, result = post(draft)
    print(f"ok={ok} result={result}")
    sys.exit(0 if ok else 1)
