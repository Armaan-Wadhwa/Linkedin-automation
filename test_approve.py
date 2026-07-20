"""
test_approve.py — stubbed checks for Phase 2 Task 6 (Run B: approval poller).

No network, no real files: telegram_api is mocked and the state file lives in
a temp dir. Run from the repo root:  python test_approve.py
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


_UNSET = object()   # so created=None means "literally null", not "use default"


def _state(status="awaiting_approval", age_h=1, message_id=MSG_ID, created=_UNSET):
    if created is _UNSET:
        created = (datetime.now(timezone.utc) - timedelta(hours=age_h)).isoformat()
    return {"draft": "The draft text.", "created_utc": created,
            "telegram_message_id": message_id, "status": status}


def _run(tmp, state, updates=(), raise_on=None):
    """Run approve.run() against a temp state file with getUpdates stubbed.
    Returns (exit_code, state_dict_or_None, list_of_api_calls)."""
    path = os.path.join(tmp, "pending_post.json")
    if state is not None:
        with open(path, "w", encoding="utf-8") as fh:
            if isinstance(state, str):
                fh.write(state)          # raw text, for the corrupt-file case
            else:
                json.dump(state, fh)
    calls = []

    def fake_api(method, payload, token, **kwargs):
        calls.append((method, payload))
        if raise_on and method == raise_on:
            raise RuntimeError("boom")
        return list(updates) if method == "getUpdates" else {"ok": True}

    env = {config.TELEGRAM_TOKEN_ENV: "TESTTOKEN", config.TELEGRAM_CHAT_ID_ENV: CHAT_ID}
    with mock.patch.dict(os.environ, env), \
         mock.patch.object(approve, "PENDING_POST_PATH", path), \
         mock.patch.object(approve.telegram_api, "api_call", side_effect=fake_api):
        rc = approve.run()
    saved = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
        except json.JSONDecodeError:
            saved = "CORRUPT"
    return rc, saved, calls


def test_missing_file_is_success():
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, calls = _run(tmp, None)
    assert rc == 0, rc
    assert saved is None
    assert calls == [], "must not touch Telegram with no draft"


def test_terminal_statuses_do_nothing():
    for status in ("superseded", "notify_failed", "approved", "rejected", "expired"):
        with tempfile.TemporaryDirectory() as tmp:
            rc, saved, calls = _run(tmp, _state(status=status))
        assert rc == 0, (status, rc)
        assert saved["status"] == status, saved
        assert calls == [], f"{status} must not call Telegram"


def test_corrupt_file_exits_nonzero():
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, calls = _run(tmp, "{not json at all")
    assert rc == 1, rc
    assert calls == []


def test_expired_draft_is_marked_expired():
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, calls = _run(tmp, _state(age_h=config.APPROVAL_EXPIRY_H + 1))
    assert rc == 0, rc
    assert saved["status"] == "expired", saved
    assert calls == [], "expired path must not need a token"


def test_malformed_and_naive_created_utc_expire():
    for created in ("not-a-date", None,
                    datetime.now(timezone.utc).replace(tzinfo=None).isoformat()):
        with tempfile.TemporaryDirectory() as tmp:
            rc, saved, _ = _run(tmp, _state(created=created))
        assert rc == 0, (created, rc)
        assert saved["status"] == "expired", (created, saved)


def test_non_int_message_id_is_corrupt():
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, calls = _run(tmp, _state(message_id=None))
    assert rc == 1, rc
    assert saved["status"] == "awaiting_approval", "must not be made approvable"
    assert calls == []


def test_approve_happy_path():
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, calls = _run(tmp, _state(), [_callback(1, "approve")])
    assert rc == 0, rc
    assert saved["status"] == "approved", saved
    assert saved["callback_update_id"] == 1
    assert saved["draft"] == "The draft text.", "draft must survive for Task 7"
    datetime.fromisoformat(saved["decided_utc"])
    assert "from" not in json.dumps(saved), "must not leak sender identity"
    edits = [p for m, p in calls if m == "editMessageReplyMarkup"]
    assert edits[0]["reply_markup"] == {"inline_keyboard": []}, edits


def test_reject_happy_path():
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, _ = _run(tmp, _state(), [_callback(1, "reject")])
    assert rc == 0, rc
    assert saved["status"] == "rejected", saved


def test_reject_wins_both_orders():
    # A fat-finger approve-then-reject correction must never post.
    for updates in ([_callback(1, "approve"), _callback(2, "reject")],
                    [_callback(1, "reject"), _callback(2, "approve")]):
        with tempfile.TemporaryDirectory() as tmp:
            rc, saved, _ = _run(tmp, _state(), updates)
        assert rc == 0, rc
        assert saved["status"] == "rejected", (updates, saved)


def test_int_chat_id_matches_str_env():
    # Telegram sends int, env is str; 123 == "123" is False. Regression guard:
    # getting this wrong makes the bot silently never approve anything.
    with tempfile.TemporaryDirectory() as tmp:
        rc, saved, _ = _run(tmp, _state(), [_callback(1, "approve", chat=CHAT_ID_INT)])
    assert saved["status"] == "approved", saved


def test_foreign_callbacks_are_ignored():
    cases = {"wrong chat": _callback(1, "approve", chat=999),
             "wrong sender": _callback(2, "approve", sender=999),
             "wrong message": _callback(3, "approve", message_id=999),
             "unknown data": _callback(4, "cancel")}
    for name, upd in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            rc, saved, _ = _run(tmp, _state(), [upd])
        assert rc == 0, (name, rc)
        assert saved["status"] == "awaiting_approval", (name, saved)
        assert "decided_utc" not in saved, name


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
        rc, saved, calls = _run(tmp, _state(), [_callback(1, "approve")],
                                raise_on="answerCallbackQuery")
    assert rc == 0, rc
    assert saved["status"] == "approved", "decision is the source of truth"
    # Each confirmation is guarded individually: a failing answerCallbackQuery
    # must NOT skip the button removal, which is what Harvey actually sees.
    methods = [m for m, _ in calls]
    assert "editMessageReplyMarkup" in methods, methods
    assert "sendMessage" in methods, methods


def test_missing_credentials_exits_nonzero():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "pending_post.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_state(), fh)
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(approve, "PENDING_POST_PATH", path):
            rc = approve.run()
    assert rc == 1, "a missing secret must be loud, not a silent no-op"


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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} checks passed.")
