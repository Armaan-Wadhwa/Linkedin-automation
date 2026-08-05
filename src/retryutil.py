"""
retryutil.py — shared backoff math + sleep seam for the three non-LLM network
               retries (Phase 4, Task 17).                                 # STEP [26]

WHY THIS EXISTS (and why it is this small):
- generate.py's llm_call already retries Gemini/Ollama, but that helper is
  provider-specific (dispatch + re-raise) and not reusable. The three sites in
  this task have DIFFERENT policies, so one generic retry loop would need a pile
  of callbacks. Instead each site owns a small policy-specific loop and calls the
  TWO shared primitives here:
    backoff_delay(attempt, base, cap)  — the exponential-delay formula, defined
                                         once (not copy-pasted three times).
    sleep(seconds)                     — a single time.sleep indirection.
- Every site calls retryutil.sleep(...) (attribute access at call time), so tests
  patch ONE name — `retryutil.sleep` — to make the whole suite instant and to
  assert exact sleep values (e.g. Telegram's 429 retry_after). No test ever
  really sleeps.

Constants (max attempts / base / cap) live in config.py — no magic numbers in the
loops. This module holds no policy and no constants of its own.
"""

import time as _time   # STEP [26]


def backoff_delay(attempt, base, cap):                                   # STEP [26]
    """Exponential backoff seconds for a 1-based attempt number.

    base * 2**(attempt-1), hard-capped at `cap`. Pure (no I/O) so it is directly
    unit-testable. attempt=1 is never passed (the first try is never preceded by
    a sleep); the first retry is attempt=2 -> base, then 2*base, 4*base, ..."""
    return float(min(base * (2 ** (attempt - 1)), cap))


def sleep(seconds):                                                      # STEP [26]
    """Single sleep seam. Tests patch THIS attribute on the retryutil module so
    every caller (post_linkedin, telegram_api, notify, gmail_fetch) stops really
    sleeping at once. Kept as a function (not `time.sleep` re-exported) precisely
    so `mock.patch("retryutil.sleep")` is the one stub point."""
    _time.sleep(seconds)
