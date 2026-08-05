"""
test_retries.py — Phase 4 Task 17 (STEP 26). Fully stubbed: NO network, and
absolutely NO real LinkedIn POST. requests is mocked per-module; retryutil.sleep
is patched everywhere so the suite is instant AND so we can assert exact backoff
delays (and Telegram's 429 retry_after). Run from the repo root:  python test_retries.py

The LinkedIn POST is NOT idempotent, so the call-count assertions (== exactly
the attempts we expect, no more) ARE the test that no double-post path exists.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import approve         # noqa: E402
import config          # noqa: E402
import gmail_fetch     # noqa: E402
import notify          # noqa: E402
import post_linkedin   # noqa: E402
import retryutil       # noqa: E402
import telegram_api    # noqa: E402

TOKEN = "SECRET-TOKEN-123"
URN = "urn:li:person:ABC123"
POST_ID = "urn:li:share:987654321"
LK_ENV = {config.LINKEDIN_TOKEN_ENV: TOKEN, config.LINKEDIN_PERSON_URN_ENV: URN}
TG_ENV = {config.TELEGRAM_TOKEN_ENV: "TGTOKEN", config.TELEGRAM_CHAT_ID_ENV: "123"}


# --- response builders -------------------------------------------------------
def _lk_resp(status_code=201, post_id=POST_ID, text=""):
    r = mock.Mock()
    r.status_code = status_code
    r.headers = {}
    if post_id is not None:
        r.headers["x-restli-id"] = post_id
    r.text = text
    return r


def _tg_resp(ok=True, message_id=None, error_code=None, retry_after=None,
             description=""):
    r = mock.Mock()
    r.status_code = error_code if error_code else 200
    body = {"ok": ok}
    if ok:
        body["result"] = {"message_id": message_id if message_id is not None else 1}
    else:
        body["error_code"] = error_code if error_code is not None else 400
        body["description"] = description
        if retry_after is not None:
            body["parameters"] = {"retry_after": retry_after}
    r.json.return_value = body
    return r


# LinkedIn exceptions, constructed so the classifier can read the cause chain
# the way real requests/urllib3 would surface them.
def _refused():
    exc = post_linkedin.requests.exceptions.ConnectionError("refused")
    exc.__cause__ = ConnectionRefusedError("Connection refused")   # establishment
    return exc


def _reset_midflight():
    exc = post_linkedin.requests.exceptions.ConnectionError("reset")
    exc.__cause__ = ConnectionResetError("Connection reset by peer")  # mid-flight
    return exc


passed = failed = 0


def run_check(name, fn):
    global passed, failed
    try:
        fn()
        passed += 1
        print(f"  PASS  {name}")
    except Exception as exc:  # noqa: BLE001
        failed += 1
        print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")


# ===========================================================================
# backoff_delay — pure unit checks
# ===========================================================================
def test_backoff_delay_math():
    assert retryutil.backoff_delay(1, 2.0, 16.0) == 2.0
    assert retryutil.backoff_delay(2, 2.0, 16.0) == 4.0
    assert retryutil.backoff_delay(3, 2.0, 16.0) == 8.0
    assert retryutil.backoff_delay(10, 2.0, 16.0) == 16.0   # capped
    assert retryutil.backoff_delay(2, 2.0, 30.0) == 4.0     # telegram cap unused here


# ===========================================================================
# LinkedIn POST classification (the critical set) — call_count = no double-post
# ===========================================================================
def test_lk_connection_refused_retries_to_max_then_fails():
    with mock.patch.dict(os.environ, LK_ENV), \
         mock.patch("retryutil.sleep"), \
         mock.patch.object(post_linkedin.requests, "post", side_effect=_refused()) as posted:
        ok, result, _ = post_linkedin.post("draft")
    assert ok is False, (ok, result)
    assert posted.call_count == config.LINKEDIN_POST_MAX_ATTEMPTS, \
        f"refused=SAFE: must retry to max ({config.LINKEDIN_POST_MAX_ATTEMPTS}), got {posted.call_count}"
    assert "OUTCOME UNKNOWN" not in result, "refused never reached LinkedIn -> not unknown"
    assert "safe-retry" in result, result


def test_lk_connect_timeout_is_safe_retried():
    exc = post_linkedin.requests.exceptions.ConnectTimeout("connect timed out")
    with mock.patch.dict(os.environ, LK_ENV), \
         mock.patch("retryutil.sleep"), \
         mock.patch.object(post_linkedin.requests, "post", side_effect=exc) as posted:
        ok, result, _ = post_linkedin.post("draft")
    assert ok is False
    assert posted.call_count == config.LINKEDIN_POST_MAX_ATTEMPTS, \
        f"ConnectTimeout=SAFE: must retry to max, got {posted.call_count}"


def test_lk_read_timeout_is_ambiguous_one_attempt():
    # READ-timeout = request was sent, awaiting reply -> AMBIGUOUS. Exactly ONE
    # attempt, never retried (a retry could double-post), explicit unknown msg.
    exc = post_linkedin.requests.exceptions.ReadTimeout("read timed out")
    with mock.patch.dict(os.environ, LK_ENV), \
         mock.patch("retryutil.sleep") as sl, \
         mock.patch.object(post_linkedin.requests, "post", side_effect=exc) as posted:
        ok, result, _ = post_linkedin.post("draft")
    assert ok is False
    assert posted.call_count == 1, \
        f"ReadTimeout=AMBIGUOUS: must make EXACTLY ONE attempt, got {posted.call_count}"
    assert sl.call_count == 0, "ambiguous must NOT sleep/retry"
    low = result.lower()
    assert "unknown" in low and "profile" in low, f"ambiguous msg must say unknown/profile: {result!r}"


def test_lk_connection_reset_is_ambiguous_one_attempt():
    with mock.patch.dict(os.environ, LK_ENV), \
         mock.patch("retryutil.sleep") as sl, \
         mock.patch.object(post_linkedin.requests, "post", side_effect=_reset_midflight()) as posted:
        ok, result, _ = post_linkedin.post("draft")
    assert ok is False
    assert posted.call_count == 1, \
        f"reset=AMBIGUOUS: must make EXACTLY ONE attempt, got {posted.call_count}"
    assert sl.call_count == 0
    assert "unknown" in result.lower() and "profile" in result.lower()


def test_lk_permanent_401_403_400_no_retry():
    for code in (400, 401, 403):
        resp = _lk_resp(code, None, f'{{"message":"{code}"}}')
        with mock.patch.dict(os.environ, LK_ENV), \
             mock.patch.object(post_linkedin.requests, "post", return_value=resp) as posted:
            ok, result, _ = post_linkedin.post("draft")
        assert ok is False, (code, result)
        assert posted.call_count == 1, f"HTTP {code}=PERMANENT: one attempt, got {posted.call_count}"
        assert f"HTTP {code}" in result, result
        assert "OUTCOME UNKNOWN" not in result, f"{code} is permanent, not unknown"


def test_lk_429_retried_then_succeeds():
    with mock.patch.dict(os.environ, LK_ENV), \
         mock.patch("retryutil.sleep") as sl, \
         mock.patch.object(post_linkedin.requests, "post",
                           side_effect=[_lk_resp(429, None, "rate limited"),
                                        _lk_resp(201, POST_ID)]) as posted:
        ok, result, _ = post_linkedin.post("draft")
    assert ok is True, (ok, result)
    assert result == POST_ID, result
    assert posted.call_count == 2, f"429 then 201 -> 2 calls, got {posted.call_count}"
    assert sl.call_count == 1, "one backoff sleep before the retry"
    assert sl.call_args.args[0] == config.LINKEDIN_POST_BACKOFF_BASE_S


def test_lk_5xx_is_ambiguous_one_attempt():
    # Harvey's call (Task 17): a POST 5xx may mean "created then failed to
    # respond"; we can't prove otherwise, so do NOT retry. Outcome unknown.
    with mock.patch.dict(os.environ, LK_ENV), \
         mock.patch("retryutil.sleep") as sl, \
         mock.patch.object(post_linkedin.requests, "post",
                           return_value=_lk_resp(503, None, "edge error")) as posted:
        ok, result, _ = post_linkedin.post("draft")
    assert ok is False
    assert posted.call_count == 1, f"5xx=AMBIGUOUS: one attempt, got {posted.call_count}"
    assert sl.call_count == 0
    assert "unknown" in result.lower() and "503" in result


def test_lk_safe_failure_then_success_propagates_id_through_approve():
    # Safe failure (ConnectTimeout) on attempt 1, 201 on attempt 2 -> success,
    # AND the post_id propagates through approve._post_approved_draft to the
    # state file as linkedin_post_id with status 'posted'.
    exc = post_linkedin.requests.exceptions.ConnectTimeout("connect timed out")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "pending_post.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"draft": "A draft.", "created_utc":
                       datetime.now(timezone.utc).isoformat(),
                       "telegram_message_id": 9, "status": "approved"}, fh)
        with mock.patch.dict(os.environ, LK_ENV), \
             mock.patch("retryutil.sleep"), \
             mock.patch.object(post_linkedin.requests, "post",
                               side_effect=[exc, _lk_resp(201, POST_ID)]) as posted, \
             mock.patch.object(approve, "PENDING_POST_PATH", path):
            ok, post_id, err, _, _ = approve._post_approved_draft()
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
    assert ok is True, (ok, err)
    assert post_id == POST_ID
    assert posted.call_count == 2, f"safe failure then success -> 2 calls, got {posted.call_count}"
    assert saved["status"] == "posted", saved
    assert saved["linkedin_post_id"] == POST_ID, saved   # id propagated to the success path


def test_lk_201_no_id_is_ambiguous():
    # 201 means likely created; with no id we can't confirm/link -> one attempt,
    # explicit unknown message (NOT a plain "x-restli-id absent" failure).
    with mock.patch.dict(os.environ, LK_ENV), \
         mock.patch.object(post_linkedin.requests, "post",
                           return_value=_lk_resp(201, post_id=None)) as posted:
        ok, result, _ = post_linkedin.post("draft")
    assert ok is False
    assert posted.call_count == 1
    assert "unknown" in result.lower() and "profile" in result.lower(), result


# ===========================================================================
# Telegram — notify.send_draft (button-message-id invariant) + api_call
# ===========================================================================
def test_tg_send_draft_transient_then_success_returns_final_id():
    # Transient non-ok (error_code 500) then ok with message_id 99 -> we must
    # return the FINALLY-confirmed id (99), proving the button-id invariant.
    with mock.patch.dict(os.environ, TG_ENV), \
         mock.patch("retryutil.sleep"), \
         mock.patch.object(notify.requests, "post",
                           side_effect=[_tg_resp(ok=False, error_code=500,
                                                 description="internal"),
                                        _tg_resp(ok=True, message_id=99)]) as posted:
        ok, mid = notify.send_draft("draft body.", [])
    assert posted.call_count == 2, f"500 then ok -> 2 calls, got {posted.call_count}"
    assert (ok, mid) == (True, 99), (ok, mid)   # FINAL id, not a stale/partial one


def test_tg_send_draft_network_error_then_success():
    # Stronger invariant: attempt 1 raises (no response, so NO id known at all),
    # attempt 2 ok -> returned id is the confirmed one. An orphaned draft from a
    # silently-succeeded attempt 1 would carry an id we never store -> can't post.
    boom = notify.requests.exceptions.ConnectionError("blip")
    with mock.patch.dict(os.environ, TG_ENV), \
         mock.patch("retryutil.sleep"), \
         mock.patch.object(notify.requests, "post",
                           side_effect=[boom, _tg_resp(ok=True, message_id=77)]) as posted:
        ok, mid = notify.send_draft("draft body.", [])
    assert posted.call_count == 2
    assert (ok, mid) == (True, 77), (ok, mid)


def test_tg_api_call_400_401_not_retried():
    for code in (400, 401):
        with mock.patch.object(telegram_api.requests, "post",
                               return_value=_tg_resp(ok=False, error_code=code,
                                                      description="nope")) as posted:
            res = telegram_api.api_call("sendMessage", {"chat_id": 1, "text": "x"}, "TOK")
        assert res is None, (code, res)
        assert posted.call_count == 1, f"HTTP {code}=permanent: one attempt, got {posted.call_count}"


def test_tg_api_call_429_honors_retry_after():
    with mock.patch.object(telegram_api.requests, "post",
                           side_effect=[_tg_resp(ok=False, error_code=429,
                                                 retry_after=7,
                                                 description="retry after 7"),
                                        _tg_resp(ok=True, message_id=5)]) as posted, \
         mock.patch("retryutil.sleep") as sl:
        res = telegram_api.api_call("sendMessage", {"chat_id": 1, "text": "x"}, "TOK")
    assert posted.call_count == 2
    assert res == {"message_id": 5}, res
    assert sl.call_count == 1, "must sleep once for the 429"
    assert sl.call_args.args[0] == 7.0, f"honor retry_after=7, got {sl.call_args}"


# ===========================================================================
# Gmail IMAP — retry connect on transient socket errors, stay non-fatal
# ===========================================================================
class _FakeIMAP:
    """Minimal stand-in: select returns count 0 -> fetch returns [] cleanly."""
    def __init__(self):
        self._opened = True
    def select(self, box):
        return ("OK", [b"0"])
    def close(self):
        pass
    def logout(self):
        pass


def _gmail_env():
    os.environ[config.GMAIL_ADDRESS_ENV] = "x@gmail.com"
    os.environ[config.GMAIL_APP_PASSWORD_ENV] = "y"


def _gmail_env_clear():
    os.environ.pop(config.GMAIL_ADDRESS_ENV, None)
    os.environ.pop(config.GMAIL_APP_PASSWORD_ENV, None)


def test_imap_transient_then_success_retried():
    _gmail_env()
    state = {"n": 0}

    def connect():
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("transient socket error")   # transient -> retry
        return _FakeIMAP()

    try:
        with mock.patch.object(gmail_fetch, "_imap_connect", side_effect=connect), \
             mock.patch("retryutil.sleep") as sl:
            out = gmail_fetch.fetch_newsletter_stories()
        assert state["n"] == 2, f"transient then success -> 2 connect attempts, got {state['n']}"
        assert sl.call_count == 1, "one backoff between attempts"
        assert out == [], "non-fatal: returns [] (empty label), never raises"
    finally:
        _gmail_env_clear()


def test_imap_total_failure_is_non_fatal():
    _gmail_env()
    try:
        with mock.patch.object(gmail_fetch, "_imap_connect",
                               side_effect=OSError("down")) as connect, \
             mock.patch("retryutil.sleep"):
            out = gmail_fetch.fetch_newsletter_stories()
        assert out == [], "total failure must stay non-fatal (newsletters skipped)"
        assert connect.call_count == config.GMAIL_IMAP_MAX_ATTEMPTS, \
            f"must retry to max ({config.GMAIL_IMAP_MAX_ATTEMPTS}), got {connect.call_count}"
    finally:
        _gmail_env_clear()


def test_imap_auth_error_not_retried():
    # imaplib.IMAP4.error = auth/server-side -> NOT OSError -> must NOT retry.
    import imaplib
    _gmail_env()
    try:
        with mock.patch.object(gmail_fetch, "_imap_connect",
                               side_effect=imaplib.IMAP4.error("AUTHENTICATIONFAILED")) as connect, \
             mock.patch("retryutil.sleep") as sl:
            out = gmail_fetch.fetch_newsletter_stories()
        assert out == []
        assert connect.call_count == 1, "auth error is permanent: one attempt only"
        assert sl.call_count == 0
    finally:
        _gmail_env_clear()


# ===========================================================================
# approve._announce_outcome — ambiguous surfaces "verify profile" wording
# ===========================================================================
def test_announce_unknown_vs_plain_failure():
    calls = []

    def fake_api(method, payload, token, **kw):
        calls.append((method, payload))
        return {"ok": True}

    with mock.patch.object(approve.telegram_api, "api_call", side_effect=fake_api):
        approve._announce_outcome(
            "TOK", "123", False, None,
            error=post_linkedin.UNKNOWN_OUTCOME_MARKER + " HTTP 503 (edge).")
        approve._announce_outcome(
            "TOK", "123", False, None, error="HTTP 401: unauthorized")
    sends = [p["text"] for m, p in calls if m == "sendMessage"]
    assert any("UNKNOWN" in t and "PROFILE" in t.upper() for t in sends), sends
    assert any("posting failed" in t.lower() for t in sends), sends


# ===========================================================================
if __name__ == "__main__":
    checks = [(n, v) for n, v in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in checks:
        run_check(name, fn)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
