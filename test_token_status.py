"""
test_token_status.py — Phase 4 Task 16 (STEP 24).

Covers the pure decision seam token_status.evaluate() across the seven states
the monitor must distinguish, plus a stubbed run() proving the healthy path
stays SILENT and that a failed Telegram send goes red.

No network, no Telegram, no LinkedIn. Run: python test_token_status.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import config           # noqa: E402
import token_status     # noqa: E402

VALID = token_status.PROBE_VALID
INVALID = token_status.PROBE_INVALID
INCONCLUSIVE = token_status.PROBE_INCONCLUSIVE

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name} {extra}")


print("evaluate() — the 7 states")

# 1. healthy: young token, probe says alive -> say nothing at all.
m = token_status.evaluate(40, VALID)
check("age 40 + 200 -> no message", m is None, f"got {m!r}")

# 2. age warning with the day maths spelled out.
m = token_status.evaluate(55, VALID)
check("age 55 + 200 -> warns", m is not None)
check("  ...says 55 days", m and "55 days old" in m, f"got {m!r}")
check("  ...says 5 day(s) left", m and "5 day(s) left" in m, f"got {m!r}")
check("  ...asks for re-auth", m and "Re-authorize LinkedIn" in m)
check("  ...names both secrets",
      m and config.LINKEDIN_TOKEN_ENV in m
      and config.LINKEDIN_TOKEN_ISSUED_UTC_ENV in m)

# 3. issue date missing -> alertable in its OWN right, never assumed fresh.
m = token_status.evaluate(None, VALID)
check("date missing -> asks him to record one", m is not None)
check("  ...does NOT claim the token is fine",
      m and "No usable issue date" in m, f"got {m!r}")
check("  ...names the date secret",
      m and config.LINKEDIN_TOKEN_ISSUED_UTC_ENV in m)

# 4. unparseable date reaches evaluate() as None too (days_old collapses them).
check("unparseable date -> same path as missing",
      token_status.evaluate(None, VALID) == m)

# 5. probe 401 while the age is fine -> dead-now alert.
m = token_status.evaluate(40, INVALID)
check("401 + young age -> dead-now alert", m is not None)
check("  ...says no longer valid", m and "NO LONGER VALID" in m, f"got {m!r}")
check("  ...warns posting will fail", m and "Posting will fail" in m)

# 6. transient failure must not cry wolf.
m = token_status.evaluate(40, INCONCLUSIVE)
check("500/network + young age -> silent", m is None, f"got {m!r}")
m = token_status.evaluate(55, INCONCLUSIVE)
check("500/network + old age -> age still evaluated independently",
      m is not None and "55 days old" in m, f"got {m!r}")
check("  ...and says nothing about the token being dead",
      m and "NO LONGER VALID" not in m)

# 7. both signals -> exactly ONE message, both facts present.
m = token_status.evaluate(55, INVALID)
check("age 55 + 401 -> one combined message", m is not None)
check("  ...contains the 401 fact", m and "NO LONGER VALID" in m)
check("  ...contains the age fact", m and "55 days old" in m)
check("  ...is a single message (one header)",
      m and m.count("🔑 LinkedIn token check") == 1, f"got {m!r}")

# boundary: exactly at the threshold is NOT yet a warning (> not >=).
check("age == warn threshold -> still silent",
      token_status.evaluate(config.LINKEDIN_TOKEN_WARN_DAYS, VALID) is None)
check("age == threshold+1 -> warns",
      token_status.evaluate(config.LINKEDIN_TOKEN_WARN_DAYS + 1, VALID) is not None)

# past the hard limit -> the blunter wording, no negative "days left".
m = token_status.evaluate(config.LINKEDIN_TOKEN_LIFETIME_DAYS + 3, VALID)
check("past 60 days -> PAST-the-limit wording", m and "PAST the" in m, f"got {m!r}")
check("  ...no negative days-left", m and "-3 day" not in m)


print("\nrun() — stubbed, no network")

sent = []


def fake_api_call(method, payload, token, quiet=False):
    sent.append((method, payload))
    return {"message_id": 1}


def fake_creds():
    return "FAKE-TG-TOKEN", "12345"


orig_call, orig_creds = token_status.telegram_api.api_call, token_status.telegram_api.credentials
orig_probe = token_status.probe_token
token_status.telegram_api.api_call = fake_api_call
token_status.telegram_api.credentials = fake_creds

try:
    # healthy: fresh date + valid token -> ZERO messages, exit 0.
    from datetime import datetime, timedelta, timezone
    fresh = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    os.environ[config.LINKEDIN_TOKEN_ISSUED_UTC_ENV] = fresh
    os.environ[config.LINKEDIN_TOKEN_ENV] = "irrelevant-stub"
    token_status.probe_token = lambda t: VALID
    sent.clear()
    rc = token_status.run()
    check("healthy run -> exit 0", rc == 0, f"got {rc}")
    check("healthy run -> sent NOTHING", len(sent) == 0, f"sent {len(sent)}")

    # stale: 55-day-old date -> exactly one sendMessage, exit 0.
    old = (datetime.now(timezone.utc) - timedelta(days=55)).isoformat()
    os.environ[config.LINKEDIN_TOKEN_ISSUED_UTC_ENV] = old
    sent.clear()
    rc = token_status.run()
    check("stale run -> exit 0 (reported successfully)", rc == 0, f"got {rc}")
    check("stale run -> exactly ONE message", len(sent) == 1, f"sent {len(sent)}")
    check("  ...via sendMessage", sent and sent[0][0] == "sendMessage")
    check("  ...with NO approve/reject buttons",
          sent and "reply_markup" not in sent[0][1], f"got {sent[0][1].keys()}")
    check("  ...token value not in the payload",
          sent and "irrelevant-stub" not in str(sent[0][1]))

    # telegram down while there IS something to report -> red run.
    token_status.telegram_api.api_call = lambda *a, **k: None
    rc = token_status.run()
    check("stale run + Telegram down -> exit 1 (red)", rc == 1, f"got {rc}")

    # missing date -> alerts (does not silently pass).
    token_status.telegram_api.api_call = fake_api_call
    os.environ.pop(config.LINKEDIN_TOKEN_ISSUED_UTC_ENV, None)
    sent.clear()
    rc = token_status.run()
    check("missing date -> sends an alert", len(sent) == 1, f"sent {len(sent)}")
    check("missing date -> exit 0 (reported)", rc == 0, f"got {rc}")
finally:
    token_status.telegram_api.api_call = orig_call
    token_status.telegram_api.credentials = orig_creds
    token_status.probe_token = orig_probe
    os.environ.pop(config.LINKEDIN_TOKEN_ISSUED_UTC_ENV, None)
    os.environ.pop(config.LINKEDIN_TOKEN_ENV, None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
