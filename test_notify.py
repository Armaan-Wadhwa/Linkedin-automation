"""
test_notify.py — stubbed checks for Phase 2 Task 5 (Run A: notify step).

No network, no real files touched: requests is mocked, history/pending paths
point at a temp dir. Run from the repo root:  python test_notify.py
"""

import contextlib
import io
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import config    # noqa: E402
import main      # noqa: E402
import notify    # noqa: E402

FAKE_ENV = {config.TELEGRAM_TOKEN_ENV: "TESTTOKEN123",
            config.TELEGRAM_CHAT_ID_ENV: "123"}


def _ok_response(message_id=42):
    resp = mock.Mock()
    resp.status_code = 200
    resp.json.return_value = {"ok": True, "result": {"message_id": message_id}}
    return resp


def test_send_draft_success():
    with mock.patch.dict(os.environ, FAKE_ENV), \
         mock.patch.object(notify.requests, "post",
                           return_value=_ok_response()) as post:
        ok, mid = notify.send_draft("My draft body.", ["w1", "w2"])
    assert (ok, mid) == (True, 42), (ok, mid)
    payload = post.call_args.kwargs["json"]
    assert payload["chat_id"] == "123"
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["approve", "reject"], buttons
    assert "⚠ checks: w1; w2" in payload["text"]
    assert "My draft body." in payload["text"]
    assert "TESTTOKEN123" in post.call_args.args[0]  # token in URL, not payload


def test_send_draft_truncates_preview():
    with mock.patch.dict(os.environ, FAKE_ENV), \
         mock.patch.object(notify.requests, "post",
                           return_value=_ok_response()) as post:
        ok, _ = notify.send_draft("x" * 6000, [])
    assert ok
    text = post.call_args.kwargs["json"]["text"]
    assert len(text) <= config.TELEGRAM_MSG_LIMIT, len(text)
    assert "preview truncated" in text


def test_send_draft_failure_never_raises_or_leaks_token():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    notify.log.addHandler(handler)
    notify.log.setLevel(logging.DEBUG)
    try:
        boom = notify.requests.exceptions.ConnectionError(
            "HTTPSConnectionPool: /botTESTTOKEN123/sendMessage refused")
        with mock.patch.dict(os.environ, FAKE_ENV), \
             mock.patch.object(notify.requests, "post", side_effect=boom):
            ok, mid = notify.send_draft("draft", [])
    finally:
        notify.log.removeHandler(handler)
    assert (ok, mid) == (False, None), (ok, mid)
    logged = buf.getvalue()
    assert "TESTTOKEN123" not in logged, "token leaked into logs!"
    assert "<token>" in logged


def test_send_draft_missing_env():
    with mock.patch.dict(os.environ, {}, clear=True), \
         mock.patch.object(notify.requests, "post") as post:
        ok, mid = notify.send_draft("draft", [])
    assert (ok, mid) == (False, None), (ok, mid)
    post.assert_not_called()


def _stub_run(tmp, send_result, story_image=None):
    """Run main.run() with fetch/generate/notify stubbed, real rank+history.
    # STEP [11] story_image controls the top story's image_url (None = no image)."""
    story = {"source_id": 1, "source_name": "Test", "priority": 6,
             "title": "Anthropic launches test thing", "link": "http://x",
             "summary": "some summary",
             "published": datetime.now(timezone.utc),
             "image_url": story_image}                               # STEP [11]
    draft = "Hook line under 140 chars.\n\nBody paragraph.\n\n#AI #LLM"
    hist_path = os.path.join(tmp, "history.json")
    pend_path = os.path.join(tmp, "pending_post.json")
    with mock.patch.object(main.fetch, "fetch_all", return_value=[story]), \
         mock.patch.object(main.generate, "generate_post", return_value=draft), \
         mock.patch.object(main.notify, "send_draft",
                           return_value=send_result) as send, \
         mock.patch.object(main, "HISTORY_PATH", hist_path), \
         mock.patch.object(main, "PENDING_POST_PATH", pend_path), \
         contextlib.redirect_stdout(io.StringIO()):
        rc = main.run()
    send.assert_called_once()
    assert send.call_args.args[0] == draft
    with open(pend_path, encoding="utf-8") as fh:
        pending = json.load(fh)
    with open(hist_path, encoding="utf-8") as fh:
        history = json.load(fh)
    return rc, pending, history, draft


def test_run_success_writes_awaiting_approval():
    with tempfile.TemporaryDirectory() as tmp:
        rc, pending, history, draft = _stub_run(tmp, (True, 42))
    assert rc == 0, rc
    assert pending["status"] == "awaiting_approval", pending
    assert pending["draft"] == draft
    assert pending["telegram_message_id"] == 42
    datetime.fromisoformat(pending["created_utc"])  # parses, tz-aware ISO
    assert len(history["hashes"]) == 1, history


def test_run_early_failure_supersedes_stale_pending():
    # Day 1 committed an "awaiting_approval" file; Day 2 fails at fetch.
    # The stale file must become terminal, never stay approvable.
    with tempfile.TemporaryDirectory() as tmp:
        pend_path = os.path.join(tmp, "pending_post.json")
        with open(pend_path, "w", encoding="utf-8") as fh:
            json.dump({"draft": "old draft", "telegram_message_id": 7,
                       "status": "awaiting_approval"}, fh)
        with mock.patch.object(main.fetch, "fetch_all", return_value=[]), \
             mock.patch.object(main, "HISTORY_PATH",
                               os.path.join(tmp, "history.json")), \
             mock.patch.object(main, "PENDING_POST_PATH", pend_path), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = main.run()
        assert rc == 1, rc
        with open(pend_path, encoding="utf-8") as fh:
            pending = json.load(fh)
    assert pending == {"status": "superseded"}, pending


def test_run_notify_failure_goes_red_but_records_history():
    with tempfile.TemporaryDirectory() as tmp:
        rc, pending, history, _ = _stub_run(tmp, (False, None))
    assert rc == 1, rc
    assert pending["status"] == "notify_failed", pending
    assert pending["telegram_message_id"] is None
    assert len(history["hashes"]) == 1, "notify failure must still record history"


# STEP [11] main.py carries the top story's image_url into pending_post.json.
def test_run_writes_image_url_from_top_story():
    with tempfile.TemporaryDirectory() as tmp:
        rc, pending, _, _ = _stub_run(tmp, (True, 42),
                                      story_image="http://example.com/a.jpg")
    assert rc == 0, rc
    assert pending["image_url"] == "http://example.com/a.jpg", pending
    assert pending["image_source"] == "story", pending


def test_run_no_image_writes_none():
    with tempfile.TemporaryDirectory() as tmp:
        rc, pending, _, _ = _stub_run(tmp, (True, 42), story_image=None)
    assert rc == 0, rc
    assert pending["image_url"] is None, pending
    assert pending["image_source"] is None, pending


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} checks passed.")
