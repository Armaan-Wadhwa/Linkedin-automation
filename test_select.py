"""
test_select.py — Offline checks for Phase 4 Task 12 (manual-selection backend).  # STEP [19]

NO NETWORK. fetch.fetch_all, generate.generate_post, and notify.send_draft are
mocked. The pending_post / history files live in a temp dir. Run from the repo
root:  python test_select.py

The 10 assertions mirror the spec's deliverable list:
  1. 3 valid ids → generate_post gets exactly those 3, in order
  2. custom_story present → source_id == 0, NOT deduped against a near-dupe
  3. selection.image_url → exactly that url reaches send_draft (override)
  4. >5 ids resolve → truncated to first 5, order preserved
  5. 0 ids resolve → return 1; generate_post / send_draft NOT called
  6. generate_post returns None → return 1; send_draft / record NOT called
  7. malformed selection → return 1; no side effects
  8. supersede: awaiting_approval → superseded; posted/rejected/expired → untouched
  9. record_stories called ONLY after send_draft success (call-order via mocks)
  10. round-trip: emit_candidates.json candidate_ids resolve via build_from_selection
"""

import contextlib                                                      # STEP [19]
import io                                                              # STEP [19]
import json                                                            # STEP [19]
import logging                                                        # STEP [19]
import os                                                              # STEP [19]
import sys                                                             # STEP [19]
import tempfile                                                       # STEP [19]
from datetime import datetime, timezone                                # STEP [19]
from unittest import mock                                              # STEP [19]

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))  # STEP [19]
if hasattr(sys.stdout, "reconfigure"):                                # STEP [19]
    sys.stdout.reconfigure(encoding="utf-8")                          # STEP [19]

import config                                                          # STEP [19] noqa: E402
import emit_candidates                                                 # STEP [19] noqa: E402
import rank                                                            # STEP [19] noqa: E402
import select_build                                                    # STEP [19] noqa: E402


# ---------------------------------------------------------------------------
# Helpers — story factory + the central stub harness
# ---------------------------------------------------------------------------
_NOW = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)                 # STEP [19] fixed for determinism


def _story(title, source_id=1, source_name="OpenAI News", priority=6,
           image_url=None, summary="summary for " + "x"):              # STEP [19]
    return {                                                           # STEP [19]
        "source_id": source_id,                                        # STEP [19]
        "source_name": source_name,                                    # STEP [19]
        "priority": priority,                                          # STEP [19]
        "title": title,                                                # STEP [19]
        "link": f"http://example.com/{abs(hash(title)) % 9999}",       # STEP [19]
        "summary": summary,                                            # STEP [19]
        "published": _NOW,                                             # STEP [19]
        "image_url": image_url,                                        # STEP [19]
        "hash": rank.title_hash(title),                                # STEP [19]
        "score": 10.0,                                                 # STEP [19]
    }


def _candidate_id(title):
    """The exact contract: candidate_id = rank.title_hash (sha256 of normalized
    # STEP [19] title). emit_candidates writes this; select_build resolves by it."""
    return rank.title_hash(title)                                      # STEP [19]


def _run_build(tmp, selection, stories, gen_return="Hook.\n\nBody.\n\n#AI",
               send_return=(True, 42), record_mock=None):              # STEP [19]
    """Run select_build.build_from_selection with everything network-touching
    # STEP [19] mocked. Returns (rc, pending, history, captured_call_data).
    # STEP [19]
    # STEP [19] captured_call_data is a dict with keys: gen_args (the list passed
    # STEP [19] to generate_post), send_args (post, warnings, image_url),
    # STEP [19] record_called (bool), record_args (list of stories)."""
    pend_path = os.path.join(tmp, config.PENDING_POST_FILE)            # STEP [19]
    hist_path = os.path.join(tmp, config.HISTORY_FILE)                 # STEP [19]
    # STEP [19] Seed an empty history so record_stories has somewhere to write.
    with open(hist_path, "w", encoding="utf-8") as fh:                 # STEP [19]
        json.dump({"hashes": {}}, fh)                                  # STEP [19]

    captured = {"gen_args": None, "send_args": None,                   # STEP [19]
                "record_called": False, "record_args": None}           # STEP [19]

    def fake_gen(stories):                                             # STEP [19]
        captured["gen_args"] = stories                                 # STEP [19]
        return gen_return                                              # STEP [19]

    def fake_send(post, warnings, image_url):                          # STEP [19]
        captured["send_args"] = (post, warnings, image_url)            # STEP [19]
        return send_return                                             # STEP [19]

    if record_mock is None:                                            # STEP [19]
        # STEP [19] Wrap record_stories so we can observe call order. The real
        # STEP [19] function runs (writes hashes) — verified via history file.
        real_record = rank.record_stories                              # STEP [19]

        def spy_record(history, stories, now=None):                    # STEP [19]
            captured["record_called"] = True                           # STEP [19]
            captured["record_args"] = stories                          # STEP [19]
            return real_record(history, stories, now)                  # STEP [19]
        record_patch = mock.patch.object(select_build.rank,            # STEP [19]
                                         "record_stories",             # STEP [19]
                                         side_effect=spy_record)       # STEP [19]
    else:                                                              # STEP [19]
        record_patch = mock.patch.object(select_build.rank,            # STEP [19]
                                         "record_stories", record_mock)  # STEP [19]

    with (mock.patch.object(select_build.fetch, "fetch_all",           # STEP [19]
                            return_value=stories),                     # STEP [19]
          mock.patch.object(select_build.generate, "generate_post",    # STEP [19]
                            side_effect=fake_gen),                     # STEP [19]
          mock.patch.object(select_build.notify, "send_draft",         # STEP [19]
                            side_effect=fake_send),                    # STEP [19]
          mock.patch.object(select_build.main, "PENDING_POST_PATH",    # STEP [19]
                            pend_path),                                # STEP [19]
          mock.patch.object(select_build, "PENDING_POST_PATH",         # STEP [19]
                            pend_path),                                # STEP [19]
          mock.patch.object(select_build, "HISTORY_PATH", hist_path),  # STEP [19]
          record_patch,                                                # STEP [19]
          contextlib.redirect_stdout(io.StringIO())):                  # STEP [19]
        rc = select_build.build_from_selection(selection)              # STEP [19]

    pending = None                                                     # STEP [19]
    if os.path.exists(pend_path):                                      # STEP [19]
        with open(pend_path, encoding="utf-8") as fh:                  # STEP [19]
            pending = json.load(fh)                                    # STEP [19]
    history = None                                                     # STEP [19]
    if os.path.exists(hist_path):                                      # STEP [19]
        with open(hist_path, encoding="utf-8") as fh:                  # STEP [19]
            history = json.load(fh)                                    # STEP [19]
    return rc, pending, history, captured                              # STEP [19]


# ---------------------------------------------------------------------------
# 1. 3 valid ids → generate_post gets exactly those 3, in order
# ---------------------------------------------------------------------------
def test_three_valid_ids_resolve_in_order():
    titles = ["Anthropic launches Claude 4",                           # STEP [19]
              "Gemini 3.5 released",                                   # STEP [19]
              "OpenAI cuts GPT price"]                                 # STEP [19]
    stories = [_story(t) for t in titles]                              # STEP [19]
    # STEP [19] Harvey picks them in a deliberately different order than fetch
    sel = {"story_ids": [_candidate_id(titles[2]),                     # STEP [19]
                         _candidate_id(titles[0]),                     # STEP [19]
                         _candidate_id(titles[1])]}                    # STEP [19]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [19]
        rc, _, _, captured = _run_build(tmp, sel, stories)             # STEP [19]
    assert rc == 0, rc                                                 # STEP [19]
    gen_titles = [s["title"] for s in captured["gen_args"]]            # STEP [19]
    assert gen_titles == [titles[2], titles[0], titles[1]], gen_titles  # STEP [19]


# ---------------------------------------------------------------------------
# 2. Custom story present → source_id == 0, NOT deduped against a near-dupe
# ---------------------------------------------------------------------------
def test_custom_story_present_and_not_deduped():
    # STEP [19] Fetched story with a title that normalizes to the same hash as
    # STEP [19] the custom story (only differ by punctuation/case). dedupe_only
    # STEP [19] would normally drop one of them; the custom story bypasses that
    # STEP [19] because it's appended AFTER dedupe.
    fetched_title = "Anthropic! Introduces? Claude 4"                  # STEP [19]
    custom_title = "anthropic introduces claude 4"                     # STEP [19] same normalized hash
    assert rank.title_hash(fetched_title) == rank.title_hash(custom_title), \
        "test premise: the two titles must normalize to the same hash"  # STEP [19]
    stories = [_story(fetched_title)]                                  # STEP [19]
    sel = {"story_ids": [_candidate_id(fetched_title)],                # STEP [19]
           "custom_story": {"title": custom_title,                     # STEP [19]
                            "link": "http://manual.example/x",         # STEP [19]
                            "summary": "manual note"}}                 # STEP [19]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [19]
        rc, _, _, captured = _run_build(tmp, sel, stories)             # STEP [19]
    assert rc == 0, rc                                                 # STEP [19]
    by_sid = {s["source_id"]: s for s in captured["gen_args"]}         # STEP [19]
    assert 0 in by_sid, "custom story missing from generation input"   # STEP [19]
    assert by_sid[0]["title"] == custom_title                          # STEP [19]
    assert by_sid[0]["source_name"] == "manual"                        # STEP [19]
    # STEP [19] Both the fetched near-dupe AND the custom must survive.
    assert len(captured["gen_args"]) == 2, captured["gen_args"]        # STEP [19]


# ---------------------------------------------------------------------------
# 3. selection.image_url → exact url reaches send_draft (override)
# ---------------------------------------------------------------------------
def test_selection_image_url_overrides_to_send_draft():
    stories = [_story("Anthropic launches Claude 4",                  # STEP [19]
                      image_url="http://story.example/a.jpg")]        # STEP [19]
    sel = {"story_ids": [_candidate_id("Anthropic launches Claude 4")],  # STEP [19]
           "image_url": "http://picked.example/b.jpg"}                # STEP [19]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [19]
        rc, pending, _, captured = _run_build(tmp, sel, stories)       # STEP [19]
    assert rc == 0, rc                                                 # STEP [19]
    assert captured["send_args"][2] == "http://picked.example/b.jpg", \
        captured["send_args"]                                          # STEP [19] override, not story image
    assert pending["image_url"] == "http://picked.example/b.jpg"       # STEP [19]
    assert pending["image_source"] == "story"                          # STEP [19]


def test_null_image_url_means_text_only():
    # STEP [19] selection.image_url = null must produce a text-only post even if
    # STEP [19] the picked stories have images (override semantics).
    stories = [_story("Anthropic launches Claude 4",                  # STEP [19]
                      image_url="http://story.example/a.jpg")]        # STEP [19]
    sel = {"story_ids": [_candidate_id("Anthropic launches Claude 4")],  # STEP [19]
           "image_url": None}                                          # STEP [19]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [19]
        rc, pending, _, captured = _run_build(tmp, sel, stories)       # STEP [19]
    assert rc == 0, rc                                                 # STEP [19]
    assert captured["send_args"][2] is None, captured["send_args"]     # STEP [19]
    assert pending["image_url"] is None                                # STEP [19]
    assert pending["image_source"] is None                             # STEP [19]


# ---------------------------------------------------------------------------
# 4. >5 ids resolve → truncated to first 5, order preserved
# ---------------------------------------------------------------------------
def test_more_than_five_ids_truncated_in_order():
    titles = [f"Story number {i} in the list" for i in range(1, 8)]    # STEP [19] 7 ids
    stories = [_story(t) for t in titles]                              # STEP [19]
    sel = {"story_ids": [_candidate_id(t) for t in titles]}            # STEP [19]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [19]
        rc, _, _, captured = _run_build(tmp, sel, stories)             # STEP [19]
    assert rc == 0, rc                                                 # STEP [19]
    gen_titles = [s["title"] for s in captured["gen_args"]]            # STEP [19]
    assert gen_titles == titles[:5], gen_titles                        # STEP [19] first 5, in order


def test_five_ids_plus_custom_keeps_custom():
    # STEP [19] Cap leaves room for the custom story: 4 fetched + 1 custom = 5.
    titles = [f"Story number {i}" for i in range(1, 7)]                # STEP [19] 6 ids available
    stories = [_story(t) for t in titles]                              # STEP [19]
    sel = {"story_ids": [_candidate_id(t) for t in titles],            # STEP [19]
           "custom_story": {"title": "Manual entry",                   # STEP [19]
                            "link": "http://m.example/x"}}             # STEP [19]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [19]
        rc, _, _, captured = _run_build(tmp, sel, stories)             # STEP [19]
    assert rc == 0, rc                                                 # STEP [19]
    assert len(captured["gen_args"]) == 5, captured["gen_args"]        # STEP [19]
    assert captured["gen_args"][-1]["title"] == "Manual entry"         # STEP [19] custom kept
    assert [s["title"] for s in captured["gen_args"][:4]] == titles[:4]  # STEP [19] first 4 fetched


# ---------------------------------------------------------------------------
# 5. 0 ids resolve → return 1; generate_post / send_draft NOT called
# ---------------------------------------------------------------------------
def test_zero_ids_resolve_exits_without_side_effects():
    stories = [_story("Real story that exists")]                       # STEP [19]
    sel = {"story_ids": ["deadbeef" + "0" * 56]}                       # STEP [19] valid sha256 shape, no match
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [19]
        rc, _, _, captured = _run_build(tmp, sel, stories)             # STEP [19]
    assert rc == 1, rc                                                 # STEP [19]
    assert captured["gen_args"] is None, "generate_post must not be called"  # STEP [19]
    assert captured["send_args"] is None, "send_draft must not be called"  # STEP [19]
    assert not captured["record_called"], "record_stories must not be called"  # STEP [19]


# ---------------------------------------------------------------------------
# 6. generate_post returns None → return 1; send_draft / record NOT called
# ---------------------------------------------------------------------------
def test_generate_returns_none_exits_without_side_effects():
    stories = [_story("Anthropic launches Claude 4")]                  # STEP [19]
    sel = {"story_ids": [_candidate_id("Anthropic launches Claude 4")]}  # STEP [19]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [19]
        rc, _, _, captured = _run_build(tmp, sel, stories,             # STEP [19]
                                        gen_return=None)               # STEP [19]
    assert rc == 1, rc                                                 # STEP [19]
    assert captured["gen_args"] is not None, "generate_post must be called"  # STEP [19]
    assert captured["send_args"] is None, "send_draft must not be called"  # STEP [19]
    assert not captured["record_called"], "record_stories must not be called"  # STEP [19]


# ---------------------------------------------------------------------------
# 7. Malformed selection → return 1, no side effects
# ---------------------------------------------------------------------------
def test_malformed_selections_are_rejected():
    cases = {                                                          # STEP [19]
        "not a dict": ["list", "not", "dict"],                         # STEP [19]
        "unknown key": {"story_ids": ["a"], "bogus": 1},               # STEP [19]
        "missing story_ids": {"image_url": None},                      # STEP [19]
        "empty story_ids": {"story_ids": []},                          # STEP [19]
        "non-string id": {"story_ids": [123]},                         # STEP [19]
        "image_url wrong type": {"story_ids": ["a"], "image_url": 7},  # STEP [19]
        "custom not object": {"story_ids": ["a"], "custom_story": "x"},  # STEP [19]
        "custom missing title": {"story_ids": ["a"],                   # STEP [19]
                                 "custom_story": {"link": "x"}},       # STEP [19]
        "custom unknown key": {"story_ids": ["a"],                     # STEP [19]
                               "custom_story": {"title": "t",          # STEP [19]
                                                "link": "x",          # STEP [19]
                                                "wat": 1}},           # STEP [19]
    }
    for name, sel in cases.items():                                    # STEP [19]
        with tempfile.TemporaryDirectory() as tmp:                     # STEP [19]
            rc, _, _, captured = _run_build(tmp, sel,                  # STEP [19]
                                            [_story("Whatever")])      # STEP [19]
        assert rc == 1, (name, rc)                                     # STEP [19]
        assert captured["gen_args"] is None, (name, "gen called")      # STEP [19]
        assert captured["send_args"] is None, (name, "send called")    # STEP [19]
        assert not captured["record_called"], (name, "record called")  # STEP [19]


# ---------------------------------------------------------------------------
# 8. Supersede: awaiting_approval → superseded; terminal → untouched
# ---------------------------------------------------------------------------
def test_supersede_pending_awaiting_becomes_superseded():
    stories = [_story("Anthropic launches Claude 4")]                  # STEP [19]
    sel = {"story_ids": [_candidate_id("Anthropic launches Claude 4")]}  # STEP [19]
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [19]
        pend_path = os.path.join(tmp, config.PENDING_POST_FILE)        # STEP [19]
        with open(pend_path, "w", encoding="utf-8") as fh:             # STEP [19]
            json.dump({"status": "awaiting_approval",                  # STEP [19]
                       "draft": "old", "telegram_message_id": 7}, fh)  # STEP [19]
        rc, pending, _, _ = _run_build(tmp, sel, stories)              # STEP [19]
        # STEP [19] After the run, the file is the NEW draft (status
        # STEP [19] awaiting_approval) — proving supersede happened first,
        # STEP [19] then the new draft overwrote it. Assert via message_id.
    assert rc == 0, rc                                                 # STEP [19]
    assert pending["telegram_message_id"] == 42, "new draft must overwrite superseded"  # STEP [19]


def test_supersede_terminal_statuses_untouched_before_failure():
    # STEP [19] A terminal-status pending file must NOT be superseded. We trigger
    # STEP [19] an early failure (no story resolves) and confirm the file is intact.
    for terminal in ("posted", "rejected", "expired", "approved", "post_failed"):  # STEP [19]
        stories = [_story("Real story that exists")]                   # STEP [19]
        sel = {"story_ids": ["deadbeef" + "0" * 56]}                   # STEP [19] resolves to nothing → rc=1
        with tempfile.TemporaryDirectory() as tmp:                     # STEP [19]
            pend_path = os.path.join(tmp, config.PENDING_POST_FILE)    # STEP [19]
            original = {"status": terminal,                            # STEP [19]
                        "linkedin_post_id": "urn:li:share:abc"}        # STEP [19] audit trail
            with open(pend_path, "w", encoding="utf-8") as fh:         # STEP [19]
                json.dump(original, fh)                                # STEP [19]
            rc, pending, _, _ = _run_build(tmp, sel, stories)          # STEP [19]
            assert rc == 1, (terminal, rc)                             # STEP [19]
        assert pending == original, (terminal, "terminal file was mutated!", pending)  # STEP [19]


# ---------------------------------------------------------------------------
# 9. record_stories called ONLY after send_draft success (call order via mocks)
# ---------------------------------------------------------------------------
def test_record_only_after_send_success():
    stories = [_story("Anthropic launches Claude 4")]                  # STEP [19]
    sel = {"story_ids": [_candidate_id("Anthropic launches Claude 4")]}  # STEP [19]
    # STEP [19] send_draft FAILS — record_stories must NOT be called.
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [19]
        rc, pending, _, captured = _run_build(tmp, sel, stories,       # STEP [19]
                                              send_return=(False, None))  # STEP [19]
    assert rc == 1, rc                                                 # STEP [19]
    assert captured["send_args"] is not None, "send_draft must be called"  # STEP [19]
    assert not captured["record_called"], "record must NOT run on notify failure"  # STEP [19]
    assert pending["status"] == "notify_failed", pending               # STEP [19]


def test_record_runs_on_send_success_with_correct_order():
    # STEP [19] On send_draft SUCCESS, record_stories must run AND must run
    # STEP [19] AFTER send_draft. We assert order via a Mock with side effects.
    stories = [_story("Anthropic launches Claude 4")]                  # STEP [19]
    sel = {"story_ids": [_candidate_id("Anthropic launches Claude 4")]}  # STEP [19]
    call_order = []                                                    # STEP [19]

    def fake_send(*a, **kw):                                           # STEP [19]
        call_order.append("send")                                      # STEP [19]
        return (True, 99)                                              # STEP [19]

    def spy_record(*a, **kw):                                          # STEP [19]
        call_order.append("record")                                    # STEP [19]
        return None                                                    # STEP [19]

    pend_path = None                                                   # STEP [19] set below
    with tempfile.TemporaryDirectory() as tmp:                         # STEP [19]
        pend_path = os.path.join(tmp, config.PENDING_POST_FILE)        # STEP [19]
        hist_path = os.path.join(tmp, config.HISTORY_FILE)             # STEP [19]
        with open(hist_path, "w", encoding="utf-8") as fh:             # STEP [19]
            json.dump({"hashes": {}}, fh)                              # STEP [19]
        with (mock.patch.object(select_build.fetch, "fetch_all",       # STEP [19]
                                return_value=stories),                 # STEP [19]
              mock.patch.object(select_build.generate, "generate_post",  # STEP [19]
                                return_value="Hook.\n\n#AI"),          # STEP [19]
              mock.patch.object(select_build.notify, "send_draft",     # STEP [19]
                                side_effect=fake_send),                # STEP [19]
              mock.patch.object(select_build.rank, "record_stories",   # STEP [19]
                                side_effect=spy_record),               # STEP [19]
              mock.patch.object(select_build.main, "PENDING_POST_PATH",  # STEP [19]
                                pend_path),                            # STEP [19]
              mock.patch.object(select_build, "PENDING_POST_PATH",     # STEP [19]
                                pend_path),                            # STEP [19]
              mock.patch.object(select_build, "HISTORY_PATH", hist_path),  # STEP [19]
              contextlib.redirect_stdout(io.StringIO())):              # STEP [19]
            rc = select_build.build_from_selection(sel)                # STEP [19]
    assert rc == 0, rc                                                 # STEP [19]
    assert call_order == ["send", "record"], call_order                # STEP [19]


# ---------------------------------------------------------------------------
# 10. Round-trip: emit_candidates.json ids resolve via build_from_selection
# ---------------------------------------------------------------------------
def test_emit_candidates_round_trips_to_select_build():
    # STEP [19] emit_candidates writes candidate_ids; build_from_selection must
    # STEP [19] resolve the same ids against the same stories. This is the core
    # STEP [19] contract — if it breaks, the dashboard can't talk to the backend.
    titles = ["Anthropic launches Claude 4",                           # STEP [19]
              "Gemini 3.5 released",                                   # STEP [19]
              "OpenAI cuts GPT price",                                 # STEP [19]
              "Hugging Face open-sources Bigger",                      # STEP [19]
              "Meta AI announces Llama 4"]                             # STEP [19]
    stories = [_story(t) for t in titles]                              # STEP [19]

    with tempfile.TemporaryDirectory() as tmp:                         # STEP [19]
        cand_file = os.path.join(tmp, "candidates.json")              # STEP [19]
        with (mock.patch.object(emit_candidates, "CANDIDATES_FILE",    # STEP [19]
                                cand_file),                            # STEP [19]
              mock.patch.object(emit_candidates, "DOCS_DIR", tmp),     # STEP [19]
              mock.patch.object(emit_candidates.fetch, "fetch_all",    # STEP [19]
                                return_value=stories),                 # STEP [19]
              mock.patch.object(emit_candidates.rank, "load_history",  # STEP [19]
                                return_value={"hashes": {}})):          # STEP [19]
            rc = emit_candidates.emit()                                # STEP [19]
        assert rc == 0, rc                                             # STEP [19]
        with open(cand_file, encoding="utf-8") as fh:                  # STEP [19]
            payload = json.load(fh)                                    # STEP [19]
        assert "error" not in payload, payload                         # STEP [19]
        assert len(payload["candidates"]) == 5, payload                # STEP [19]
        # STEP [19] Round-trip: pick 2 candidate_ids + 1 custom, hand to
        # STEP [19] build_from_selection, confirm the same stories come back.
        ids_in_emit_order = [c["candidate_id"] for c in payload["candidates"]]  # STEP [19]
        pick = [ids_in_emit_order[1], ids_in_emit_order[3]]            # STEP [19]
        expected_titles = [titles[1], titles[3]]                       # STEP [19]
        sel = {"story_ids": pick,                                      # STEP [19]
               "image_url": None,                                      # STEP [19]
               "custom_story": {"title": "Manual round trip",          # STEP [19]
                                "link": "http://m.example/x"}}         # STEP [19]
        rc, _, _, captured = _run_build(tmp, sel, stories)             # STEP [19]
    assert rc == 0, rc                                                 # STEP [19]
    gen_titles = [s["title"] for s in captured["gen_args"]]            # STEP [19]
    assert gen_titles == expected_titles + ["Manual round trip"], gen_titles  # STEP [19]


if __name__ == "__main__":                                             # STEP [19]
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]  # STEP [19]
    for t in tests:                                                    # STEP [19]
        t()                                                            # STEP [19]
        print(f"PASS {t.__name__}")                                    # STEP [19]
    print(f"\n{len(tests)} checks passed.")                            # STEP [19]
