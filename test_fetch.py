"""
test_fetch.py — stubbed checks for Phase 3 Task 8a (feed image extraction).

No network: _extract_image is tested directly with dict entries that mimic
feedparser's structure. Run from the repo root:  python test_fetch.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import fetch  # noqa: E402


def _entry(**kw):
    """Minimal feedparser-like entry (a plain dict with .get support)."""
    base = {"title": "Test", "link": "http://x", "summary": ""}
    base.update(kw)
    return base


# --- media:content ---------------------------------------------------------

def test_media_content_image_type():
    e = _entry(media_content=[{"url": "http://img/a.jpg", "type": "image/jpeg"}])
    assert fetch._extract_image(e) == "http://img/a.jpg"


def test_media_content_untyped_accepted():
    e = _entry(media_content=[{"url": "http://img/a.jpg"}])
    assert fetch._extract_image(e) == "http://img/a.jpg"


def test_media_content_non_image_skipped():
    e = _entry(media_content=[{"url": "http://vid/v.mp4", "type": "video/mp4"}])
    assert fetch._extract_image(e) is None


# --- media:thumbnail -------------------------------------------------------

def test_media_thumbnail():
    e = _entry(media_thumbnail=[{"url": "http://img/t.jpg", "width": 320}])
    assert fetch._extract_image(e) == "http://img/t.jpg"


# --- enclosure -------------------------------------------------------------

def test_enclosure_image():
    e = _entry(links=[{"rel": "enclosure", "href": "http://img/e.png",
                        "type": "image/png"}])
    assert fetch._extract_image(e) == "http://img/e.png"


def test_enclosure_non_image_skipped():
    e = _entry(links=[{"rel": "enclosure", "href": "http://x/podcast.mp3",
                        "type": "audio/mpeg"}])
    assert fetch._extract_image(e) is None


# --- inline <img> ----------------------------------------------------------

def test_img_in_content():
    e = _entry(content=[{"value": '<p>hi</p><img src="http://img/c.jpg">'}])
    assert fetch._extract_image(e) == "http://img/c.jpg"


def test_img_in_summary():
    e = _entry(summary='<div><img src="http://img/s.jpg" alt="x"></div>')
    assert fetch._extract_image(e) == "http://img/s.jpg"


def test_img_single_quote_src():
    e = _entry(summary="<img src='http://img/q.jpg'>")
    assert fetch._extract_image(e) == "http://img/q.jpg"


# --- none / priority -------------------------------------------------------

def test_no_image_anywhere():
    e = _entry(summary="plain text, no images here")
    assert fetch._extract_image(e) is None


def test_media_content_beats_inline_img():
    e = _entry(media_content=[{"url": "http://img/media.jpg", "type": "image/jpeg"}],
               summary='<img src="http://img/inline.jpg">')
    assert fetch._extract_image(e) == "http://img/media.jpg"


def test_thumbnail_beats_enclosure():
    e = _entry(media_thumbnail=[{"url": "http://img/thumb.jpg"}],
               links=[{"rel": "enclosure", "href": "http://img/enc.png",
                        "type": "image/png"}])
    assert fetch._extract_image(e) == "http://img/thumb.jpg"


# --- malformed / crash-proof -----------------------------------------------

def test_malformed_media_content_no_crash():
    e = _entry(media_content=["not-a-dict", {"no_url": True}, None])
    assert fetch._extract_image(e) is None


def test_malformed_links_no_crash():
    e = _entry(links=["garbage", 42, {"rel": "enclosure"}])
    assert fetch._extract_image(e) is None


def test_empty_entry():
    assert fetch._extract_image(_entry()) is None


def test_completely_broken_entry():
    assert fetch._extract_image({}) is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} checks passed.")
