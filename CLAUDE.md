# CLAUDE.md — LinkedIn AI News Digest Bot

Fully automated, **zero-cost** pipeline that publishes ONE daily AI-news digest
post to Harvey's personal LinkedIn profile. Human approval before posting is
MANDATORY and non-negotiable.

## Locked architecture (do not change without Harvey's explicit approval)
GitHub Actions (cron, free) → Python → fetch news (RSS + Gmail newsletters)
→ dedupe & rank → Gemini API free tier (Ollama local fallback) → Telegram bot
approval (two-run pattern) → LinkedIn Posts API → commit history log to repo.

## Hard constraints (NEVER violate)
1. **₹0/$0 cost.** Free tiers, free APIs, open source only. Flag any free-tier
   risk and give a free fallback. Never suggest anything paid.
2. **Official LinkedIn API only**: "Share on LinkedIn" product,
   `w_member_social` scope, `POST https://api.linkedin.com/rest/posts`.
   NEVER cookie/session/scraping approaches (account-ban risk). Same rule for
   every platform: no ToS-violating access anywhere (X, Instagram, etc.).
3. **Secrets** (LinkedIn token, person URN, Gemini key, Telegram token/chat ID,
   Gmail app password, Reddit feed token) live ONLY in GitHub Actions secrets
   or a gitignored local `.env`. Never hardcode, never print/log them.
4. **The bot must NEVER post to LinkedIn without Harvey's explicit Telegram
   approval.** No auto-approve paths, no timeout-defaults-to-yes.
5. Generated posts must be grounded ONLY in fetched stories. No invented facts.

## Coding rules (strict — Harvey enforces these)
- Read every relevant existing file fully before writing or editing anything.
- Surgical, minimal changes. No unrequested features, refactors, or drive-by
  "improvements".
- ONE task at a time. Finish it, show the result, STOP and wait for Harvey's
  confirmation before the next task.
- Annotate every changed line in existing code with `# FIX [N]` / `# STEP [N]`
  comments (continue the numbering already in the files; next free number: 8).
- Every network call wrapped in try/except with clear logging; a failing
  source/service must NEVER crash the whole run — log, skip, continue.
- Python 3.11+. Minimal deps: `feedparser`, `requests`, `google-genai`,
  stdlib (`imaplib`, `email`, `hashlib`, `json`). Justify any new dependency
  to Harvey before adding it.
- Always state which Phase/Task you are working on at the start of a session.

## Repo layout & module contracts
- `src/config.py` — single source of truth: 20 sources with priorities, HTTP,
  ranking, and LLM constants. No secrets ever (env-var *names* only).
- `src/fetch.py` — `fetch_all()` → list of story dicts
  `{source_id, source_name, priority, title, link, summary, published}`.
  Per-source fault isolation; Reddit throttle (6s) + `REDDIT_FEED_PARAMS`
  env token (never logged); feeds sorted newest-first; 30/source cap;
  HN AI-keyword filter (excludes "Launch HN"/"Ask HN").
- `src/rank.py` — `dedupe_and_rank(stories, history)` → top 5.
  Normalized-title SHA-256 dedupe (cross-source + against history.json);
  score = recency (12→0 over 48h) + source priority (2–10) + topic tier
  (Claude 10 / Gemini 8 / OpenAI 6, halved for community sources) + launch(+4)
  + use-case(+2) bonuses; max 2 stories/source in top 5.
  History: `load_history()/record_stories()/save_history()`;
  **record AFTER successful generation only** (failed runs must not burn the
  day's stories); 30-day retention with pruning; corrupt file → empty + warn.
- `src/generate.py` — `llm_call(prompt)` is the ONLY LLM seam
  (`LLM_PROVIDER` env: "gemini" default / "ollama"); retries w/ backoff;
  `generate_post()` returns text or None, never raises; `validate_post()`
  returns advisory warnings (length 1300–1900, hook ≤140 chars, no links,
  hashtags last line) — warnings inform, never block.
- `src/main.py` — orchestrator; resolves `history.json` at repo root from its
  own path (CWD-independent); exit 0 = draft OK, exit 1 = failure (red run =
  alerting).
- `.github/workflows/daily.yml` — cron `53 2 * * *` (08:23 IST, odd minute,
  ~35 min buffer before 9:00 target) + `workflow_dispatch`; commits
  `history.json` + `runs.log` every run (`if: always()`) so the repo never
  hits the 60-day inactivity auto-disable; failed generation still logs, then
  re-fails the run.

## Verified facts (from live testing — don't "fix" these)
- Gemini free-tier model that works: **`gemini-3.5-flash`**. 503s under load
  are common; the existing retry/backoff handles them. Keep model name in
  config only.
- Reddit `.rss` needs the personal feed token (`REDDIT_FEED_PARAMS` =
  `feed=...&user=...` from reddit.com/prefs/feeds) AND 6s spacing; expect
  429s from GitHub Actions datacenter IPs anyway — non-fatal by design.
  Token dies if Harvey changes his Reddit password.
- blog.google feeds use the post-redirect URLs already in config.
- Community feeds (taobojlen, Olshansk×4) are slow-cadence but alive; feeds
  are NOT reliably sorted — never trust `entries[0]`, use max-date (done).
- X/Twitter and Instagram ingestion: rejected (paid API / ToS risk). Covered
  via Meta AI / Ollama / xAI RSS feeds + existing sources. Don't re-suggest.

## LinkedIn post rules (already in the generation prompt — keep them there)
1300–1900 chars plain text; hook <140 chars first line (never "here's my
digest"); 3–5 emoji-led story bullets each with one concrete detail; short
paragraphs + white space; neutral-expert first-person, no hype words; ONE
closing question; 3–5 niche hashtags last line; NO external links in body.

## Known gotchas
- **LinkedIn standard apps: 60-day access token, NO refresh token.** Manual
  re-auth ~every 55 days. Phase 4 adds a Telegram token-age warning (>50 days).
- GitHub Actions cron is best-effort (delays up to ~50 min; occasional drops).
- Newsletter HTML parsing (Phase 3) is fragile — always non-fatal.
- Gemini free-tier limits/models shift — that's why the single `llm_call` seam.

## Phase status
- ✅ Phase 0 — source verification (18→20 sources, health-tested; `test_sources.py`)
- ✅ Phase 1 — MVP: fetch → rank → generate → log (local runs verified;
  first Actions run may still be pending — check before assuming)
- ▶ Phase 2 — NEXT: Telegram approval (two-run pattern) + LinkedIn OAuth +
  Posts API. Tasks: (5) `notify.py` draft→Telegram with Approve/Reject
  buttons + pending-post state file; (6) poller run that reads the decision;
  (7) LinkedIn OAuth walkthrough + `post_linkedin.py`; wire into workflows.
- Phase 3 — Gmail newsletters (IMAP), YouTube transcripts, fork tim-hilde feed
- Phase 4 — hardening: retries audit, token-age alerts, prompt tuning
  (watch: filler intro lines, paragraph spacing in drafts)

## Testing conventions
- Local: run modules directly (`python src/main.py` from root, or from `src/`
  — both work). Env vars per run: `GEMINI_API_KEY`, `REDDIT_FEED_PARAMS`,
  optional `LLM_PROVIDER=ollama`.
- Never commit `.env`, `history.json` conflicts, or anything secret-shaped.
- After each task: syntax check + a real or stubbed run before showing Harvey.
