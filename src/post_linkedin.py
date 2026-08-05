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
import retryutil   # STEP [26] shared backoff delay + sleep seam

log = logging.getLogger(__name__)


def _scrub(text, token):
    """Strip the token from any string so it can't reach a log or state file.
    Mirrors telegram_api.py's pattern; safe to call on non-strings."""
    if not token or not isinstance(text, str):
        return text
    return text.replace(token, "<token>")


# STEP [12] Image upload helpers — each returns None/False on ANY failure so
# STEP [12] post() can silently fall back to text-only. An image error must
# STEP [12] NEVER turn into a post_failed or block/delay the text post.

def _detect_content_type(data):                                       # STEP [12]
    """Sniff the image MIME type from magic bytes. Returns 'image/jpeg',
    'image/png', or None. No deps — just the file signatures."""
    if not data or len(data) < 4:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"\x89PNG":
        return "image/png"
    return None


def _get_image_bytes(image_ref):                                      # STEP [12]
    """Fetch image bytes from a remote URL (story) or local file (telegram
    override). Returns (data, content_type) or (None, None) on failure.
    Never raises."""
    url = image_ref.get("url") if isinstance(image_ref, dict) else None
    source = image_ref.get("source") if isinstance(image_ref, dict) else None
    if not url:
        return None, None
    try:
        if source == "telegram_override":
            with open(url, "rb") as fh:
                data = fh.read()
        else:
            resp = requests.get(url, timeout=config.TIMEOUT,
                                headers={"User-Agent": config.USER_AGENT})
            if resp.status_code != 200:
                log.warning("image: download HTTP %s — skipping", resp.status_code)
                return None, None
            data = resp.content
    except Exception as exc:  # noqa: BLE001 — image fetch must not crash the post
        log.warning("image: fetch failed (%s: %s)", type(exc).__name__, exc)
        return None, None
    ctype = _detect_content_type(data)
    if not ctype:
        log.warning("image: unrecognised type (not JPEG/PNG) — skipping")
        return None, None
    return data, ctype


def _initialize_upload(token, person_urn):                             # STEP [12]
    """Register an image upload with LinkedIn. Returns (upload_url, image_urn)
    or None. Never raises; scrubs the token from any error."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": config.LINKEDIN_VERSION,
    }
    body = {"initializeUploadRequest": {"owner": person_urn}}
    try:
        resp = requests.post(
            f"{config.LINKEDIN_IMAGES_URL}?action=initializeUpload",
            json=body, headers=headers, timeout=config.TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        log.warning("image: initializeUpload raised (%s: %s)",
                    type(exc).__name__, _scrub(str(exc), token))
        return None
    if resp.status_code != 200:
        snippet = _scrub(resp.text[:200], token) if resp.text else ""
        log.warning("image: initializeUpload HTTP %s — %s",
                    resp.status_code, snippet)
        return None
    try:
        value = resp.json().get("value", {})
        upload_url = value.get("uploadUrl")
        image_urn = value.get("image")
    except (ValueError, AttributeError):
        log.warning("image: initializeUpload returned malformed JSON")
        return None
    if not upload_url or not image_urn:
        log.warning("image: initializeUpload response missing uploadUrl/image")
        return None
    return upload_url, image_urn


def _upload_bytes(upload_url, token, data, content_type):              # STEP [12]
    """PUT image bytes to LinkedIn's upload URL. Returns True/False.
    Never raises; scrubs the token from any error."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": content_type}
    try:
        resp = requests.put(upload_url, data=data, headers=headers,
                           timeout=config.TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        log.warning("image: upload PUT raised (%s: %s)",
                    type(exc).__name__, _scrub(str(exc), token))
        return False
    if resp.status_code != 201:
        log.warning("image: upload PUT HTTP %s — image not stored",
                    resp.status_code)
        return False
    return True


def _try_upload_image(token, person_urn, image_ref):                    # STEP [12]
    """Full image upload pipeline. Returns the image URN on success, or None
    on ANY failure (caller falls back to text-only). Never raises.

    # STEP [12] Pipeline: get bytes → validate type/size → initializeUpload
    # STEP [12] → PUT bytes → return URN. Every step is fail-safe: a None at
    # STEP [12] any point means text-only, never post_failed."""
    data, ctype = _get_image_bytes(image_ref)
    if not data:
        return None
    if ctype not in config.IMAGE_ALLOWED_TYPES:
        log.warning("image: type %s not in allowed %s — text-only",
                    ctype, config.IMAGE_ALLOWED_TYPES)
        return None
    if len(data) > config.IMAGE_MAX_BYTES:
        log.warning("image: %d bytes > %d cap — text-only",
                    len(data), config.IMAGE_MAX_BYTES)
        return None
    upload_info = _initialize_upload(token, person_urn)
    if not upload_info:
        return None
    upload_url, image_urn = upload_info
    if not _upload_bytes(upload_url, token, data, ctype):
        return None
    log.info("image: uploaded → %s", image_urn)
    return image_urn


# ---------------------------------------------------------------------------
# STEP [26] Task 17 — POST retry classification. The LinkedIn POST is NOT
# STEP [26] idempotent: a duplicate live post is public and not quietly fixable,
# STEP [26] so we retry ONLY on failures provably safe (never reached the server)
# STEP [26] and make EXACTLY ONE attempt on anything ambiguous. Permanent errors
# STEP [26] (400/401/403) are handled at the HTTP-status layer in post().
# ---------------------------------------------------------------------------
# STEP [26] Marker approve.py detects so the Telegram announce can say "verify
# STEP [26] your profile" instead of the generic "posting failed". Substring match
# STEP [26] keeps the two modules decoupled (no shared state enum).
UNKNOWN_OUTCOME_MARKER = "OUTCOME UNKNOWN —"


def _walk_exc_chain(exc):                                                # STEP [26]
    """Yield exc and its linked causes/contexts/args/reason (2-ish levels deep).
    requests wraps urllib3 (MaxRetryError) which wraps the real builtin, so the
    decisive type (e.g. ConnectionRefusedError) is often nested. We match on
    type NAMES (no urllib3 import) so this survives a requests/urllib3 bump."""
    seen = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        for a in getattr(cur, "args", ()) or ():
            if isinstance(a, BaseException):
                stack.append(a)
        for attr in ("__cause__", "__context__", "reason"):              # STEP [26] reason = urllib3 MaxRetryError
            val = getattr(cur, attr, None)
            if isinstance(val, BaseException):
                stack.append(val)


# Type NAMES that prove the request never reached LinkedIn's app server.
_SAFE_NAMES = {"gaierror", "NameResolutionError", "NewConnectionError",   # STEP [26]
               "ConnectionRefusedError", "ConnectTimeout"}
_SAFE_MSG = ("failed to establish a new connection", "name or service not known",
             "connection refused", "nodename nor servname",               # STEP [26]
             "temporary failure in name resolution", "getaddrinfo failed",
             "no address associated with hostname")
# Type NAMES / messages that mean the request WAS sent and then dropped mid-flight.
_AMBIG_NAMES = {"ReadTimeout", "ConnectionResetError", "ConnectionAbortedError",  # STEP [26]
                "RemoteDisconnected", "ChunkedEncodingError", "IncompleteRead",
                "TimeoutError"}
_AMBIG_MSG = ("connection reset", "remote end closed",                    # STEP [26]
              "connection aborted", "connection broken")


def _root_is_establishment_failure(exc):                                 # STEP [26]
    """True ONLY if exc's chain proves a pre-send establishment failure (DNS /
    connect-refused / TLS-handshake). Reset / abort / remote-disconnect or
    anything unclassifiable -> False (caller treats as ambiguous: do not retry)."""
    found_safe = found_ambiguous = False
    for item in _walk_exc_chain(exc):
        name = type(item).__name__
        low = str(item).lower()
        if name in _AMBIG_NAMES or any(m in low for m in _AMBIG_MSG):
            found_ambiguous = True
        if name in _SAFE_NAMES or any(m in low for m in _SAFE_MSG):
            found_safe = True
    if found_ambiguous:
        return False          # mid-flight signal present -> ambiguous (bias)
    return found_safe         # True only on a clear establishment marker


def _classify_post_exception(exc):                                       # STEP [26]
    """'safe' (request provably never sent -> may retry) or 'ambiguous'
    (request may have been sent/processed -> exactly ONE attempt, never retry).

    # STEP [26] isinstance ORDER matters: ConnectTimeout and SSLError are BOTH
    # STEP [26] subclasses of ConnectionError, so they must be tested FIRST.
    # STEP [26] ConnectTimeout (connect phase) is safe; ReadTimeout (awaiting
    # STEP [26] reply AFTER send) is ambiguous — requests distinguishes them and
    # STEP [26] so do we, rather than catching Timeout broadly."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):               # STEP [26]
        return "safe"
    if isinstance(exc, requests.exceptions.ReadTimeout):                  # STEP [26]
        return "ambiguous"
    if isinstance(exc, requests.exceptions.SSLError):                     # STEP [26] TLS handshake
        return "safe"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "safe" if _root_is_establishment_failure(exc) else "ambiguous"
    return "ambiguous"   # plain Timeout / unknown -> may have been sent


def post(text, image_ref=None):
    """Publish `text` (with an optional image) to the author's LinkedIn feed.

    # STEP [12] image_ref: {"url": ..., "source": "story"|"telegram_override"}
    # STEP [12] or None. On ANY image failure the post falls back to text-only
    # STEP [12] — the image must never block, delay, or fail a post. The 3rd
    # STEP [12] return value tells the caller whether the image made it in.

    Returns (ok: bool, post_id_or_error: str, image_attached: bool):
      - 201 success  → (True, x-restli-id, True/False)
      - dry-run      → (True, "DRY_RUN_<utc>", True/False)  — no API call
      - any failure  → (False, short scrubbed reason, False)

    Never raises."""
    token = os.environ.get(config.LINKEDIN_TOKEN_ENV)
    person_urn = os.environ.get(config.LINKEDIN_PERSON_URN_ENV)
    if not token or not person_urn:
        # Log env-var NAMES only — never the values.
        log.error("post: %s / %s not set — cannot post to LinkedIn",
                  config.LINKEDIN_TOKEN_ENV, config.LINKEDIN_PERSON_URN_ENV)
        return False, f"{config.LINKEDIN_TOKEN_ENV} / {config.LINKEDIN_PERSON_URN_ENV} not set", False

    if not isinstance(text, str) or not text.strip():
        log.error("post: draft text is empty/non-str — refusing to post nothing")
        return False, "empty draft text", False

    # STEP [12] Image upload (or fake URN in dry-run). Any failure → image_urn
    # STEP [12] stays None → body has no content.media → text-only post.
    dry_run = os.environ.get(config.LINKEDIN_DRY_RUN_ENV) == "1"
    image_urn = None
    if image_ref:
        if dry_run:
            image_urn = f"DRY_RUN_IMAGE_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            log.info("post: LINKEDIN_DRY_RUN=1 — would upload image %s → fake %s",
                     image_ref, image_urn)
        else:
            image_urn = _try_upload_image(token, person_urn, image_ref)
            if image_urn:
                log.info("post: image attached → %s", image_urn)
            else:
                log.warning("post: image unavailable — falling back to text-only")

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
    if image_urn:                                                       # STEP [12]
        body["content"] = {"media": {"id": image_urn,
                                      "altText": "AI news digest image"}}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": config.LINKEDIN_VERSION,
    }

    # STEP [10] Dry-run: log the exact request, fake a success, do NOT call.
    if dry_run:
        fake_id = f"DRY_RUN_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        log.info("post: LINKEDIN_DRY_RUN=1 — NOT calling LinkedIn. Would send:")
        log.info("  POST %s", config.LINKEDIN_POSTS_URL)
        log.info("  headers: %s", {k: (_scrub(v, token) if k == "Authorization" else v)
                                   for k, v in headers.items()})
        log.info("  body: %s", body)
        log.info("  → returning fake success id=%s (image=%s)",
                 fake_id, bool(image_urn))
        return True, fake_id, bool(image_urn)

    # STEP [26] Retry loop. The LinkedIn POST is NOT idempotent — a duplicate
    # STEP [26] live post is public and not quietly fixable — so retry ONLY on
    # STEP [26] failures provably safe (never reached the server) and make
    # STEP [26] EXACTLY ONE attempt on anything ambiguous (read-timeout, reset,
    # STEP [26] 5xx, 201-no-id), surfacing UNKNOWN_OUTCOME_MARKER so approve.py's
    # STEP [26] announce says "verify your profile". 400/401/403 fail fast. Bias:
    # STEP [26] when in doubt, do NOT retry the post. Constants in config;
    # STEP [26] retryutil.sleep is the test-stubbed sleep seam (never real-sleeps).
    for attempt in range(1, config.LINKEDIN_POST_MAX_ATTEMPTS + 1):       # STEP [26]
        try:
            resp = requests.post(config.LINKEDIN_POSTS_URL, json=body,
                                 headers=headers, timeout=config.TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — classified below, never crashes
            verdict = _classify_post_exception(exc)                       # STEP [26]
            reason = _scrub(f"{type(exc).__name__}: {exc}", token)
            if verdict == "ambiguous":                                    # STEP [26]
                # Request may have been sent/processed. NEVER retry — record as
                # terminal post_failed (approve.py never auto-retries it) and
                # tell Harvey to verify his profile before re-triggering.
                msg = (UNKNOWN_OUTCOME_MARKER +
                       f" request raised ({reason}). The post MAY have been "
                       "published — LinkedIn gave no confirmable response. "
                       "CHECK YOUR LINKEDIN PROFILE before re-triggering.")
                log.error("post: AMBIGUOUS outcome, NOT retrying — %s", msg)
                return False, msg, False
            if attempt < config.LINKEDIN_POST_MAX_ATTEMPTS:               # STEP [26] safe -> retry
                wait = retryutil.backoff_delay(
                    attempt, config.LINKEDIN_POST_BACKOFF_BASE_S,
                    config.LINKEDIN_POST_BACKOFF_MAX_S)
                log.warning("post: attempt %d/%d SAFE-retry (%s) — retry in %.1fs",
                            attempt, config.LINKEDIN_POST_MAX_ATTEMPTS,
                            reason, wait)
                retryutil.sleep(wait)
                continue
            log.error("post: %d safe-retry attempt(s) all failed — %s",
                      attempt, reason)
            return False, f"all {attempt} safe-retry attempts failed ({reason})", False

        if resp.status_code == 201:
            post_id = resp.headers.get("x-restli-id")
            if not post_id:
                # 201 means the post was very likely CREATED; with no id we can
                # neither confirm nor link it. One attempt, ambiguous message.
                log.error("post: 201 returned but x-restli-id missing — outcome unknown")
                return False, (UNKNOWN_OUTCOME_MARKER +                   # STEP [26]
                               " LinkedIn returned 201 but no x-restli-id header; "
                               "the post MAY be live. CHECK YOUR LINKEDIN PROFILE."), False
            if attempt > 1:                                               # STEP [26]
                log.info("post: published on attempt %d/%d",
                         attempt, config.LINKEDIN_POST_MAX_ATTEMPTS)
            log.info("post: published — linkedin_post_id=%s (len=%d, image=%s)",
                     post_id, len(text), bool(image_urn))
            return True, post_id, bool(image_urn)

        # Non-201. Only resp.text[:200] (could be large), token-scrubbed.
        snippet = _scrub(resp.text[:200], token) if resp.text else ""

        if resp.status_code == 429 and attempt < config.LINKEDIN_POST_MAX_ATTEMPTS:  # STEP [26]
            # 429 = LinkedIn rate-limited at the edge, request NOT processed ->
            # provably safe to retry. (Not ambiguous: nothing was created.)
            wait = retryutil.backoff_delay(
                attempt, config.LINKEDIN_POST_BACKOFF_BASE_S,
                config.LINKEDIN_POST_BACKOFF_MAX_S)
            log.warning("post: HTTP 429 rate-limited (SAFE-retry) attempt %d/%d "
                        "— retry in %.1fs", attempt,
                        config.LINKEDIN_POST_MAX_ATTEMPTS, wait)
            retryutil.sleep(wait)
            continue

        if 500 <= resp.status_code < 600:                                 # STEP [26]
            # Ambiguous (Harvey's call, Task 17): a POST 5xx may mean "created
            # then failed to respond" just as much as "rejected at the edge" —
            # we can't prove either, so we do NOT retry. Outcome unknown.
            msg = (UNKNOWN_OUTCOME_MARKER +
                   f" HTTP {resp.status_code} ({snippet}). The post MAY have been "
                   "published. CHECK YOUR LINKEDIN PROFILE before re-triggering.")
            log.error("post: AMBIGUOUS HTTP %s, NOT retrying — %s",
                      resp.status_code, msg)
            return False, msg, False

        # 4xx (400/401/403 and other) / non-201: permanent client error. Retrying
        # wastes time and hides the real cause. One attempt, plain error.
        reason = f"HTTP {resp.status_code}: {snippet}".strip()
        log.error("post: LinkedIn rejected the post — %s", reason)
        return False, reason, False

    return False, "post: retry loop exhausted without a result (unexpected)", False  # STEP [26]


if __name__ == "__main__":
    # Manual smoke test. Set LINKEDIN_ACCESS_TOKEN / LINKEDIN_PERSON_URN in env
    # or .env. Use LINKEDIN_DRY_RUN=1 to avoid a real post.
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    draft = ("Test draft from post_linkedin.py — please ignore.\n\n"
             "#AI #Test")
    ok, result, img = post(draft)
    print(f"ok={ok} result={result} image={img}")
    sys.exit(0 if ok else 1)
