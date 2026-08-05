"""
gmail_fetch.py — Gmail newsletter ingestion (Phase 3, Task 9).    # STEP [14]

Newsletters are selected by Gmail LABEL only (config.GMAIL_LABEL), from the last
GMAIL_LOOKBACK_H hours. Each qualifying link in a newsletter becomes ONE candidate
Story:
    title      = anchor text
    link       = the URL
    summary    = ""
    published  = the email's Date header (tz-aware UTC, or None)
    source_name= "Newsletter: <sender display name>"
    source_id  = config.NEWSLETTER_SOURCE_ID (18)
    priority   = config.NEWSLETTER_PRIORITY (3)
    image_url  = None

These flow into the EXISTING rank/dedupe unchanged: the normalized-title SHA-256
dedupe collapses a newsletter link that duplicates an RSS story, and the normal
scoring ranks them. Because all newsletters share source_id 18, rank's
MAX_PER_SOURCE_IN_TOP caps the whole newsletter layer at 2 in the digest — that
is intended (newsletters are a priority-3 supplement, not the lead).

OVERRIDING RULE — Gmail is ADDITIVE and ALWAYS NON-FATAL (it is the most fragile
input: IMAP + newsletter HTML that changes without notice):
- ANY failure (login, empty/missing label, parse, decode) -> log WARNING and
  return whatever was gathered so far (possibly []). NEVER raise. The pipeline
  must still produce today's normal post from the RSS sources.
- Only stdlib: imaplib, email (+ email.header / email.utils / email.policy),
  html.parser. No new dependencies.
- Secrets GMAIL_ADDRESS / GMAIL_APP_PASSWORD are read from env and NEVER logged.
- The IMAP connection is ALWAYS closed in finally, guarded against the
  login-failure path (imap is None) so cleanup can never raise and defeat the
  non-fatal guarantee.

The network seam is isolated in _imap_connect(); _parse_one_email(raw_bytes) and
_extract_links(...) are testable offline with no IMAP at all.
"""

import imaplib                                                        # STEP [14]
import logging                                                        # STEP [14]
import os                                                             # STEP [14]
import re                                                             # STEP [14]
from datetime import datetime, timedelta, timezone                    # STEP [14]
from email import message_from_bytes                                  # STEP [14]
from email import policy as email_policy                              # STEP [14]
from email.header import decode_header, make_header                   # STEP [14]
from email.utils import parseaddr, parsedate_to_datetime              # STEP [14]
from html.parser import HTMLParser                                    # STEP [14]

import config                                                         # STEP [14]
import retryutil                                                      # STEP [26]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Link extraction from newsletter HTML                                  # STEP [14]
# ---------------------------------------------------------------------------
class _LinkCollector(HTMLParser):                                     # STEP [14]
    """Collect (href, anchor_text) pairs from newsletter HTML.

    # STEP [14] Tolerant: nested inline tags inside <a> (e.g. <b>, <span>) have
    # STEP [14] their text folded into the anchor text. Anchors without href are
    # STEP [14] ignored. Never raises — malformed HTML is the norm for email."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []          # list of (href, text)
        self._href = None        # current anchor href, or None when not inside <a>
        self._buf = []           # collected text nodes for the current anchor

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            d = dict(attrs)
            href = (d.get("href") or "").strip()
            if href:                     # ignore <a name="x"> with no href
                self._href = href
                self._buf = []

    def handle_data(self, data):
        if self._href is not None:       # only gather text inside an anchored <a>
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            text = " ".join("".join(self._buf).split()).strip()
            self.links.append((self._href, text))
            self._href = None
            self._buf = []


# Bare-URL regex for the text/plain fallback.                           # STEP [14]
_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)          # STEP [14]


def _extract_links(body, sender_name, email_dt, is_html=True):        # STEP [14]
    """Turn a newsletter body into a list of qualifying Story dicts.

    # STEP [14] Filters (keep newsletter noise out): drop non-http(s) / mailto:,
    # STEP [14] drop duplicate URLs within this email, drop anchors shorter than
    # STEP [14] NEWSLETTER_MIN_ANCHOR_CHARS, drop anchors matching a boilerplate
    # STEP [14] stopword, cap at NEWSLETTER_MAX_LINKS_PER_EMAIL in document order.
    # STEP [14] Never raises — a parse failure yields []."""
    try:
        if is_html:
            collector = _LinkCollector()
            try:
                collector.feed(body or "")
            except Exception as exc:        # noqa: BLE001 — never let bad HTML crash us
                log.warning("gmail: HTML parse failed (%s) — no links from this email", exc)
                return []
            raw_links = collector.links
        else:
            # STEP [14] PLAINTEXT FALLBACK: bare URLs carry no anchor text, so the
            # STEP [14] MIN_ANCHOR_CHARS filter drops essentially all of them —
            # STEP [14] plaintext newsletters contribute little under the current
            # STEP [14] filter (tunable later: lower the threshold, or pull the line
            # STEP [14] of text around a URL as the anchor).
            raw_links = []
            for m in _URL_RE.finditer(body or ""):
                raw_links.append((m.group(0).rstrip(".,);]'\""), ""))
    except Exception as exc:  # noqa: BLE001
        log.warning("gmail: link extraction failed (%s) — skipping email", exc)
        return []

    stories = []
    seen_urls = set()
    for href, text in raw_links:
        url = (href or "").strip()
        if not url:
            continue
        low = url.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            continue  # mailto:, javascript:, ftp:, anchors (#), etc.
        if url in seen_urls:
            continue  # collapse identical URLs within one email
        seen_urls.add(url)
        anchor_low = (text or "").lower()
        if any(b in anchor_low for b in config.NEWSLETTER_BOILERPLATE):
            continue  # footer / social / ad boilerplate
        if len(text) < config.NEWSLETTER_MIN_ANCHOR_CHARS:
            continue  # too short to be a real headline
        stories.append({
            "source_id": config.NEWSLETTER_SOURCE_ID,
            "source_name": f"Newsletter: {sender_name}",
            "priority": config.NEWSLETTER_PRIORITY,
            "title": text[:300],
            "link": url,
            "summary": "",
            "published": email_dt,
            "image_url": None,
        })
        if len(stories) >= config.GMAIL_MAX_LINKS_PER_EMAIL:
            break  # cap in document order
    return stories


# ---------------------------------------------------------------------------
# One email -> stories (no IMAP; testable offline)                     # STEP [14]
# ---------------------------------------------------------------------------
def _decode_header_value(raw):
    """RFC2047-decode a header (e.g. display name). Returns a clean str; never raises."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # noqa: BLE001
        try:
            return str(raw)
        except Exception:  # noqa: BLE001
            return ""


def _sender_display(msg):                                             # STEP [14]
    """Sender display name for source_name (falls back to address, then 'newsletter')."""
    try:
        name, addr = parseaddr(str(msg.get("From", "")))
        name = _decode_header_value(name)
        return (name or addr or "newsletter").strip() or "newsletter"
    except Exception:  # noqa: BLE001
        return "newsletter"


def _parse_email_date(msg):                                           # STEP [14]
    """Email Date header -> tz-aware UTC datetime, or None on any failure.

    # STEP [14] None is safe: rank discards undated stories, so a bad Date can't
    # STEP [14] crash the run or leak a stale story."""
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        log.debug("gmail: unparseable Date header (%s)", exc)
        return None


def _body_text(msg):                                                  # STEP [14]
    """Return (content_str, is_html). Prefer text/html; fall back to text/plain.

    # STEP [14] get_content() (policy=default) decodes both the transfer encoding
    # STEP [14] (base64 / quoted-printable) and the charset for us. A second
    # STEP [14] best-effort fallback guards odd clients."""
    html = ""
    text = ""
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = (part.get_content_type() or "").lower()
        if ctype not in ("text/html", "text/plain"):
            continue
        try:
            content = part.get_content()
        except Exception:  # noqa: BLE001
            try:
                raw = part.get_payload(decode=True) or b""
                cs = part.get_content_charset() or "utf-8"
                content = raw.decode(cs, "replace")
            except Exception:  # noqa: BLE001
                continue
        if not isinstance(content, str):
            continue
        if ctype == "text/html" and not html:
            html = content
        elif ctype == "text/plain" and not text:
            text = content
    if html:
        return html, True
    return text, False


def _parse_one_email(raw_bytes):                                      # STEP [14]
    """Parse one raw email (bytes) -> list of Story dicts. Never raises; [] on failure."""
    try:
        msg = message_from_bytes(raw_bytes, policy=email_policy.default)
    except Exception as exc:  # noqa: BLE001
        log.warning("gmail: email parse failed (%s)", exc)
        return []
    sender = _sender_display(msg)
    when = _parse_email_date(msg)
    body, is_html = _body_text(msg)
    if not body:
        return []
    return _extract_links(body, sender, when, is_html=is_html)


# ---------------------------------------------------------------------------
# IMAP seam (the ONLY network surface)                                 # STEP [14]
# ---------------------------------------------------------------------------
def _imap_connect():                                                  # STEP [14]
    """Connect to imap.gmail.com over SSL and log in. Raises on any failure.

    # STEP [14] Credential check happens BEFORE any network object is created, so a
    # STEP [14] missing-secret path never opens a socket. The password is NEVER
    # STEP [14] logged by this function or its callers."""
    addr = os.environ.get(config.GMAIL_ADDRESS_ENV)
    pwd = os.environ.get(config.GMAIL_APP_PASSWORD_ENV)
    if not addr or not pwd:
        raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")
    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    imap.login(addr, pwd)  # NEVER log pwd; callers log only the failure type
    return imap


def fetch_newsletter_stories():                                       # STEP [14]
    """Fetch newsletter stories from Gmail.

    # STEP [14] ADDITIVE + ALWAYS non-fatal: returns [] (or partial) on ANY failure,
    # STEP [14] NEVER raises. See module docstring. The IMAP connection is always
    # STEP [14] released in finally, guarded against the login-failure path."""
    imap = None
    try:
        # STEP [26] Retry the connect/login on TRANSIENT socket errors only.
        # STEP [26] OSError covers DNS failure, connection refused, and socket
        # STEP [26] timeout (IMAP4_SSL opens the socket in open()). An
        # STEP [26] imaplib.IMAP4.error (auth / "INVALID CREDENTIALS") or a
        # STEP [26] RuntimeError (missing secrets, from _imap_connect) is NOT an
        # STEP [26] OSError -> escapes this loop to the outer except below -> NOT
        # STEP [26] retried (a wrong app password won't fix itself; retrying login
        # STEP [26] risks looking like a brute-force). Always non-fatal: exhaustion -> [].
        for attempt in range(1, config.GMAIL_IMAP_MAX_ATTEMPTS + 1):    # STEP [26]
            try:
                imap = _imap_connect()                # may raise -> imap stays None
                break
            except OSError as exc:                    # STEP [26] transient connect -> retry
                if attempt < config.GMAIL_IMAP_MAX_ATTEMPTS:
                    wait = retryutil.backoff_delay(
                        attempt, config.GMAIL_IMAP_BACKOFF_BASE_S,
                        config.GMAIL_IMAP_BACKOFF_MAX_S)
                    log.warning("gmail: IMAP connect transient (%s: %s) — "
                                "retry %d/%d in %.1fs",
                                type(exc).__name__, exc, attempt,
                                config.GMAIL_IMAP_MAX_ATTEMPTS, wait)
                    retryutil.sleep(wait)
                    continue
                log.warning("gmail: IMAP connect failed after %d attempts "
                            "(%s: %s) — newsletters skipped",
                            attempt, type(exc).__name__, exc)
                return []
            except Exception as exc:        # STEP [26] auth (IMAP4.error) / RuntimeError (missing creds) -> permanent, NO retry
                log.warning("gmail: IMAP login failed (%s: %s) — newsletters skipped",
                            type(exc).__name__, exc)
                return []

        # Select the label as a mailbox (Gmail labels are selectable mailboxes).
        # STEP [14] A wrong/empty label returns OK with count 0 — treat as no stories,
        # STEP [14] not an error. Wrap in case select itself raises.
        try:
            typ, sel = imap.select(f'"{config.GMAIL_LABEL}"')
        except Exception as exc:                  # noqa: BLE001
            log.warning("gmail: select('%s') raised (%s) — newsletters skipped",
                        config.GMAIL_LABEL, exc)
            return []
        count = 0
        try:
            if sel and sel[0] is not None:
                count = int(sel[0])
        except (TypeError, ValueError):
            count = 0
        if typ != "OK" or count == 0:
            log.info("gmail: label '%s' empty or not selectable (count=%s) — no newsletters",
                     config.GMAIL_LABEL, count)
            return []

        # IMAP SINCE is date-only; rank's MAX_STORY_AGE_H is the real time filter
        # STEP [14] (defense in depth). %d-%b-%Y is the IMAP date format.
        since = (datetime.now(timezone.utc) - timedelta(hours=config.GMAIL_LOOKBACK_H)
                 ).strftime("%d-%b-%Y")
        try:
            typ, data = imap.search(None, "SINCE", since)
        except Exception as exc:                  # noqa: BLE001
            log.warning("gmail: search failed (%s: %s) — newsletters skipped",
                        type(exc).__name__, exc)
            return []
        if typ != "OK" or not data or not data[0]:
            log.info("gmail: no newsletter emails since %s", since)
            return []

        uids = data[0].split()
        stories = []
        for uid in uids:
            try:
                typ, msgdata = imap.fetch(uid, "(BODY.PEEK[])")
            except Exception as exc:              # noqa: BLE001
                log.warning("gmail: fetch uid %s failed (%s) — skipping", uid, exc)
                continue
            if typ != "OK" or not msgdata or not isinstance(msgdata[0], tuple):
                continue
            raw = msgdata[0][1]
            if not raw:
                continue
            try:
                stories.extend(_parse_one_email(raw))
            except Exception as exc:              # noqa: BLE001
                log.warning("gmail: email uid %s failed (%s) — skipping", uid, exc)
                continue
        log.info("gmail: %d newsletter stories from %d email(s)",
                 len(stories), len(uids))
        return stories
    except Exception as exc:  # noqa: BLE001 — absolute backstop: a bug here must never crash the run
        log.warning("gmail: unexpected failure (%s: %s) — returning []",
                    type(exc).__name__, exc)
        return []
    finally:
        # STEP [14] Cleanup is guarded against imap == None (login-failure path)
        # STEP [14] so it can NEVER raise and defeat the non-fatal guarantee.
        # STEP [14] close() is only valid after a successful SELECT, and logout()
        # STEP [14] is safe even on a half-open session — wrap both independently.
        if imap is not None:                                            # STEP [14]
            try:                                                        # STEP [14]
                imap.close()                                            # STEP [14]
            except Exception:  # noqa: BLE001                          # STEP [14]
                pass                                                    # STEP [14]
            try:                                                        # STEP [14]
                imap.logout()                                           # STEP [14]
            except Exception:  # noqa: BLE001                          # STEP [14]
                pass                                                    # STEP [14]


if __name__ == "__main__":
    # Manual live test: python src/gmail_fetch.py (run from src/) — needs
    # GMAIL_ADDRESS + GMAIL_APP_PASSWORD + the AI-Newsletters label set up.
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = fetch_newsletter_stories()
    print(f"\n{len(result)} newsletter stories:")
    for s in result:
        when = s["published"].strftime("%Y-%m-%d %H:%M UTC") if s["published"] else "undated"
        print(f"  [{s['source_name']}] ({when}) {s['title'][:80]}\n    {s['link']}")
