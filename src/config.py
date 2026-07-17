"""
config.py — Canonical configuration for the LinkedIn AI News Digest Bot.

Single source of truth for: news sources (with ranking priority), HTTP
settings, and keyword lists. No secrets in this file, ever — secrets come
from environment variables (GitHub Actions secrets or a local .env).

Source list finalized in Phase 0 (verified 2026-07-16/17):
- 20 automated RSS/Atom sources (X/Instagram excluded: paid API / ToS risk;
  covered via Meta AI, Ollama, xAI feeds + existing sources).
- Gmail newsletters (#18) are Phase 3 and intentionally absent here.
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