"""
test_main.py — Offline checks for the once-a-day guard (STEP 31).           # STEP [31]

NO NETWORK. fetch.fetch_all is mocked; pending_post.json lives in a temp dir.
Run from the repo root:  python test_main.py

Why this file exists: daily.yml now fires FOUR morning crons because GitHub
drops most scheduled events on this repo. already_ran_today() is the ONLY thing
stopping the other three from publishing a second digest, so it gets a test.

  1-8  already_ran_today: today / yesterday / missing / corrupt / no stamp /
       naive stamp / non-dict / any status counts
  9    the guard runs BEFORE supersede_pending — the ordering trap
  10   guard fires -> exit 0, fetch_all NOT called
  11   --force bypasses the guard
  12   yesterday's draft -> guard does not fire, run() proceeds
"""

import json                                                            # STEP [31]
import os                                                              # STEP [31]
import sys                                                             # STEP [31]
import tempfile                                                        # STEP [31]
from datetime import datetime, timedelta, timezone                     # STEP [31]
from unittest import mock                                              # STEP [31]

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))  # STEP [31]
if hasattr(sys.stdout, "reconfigure"):                                 # STEP [31]
    sys.stdout.reconfigure(encoding="utf-8")                           # STEP [31]

import main                                                            # STEP [31] noqa: E402

_NOW = datetime.now(timezone.utc)                                      # STEP [31] live clock, per the STEP 28 rule


def _pending(tmp, payload):                                            # STEP [31]
    """Write a pending_post.json into tmp and return its path."""      # STEP [31]
    path = os.path.join(tmp, "pending_post.json")                      # STEP [31]
    with open(path, "w", encoding="utf-8") as fh:                      # STEP [31]
        if isinstance(payload, str):                                   # STEP [31] raw (corrupt) text
            fh.write(payload)                                          # STEP [31]
        else:                                                          # STEP [31]
            json.dump(payload, fh)                                     # STEP [31]
    return path                                                        # STEP [31]


# ---------------------------------------------------------------------------
# 1-8  already_ran_today
# ---------------------------------------------------------------------------
def test_01_today_is_true():                                           # STEP [31]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [31]
        p = _pending(tmp, {"created_utc": _NOW.isoformat(),            # STEP [31]
                           "status": "awaiting_approval"})             # STEP [31]
        assert main.already_ran_today(now=_NOW, pending_path=p) is True  # STEP [31]


def test_02_yesterday_is_false():                                      # STEP [31]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [31]
        y = (_NOW - timedelta(days=1)).isoformat()                     # STEP [31]
        p = _pending(tmp, {"created_utc": y, "status": "awaiting_approval"})  # STEP [31]
        assert main.already_ran_today(now=_NOW, pending_path=p) is False  # STEP [31]


def test_03_missing_file_is_false():                                   # STEP [31]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [31]
        p = os.path.join(tmp, "nope.json")                             # STEP [31]
        assert main.already_ran_today(now=_NOW, pending_path=p) is False  # STEP [31]


def test_04_corrupt_json_is_false():                                   # STEP [31]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [31]
        p = _pending(tmp, "{not json at all")                          # STEP [31]
        assert main.already_ran_today(now=_NOW, pending_path=p) is False  # STEP [31]


def test_05_no_stamp_is_false():                                       # STEP [31]
    # STEP [31] This is exactly what supersede_pending leaves behind.
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [31]
        p = _pending(tmp, {"status": "superseded"})                    # STEP [31]
        assert main.already_ran_today(now=_NOW, pending_path=p) is False  # STEP [31]


def test_06_naive_stamp_is_false():                                    # STEP [31]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [31]
        p = _pending(tmp, {"created_utc": _NOW.replace(tzinfo=None).isoformat()})  # STEP [31]
        assert main.already_ran_today(now=_NOW, pending_path=p) is False  # STEP [31]


def test_07_non_dict_is_false():                                       # STEP [31]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [31]
        p = _pending(tmp, ["a", "list"])                               # STEP [31]
        assert main.already_ran_today(now=_NOW, pending_path=p) is False  # STEP [31]


def test_08_any_status_counts():                                       # STEP [31]
    # STEP [31] A posted or rejected draft must still block a later cron —
    # STEP [31] "already handled today" is the question, not "still pending".
    for status in ("posted", "rejected", "post_failed", "expired"):    # STEP [31]
        with tempfile.TemporaryDirectory() as tmp:                     # STEP [31]
            p = _pending(tmp, {"created_utc": _NOW.isoformat(), "status": status})  # STEP [31]
            assert main.already_ran_today(now=_NOW, pending_path=p) is True, status  # STEP [31]


# ---------------------------------------------------------------------------
# 9-12  run() wiring
# ---------------------------------------------------------------------------
def test_09_guard_runs_before_supersede():                             # STEP [31]
    """The ordering trap: supersede_pending rewrites the file to
    {"status": "superseded"} with NO created_utc. If the guard were checked
    after it, every cron would see "nothing today" and regenerate — and today's
    awaiting_approval draft would be destroyed on the way."""          # STEP [31]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [31]
        p = _pending(tmp, {"created_utc": _NOW.isoformat(),            # STEP [31]
                           "status": "awaiting_approval",              # STEP [31]
                           "draft": "todays text"})                    # STEP [31]
        with mock.patch.object(main, "PENDING_POST_PATH", p):          # STEP [31]
            assert main.run() == 0                                     # STEP [31]
        with open(p, encoding="utf-8") as fh:                          # STEP [31]
            after = json.load(fh)                                      # STEP [31]
        assert after["status"] == "awaiting_approval", after           # STEP [31] survived
        assert after["draft"] == "todays text", after                  # STEP [31] not clobbered


def test_10_guard_short_circuits_run():                                # STEP [31]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [31]
        p = _pending(tmp, {"created_utc": _NOW.isoformat(), "status": "posted"})  # STEP [31]
        with mock.patch.object(main, "PENDING_POST_PATH", p), \
             mock.patch.object(main.fetch, "fetch_all") as fa:         # STEP [31]
            rc = main.run()                                            # STEP [31]
        assert rc == 0, rc                                             # STEP [31] green no-op
        assert fa.call_count == 0, fa.call_count                       # STEP [31] never even fetched


def test_11_force_bypasses_guard():                                    # STEP [31]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [31]
        p = _pending(tmp, {"created_utc": _NOW.isoformat(), "status": "posted"})  # STEP [31]
        with mock.patch.object(main, "PENDING_POST_PATH", p), \
             mock.patch.object(main.fetch, "fetch_all", return_value=[]) as fa:  # STEP [31]
            rc = main.run(force=True)                                  # STEP [31]
        assert fa.call_count == 1, fa.call_count                       # STEP [31] got past the guard
        assert rc == 1, rc                                             # STEP [31] no stories -> red, unchanged


def test_12_yesterday_draft_does_not_block():                          # STEP [31]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [31]
        y = (_NOW - timedelta(days=1)).isoformat()                     # STEP [31]
        p = _pending(tmp, {"created_utc": y, "status": "awaiting_approval"})  # STEP [31]
        with mock.patch.object(main, "PENDING_POST_PATH", p), \
             mock.patch.object(main.fetch, "fetch_all", return_value=[]) as fa:  # STEP [31]
            main.run()                                                 # STEP [31]
        assert fa.call_count == 1, fa.call_count                       # STEP [31]
        with open(p, encoding="utf-8") as fh:                          # STEP [31]
            assert json.load(fh)["status"] == "superseded"             # STEP [31] stale draft invalidated


if __name__ == "__main__":                                             # STEP [31]
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]  # STEP [31]
    for t in tests:                                                    # STEP [31]
        t()                                                            # STEP [31]
        print(f"PASS {t.__name__}")                                    # STEP [31]
    print(f"\n{len(tests)} checks passed.")                            # STEP [31]
