"""
test_youtube_enrich.py — offline checks for Phase 3 Task 11 (YouTube transcripts).
                                                    # STEP [16]

NO NETWORK. The youtube-transcript-api library is stubbed via sys.modules
injection (works whether or not the real library is installed, since
youtube_enrich._fetch_transcript_text imports it lazily inside the function).

Tests cover:
- video-id extraction (watch?v= and youtu.be/ forms)
- source gate (source_id != 12 untouched, no API call)
- successful transcript -> summary populated
- trim to YT_TRANSCRIPT_MAX_CHARS on a word boundary
- missing captions / generic exception -> story returned unchanged, no raise
- log level INFO (not WARNING) for the normal missing-captions case
- manual transcript preferred over auto
- fetch.py import-gate: library absent -> clean skip, other stories unaffected
- YT_MAX_ENRICH cap respected

Run from the repo root:  python test_youtube_enrich.py
"""

import logging
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import config          # noqa: E402
import youtube_enrich  # noqa: E402


# ---------------------------------------------------------------------------
# Stubs for youtube_transcript_api (installed into sys.modules per-test)
# ---------------------------------------------------------------------------
class _FakeSnippetObj:
    """Object-form snippet (youtube-transcript-api 1.x shape)."""
    def __init__(self, text):
        self.text = text


class _FakeSnippetDict(dict):
    """Dict-form snippet (youtube-transcript-api 0.6.x shape)."""


class _FakeTranscript:
    def __init__(self, snippets, is_generated=False, language_code="en"):
        self._snippets = snippets
        self.is_generated = is_generated
        self.language_code = language_code
        self.language = language_code

    def fetch(self):
        return self._snippets


class _NoTranscript(Exception):
    """Stand-in for the real NoTranscriptFound; _fetch catches Exception broadly."""


class _FakeTranscriptList:
    def __init__(self, transcripts):
        self._transcripts = transcripts

    def find_manually_created_transcript(self, codes):
        for t in self._transcripts:
            if not t.is_generated and t.language_code in codes:
                return t
        raise _NoTranscript("no manual transcript")

    def find_generated_transcript(self, codes):
        for t in self._transcripts:
            if t.is_generated and t.language_code in codes:
                return t
        raise _NoTranscript("no generated transcript")

    def __iter__(self):
        return iter(self._transcripts)


class _FakeApi:
    """Configurable stub for YouTubeTranscriptApi. Set .list_return / .list_raises.

    Supports BOTH the 1.x `list` instance method and the 0.6.x `list_transcripts`
    classmethod so the version-probing code in _fetch_transcript_text is covered."""
    list_return = None
    list_raises = None

    @classmethod
    def list_transcripts(cls, video_id):  # 0.6.x classmethod form
        if cls.list_raises is not None:
            exc = cls.list_raises
            cls.list_raises = None  # one-shot
            raise exc
        return cls.list_return

    def list(self, video_id):  # 1.x instance method form — delegates to the classmethod
        return type(self).list_transcripts(video_id)


def _install_fake_api():
    """Install _FakeApi as youtube_transcript_api in sys.modules."""
    mod = types.ModuleType("youtube_transcript_api")
    mod.YouTubeTranscriptApi = _FakeApi
    mod.NoTranscriptFound = _NoTranscript  # exposed for tests that want it
    sys.modules["youtube_transcript_api"] = mod


def _reset_fake_api():
    _FakeApi.list_return = None
    _FakeApi.list_raises = None


def _yt_story(title="The results shocked me!",
              link="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
              summary=""):
    return {"source_id": config.YOUTUBE_SOURCE_ID,
            "source_name": "Vaibhav Sisinty (YouTube)",
            "priority": 4, "title": title, "link": link,
            "summary": summary, "published": None, "image_url": None}


# ---------------------------------------------------------------------------
# video-id extraction
# ---------------------------------------------------------------------------
def test_video_id_from_watch_url():
    assert youtube_enrich._extract_video_id(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_video_id_from_watch_url_with_params():
    assert youtube_enrich._extract_video_id(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=shared&t=42") == "dQw4w9WgXcQ"


def test_video_id_from_youtu_be():
    assert youtube_enrich._extract_video_id(
        "https://youtu.be/dQw4w9WgXcQ?t=10") == "dQw4w9WgXcQ"


def test_video_id_from_shorts_url():
    assert youtube_enrich._extract_video_id(
        "https://www.youtube.com/shorts/SsNRQuLRz4Y") == "SsNRQuLRz4Y"


def test_video_id_bad_url_returns_none():
    assert youtube_enrich._extract_video_id("https://example.com/no/v/here") is None
    assert youtube_enrich._extract_video_id("") is None
    assert youtube_enrich._extract_video_id(None) is None


# ---------------------------------------------------------------------------
# source gate: source_id != 12 untouched, no API call
# ---------------------------------------------------------------------------
def test_non_youtube_source_untouched():
    _install_fake_api()
    _reset_fake_api()
    calls = {"n": 0}
    orig = _FakeApi.list_transcripts

    def counting(video_id):
        calls["n"] += 1
        return orig(video_id)
    _FakeApi.list_transcripts = counting
    try:
        story = {"source_id": 2, "source_name": "Google AI Blog", "priority": 8,
                 "title": "Gemini update", "link": "https://youtube.com/watch?v=abc12345678",
                 "summary": "original", "published": None, "image_url": None}
        out = youtube_enrich.enrich(story)
        assert out is story
        assert out["summary"] == "original"
        assert calls["n"] == 0, "no API call should be made for non-YouTube source"
    finally:
        _FakeApi.list_transcripts = orig


# ---------------------------------------------------------------------------
# successful transcript -> summary populated
# ---------------------------------------------------------------------------
def test_successful_transcript_populates_summary():
    _install_fake_api()
    _reset_fake_api()
    _FakeApi.list_return = _FakeTranscriptList([
        _FakeTranscript([_FakeSnippetObj("hello world"), _FakeSnippetObj("second part")])
    ])
    story = _yt_story(summary="")
    out = youtube_enrich.enrich(story)
    assert "hello world" in out["summary"]
    assert "second part" in out["summary"]
    assert out["summary"].startswith("hello world")


def test_successful_transcript_dict_snippets():
    # 0.6.x dict-form snippets also supported.
    _install_fake_api()
    _reset_fake_api()
    _FakeApi.list_return = _FakeTranscriptList([
        _FakeTranscript([_FakeSnippetDict({"text": "dict form works"})])
    ])
    out = youtube_enrich.enrich(_yt_story())
    assert "dict form works" in out["summary"]


def test_successful_transcript_prefixes_when_original_summary_real():
    _install_fake_api()
    _reset_fake_api()
    _FakeApi.list_return = _FakeTranscriptList([
        _FakeTranscript([_FakeSnippetObj("transcript body")])
    ])
    story = _yt_story(summary="Real RSS summary text here")
    out = youtube_enrich.enrich(story)
    assert out["summary"].startswith("Real RSS summary text here")
    assert "transcript body" in out["summary"]


# ---------------------------------------------------------------------------
# trim to cap
# ---------------------------------------------------------------------------
def test_transcript_trimmed_to_cap():
    _install_fake_api()
    _reset_fake_api()
    _FakeApi.list_return = _FakeTranscriptList([
        _FakeTranscript([_FakeSnippetObj("alpha " * 400)])  # 2000 chars
    ])
    story = _yt_story()
    orig_cap = config.YT_TRANSCRIPT_MAX_CHARS
    try:
        config.YT_TRANSCRIPT_MAX_CHARS = 100
        out = youtube_enrich.enrich(story)
        body = out["summary"]
        # 100 + ellipsis char at most (trim backs up to a word boundary, then +1 ellipsis)
        assert len(body) <= 101, f"expected <=101 chars after trim, got {len(body)}"
        assert body.endswith("\u2026"), "trimmed text should end with ellipsis"
        assert "alpha" in body  # still has content
    finally:
        config.YT_TRANSCRIPT_MAX_CHARS = orig_cap


def test_transcript_under_cap_not_trimmed():
    _install_fake_api()
    _reset_fake_api()
    _FakeApi.list_return = _FakeTranscriptList([
        _FakeTranscript([_FakeSnippetObj("short")])
    ])
    out = youtube_enrich.enrich(_yt_story())
    assert out["summary"] == "short"
    assert not out["summary"].endswith("\u2026")


# ---------------------------------------------------------------------------
# missing captions / generic exception -> unchanged, no raise
# ---------------------------------------------------------------------------
def test_missing_captions_returns_unchanged():
    _install_fake_api()
    _reset_fake_api()
    # No manual, no auto, empty list -> falls through to no candidate.
    _FakeApi.list_return = _FakeTranscriptList([])
    story = _yt_story(summary="")
    out = youtube_enrich.enrich(story)  # must not raise
    assert out["summary"] == ""
    assert out["title"] == "The results shocked me!"


def test_list_transcripts_raises_returns_unchanged():
    _install_fake_api()
    _reset_fake_api()
    _FakeApi.list_raises = _NoTranscript("video unavailable")
    story = _yt_story(summary="")
    out = youtube_enrich.enrich(story)  # must not raise
    assert out["summary"] == ""


def test_fetch_raises_returns_unchanged():
    _install_fake_api()
    _reset_fake_api()

    class _BoomTranscript:
        is_generated = False
        language_code = "en"
        def fetch(self):
            raise ConnectionError("rate limited")
    _FakeApi.list_return = _FakeTranscriptList([_BoomTranscript()])
    story = _yt_story(summary="original")
    out = youtube_enrich.enrich(story)  # must not raise
    assert out["summary"] == "original"


def test_no_video_id_returns_unchanged():
    _install_fake_api()
    _reset_fake_api()
    story = _yt_story(link="https://example.com/not-youtube")
    out = youtube_enrich.enrich(story)
    assert out["summary"] == ""


# ---------------------------------------------------------------------------
# log level INFO (not WARNING) for missing captions
# ---------------------------------------------------------------------------
def test_missing_captions_logged_at_info_not_warning():
    _install_fake_api()
    _reset_fake_api()
    _FakeApi.list_raises = _NoTranscript("none")
    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Handler(level=logging.DEBUG)
    logger = logging.getLogger("youtube_enrich")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        youtube_enrich.enrich(_yt_story())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    yt_records = [r for r in records if "youtube_enrich" in r.name]
    assert yt_records, "expected at least one log record from enrichment"
    assert all(r.levelno <= logging.INFO for r in yt_records), \
        f"missing-captions must log at INFO, got levels {[r.levelname for r in yt_records]}"


# ---------------------------------------------------------------------------
# manual transcript preferred over auto
# ---------------------------------------------------------------------------
def test_manual_preferred_over_auto():
    _install_fake_api()
    _reset_fake_api()
    manual = _FakeTranscript([_FakeSnippetObj("MANUAL TEXT")], is_generated=False)
    auto = _FakeTranscript([_FakeSnippetObj("AUTO TEXT")], is_generated=True)
    _FakeApi.list_return = _FakeTranscriptList([manual, auto])
    out = youtube_enrich.enrich(_yt_story())
    assert "MANUAL TEXT" in out["summary"]
    assert "AUTO TEXT" not in out["summary"]


def test_auto_used_when_no_manual():
    _install_fake_api()
    _reset_fake_api()
    auto = _FakeTranscript([_FakeSnippetObj("AUTO ONLY")], is_generated=True)
    _FakeApi.list_return = _FakeTranscriptList([auto])
    out = youtube_enrich.enrich(_yt_story())
    assert "AUTO ONLY" in out["summary"]


def test_legacy_0_6_x_classmethod_path():
    """When the library only exposes list_transcripts (0.6.x classmethod, no
    `list`), the version-probe must fall back to it. Simulate by hiding `list`."""
    _install_fake_api()
    _reset_fake_api()
    _FakeApi.list_return = _FakeTranscriptList([
        _FakeTranscript([_FakeSnippetObj("LEGACY PATH OK")])
    ])
    # Hide the 1.x `list` attribute so the probe takes the list_transcripts branch.
    orig_list = _FakeApi.list
    delattr(_FakeApi, "list")
    try:
        out = youtube_enrich.enrich(_yt_story())
        assert "LEGACY PATH OK" in out["summary"]
    finally:
        _FakeApi.list = orig_list


# ---------------------------------------------------------------------------
# fetch.py integration: import-gate + cap (via _enrich_youtube directly)
# ---------------------------------------------------------------------------
def test_library_absent_skips_cleanly():
    # Simulate youtube-transcript-api not installed: sys.modules[...] = None
    # makes `import youtube_transcript_api` raise ImportError.
    saved = sys.modules.get("youtube_transcript_api")
    sys.modules["youtube_transcript_api"] = None
    try:
        import importlib
        import fetch
        importlib.reload(fetch)  # re-import so the gate sees the None entry
        # A non-YouTube story must be completely unaffected.
        other = {"source_id": 2, "source_name": "Google AI Blog", "priority": 8,
                 "title": "Gemini", "link": "https://g/x", "summary": "keep me",
                 "published": None, "image_url": None}
        yt = _yt_story(summary="yt-title-only")
        fetch._enrich_youtube([other, yt])  # must not raise
        assert other["summary"] == "keep me"
        assert yt["summary"] == "yt-title-only"  # untouched (gate skipped)
    finally:
        if saved is not None:
            sys.modules["youtube_transcript_api"] = saved
        else:
            sys.modules.pop("youtube_transcript_api", None)
        import importlib
        import fetch
        importlib.reload(fetch)  # restore real module reference for later tests


def test_max_enrich_respected():
    _install_fake_api()
    _reset_fake_api()
    # Monkeypatch _fetch_transcript_text directly: counts calls into the cap
    # without needing to drive the fake API's classmethod plumbing. enrich()
    # resolves _fetch_transcript_text by bare name via youtube_enrich's globals,
    # so this patch takes effect for every enrich() call inside _enrich_youtube.
    calls = []
    orig_fetch = youtube_enrich._fetch_transcript_text

    def counting_fetch(video_id):
        calls.append(video_id)
        return f"body {video_id}"
    youtube_enrich._fetch_transcript_text = counting_fetch
    import fetch

    stories = []
    for i in range(5):
        stories.append(_yt_story(
            link=f"https://www.youtube.com/watch?v=vid0000000{i}",
            summary=""))
    orig_cap = config.YT_MAX_ENRICH
    try:
        config.YT_MAX_ENRICH = 2
        fetch._enrich_youtube(stories)
        assert len(calls) == 2, f"cap=2 should yield 2 fetches, got {len(calls)}"
        enriched = [s for s in stories if s["summary"].startswith("body ")]
        assert len(enriched) == 2, f"expected 2 enriched, got {len(enriched)}"
    finally:
        config.YT_MAX_ENRICH = orig_cap
        youtube_enrich._fetch_transcript_text = orig_fetch


def test_enrich_youtube_leaves_non_yt_stories_alone():
    _install_fake_api()
    _reset_fake_api()
    _FakeApi.list_return = _FakeTranscriptList([
        _FakeTranscript([_FakeSnippetObj("yt body")])
    ])
    import fetch
    other = {"source_id": 1, "source_name": "OpenAI News", "priority": 6,
             "title": "GPT-6", "link": "https://openai.com/x",
             "summary": "untouchable", "published": None, "image_url": None}
    yt = _yt_story(summary="")
    fetch._enrich_youtube([other, yt])
    assert other["summary"] == "untouchable"
    assert yt["summary"] == "yt body"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} checks passed.")
    sys.exit(1 if failed else 0)
