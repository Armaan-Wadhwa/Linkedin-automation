"""
test_generate.py — STEP [27]. Unit cover for generate.llm_call's two-phase retry
+ the _is_transient_overload detector. Fully stubbed: NO network, NO real Gemini
call. generate.time.sleep is patched so the suite is instant AND so we can assert
the exact backoff schedule (including the new 30s/90s overload tail).

Run from the repo root:  python test_generate.py

Call-count assertions ARE the test:
- a non-overload error must make exactly (1 + LLM_RETRIES) calls and STOP — the
  overload tail must never run on a genuine failure (fail-fast invariant);
- an all-503 run must make exactly (1 + LLM_RETRIES + LLM_OVERLOAD_EXTRA_RETRIES)
  calls — no more, no less;
- a spike that clears must succeed on the last allowed attempt.
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import config          # noqa: E402
import generate        # noqa: E402

GEMINI_ENV = {config.LLM_PROVIDER_ENV: "gemini"}


# --- exception builders that exercise each branch of _is_transient_overload ---
class _CodeError(Exception):            # STEP [27] genai ServerError shape (.code)
    def __init__(self, code, message="high demand"):
        super().__init__(f"{code} UNAVAILABLE. {{'error': {{'code': {code}, "
                         f"'message': '{message}'}}}}")
        self.code = code


class _StatusError(Exception):          # STEP [27] requests-style (.status_code)
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _resp_error(status_code):           # STEP [27] HTTPError with .response carrier
    exc = Exception(f"server returned {status_code}")
    exc.response = mock.Mock(status_code=status_code)
    return exc


def _genai_503():                       # STEP [27] the live 2026-08-06 failure shape
    return _CodeError(503, "This model is currently experiencing high demand. "
                           "Spikes in demand are usually temporary.")


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
# _is_transient_overload — every detection branch + the negatives
# ===========================================================================
def test_overload_detects_code_attr_503():
    assert generate._is_transient_overload(_CodeError(503)) is True


def test_overload_detects_status_code_attr_503():
    assert generate._is_transient_overload(_StatusError(503)) is True


def test_overload_detects_response_carrier_503():
    assert generate._is_transient_overload(_resp_error(503)) is True


def test_overload_detects_string_fallback():
    # No .code / .status_code / .response — only the message carries 503.
    assert generate._is_transient_overload(
        Exception("503 UNAVAILABLE. high demand")) is True


def test_overload_false_on_non_503_errors():
    # Genuine failures must NOT be classified as overload (they'd wrongly enter
    # the long phase-2 tail). Empty-response RuntimeError, 500, 400, generic.
    for exc in (RuntimeError("Gemini returned an empty response"),
                _CodeError(500),
                _StatusError(400),
                _resp_error(429),
                ValueError("totally unrelated"),
                Exception("no 503 anywhere here")):
        assert generate._is_transient_overload(exc) is False, \
            f"{exc!r} must NOT count as overload"


def test_overload_false_when_503_is_a_substring_coincidence():
    # '503' inside a longer token (e.g. a model id) without an overload keyword
    # must not trip the string fallback.
    assert generate._is_transient_overload(Exception("model gpt-503x not found")) is False


# ===========================================================================
# llm_call — phase 1 (non-overload) fails fast, phase 2 never runs
# ===========================================================================
def test_non_overload_error_fails_fast_after_phase1():
    # A genuine error retries across phase 1 (LLM_RETRIES) then STOPS — the
    # overload tail must not run because waiting won't fix a real problem.
    fake = mock.Mock(side_effect=RuntimeError("empty response"))
    with mock.patch.dict(os.environ, GEMINI_ENV), \
         mock.patch.dict(generate._PROVIDERS, {"gemini": fake}), \
         mock.patch("generate.time.sleep") as sl:
        try:
            generate.llm_call("p")
            assert False, "non-overload error must raise"
        except RuntimeError as exc:
            assert "all attempts failed" in str(exc)
    expected_calls = 1 + config.LLM_RETRIES
    assert fake.call_count == expected_calls, \
        f"non-overload must make exactly {expected_calls} phase-1 calls, " \
        f"got {fake.call_count}"
    assert [c.args[0] for c in sl.call_args_list] == list(config.LLM_BACKOFF_S), \
        f"phase-1 backoff only: expected {config.LLM_BACKOFF_S}, " \
        f"got {[c.args[0] for c in sl.call_args_list]}"


# ===========================================================================
# llm_call — 503 spike rides the full two-phase budget then raises
# ===========================================================================
def test_all_503_exhausts_both_phases_then_raises():
    # The live 2026-08-06 scenario: every attempt 503. Must use the WHOLE budget
    # (phase 1 + overload tail) and then fail. Call count is the invariant.
    fake = mock.Mock(side_effect=_genai_503())
    with mock.patch.dict(os.environ, GEMINI_ENV), \
         mock.patch.dict(generate._PROVIDERS, {"gemini": fake}), \
         mock.patch("generate.time.sleep") as sl:
        try:
            generate.llm_call("p")
            assert False, "all-503 must raise"
        except RuntimeError as exc:
            assert "all attempts failed" in str(exc)
    expected_calls = 1 + config.LLM_RETRIES + config.LLM_OVERLOAD_EXTRA_RETRIES
    assert fake.call_count == expected_calls, \
        f"all-503 must use the FULL budget ({expected_calls} calls), " \
        f"got {fake.call_count}"
    expected_sleeps = list(config.LLM_BACKOFF_S) + list(config.LLM_BACKOFF_OVERLOAD_S)
    assert [c.args[0] for c in sl.call_args_list] == expected_sleeps, \
        f"two-phase backoff schedule: expected {expected_sleeps}, " \
        f"got {[c.args[0] for c in sl.call_args_list]}"


def test_503_spike_clears_on_last_overload_attempt():
    # Phase 1 all 503 (3 calls), phase 2: first 503 then SUCCESS on the final
    # allowed attempt -> the spike rode-out path that actually saves the day.
    seq = [_genai_503(), _genai_503(), _genai_503(), _genai_503(), "POST OK"]
    fake = mock.Mock(side_effect=seq)
    with mock.patch.dict(os.environ, GEMINI_ENV), \
         mock.patch.dict(generate._PROVIDERS, {"gemini": fake}), \
         mock.patch("generate.time.sleep") as sl:
        out = generate.llm_call("p")
    assert out == "POST OK", out
    assert fake.call_count == 5, \
        f"success on the 5th (last) allowed call, got {fake.call_count}"
    assert [c.args[0] for c in sl.call_args_list] == [5, 20, 30, 90], \
        [c.args[0] for c in sl.call_args_list]


def test_503_in_phase2_flips_to_non_overload_exits_early():
    # Phase 1 all 503, then phase-2 attempt fails with a NON-overload error:
    # the while-condition must re-check and bail (don't burn the rest of the
    # spike budget waiting on a problem waiting won't fix).
    seq = [_genai_503(), _genai_503(), _genai_503(),
           RuntimeError("now a different failure")]
    fake = mock.Mock(side_effect=seq)
    with mock.patch.dict(os.environ, GEMINI_ENV), \
         mock.patch.dict(generate._PROVIDERS, {"gemini": fake}), \
         mock.patch("generate.time.sleep") as sl:
        try:
            generate.llm_call("p")
            assert False, "must raise once the error flips non-overload"
        except RuntimeError as exc:
            assert "all attempts failed" in str(exc)
    assert fake.call_count == 4, \
        f"3 phase-1 + 1 phase-2 (then bail): 4 calls, got {fake.call_count}"
    assert [c.args[0] for c in sl.call_args_list] == [5, 20, 30], \
        [c.args[0] for c in sl.call_args_list]


def test_503_clears_within_phase1_uses_only_short_backoff():
    # 503 on attempt 1, success on attempt 2 -> stays inside phase 1; the
    # overload tail is never needed and never entered. Proves phase 1 still
    # handles a SHORT spike exactly as before (no behavior change).
    fake = mock.Mock(side_effect=[_genai_503(), "POST OK"])
    with mock.patch.dict(os.environ, GEMINI_ENV), \
         mock.patch.dict(generate._PROVIDERS, {"gemini": fake}), \
         mock.patch("generate.time.sleep") as sl:
        out = generate.llm_call("p")
    assert out == "POST OK"
    assert fake.call_count == 2, fake.call_count
    assert [c.args[0] for c in sl.call_args_list] == [5], \
        [c.args[0] for c in sl.call_args_list]


# ===========================================================================
if __name__ == "__main__":
    checks = [(n, v) for n, v in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in checks:
        run_check(name, fn)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
