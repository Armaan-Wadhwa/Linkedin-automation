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
  comments (continue the numbering already in the files; next free number: 36).
  (25 = CI stale-checkout rebase fix; 26 = Phase 4 Task 17 retries audit;
  29 = approve.py stale-tap Telegram nudge; 30 = r/Anthropic source removed;
  31 = daily.yml four crons + `already_ran_today` guard; 32 = approve.py long
  poll; 33 = dashboard "Approve now" button; 34 = missing-GMAIL-secret warning;
  35 = newsletter title extraction.)
- Every network call wrapped in try/except with clear logging; a failing
  source/service must NEVER crash the whole run — log, skip, continue.
- Python 3.11+. Minimal deps: `feedparser`, `requests`, `google-genai`,
  `youtube-transcript-api`, stdlib (`imaplib`, `email`, `hashlib`, `json`).
  Justify any new dependency to Harvey before adding it.
- Always state which Phase/Task you are working on at the start of a session.

## Repo layout & module contracts
- `src/config.py` — single source of truth: 19 sources with priorities, HTTP,
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
  **two-phase retry (STEP 27):** phase 1 (`LLM_RETRIES`/`LLM_BACKOFF_S`) retries
  ANY error exactly as before; phase 2 (`LLM_OVERLOAD_EXTRA_RETRIES`/
  `LLM_BACKOFF_OVERLOAD_S`) runs ONLY when phase 1 ended on a 503 —
  `_is_transient_overload(exc)` gates entry — so a Gemini capacity spike is
  ridden out over ~3.5 min (30s+90s settles) instead of dying at ~25s. Non-503
  errors still fail fast after phase 1. Still uses `time.sleep` directly (NOT
  `retryutil.sleep` — the STEP 26 LLM-seam-is-separate invariant holds; tests
  patch `generate.time.sleep`). `generate_post()` returns text or None, never
  raises; `validate_post()` returns advisory warnings (length 1300–1900,
  hook ≤140 chars, no links, hashtags last line) — warnings inform, never block.
- `src/retryutil.py` — `backoff_delay(attempt, base, cap)` + `sleep(seconds)`
  (STEP 26, Task 17). The shared backoff math + the ONE sleep seam every
  retrying site (`post_linkedin`, `telegram_api`, `notify`, `gmail_fetch`)
  calls as `retryutil.sleep(...)`. Each site owns its own policy-specific loop
  (LinkedIn classifies safe/ambiguous/permanent; Telegram/IMAP retry-freely);
  the delay formula and the sleep target are defined once here. Tests patch
  `retryutil.sleep` (single name) so the suite is instant and can assert exact
  delays (e.g. Telegram 429 `retry_after`). Not used by `generate.llm_call`
  (that has its own backoff; different semantics, untouched).
- `src/main.py` — orchestrator; resolves `history.json` at repo root from its
  own path (CWD-independent); exit 0 = draft OK, exit 1 = failure (red run =
  alerting). **STEP 31 adds `already_ran_today(now=None, pending_path=None)`**:
  True when `pending_post.json` holds a draft created on today's UTC date, ANY
  status (a posted or rejected draft still blocks a later cron — the question is
  "handled today?", not "still pending?"). `run(force=False)` calls it FIRST,
  **before `supersede_pending()`** — that rewrites the file to
  `{"status": "superseded"}` with no `created_utc`, so a check after it always
  reads "nothing today" and every cron regenerates, destroying the pending draft
  on the way (`test_main.py:test_09` pins this ordering). Unprovable date
  (missing/corrupt/naive) → False, i.e. GENERATE: the deliberate OPPOSITE of
  `approve._is_expired`'s fail-closed stance, because here the worst case is a
  duplicate draft that supersede clobbers anyway, while there it is posting
  without consent. `--force` bypasses it (manual dispatch only).
- `src/approve.py` — Run B. **STEP 32: `run()` LONG-POLLS.** getUpdates used to
  be ONE call with `timeout: 0` — an instantaneous snapshot, so a tap landing a
  second later waited for the next scheduled run. Now a loop polls with
  `timeout=APPROVE_LONGPOLL_S` (50s, blocking server-side) until a decision
  matches or `APPROVE_LONGPOLL_BUDGET_S` (540s) is spent. Three things are load-
  bearing: (a) **`offset` advances to `max_update_id + 1` between polls** — with
  no offset every call re-returns the same unconfirmed updates, so one stale
  unmatched callback makes every poll return instantly and spins a hot loop for
  the whole budget; (b) batches accumulate into ONE `updates` list so
  `_find_override_photo` / `_find_decision` / `_stale_tap_message_id` /
  `_confirm_updates` are untouched and see the full picture; (c) a SECOND bound,
  `max_polls`, in case Telegram ever ignores the server-side wait. `batch is
  None` with nothing read yet keeps the old meaning (write nothing, `return 0`,
  retry next run); with updates already read it acts on those. Ordering
  invariants unchanged: the decision is saved before `_confirm_updates`, and
  `_post_approved_draft()` still re-reads from disk before posting.
- `src/gmail_fetch.py` — IMAP newsletter ingestion (STEP 14), selected by Gmail
  LABEL only. **STEP 35 rewrote which links become stories.** It used to treat
  EVERY `<a>` as a story with the raw anchor text as the title, so one beehiiv
  issue gave 8 "stories" of which 3 were headlines; the rest were mid-sentence
  fragments, CTA buttons, ads and poll widgets. That matters more than it looks:
  newsletter stories carry `summary = ""`, so the title is the ONLY grounding for
  the generated bullet (hard constraint #5). `_extract_links` now picks a tier
  PER EMAIL, after the unchanged basic filters:
  * **Tier 1, markup** — `_LinkCollector` keeps an ancestor tag stack and flags
    anchors inside `config.NEWSLETTER_HEADLINE_TAGS` (h1-h6/strong/b). If ANY
    anchor is flagged, ONLY those are kept. Measured on real beehiiv mail: 3/3
    real headlines flagged, 0/13 junk anchors flagged — perfect precision.
  * **Tier 2, text heuristic** — for newsletters with no headline markup at all
    (Vaibhav Sisinty; the TLDR/Rundown plain-`<li>`/`<td>` fixtures in
    `test_gmail.py`). `_looks_like_headline` drops (a) anchors starting with a
    LOWERCASE letter (8/8 of the measured sentence fragments; `islower()` only,
    so `$35B cloud deal…` survives) and (b) anchors whose FIRST WORD is in
    `NEWSLETTER_CTA_PREFIXES` — first word only, never a substring, or
    "Apple Watch gets an AI upgrade" dies alongside "Watch the recording".
  Headings-only was rejected on evidence: it would silently zero out any
  newsletter that does not use heading markup. "Is the anchor preceded by text in
  its block?" was prototyped and DISCARDED — beehiiv wraps inline links in their
  own `<span>`, so 8/8 fragments looked standalone. `_VOID_TAGS` guards the
  stack: `<br>` arrives bare and self-closing, and an unguarded pop would unwind
  the whole stack hunting a tag that was never pushed. The cap is applied AFTER
  the tier so junk cannot crowd headlines out of the 8 slots, and one INFO line
  per email logs anchors/candidates/kept/tier so a format change is visible
  rather than a quietly shrinking digest. Live result: 16 stories → 4, all real.
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
- `.github/workflows/daily.yml` — **FOUR crons (STEP 31)**: `53 2`, `11 3`,
  `29 3`, `47 3` UTC = 08:23 / 08:41 / 08:59 / 09:17 IST, odd minutes, all
  before the 9:00 target. GitHub drops most scheduled events on this repo, so
  one cron is one chance; four is four. `main.already_ran_today()` is what makes
  that safe — the first cron GitHub actually STARTS writes the draft, the rest
  exit 0 without generating. `workflow_dispatch` passes `--force` (via `env:`
  `FORCE_FLAG`, never interpolated into the `run:` string — the STEP 23 rule) so
  a manual run can always regenerate. Commits `history.json` + `runs.log` every
  run (`if: always()`) so the repo never hits the 60-day inactivity
  auto-disable; failed generation still logs, then re-fails the run.
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
- `src/token_status.py` — LinkedIn token age + validity. TWO callers with
  DELIBERATELY OPPOSITE stances on an unprovable age; do not "unify" them:
  * `warn_if_stale()` (STEP 10) → `approve.py` appends it to the "✅ Posted to
    LinkedIn" message. Returns None when the age is unprovable. **Must stay
    that way** — make it alert and every successful post carries a nudge.
  * `evaluate(age_days, probe)` (STEP 24) → the standalone monitor. PURE (no
    I/O) so it is unit-testable; an unprovable age IS an alert here, because
    silence is exactly the failure being removed. Emits at most ONE combined
    message; returns None when healthy (no daily "all good" ping).
  * `probe_token(token)` (STEP 24) — one read-only GET to
    `config.LINKEDIN_USERINFO_URL` (`/v2/userinfo`, so NO `LinkedIn-Version`
    header — that is a `/rest/` requirement). 200=valid, 401=invalid,
    everything else INCONCLUSIVE (never cry wolf on a 5xx). A 403 means the
    token lacks the `openid`/`profile` scope, so the probe is inert but
    harmless. Logs status codes only, never the token.
  * `run()` exits 1 ONLY when it had something to report and the Telegram send
    failed — the monitor's own failure must be visible. A dead token that IS
    reported stays green: Telegram is the alert channel.
  * Reuses `telegram_api.api_call("sendMessage", …)` — a plain send with NO
    `reply_markup`. `notify.send_draft` is unsuitable: it always attaches the
    ✅/❌ approve buttons.
- `.github/workflows/token_check.yml` — STEP 24, Task 16. Daily `37 3 * * *`
  (09:07 IST; clear of daily.yml's `53 2`/`11 3`/`29 3`/`47 3` and approve.yml's
  `:9,:29,:49` — STEP 31/32 moved both) +
  `workflow_dispatch`. Runs `python src/token_status.py`. **No concurrency
  group and no commit step, unlike the other four** — it writes no files and
  pushes nothing, so it cannot race a git push; sharing `daily-digest` would
  only queue a monitor behind a 15-min digest. `permissions: contents: read`.
  No `continue-on-error`, so no STEP 22 guard is needed — a non-zero exit
  fails the job red on its own.
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
  **STEP 33 adds a 6th step, "Approve now"** — a button that dispatches
  `approve.yml` (which already declared `workflow_dispatch: {}`, so this was a
  pure frontend change: `WORKFLOWS.approve` + a `triggerApprove()` sibling of
  `triggerRefresh()` + one listener). It runs the POLLER on demand, turning "wait
  for the next cron" into ~30s. It does NOT approve anything and must never be
  made to — the ✅ tap in Telegram remains the only approval (hard constraint #4),
  and the button copy says so.
- `.github/workflows/approve.yml` — cron `9,29,49 * * * *` (every 20 min, odd
  minutes; was `23,53` = every 30) + `workflow_dispatch` (what the STEP 33
  dashboard button fires). `timeout-minutes: 14` — the STEP 32 long-poll budget
  alone is 9 min, plus checkout/setup/install. Shares
  `concurrency.group: daily-digest`: **do not remove it.** It is the only thing
  stopping `main.py` superseding a draft while approve.py holds an older copy in
  memory and is about to post it — a longer-running poller makes that window
  wider, not narrower, and STEP 31's four-cron guard is the mitigation (a
  daily.yml run cancelled while pending simply retries 18 min later).

## Verified facts (from live testing — don't "fix" these)
- Gemini free-tier model that works: **`gemini-3.5-flash`**. 503s under load
  are common — a SUSTAINED spike (not a one-off) burned all 3 phase-1 attempts
  within ~25s on 2026-08-06 and failed the day, which is why STEP 27 added the
  503-only phase-2 tail. The model is still correct (the 503 is Google's overload
  response, not retirement). Keep model name in config only.
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
  re-auth ~every 55 days. ✅ DONE (STEP 24, Task 16): `token_check.yml` runs
  `token_status.run()` daily and Telegrams a warning. **The token is an opaque
  string with NO embedded date** — its age is knowable ONLY from the
  `LINKEDIN_TOKEN_ISSUED_UTC` secret, so a missing/malformed/timezone-naive
  stamp is treated as its OWN alert ("you will get no warning before expiry"),
  never as "probably fine".
- **GitHub Actions cron is FAR worse than "delays up to ~50 min" (measured
  2026-09-02, the STEP 30-33 investigation).** On this repo `approve.yml` asked
  for 48 runs/day and got **5–9**; `daily.yml` held 03:30Z through Aug 26 and
  then drifted to 07:21–08:56Z (12:51–14:26 IST). That is what "the approve
  button does nothing" actually was: taps were read up to **8h26m** late, never
  dropped. Diagnose from `git log` on `origin/main` — every run commits
  `runs.log`, so commit timestamps ARE the run history (note: a run whose push
  fails leaves no line, so this undercounts). The repo was **private** at the
  time, i.e. on the 2,000 free Actions-minutes/month budget, which 48 polls/day
  at ~1.5–2 billed min/job blows through; it was made public on 2026-09-02.
  Design rule that follows: never let approval latency depend on cron frequency
  alone — hence STEP 32's long poll and STEP 33's on-demand button.
- **The local clone silently rots.** The bot pushes a commit every run, so a
  clone left alone for weeks is hundreds of commits behind (566 on 2026-09-02)
  and `pending_post.json` / `runs.log` read as if the bot died. **`git fetch`
  before diagnosing anything**, and read state from `origin/main`, never from
  the working tree.
- Newsletter HTML parsing (Phase 3) is fragile — always non-fatal. **And a
  missing Gmail secret used to be SILENT (STEP 34):** `fetch.fetch_all` gates the
  whole Gmail pass on `GMAIL_ADDRESS` and logged the skip at DEBUG while the
  workflows run at INFO, so newsletters vanished from every run with no trace.
  Now a WARNING. Symptom to recognise: `docs/candidates.json` and the digest
  contain zero `Newsletter:` sources while a local run with `.env` works fine.
- Gemini free-tier limits/models shift — that's why the single `llm_call` seam.
- **Test fixtures with a pinned calendar date rot against `rank`'s 48h age gate.**
  `rank.dedupe_only` scores stories against the LIVE clock (`now=None` →
  `datetime.now`) and discards any older than `MAX_STORY_AGE_H` (48h).
  `test_select.py` hardcoded `_NOW = datetime(2026,8,4,...)` for "determinism",
  so ~48h after STEP 19 landed every test story went stale, `dedupe_only`
  returned `[]`, and the whole suite started failing (the alphabetical-first
  test aborted the no-try/except runner, masking the rest). Fixed STEP 28:
  `_NOW = datetime.now(timezone.utc)`. Rule: any test that feeds stories through
  `dedupe_only`/`dedupe_and_rank` MUST use a recent (or live-now) `published`,
  never a pinned past date.

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
- Phase 4 — hardening: ✅ retries audit (STEP 26, Task 17), ✅ token-age alerts
  (STEP 24, Task 16 — daily `token_check.yml`, age>50 + 401 probe + "no date
  recorded" alert), prompt tuning
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
  **STEP 26 done (Task 17): retries audit.** Three non-LLM network seams now
  retry; one shared backoff helper (no copy-pasted loops). LinkedIn POST =
  STRICT (see standing rule below); Telegram `api_call` + `notify.send_draft`
  retry freely on transient/5xx, honor 429 `retry_after`, fail fast on 400/401,
  and the button-`message_id` invariant holds across a retry (the finally-
  confirmed id is stored); Gmail IMAP retries connect on `OSError`, treats
  `imaplib.IMAP4.error` (auth) as permanent, stays non-fatal. Constants in
  `config.py` (`*_MAX_ATTEMPTS` / `*_BACKOFF_*`); sleeps via `retryutil.sleep`
  (tests patch one name). `test_retries.py`: 18 checks with explicit call-count
  assertions proving no double-post path. Full suite green except the known
  pre-existing `test_normalize_strips_webp_params`.
  **STEP 27 done (Task 17b): 503 overload tail in the LLM seam.** The
  2026-08-06 daily run failed because a SUSTAINED Gemini 503 "high demand"
  spike outlasted the ~25s phase-1 window (3 attempts at t=0, +5s, +25s all
  503). `generate.llm_call` now has a two-phase retry: phase 1 is the unchanged
  `LLM_RETRIES`/`LLM_BACKOFF_S` loop (retries ANY error, fast-fail preserved);
  phase 2 (`LLM_OVERLOAD_EXTRA_RETRIES=2` / `LLM_BACKOFF_OVERLOAD_S=[30,90]`)
  runs ONLY when phase 1 ended on a 503 (`_is_transient_overload` gates entry —
  probes `.code`/`.status_code`/`.response.status_code` then a `"503" + overload-
  keyword` string fallback, version-tolerant across genai SDK reshuffles). So a
  real spike is ridden out over ~3.5 min (5 total calls) instead of dying at 25s.
  `gemini-3.5-flash` unchanged (503 = overload, not retirement). Still `time.sleep`
  directly (STEP 26 invariant holds). `test_generate.py`: 11 checks (detector
  branches + call-count invariants: non-overload fast-fail at 3 calls, all-503
  exhausts at 5, spike clears on last call, phase-2 flips-to-non-overload bails).
  Worst case ~3.5 min in Gemini fits daily.yml's `timeout-minutes: 15`.

## Standing rule — LinkedIn POST retry safety (STEP 26, Task 17)
The LinkedIn POST is **not idempotent** — a duplicate live post is public and
not quietly fixable. So `post_linkedin.post()` retries ONLY on failures provably
safe (the request never reached the server): `ConnectTimeout`, `SSLError`
(TLS handshake), `ConnectionError` whose root cause is an establishment failure
(`ConnectionRefusedError`/`gaierror`/`NewConnectionError`), and HTTP **429**.
Anything **ambiguous** (request may have been sent/processed) makes **exactly
ONE attempt** and returns `UNKNOWN_OUTCOME_MARKER` so the Telegram announce
says "verify your profile": `ReadTimeout`, connection reset/abort/remote-
disconnect, **all HTTP 5xx**, and `201`-with-no-`x-restli-id`. **400/401/403**
are permanent and fail fast. Bias, codified: **when in doubt, do NOT retry the
post.** An ambiguous outcome is recorded as the terminal `post_failed` state
(the "unknown" *message* distinguishes it); `post_failed` is never auto-retried
(only `approved` re-posts), so it can never double-post. Gemini's existing
`generate.llm_call` retry is untouched (different semantics). RSS fetches,
YouTube transcripts, and the userinfo probe were audited and left as-is (they
already degrade safely; retrying them adds risk/noise for no real gain).

## Harvey-side setup (do once, manually)
- **Set the `LINKEDIN_TOKEN_ISSUED_UTC` secret (STEP 24):** a **timezone-aware**
  ISO stamp of the last re-authorization, e.g. `2026-07-26T00:00:00+00:00`.
  A naive stamp (`2026-07-26`) is rejected on purpose — it could miscount the
  age by up to 14h. **Update it every time you re-auth, alongside
  `LINKEDIN_ACCESS_TOKEN`** — otherwise the age check silently measures the
  wrong token. Until it is set, `token_check.yml` will (correctly) Telegram a
  daily "no issue date recorded" alert.
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
