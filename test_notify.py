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


def _stub_run(tmp, send_result, story_image=None, stories=None):
    """Run main.run() with fetch/generate/notify stubbed, real rank+history.
    # STEP [11] story_image controls the top story's image_url (None = no image).
    # STEP [17] stories overrides the single-story default (for image-fallback tests).
    # STEP [17] Returns (rc, pending, history, draft, send_mock)."""
    if stories is None:                                                   # STEP [17]
        story = {"source_id": 1, "source_name": "Test", "priority": 6,
                 "title": "Anthropic launches test thing", "link": "http://x",
                 "summary": "some summary",
                 "published": datetime.now(timezone.utc),
                 "image_url": story_image}                               # STEP [11]
        stories = [story]                                                 # STEP [17]
    draft = "Hook line under 140 chars.\n\nBody paragraph.\n\n#AI #LLM"
    hist_path = os.path.join(tmp, "history.json")
    pend_path = os.path.join(tmp, "pending_post.json")
    with mock.patch.object(main.fetch, "fetch_all", return_value=stories), \
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
    return rc, pending, history, draft, send                              # STEP [17]


def test_run_success_writes_awaiting_approval():
    with tempfile.TemporaryDirectory() as tmp:
        rc, pending, history, draft, _ = _stub_run(tmp, (True, 42))       # STEP [17]
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
        rc, pending, history, _, _ = _stub_run(tmp, (False, None))        # STEP [17]
    assert rc == 1, rc
    assert pending["status"] == "notify_failed", pending
    assert pending["telegram_message_id"] is None
    assert len(history["hashes"]) == 1, "notify failure must still record history"


# STEP [11] main.py carries the top story's image_url into pending_post.json.
# STEP [17] Now also verifies send_draft received the image_url (3rd positional arg).
def test_run_writes_image_url_from_top_story():
    with tempfile.TemporaryDirectory() as tmp:
        rc, pending, _, _, send = _stub_run(                              # STEP [17]
            tmp, (True, 42), story_image="http://example.com/a.jpg")
    assert rc == 0, rc
    assert pending["image_url"] == "http://example.com/a.jpg", pending
    assert pending["image_source"] == "story", pending
    assert send.call_args.args[2] == "http://example.com/a.jpg"           # STEP [17]


def test_run_no_image_writes_none():
    with tempfile.TemporaryDirectory() as tmp:
        rc, pending, _, _, send = _stub_run(tmp, (True, 42), story_image=None)  # STEP [17]
    assert rc == 0, rc
    assert pending["image_url"] is None, pending
    assert pending["image_source"] is None, pending
    assert send.call_args.args[2] is None                                 # STEP [17]


# STEP [17] Image fallback: when the #1 story has no image but a lower-ranked
# STEP [17] story does, main.py picks the first available image from the top 5.
def test_run_picks_image_from_lower_story_when_top_has_none():           # STEP [17]
    now = datetime.now(timezone.utc)
    stories = [
        {"source_id": 1, "source_name": "TopNoImg", "priority": 10,
         "title": "Top story has no image at all", "link": "http://a",
         "summary": "", "published": now, "image_url": None},
        {"source_id": 2, "source_name": "HasImg", "priority": 8,
         "title": "Second story has a great image", "link": "http://b",
         "summary": "", "published": now, "image_url": "http://example.com/b.jpg"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        rc, pending, _, _, send = _stub_run(tmp, (True, 42), stories=stories)
    assert rc == 0, rc
    assert pending["image_url"] == "http://example.com/b.jpg", pending   # STEP [17]
    assert pending["image_source"] == "story", pending                   # STEP [17]
    assert send.call_args.args[2] == "http://example.com/b.jpg"          # STEP [17]


# STEP [17] sendPhoto preview tests (no network — requests is mocked). _send_photo
# STEP [17] downloads the image (requests.get) then uploads as multipart (requests.post),
# STEP [17] so both must be mocked.
def _img_response(data=b"\xff\xd8\xff fakejpeg", status=200):             # STEP [17]
    r = mock.Mock()
    r.status_code = status
    r.content = data
    return r


def test_send_draft_with_image_sends_photo_first():                      # STEP [17]
    photo_resp = _ok_response(41)
    msg_resp = _ok_response(42)
    with mock.patch.dict(os.environ, FAKE_ENV), \
         mock.patch.object(notify.requests, "get", return_value=_img_response()) as get, \
         mock.patch.object(notify.requests, "post",
                           side_effect=[photo_resp, msg_resp]) as post:
        ok, mid = notify.send_draft("draft body.", [], "http://example.com/img.jpg")
    assert (ok, mid) == (True, 42), (ok, mid)  # returns TEXT msg id (with buttons)
    # Image was downloaded then uploaded via sendPhoto
    assert get.call_count == 1                                           # STEP [17]
    assert "/sendPhoto" in post.call_args_list[0].args[0]
    assert "photo" in post.call_args_list[0].kwargs["files"]             # STEP [17] multipart
    # Then sendMessage with buttons — URL NOT in text (photo succeeded)
    assert "/sendMessage" in post.call_args_list[1].args[0]
    assert "http://example.com/img.jpg" not in post.call_args_list[1].kwargs["json"]["text"]


def test_send_draft_photo_failure_includes_url_in_text():                # STEP [17]
    msg_resp = _ok_response(42)
    with mock.patch.dict(os.environ, FAKE_ENV), \
         mock.patch.object(notify.requests, "get",
                           return_value=_img_response(status=404, data=b"")) as get, \
         mock.patch.object(notify.requests, "post", return_value=msg_resp) as post:
        ok, mid = notify.send_draft("draft body.", [], "http://example.com/img.jpg")
    assert (ok, mid) == (True, 42), (ok, mid)
    # Download 404 → no sendPhoto POST, only sendMessage
    assert get.call_count == 1                                           # STEP [17]
    assert post.call_count == 1, "download failed → must skip sendPhoto POST"  # STEP [17]
    assert "/sendMessage" in post.call_args.args[0]
    # URL appears in text as a tappable fallback
    assert "http://example.com/img.jpg" in post.call_args.kwargs["json"]["text"]


def test_send_draft_no_image_skips_photo():                              # STEP [17]
    with mock.patch.dict(os.environ, FAKE_ENV), \
         mock.patch.object(notify.requests, "get") as get, \
         mock.patch.object(notify.requests, "post",
                           return_value=_ok_response(42)) as post:
        ok, mid = notify.send_draft("draft body.", [])
    assert (ok, mid) == (True, 42), (ok, mid)
    assert get.call_count == 0, "no image_url → must not download anything"  # STEP [17]
    assert post.call_count == 1, "no image_url → must not call sendPhoto"
    assert "/sendMessage" in post.call_args.args[0]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} checks passed.")
