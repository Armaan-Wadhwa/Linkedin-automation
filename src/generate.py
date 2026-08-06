"""
generate.py — Turn the top-ranked stories into a LinkedIn post draft.

Design:
- llm_call(prompt) is the ONLY place any LLM is touched. Provider selected by
  the LLM_PROVIDER env var: "gemini" (default, free tier) or "ollama" (local).
  Swapping providers requires zero changes anywhere else.
- Retries with backoff (LLM_RETRIES / LLM_BACKOFF_S) around every attempt.
- generate_post(stories) returns the post text, or None on total failure —
  it never raises, so main.py can log-and-exit cleanly.
- validate_post(text) returns human-readable warnings (length, hook, links)
  to show alongside the draft; warnings never block, Harvey decides.
- Secrets: GEMINI_API_KEY read from env inside the call, never logged.
- Factual grounding: the prompt forbids invented facts; thin stories (e.g.
  clickbait titles with no summary) must get thin bullets.
"""

import logging
import os
import re
import time

import requests

import config

log = logging.getLogger(__name__)

PROMPT_TEMPLATE = """You are a LinkedIn ghostwriter for a software developer who posts a daily AI-news digest.
Write ONE LinkedIn post from the stories below.

RULES (LinkedIn 2026 algorithm):
- 1,300-1,900 characters total. Plain text only, no markdown.
- First line = a scroll-stopping hook under 140 characters (bold claim, stat, or question). Never "Here's my daily digest" or similar.
- Then 3-5 top stories, each as a 1-2 line bullet led by a relevant emoji, each with one concrete detail (number, model name, capability) taken from the story.
- Short 1-2 line paragraphs with generous white space between them.
- Neutral-expert first-person tone. No hype words ("game-changer", "revolutionary", "mind-blowing").
- End with ONE genuine question to invite comments.
- Last line: 3-5 niche hashtags (e.g. #AI #LLM #Claude #Gemini #OpenAI).
- NO external links anywhere in the post.
- CRITICAL: use ONLY facts present in the stories below. Do not invent numbers, names, or capabilities. If a story gives little detail, keep its bullet vague rather than inventing specifics, or skip it in favor of a better-documented story.

Respond with the post text only — no preamble, no explanation, no quotation marks around the post.

STORIES:
{stories_block}"""


def _stories_block(stories):
    """Render ranked stories for the prompt: title, source, age, summary."""
    lines = []
    for i, s in enumerate(stories, 1):
        when = s["published"].strftime("%Y-%m-%d %H:%M UTC") if s.get("published") else "recent"
        summary = s.get("summary") or "(no summary available)"
        lines.append(f"{i}. [{s['source_name']}] ({when}) {s['title']}\n   {summary}")
    return "\n".join(lines)


def build_prompt(stories):
    return PROMPT_TEMPLATE.format(stories_block=_stories_block(stories))


# ---------------------------------------------------------------------------
# Providers — called only via llm_call()
# ---------------------------------------------------------------------------
def _call_gemini(prompt):
    from google import genai  # lazy import: not needed for Ollama-only setups
    from google.genai import types

    api_key = os.environ.get(config.GEMINI_KEY_ENV)
    if not api_key:
        raise RuntimeError(f"{config.GEMINI_KEY_ENV} is not set")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=config.LLM_TEMPERATURE),
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response")
    return text


def _call_ollama(prompt):
    response = requests.post(
        config.OLLAMA_URL,
        json={"model": config.OLLAMA_MODEL, "prompt": prompt, "stream": False,
              "options": {"temperature": config.LLM_TEMPERATURE}},
        timeout=config.LLM_TIMEOUT,
    )
    response.raise_for_status()
    text = (response.json().get("response") or "").strip()
    if not text:
        raise RuntimeError("Ollama returned an empty response")
    return text


_PROVIDERS = {"gemini": _call_gemini, "ollama": _call_ollama}


def _is_transient_overload(exc):                                            # STEP [27]
    """True if `exc` looks like a server-side capacity/overload failure (503 /
    # STEP [27] Service Unavailable) — the ONE case where waiting longer actually
    # STEP [27] helps. Gemini's google-genai SDK raises a ServerError whose str()
    # STEP [27] is '503 UNAVAILABLE. {...high demand...}'; Ollama's requests path
    # STEP [27] raises an HTTPError with a .response carrying status_code 503.
    # STEP [27] Probe every shape (attribute then string) so this stays robust
    # STEP [27] across SDK version reshuffles — the live 2026-08-06 failure log
    # STEP [27] confirms '503' appears in str(exc) as a final fallback. Non-503
    # STEP [27] errors return False so llm_call still fails fast on real problems."""
    code = getattr(exc, "code", None)                                       # STEP [27]
    if code == 503:                                                         # STEP [27]
        return True
    status = getattr(exc, "status_code", None)                              # STEP [27]
    if status == 503:                                                       # STEP [27]
        return True
    resp = getattr(exc, "response", None)                                   # STEP [27]
    if resp is not None and getattr(resp, "status_code", None) == 503:      # STEP [27]
        return True
    text = str(exc)                                                         # STEP [27]
    low = text.lower()                                                      # STEP [27]
    return "503" in text and ("unavailable" in low or "high demand" in low)  # STEP [27]


def llm_call(prompt):
    """The single LLM seam. Retries with backoff; raises after final failure.

    # STEP [27] Two-phase retry. Phase 1 (LLM_RETRIES attempts, LLM_BACKOFF_S
    # STEP [27] waits) retries ANY failure — this loop is unchanged from the
    # STEP [27] original. Phase 2 (LLM_OVERLOAD_EXTRA_RETRIES attempts,
    # STEP [27] LLM_BACKOFF_OVERLOAD_S waits) runs ONLY when phase 1 ended on a
    # STEP [27] 503/overload (_is_transient_overload gates entry). A Gemini
    # STEP [27] capacity spike outlasts the ~25s phase-1 window; the long settles
    # STEP [27] (30s, 90s) span a real multi-minute spike instead of failing the
    # STEP [27] day. Genuine (non-overload) errors still fail fast after phase 1.
    # STEP [27] Still uses time.sleep directly (NOT retryutil.sleep): the STEP 26
    # STEP [27] invariant keeps the LLM seam's backoff separate from the non-LLM
    # STEP [27] sites (different semantics; tests patch generate.time.sleep)."""
    provider = os.environ.get(config.LLM_PROVIDER_ENV, "gemini").lower()
    call = _PROVIDERS.get(provider)
    if call is None:
        raise RuntimeError(f"Unknown LLM provider '{provider}' "
                           f"(expected one of {sorted(_PROVIDERS)})")
    last_exc = None

    # STEP [27] Phase 1: original retry budget, retries ANY error (unchanged).
    for attempt in range(1 + config.LLM_RETRIES):
        if attempt:
            wait = config.LLM_BACKOFF_S[min(attempt - 1, len(config.LLM_BACKOFF_S) - 1)]
            log.warning("llm_call: retry %d/%d in %ds", attempt, config.LLM_RETRIES, wait)
            time.sleep(wait)
        try:
            return call(prompt)
        except Exception as exc:  # noqa: BLE001 — any provider failure => retry
            last_exc = exc
            log.warning("llm_call: %s attempt %d failed: %s: %s",
                        provider, attempt + 1, type(exc).__name__, exc)

    # STEP [27] Phase 2: overload-only tail. Entered only if phase 1 exhausted on
    # STEP [27] a 503 — a capacity spike the short phase-1 window couldn't clear.
    # STEP [27] A non-overload last_exc skips this and fails fast at the raise
    # STEP [27] below. If an overload attempt fails with a NON-overload error, the
    # STEP [27] while-condition re-checks last_exc next pass and exits (no point
    # WAITING through a spike budget on a problem waiting won't fix).
    overload_idx = 0                                                   # STEP [27]
    while (_is_transient_overload(last_exc)                            # STEP [27]
           and overload_idx < config.LLM_OVERLOAD_EXTRA_RETRIES):
        wait = config.LLM_BACKOFF_OVERLOAD_S[                          # STEP [27]
            min(overload_idx, len(config.LLM_BACKOFF_OVERLOAD_S) - 1)]
        log.warning("llm_call: %s overload retry %d/%d in %ds (503 spike)",     # STEP [27]
                    provider, overload_idx + 1,
                    config.LLM_OVERLOAD_EXTRA_RETRIES, wait)
        time.sleep(wait)                                               # STEP [27]
        try:
            return call(prompt)                                        # STEP [27]
        except Exception as exc:  # noqa: BLE001
            last_exc = exc                                             # STEP [27]
            log.warning("llm_call: %s overload attempt %d failed: %s: %s",         # STEP [27]
                        provider, overload_idx + 1, type(exc).__name__, exc)
        overload_idx += 1                                              # STEP [27]

    raise RuntimeError(f"llm_call: all attempts failed ({provider})") from last_exc


# ---------------------------------------------------------------------------
# Post generation + validation
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)


def validate_post(text):
    """Return a list of human-readable warnings (empty = looks good).
    Warnings inform Harvey's approval decision; they never block."""
    warnings = []
    n = len(text)
    if n < config.POST_MIN_CHARS:
        warnings.append(f"too short: {n} chars (target {config.POST_MIN_CHARS}-{config.POST_MAX_CHARS})")
    elif n > config.POST_MAX_CHARS:
        warnings.append(f"too long: {n} chars (target {config.POST_MIN_CHARS}-{config.POST_MAX_CHARS})")
    first_line = text.splitlines()[0].strip() if text.strip() else ""
    if len(first_line) > config.HOOK_MAX_CHARS:
        warnings.append(f"hook is {len(first_line)} chars (mobile fold is ~{config.HOOK_MAX_CHARS})")
    if _URL_RE.search(text):
        warnings.append("contains an external link (links cut reach; move to first comment)")
    if not text.rstrip().splitlines()[-1].lstrip().startswith("#") if text.strip() else True:
        warnings.append("last line does not look like hashtags")
    return warnings


def generate_post(stories):
    """Generate the LinkedIn post. Returns text or None; never raises."""
    if not stories:
        log.error("generate_post: no stories to work with — skipping generation")
        return None
    if len(stories) < config.MIN_STORIES_TO_GENERATE:
        log.warning("generate_post: thin news day (%d stories) — generating anyway",
                    len(stories))
    try:
        text = llm_call(build_prompt(stories))
    except Exception as exc:  # noqa: BLE001 — a failed LLM must not crash the run
        log.error("generate_post: generation failed: %s", exc)
        return None
    for warning in validate_post(text):
        log.warning("generate_post: draft check — %s", warning)
    log.info("generate_post: draft ready (%d chars, %d stories)", len(text), len(stories))
    return text


if __name__ == "__main__":
    # Manual end-to-end test: fetch -> rank -> generate -> print.
    # Requires GEMINI_API_KEY (or LLM_PROVIDER=ollama with Ollama running).
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    import fetch
    import rank
    top = rank.dedupe_and_rank(fetch.fetch_all(), rank.load_history())
    post = generate_post(top)
    print("\n" + "=" * 70)
    print(post if post else "GENERATION FAILED — see warnings above")
    print("=" * 70)
    if post:
        print(f"({len(post)} characters)")