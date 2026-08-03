"""
config.py — Canonical configuration for the LinkedIn AI News Digest Bot.

Single source of truth for: news sources (with ranking priority), HTTP
settings, and keyword lists. No secrets in this file, ever — secrets come
from environment variables (GitHub Actions secrets or a local .env).

Source list finalized in Phase 0 (verified 2026-07-16/17):
- 20 automated RSS/Atom sources (X/Instagram excluded: paid API / ToS risk;
  covered via Meta AI, Ollama, xAI feeds + existing sources).
- Gmail newsletters are a Phase 3 source (Task 9, STEP [14]) wired through
  src/gmail_fetch.py as source_id 18 — NOT an RSS tuple, so absent from SOURCES.
  Shared id 18 means MAX_PER_SOURCE_IN_TOP caps the whole newsletter layer at 2
  in the digest (intended: newsletters are a priority-3 supplement, not the lead).
"""

import os

# Load .env from workspace root if it exists
_env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#"):
                continue
            if "=" in _line:
                _key, _val = _line.split("=", 1)
                os.environ[_key.strip()] = _val.strip().strip("'\"")

# ---------------------------------------------------------------------------
# HTTP settings
# ---------------------------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 20                 # seconds per feed request
REDDIT_DELAY_S = 6           # FIX [5] 3->6s: token worked but 3s bursts still 429'd
REDDIT_PARAMS_ENV = "REDDIT_FEED_PARAMS"  # env var: "feed=TOKEN&user=NAME"
MAX_ENTRIES_PER_SOURCE = 30  # some feeds ship 1000+ entries; cap the newest N

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
# priority: base score added during ranking (higher = more important to Harvey)
#   10 = Claude/Anthropic   8 = Gemini/Google   6 = OpenAI
#    4 = other model/tool vendors   3 = aggregators/media (rely on keywords)
SOURCES = [
    # id, name, url, priority
    (1,  "OpenAI News",               "https://openai.com/news/rss.xml",                                                            6),
    (2,  "Google AI Blog",            "https://blog.google/innovation-and-ai/technology/ai/rss/",                                   8),
    (3,  "Gemini Blog",               "https://blog.google/products-and-platforms/products/gemini/rss/",                            8),
    (4,  "Google DeepMind",           "https://deepmind.google/blog/rss.xml",                                                       8),
    (5,  "Anthropic News (tim-hilde)","https://tim-hilde.github.io/anthropic-rss/rss.xml",                                          10),
    (6,  "Anthropic News (taobojlen)","https://raw.githubusercontent.com/taobojlen/anthropic-rss-feed/main/anthropic_news_rss.xml", 10),
    (7,  "Anthropic Eng (Olshansk)",  "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_engineering.xml", 10),
    (8,  "MarkTechPost",              "https://www.marktechpost.com/feed/",                                                         3),
    (9,  "r/Anthropic",               "https://www.reddit.com/r/Anthropic/.rss",                                                    2),  # FIX [5] 5->2
    (10, "r/LocalLLaMA",              "https://www.reddit.com/r/LocalLLaMA/.rss",                                                   2),  # FIX [5] 4->2
    (11, "r/OpenAI",                  "https://www.reddit.com/r/OpenAI/.rss",                                                       2),  # FIX [5] 4->2
    (12, "Vaibhav Sisinty (YouTube)", "https://www.youtube.com/feeds/videos.xml?channel_id=UClXAalunTPaX1YV185DWUeg",               4),
    (13, "Hugging Face Blog",         "https://huggingface.co/blog/feed.xml",                                                       4),
    (14, "The Verge AI",              "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",                          3),
    (15, "VentureBeat AI",            "https://venturebeat.com/category/ai/feed/",                                                  3),
    (16, "Ars Technica AI",           "https://arstechnica.com/ai/feed/",                                                           3),
    (17, "HN frontpage",              "https://hnrss.org/frontpage",                                                                3),
    (19, "Meta AI Blog (Olshansk)",   "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_meta_ai.xml",           4),
    (20, "Ollama Blog (Olshansk)",    "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_ollama.xml",            4),
    (21, "xAI News (Olshansk)",       "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_xainews.xml",           4),
]

# HN is high-volume and mostly non-AI: keep an entry only if its title
# contains one of these (case-insensitive substring match).
HN_AI_KEYWORDS = [
    "ai", "llm", "gpt", "claude", "anthropic", "gemini", "openai", "deepmind",
    "mistral", "llama", "qwen", "grok", "xai", "ollama", "hugging face",
    "transformer", "diffusion", "agent", "copilot", "chatbot", "neural",
    "machine learning", "deep learning", "rag", "fine-tun", "open weights",
    "open-source model", "inference", "genai",
]
# Guard against false positives from bare-"ai" substrings ("air", "aid"...):
# "ai" and "agent" only count as whole words (handled in fetch.py).
HN_WHOLE_WORD_ONLY = {"ai", "agent", "rag"}
HN_EXCLUDE_PREFIXES = ("launch hn", "ask hn")  # FIX [5] startup self-promo / Q&A, not news

# ---------------------------------------------------------------------------
# Ranking (used by rank.py)                                        # STEP [2]
# ---------------------------------------------------------------------------
MAX_STORY_AGE_H = 48         # STEP [2] discard stories older than this (or undated)
RECENCY_MAX_POINTS = 12      # FIX [5] 24->12: recency is a tiebreaker, not the dominator
TOP_N_STORIES = 5            # STEP [2] stories handed to the LLM
MAX_PER_SOURCE_IN_TOP = 2    # FIX [5] source diversity in the final selection
COMMUNITY_SOURCE_IDS = {9, 10, 11, 17}  # FIX [5] Reddit + HN: chatter, not announcements
COMMUNITY_TOPIC_FACTOR = 0.5            # FIX [5] community posts get half the topic bonus
HISTORY_FILE = "history.json"      # STEP [2] committed story-hash log (repo root)
HISTORY_RETENTION_DAYS = 30        # STEP [2] prune hashes older than this

# STEP [2] topic bonus tiers: highest matching tier wins (not summed),
# mirroring Harvey's content priorities. Launch/use-case bonuses stack on top.
TOPIC_TIERS = [
    (10, ["claude", "anthropic", "fable 5", "mythos"]),
    (8,  ["gemini", "deepmind", "google ai", "gemma", "nano banana"]),
    (6,  ["openai", "gpt", "chatgpt", "codex", "sora"]),
]
LAUNCH_BONUS = 4             # STEP [2]
LAUNCH_KEYWORDS = ["introducing", "launches", "launched", "announces",
                   "announcing", "unveils", "releases", "released",
                   "new model", "now available", "open-source", "open weights"]
USECASE_BONUS = 2            # STEP [2]
USECASE_KEYWORDS = ["how to", "guide", "tutorial", "use case", "workflow",
                    "automation", "built with", "hands-on"]

# ---------------------------------------------------------------------------
# LLM (used by generate.py)                                        # STEP [3]
# ---------------------------------------------------------------------------
LLM_PROVIDER_ENV = "LLM_PROVIDER"      # STEP [3] "gemini" (default) or "ollama"
GEMINI_KEY_ENV = "GEMINI_API_KEY"      # STEP [3] secret: env var name only, never the value
GEMINI_MODEL = "gemini-3.5-flash"        # STEP [3] free-tier workhorse; model names shift, keep here
OLLAMA_URL = "http://localhost:11434/api/generate"   # STEP [3] local fallback
OLLAMA_MODEL = "qwen2.5:7b"            # STEP [3] best structured summarizer on 8GB RAM
LLM_TEMPERATURE = 0.7                  # STEP [3]
LLM_TIMEOUT = 120                      # STEP [3] seconds (local models can be slow)
LLM_RETRIES = 2                        # STEP [3] retries after the first attempt
LLM_BACKOFF_S = [5, 20]                # STEP [3] wait before retry 1, retry 2
MIN_STORIES_TO_GENERATE = 2            # STEP [3] below this, still generate but warn loudly

# STEP [3] post-quality bounds used for validation warnings (not hard failures)
POST_MIN_CHARS = 1300
POST_MAX_CHARS = 1900
HOOK_MAX_CHARS = 140

# ---------------------------------------------------------------------------
# Telegram approval (used by notify.py)                             # STEP [8]
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"    # STEP [8] secret: env var name only
TELEGRAM_CHAT_ID_ENV = "TELEGRAM_CHAT_ID"    # STEP [8] secret: env var name only
TELEGRAM_MSG_LIMIT = 4096                    # STEP [8] hard Bot API cap per message
PENDING_POST_FILE = "pending_post.json"      # STEP [8] repo-root state file for Run B

# ---------------------------------------------------------------------------
# Approval poller / Run B (used by approve.py)                      # STEP [9]
# ---------------------------------------------------------------------------
# STEP [18] Was 14h (STEP [13]). Raised to 26h: with 24h polling, a tap at any
# STEP [18] hour is read within 30 min, but the DRAFT must still be un-expired
# STEP [18] when the poll reads it. A draft created at 08:23 IST that Harvey taps
# STEP [18] at 23:00 is read at 23:23 IST (15h old, fine). But if the 23:23 poll
# STEP [18] is missed (GitHub cron delay), the next morning's poll at 08:53 IST
# STEP [18] is 24.5h after creation — needs >24h expiry to still be approvable.
# STEP [18] The old "never above 24" concern was about Telegram's 24h update
# STEP [18] retention, but that's measured from TAP time (not draft creation).
# STEP [18] A tap at 23:00 IST is still in Telegram's queue at 08:53 IST (10h < 24h).
APPROVAL_EXPIRY_H = 26
TELEGRAM_UPDATE_LIMIT = 100   # STEP [9] getUpdates max

# ---------------------------------------------------------------------------
# LinkedIn posting (used by post_linkedin.py / approve.py)           # STEP [10]
# ---------------------------------------------------------------------------
# STEP [10] Official "Share on LinkedIn" product, w_member_social scope,
# STEP [10] POST https://api.linkedin.com/rest/posts — the ONLY sanctioned
# STEP [10] method (hard constraint #2: no cookie/session/scraping ever).
# STEP [10] env-var NAMES only here; values live in GitHub secrets / .env.
LINKEDIN_TOKEN_ENV = "LINKEDIN_ACCESS_TOKEN"          # STEP [10] secret: env var NAME only
LINKEDIN_PERSON_URN_ENV = "LINKEDIN_PERSON_URN"       # STEP [10] secret: env var NAME only
LINKEDIN_POSTS_URL = "https://api.linkedin.com/rest/posts"  # STEP [10] official Posts API
LINKEDIN_IMAGES_URL = "https://api.linkedin.com/rest/images"  # STEP [12] image upload (initializeUpload)
LINKEDIN_VERSION = "202607"                            # STEP [10] LinkedIn-Version header (bump here only; active for 12mo)
# STEP [10] Standard-app token: 60-day credential, NO refresh. The issued-date
# STEP [10] env var (ISO stamp you set at OAuth time) lets token_status.py warn
# STEP [10] before expiry so Harvey can re-auth — non-fatal if missing/malformed.
LINKEDIN_TOKEN_ISSUED_UTC_ENV = "LINKEDIN_TOKEN_ISSUED_UTC"  # STEP [10] ISO date for age check
LINKEDIN_TOKEN_WARN_DAYS = 50                          # STEP [10] re-auth nudge threshold (dies at 60d)
# STEP [10] Dry-run switch: =1 logs the exact request and returns a fake success
# STEP [10] WITHOUT calling LinkedIn — proves the approve→post path end-to-end
# STEP [10] with a real Telegram tap while posting nothing live.
LINKEDIN_DRY_RUN_ENV = "LINKEDIN_DRY_RUN"              # STEP [10] =1 → fake success, no API call

# ---------------------------------------------------------------------------
# Image attachment (Phase 3, Task 8)                                # STEP [11]
# ---------------------------------------------------------------------------
# STEP [11] Image candidates come from two sources: the top-ranked story's
# STEP [11] feed image (auto), or a Telegram photo reply (manual override).
# STEP [11] An image failure ALWAYS downgrades to text-only — it must never
# STEP [11] block, delay, or fail a post. Validation (type/size) happens in 8b
# STEP [11] before the LinkedIn upload; 8a only captures the candidates.
IMAGE_MAX_BYTES = 5_242_880        # STEP [11] 5 MB LinkedIn image upload cap
IMAGE_ALLOWED_TYPES = ("image/jpeg", "image/png")  # STEP [11] LinkedIn-accepted
IMAGE_TEMP_PREFIX = "digest_photo_"  # STEP [11] tempfile prefix for TG photos

# ---------------------------------------------------------------------------
# Gmail newsletters (Phase 3, Task 9)                              # STEP [14]
# ---------------------------------------------------------------------------
# STEP [14] Newsletters selected by GMAIL LABEL only (not sender lists), last
# STEP [14] GMAIL_LOOKBACK_H hours. Each qualifying link in a newsletter becomes
# STEP [14] one candidate Story (title=anchor text, link=URL, summary="",
# STEP [14] published=email Date, source_name="Newsletter: <sender>").
# STEP [14] ADDITIVE + ALWAYS non-fatal: any IMAP/parse failure -> WARNING + [],
# STEP [14] the pipeline still ships today's normal RSS post. Stdlib only
# STEP [14] (imaplib + email + html.parser) — no new deps.
# STEP [14] env-var NAMES only here; values live in GitHub secrets / .env.
GMAIL_ADDRESS_ENV = "GMAIL_ADDRESS"            # STEP [14] secret: env var NAME only
GMAIL_APP_PASSWORD_ENV = "GMAIL_APP_PASSWORD"  # STEP [14] secret: env var NAME only (16-char app code)
GMAIL_LABEL = "AI-Newsletter"                  # STEP [15] FIX: match Harvey's actual Gmail label (singular). Was "AI-Newsletters" → count=0 every run.
GMAIL_LOOKBACK_H = 26                          # STEP [14] ~daily cadence w/ slack
GMAIL_MAX_LINKS_PER_EMAIL = 8                  # STEP [14] cap, applied in document order
NEWSLETTER_SOURCE_ID = 18                      # STEP [14] reserved id (see comment line 11)
NEWSLETTER_PRIORITY = 3                        # STEP [14] same tier as media/aggregators
NEWSLETTER_MIN_ANCHOR_CHARS = 15               # STEP [14] drop links w/ too-short anchor text
# STEP [14] boilerplate anchors dropped (case-insensitive substring match on the
# STEP [14] anchor text): footers, social icons, ad slots. Bare social-platform
# STEP [14] names can over-match a rare headline about that platform — accepted.
NEWSLETTER_BOILERPLATE = (
    "unsubscribe", "view in browser", "view online", "privacy", "preferences",
    "sponsor", "sponsored", "advertise", "advertising", "mailto:",
    "twitter", "x.com", "linkedin", "facebook", "instagram", "youtube",
    "tiktok", "forward to a friend", "update your profile", "manage preferences",
)