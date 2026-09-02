"""
test_gmail.py — offline checks for Phase 3 Task 9 (Gmail newsletters). STEP [14]

No network, no real IMAP. Tests the link extractor (_extract_links), the email
parser (_parse_one_email / MIME decode), and the failure / non-fatal paths
directly. Also wires a tiny stub through rank.dedupe_and_rank to prove a
newsletter duplicate of an RSS title collapses, that newsletters can ADD a fresh
story, and that the shared source_id 18 caps the newsletter layer at 2.

Run from the repo root:  python test_gmail.py
"""

import base64
import os
import quopri
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import config      # noqa: E402
import gmail_fetch  # noqa: E402
import rank         # noqa: E402
import retryutil    # noqa: E402  STEP [26] neutralize IMAP retry sleeps

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Inline newsletter fixtures (3 layouts: TLDR list, Rundown cards, plaintext)
# ---------------------------------------------------------------------------
TLDR_HTML = """<html><body>
<h2>TLDR AI</h2>
<ul>
<li><a href="https://example.com/anthropic-fable">Anthropic releases Fable 5 with improved reasoning</a></li>
<li><a href="https://example.com/gemini-drop">Google's Gemini gets a major context upgrade this week</a></li>
<li><a href="https://example.com/openai-pricing">OpenAI cuts API pricing across the GPT lineup</a></li>
</ul>
<hr>
<a href="https://tldr.com/unsubscribe">Unsubscribe</a> |
<a href="https://tldr.com/web">View in browser</a> |
<a href="https://tldr.com/x">Follow us on X (Twitter)</a> |
<a href="mailto:hi@tldr.com">Contact us today please</a>
</body></html>"""

RUNDOWN_HTML = """<html><body><table>
<tr><td><a href="https://rundown.ai/card1">Meta's new Llama 4 model benchmarks revealed</a></td></tr>
<tr><td><a href="https://rundown.ai/card2">Mistral announces open weights for their latest release</a></td></tr>
<tr><td><a href="https://rundown.ai/more">Read more</a></td></tr>
<tr><td><a href="https://rundown.ai/social">Follow us on X (Twitter)</a></td></tr>
</table></body></html>"""

PLAIN_TEXT = (
    "Welcome to Import AI.\n\n"
    "Story one: https://importai.net/stories/123 about new research\n"
    "Story two: https://importai.net/stories/456 here\n"
    "Unsubscribe: https://importai.net/unsub\n"
)


def _raw_email(html=None, text=None,
               sender="TLDR AI <tldr@example.com>",
               date="Wed, 29 Jul 2026 07:00:00 +0000", cte=None):
    """Build raw RFC822 bytes for a single-part text/html or text/plain email.

    cte in {'base64','quoted-printable',None} exercises transfer-encoding decode."""
    if html is not None:
        if cte == "base64":
            body = base64.b64encode(html.encode("utf-8")).decode("ascii")
        elif cte == "quoted-printable":
            body = quopri.encodestring(html.encode("utf-8")).decode("ascii")
        else:
            body = html
        ctype = 'text/html; charset="utf-8"'
    else:
        body = text if text is not None else ""
        ctype = 'text/plain; charset="utf-8"'
    return (
        f"From: {sender}\r\n"
        f"Date: {date}\r\n"
        "MIME-Version: 1.0\r\n"
        f"Content-Type: {ctype}\r\n"
        + (f"Content-Transfer-Encoding: {cte}\r\n" if cte else "")
        + "\r\n"
        f"{body}\r\n"
    ).encode("utf-8", "replace")


def _nl(title, link="https://nl.example/x", published=NOW):
    """A newsletter Story dict (source_id 18)."""
    return {"source_id": config.NEWSLETTER_SOURCE_ID, "source_name": "Newsletter: T",
            "priority": config.NEWSLETTER_PRIORITY, "title": title, "link": link,
            "summary": "", "published": published, "image_url": None}


# --- HTML link extraction ------------------------------------------------

def test_tldr_headlines_extracted():
    stories = gmail_fetch._extract_links(TLDR_HTML, "TLDR AI", NOW)
    titles = [s["title"] for s in stories]
    assert len(stories) == 3, f"expected 3 headlines, got {len(stories)}: {titles}"
    assert "Anthropic releases Fable 5 with improved reasoning" in titles


def test_tldr_boilerplate_dropped():
    stories = gmail_fetch._extract_links(TLDR_HTML, "TLDR AI", NOW)
    titles = [s["title"] for s in stories]
    for bad in ("Unsubscribe", "View in browser", "Follow us on X (Twitter)",
                "Contact us today please"):
        assert bad not in titles, f"boilerplate '{bad}' was not dropped"


def test_tldr_story_shape():
    s = gmail_fetch._extract_links(TLDR_HTML, "TLDR AI", NOW)[0]
    assert s["source_id"] == config.NEWSLETTER_SOURCE_ID == 18
    assert s["priority"] == config.NEWSLETTER_PRIORITY == 3
    assert s["source_name"] == "Newsletter: TLDR AI"
    assert s["image_url"] is None
    assert s["summary"] == ""
    assert s["published"] == NOW
    assert s["link"].startswith("http")


def test_rundown_table_layout():
    # 2 real headlines kept; "Read more" (9 chars) + social (boilerplate) dropped.
    stories = gmail_fetch._extract_links(RUNDOWN_HTML, "The Rundown AI", NOW)
    assert len(stories) == 2, f"expected 2, got {len(stories)}"
    assert all("Llama" in s["title"] or "Mistral" in s["title"] for s in stories)


def test_min_anchor_length_filter():
    html = '<a href="https://x.com/a">Read more</a>'  # 9 chars, not boilerplate
    assert gmail_fetch._extract_links(html, "S", NOW) == []


def test_min_anchor_length_boundary_15():
    # 15-char anchor passes (>=), 14-char anchor drops.
    kept = gmail_fetch._extract_links(
        '<a href="https://e.com/a">AnthropicFable5</a>', "S", NOW)  # 15 chars
    dropped = gmail_fetch._extract_links(
        '<a href="https://e.com/b">AnthropicFable</a>', "S", NOW)   # 14 chars
    assert len(kept) == 1
    assert dropped == []


def test_dup_urls_collapsed():
    html = ('<a href="https://dup.example/a">First headline long enough</a>'
            '<a href="https://dup.example/a">Second headline long enough</a>')
    out = gmail_fetch._extract_links(html, "S", NOW)
    assert len(out) == 1, f"expected dup collapsed to 1, got {len(out)}"


def test_non_http_schemes_dropped():
    html = ('<a href="mailto:a@b.com">Contact us today please</a>'
            '<a href="javascript:void(0)">Click this anchor please</a>'
            '<a href="ftp://x/y/file">An ftp link headline here</a>'
            '<a href="#section">An in-page anchor headline here</a>')
    assert gmail_fetch._extract_links(html, "S", NOW) == []


def test_cap_respected_document_order():
    orig = config.GMAIL_MAX_LINKS_PER_EMAIL
    try:
        config.GMAIL_MAX_LINKS_PER_EMAIL = 3
        html = "".join(
            f'<a href="https://e.com/{i}">Headline number {i} is long enough</a>'
            for i in range(6))
        out = gmail_fetch._extract_links(html, "S", NOW)
        assert len(out) == 3, f"cap should yield 3, got {len(out)}"
        assert out[0]["link"] == "https://e.com/0"  # document order preserved
    finally:
        config.GMAIL_MAX_LINKS_PER_EMAIL = orig


def test_nested_formatting_text_collected():
    # Bold inside an anchor should still yield the full headline as anchor text.
    html = '<a href="https://e.com/a"><b>Anthropic</b> ships Fable 5 today</a>'
    out = gmail_fetch._extract_links(html, "S", NOW)
    assert len(out) == 1
    assert out[0]["title"] == "Anthropic ships Fable 5 today"


# --- plaintext fallback (documents "contributes little") ---------------

def test_plaintext_contributes_little():
    # Bare URLs have no anchor text -> all dropped by MIN_ANCHOR_CHARS.
    out = gmail_fetch._extract_links(PLAIN_TEXT, "Import AI", NOW, is_html=False)
    assert out == [], f"plaintext should yield [], got {len(out)}"


# --- MIME decode --------------------------------------------------------

def test_mime_base64_decoded():
    raw = _raw_email(html=TLDR_HTML, cte="base64")
    stories = gmail_fetch._parse_one_email(raw)
    titles = [s["title"] for s in stories]
    assert len(stories) == 3, f"base64 body should decode to 3 links, got {len(stories)}"
    assert any("Anthropic" in t for t in titles)


def test_mime_quoted_printable_decoded():
    # Em dash (U+2014) forces real QP encoding (=E2=80=94); decode must round-trip.
    html = '<a href="https://e.com/a">Anthropic launches — Fable 5 today</a>'
    raw = _raw_email(html=html, cte="quoted-printable")
    out = gmail_fetch._parse_one_email(raw)
    assert len(out) == 1
    assert "Fable 5" in out[0]["title"]


def test_email_date_parsed_to_utc():
    raw = _raw_email(html=TLDR_HTML, date="Wed, 29 Jul 2026 09:30:00 +0530")
    when = gmail_fetch._parse_one_email(raw)[0]["published"]
    assert when is not None
    assert when.tzinfo is not None
    assert when.utcoffset().total_seconds() == 0   # normalized to UTC
    assert when.hour == 4                            # 09:30 +0530 -> 04:00 UTC


def test_email_date_unparseable_returns_none():
    raw = _raw_email(html=TLDR_HTML, date="not a real date")
    when = gmail_fetch._parse_one_email(raw)[0]["published"]
    assert when is None  # undated -> rank will discard; never crashes


def test_sender_display_used_in_source_name():
    raw = _raw_email(html=TLDR_HTML, sender="The Rundown AI <rundown@example.com>")
    assert gmail_fetch._parse_one_email(raw)[0]["source_name"] == "Newsletter: The Rundown AI"


# --- failure paths: ALWAYS non-fatal, never raise ----------------------

def test_undecodable_body_returns_empty():
    out = gmail_fetch._parse_one_email(b"\x00\x01\x02 not a real message \xff\xfe")
    assert out == []


def test_missing_env_returns_empty():
    # _imap_connect checks creds BEFORE opening a socket -> no network here.
    os.environ.pop(config.GMAIL_ADDRESS_ENV, None)
    os.environ.pop(config.GMAIL_APP_PASSWORD_ENV, None)
    assert gmail_fetch.fetch_newsletter_stories() == []


def test_login_failure_returns_empty_clean_through_finally():
    # STEP [14] item (3): imap stays None on the login-failure path; finally cleanup
    # must NOT raise, so the non-fatal guarantee holds. Patches _imap_connect to fail.
    os.environ[config.GMAIL_ADDRESS_ENV] = "x@gmail.com"
    os.environ[config.GMAIL_APP_PASSWORD_ENV] = "wrong"
    orig = gmail_fetch._imap_connect

    def boom():
        raise OSError("auth failure")

    gmail_fetch._imap_connect = boom
    orig_sleep = retryutil.sleep                                       # STEP [26]
    retryutil.sleep = lambda _s: None   # STEP [26] retried now -> keep the suite fast
    try:
        out = gmail_fetch.fetch_newsletter_stories()  # must not raise
        assert out == []
    finally:
        gmail_fetch._imap_connect = orig
        retryutil.sleep = orig_sleep                                   # STEP [26]
        os.environ.pop(config.GMAIL_ADDRESS_ENV, None)
        os.environ.pop(config.GMAIL_APP_PASSWORD_ENV, None)


def test_empty_label_returns_empty():
    # Fake IMAP (no network): label select returns count 0 -> [].
    class FakeIMAP:
        def select(self, box):
            return ("OK", [b"0"])
        def close(self):
            pass
        def logout(self):
            pass

    os.environ[config.GMAIL_ADDRESS_ENV] = "x@gmail.com"
    os.environ[config.GMAIL_APP_PASSWORD_ENV] = "y"
    orig = gmail_fetch._imap_connect
    gmail_fetch._imap_connect = lambda: FakeIMAP()
    try:
        assert gmail_fetch.fetch_newsletter_stories() == []
    finally:
        gmail_fetch._imap_connect = orig
        os.environ.pop(config.GMAIL_ADDRESS_ENV, None)
        os.environ.pop(config.GMAIL_APP_PASSWORD_ENV, None)


# --- end-to-end through the EXISTING rank/dedupe -----------------------

def test_newsletter_dupe_collapses_against_rss():
    rss = {"source_id": 2, "source_name": "Google AI Blog", "priority": 8,
           "title": "Anthropic Releases Fable 5 With Improved Reasoning",
           "link": "https://blog.google/x", "summary": "", "published": NOW,
           "image_url": None}
    nl = _nl("Anthropic releases Fable 5 with improved reasoning")  # same normalized title
    top = rank.dedupe_and_rank([rss, nl], {"hashes": {}}, now=NOW)
    assert len(top) == 1, f"dupe should collapse to 1, got {len(top)}"
    assert top[0]["source_id"] == 2  # higher-priority RSS copy wins


def test_newsletter_unique_story_ranks_alongside_rss():
    rss = {"source_id": 2, "source_name": "Google AI Blog", "priority": 8,
           "title": "Gemini context window expanded again", "link": "https://g/x",
           "summary": "", "published": NOW, "image_url": None}
    nl = _nl("OpenAI announces a brand new Codex agent feature")  # distinct title
    top = rank.dedupe_and_rank([rss, nl], {"hashes": {}}, now=NOW)
    assert len(top) == 2, f"both should survive, got {len(top)}"
    assert any(s["source_id"] == config.NEWSLETTER_SOURCE_ID for s in top)


def test_newsletter_layer_capped_at_two_in_top():
    # STEP [14] decision: shared source_id 18 -> MAX_PER_SOURCE_IN_TOP caps the
    # whole newsletter layer at 2, even if 4 distinct newsletter stories qualify.
    nls = [_nl(f"Newsletter scoop number {tag}") for tag in ("alpha", "beta", "gamma", "delta")]
    top = rank.dedupe_and_rank(nls, {"hashes": {}}, now=NOW)
    assert len(top) == 2, f"newsletter layer should be capped at 2, got {len(top)}"
    assert all(s["source_id"] == config.NEWSLETTER_SOURCE_ID for s in top)



# ---------------------------------------------------------------------------
# STEP [35] Title quality. Modelled on the real beehiiv issue that exposed this:
# STEP [35] one email yielded 8 "stories" of which 3 were headlines and the rest
# STEP [35] were mid-sentence fragments, CTA buttons, ads and poll widgets.
# ---------------------------------------------------------------------------
BEEHIIV_HTML = """<html><body>
<h2><a href="https://nl.example/story1">Anthropic ships Fable 5.1 with better reasoning</a></h2>
<p><span>The new model <a href="https://nl.example/inline1">came out ahead of Opus 5 on every benchmark</a>
that was published this week.</span></p>
<h2><a href="https://nl.example/story2">OpenAI model hits a Critical Cyber capability limit</a></h2>
<p><span>Red-teamers <a href="https://nl.example/inline2">found two unknown flaws</a> in the release.</span></p>
<div><a href="https://nl.example/cta">Try Fable 5.1 Now</a></div>
<div><a href="https://nl.example/ad">Book more calls with Aimfox Avatars</a></div>
<div><a href="https://nl.example/poll">Absolute fire, loved this issue</a></div>
<div><a href="https://nl.example/footer">Powered by beehiiv today</a></div>
</body></html>"""

# Same content, headline markup removed — the Vaibhav Sisinty / TLDR shape.
NO_MARKUP_HTML = """<html><body>
<div><a href="https://nl.example/story1">Anthropic ships Fable 5.1 with better reasoning</a></div>
<div><a href="https://nl.example/inline1">came out ahead of Opus 5 on every benchmark</a></div>
<div><a href="https://nl.example/inline2">found two unknown flaws in the release</a></div>
<div><a href="https://nl.example/frag">the falls (and the fire) explained</a></div>
<div><a href="https://nl.example/cta">Watch the on-demand recording</a></div>
<div><a href="https://nl.example/ad">Book more calls with Aimfox Avatars</a></div>
<div><a href="https://nl.example/money">$35B cloud deal with Nvidia-backed Lambda</a></div>
<div><a href="https://nl.example/keep">Apple Watch gets an AI upgrade this autumn</a></div>
</body></html>"""


def test_headline_markup_tier_wins():
    """A newsletter that marks its headlines: trust the markup, drop the rest."""
    titles = [s["title"] for s in
              gmail_fetch._extract_links(BEEHIIV_HTML, "Beehiiv NL", NOW)]
    assert titles == [
        "Anthropic ships Fable 5.1 with better reasoning",
        "OpenAI model hits a Critical Cyber capability limit",
    ], titles


def test_headline_tier_drops_inline_fragments_and_ads():
    titles = [s["title"] for s in
              gmail_fetch._extract_links(BEEHIIV_HTML, "Beehiiv NL", NOW)]
    for bad in ("came out ahead of Opus 5 on every benchmark",
                "found two unknown flaws",
                "Try Fable 5.1 Now",
                "Book more calls with Aimfox Avatars",
                "Absolute fire, loved this issue"):
        assert bad not in titles, f"'{bad}' survived the headline tier"


def test_no_markup_falls_back_to_text_heuristic():
    """No headline markup anywhere -> tier 2 keeps the plain-<div>/<li> layouts
    (TLDR, The Rundown, Vaibhav Sisinty) working instead of zeroing them out."""
    titles = [s["title"] for s in
              gmail_fetch._extract_links(NO_MARKUP_HTML, "Plain NL", NOW)]
    assert "Anthropic ships Fable 5.1 with better reasoning" in titles, titles
    assert len(titles) == 3, titles


def test_lowercase_start_dropped_as_fragment():
    titles = [s["title"] for s in
              gmail_fetch._extract_links(NO_MARKUP_HTML, "Plain NL", NOW)]
    for bad in ("came out ahead of Opus 5 on every benchmark",
                "found two unknown flaws in the release",
                "the falls (and the fire) explained"):
        assert bad not in titles, f"fragment '{bad}' was not dropped"


def test_cta_prefix_matches_first_word_only():
    """'Watch the recording' is a button; 'Apple Watch gets...' is a headline.
    A substring match would kill both — hence first-word-only."""
    titles = [s["title"] for s in
              gmail_fetch._extract_links(NO_MARKUP_HTML, "Plain NL", NOW)]
    assert "Watch the on-demand recording" not in titles, titles
    assert "Book more calls with Aimfox Avatars" not in titles, titles
    assert "Apple Watch gets an AI upgrade this autumn" in titles, titles


def test_non_letter_first_char_survives():
    """islower() is the test, not isupper() — a $-led headline is still a headline."""
    titles = [s["title"] for s in
              gmail_fetch._extract_links(NO_MARKUP_HTML, "Plain NL", NOW)]
    assert "$35B cloud deal with Nvidia-backed Lambda" in titles, titles


def test_looks_like_headline_unit():
    keep = ("Anthropic ships Fable 5.1", "$35B cloud deal with Lambda",
            "Apple Watch gets an AI upgrade", "3 models worth watching")
    drop = ("came out ahead of Opus 5", "the falls (and the fire)",
            "Watch the recording", "Try Fable 5.1 Now", "Book a demo now", "")
    for t in keep:
        assert gmail_fetch._looks_like_headline(t), f"should keep: {t!r}"
    for t in drop:
        assert not gmail_fetch._looks_like_headline(t), f"should drop: {t!r}"


def test_void_tags_do_not_unwind_the_ancestor_stack():
    """<br> arrives both bare and self-closing in email HTML. An unguarded pop
    would unwind the stack hunting a 'br' that was never pushed, so a later
    heading anchor would lose its <h2> ancestor and be misread as plain."""
    html = ('<html><body><h2>Section<br>title'
            '<a href="https://nl.example/a">A properly marked headline here</a>'
            '</h2><img src="x.png"><div>'
            '<a href="https://nl.example/b">plain lowercase fragment link</a>'
            '</div></body></html>')
    titles = [s["title"] for s in gmail_fetch._extract_links(html, "S", NOW)]
    assert titles == ["A properly marked headline here"], titles


def test_stray_close_tag_does_not_break_extraction():
    html = ('<html><body></span></div>'
            '<h3><a href="https://nl.example/a">Headline after stray closers</a></h3>'
            '</body></html>')
    titles = [s["title"] for s in gmail_fetch._extract_links(html, "S", NOW)]
    assert titles == ["Headline after stray closers"], titles


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
