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


def llm_call(prompt):
    """The single LLM seam. Retries with backoff; raises after final failure."""
    provider = os.environ.get(config.LLM_PROVIDER_ENV, "gemini").lower()
    call = _PROVIDERS.get(provider)
    if call is None:
        raise RuntimeError(f"Unknown LLM provider '{provider}' "
                           f"(expected one of {sorted(_PROVIDERS)})")
    last_exc = None
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