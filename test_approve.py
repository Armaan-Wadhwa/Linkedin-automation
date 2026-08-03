"""
test_approve.py — stubbed checks for Phase 2 Task 6 + Task 7 (Run B: approval
poller that posts to LinkedIn on a winning ✅).

No network, no real files: telegram_api AND post_linkedin are mocked, the state
file lives in a temp dir. Run from the repo root:  python test_approve.py
"""

import io
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import approve        # noqa: E402
import config         # noqa: E402
import telegram_api   # noqa: E402

CHAT_ID = "1101049203"      # str, as os.environ gives it
CHAT_ID_INT = 1101049203    # int, as Telegram sends it
MSG_ID = 42


def _callback(update_id, data, chat=CHAT_ID_INT, sender=None, message_id=MSG_ID):
    return {"update_id": update_id,
            "callback_query": {"id": f"cb{update_id}", "data": data,
                               "from": {"id": CHAT_ID_INT if sender is None else sender},
                               "message": {"message_id": message_id,
                                           "chat": {"id": chat}}}}


def _photo_msg(update_id, file_id="large_fid", chat=CHAT_ID_INT, sender=None,
               date=None):                                            # STEP [11]
    """A Telegram photo message update (mimics getUpdates format)."""
    if date is None:
        date = int(datetime.now(timezone.utc).timestamp())
    sender_id = CHAT_ID_INT if sender is None else sender
    photo = [{"file_id": f"fid_{w}", "file_unique_id": f"u{w}",
              "width": w, "height": w, "file_size": w * 10}
             for w in (320, 640, 1280)]
    photo[-1]["file_id"] = file_id   # largest photo carries the named id
    return {"update_id": update_id,
            "message": {"message_id": 900 + update_id,
                        "from": {"id": sender_id},
                        "chat": {"id": chat},
                        "date": date,
                        "photo": photo}}


_UNSET = object()   # so created=None means "literally null", not "use default"


def _state(status="awaiting_approval", age_h=1, message_id=MSG_ID, created=_UNSET,
           draft="The draft text."):
    if created is _UNSET:
        created = (datetime.now(timezone.utc) - timedelta(hours=age_h)).isoformat()
    return {"draft": draft, "created_utc": created,
            "telegram_message_id": message_id, "status": status}


def _run(tmp, state, updates=(), raise_on=None,
         post_result=(True, "urn:li:share:1", False)):               # STEP [12]
    """Run approve.run() against a temp state file with getUpdates + post stubbed.
    Returns (exit_code, state_dict_or_None, telegram_calls, post_calls).

    # STEP [12] post_result is a 3-tuple (ok, result, image_attached)."""
    path = os.path.join(tmp, "pending_post.json")
    if state is not None:
        with open(path, "w", encoding="utf-8") as fh:
            if isinstance(state, str):
                fh.write(state)          # raw text, for the corrupt-file case
            else:
                json.dump(state, fh)
    calls = []
    post_calls = []

    def fake_api(method, payload, token, **kwargs):
        calls.append((method, payload))
        if raise_on and method == raise_on:
            raise RuntimeError("boom")
        if method == "getUpdates":
            # STEP [18] confirm call (offset > 0) returns no new updates
            if payload.get("offset", 0) > 0:
                return []
            return list(updates)
        return {"ok": True}

    def fake_post(text, image_ref=None):                              # STEP [12]
        post_calls.append(text)
        return post_result

    env = {config.TELEGRAM_TOKEN_ENV: "TESTTOKEN", config.TELEGRAM_CHAT_ID_ENV: CHAT_ID}
    with mock.patch.dict(os.environ, env), \
         mock.patch.object(approve, "PENDING_POST_PATH", path), \
         mock.patch.object(approve.telegram_api, "api_call", side_effect=fake_api), \
         mock.patch.object(approve.post_linkedin, "post", side_effect=fake_post):
        rc = approve.run()
    saved = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
        except json.JSONDecodeError:
            saved = "CORRUPT"
    return rc, saved, calls, post_calls


def _read(tmp):
    with open(os.path.join(tmp, "pending_post.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_missing_file_is_success():
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, calls, post_calls = _run(tmp, None)
    assert rc == 0, rc
    assert saved is None
    assert calls == [], "must not touch Telegram with no draft"
    assert post_calls == []


def test_terminal_statuses_do_nothing():
    # 'approved' is NOT in this list — Task 7 makes it the retryable/posting
    # state. 'posted' and 'post_failed' are the new terminals.
    for status in ("superseded", "notify_failed", "rejected", "expired",
                   "posted", "post_failed"):
        with tempfile.TemporaryDirectory() as tmp:
            rc, saved, calls, post_calls = _run(tmp, _state(status=status))
        assert rc == 0, (status, rc)
        assert saved["status"] == status, saved
        assert calls == [], f"{status} must not call Telegram"
        assert post_calls == [], f"{status} must not post"


def test_corrupt_file_exits_nonzero():
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, calls, post_calls = _run(tmp, "{not json at all")
    assert rc == 1, rc
    assert calls == []
    assert post_calls == []


def test_expired_draft_is_marked_expired():
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, calls, post_calls = _run(tmp, _state(age_h=config.APPROVAL_EXPIRY_H + 1))
    assert rc == 0, rc
    assert saved["status"] == "expired", saved
    assert calls == [], "expired path must not need a token"
    assert post_calls == []


def test_malformed_and_naive_created_utc_expire():
    for created in ("not-a-date", None,
                    datetime.now(timezone.utc).replace(tzinfo=None).isoformat()):
        with tempfile.TemporaryDirectory() as tmp:
            rc, saved, _, _ = _run(tmp, _state(created=created))
        assert rc == 0, (created, rc)
        assert saved["status"] == "expired", (created, saved)


def test_non_int_message_id_is_corrupt():
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, calls, post_calls = _run(tmp, _state(message_id=None))
    assert rc == 1, rc
    assert saved["status"] == "awaiting_approval", "must not be made approvable"
    assert calls == []
    assert post_calls == []


def test_approve_happy_path_transitions_to_posted():
    # Task 7: a winning ✅ now posts in the same run, so the terminal state is
    # 'posted' (not 'approved'). The 'approved' state is an in-run waypoint only.
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, calls, post_calls = _run(tmp, _state(), [_callback(1, "approve")])
    assert rc == 0, rc
    assert saved["status"] == "posted", saved
    assert saved["linkedin_post_id"] == "urn:li:share:1", saved
    assert saved["callback_update_id"] == 1
    assert saved["draft"] == "The draft text.", "draft must survive the post"
    datetime.fromisoformat(saved["decided_utc"])
    datetime.fromisoformat(saved["posted_utc"])
    assert "from" not in json.dumps(saved), "must not leak sender identity"
    assert len(post_calls) == 1
    assert post_calls[0] == "The draft text."
    # Buttons stripped immediately (skip_message=True on approve)
    edits = [p for m, p in calls if m == "editMessageReplyMarkup"]
    assert edits[0]["reply_markup"] == {"inline_keyboard": []}, edits
    # Outcome announced
    sends = [p["text"] for m, p in calls if m == "sendMessage"]
    assert any("Posted to LinkedIn" in t for t in sends), sends


def test_approve_post_failure_transitions_to_post_failed():
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, calls, post_calls = _run(
            tmp, _state(), [_callback(1, "approve")],
            post_result=(False, "HTTP 500: server error", False))
    assert rc == 1, "post_failed must go red"
    assert saved["status"] == "post_failed", saved
    assert "HTTP 500" in saved["post_error"], saved
    assert len(post_calls) == 1
    sends = [p["text"] for m, p in calls if m == "sendMessage"]
    assert any("failed" in t.lower() for t in sends), sends


def test_reject_happy_path():
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, calls, post_calls = _run(tmp, _state(), [_callback(1, "reject")])
    assert rc == 0, rc
    assert saved["status"] == "rejected", saved
    assert post_calls == [], "reject must never post"


def test_reject_wins_both_orders():
    # A fat-finger approve-then-reject correction must never post.
    for updates in ([_callback(1, "approve"), _callback(2, "reject")],
                    [_callback(1, "reject"), _callback(2, "approve")]):
        with tempfile.TemporaryDirectory() as tmp:
            rc, saved, _, post_calls = _run(tmp, _state(), updates)
        assert rc == 0, rc
        assert saved["status"] == "rejected", (updates, saved)
        assert post_calls == [], (updates, "reject-wins must never post")


def test_int_chat_id_matches_str_env():
    # Telegram sends int, env is str; 123 == "123" is False. Regression guard:
    # getting this wrong makes the bot silently never approve anything.
    # (Task 7: approve→posted, so the assertion is 'posted' now.)
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, _, _ = _run(tmp, _state(), [_callback(1, "approve", chat=CHAT_ID_INT)])
    assert saved["status"] == "posted", saved


def test_foreign_callbacks_are_ignored():
    cases = {"wrong chat": _callback(1, "approve", chat=999),
             "wrong sender": _callback(2, "approve", sender=999),
             "wrong message": _callback(3, "approve", message_id=999),
             "unknown data": _callback(4, "cancel")}
    for name, upd in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            rc, saved, _, post_calls = _run(tmp, _state(), [upd])
        assert rc == 0, (name, rc)
        assert saved["status"] == "awaiting_approval", (name, saved)
        assert "decided_utc" not in saved, name
        assert post_calls == [], (name, "ignored callback must not post")


def test_unreachable_telegram_leaves_draft_pending():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "pending_post.json")
        state = _state()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        before = open(path, "rb").read()
        env = {config.TELEGRAM_TOKEN_ENV: "T", config.TELEGRAM_CHAT_ID_ENV: CHAT_ID}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(approve, "PENDING_POST_PATH", path), \
             mock.patch.object(approve.telegram_api, "api_call", return_value=None):
            rc = approve.run()
        after = open(path, "rb").read()
    assert rc == 0, "a transient outage must not go red or become terminal"
    assert before == after, "state file must be byte-unchanged"


def test_confirm_failure_does_not_revert_decision():
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, calls, _ = _run(tmp, _state(), [_callback(1, "approve")],
                                   raise_on="answerCallbackQuery")
    # answerCallbackQuery failing must NOT block the post (decision is source of
    # truth); the post still succeeds and the outcome still gets announced.
    assert rc == 0, rc
    assert saved["status"] == "posted", "post must complete despite ack failure"
    methods = [m for m, _ in calls]
    assert "editMessageReplyMarkup" in methods, "buttons still stripped"
    assert "sendMessage" in methods, "outcome still announced"


def test_missing_credentials_exits_nonzero():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "pending_post.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_state(), fh)
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(approve, "PENDING_POST_PATH", path):
            rc = approve.run()
    assert rc == 1, "a missing secret must be loud, not a silent no-op"


def test_post_re_read_guard_rejects_non_approved():
    # The ordering-hazard guard: _post_approved_draft re-reads the state file and
    # refuses if status != "approved". Tested directly so we control the on-disk
    # status at re-read time. A 'rejected' file must NOT be posted.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "pending_post.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_state(status="rejected"), fh)
        env = {config.TELEGRAM_TOKEN_ENV: "T", config.TELEGRAM_CHAT_ID_ENV: CHAT_ID}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(approve, "PENDING_POST_PATH", path), \
             mock.patch.object(approve.post_linkedin, "post") as fake_post:
            ok, post_id, err, _, _ = approve._post_approved_draft()   # STEP [12]
        assert ok is False, ok
        assert "rejected" in err, err
        fake_post.assert_not_called()
        assert _read(tmp)["status"] == "post_failed", "refusal recorded as post_failed"


def test_retry_on_approved_status():
    # A prior run recorded 'approved' but crashed before posting (or its save of
    # post_failed also failed). This run picks up 'approved' and retries the post
    # WITHOUT rescanning Telegram.
    state = _state(status="approved", draft="Previously approved draft.")
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, calls, post_calls = _run(tmp, state, updates=())
    assert rc == 0, rc
    assert saved["status"] == "posted", saved
    assert saved["linkedin_post_id"] == "urn:li:share:1"
    assert len(post_calls) == 1
    assert post_calls[0] == "Previously approved draft."


def test_token_age_warning_appended_on_success():
    with mock.patch.object(approve.token_status, "warn_if_stale",
                           return_value="⚠ re-auth soon"):
        with tempfile.TemporaryDirectory() as tmp:
            rc, saved, calls, _ = _run(tmp, _state(), [_callback(1, "approve")])
    assert rc == 0, rc
    sends = [p["text"] for m, p in calls if m == "sendMessage"]
    assert any("Posted to LinkedIn" in t for t in sends), sends
    assert any("re-auth soon" in t for t in sends), "nudge must be appended"


def test_api_call_never_leaks_token():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    telegram_api.log.addHandler(handler)
    telegram_api.log.setLevel(logging.DEBUG)
    try:
        boom = telegram_api.requests.exceptions.ConnectionError(
            "HTTPSConnectionPool: /botSECRET123/getUpdates refused")
        with mock.patch.object(telegram_api.requests, "post", side_effect=boom):
            result = telegram_api.api_call("getUpdates", {}, "SECRET123")
    finally:
        telegram_api.log.removeHandler(handler)
    assert result is None
    logged = buf.getvalue()
    assert "SECRET123" not in logged, "token leaked into logs!"
    assert "<token>" in logged


# --- STEP [11] photo override tests ----------------------------------------

def _run_with_photo(tmp, state_dict, updates, getfile_result=None,
                    download_bytes=b"\x89PNGimg", post_result=(True, "urn:li:share:1", False)):  # STEP [12]
    """Run approve.run() with photo-related stubs. Returns (rc, saved, api_calls).
    # STEP [11] Standalone helper (not _run) because photo tests need getFile +
    # STEP [11] download_file mocks that _run doesn't provide."""
    path = os.path.join(tmp, "pending_post.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state_dict, fh)
    calls = []

    def fake_api(method, payload, token, **kw):
        calls.append((method, payload))                              # STEP [12]
        if method == "getUpdates":
            if payload.get("offset", 0) > 0:                        # STEP [18] confirm
                return []
            return list(updates)
        if method == "getFile":
            return getfile_result
        return {"ok": True}

    env = {config.TELEGRAM_TOKEN_ENV: "TESTTOKEN", config.TELEGRAM_CHAT_ID_ENV: CHAT_ID}
    with mock.patch.dict(os.environ, env), \
         mock.patch.object(approve, "PENDING_POST_PATH", path), \
         mock.patch.object(approve.telegram_api, "api_call", side_effect=fake_api), \
         mock.patch.object(approve.telegram_api, "download_file",
                           return_value=download_bytes), \
         mock.patch.object(approve.post_linkedin, "post", return_value=post_result):
        rc = approve.run()
    with open(path, encoding="utf-8") as fh:
        saved = json.load(fh)
    return rc, saved, calls


def test_photo_override_beats_story_image():
    with tempfile.TemporaryDirectory() as tmp:
        state = _state()
        state["image_url"] = "http://story.example/img.jpg"
        state["image_source"] = "story"
        rc, saved, calls = _run_with_photo(
            tmp, state,
            [_photo_msg(10), _callback(11, "approve")],
            getfile_result={"file_path": "photos/t.jpg", "file_size": 50000})
    assert rc == 0, rc
    assert saved["image_source"] == "telegram_override", saved
    assert saved["image_url"].endswith(".jpg"), saved["image_url"]
    assert saved["image_file_id"] == "large_fid", saved
    assert saved["status"] == "posted", "approve+post must still complete"


def test_photo_from_wrong_chat_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        state = _state()
        state["image_url"] = "http://story.example/img.jpg"
        state["image_source"] = "story"
        rc, saved, _ = _run_with_photo(
            tmp, state,
            [_photo_msg(10, chat=999), _callback(11, "approve")],
            getfile_result={"file_path": "p/t.jpg", "file_size": 5000})
    assert rc == 0, rc
    assert saved["image_source"] == "story", "wrong-chat photo must not override"
    assert saved["image_url"] == "http://story.example/img.jpg"


def test_photo_before_draft_creation_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        state = _state()
        state["image_url"] = "http://story.example/img.jpg"
        state["image_source"] = "story"
        # Photo sent 10h ago; draft created 1h ago → photo predates draft
        old_ts = int((datetime.now(timezone.utc) - timedelta(hours=10)).timestamp())
        rc, saved, _ = _run_with_photo(
            tmp, state,
            [_photo_msg(10, date=old_ts), _callback(11, "approve")],
            getfile_result={"file_path": "p/t.jpg", "file_size": 5000})
    assert rc == 0, rc
    assert saved["image_source"] == "story", "pre-draft photo must not override"


def test_photo_download_failure_is_non_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        state = _state()
        state["image_url"] = "http://story.example/img.jpg"
        state["image_source"] = "story"
        rc, saved, _ = _run_with_photo(
            tmp, state,
            [_photo_msg(10), _callback(11, "approve")],
            getfile_result={"file_path": "p/t.jpg", "file_size": 5000},
            download_bytes=None)   # download fails
    assert rc == 0, "download failure must not crash the run"
    assert saved["image_source"] == "story", "story image preserved on dl failure"
    assert saved["status"] == "posted", "post must still succeed without image"


def test_no_photo_preserves_story_image():
    with tempfile.TemporaryDirectory() as tmp:
        state = _state()
        state["image_url"] = "http://story.example/img.jpg"
        state["image_source"] = "story"
        rc, saved, _ = _run_with_photo(
            tmp, state, [_callback(11, "approve")])
    assert rc == 0, rc
    assert saved["image_source"] == "story", "no photo → story image unchanged"
    assert saved["status"] == "posted"


def test_photo_without_approval_saves_state():
    # Photo found but no ✅ yet: state saved with override, status stays pending.
    with tempfile.TemporaryDirectory() as tmp:
        state = _state()
        state["image_url"] = "http://story.example/img.jpg"
        state["image_source"] = "story"
        rc, saved, _ = _run_with_photo(
            tmp, state,
            [_photo_msg(10)],   # photo only, no callback
            getfile_result={"file_path": "p/t.jpg", "file_size": 5000})
    assert rc == 0, rc
    assert saved["image_source"] == "telegram_override", saved
    assert saved["status"] == "awaiting_approval", "no ✅ → must not post"
    assert saved["image_file_id"] == "large_fid"


def test_photo_oversize_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        state = _state()
        state["image_url"] = "http://story.example/img.jpg"
        state["image_source"] = "story"
        rc, saved, _ = _run_with_photo(
            tmp, state,
            [_photo_msg(10), _callback(11, "approve")],
            getfile_result={"file_path": "p/huge.jpg",
                            "file_size": config.IMAGE_MAX_BYTES + 1})
    assert rc == 0, rc
    assert saved["image_source"] == "story", "oversize photo must not override"


# --- STEP [12] image_ref passing + announce tests --------------------------

def test_image_ref_passed_to_post_from_state():
    # _post_approved_draft builds image_ref from the re-read state and passes
    # it to post_linkedin.post(). Captures the call args.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "pending_post.json")
        state = _state(status="approved")
        state["image_url"] = "http://story.example/img.jpg"
        state["image_source"] = "story"
        state["draft"] = "A draft."
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        env = {config.TELEGRAM_TOKEN_ENV: "T", config.TELEGRAM_CHAT_ID_ENV: CHAT_ID}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(approve, "PENDING_POST_PATH", path), \
             mock.patch.object(approve.post_linkedin, "post",
                               return_value=(True, "urn:li:share:1", True)) as fp:
            approve._post_approved_draft()
        args, kwargs = fp.call_args
        assert args[0] == "A draft.", "draft text must be passed"
        assert args[1] == {"url": "http://story.example/img.jpg",
                           "source": "story"}, args                      # STEP [12]


def test_no_image_ref_when_state_has_none():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "pending_post.json")
        state = _state(status="approved", draft="A draft.")
        # no image_url / image_source keys
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        env = {config.TELEGRAM_TOKEN_ENV: "T", config.TELEGRAM_CHAT_ID_ENV: CHAT_ID}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(approve, "PENDING_POST_PATH", path), \
             mock.patch.object(approve.post_linkedin, "post",
                               return_value=(True, "urn:li:share:1", False)) as fp:
            approve._post_approved_draft()
        assert fp.call_args.args[1] is None                              # STEP [12]


def test_announce_says_with_image():
    with tempfile.TemporaryDirectory() as tmp:
        state = _state()
        state["image_url"] = "http://story.example/img.jpg"
        state["image_source"] = "story"
        rc, saved, calls = _run_with_photo(
            tmp, state, [_callback(11, "approve")],
            post_result=(True, "urn:li:share:1", True))   # image attached
    assert rc == 0, rc
    sends = [p["text"] for m, p in calls if m == "sendMessage"]
    assert any("with image" in t for t in sends), sends


def test_announce_says_text_only_when_image_unavailable():
    with tempfile.TemporaryDirectory() as tmp:
        state = _state()
        state["image_url"] = "http://story.example/img.jpg"
        state["image_source"] = "story"
        rc, saved, calls = _run_with_photo(
            tmp, state, [_callback(11, "approve")],
            post_result=(True, "urn:li:share:1", False))  # image attempted, failed
    assert rc == 0, rc
    sends = [p["text"] for m, p in calls if m == "sendMessage"]
    assert any("text-only" in t for t in sends), sends


def test_announce_no_image_mention_when_not_attempted():
    # No image_ref in state → announce must read exactly as Task 7 (no image note)
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, calls = _run_with_photo(
            tmp, _state(), [_callback(11, "approve")],
            post_result=(True, "urn:li:share:1", False))
    sends = [p["text"] for m, p in calls if m == "sendMessage"]
    assert any("Posted to LinkedIn" in t for t in sends), sends
    assert not any("image" in t.lower() for t in sends), "must not mention image"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} checks passed.")
