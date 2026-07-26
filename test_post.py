"""
test_post.py — stubbed checks for Phase 2 Task 7 (LinkedIn posting + token age).

No network, no real files: requests is mocked, env is patched via mock.dict.
Run from the repo root:  python test_post.py
"""

import io
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import config         # noqa: E402
import post_linkedin  # noqa: E402
import token_status   # noqa: E402

TOKEN = "SECRET-TOKEN-123"
URN = "urn:li:person:ABC123"
POST_ID = "urn:li:share:987654321"

FAKE_ENV = {
    config.LINKEDIN_TOKEN_ENV: TOKEN,
    config.LINKEDIN_PERSON_URN_ENV: URN,
}


def _resp(status_code=201, post_id=POST_ID, text=""):
    r = mock.Mock()
    r.status_code = status_code
    r.headers = {}
    if post_id is not None:
        r.headers["x-restli-id"] = post_id
    r.text = text
    return r


# --- post_linkedin.post ----------------------------------------------------

def test_post_201_captures_id():
    with mock.patch.dict(os.environ, FAKE_ENV), \
         mock.patch.object(post_linkedin.requests, "post",
                           return_value=_resp(201, POST_ID)) as posted:
        ok, result = post_linkedin.post("draft body")
    assert ok is True, (ok, result)
    assert result == POST_ID, result
    # Verify the official Posts API contract (author, visibility, distribution…)
    assert posted.call_args.args[0] == config.LINKEDIN_POSTS_URL
    body = posted.call_args.kwargs["json"]
    assert body["author"] == URN
    assert body["commentary"] == "draft body"
    assert body["visibility"] == "PUBLIC"
    assert body["distribution"]["feedDistribution"] == "MAIN_FEED"
    assert body["distribution"]["targetEntities"] == []
    assert body["distribution"]["thirdPartyDistributionChannels"] == []
    assert body["lifecycleState"] == "PUBLISHED"
    assert body["isReshareDisabledByAuthor"] is False
    headers = posted.call_args.kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert headers["LinkedIn-Version"] == config.LINKEDIN_VERSION


def test_post_non_201_is_failure():
    with mock.patch.dict(os.environ, FAKE_ENV), \
         mock.patch.object(post_linkedin.requests, "post",
                           return_value=_resp(400, None, '{"message":"bad request"}')):
        ok, result = post_linkedin.post("draft")
    assert ok is False, (ok, result)
    assert "HTTP 400" in result, result
    assert "bad request" in result


def test_post_201_without_id_is_failure():
    # API contract violation: 201 but no x-restli-id — must not claim success
    # with a None we'd persist as the linkedin_post_id.
    with mock.patch.dict(os.environ, FAKE_ENV), \
         mock.patch.object(post_linkedin.requests, "post",
                           return_value=_resp(201, post_id=None)):
        ok, result = post_linkedin.post("draft")
    assert ok is False, (ok, result)
    assert "x-restli-id" in result, result


def test_post_missing_env():
    with mock.patch.dict(os.environ, {}, clear=True), \
         mock.patch.object(post_linkedin.requests, "post") as posted:
        ok, result = post_linkedin.post("draft")
    assert ok is False, (ok, result)
    assert "not set" in result, result
    posted.assert_not_called()


def test_post_empty_text_is_failure():
    with mock.patch.dict(os.environ, FAKE_ENV), \
         mock.patch.object(post_linkedin.requests, "post") as posted:
        ok, result = post_linkedin.post("")
    assert ok is False, (ok, result)
    posted.assert_not_called()


def test_post_dry_run_no_api_call():
    env = dict(FAKE_ENV)
    env[config.LINKEDIN_DRY_RUN_ENV] = "1"
    with mock.patch.dict(os.environ, env), \
         mock.patch.object(post_linkedin.requests, "post") as posted:
        ok, result = post_linkedin.post("dry-run draft")
    assert ok is True, (ok, result)
    assert result.startswith("DRY_RUN_"), result
    posted.assert_not_called()


def test_post_scrubs_token_from_exception():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    post_linkedin.log.addHandler(handler)
    post_linkedin.log.setLevel(logging.DEBUG)
    try:
        boom = post_linkedin.requests.exceptions.ConnectionError(
            f"HTTPSConnectionPool host=api.linkedin.com Bearer {TOKEN} refused")
        with mock.patch.dict(os.environ, FAKE_ENV), \
             mock.patch.object(post_linkedin.requests, "post", side_effect=boom):
            ok, result = post_linkedin.post("draft")
    finally:
        post_linkedin.log.removeHandler(handler)
    assert ok is False, (ok, result)
    assert TOKEN not in result, f"token leaked into returned error: {result}"
    assert TOKEN not in buf.getvalue(), "token leaked into logs!"


def test_post_never_raises_on_timeout():
    boom = post_linkedin.requests.exceptions.Timeout("connection timed out")
    with mock.patch.dict(os.environ, FAKE_ENV), \
         mock.patch.object(post_linkedin.requests, "post", side_effect=boom):
        ok, result = post_linkedin.post("draft")   # must NOT raise
    assert ok is False, (ok, result)
    assert "timed out" in result or "Timeout" in result, result


# --- token_status ----------------------------------------------------------

def _issued(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_days_old_missing_returns_none():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert token_status.days_old() is None


def test_days_old_returns_int():
    with mock.patch.dict(os.environ, {config.LINKEDIN_TOKEN_ISSUED_UTC_ENV: _issued(10)}):
        age = token_status.days_old()
    assert age == 10, age


def test_days_old_malformed_returns_none():
    with mock.patch.dict(os.environ, {config.LINKEDIN_TOKEN_ISSUED_UTC_ENV: "not-a-date"}):
        assert token_status.days_old() is None


def test_days_old_naive_returns_none():
    naive = (datetime.now(timezone.utc) - timedelta(days=10)).replace(tzinfo=None).isoformat()
    with mock.patch.dict(os.environ, {config.LINKEDIN_TOKEN_ISSUED_UTC_ENV: naive}):
        assert token_status.days_old() is None


def test_warn_if_stale_fires_past_threshold():
    with mock.patch.dict(os.environ,
                         {config.LINKEDIN_TOKEN_ISSUED_UTC_ENV:
                          _issued(config.LINKEDIN_TOKEN_WARN_DAYS + 1)}):
        msg = token_status.warn_if_stale()
    assert msg is not None, msg
    assert "re-auth" in msg.lower() or "expir" in msg.lower(), msg


def test_warn_if_stale_silent_when_fresh():
    with mock.patch.dict(os.environ, {config.LINKEDIN_TOKEN_ISSUED_UTC_ENV: _issued(5)}):
        assert token_status.warn_if_stale() is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} checks passed.")
