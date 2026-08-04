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
  comments (continue the numbering already in the files; next free number: 24).
- Every network call wrapped in try/except with clear logging; a failing
  source/service must NEVER crash the whole run — log, skip, continue.
- Python 3.11+. Minimal deps: `feedparser`, `requests`, `google-genai`,
  `youtube-transcript-api`, stdlib (`imaplib`, `email`, `hashlib`, `json`).
  Justify any new dependency to Harvey before adding it.
- Always state which Phase/Task you are working on at the start of a session.

## Repo layout & module contracts
- `src/config.py` — single source of truth: 20 sources with priorities, HTTP,
  ranking, and LLM constants. No secrets ever (env-var *names* only).
- `src/fetch.py` — `fetch_all()` → list of story dicts
  `{source_id, source_name, priority, title, link, summary, published}`.
  Per-source fault isolation; Reddit throttle (6s) + `REDDIT_FEED_PARAMS`
  env token (never logged); feeds sorted newest-first; 30/source cap;
  HN AI-keyword filter (excludes "Launch HN"/"Ask HN"); YouTube transcript
  enrichment pass for source_id 12 only (STEP 16, own try/except, capped at
  `YT_MAX_ENRICH`, skipped cleanly if the library isn't installed).
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
- `src/youtube_enrich.py` — `enrich(story) -> story` (STEP 16): pulls the
  YouTube transcript for source_id 12 stories and writes it into `summary` so
  generate.py's bullet is grounded in real content, not a clickbait title.
  NEVER raises; missing captions / rate limit / library absent → story kept
  title-only (INFO log, not WARNING). Manual transcripts preferred over auto,
  English first. Version-tolerant across youtube-transcript-api 0.6.x / 1.x
  API shapes (probe both). Handles `watch?v=`, `youtu.be/`, and `/shorts/` URLs.
- `src/select_build.py` — `build_from_selection(selection) -> int` (STEP 19,
  Phase 4 Task 12 part A): manual-selection entry path. The future dashboard
  (Task 13) sends `{story_ids, image_url, custom_story}`; this rebuilds the
  SAME candidate universe via `fetch_all()` + `rank.dedupe_only()`, resolves
  `story_ids` by `candidate_id` (= `rank.title_hash`), appends the optional
  custom story (sentinel `source_id=0`, `"manual"`, priority 10; bypasses
  dedupe because it's appended AFTER), caps at 5, then calls the EXISTING
  `generate.generate_post` + `notify.send_draft` + writes `pending_post.json`
  in the EXACT same shape `main.run()` does. Reuses `main.supersede_pending()`
  verbatim — invariant holds by code reuse, not by reimplementation.
  DIVERGENCE from `main.run()` (intentional, STEP 19): records history AFTER
  a successful `send_draft` (the auto path records before; the manual path
  won't burn hand-picked stories on a Telegram outage). Never raises.
- `src/emit_candidates.py` — `emit() -> int` (STEP 19): standalone refresh
  entry that writes `docs/candidates.json` (the GitHub Pages source the Task 13
  dashboard reads). Fetch + `rank.dedupe_only()` (full ranked+deduped list,
  NOT the top-5 `dedupe_and_rank`), serialize each as
  `{candidate_id, source_id, source_name, priority, title, link, summary,
  published, image_url}`. `candidate_id` = `rank.title_hash` verbatim — the
  single contract that makes selection work (emit → pick → build round-trip).
  Capped at 40 candidates. ALWAYS non-fatal: any failure writes
  `{generated_utc, candidates: [], error}` so the dashboard degrades
  gracefully. No secrets in the file.
- `src/main.py` — orchestrator; resolves `history.json` at repo root from its
  own path (CWD-independent); exit 0 = draft OK, exit 1 = failure (red run =
  alerting). STEP 19 adds `supersede_pending(pending_path=None) -> int`
  (extracted from `run()` so both the auto path and the manual selection path
  reuse the SAME terminal-state guard) and an `argparse` entry
  `--from-selection <path>` that delegates to `select_build.build_from_selection`
  (lazy import keeps the auto path import-light). Default (no args) is the
  unchanged auto-ranked flow.
- `.github/workflows/daily.yml` — cron `53 2 * * *` (08:23 IST, odd minute,
  ~35 min buffer before 9:00 target) + `workflow_dispatch`; commits
  `history.json` + `runs.log` every run (`if: always()`) so the repo never
  hits the 60-day inactivity auto-disable; failed generation still logs, then
  re-fails the run.
- **Failure-visibility guard (STEP 22, all four workflows).** Every workflow
  ends with a guard step, AFTER the `if: always()` log/commit step:
  `if: always() && steps.<id>.outcome != 'success'` → echo the outcome, `exit 1`.
  Two rules make it work, and both are easy to get wrong:
  (a) a bare `if:` is implicitly wrapped in `success()`, so a guard without
  `always()` is itself skipped once an earlier step failed and asserts nothing;
  (b) `!= 'success'` (not `== 'failure'`) is what covers `skipped` — the state a
  step is left in when setup/install dies before it. Keep "always commit the
  log" and "always go green" strictly separate: the commit step is `always()`,
  the job conclusion is not. Never add `continue-on-error` to checkout /
  setup-python / install — only the ONE main step per workflow carries it, so
  the log/commit steps can run.
- `.github/workflows/refresh_candidates.yml` — STEP 19, `workflow_dispatch`
  only (no cron — Harvey triggers from the dashboard when he wants a fresh
  menu). Runs `python src/emit_candidates.py`, commits `docs/candidates.json`
  + `runs.log` `if: always()`. Shares `concurrency.group: daily-digest` with
  `daily.yml` + `approve.yml` + `generate_from_selection.yml` so it can never
  race a git push.
- `.github/workflows/generate_from_selection.yml` — STEP 19,
  `workflow_dispatch` with a single string input `selection` (JSON). Writes
  the input to a tmpfile, runs `python src/main.py --from-selection <tmp>`.
  Commits `history.json` + `pending_post.json` + `runs.log` `if: always()`.
  Does NOT post to LinkedIn (approve.yml's job, untouched). Same concurrency
  group as the other three workflows.
  **STEP 23 (Task 15): the `selection` input is passed via `env:` and read from
  `os.environ`, NEVER interpolated into a `run:` command string.** GitHub pastes
  `${{ }}` in as raw text before the shell parses the line, so the old
  `printf '%s' '${{ inputs.selection }}'` mis-parsed any apostrophe in a story
  title. THREE measured failure modes, not one: an **odd** number of apostrophes
  → bash syntax error, exit 2, no file written (loud); an **even** number → exit
  0 and a SILENTLY corrupted file (`OpenAI's and Anthropic's models` was written
  as `OpenAIsandAnthropics models`); an apostrophe followed by shell
  metacharacters → command substitution executes on the runner. The even case is
  the dangerous one — it does not go red, it just builds a post from mangled
  data. Via `env:` the value never reaches the shell parser at all. Applies to
  every future `workflow_dispatch` input; never "quote it better" inline.
- `docs/index.html` — STEP 20, Phase 4 Task 13: the static GitHub Pages
  dashboard (frontend for the Task 12 backend). Single file, vanilla HTML +
  inline `<style>` + inline `<script>`, zero runtime dependencies, zero CDN
  calls. Reads `./candidates.json` (same-origin, no token needed). Fires
  `refresh_candidates.yml` + `generate_from_selection.yml` via the Actions
  dispatch API. Owner/repo/branch/PAT stored in browser `localStorage` ONLY
  (PAT masked via `type="password"`, sent ONLY in `Authorization: Bearer`
  header to api.github.com, never logged / never in URL / never in DOM text).
  Selection JSON matches `select_build.build_from_selection` exactly:
  `{story_ids: [candidate_id hex strings in chosen order],
  image_url: str|null, custom_story: {title,link,summary|null}|null}`.
  Dashboard's last action is firing the generate workflow — final approval
  stays in Telegram (unchanged). Design: Newsroom Terminal (dark), signature
  element = numbered selection badges (glowing green chips with mono digits).
  **STEP 21 (UI rebuild)** — same file, same contracts, rebuilt layout:
  explicit 5-step flow (sticky rail of plain `<a href="#stepN">` anchors +
  numbered `<h2>` step headers, each with a one-line explainer and a live
  status), Settings moved into the header as a dropdown `<details>`, story
  cards gained an "open article" `<a>` that is a SIBLING of the row `<label>`
  (inside it, any click would toggle the checkbox). Six fixes, all commented
  `FIX 21a`–`21f` in the file: (a) `#toast[aria-live]{pointer-events:auto}`
  put an invisible click-blocker over the Generate button — rule deleted;
  (b) `.story-check` had no positioned ancestor, so Tab yanked scroll to page
  top — `article.story` is now `position:relative`; (c) `syncImageSelection()`
  is the SINGLE guard stopping a story-sourced `image_url` outliving its story
  — called from `toggleStory`, `onCustomInput` (cap 5→4 pops one) and
  `fetchCandidates`; never reimplement it per-caller; (d) `saveSettings` now
  `removeItem`s an emptied token instead of leaving the old one behind;
  (e) at-cap rows are no longer `disabled` (that removed them from the tab
  order) — `toggleStory` explains the cap instead; (f) `networkErrMsg` reports
  the real cause. Also: skip link, contiguous heading levels, `<fieldset>` +
  screen-reader legend on the image radios, `aria-live` on the cap notice and
  refresh status, `aria-describedby` on the Generate button, 44px buttons.

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
- youtube-transcript-api (Vaibhav transcripts, source 12): no API key needed.
  **1.x renamed `list_transcripts`→`list` and made it an INSTANCE method** —
  `_fetch_transcript_text` probes both shapes so the loose `>=0.6.2` pin works.
  Shorts use `/shorts/ID` URLs (not just `watch?v=`); ~half of Vaibhav's feed
  is Shorts. Transcripts often missing/disabled — non-fatal by design (INFO
  log, title-only fallback). Manual transcripts preferred over auto, English
  first.
- **`candidate_id` contract (STEP 19):** `emit_candidates.py` writes
  `candidate_id = rank.title_hash(title)` (sha256 of normalized title) into
  `docs/candidates.json`. `select_build.py` resolves `selection.story_ids`
  against the SAME hash via `rank.dedupe_only()`. The round-trip works ONLY
  because both sides import `rank.title_hash` — never reimplement, never
  invent a second normalization, never change one without the other.

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
- Phase 3 — ✅ Gmail newsletters (IMAP, STEP 14); ✅ YouTube transcripts
  (Vaibhav source 12, STEP 16); fork tim-hilde feed (pending)
- Phase 4 — hardening: retries audit, token-age alerts, prompt tuning
  (watch: filler intro lines, paragraph spacing in drafts). **STEP 19 done:
  web-selection BACKEND seam** (`select_build.py`, `emit_candidates.py`,
  `--from-selection` entry, two `workflow_dispatch` workflows). **STEP 20
  done: web-selection FRONTEND** (`docs/index.html` static dashboard). All
  four pieces wired together; the dashboard fires the workflows via the
  Actions dispatch API, final approval stays in Telegram. **STEP 21 done:
  dashboard UI rebuild** — 5-step flow, per-card open link, six fixes
  (`FIX 21a`–`21f`); no backend, workflow, or contract changes.
  **STEP 22 done (Task 14): `outcome=skipped` gap CLOSED** in all four
  workflows — guard widened from `== 'failure'` to
  `always() && outcome != 'success'` (see the guard note above).
  Investigation finding, recorded so it isn't re-litigated: the three
  `outcome=skipped` days in `runs.log` (2026-07-18/19/20) were **red runs, not
  green** — `pip install` has no `continue-on-error`, so its failure already
  failed the job by GitHub's default propagation. `skipped` is the label the
  runs.log line records, not evidence of a green run. STEP 22 changes NO job
  conclusion in any reachable scenario; it converts an invariant that was
  inherited from a GitHub default into one the YAML states explicitly, and
  makes the red run name its cause. Not verified against the Actions UI (no
  `gh`, private repo) — conclusion rests on documented Actions semantics.

## Harvey-side setup (do once, manually)
- **Enable GitHub Pages on `/docs`:** repo Settings → Pages → Source =
  "Deploy from a branch" → Branch = `main` / `/docs` folder. Dashboard then
  lives at `https://<owner>.github.io/<repo>/`.
- **Create a fine-grained PAT for the dashboard:** github.com → Settings →
  Developer settings → Personal access tokens (fine-grained) → generate,
  scoped to THIS repo only, with just `Actions: write` + `Contents: read`.
  Paste it into the dashboard's Settings strip; it lives only in the
  browser's localStorage on that device. Never commit, never reuse a
  classic/broad token.

## Testing conventions
- Local: run modules directly (`python src/main.py` from root, or from `src/`
  — both work). Env vars per run: `GEMINI_API_KEY`, `REDDIT_FEED_PARAMS`,
  optional `LLM_PROVIDER=ollama`.
- Never commit `.env`, `history.json` conflicts, or anything secret-shaped.
- After each task: syntax check + a real or stubbed run before showing Harvey.
